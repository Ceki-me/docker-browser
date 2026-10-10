"""SDK Provider Daemon — one provider-WS per schedule, one Chrome per match.

The daemon replaces the "one provider = one long-lived Chrome held by app.py"
model with a thin WS multiplexor + process manager:

  * exactly ONE provider WS connection to the relay is held for the schedule
    (the same provider protocol app.py speaks today);
  * a ``match`` from the relay spawns a dedicated Chrome instance — its own
    Xvfb display, its own CDP port, its own profile dir under ``/sessions``
    (tmpfs by default), with the live extension inside;
  * the extension inside each Chrome speaks its presence-WS to the daemon's
    local WS endpoint (instead of the relay); the daemon multiplexes every
    message onto/off the single relay connection, keyed by ``session_id``.

Stage 1 is strictly sequential: one active session. The relay gate
(``provider.activeSession !== null`` → ``provider_busy``) already prevents a
second match while the daemon holds one session. The addressing by
``session_id`` is kept anyway so the parallel stage (stage 3) can be added
without reworking the router.

Extension URL configuration is delivered three ways (stage 2 + ev 9362):
1. managed storage — the entrypoint writes
   ``/etc/chromium/policies/managed/ceki.json`` (unbranded Chromium) or
   ``/etc/opt/yandex/browser/policies/managed/`` (Yandex), and the extension's
   ``configReady()`` merges it over build defaults (``relay_ws`` →
   ``ws://127.0.0.1:<daemon_port>``);
2. local-storage runtime override — the daemon's CDP handshake writes
   ``ceki_runtime_config.relay_ws`` into chrome.storage.local (same channel as
   sanctum_token). This is the RELIABLE path on branded builds (Yandex
   corporate) that do NOT surface config-dir managed policy into
   chrome.storage.managed; the extension's configReady() reads it with highest
   precedence (ev 9362);
3. as a fallback, the daemon still patches a copy of the unpacked dist so
   ``relay_ws`` points at the local endpoint before ``--load-extension`` — this
   covers Chrome builds that do not surface config-dir extension policies
   (e.g. Chrome for Testing). All deliver the same target and are idempotent.

Profiles are wiped by default: tmpfs ``/sessions/<key>``, ``rm -rf`` on
session end, startup sweep after a daemon crash. A ``persist`` env flag (NOT
MVP, not documented) switches the base dir to a persistent mount
(``/sessions-persist``) and disables both rm and sweep for those profiles.
Host-side cron owns persist-profile cleanup; the daemon implements no quota/LRU.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import websockets

from ceki_browser_provider import app as provider_app
from ceki_browser_provider import provider_debug

log = logging.getLogger("ceki.provider.daemon")

# --- Defaults (mirror app.py / entrypoint) -------------------------------------

_DEFAULT_EXT_ID = "gfionhbdkojjnjpbhlblopoaecdpllhb"
_DEFAULT_WS_URL = "wss://browser.ceki.me/ws/provider"
_DEFAULT_API_URL = "https://api.ceki.me"
DAEMON_PORT_DEFAULT = 17890
DEFAULT_CDP_PORT_START = 9223
DEFAULT_DISPLAY_START = 101

# Chrome flags reused from app.py's base set (identical semantics).
_CHROME_ARGS = [
    "--no-sandbox",
    "--disable-gpu",
    "--disable-dev-shm-usage",
    "--no-first-run",
    "--no-default-browser-check",
    "--use-fake-ui-for-media-stream",
    "--use-fake-device-for-media-stream",
    "--disable-blink-features=AutomationControlled",
    "--lang=en-US",
    "--window-position=0,0",
    # Chromium 150+ closes remote-debugging access to extension targets by
    # default: /json/list still lists the extension SW, but WS-upgrade to it
    # answers HTTP 500 and the token handshake dies (storage never lands, no
    # presence-WS). These two switches re-open extension targets to the
    # debugging port. Verified needed on system Chromium 154 (unbranded);
    # Yandex corporate ignores them (it gates extension debugging elsewhere).
    "--remote-debugging-allow-extension-targets",
    "--enable-unsafe-extension-debugging",
    # Idle-rent memory/CPU: without these, Yandex opens ya.ru as the home
    # page plus its native chrome://wallpaper / alissenger-bubble surfaces —
    # ~5 renderer processes / ~1.9GB for an otherwise-blank rent. Keep the
    # session tab blank until the extension navigates it (about:blank).
    # NOTE: --disable-background-networking is deliberately NOT set here —
    # the policy-installed CRX (ExtensionInstallForcelist) relies on Chrome's
    # background update check to download/refresh the extension.
    "--homepage=about:blank",
    "--no-pings",
    "--disable-sync",
    "--disable-session-crashed-bubble",
    "--disable-component-update",
    "--disable-background-mode",
    "--disable-features=AlessengerBubble,Wallpaper,DesktopBackgroundMode",
    # Native download target. Without this Chromium writes downloads to the
    # OS default dir (~/Downloads) with a temp name (.org.chromium.Chromium.*)
    # and NEVER finalizes the real filename, so chrome.downloads sees only an
    # in-progress item (poll never matches 'complete') and no
    # Page.downloadWillBegin fires. With an explicit dir Chromium writes the
    # file there under its final name and emits Page.downloadWillBegin/Progress,
    # which the extension's DL transfer hooks onto. Cross-build: works on plain
    # Chromium; Yandex ignores the flag but ALSO lacks the download events, so
    # its transfer goes through the chrome.downloads poll (already supported).
    # Dir is created by the container entrypoint (/tmp/ceki-dl, 1777).
    "--download-default-directory=/tmp/ceki-dl",
]

# Token handshake is performed over CDP directly against the extension
# service worker (see SpawnManager._handshake) — no JS snippet needed here.


# Browser flavor the provider runs. Mirrors provider_app._BROWSER_FLAVOR: the
# image selects it via CEKI_PROVIDER_BROWSER ('yandex' | 'pseudo-yandex' |
# 'chromium'), defaulting to unbranded Chromium. The flavor is advertised in
# the welcome packet so the relay/backend can show the real browser instead of
# a generic "Chrome Linux".
_FLAVOR_ALIASES = {
    "chromium": "chrome",        # unbranded Playwright Chromium → "chrome"
    "yandex": "yandex",          # real YaBrowser (corporate build)
    "pseudo-yandex": "yandex",   # Chromium posing as YaBrowser → reports yandex
}
_FLAVOR_NAMES = {
    "chrome": "Chrome",
    "yandex": "Yandex Browser",
}


def provider_browser_flavor() -> str:
    """Canonical flavor key advertised in welcome / stored in settings.

    Maps the launcher's CEKI_PROVIDER_BROWSER value onto the user-facing key:
    unbranded Chromium reports "chrome", yandex/pseudo-yandex report "yandex".
    """
    raw = os.environ.get("CEKI_PROVIDER_BROWSER", "chromium").strip().lower()
    return _FLAVOR_ALIASES.get(raw, "chrome")


def provider_browser_name() -> str:
    """Human-readable browser name for ceki_browser.name / settings.browser_name."""
    return _FLAVOR_NAMES.get(provider_browser_flavor(), "Chrome")


def _pseudo_yandex_enabled() -> bool:
    """True when the daemon must serve the YaBrowser UA on rent pages.

    Only the pseudo-yandex flavor enables the spoof. Real yandex ships the UA
    natively. The flavor reports ``yandex`` (same alias), so we must compare
    the raw env value, not ``provider_browser_flavor()``.
    """
    return os.environ.get("CEKI_PROVIDER_BROWSER", "chromium").strip().lower() == "pseudo-yandex"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass
class ProxySpec:
    """Outbound proxy applied to every rental browser (from container env)."""
    scheme: str  # 'http' | 'socks5'
    host: str
    port: int
    username: str | None = None
    password: str | None = None
    bypass_extra: tuple[str, ...] = ()

    @property
    def server_url(self) -> str:
        """--proxy-server value. Credentials are NOT inlined here: Chrome CLI
        rejects user:pass in the flag; auth is answered by the extension via
        webRequest.onAuthRequired instead."""
        return f"{self.scheme}://{self.host}:{self.port}"

    @property
    def bypass_list(self) -> list[str]:
        """Chrome proxy bypass list. Base entries never route through the
        proxy; ``CEKI_PROXY_BYPASS_EXTRA`` (space- or comma-separated) appends
        operator entries (e.g. internal hosts) without hard-coding them here."""
        base = ["<local>", "localhost", "127.0.0.1", "::1", "*.ceki.me", "*.ceki.com"]
        extra = [e.strip() for e in os.environ.get("CEKI_PROXY_BYPASS_EXTRA", "").replace("\n", " ").split() if e.strip()]
        return base + extra


@dataclass
class DaemonConfig:
    token: str
    schedule_id: int | None
    api_base: str
    ext_dir: str
    daemon_port: int
    width: int
    height: int
    storage_key: str = "session_id"
    persist: bool = False
    session_dir: str = "/sessions"
    persist_session_dir: str = "/sessions-persist"
    max_sessions: int = 1
    browser_binary: str | None = None
    policy_installed_ext: bool = False  # True for branded builds (yandex): the
    # extension loads via ExtensionInstallForcelist policy (CRX), NOT
    # --load-extension, which the corporate build strips.
    cdps: list[int] = field(default_factory=list)
    displays: list[int] = field(default_factory=list)
    local_ws_url: str = ""
    proxy: ProxySpec | None = None

    @property
    def relay_url(self) -> str:
        """Provider-WS endpoint the daemon connects to (the same one the
        extension build uses). Overridable via CEKI_WS_URL like entrypoint."""
        return os.environ.get("CEKI_WS_URL") or _DEFAULT_WS_URL


def _env_bool(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _schedule_max_sessions(api_base: str, token: str, schedule_id: int | None) -> int:
    """Best-effort: read the schedule's multi-session capacity from the backend.

    Fetches /api/browser/me (the same card the extension pulls on connect) and
    returns ``settings.max_sessions`` when the schedule advertises
    multi_sessions. Any failure returns 0 so the caller falls back to the
    default of 1 — the daemon must still start when the backend is unreachable.
    """
    try:
        # The provider daemon dials the backend through the SAME outbound
        # proxy the rented browsers use (CEKI_PROXY_URL), so the server sees
        # the proxy egress IP and can assign the right geo to this schedule.
        # Without a proxy configured this is a direct call (unchanged).
        _proxy = _proxy_from_env()
        _proxy_url = _proxy_url_with_creds(_proxy)
        _client = httpx.Client(proxy=_proxy_url, timeout=15)
        try:
            resp = _client.get(
                f"{api_base}/api/browser/me",
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            )
        finally:
            _client.close()
        resp.raise_for_status()
        data = resp.json()
        s = data.get("settings", {}) if isinstance(data, dict) else {}
        if s.get("multi_session") is True:
            n = s.get("max_sessions")
            n = int(n) if n not in (None, "") else 0
            if n > 0:
                log.info(
                    "daemon: adopting schedule max_sessions=%d (multi_session) for schedule %s",
                    n, schedule_id,
                )
                return n
        log.info(
            "daemon: schedule %s multi_session=%s max_sessions=%s -> default 1",
            schedule_id, s.get("multi_session"), s.get("max_sessions"),
        )
    except Exception as exc:
        log.warning("daemon: schedule fetch failed (%s) — default max_sessions=1", exc)
    return 0


def _proxy_from_env() -> ProxySpec | None:
    """Parse proxy config from container env.

    Accepts either a single URL (CEKI_PROXY_URL, full URL optionally with
    credentials) or discrete parts:
        CEKI_PROXY_URL=http://msk:pass@176.12.64.201:3128
        # or
        CEKI_PROXY_SCHEME=http
        CEKI_PROXY_HOST=176.12.64.201
        CEKI_PROXY_PORT=3128
        CEKI_PROXY_USERNAME=msk
        CEKI_PROXY_PASSWORD=...
    Returns None when no proxy is configured.
    """
    url = os.environ.get("CEKI_PROXY_URL") or os.environ.get("CEKI_PROXY")
    if url:
        s = url.strip()
        # Accept "protocol://user:pass@host:port" and strip any trailing /
        if "://" not in s:
            s = "http://" + s
        try:
            from urllib.parse import urlparse

            u = urlparse(s)
            scheme = (u.scheme or "http").lower()
            if scheme == "https":
                scheme = "http"
            if not u.hostname:
                return None
            return ProxySpec(
                scheme=scheme,
                host=u.hostname,
                port=u.port or (3128 if scheme == "http" else 1080),
                username=u.username or None,
                password=u.password or None,
            )
        except Exception:
            return None

    scheme = (os.environ.get("CEKI_PROXY_SCHEME") or "http").lower()
    host = (os.environ.get("CEKI_PROXY_HOST") or "").strip()
    if not host:
        return None
    try:
        port = int(os.environ.get("CEKI_PROXY_PORT") or "3128")
    except ValueError:
        port = 3128
    return ProxySpec(
        scheme=scheme,
        host=host,
        port=port,
        username=os.environ.get("CEKI_PROXY_USERNAME") or None,
        password=os.environ.get("CEKI_PROXY_PASSWORD") or None,
    )


def _proxy_url_with_creds(proxy: ProxySpec | None) -> str | None:
    """Proxy URL with credentials embedded (httpx/websockets need userinfo)."""
    if proxy is None:
        return None
    netloc = proxy.host
    if proxy.port:
        netloc += f":{proxy.port}"
    if proxy.username:
        from urllib.parse import quote
        netloc = f"{quote(proxy.username, safe='')}:{quote(proxy.password or '', safe='')}@{netloc}"
    return f"{proxy.scheme}://{netloc}"


def load_config() -> DaemonConfig:
    token = (
        os.environ.get("CEKI_PROVIDER_TOKEN")
        or os.environ.get("PROVIDER_TOKEN")
        or ""
    ).strip()
    if not token:
        raise SystemExit("ceki-provider-daemon: CEKI_PROVIDER_TOKEN is required")

    api_base = (os.environ.get("CEKI_API_URL") or provider_app.default_api_url()).rstrip("/")
    if api_base.endswith("/api"):
        api_base = api_base[: -len("/api")]

    ext_dir = (
        os.environ.get("CEKI_PROVIDER_EXT_DIR")
        or os.environ.get("CEKI_EXT_DIR")
        or str(Path(provider_app.__file__).resolve().parent / "provider_assets" / "extension")
    )

    width, height = provider_app._parse_viewport(os.environ.get("CEKI_PROVIDER_VIEWPORT"))

    schedule_id: int | None = None
    raw_sid = os.environ.get("CEKI_PROVIDER_SCHEDULE_ID") or os.environ.get("PROVIDER_SCHEDULE_ID")
    if raw_sid:
        try:
            schedule_id = int(raw_sid)
        except ValueError:
            schedule_id = None

    max_sessions_env = os.environ.get("CEKI_DAEMON_MAX_SESSIONS")
    max_sessions = int(max_sessions_env) if max_sessions_env and max_sessions_env.strip() else 0
    # If the operator did not pin CEKI_DAEMON_MAX_SESSIONS, adopt the schedule's
    # own multi-session capacity from the backend (settings.max_sessions). The
    # daemon otherwise defaults to 1 and rejects parallel rents even when the
    # schedule advertises multi_session (e.g. subscription rents on 42306/42307
    # with max_sessions=10). The schedule card is fetched with the same provider
    # token the daemon uses on the provider WS; a failure is non-fatal (keep 1).
    if max_sessions <= 0:
        max_sessions = _schedule_max_sessions(api_base, token, schedule_id)

    cdp_start = int(os.environ.get("CEKI_DAEMON_CDP_START", str(DEFAULT_CDP_PORT_START)))
    display_start = int(os.environ.get("CEKI_DAEMON_DISPLAY_START", str(DEFAULT_DISPLAY_START)))

    storage_key = os.environ.get("CEKI_DAEMON_STORAGE_KEY", "session_id").strip().lower()
    persist = _env_bool("CEKI_DAEMON_PERSIST", default=False)  # NOT documented (stage 5)
    daemon_port = int(os.environ.get("CEKI_DAEMON_PORT", str(DAEMON_PORT_DEFAULT)))

    cfg = DaemonConfig(
        token=token,
        schedule_id=schedule_id,
        api_base=api_base,
        ext_dir=ext_dir,
        daemon_port=daemon_port,
        width=width,
        height=height,
        storage_key=storage_key,
        persist=persist,
        session_dir=os.environ.get("CEKI_SESSION_DIR", "/sessions"),
        persist_session_dir=os.environ.get("CEKI_SESSION_PERSIST_DIR", "/sessions-persist"),
        max_sessions=max(1, max_sessions),
        browser_binary=provider_app._browser_binary(),
        # Branded builds (yandex) install the extension via
        # ExtensionInstallForcelist (CRX from the update channel); their
        # corporate build strips --load-extension, so the daemon must not rely
        # on an unpacked copy there.
        policy_installed_ext=(os.environ.get("CEKI_PROVIDER_BROWSER") == "yandex"),
        cdps=list(range(cdp_start, cdp_start + max(1, max_sessions))),
        displays=list(range(display_start, display_start + max(1, max_sessions))),
        proxy=_proxy_from_env(),
    )
    cfg.local_ws_url = f"ws://127.0.0.1:{cfg.daemon_port}"
    return cfg


# ---------------------------------------------------------------------------
# Instance + SpawnManager
# ---------------------------------------------------------------------------

@dataclass
class Instance:
    session_id: str
    storage_key: str
    profile_dir: str
    display: int
    cdp_port: int
    chrome_pid: int | None = None
    xvfb_pid: int | None = None
    ws: Any = None          # local WS server connection (extension side)
    ready: bool = False
    profile_mode: str | None = None  # 'main' | 'incognito' (None → incognito semantics)
    created_at: float = field(default_factory=time.time)
    # Fallback CDP target (offscreen page) for storage seed when the SW route
    # is sticky-500 on branded builds (Yandex).
    offscreen_ws_fallback: str | None = None


def _pid_alive(pid: int) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    # kill(pid, 0) returns successfully for zombies — treat them as dead so a
    # re-spawn can reclaim the CDP/display slot without waiting for a reap.
    try:
        stat = Path(f"/proc/{pid}/stat").read_text().split()
        return stat[2] != "Z"
    except Exception:
        return True


def _cmdlines() -> list[str]:
    """Yield `cat /proc/<pid>/cmdline` joined by spaces for all live processes."""
    out: list[str] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "cmdline").read_bytes().replace(b"\x00", b" ")
            data = raw.decode("utf-8", "replace")
        except Exception:
            continue
        if data.strip():
            out.append((entry.name, data))
    return [(pid, data) for pid, data in out]


def _orphaned_chrome_procs(base_dirs: tuple[str, ...]) -> list[tuple[int, str]]:
    """Find live chromium processes whose --user-data-dir is under any base_dir."""
    found: list[tuple[int, str]] = []
    for pid, data in _cmdlines():
        if "user-data-dir" not in data:
            continue
        m = re.search(r"--user-data-dir=([^\s]+)", data)
        if not m:
            continue
        udd = m.group(1)
        if any(udd.startswith(b) for b in base_dirs):
            try:
                found.append((int(pid), udd))
            except ValueError:
                continue
    return found


def _procs_with_display(display: int) -> list[tuple[int, str]]:
    """Find live processes running on an X display number (Xvfb instances)."""
    found: list[tuple[int, str]] = []
    for pid, data in _cmdlines():
        if "Xvfb" not in data:
            continue
        if f":{display}" in data.split():
            try:
                found.append((int(pid), data))
            except ValueError:
                continue
    return found


class SpawnManager:
    """Process manager + spawner. Interface mirrors the spec's SpawnManager:

      ensure(sessionId, params)        → Instance
      routeToRelay(sessionId, msg)     → (router handles)
      routeToSession(sessionId, msg)   → (router handles)
      destroy(sessionId, reason)       → kill Chrome+Xvfb, rm profile (unless persist)
      active()                         → dict[session_id, Instance]
    """

    def __init__(self, cfg: DaemonConfig):
        self.cfg = cfg
        self._instances: dict[str, Instance] = {}
        self._lock = threading.Lock()
        self._queued: dict[str, list[dict]] = {}
        self._patch_stamp: str | None = None
        self._swept = False
        self._boot_slot = 0
        self._captures: list[Any] = []
        self._captures_stop: list[threading.Event] = []

    # -- public API (spec-shaped) ----------------------------------------------

    def active(self) -> dict[str, Instance]:
        with self._lock:
            return dict(self._instances)

    def ensure(self, session_id: str, params: dict) -> Instance | None:
        """Return the live instance for ``session_id`` or spawn one.

        A reconnect within an ACTIVE session finds the live instance and
        reuses it (no re-spawn). Otherwise spawns Xvfb + Chrome and performs
        the token handshake.
        """
        with self._lock:
            inst = self._instances.get(session_id)
            if inst is not None:
                return inst
            if len(self._instances) >= self.cfg.max_sessions:
                log.warning("session %s: max_sessions reached, denying spawn", session_id)
                return None
            key = self._storage_key_for(session_id, params)
            cdp = self.cfg.cdps.pop(0) if self.cfg.cdps else None
            disp = self.cfg.displays.pop(0) if self.cfg.displays else None
            if cdp is None or disp is None:
                log.warning("session %s: no free CDP/display slot", session_id)
                return None
            inst = Instance(
                session_id=session_id,
                storage_key=key,
                profile_dir=self._profile_path(key, params.get("profile_mode")),
                display=disp,
                cdp_port=cdp,
                profile_mode=params.get("profile_mode"),
            )
            self._instances[session_id] = inst
            # Stage-4 boot stagger (ev 10077): N parallel ensure() calls each
            # launch a full Xvfb + two-launch Chromium (~1GB transient each).
            # Starting all N at the same instant spikes host RAM/CPU/SHM and
            # reliably wedges one of the Chromes (its extension never reaches
            # the presence-WS, watchdog reaps it → B9 1-of-3 loss even with the
            # ext-dir and re-delivery fixes). Slot the spawns ~2s apart: still
            # race-free (every session gets handshake+CDP), but the boot burst
            # is spread so the host stays responsive.
            self._boot_slot += 1
            boot_no = self._boot_slot

        stagger = int(os.environ.get("CEKI_DAEMON_BOOT_STAGGER_S", "2"))
        if self.cfg.max_sessions > 1 and boot_no > 1 and stagger > 0:
            delay = (boot_no - 1) * stagger
            log.info("ensure[%s]: boot stagger %ds (slot %d)", session_id, delay, boot_no)
            time.sleep(delay)
        try:
            self._spawn_and_handshake(inst, params)
        except Exception as exc:
            log.exception("session %s spawn failed: %s", session_id, exc)
            self.destroy(session_id, "crashed")
            return None
        return inst

    def destroy(self, session_id: str, reason: str) -> None:
        with self._lock:
            inst = self._instances.pop(session_id, None)
        if inst is None:
            return
        # Stop any debug-capture manager bound to this instance's CDP port.
        stops = getattr(self, "_captures_stop", [])
        if stops:
            for s in stops:
                s.set()
            self._captures_stop = []
            self._captures = []
        self._kill_group(inst)
        # Return CDP/display slots to the pool so a later ensure() can reuse
        # them (sequential rent cycle: session_end -> next match spawns again).
        if inst.cdp_port not in self.cfg.cdps and len(self.cfg.cdps) < self.cfg.max_sessions:
            self.cfg.cdps.append(inst.cdp_port)
        if inst.display not in self.cfg.displays and len(self.cfg.displays) < self.cfg.max_sessions:
            self.cfg.displays.append(inst.display)
        # Persist decision is per-session by profile_mode, not global: a 'main'
        # rent keeps its profile (persistent, keyed by billable), an incognito /
        # unset rent is ephemeral and its profile is removed on end. Falls back
        # to the global cfg.persist when profile_mode is unset (legacy mode).
        keep_profile = inst.profile_mode == "main" if inst.profile_mode else self.cfg.persist
        if not keep_profile:
            shutil.rmtree(inst.profile_dir, ignore_errors=True)
            log.info("session %s destroyed (%s), profile removed", session_id, reason)
        else:
            log.info("session %s destroyed (%s), profile kept (persist)", session_id, reason)

    def destroy_all(self, reason: str) -> None:
        for sid in list(self.active().keys()):
            self.destroy(sid, reason)

    def _storage_key_for(self, session_id: str, params: dict) -> str:
        """Resolve profile-dir key per the daemon config.

        Default: ``session_id``. A composite ``billable_type:billable_id`` key
        is built from those two fields of the match payload (user N and agent N
        must not share a profile). Any other field of the payload is used
        verbatim, falling back to session_id when absent.
        """
        sk = self.cfg.storage_key
        # main-rents persist under the billable key (stable across rents of the
        # same owner/agent); incognito/unset rents use session_id (ephemeral).
        if params.get("profile_mode") == "main" and sk == "session_id":
            return session_id
        if params.get("profile_mode") == "main":
            if ":" in sk:
                a, b = sk.split(":", 1)
                va, vb = params.get(a), params.get(b)
                if va is not None and vb is not None:
                    return f"{va}:{vb}"
            val = params.get(sk)
            if val is not None:
                return str(val)
        # incognito / unset: ephemeral per-session profile
        return session_id

    def _profile_path(self, key: str, profile_mode: str | None = None) -> str:
        # main-rents persist under /sessions-persist; incognito/unset are
        # ephemeral under /sessions (even in a persist-configured container).
        is_main = profile_mode == "main"
        base = (
            Path(self.cfg.persist_session_dir if is_main else self.cfg.session_dir)
            if self.cfg.persist
            else Path(self.cfg.session_dir)
        )
        base.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", key)
        return str(base / safe)

    # -- local WS binding -------------------------------------------------------

    def buffer(self, session_id: str, msg: dict) -> None:
        """Queue a relay message for a session whose extension is not connected
        yet (spawning / reconnecting). Flushed on WS bind."""
        with self._lock:
            self._queued.setdefault(session_id, []).append(msg)

    def match_ws(self, ws: Any, session_id: str | None = None) -> Instance | None:
        """Bind a new local WS connection (extension presence) to an instance.

        With parallel sessions the extension carries its rent's session_id as a
        query param (offscreen appends `?session_id=`), so we match by THAT id
        first — "first free instance" is racy and can bind an extension to the
        wrong rent's Chrome, killing the real session. Fall back to the old
        behaviour (first instance with no live ws, or rebind when only one)
        for clients that don't send the param / reconnect within a session.
        """
        with self._lock:
            if session_id:
                inst = self._instances.get(session_id)
                if inst is not None:
                    inst.ws = ws
                    inst.ready = True
                    log.info("match_ws: by session_id %s -> inst %s", session_id, inst.session_id)
                    return inst
                log.info("match_ws: session_id %s not in active instances (%s)", session_id, list(self._instances))
            for inst in self._instances.values():
                if inst.ws is None or inst.ws.state == 3:  # CLOSED
                    inst.ws = ws
                    inst.ready = True
                    log.info("match_ws: first-free bind -> inst %s (query sid=%s)", inst.session_id, session_id)
                    return inst
            # Overwrite: rebind the only instance (reconnect).
            if len(self._instances) == 1:
                inst = next(iter(self._instances.values()))
                inst.ws = ws
                inst.ready = True
                log.info("match_ws: overwrite-> inst %s (query sid=%s)", inst.session_id, session_id)
                return inst
        return None

    def take_buffer(self, session_id: str) -> list[dict]:
        with self._lock:
            return self._queued.pop(session_id, [])

    @property
    def queued_count(self) -> int:
        with self._lock:
            return sum(len(v) for v in self._queued.values())

    # -- spawn internals --------------------------------------------------------

    def _patched_ext_dir(self) -> str:
        # Stage 2 (managed storage): the entrypoint writes a policy file
        # (chrome.storage.managed) that overrides relay_ws→daemon, but Chrome
        # does not always surface config-dir extension policies (Chrome for
        # Testing), so we ALSO patch the unpacked copy as a fallback — both
        # deliver the same ws://127.0.0.1:<daemon_port> and are idempotent.
        src = self.cfg.ext_dir
        if not Path(src, "manifest.json").is_file():
            raise RuntimeError(f"extension dist not found: {src}")
        # Patch a copy under the session dir so the baked-in dist is never
        # mutated (a fresh copy per daemon process, keyed by local_ws_url).
        base = (
            Path(self.cfg.session_dir)
            if not self.cfg.persist
            else Path(self.cfg.persist_session_dir)
        )
        base.mkdir(parents=True, exist_ok=True)
        dst = str(base / "_ext-patched")
        dst_p = Path(dst)
        # The patched copy is a SHARED singleton (one per daemon process), and
        # _spawn_and_handshake runs OUTSIDE self._lock — N concurrent ensure()
        # threads can reach here at once. An unlocked rmtree+copytree race
        # tears the shared dir mid-copy: one Chrome loads a half-built
        # extension, its service worker never comes up, and that rent's nav+ss
        # fails (the B9 1-of-3 loss, ev 10077). Hold the lock across the whole
        # rebuild so only one thread patches at a time and the others see the
        # finished copy.
        with self._lock:
            fresh = dst_p.joinpath("manifest.json").exists()
            already = fresh and self._patch_stamp == self.cfg.local_ws_url
            if already:
                return dst
            shutil.rmtree(dst, ignore_errors=True)
            shutil.copytree(src, dst)
            for f in dst_p.rglob("*.js"):
                try:
                    text = f.read_text()
                except OSError:
                    continue
                new_text = text.replace(_DEFAULT_WS_URL, self.cfg.local_ws_url)
                if new_text != text:
                    f.write_text(new_text)
            self._patch_stamp = self.cfg.local_ws_url
            log.info("extension patched: relay_ws -> %s", self.cfg.local_ws_url)
        return dst

    def _chrome_binary(self) -> str:
        if self.cfg.browser_binary and Path(self.cfg.browser_binary).exists():
            return self.cfg.browser_binary
        # Fall back to Playwright's pinned Chromium.
        try:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as p:
                exe = p.chromium.executable_path
                if exe and Path(exe).exists():
                    return exe
        except Exception as exc:
            log.warning("playwright resolve failed: %s", exc)
        raise RuntimeError("no chrome binary available (CEKI_PROVIDER_BROWSER / playwright)")

    def _build_chrome_args(self, inst: Instance, ext_dir: str) -> list[str]:
        args = list(_CHROME_ARGS)
        if self.cfg.proxy:
            # Proxy is applied by the EXTENSION via chrome.proxy.settings
            # (seeded from env → storage ceki_proxy). Verified: --proxy-server
            # is NOT honored by Yandex corporate on the network level — without
            # creds it 407-stalls (white frame), with creds it yields
            # chrome-error:// (unreachable page). chrome.proxy.settings from
            # the extension is the only path that actually tunnels (works on
            # Chrome; fixing Yandex adoption is the current task).
            log.info("daemon: rental proxy applied by extension -> %s", self.cfg.proxy.server_url)
        args.append(f"--window-size={self.cfg.width},{self.cfg.height}")
        if _pseudo_yandex_enabled():
            # pseudo-yandex: launch-level YaBrowser UA. Page-level CDP override
            # conflicts with the extension's chrome.debugger session on system
            # Chromium 154 (SW CDP answers 500 and the rent dies) — this is the
            # only path that survives the handshake.
            args.append("--user-agent=" + provider_app._PSEUDO_UA)
        args.append(f"--user-data-dir={inst.profile_dir}")
        args.append(f"--disk-cache-dir={inst.profile_dir}/cache")
        args.append(f"--remote-debugging-port={inst.cdp_port}")
        args.append("--remote-allow-origins=*")
        if not self.cfg.policy_installed_ext:
            # Unpacked-extension flags. Skipped on branded builds (yandex):
            # the corporate build strips --load-extension, so the extension is
            # installed from the update channel via ExtensionInstallForcelist
            # instead and the socket-path patch is delivered by the managed
            # policy (write_managed_policy in entrypoint).
            args.append(f"--load-extension={ext_dir}")
            args.append(f"--disable-extensions-except={ext_dir}")
        return args

    def _launch_chrome(self, inst: Instance, args: list[str]) -> subprocess.Popen:
        # A previous run on a PERSIST profile may have left stale Chrome
        # singleton locks (SingletonLock/SingletonSocket/SingletonCookie) after a
        # hard kill. Chrome treats those as "profile in use by another instance"
        # and can fail to open + reach the extension target (discover fails).
        # Remove them so a fresh launch on the same persisted profile works.
        for lock in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
            try:
                p = Path(inst.profile_dir) / lock
                if p.exists() or p.is_symlink():
                    p.unlink()
            except OSError:
                pass
        env = dict(os.environ)
        env["DISPLAY"] = f":{inst.display}"
        proc = subprocess.Popen(
            [self._chrome_binary(), *args],
            env=env,
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            log.info("launch[%s]: chrome pid=%s pgid=%s display=:%d", inst.session_id, proc.pid, os.getpgid(proc.pid), inst.display)
        except Exception:
            pass
        return proc

    def _launch_xvfb(self, inst: Instance) -> subprocess.Popen:
        # Adoption path (host-provided Xvfb): if an X server already answers
        # :<display>, use it instead of spawning our own. A self-spawned Xvfb
        # puts its socket in a container-private tmpfs the stream sidecar can
        # never reach, making rentals invisible (the core bug we fixed by
        # pointing containers at the host /tmp/.X11-unix). Adopt the existing
        # live display: return a handle carrying its real pid so destroy() can
        # still kill the right process and no signalfig occurs.
        probe = subprocess.run(
            ["timeout", "3", "xwininfo", "-display", f":{inst.display}", "-root"],
            capture_output=True,
        )
        if probe.returncode == 0:
            ext = _procs_with_display(inst.display)
            if ext:
                pid = ext[0][0]
                log.info("xvfb: adopting host Xvfb on :%d (pid %s)", inst.display, pid)
                env = dict(os.environ)
                return _Popen_handle(pid=pid)
            # xwininfo answered but NO process actually owns this display
            # (transient/stale probe success — seen as "adopting host Xvfb on
            # :142 (pid -1)", which left rentals with NO X server → Chrome
            # "Missing X server" → rent never spawned). Treat as absent and
            # fall through to spawn our own Xvfb below.
            log.warning(
                "xvfb: :%d xwininfo ok but no owning process — spawning own Xvfb",
                inst.display,
            )

        # A previous run may have left an orphaned Xvfb on this display (its
        # socket was unlinked but the process survived a hard kill). That
        # stale process owns the display number and blocks a fresh Xvfb from
        # starting — kill any live process whose cmdline carries this display
        # before we unlink the socket.
        stale = _procs_with_display(inst.display)
        for pid, _data in stale:
            try:
                os.kill(pid, signal.SIGKILL)
                log.info("xvfb: killed stale process on :%d (pid %s)", inst.display, pid)
            except (ProcessLookupError, PermissionError):
                pass
        sock = f"/tmp/.X{inst.display}-lock"
        try:
            os.unlink(sock)
        except OSError:
            pass
        try:
            os.unlink(f"/tmp/.X11-unix/X{inst.display}")
        except OSError:
            pass
        env = dict(os.environ)
        proc = subprocess.Popen(
            [
                "Xvfb",
                f":{inst.display}",
                "-screen",
                "0",
                f"{self.cfg.width}x{self.cfg.height}x24",
                "-nolisten",
                "tcp",
            ],
            env=env,
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            log.info("launch[%s]: xvfb pid=%s pgid=%s display=:%d", inst.session_id, proc.pid, os.getpgid(proc.pid), inst.display)
        except Exception:
            pass
        return proc

    def _maybe_start_debug_capture(self, inst: Instance) -> None:
        """Start extension SW-console capture on this instance's CDP port when
        CEKI_PROVIDER_DEBUG_LOG is set. Unlike app mode, the browser already
        listens on inst.cdp_port (no separate 9333 browser), so we feed the
        capture manager that port. Pure opt-in; no-op otherwise."""
        cfg = provider_debug.config_from_env()
        if cfg is None:
            return
        self._captures = getattr(self, "_captures", [])
        self._captures_stop = getattr(self, "_captures_stop", [])
        stop = threading.Event()
        mgr = provider_debug.start_capture(
            provider_debug.DebugConfig(log_path=cfg.log_path, port=inst.cdp_port,
                                       ping_interval=cfg.ping_interval,
                                       ping_timeout=cfg.ping_timeout),
            _DEFAULT_EXT_ID,
            stop,
        )
        if mgr is not None:
            self._captures.append(mgr)
            self._captures_stop.append(stop)
            log.info("debug capture started for instance %s on cdp port %s", inst.session_id, inst.cdp_port)

    def _spawn_and_handshake(self, inst: Instance, params: dict) -> None:
        """Blocking spawn: Xvfb + Chrome (two-launch incognito) + token handshake.

        Mirrors app.py's two-launch incognito-grant + panel handshake, driving
        the Chrome binary directly via subprocess and attaching over CDP for
        the handshake (no Playwright persistent-context ownership).
        """
        ext_dir = "" if self.cfg.policy_installed_ext else self._patched_ext_dir()
        profile = inst.profile_dir
        Path(profile).mkdir(parents=True, exist_ok=True)
        Path(profile, "Default").mkdir(parents=True, exist_ok=True)
        Path(profile, "cache").mkdir(parents=True, exist_ok=True)

        chrome_args = self._build_chrome_args(inst, ext_dir)

        # 1) Xvfb on the instance display.
        xvfb = self._launch_xvfb(inst)
        inst.xvfb_pid = xvfb.pid
        # Wait for the X socket.
        x_sock = f"/tmp/.X11-unix/X{inst.display}"
        for _ in range(40):
            if os.path.exists(x_sock):
                break
            time.sleep(0.5)
        else:
            log.warning("X socket %s not ready after 20s, continuing", x_sock)

        # 2) install phase: Chrome loads the extension, we learn the id.
        proc1 = self._launch_chrome(inst, chrome_args)
        inst.chrome_pid = proc1.pid
        ext_id = self._discover_ext_id(inst, expected=_DEFAULT_EXT_ID, timeout=45)
        # terminate install-phase Chrome cleanly before Preferences edit
        self._kill_proc_group(proc1.pid)
        self._wait_exit(proc1.pid)
        inst.chrome_pid = None
        if not ext_id:
            self._kill_proc_group(xvfb.pid)
            raise RuntimeError("could not discover extension id in install phase")

        # 3) grant incognito access in Preferences (two-launch like app.py)
        default_dir = Path(profile) / "Default"
        prefs_path = default_dir / "Preferences"
        try:
            prefs = json.loads(prefs_path.read_text())
        except Exception:
            prefs = {}
        settings = prefs.setdefault("extensions", {}).setdefault("settings", {})
        entry = settings.setdefault(ext_id, {})
        entry["incognito"] = True
        entry["state"] = 1

        # 3b) outbound proxy baked into Preferences (applies at launch, both
        # regular and incognito windows). Credentials are NOT stored here —
        # the extension answers CONNECT auth via webRequest.onAuthRequired
        # using the creds the daemon seeds into chrome.storage.local.
        if self.cfg.proxy:
            prefs["proxy"] = {
                "mode": "fixed_servers",
                "server": {
                    "scheme": self.cfg.proxy.scheme,
                    "host": self.cfg.proxy.host,
                    "port": self.cfg.proxy.port,
                },
                "bypass_list": self.cfg.proxy.bypass_list,
            }

        prefs_path.write_text(json.dumps(prefs))
        log.info("two-launch: incognito granted for %s", ext_id)

        # 4) run phase: relaunch the same profile, now with incognito + token.
        proc2 = self._launch_chrome(inst, chrome_args)
        inst.chrome_pid = proc2.pid
        self._discover_ext_id(inst, timeout=30)
        # 5) token handshake via the extension panel over CDP.
        self._handshake(inst)
        # 5a) optional SW-console capture on this instance's CDP port.
        self._maybe_start_debug_capture(inst)
        # 5b) Re-spawn guard: if the extension never connected its presence-WS
        # (handshake failed on a CDP 500 / dead target / stale browser on this
        # port), give ONE fresh relaunch of the run-phase Chrome before the
        # watchdog reaps the session. Cheap, bounded, and it turns the
        # intermittent Yandex CDP WS 500 (which currently kills ~1/3 rents)
        # into a recoverable second attempt.
        ws = getattr(inst, "ws", None)
        ws_open = ws is not None and getattr(ws, "state", None) == 1
        if not ws_open:
            log.warning("handshake: presence-WS not connected after handshake, relaunching run-phase Chrome once")
            self._kill_proc_group(proc2.pid)
            self._wait_exit(proc2.pid)
            proc2 = self._launch_chrome(inst, chrome_args)
            inst.chrome_pid = proc2.pid
            self._discover_ext_id(inst, timeout=30)
            self._handshake(inst)
        # 6) opportunistic: drop Yandex's default background tabs (ya.ru,
        #    chrome://wallpaper, alissenger) that eat ~1GB on an idle rent.
        #    Deferred so the extension's session tab (about:blank) opens first;
        #    failures are non-fatal.
        threading.Timer(2.0, self._close_idle_tabs, args=(inst,)).start()
        # pseudo-yandex YaBrowser UA is applied at launch (--user-agent in
        # _build_chrome_args); page-level CDP patching conflicts with the
        # extension's debugger session on Chromium 154 and kills the rent.

    def _discover_ext_id(self, inst: Instance, expected: str | None = None,
                         timeout: float = 60) -> str | None:
        """Wait for a chrome-extension:// target with the expected id.

        Only the service-worker / page targets of the extension itself match;
        other chrome-extension:// pages (built-in extensions) are ignored.
        """
        cdp = f"http://127.0.0.1:{inst.cdp_port}"
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                resp = httpx.get(f"{cdp}/json/list", timeout=2)
                targets = resp.json()
            except Exception:
                targets = []
            for t in targets:
                url = t.get("url") or ""
                m = re.search(r"chrome-extension://([a-z]+)/", url)
                if not m:
                    continue
                found = m.group(1)
                if expected is None or found == expected:
                    log.info("discover[%s]: found ext target url=%s", inst.session_id, url[:120])
                    return found
            time.sleep(0.5)
        log.warning("discover[%s]: ext target not found after %.0fs (expected=%s)", inst.session_id, timeout, expected)
        return None

    def _handshake(self, inst: Instance) -> None:
        """Store sanctum_token + ceki_browser in chrome.storage.local over CDP.

        Drives the extension's service worker directly over the DevTools
        protocol (no Playwright page round-trip): Runtime.evaluate with a
        chrome.storage.local.set() payload. The SW's storage.onChanged handler
        then pushes token_updated to the offscreen document, which opens the
        presence-WS against the daemon's local endpoint.

        Everything is a short-lived CDP attach; the Chrome process stays owned
        by the daemon.
        """
        # The service worker may take a moment to spin up after launch; poll
        # for it before giving up. On branded builds (yandex) the SW target
        # answers HTTP 500 on ANY WS-upgrade (chrome.debugger already attached
        # there) — the offscreen document is a plain page target that usually
        # answers fine, so for yandex we wait for it FIRST.
        if self.cfg.policy_installed_ext:
            sw_ws = None
            for _ in range(40):  # up to ~20s for offscreen to appear
                off = self._find_offscreen_target(inst)
                if off is not None:
                    sw_ws = off
                    break
                time.sleep(0.5)
            if sw_ws is None:
                sw_ws = self._find_target_ws(inst, "service_worker", _DEFAULT_EXT_ID)
            log.info("handshake: yandex storage channel -> %s", ("offscreen" if "offscreen" in (sw_ws or "") else "sw"))
        else:
            # The token MUST land in the extension's service worker: that is the
            # context whose storage.onChanged pushes token_updated to the
            # offscreen doc (which opens the presence-WS). The background page
            # target on Chromium 154 answers CDP but has NO extension API
            # (chrome is undefined) — writing storage.set there fails silently
            # and the rent dies without a presence-WS. Prefer the SW; fall back
            # to background_page only if no SW target exists at all.
            sw_ws = None
            for _ in range(40):
                sw_ws = self._find_target_ws(inst, "service_worker", _DEFAULT_EXT_ID)
                if sw_ws is not None:
                    break
                time.sleep(0.5)
            if sw_ws is None:
                sw_ws = self._find_target_ws(inst, "background_page", _DEFAULT_EXT_ID)
        # Store an offscreen fallback on the INSTANCE (not self — parallel
        # rents share the daemon). Used when the SW route is sticky-500.
        inst.offscreen_ws_fallback = self._find_offscreen_target(inst) or sw_ws
        if sw_ws is None:
            log.warning("handshake[%s]: extension service worker target not found", inst.session_id)
            return

        # Plugin UI toggles — mirror app.py (idle provider) so the rental
        # window opens 'normal' + focused on the daemon's display too. Without
        # these the extension falls back to its own defaults (ports.ts):
        # open_window_normal undefined -> minimized, which leaves the rental
        # content off the streamed X window (empty NTP on X151).
        #
        # IMPORTANT: env flags (CEKI_PROVIDER_OPEN_*) only define the INITIAL
        # state, and who owns that state depends on the profile mode:
        #
        #   * main  — the host's REAL profile. chrome.storage.local belongs to
        #     the user, the plugin panel is the only writer. Writing the toggles
        #     unconditionally on every handshake/re-delivery silently reverts
        #     the user's choices (rental windows pop to normal+focused even when
        #     the host chose minimized). Seed only while still undefined.
        #
        #   * incognito / unset — the daemon's OWN throwaway profile, recreated
        #     per rent under /sessions and deleted afterwards. There is no user
        #     to preserve, but the extension seeds `open_window_normal: false`
        #     itself on first install (background.ts onInstalled), so by the
        #     time we write, the key is never undefined — the env-driven value
        #     was therefore never applied, the rental window opened
        #     `focused: false` / `state: 'minimized'` (ports.ts), and on an Xvfb
        #     display with no window manager the window never painted: the
        #     stream saw an empty "New Tab" window and stayed on idle. Seed the
        #     toggles unconditionally for these profiles.
        def _flag(name: str, default: bool) -> bool:
            v = os.environ.get(name)
            return default if v is None else v.lower() in ("1", "true", "yes", "on")

        ui_seed = {
            "open_window_normal": _flag("CEKI_PROVIDER_OPEN_NORMAL", True),
            "open_window_focused": _flag("CEKI_PROVIDER_OPEN_FOCUSED", True),
            "restore_focus_on_rental": _flag("CEKI_PROVIDER_RESTORE_FOCUS_ON_RENTAL", False),
        }

        payload = {
            "sanctum_token": self.cfg.token,
            "ceki_browser": {
                "id": self.cfg.schedule_id or 0,
                "online": False,
                "name": provider_browser_name(),
                "flavor": provider_browser_flavor(),
            },
            "paired_at": int(time.time() * 1000),
            "incognito_available": True,
            "auto_accept": True,
        }
        # Local relay endpoint override (ev 9362). Some branded builds (Yandex
        # corporate) do NOT surface config-dir managed policy into
        # chrome.storage.managed, so the managed-policy path alone cannot point
        # the extension's presence-WS at this daemon's local endpoint. Deliver
        # it over the same reliable channel as the token — chrome.storage.local
        # via CDP — under a dedicated key the extension's configReady() reads
        # with highest precedence. Backend/chat defaults come from the build /
        # managed policy; only the local relay_ws differs per daemon.
        payload["ceki_runtime_config"] = {
            "relay_ws": self.cfg.local_ws_url,
        }
# One round-trip: unconditionally write the service keys, then seed the
        # UI toggles — unconditionally for the daemon's own (non-main) profiles,
        # only-when-undefined for the host's real profile. The JS reads the
        # current storage in-process, so there is no daemon-side race with the
        # panel writing at the same moment.
        # Seed policy: the DAEMON'S OWN (non-main) profiles are throwaway —
        # recreated per rent /sessions and deleted after — nothing user-owned
        # survives between rentals, so the toggles are seeded unconditionally
        # (persist is always off and not planned). The host's real 'main'
        # profile keeps the undefined-only guard: its storage is the user's and
        # an unconditional overwrite would silently revert their panel choices.
        seed_only_undefined = inst.profile_mode == "main"
        ui_seed_json = json.dumps(ui_seed)
        # host profile → keep the user's value; daemon's own profile → overwrite.
        guard = "current[k] === undefined" if seed_only_undefined else "true"
        # Outbound proxy (from container env): the extension answers proxy auth
        # via webRequest.onAuthRequired, so pass the credentials through the
        # same storage channel. When no proxy is configured this is absent and
        # the extension keeps its default (direct / per-rent configure).
        if self.cfg.proxy:
            payload["ceki_proxy"] = {
                "enabled": True,
                "scheme": self.cfg.proxy.scheme,
                "host": self.cfg.proxy.host,
                "port": self.cfg.proxy.port,
                "username": self.cfg.proxy.username,
                "password": self.cfg.proxy.password,
            }
        expr = (
            "(async () => {"
            "const keys = Object.keys(" + ui_seed_json + ");"
            "const current = await chrome.storage.local.get(keys);"
            "const seed = {};"
            "for (const k of keys) {"
            "  if (" + guard + ") seed[k] = " + ui_seed_json + "[k];"
            "}"
            "await chrome.storage.local.set(" + json.dumps(payload) + ");"
            "const seeded = Object.keys(seed);"
            "if (seeded.length) await chrome.storage.local.set(seed);"
            "return JSON.stringify({ seeded });"
            "})()"
        )
# Retry the storage write a few times against a FRESH target ws. On
        # Yandex corporate, a CDP WS-upgrade can intermittently answer
        # HTTP 500 (stale/racing target); re-listing /json/list usually yields
        # a working websocketDebuggerUrl. Doing this here (not inside
        # _cdp_eval) keeps every other CDP eval predictable.
        max_sets = 4
        tried_fallback = False
        for attempt in range(max_sets):
            try:
                result = self._cdp_eval(sw_ws, expr)
                log.info("handshake: storage set -> %s", json.dumps(result)[:200])
                break
            except Exception as exc:
                log.warning(
                    "handshake: storage.set attempt %d/%d failed on %s: %r",
                    attempt + 1, max_sets, sw_ws, exc,
                )
                # Re-resolve the SW target — the previous ws may be dead
                # (HTTP 500) and the extension may have re-registered it.
                refreshed = self._find_target_ws(inst, "service_worker", _DEFAULT_EXT_ID)
                if refreshed is None:
                    refreshed = self._find_target_ws(inst, "background_page", _DEFAULT_EXT_ID)
                if refreshed and refreshed != sw_ws:
                    log.info("handshake: SW target changed %s -> %s", sw_ws, refreshed)
                    sw_ws = refreshed
                elif not tried_fallback and getattr(inst, "offscreen_ws_fallback", None):
                    # SW route is sticky-500 (Yandex): try the offscreen page
                    # target as the storage.set channel instead.
                    log.info("handshake: SW target sticky-fail — switching to offscreen target")
                    sw_ws = inst.offscreen_ws_fallback
                    tried_fallback = True
                time.sleep(1.0)
        self._retry_token_after_offscreen(inst, sw_ws, payload)

    def _retry_token_after_offscreen(
        self, inst: Instance, sw_ws: str, payload: dict
    ) -> None:
        """Keep the token set until the extension's presence-WS connects.

        Chromium 154: the offscreen document (which opens the presence-WS) is
        created by the extension SW only AFTER a stable sanctum_token lands in
        chrome.storage.local. Destructive clear+set loops the token_cleared /
        token_updated events so fast the offscreen never stabilises. Instead:
        write (or re-write) the token + relay config, then poll for the offscreen
        CDP target and finally wait for presence-WS — no removes.
        """
        deadline = time.time() + int(os.environ.get("CEKI_DAEMON_CONNECT_TIMEOUT", "90"))
        # 1. Ensure the token/config is set (idempotent — re-set refreshes the
        #    storage.onChanged trigger without tearing the SW down).
        re_set = 0
        while time.time() < deadline:
            # If presence already connected, done.
            ws = getattr(inst, "ws", None)
            if ws is not None and getattr(ws, "state", 3) == 1:
                log.info("handshake: presence-WS connected (re-set round %d)", re_set)
                return
            # Re-resolve SW target (stale WS after relaunch / 500).
            cur_sw = sw_ws
            found = self._find_target_ws(inst, "service_worker", _DEFAULT_EXT_ID)
            if found is None:
                found = self._find_target_ws(inst, "background_page", _DEFAULT_EXT_ID)
            if found is not None:
                cur_sw = found
            try:
                self._cdp_eval(
                    cur_sw,
                    "chrome.storage.local.set(" + json.dumps(payload) + ")",
                )
                re_set += 1
            except Exception as exc:
                log.warning("handshake: token set (round %d) failed: %s", re_set, exc)
                time.sleep(1.0)
                continue
            # 2. Give the SW a beat to create the offscreen document (which
            #    opens the presence-WS on token_updated).
            time.sleep(2.0)
        log.warning("handshake: presence-WS did not connect within %ds", int(os.environ.get("CEKI_DAEMON_CONNECT_TIMEOUT", "90")))


    def _find_target_ws(self, inst: Instance, kind: str, ext_id: str) -> str | None:
        """Return the webSocketDebuggerUrl of a live SW/background target.

        Chrome may list MULTIPLE service_workers / background_pages for the
        same extension (stale generations from previous rentals linger until
        GC'd). Picking the FIRST one hits a dead target whose WS answers 500
        or ECONNREFUSED — the root of the intermittent spawn flak. Take the
        LAST (newest) target instead, and verify it is actually connectable.
        """
        cdp = f"http://127.0.0.1:{inst.cdp_port}"
        try:
            targets = httpx.get(f"{cdp}/json/list", timeout=2).json()
        except Exception:
            return None
        candidates: list[dict] = []
        for t in targets:
            if t.get("type") != kind:
                continue
            url = t.get("url") or ""
            if f"chrome-extension://{ext_id}/" not in url:
                continue
            candidates.append(t)
        if not candidates:
            return None
        # newest (last listed) first; probe each with a real WS handshake.
        # A plain TCP connect is NOT enough on Chromium 154: the port is open
        # but the WS-upgrade to a stale/stopped extension worker answers
        # HTTP 500 (and the storage handshake dies). Return the first target
        # whose WS-upgrade actually succeeds.
        import websockets.sync.client as _wscl
        for t in reversed(candidates):
            ws_url = t.get("webSocketDebuggerUrl") or ""
            try:
                with _wscl.connect(ws_url, open_timeout=1.0, close_timeout=0.2):
                    return ws_url
            except Exception:
                continue  # 500 / refused / stale — try next
        # No WS-connectable candidate. Do NOT fall back to a dead target:
        # storage.set there answers 500 and the handshake stalls. Return None
        # so the caller keeps polling — a healthy worker appears a moment
        # later (Chromium 154 spawns the extension SW async after launch).
        return None

    def _find_offscreen_target(self, inst: Instance) -> str | None:
        """Return the CDP ws URL of the extension's offscreen document, if the
        SW has created it. The offscreen doc is a `page` target whose URL is
        ``chrome-extension://<ext_id>/offscreen/offscreen.html``."""
        cdp = f"http://127.0.0.1:{inst.cdp_port}"
        try:
            targets = httpx.get(f"{cdp}/json/list", timeout=2).json()
        except Exception:
            return None
        for t in targets:
            url = t.get("url") or ""
            if f"/offscreen/offscreen.html" in url:
                return t.get("webSocketDebuggerUrl")
        return None

    def _cdp_send(self, ws_url: str, method: str, params: dict | None = None,
                  timeout: float = 5.0) -> Any:
        """Send one CDP command to a target and return its result.

        Shared transport for ``_cdp_eval`` (Runtime.evaluate) and
        ``_cdp_command`` (any other method, e.g. Network.setUserAgentOverride).
        Short timeout (5s): during spawn the extension SW may appear in
        /json/list before it is ready to serve CDP — a 20s send turned one
        retry loop into an 80s stall and the watchdog reaped the rent. A fast
        refusal lets the caller's retry/refresh/relaunch path kick in quickly.
        """
        async def _run() -> Any:
            # websockets 13.x: ``connect()`` returns an async-CM (opens on enter).
            # Keep the open-phase exception visible with the target URL so a
            # branded-yandex CDP rejection (HTTP 40x/500 on WS-upgrade) is
            # distinguishable from a transport failure in the daemon log.
            try:
                async with websockets.connect(ws_url, max_size=64 * 1024 * 1024) as sock:
                    request_id = 1
                    await sock.send(json.dumps({
                        "id": request_id,
                        "method": method,
                        "params": params or {},
                    }))
                    while True:
                        msg = json.loads(await asyncio.wait_for(sock.recv(), timeout=timeout))
                        if msg.get("id") == request_id:
                            if "error" in msg:
                                raise RuntimeError(f"CDP error: {msg['error']}")
                            return msg.get("result", {})
            except Exception as exc:
                log.warning("cdp: CDP %s failed ws=%s : %r", method, ws_url, exc)
                raise
        return asyncio.run(_run())

    def _cdp_eval(self, ws_url: str, expression: str, timeout: float = 5.0) -> Any:
        """Evaluate ``expression`` in a CDP target via its webSocketDebuggerUrl."""
        res = self._cdp_send(
            ws_url, "Runtime.evaluate", {
                "expression": expression,
                "awaitPromise": True,
                "returnByValue": True,
            }, timeout=timeout,
        )
        return (res or {}).get("result", {}).get("value")

    # -- pseudo-yandex UA ------------------------------------------------
    # The YaBrowser UA is applied at LAUNCH via --user-agent (see
    # _build_chrome_args). Page-level Network.setUserAgentOverride over a
    # daemon-owned WS conflicts with the extension's own chrome.debugger
    # session on system Chromium 154 (SW CDP answers 500) and breaks the
    # rental; launch-level is the only path that survives the handshake.

    def _close_idle_tabs(self, inst: Instance) -> None:
        """Close Yandex's default background surfaces after launch.

        A fresh Yandex profile opens its home page (ya.ru) plus the native
        chrome://wallpaper / chrome://alissenger-bubble / chrome://ntp surfaces
        on first run — ~4-5 renderer processes / ~700MB-1GB of an otherwise
        blank rent. The session tab is opened separately by the extension
        (about:blank or the rent URL), so these default tabs are pure overhead.

        Only known Yandex garbage surfaces are closed, and never the active /
        foreground tab. An earlier allowlist (close everything that is not
        about:blank / extension / newtab) closed the SESSION tab once the
        extension navigated it to a real URL — e.g. close_idle closed
        https://www.google.com/ ~2s after match → session destroyed →
        user_stop (dev sessions 11418/11419). Purely opportunistic — failures
        are ignored.
        """
        # Never touch tabs once this instance is paired to a live rent — the
        # session tab may already be navigated to a real URL by the extension,
        # and closing it kills the session (user_stop). The trim is only meant
        # for the startup window before the first match.
        if inst.ws is not None or (inst.session_id and self.active().get(inst.session_id) is inst):
            log.info("close_idle: skip (session already active for %s)", inst.session_id)
            return
        log.info("close_idle: running for session %s", inst.session_id)
        cdp = f"http://127.0.0.1:{inst.cdp_port}"
        garbage_prefixes = (
            "chrome://wallpaper",
            "chrome://alissenger-bubble",
            "chrome://ntp",
        )
        home_hosts = ("ya.ru", "yandex.ru")
        deadline = time.time() + 30
        closed = 0
        while time.time() < deadline:
            try:
                targets = httpx.get(f"{cdp}/json/list", timeout=2).json()
            except Exception:
                time.sleep(0.5)
                continue
            found = False
            for t in targets:
                url = t.get("url") or ""
                if t.get("type") != "page":
                    continue
                if not url:
                    continue
                if t.get("active"):
                    continue  # never close the foreground/session tab
                low = url.lower()
                if not (
                    any(low.startswith(p) for p in garbage_prefixes)
                    or any(h in low for h in home_hosts)
                ):
                    continue  # not known garbage — leave it alone
                try:
                    httpx.get(f"{cdp}/json/close/{t.get('id')}", timeout=2)
                    log.info("close_idle: closed %s", url[:80])
                    closed += 1
                    found = True
                except Exception:
                    pass
            if not found:
                break
            time.sleep(0.3)
        if closed:
            log.info("close_idle: closed %d background tab(s)", closed)

    def _kill_group(self, inst: Instance) -> None:
        log.info("kill[%s]: chrome_pid=%s xvfb_pid=%s", inst.session_id, inst.chrome_pid, inst.xvfb_pid)
        for pid in (inst.chrome_pid, inst.xvfb_pid):
            if pid:
                self._kill_proc_group(pid)
        # grace then SIGKILL
        for _ in range(20):
            if not any(_pid_alive(pid) for pid in (inst.chrome_pid, inst.xvfb_pid) if pid):
                break
            time.sleep(0.5)
        for pid in (inst.chrome_pid, inst.xvfb_pid):
            if pid and _pid_alive(pid):
                try:
                    os.killpg(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass

    def _kill_proc_group(self, pid: int) -> None:
        try:
            os.killpg(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass

    def _wait_exit(self, pid: int, timeout: float = 15.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not _pid_alive(pid):
                return
            time.sleep(0.5)

    # -- startup sweep ------------------------------------------------------------

    def startup_sweep(self) -> None:
        """Sweep leftovers from a previous daemon run (base mode only).

        Kill orphaned chromium processes whose --user-data-dir points under the
        active base dir, then (base mode) wipe the whole base dir. Persist mode:
        kill only orphaned processes whose profile lives on the persist mount;
        never touch tmpfs profiles or persist profile data (host cron owns that).
        """
        if self._swept:
            return
        self._swept = True
        base = self.cfg.session_dir
        persist = self.cfg.persist_session_dir

        # Kill orphaned processes under either base (base) / persist mount (persist).
        orphans = _orphaned_chrome_procs((base, persist))
        for pid, udd in orphans:
            try:
                os.kill(pid, signal.SIGKILL)
                log.info("startup sweep: killed orphan chrome %s (%s)", pid, udd)
            except (ProcessLookupError, PermissionError):
                pass

        if self.cfg.persist:
            log.info("startup sweep: persist mode — profiles kept, orphan processes killed")
            return

        # Base mode: wipe /sessions/* (tmpfs leftovers of a crashed daemon).
        if Path(base).exists():
            for p in Path(base).iterdir():
                if p.is_dir():
                    shutil.rmtree(p, ignore_errors=True)
                else:
                    try:
                        p.unlink()
                    except OSError:
                        pass
            log.info("startup sweep: %s cleared", base)


# ---------------------------------------------------------------------------
# Daemon: async glue (provider-WS client + local WS server)
# ---------------------------------------------------------------------------

class Router:
    """Thin session-id helper shared by the relay / extension glue.

    Stage 1 keeps ``session_id`` as the lookup key so the parallel stage (3)
    only has to widen the map — routing itself lives in ProviderWsClient /
    LocalWsServer.
    """

    def __init__(self, spawner: SpawnManager):
        self.spawner = spawner

    def active_session_id(self) -> str | None:
        act = self.spawner.active()
        if not act:
            return None
        return next(iter(act), None)


class ProviderWsClient:
    """Provider-protocol WS client to the relay (single connection)."""

    def __init__(self, cfg: DaemonConfig, spawner: SpawnManager, router: Router):
        self.cfg = cfg
        self.spawner = spawner
        self.router = router
        self.ws: Any = None
        self._stop = False
        self._reconnect_delay = 1.0
        self._started_at = time.time()

    @property
    def relay_url(self) -> str:
        return self.cfg.relay_url

    async def send(self, msg: dict | str) -> None:
        ws = self.ws
        if ws is None or ws.state != 1:  # OPEN
            return
        data = msg if isinstance(msg, str) else json.dumps(msg)
        try:
            await ws.send(data)
        except Exception as exc:
            log.warning("relay send failed: %s", exc)

    async def _on_message(self, msg: dict) -> None:
        mtype = msg.get("type")
        log.info("relay-in: type=%s sid=%s route_sid=%s", mtype, msg.get("session_id") or msg.get("event_id"), self.router.active_session_id())
        if mtype == "ping":
            await self.send({"type": "pong"})
            return
        if mtype == "provider.alive_probe":
            await self.send({"type": "provider.alive_ack"})
            log.info("[heartbeat] online=online elapsed=%ds", int(time.time() - self._started_at))
            return
        if mtype == "match":
            session_id = msg.get("session_id")
            if not session_id:
                log.warning("relay match without session_id, dropping")
                return
            # CRITICAL: buffer the match BEFORE the long ensure/spawn. The
            # spawn takes ~10s (Xvfb + two-launch Chrome); meanwhile a CDP
            # (navigate) from the renter lands in the buffer and is flushed on
            # extension connect. If match is only delivered after ensure
            # returns, it arrives AFTER the buffered cdp was already flushed →
            # extension gets navigate before match → no _pendingSession →
            # no_session. Buffering match up-front guarantees the flush serves
            # match first (order preserved, match-first sort in _deliver).
            inst = self.spawner.active().get(session_id)
            if inst is None or inst.ws is None:
                self.spawner.buffer(session_id, msg)
                log.info("relay -> match buffered UPFRONT for %s", session_id)
                if inst is None:
                    try:
                        await asyncio.to_thread(self.spawner.ensure, session_id, msg)
                    except Exception as match_exc:
                        log.error("relay-in: match ensure EXC %s: %r", session_id, match_exc)
                        return
                return
            await self._deliver(session_id, msg)
            return
        # other relay → session messages: route by session_id
        session_id = msg.get("session_id") or msg.get("event_id")
        if mtype == "cdp" and not session_id:
            # Relay always stamps renter→provider CDP with session_id (cdp.ts:
            # withSessionId) so parallel sessions route to the correct browser
            # instance. active_session_id() is only a legacy fallback for a
            # CDP without one (single-session providers).
            session_id = self.router.active_session_id()
        if not session_id:
            log.warning(
                "relay -> %s without session_id (no active session), dropping", mtype
            )
            return
        await self._deliver(session_id, msg)

    async def _deliver(self, session_id: str, msg: dict) -> None:
        inst = self.spawner.active().get(session_id)
        if inst is None:
            log.warning(
                "relay -> %s for unknown session %s, dropping", msg.get("type"), session_id
            )
            return
        ws = inst.ws
        if ws is None or getattr(ws, "state", 3) != 1:
            # The instance is still spawning / the extension is reconnecting.
            # Buffer the message so it is delivered once the presence-WS is up.
            # match must not be lost (the extension only starts the rental
            # window when it receives it), and WebRTC signaling (offer / answer
            # / ice_candidate) is one-shot from the agent — the agent does not
            # resend, so dropping it leaves the P2P bridge never established.
            # take_buffer preserves append order, so match → offer → ICE arrive
            # in the order the relay delivered them.
            if msg.get("type") in (
                "match",
                "cdp",
                "webrtc.offer",
                "webrtc.answer",
                "webrtc.ice_candidate",
            ):
                self.spawner.buffer(session_id, msg)
                log.info(
                    "relay -> %s buffered for %s (extension not connected)",
                    msg.get("type"), session_id,
                )
            else:
                log.warning(
                    "relay -> %s: instance %s not connected yet, dropping",
                    msg.get("type"), session_id,
                )
            return
        try:
            await ws.send(json.dumps(msg))
            if msg.get("type") in ("session_ended", "session.kill"):
                # Relay closed the session (renter stop / backend reaper /
                # session_kill): tear the instance down locally after delivery.
                await asyncio.to_thread(
                    self.spawner.destroy, session_id,
                    "ended" if msg.get("type") == "session_ended" else "crashed",
                )
        except Exception as exc:
            log.warning("relay -> %s send failed: %s", msg.get("type"), exc)

    async def _read_loop(self, ws: Any, inbox: asyncio.Queue) -> None:
        """Read + decode relay frames into ``inbox`` (one task per message).

        On socket close, a ``None`` sentinel is queued after the drained frames
        so the consumer loop exits and the reconnect path runs.
        """
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                await inbox.put(msg)
        except websockets.exceptions.ConnectionClosed:
            pass
        except asyncio.CancelledError:
            raise
        finally:
            inbox.put_nowait(None)

    async def run(self) -> None:
        import time as _time
        while not self._stop:
            try:
                async with websockets.connect(
                    self.relay_url,
                    subprotocols=[f"bearer.{self.cfg.token}"],
                    open_timeout=15,
                    ping_interval=None,  # relay drives ping itself
                ) as ws:
                    self.ws = ws
                    self._conn_started = _time.monotonic()
                    log.info("provider-ws: connected %s", self.relay_url)
                    await self.send({
                        "type": "welcome",
                        "auto_accept": True,
                        "capabilities": {"auto_accept": True},
                        "browser_flavor": provider_browser_flavor(),
                        "browser_name": provider_browser_name(),
                        "active_session_id": self.router.active_session_id(),
                    })
                    # Concurrent message handling: a dedicated reader task
                    # decodes frames off the relay socket into an unbounded
                    # queue, and each message is processed in its own task.
                    # This keeps the loop responsive while a `match` handler is
                    # awaiting a slow spawn (asyncio.to_thread) — otherwise the
                    # single `async for` would hold the whole relay socket until
                    # the spawn finished, so a concurrent `provider.alive_probe`
                    # / `ping` (ev 9095 parallel rents) would sit unread past
                    # the relay's 2.5s probe timeout and the relay would mark
                    # the provider offline for the 2nd/3rd session.
                    inbox: asyncio.Queue = asyncio.Queue()
                    reader = asyncio.create_task(self._read_loop(ws, inbox))
                    handlers: set[asyncio.Task] = set()
                    try:
                        while True:
                            msg = await inbox.get()
                            if msg is None:  # sentinel: socket closed, drain done
                                break
                            t = asyncio.create_task(self._on_message(msg))
                            handlers.add(t)
                            t.add_done_callback(handlers.discard)
                    finally:
                        reader.cancel()
                        for t in list(handlers):
                            t.cancel()
                        await asyncio.gather(reader, *handlers, return_exceptions=True)
                        log.warning("provider-ws: relay closed the connection")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning(
                    "provider-ws: error %s (reconnect in %.1fs)",
                    exc, self._reconnect_delay,
                )
            if self._stop:
                break
            self.ws = None
            # Anti-reconnect-storm: if the connection died within a few seconds
            # of connecting (e.g. the relay rejecting/closing us, or a token
            # burst), keep growing the backoff — do NOT reset it on a short
            # lived connection, or we flap 1/s forever. Reset only after the
            # link held for a healthy stretch.
            stayed = _time.monotonic() - getattr(self, "_conn_started", _time.monotonic())
            if stayed >= 10.0:
                self._reconnect_delay = 1.0
            await asyncio.sleep(self._reconnect_delay)
            self._reconnect_delay = min(self._reconnect_delay * 2, 30.0)

    def stop(self) -> None:
        self._stop = True


class LocalWsServer:
    """Local WS endpoint that extensions inside instances connect to.

    Every instance Chrome's extension is configured (via chrome.storage.managed
    policy — see write_managed_policy in entrypoint) with relay_ws pointing at
    ws://127.0.0.1:<daemon_port>. Messages arriving here are multiplexed onto
    the single relay connection; relay messages are pushed into the right
    instance by the ProviderWsClient.

    Stage 1: the first fresh connection belongs to the (only) instance.
    """

    def __init__(self, cfg: DaemonConfig, spawner: SpawnManager, relay: ProviderWsClient):
        self.cfg = cfg
        self.spawner = spawner
        self.relay = relay

    async def handler(self, ws: Any, path: str) -> None:
        # bind this connection to an instance. Multi-rent: the extension sends
        # its rent's session_id as a query param so we bind by session, not
        # "first free" (which is racy with N parallel instances).
        session_id = None
        # path may or may not include the query depending on websockets version;
        # also try the request object when available.
        candidates = []
        if path:
            candidates.append(path)
        try:
            req_path = ws.request.path if ws.request else None
            if req_path:
                candidates.append(req_path)
        except Exception:
            pass
        for p in candidates:
            if p and '?' in p:
                qs = p.split('?', 1)[1]
                for pair in qs.split('&'):
                    if pair.startswith('session_id='):
                        session_id = pair.split('=', 1)[1]
                        break
            if session_id:
                break
        inst = self.spawner.match_ws(ws, session_id)
        if inst is None:
            log.warning("local-ws: no free instance to bind (query sid=%s), closing connection", session_id)
            await ws.close()
            return
        log.info("local-ws: extension connected for session %s (query sid=%s)", inst.session_id, session_id)
        # Flush any relay messages buffered while the instance was spawning.
        # CRITICAL ordering: the extension only creates the rental window when
        # it has a `match` (sets _pendingSession). A CDP/navigate that arrives
        # before the match (renter sends navigate right after match, while the
        # extension is still spawning — seen as `cdp buffered` earlier than
        # `match buffered`) would otherwise be applied with no pending session
        # → `no_session` (-1051). Always deliver `match` first, then the rest
        # in arrival order.
        buffered_msgs = self.spawner.take_buffer(inst.session_id)
        buffered_msgs.sort(key=lambda m: 0 if m.get("type") == "match" else 1)
        for buffered in buffered_msgs:
            try:
                await ws.send(json.dumps(buffered))
                log.info(
                    "local-ws: flushed buffered %s to %s",
                    buffered.get("type"), inst.session_id,
                )
            except Exception as exc:
                log.warning("local-ws: flush failed: %s", exc)
                break
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                mtype = msg.get("type")
                # heartbeat / presence handled locally
                if mtype == "ping":
                    await ws.send(json.dumps({"type": "pong"}))
                    continue
                if mtype == "welcome":
                    # The extension announces itself. Its welcome must NOT be
                    # forwarded to the relay: the relay (providerRouter) treats
                    # ANY welcome from the provider WS as the provider's
                    # capability announcement and overwrites auto_accept /
                    # price_per_min with it. The extension's welcome carries the
                    # panel's own auto_accept (false until the SW pushes state),
                    # so forwarding it clobbers the daemon's real
                    # auto_accept=true and the relay starts sending manual
                    # `offer`s (which the daemon does not answer) → offer_timeout
                    # on parallel rents (ev 9095). Only the daemon's own welcome
                    # (sent once on provider-WS connect) announces capabilities.
                    continue
                if mtype == "session_end":
                    # The extension ended the rental. Forward to the relay (it
                    # finishSession's) and tear the instance down locally.
                    await self.relay.send(msg)
                    await asyncio.to_thread(
                        self.spawner.destroy, inst.session_id, "ended"
                    )
                    continue
                # Everything else → relay (ext→relay direction). Some extension
                # builds send webrtc.answer/ice_candidate with an EMPTY
                # session_id (fallback `?? ""` in p2p-manager when
                # _activeSessionId is unset) — the relay then can't route them
                # to the renter (signaling: no peer map for ""), so P2P never
                # completes. Backfill the session_id from the bound instance.
                if msg.get("type") in ("cdp_response", "cdp_event"):
                    log.info("local-ws: ext->relay %s for %s", msg.get("type"), inst.session_id)
                if isinstance(msg, dict) and not msg.get("session_id"):
                    msg = {**msg, "session_id": inst.session_id}
                await self.relay.send(msg)
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            log.info("local-ws: extension disconnected (session %s)", inst.session_id)
            # If the extension dropped without session_end (crash / hard kill of
            # the Chrome), tear the instance down. A reconnect replaces inst.ws
            # with the new socket, so this must only fire when we still own it.
            if self.spawner.active().get(inst.session_id) is inst and inst.ws is ws:
                await asyncio.to_thread(self.spawner.destroy, inst.session_id, "crashed")


class Daemon:
    def __init__(self, cfg: DaemonConfig):
        self.cfg = cfg
        self.spawner = SpawnManager(cfg)
        self.router = Router(self.spawner)
        self.relay = ProviderWsClient(cfg, self.spawner, self.router)
        self.local = LocalWsServer(cfg, self.spawner, self.relay)
        self._stop_event = threading.Event()
        self._captures: list[Any] = []
        self._server = None

    async def _run(self) -> None:
        self.spawner.startup_sweep()
        async def _log_request(connection, request_headers):
            if request_headers is None:
                log.warning(
                    "local-ws: invalid handshake (no headers) from %s",
                    getattr(connection, "remote_address", "?"),
                )
            return None  # never pre-empt; handler decides

        server = await websockets.serve(
            self.local.handler,
            "127.0.0.1",
            self.cfg.daemon_port,
            ping_interval=None,
            # The extension always connects with a `bearer.<token>` subprotocol
            # (same as it does against the real relay). websockets only
            # negotiates a subprotocol when the server advertises at least one
            # (`available_subprotocols` gate), so list the expected one; the
            # select callback just picks whatever the extension offered so a
            # token rotation never drops a handshake. NB: select_subprotocol is
            # called as (client_subprotocols, server_subprotocols).
            subprotocols=[f"bearer.{self.cfg.token}"],
            select_subprotocol=lambda client, _server: client[0] if client else None,
            process_request=_log_request,
            max_size=16 * 1024 * 1024,
        )
        self._server = server
        log.info("local-ws: listening on ws://127.0.0.1:%d", self.cfg.daemon_port)
        relay_task = asyncio.create_task(self.relay.run())
        watchdog = asyncio.create_task(self._spawn_watchdog())
        try:
            while not self._stop_event.is_set():
                await asyncio.sleep(1)
        finally:
            relay_task.cancel()
            watchdog.cancel()
            for t in (relay_task, watchdog):
                try:
                    await t
                except asyncio.CancelledError:
                    pass
            server.close()
            await server.wait_closed()
            self.spawner.destroy_all("shutdown")

    async def _spawn_watchdog(self) -> None:
        """Destroy instances whose extension never connected within a timeout.

        Under parallel load (ev 9095) a spawned Chrome can occasionally fail to
        get its extension's presence-WS up (offscreen/extension init slowness on
        the 3rd+ concurrent spawn). Without a watchdog such an instance holds
        its CDP/display slot (and profile) forever — the renter's session.end
        can't reach the daemon (no local-WS to relay it) and the slot leaks.
        Periodically reap instances that are older than the connect timeout and
        still not ``ready``.
        """
        connect_timeout = int(os.environ.get("CEKI_DAEMON_CONNECT_TIMEOUT", "90"))
        while True:
            await asyncio.sleep(5)
            for session_id, inst in self.spawner.active().items():
                if inst.ready:
                    continue
                age = time.time() - inst.created_at
                if age > connect_timeout:
                    log.warning(
                        "watchdog: session %s extension never connected after %.0fs — reaping",
                        session_id, age,
                    )
                    await asyncio.to_thread(self.spawner.destroy, session_id, "crashed")

    def run(self) -> int:
        try:
            asyncio.run(self._run())
        except KeyboardInterrupt:
            pass
        return 0


def _setup_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("[%(asctime)s] %(message)s", datefmt="%H:%M:%S"))
    logger = logging.getLogger("ceki.provider")
    logger.handlers.clear()
    logger.addHandler(handler)
    level = os.environ.get("CEKI_PROVIDER_LOG_LEVEL", "").upper()
    logger.setLevel(getattr(logging, level, logging.INFO) if level else logging.INFO)
    logger.propagate = False


def main(argv: list[str] | None = None) -> int:
    _setup_logging()
    # Reap zombie children. We are PID 1 in the container: no init will collect
    # them, so every destroyed Chrome/Xvfb left a zombie behind. Accumulated
    # zombies hog pid/process-table and slow the host — with parallel spawns the
    # Nth Chrome loses resources and its extension never connects. SIGCHLD fires
    # on child exit; waitpid(-1, WNOHANG) drains everything ready to reap.
    def _on_sigchld(_signum, _frame):
        try:
            while True:
                pid, _status = os.waitpid(-1, os.WNOHANG)
                if pid == 0:
                    break
        except (ChildProcessError, OSError):
            pass

    try:
        signal.signal(signal.SIGCHLD, _on_sigchld)
    except Exception:
        pass
    cfg = load_config()
    log.info(
        "daemon: schedule=%s sessions=%d cdp_pool=%s..%s displays=%s..%s storage_key=%s persist=%s",
        cfg.schedule_id,
        cfg.max_sessions,
        cfg.cdps[0], cfg.cdps[-1],
        cfg.displays[0], cfg.displays[-1],
        cfg.storage_key,
        cfg.persist,
    )
    daemon = Daemon(cfg)
    return daemon.run()


if __name__ == "__main__":
    sys.exit(main())
