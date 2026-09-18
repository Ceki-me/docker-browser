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

import httpx
import websockets

from ceki_browser_provider import app as provider_app

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


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

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

    max_sessions = int(os.environ.get("CEKI_DAEMON_MAX_SESSIONS", "1"))
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
        cdps=list(range(cdp_start, cdp_start + max_sessions)),
        displays=list(range(display_start, display_start + max_sessions)),
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
                    return inst
            for inst in self._instances.values():
                if inst.ws is None or inst.ws.state == 3:  # CLOSED
                    inst.ws = ws
                    inst.ready = True
                    return inst
            # Overwrite: rebind the only instance (reconnect).
            if len(self._instances) == 1:
                inst = next(iter(self._instances.values()))
                inst.ws = ws
                inst.ready = True
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
        fresh = dst_p.joinpath("manifest.json").exists()
        already = fresh and self._patch_stamp == self.cfg.local_ws_url
        if not already:
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
        args.append(f"--window-size={self.cfg.width},{self.cfg.height}")
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
        return proc

    def _launch_xvfb(self, inst: Instance) -> subprocess.Popen:
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
        return proc

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
        prefs_path.write_text(json.dumps(prefs))
        log.info("two-launch: incognito granted for %s", ext_id)

        # 4) run phase: relaunch the same profile, now with incognito + token.
        proc2 = self._launch_chrome(inst, chrome_args)
        inst.chrome_pid = proc2.pid
        self._discover_ext_id(inst, timeout=30)
        # 5) token handshake via the extension panel over CDP.
        self._handshake(inst)
        # 6) opportunistic: drop Yandex's default background tabs (ya.ru,
        #    chrome://wallpaper, alissenger) that eat ~1GB on an idle rent.
        #    Deferred so the extension's session tab (about:blank) opens first;
        #    failures are non-fatal.
        threading.Timer(2.0, self._close_idle_tabs, args=(inst,)).start()

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
                    return found
            time.sleep(0.5)
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
        # for it before giving up.
        sw_ws = None
        for _ in range(40):
            sw_ws = self._find_target_ws(inst, "service_worker", _DEFAULT_EXT_ID)
            if sw_ws is not None:
                break
            time.sleep(0.5)
        if sw_ws is None:
            sw_ws = self._find_target_ws(inst, "background_page", _DEFAULT_EXT_ID)
        if sw_ws is None:
            log.warning("handshake: extension service worker target not found")
            return

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
        expr = (
            "chrome.storage.local.set("
            + json.dumps(payload)
            + ", () => true)"
        )
        try:
            result = self._cdp_eval(sw_ws, expr)
            log.info("handshake: storage set -> %s", json.dumps(result)[:200])
        except Exception as exc:
            log.warning("handshake failed: %s", exc)
        self._retry_token_after_offscreen(inst, sw_ws, payload)

    def _retry_token_after_offscreen(
        self, inst: Instance, sw_ws: str, payload: dict
    ) -> None:
        """Re-deliver the token once the offscreen document is up.

        The first ``storage.local.set`` fires ``storage.onChanged`` immediately,
        while the SW's ``offscreenPort`` may still be null (offscreen document
        not created yet). ``sendToOffscreen(token_updated)`` is then a no-op and
        the message is lost; when offscreen later connects, ``offscreen_hello``
        sees ``sanctum_token === _lastKnownToken`` and skips ``token_updated`` —
        so the offscreen never opens its presence-WS (QA repro, ev 8639).

        Fix on the daemon side (no extension change): wait for the offscreen
        page target, then clear the token and re-set it. The second
        ``onChanged`` fires with the live port: ``token_cleared`` then
        ``token_updated`` reach the offscreen, which connects to the local WS.
        """
        off_ws = None
        for _ in range(60):  # up to ~30s for offscreen to appear
            off_ws = self._find_offscreen_target(inst)
            if off_ws is not None:
                break
            time.sleep(0.5)
        if off_ws is None:
            log.warning("handshake: offscreen target never appeared, token may be lost")
            return

        # Give the offscreen a moment to deliver the first token set into its
        # presence-WS. If the extension already connected, the handshake
        # succeeded — do NOT clear the token. The offscreen treats token=null
        # as an intentional close (offscreen.ts onTokenFromSw), and the local
        # WS handler would then misread that disconnect as a crash and destroy
        # the instance mid-spawn (the parallel-rent crash, ev 9095: 2nd/3rd
        # session destroyed (crashed) right after extension connected, token
        # re-set failed with Connect call failed on the port).
        for _ in range(6):  # up to ~3s for the WS to come up
            ws = inst.ws
            if ws is not None and getattr(ws, "state", 3) == 1:
                log.info(
                    "handshake: presence-WS already connected, token delivered — skipping re-delivery"
                )
                return
            time.sleep(0.5)

        # Re-check right before the destructive clear: if the extension
        # connected during the window above (token was delivered), skip.
        ws = inst.ws
        if ws is not None and getattr(ws, "state", 3) == 1:
            log.info("handshake: presence-WS connected during wait, skipping re-delivery")
            return

        # Clear then re-set: onChanged fires token_cleared then token_updated,
        # now delivering into the live offscreenPort.
        try:
            self._cdp_eval(sw_ws, "chrome.storage.local.remove('sanctum_token', () => true)")
            log.info("handshake: token cleared for re-delivery")
        except Exception as exc:
            log.warning("handshake: token clear failed: %s", exc)
        time.sleep(0.7)  # let the onChanged debounce (50ms + tick) process
        try:
            self._cdp_eval(
                sw_ws,
                "chrome.storage.local.set(" + json.dumps(payload) + ", () => true)",
            )
            log.info("handshake: token re-set after offscreen ready")
        except Exception as exc:
            log.warning("handshake: token re-set failed: %s", exc)

    def _find_target_ws(self, inst: Instance, kind: str, ext_id: str) -> str | None:
        """Return the webSocketDebuggerUrl of the first target of ``kind`` whose
        URL belongs to ``ext_id``."""
        cdp = f"http://127.0.0.1:{inst.cdp_port}"
        try:
            targets = httpx.get(f"{cdp}/json/list", timeout=2).json()
        except Exception:
            return None
        for t in targets:
            if t.get("type") != kind:
                continue
            url = t.get("url") or ""
            if f"chrome-extension://{ext_id}/" in url:
                return t.get("webSocketDebuggerUrl")
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

    def _cdp_eval(self, ws_url: str, expression: str, timeout: float = 20.0) -> Any:
        """Evaluate ``expression`` in a CDP target via its webSocketDebuggerUrl."""
        async def _run() -> Any:
            async with websockets.connect(ws_url, max_size=64 * 1024 * 1024) as sock:
                request_id = 1
                await sock.send(json.dumps({
                    "id": request_id,
                    "method": "Runtime.evaluate",
                    "params": {
                        "expression": expression,
                        "awaitPromise": True,
                        "returnByValue": True,
                    },
                }))
                while True:
                    msg = json.loads(await asyncio.wait_for(sock.recv(), timeout=timeout))
                    if msg.get("id") == request_id:
                        if "error" in msg:
                            raise RuntimeError(f"CDP error: {msg['error']}")
                        res = msg.get("result", {}).get("result", {})
                        return res.get("value")
        return asyncio.run(_run())

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
        if mtype == "ping":
            await self.send({"type": "pong"})
            return
        if mtype == "provider.alive_probe":
            await self.send({"type": "provider.alive_ack"})
            return
        if mtype == "match":
            session_id = msg.get("session_id")
            if not session_id:
                log.warning("relay match without session_id, dropping")
                return
            inst = self.spawner.active().get(session_id)
            if inst is None:
                inst = await asyncio.to_thread(self.spawner.ensure, session_id, msg)
            if inst is None:
                return
            await self._deliver(session_id, msg)
            return
        # other relay → session messages: route by session_id
        session_id = msg.get("session_id") or msg.get("event_id")
        if mtype == "cdp":
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
        while not self._stop:
            try:
                async with websockets.connect(
                    self.relay_url,
                    subprotocols=[f"bearer.{self.cfg.token}"],
                    open_timeout=15,
                    ping_interval=None,  # relay drives ping itself
                ) as ws:
                    self.ws = ws
                    self._reconnect_delay = 1.0
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
            log.warning("local-ws: no free instance to bind, closing connection")
            await ws.close()
            return
        log.info("local-ws: extension connected for session %s", inst.session_id)
        # Flush any relay messages buffered while the instance was spawning.
        for buffered in self.spawner.take_buffer(inst.session_id):
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
