"""Provider launcher: rent out this machine's browser through Ceki.

Deploys a real Chromium (headed, run under Xvfb) with the Ceki extension loaded,
injects the provider token, brings the browser online as a public provider and
keeps the process alive while auto-accepting incoming rentals.

The extension implements the provider WebSocket protocol (welcome / accept / cdp /
webrtc over its own relay connection); this module is only the launcher: browser +
extension + token handshake + online poll + liveness.

CLI entry:
    ceki-provider [--token TOKEN] [--ext-dir DIR] [--api-url URL]
                  [--schedule-id ID] [--timeout SECONDS]

Environment variables:
    CEKI_PROVIDER_TOKEN        extension token issued for this browser (required)
    CEKI_PROVIDER_EXT_DIR      path to the unpacked Ceki extension dist
                               (required unless ``--ext-dir`` is passed)
    CEKI_API_URL               backend API base (default https://api.ceki.me)
    CEKI_PROVIDER_SCHEDULE_ID  optional; usually derived from /api/browser/me
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import shutil
import signal
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from ceki_sdk._config import default_api_url
from ceki_browser_provider import provider_debug

log = logging.getLogger("ceki.provider")

# Default viewport / resolution. The container is a headless FHD provider, so
# Full HD is the default; CEKI_PROVIDER_VIEWPORT (WxH) overrides it.
DEFAULT_VIEWPORT = "1920x1080"


def _parse_viewport(raw: str | None) -> tuple[int, int]:
    """Parse CEKI_PROVIDER_VIEWPORT (format WxH) into (width, height).

    Defaults to 1920x1080. Falls back to the default on unparseable or
    non-positive input so a bad env value never crashes provider launch.
    """
    value = (raw or DEFAULT_VIEWPORT).strip().lower()
    try:
        w_s, h_s = value.split("x", 1)
        w, h = int(w_s), int(h_s)
    except (ValueError, AttributeError):
        log.warning("CEKI_PROVIDER_VIEWPORT=%r unparseable, using %s", raw, DEFAULT_VIEWPORT)
        return 1920, 1080
    if w <= 0 or h <= 0:
        log.warning("CEKI_PROVIDER_VIEWPORT=%r invalid size, using %s", raw, DEFAULT_VIEWPORT)
        return 1920, 1080
    return w, h


VIEWPORT_WIDTH, VIEWPORT_HEIGHT = _parse_viewport(os.environ.get("CEKI_PROVIDER_VIEWPORT"))

# Stable extension id (derived from the public manifest key).  Used to grant
# incognito access to the unpacked extension in the Chromium profile.
DEFAULT_EXT_ID = "gfionhbdkojjnjpbhlblopoaecdpllhb"

# Base Chromium args shared by every launch. The extension is NOT loaded via
# --load-extension here: it is installed by Chrome itself from the external
# update policy (/usr/share/chromium/extensions/<id>.json → external_update_url),
# which makes Chrome auto-update it in the background (checks the update channel
# every few hours) — so a long-running provider container stays on the latest
# extension build without ever restarting. The unpacked --load-extension path
# is kept as a fallback (see _launch_provider) for offline / local runs where
# no external channel is reachable.
_CHROME_ARGS = [
    "--no-sandbox",
    "--disable-gpu",
    "--disable-dev-shm-usage",
    # skip first-run/default-browser prompts: faster, more deterministic cold
    # start inside a container (fewer surprises on the very first launch)
    "--no-first-run",
    "--no-default-browser-check",
    # self-fingerprint quality fixes:
    # fake media devices make AudioContext non-empty (removes the
    # "Audio context empty (headless indicator)" consistency penalty)
    "--use-fake-ui-for-media-stream",
    "--use-fake-device-for-media-stream",
    # kill the remaining "Chrome is being controlled" automation marker
    "--disable-blink-features=AutomationControlled",
    # deterministic, consistent accept-language/locale
    "--lang=en-US",
]

# Unpacked-extension flags, appended only in the fallback launch path.
_LOAD_EXT_ARGS = [
    "--disable-extensions-except={ext_dir}",
    "--load-extension={ext_dir}",
]

# Where Chrome looks for an external-extension policy (unbranded Chromium):
# the JSON file named <extension-id>.json with an external_update_url.
# Yandex Browser (corporate build) does not read these paths at all — its own
# managed-policy root and the ExtensionInstallForcelist form are handled
# separately (see _external_policy_update_url and the entrypoint).
_EXTERNAL_POLICY_DIRS = [
    "/usr/share/chromium/extensions",
    "/etc/chromium/extensions",
    "/usr/share/chrome/extensions",
    "/etc/opt/chrome/extensions",
]

# Browser flavor selection. The image ships Playwright's Chromium as the
# default provider browser; the yandex flavor (image ceki/provider:yandex)
# adds Yandex Browser and selects it via env — the same launcher code drives
# either binary.
_BROWSER_FLAVORS = {
    "chromium": None,  # None → Playwright's pinned Chromium (default)
    "yandex": "/usr/bin/yandex-browser",
    # pseudo-yandex: plain Playwright Chromium pretending to be YaBrowser —
    # UA string + Client Hints override at launch (see _UA_OVERRIDE / apply).
    # NOT a real Yandex build: no Yandex internals, ytrust, config channels.
    "pseudo-yandex": None,
}

# pseudo-yandex UA. Yandex Browser's UA format is
#   ...Chrome/<base> YaBrowser/<major>.<minor>.0.0 Safari/537.36
# (stable 26.8.1 → "Chrome/150.0.0.0 YaBrowser/26.8.0.0"; verified live).
# The Chromium major here tracks the current Yandex stable major; bump it
# when rebuilding the image. Chrome full version below must match the
# Playwright Chromium major actually in the image (JS APIs, Sec-CH-UA).
_PSEUDO_UA_CHROMIUM_MAJOR = "150"
_PSEUDO_UA_CHROMIUM_FULL = "151.0.7922.34"
_PSEUDO_YABROWSER_MAJOR = "26"
_PSEUDO_YABROWSER_MINOR = "8"
_PSEUDO_UA = (
    f"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    f"Chrome/{_PSEUDO_UA_CHROMIUM_MAJOR}.0.0.0 "
    f"YaBrowser/{_PSEUDO_YABROWSER_MAJOR}.{_PSEUDO_YABROWSER_MINOR}.0.0 Safari/537.36"
)


def _apply_ua_override(context: Any) -> None:
    """pseudo-yandex: pin the UA string and Client Hints on every existing page.

    ``user_agent=`` at launch covers the HTTP header and navigator.userAgent;
    Client Hints (navigator.userAgentData, Sec-CH-UA headers) are a separate
    layer and would otherwise expose the real Chromium — overridden per page
    via the CDP Network domain. Runs best-effort: any failure logs and moves
    on rather than blocking provider startup.
    """
    if _BROWSER_FLAVOR != "pseudo-yandex":
        return
    brands = [
        {"brand": "Not A(Brand", "version": "99.0.0.0"},
        {"brand": "Yandex", "version": f"{_PSEUDO_YABROWSER_MAJOR}.{_PSEUDO_YABROWSER_MINOR}.0.0"},
        {"brand": "Chromium", "version": _PSEUDO_UA_CHROMIUM_MAJOR},
    ]

    def patch(page: Any) -> None:
        try:
            _apply_ua_override_single(page)
        except Exception as exc:
            log.debug("ua override failed on a page: %s", exc)

    pages = list(getattr(context, "pages", []))
    for page in pages:
        patch(page)
    if pages:
        log.info("pseudo-yandex: UA override applied to %d page(s)", len(pages))


def _apply_ua_override_to_page(page: Any) -> None:
    """``page`` event handler: patch UA/Client Hints on a newly-opened page.

    The CDP session must be created after the page's session exists; on the
    very first moment of a page's life the target may not be attached yet, so
    retry briefly. Navigation itself does not reset the Network-domain override.
    """
    for _ in range(10):
        try:
            _apply_ua_override_single(page)
            return
        except Exception:
            time.sleep(0.3)
    log.debug("pseudo-yandex: gave up UA override for a new page")


def _apply_ua_override_single(page: Any) -> None:
    """Patch one page's UA + Client Hints via a fresh CDP session."""
    session = page.context.new_cdp_session(page)
    brands = [
        {"brand": "Not A(Brand", "version": "99.0.0.0"},
        {"brand": "Yandex", "version": f"{_PSEUDO_YABROWSER_MAJOR}.{_PSEUDO_YABROWSER_MINOR}.0.0"},
        {"brand": "Chromium", "version": _PSEUDO_UA_CHROMIUM_MAJOR},
    ]
    session.send(
        "Network.setUserAgentOverride",
        {
            "userAgent": _PSEUDO_UA,
            "acceptLanguage": "en-US,en;q=0.9",
            "userAgentMetadata": {
                "brands": brands,
                "fullVersionList": brands,
                "fullVersion": _PSEUDO_UA_CHROMIUM_FULL,
                "platform": "Linux",
                "platformVersion": "6.1.0",
                "architecture": "x86",
                "model": "",
                "mobile": False,
                "bitness": "64",
                "wow64": False,
            },
        },
    )
_BROWSER_FLAVOR = os.environ.get("CEKI_PROVIDER_BROWSER", "chromium").strip().lower()


def _browser_binary() -> str | None:
    """Executable path for the provider browser (None = Playwright Chromium)."""
    if _BROWSER_FLAVOR not in _BROWSER_FLAVORS:
        log.warning(
            "CEKI_PROVIDER_BROWSER=%r unknown (known: %s); using Chromium",
            _BROWSER_FLAVOR,
            ", ".join(_BROWSER_FLAVORS),
        )
        return None
    binary = _BROWSER_FLAVORS[_BROWSER_FLAVOR]
    if binary and not Path(binary).exists():
        log.warning(
            "CEKI_PROVIDER_BROWSER=yandex but %s missing (chromium image?); "
            "falling back to Playwright Chromium",
            binary,
        )
        return None
    return binary

# JS helpers injected into the extension panel page.  ``arg`` is supplied by
# Playwright when the arrow function is evaluated (see page.evaluate).
_HANDSHAKE_JS = r"""
async (args) => {
  const result = { ok: false, method: 'storage' };
  try {
    await chrome.storage.local.set({ extensionInstanceId: args.instanceId });
    // Legacy path (ZIP builds): the extension resolves the token itself.
    const tokenResult = await Promise.race([
      new Promise((resolve) => {
        chrome.runtime.sendMessage(
          {
            type: 'EXT_TOKEN_RECEIVED',
            token: args.token,
            schedule_id: args.scheduleId || undefined,
          },
          (resp) => resolve(chrome.runtime.lastError ? null : resp)
        );
      }),
      new Promise((r) => setTimeout(() => r(null), 3000)),
    ]);
    if (tokenResult && tokenResult.ok) {
      const goOnline = await new Promise((resolve, reject) => {
        chrome.runtime.sendMessage(
          { type: 'go-online' },
          (resp) => chrome.runtime.lastError ? reject(chrome.runtime.lastError) : resolve(resp)
        );
      });
      result.tokenResult = tokenResult;
      result.goOnline = goOnline;
      result.ok = true;
      result.method = 'legacy';
      return result;
    }
    // dist builds: validate the token via the API and store it directly.
    const resp = await fetch(args.apiBase + '/api/browser/me', {
      headers: { 'Authorization': 'Bearer ' + args.token },
      credentials: 'omit',
    });
    if (!resp.ok) {
      result.error = 'browser_me_' + resp.status;
      return result;
    }
    const browser = await resp.json();
    await chrome.storage.local.set({
      sanctum_token: args.token,
      ceki_browser: browser,
      paired_at: Date.now(),
      incognito_available: true,
      auto_accept: true,
    });
    result.ok = true;
    result.browser = { id: browser.id, online: browser.online };
    return result;
  } catch (e) {
    result.error = String((e && e.message) || e);
    return result;
  }
}
"""

_READ_STORED_JS = r"""
async () => {
  const s = await chrome.storage.local.get(['currentToken', 'sanctum_token', 'ceki_browser']);
  if (s.currentToken) {
    return { token: s.currentToken.token, schedule_id: s.currentToken.schedule_id };
  }
  if (s.sanctum_token) {
    return { token: s.sanctum_token, schedule_id: s.ceki_browser ? s.ceki_browser.id : null };
  }
  return null;
}
"""

_CLEAR_HEARTBEAT_JS = r"""
async () => { try { await chrome.alarms.clear('ceki-heartbeat'); } catch (e) {} }
"""

_PATCH_INCOGNITO_JS = r"""
() => { chrome.extension.isAllowedIncognitoAccess = (cb) => cb(true); }
"""

_PATCH_INCOGNITO_RECHECK_JS = r"""
() => new Promise((resolve) => {
  chrome.extension.isAllowedIncognitoAccess((ok) => {
    chrome.storage.local.set({ incognito_available: ok, incognito_checked_at: Date.now() });
    resolve(ok);
  });
})
"""


class ProviderError(Exception):
    """Raised when the provider cannot be deployed or brought online."""


def _env_int(*names: str) -> int | None:
    for name in names:
        raw = os.environ.get(name)
        if raw:
            try:
                return int(raw)
            except ValueError:
                log.warning("ignoring non-integer %s=%r", name, raw)
    return None


def resolve_token(token: str | None = None) -> str:
    """Resolve the provider extension token from arg or environment."""
    value = (
        token
        or os.environ.get("CEKI_PROVIDER_TOKEN")
        or os.environ.get("PROVIDER_TOKEN")
        or ""
    ).strip()
    if not value:
        raise ProviderError(
            "Provider token is required: set CEKI_PROVIDER_TOKEN or pass --token"
        )
    return value


def resolve_ext_dir(explicit: str | None = None) -> str:
    """Resolve the extension dist directory.

    Order: ``--ext-dir`` arg, ``CEKI_PROVIDER_EXT_DIR``, ``CEKI_EXT_DIR``,
    then a bundled copy next to the package (``ceki_sdk/provider_assets/extension``).
    """
    candidates = [
        explicit,
        os.environ.get("CEKI_PROVIDER_EXT_DIR"),
        os.environ.get("CEKI_EXT_DIR"),
        str(Path(__file__).resolve().parent / "provider_assets" / "extension"),
    ]
    for cand in candidates:
        if cand and Path(cand, "manifest.json").is_file():
            return os.path.abspath(cand)
    raise ProviderError(
        "Ceki extension dist not found. Pass --ext-dir or set CEKI_PROVIDER_EXT_DIR "
        "to the unpacked extension directory (must contain manifest.json)."
    )


def resolve_api_base(explicit: str | None = None) -> str:
    value = explicit or os.environ.get("CEKI_API_URL") or default_api_url()
    value = value.rstrip("/")
    # Accept both "https://host" and "https://host/api" (QA profiles use the
    # latter). Provider request paths are built as f"{base}/api/...", so the
    # base must be the host root.
    if value.endswith("/api"):
        value = value[: -len("/api")]
    return value


def _ensure_display() -> None:
    """Run under Xvfb when no display is available (e.g. inside a Docker container).

    Re-executes the current process through ``xvfb-run -a`` so the headed browser
    has a virtual screen.  Never returns when a re-exec happens.
    """
    if os.environ.get("DISPLAY"):
        return
    xvfb = shutil.which("xvfb-run")
    if not xvfb:
        raise ProviderError(
            "No DISPLAY is set and xvfb-run is not installed. "
            "Run under a display (e.g. xvfb-run -a ceki provider run) or install xvfb."
        )
    # Rebuild the command for how we were launched.  ``python -m`` sets argv[0]
    # to the module file (not executable on its own), so re-invoke via sys.executable.
    main_mod = sys.modules.get("__main__")
    spec = getattr(main_mod, "__spec__", None) if main_mod else None
    if spec and getattr(spec, "name", None):
        args = [sys.executable, "-m", spec.name, *sys.argv[1:]]
    else:
        args = list(sys.argv)
    log.info("no DISPLAY set — re-execing under xvfb-run: %s", " ".join(args))
    os.execvp(xvfb, [xvfb, "-a", *args])


def _setup_logging(verbose: bool = False) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("[%(asctime)s] %(message)s", datefmt="%H:%M:%S"))
    logger = logging.getLogger("ceki.provider")
    logger.handlers.clear()
    logger.addHandler(handler)
    level = os.environ.get("CEKI_PROVIDER_LOG_LEVEL", "").upper()
    if verbose or level == "DEBUG":
        logger.setLevel(logging.DEBUG)
    elif level:
        logger.setLevel(getattr(logging, level, logging.INFO))
    else:
        logger.setLevel(logging.INFO)
    logger.propagate = False


def _install_signal_handlers(stop: threading.Event) -> None:
    def handler(signum: int, _frame: Any) -> None:  # pragma: no cover - signal path
        log.info("signal %s received, shutting down", signum)
        stop.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError):  # pragma: no cover - non-main-thread edge
            pass


@dataclass
class ProviderContext:
    browser_context: Any
    profile_dir: str
    extension_id: str
    token: str
    schedule_id: int | None
    api_base: str
    debug_capture: Any = None
    debug_stop: threading.Event | None = None


def _ensure_timezone() -> None:
    """Ensure a non-UTC TZ is set so the provider browser matches the IP geolocation.

    Priority: the ``TZ`` env var (docker-compose passes the host TZ via
    ``TZ=${TZ:-}``), then ``/etc/timezone`` (Debian images).  Chromium launched
    by Playwright inherits ``TZ`` and reports the matching local time, which
    removes the "IP tz != browser tz" leak penalty in the self-fingerprint scan.
    Best effort — never raises, never hardcodes a region.
    """
    tz = os.environ.get("TZ")
    if not tz:
        try:
            tzfile = Path("/etc/timezone")
            if tzfile.is_file():
                tz = tzfile.read_text().strip()
        except Exception:
            tz = None
    if not tz:
        return
    os.environ["TZ"] = tz
    try:
        if hasattr(time, "tzset"):
            time.tzset()
    except Exception:
        pass
    log.info("provider timezone: %s", tz)


def _external_policy_update_url() -> str | None:
    """Return the update-channel URL of the external-extension policy, if any.

    Chromium (unbranded): a JSON file named ``<extension-id>.json`` in a policy
    directory (e.g. ``/usr/share/chromium/extensions/``) with an
    ``external_update_url`` — Chrome installs and auto-updates the extension
    from that channel. When such a policy exists we launch Chromium without
    ``--load-extension`` and let Chrome itself install the extension — which
    also means Chrome keeps updating it in the background (checks every few
    hours), the point of this whole mode.

    Yandex Browser (corporate build, yandex flavor): only the managed-policy
    root works, and only the ``ExtensionInstallForcelist`` form — the policy
    file is ``/etc/opt/yandex/browser/policies/managed/ceki.json`` written by
    the entrypoint, listing ``<id>;<updates.xml>`` pairs.

    Returns ``None`` when no policy is found (then the launcher falls back to
    the unpacked ``--load-extension`` path).
    """
    # Yandex corporate-policy root first (its forcelist form), then the
    # chromium external_update_url files.
    yandex_policy = Path("/etc/opt/yandex/browser/policies/managed")
    if _BROWSER_FLAVOR == "yandex" and yandex_policy.is_dir():
        try:
            for f in yandex_policy.glob("*.json"):
                try:
                    data = json.loads(f.read_text())
                except Exception:
                    continue
                for entry in data.get("ExtensionInstallForcelist") or []:
                    if isinstance(entry, str) and ";" in entry:
                        _ext_id, url = entry.split(";", 1)
                        if url:
                            log.info("external extension policy (yandex forcelist): %s -> %s", f, url)
                            return url
        except OSError:
            pass
    dirs = list(_EXTERNAL_POLICY_DIRS)
    for d in dirs:
        p = Path(d)
        if not p.is_dir():
            continue
        try:
            for f in p.glob("*.json"):
                try:
                    data = json.loads(f.read_text())
                except Exception:
                    continue
                url = data.get("external_update_url")
                if isinstance(url, str) and url:
                    log.info("external extension policy: %s -> %s", f, url)
                    return url
        except OSError:
            continue
    return None


def _ext_id_from_manifest(ext_dir: str) -> str | None:
    """Deterministic extension ID for an unpacked extension with a public ``key``.

    Chrome derives the ID of an unpacked extension from the DER-encoded public
    key in ``manifest.json``: ``sha256(key)`` → first 16 bytes → each hex nibble
    mapped to ``a``..``p`` (32 chars). Computing it up front removes the flaky
    service-worker timing race entirely. Returns ``None`` when the manifest has
    no ``key`` (then the caller falls back to runtime discovery).
    """
    try:
        manifest = json.loads((Path(ext_dir) / "manifest.json").read_text())
        key = manifest.get("key")
        if not key:
            return None
        digest = hashlib.sha256(base64.b64decode(key)).hexdigest()[:32]
        return "".join(chr(ord("a") + int(c, 16)) for c in digest)
    except Exception as exc:
        log.warning("could not derive ext id from manifest key: %s", exc)
        return None


def _discover_ext_id(
    context: Any,
    wait_s: float = 45.0,
    expected: str | None = None,
) -> str | None:
    """Find the loaded extension ID.

    When ``expected`` is known (from the manifest key) we accept any extension
    target (service worker **or** page) that matches it.  The reliable trigger
    is opening the extension panel page: a ``chrome-extension://`` page target
    appears the moment the extension is *registered* by Chromium, which happens
    well before the background service worker *starts* (the slow, timing-flaky
    part on a cold first launch).  Without ``expected`` we fall back to
    scanning every target's URL for any extension id.
    """
    deadline = time.time() + wait_s
    last_force = 0.0
    while time.time() < deadline:
        for target in list(context.service_workers) + list(context.pages):
            m = re.search(r"chrome-extension://([a-z]+)/", target.url)
            if m and (expected is None or m.group(1) == expected):
                return m.group(1)
        # Force-activate: opening the panel creates a matching page target as
        # soon as the extension is registered.  Throttled — no point spamming
        # the browser while the profile is still spinning up.
        if expected and time.time() - last_force >= 3.0:
            last_force = time.time()
            page = None
            opened = False
            try:
                page = context.new_page()
                for path in ("panel/index.html", "panel.html", "popup.html"):
                    try:
                        page.goto(
                            f"chrome-extension://{expected}/{path}",
                            wait_until="domcontentloaded",
                            timeout=4000,
                        )
                        opened = True
                        break
                    except Exception:
                        continue
            except Exception:
                pass
            finally:
                if page is not None and not opened:
                    _close_blocked_page(page)
        time.sleep(1)
    return None


def _close_blocked_page(page: Any) -> None:
    """Close a probe page without deadlocking.

    A ``chrome-extension://`` navigation that fails with ERR_BLOCKED_BY_CLIENT
    (extension installed by the external policy but not yet enabled in this
    browser session) leaves the page's load stuck; ``page.close()`` then blocks
    forever waiting for that load to finish.  Navigating to ``about:blank``
    first aborts the stuck load so ``close()`` returns immediately.  The
    ``about:blank`` goto itself may fail (interrupted navigation) — that is the
    point, and it is safe to ignore.
    """
    try:
        page.goto("about:blank", wait_until="domcontentloaded", timeout=1000)
    except Exception:
        pass
    try:
        page.close()
    except Exception:
        pass


def _open_panel(browser_context: Any, ext_id: str) -> Any | None:
    page = browser_context.new_page()
    for path in ("panel/index.html", "panel.html", "popup.html"):
        try:
            page.goto(
                f"chrome-extension://{ext_id}/{path}",
                wait_until="domcontentloaded",
                timeout=10_000,
            )
            return page
        except Exception:
            continue
    _close_blocked_page(page)
    return None


def _browser_status(api_base: str, token: str) -> str:
    """Poll /api/browser/me once.  Returns ``online`` / ``offline`` / status string."""
    try:
        resp = httpx.get(
            f"{api_base}/api/browser/me",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            if data.get("online") is True or data.get("status") == "online":
                return "online"
            return data.get("status") or ("offline" if not data.get("online") else "online")
        log.info("api check: status=%s", resp.status_code)
    except httpx.HTTPError as exc:
        log.info("api check failed: %s", exc)
    return "offline"


def _poll_online(
    api_base: str,
    token: str,
    attempts: int = 8,
    interval: float = 5.0,
) -> str:
    for i in range(attempts):
        status = _browser_status(api_base, token)
        if status == "online":
            return status
        log.info("provider not online yet: status=%s (attempt %d/%d)", status, i + 1, attempts)
        if i < attempts - 1:
            time.sleep(interval)
    return "offline"


def _maximize_window(page: Any) -> None:
    """Force the browser window to the full framebuffer via CDP.

    Xvfb runs without a window manager, so --start-maximized is a no-op and
    Playwright appends its own default --window-size=1280,800 after our args
    (that one wins on the command line). Browser.setWindowBounds is applied by
    Chromium itself through X11 ConfigureWindow — no WM needed — so it resizes
    reliably. Position is already pinned at 0,0 by --window-position, so the
    size is not clamped against a cascade offset.
    """
    try:
        cdp = page.context.new_cdp_session(page)
        win = cdp.send("Browser.getWindowForTarget")
        cdp.send(
            "Browser.setWindowBounds",
            {
                "windowId": win["windowId"],
                "bounds": {
                    "left": 0,
                    "top": 0,
                    "width": VIEWPORT_WIDTH,
                    "height": VIEWPORT_HEIGHT,
                    "windowState": "normal",
                },
            },
        )
        log.info(
            "window bounds set via CDP: %dx%d at 0,0", VIEWPORT_WIDTH, VIEWPORT_HEIGHT
        )
        # Playwright pins the renderer to the initial (1280x800) window via
        # device-metrics emulation; after the CDP resize the browser UI follows
        # the new window but the page keeps the old viewport. Clearing the
        # override lets the page reflow to the full window.
        try:
            cdp.send("Emulation.clearDeviceMetricsOverride")
        except Exception as exc:
            log.debug("clearDeviceMetricsOverride: %s", exc)
    except Exception as exc:
        log.warning("window maximize via CDP failed: %s", exc)


def _launch_provider(
    playwright: Any,
    *,
    token: str,
    ext_dir: str,
    api_base: str,
    schedule_id: int | None,
) -> ProviderContext:
    _ensure_timezone()

    # Ждём X-сокет: entrypoint запускает Xvfb и сразу exec'ит python — chromium
    # может стартовать раньше, чем Xvfb поднял сокет, и падать
    # "headed browser without XServer". Ждём появления /tmp/.X11-unix/X<N>.
    disp = os.environ.get("DISPLAY", ":99")
    x_sock = f"/tmp/.X11-unix/X{disp.lstrip(':')}"
    for _ in range(40):
        if os.path.exists(x_sock):
            break
        time.sleep(0.5)
    else:
        log.warning("X socket %s not ready after 20s, continuing anyway", x_sock)

    chromium = playwright.chromium
    debug = provider_debug.config_from_env()
    profile_dir = tempfile.mkdtemp(prefix="ceki-provider-")
    default_dir = Path(profile_dir) / "Default"
    default_dir.mkdir(parents=True, exist_ok=True)

    # External-extension mode: a policy JSON (e.g. /usr/share/chromium/extensions/
    # <id>.json) with an external_update_url is present, so Chrome itself installs
    # the extension from the update channel and keeps auto-updating it. The
    # unpacked --load-extension path is only a fallback (offline / no policy).
    external_url = _external_policy_update_url()
    load_ext = external_url is None
    if load_ext:
        log.info("extension mode: unpacked --load-extension (%s)", ext_dir)
        if _BROWSER_FLAVOR == "yandex":
            log.warning(
                "yandex flavor without an install policy: the Yandex builds strip "
                "--load-extension, so the unpacked fallback CANNOT load the extension "
                "here — the provider will run without it. Policy comes from the "
                "entrypoint; do not set CEKI_EXT_SKIP_UPDATE / CEKI_WS_URL on yandex."
            )
    else:
        log.info("extension mode: external update (%s)", external_url)

    def build_chrome_args(use_load_ext: bool) -> list[str]:
        args = list(_CHROME_ARGS)
        if use_load_ext:
            args.extend(a.format(ext_dir=ext_dir) for a in _LOAD_EXT_ARGS)
        # Debug capture: CDP port on every launch (install + run) so both phases
        # are inspectable; harmless in the install phase.
        if debug:
            args.extend(debug.chrome_args())
        # Under Xvfb there is no window manager, so --start-maximized is a no-op
        # and Chromium opens the window at its default cascade offset (~130,60):
        # a grey strip of bare framebuffer shows on the top/left and the
        # bottom-right is cut off the RTMP capture. CDP setWindowBounds fixes the
        # size but a WM-less X server ignores its position, so pin the origin at
        # launch too. Window outer size == the framebuffer → fills the screen.
        args.append("--window-position=0,0")
        args.append(f"--window-size={VIEWPORT_WIDTH},{VIEWPORT_HEIGHT}")
        return args

    def launch(use_load_ext: bool) -> Any:
        return chromium.launch_persistent_context(
            profile_dir,
            headless=False,
            # Yandex flavor: drive the Yandex Browser binary instead of
            # Playwright's pinned Chromium. Same CDP surface (it is Chromium
            # underneath); None keeps the default for the chromium image.
            executable_path=_browser_binary(),
            args=build_chrome_args(use_load_ext),
            # pseudo-yandex: launch-level UA (navigator.userAgent + HTTP header).
            # Client Hints / userAgentData are layered on top per page via CDP
            # (see _apply_ua_override_single) — launch() cannot set those.
            user_agent=_PSEUDO_UA if _BROWSER_FLAVOR == "pseudo-yandex" else None,
            # Playwright injects a batch of --disable-* args by default, three of
            # which block the external-update policy install:
            #   --disable-extensions              — extensions off entirely
            #   --disable-background-networking   — kills the background update
            #                                      check that downloads the CRX
            #   --disable-default-apps            — skips the first-run pass that
            #                                      installs policy extensions
            # Drop all three so the external extension is installed and
            # auto-updated by Chrome itself.
            ignore_default_args=[
                "--disable-extensions",
                "--disable-background-networking",
                "--disable-default-apps",
            ],
            # viewport явно = FullHD, иначе Playwright без viewport пинет
            # страницу на 1280x720 (эмуляция device-metrics), хотя окно
            # --window-size=1920x1080: трансляция видит узкую колонку.
            # Согласованно с --window-size и window-position=0,0 окно и
            # страница совпадают, полный экран, dpr=1.
            viewport={"width": 1600, "height": 1080},  # idle-страница: 1600x1080
            ignore_https_errors=True,
        )

    # Derive the expected extension ID from the manifest public key up front:
    # deterministic, so we never depend on the (timing-flaky) service-worker
    # registration to learn the ID. The external CRX is signed with the same
    # key as the bundled manifest, so the ID matches either way.
    expected_id = _ext_id_from_manifest(ext_dir)
    log.info("two-launch: expected extension id from manifest key: %s", expected_id)

    # Two-launch: install the extension, grant incognito access post-install
    # (the preseeded Preferences are overwritten by Chromium on install), then
    # relaunch with incognito permission persisted in the profile.
    log.info("two-launch: install phase (load_ext=%s)", load_ext)
    # External mode: first install downloads the CRX from the update channel,
    # which on a cold start can take longer than the usual registration wait.
    c1 = launch(load_ext)
    ext_id = _discover_ext_id(
        c1,
        expected=expected_id,
        wait_s=90.0 if not load_ext else 45.0,
    )
    # CRITICAL: Fully kill the browser process so the second launch starts fresh.
    # c1.close() only closes the context; the browser process persists and
    # the second launch_persistent_context() reuses it, creating a second
    # window on the same X11 display. The first window (1280x800) then
    # obscures the full-HD window in x11grab capture.
    c1.close()
    # Give the browser process time to exit cleanly
    time.sleep(2)
    if not ext_id and not load_ext:
        # The external policy existed but the extension did not materialise
        # (offline channel, unreachable CRX). Fall back to the unpacked copy.
        log.warning(
            "external install not seen within 90s; falling back to unpacked --load-extension"
        )
        load_ext = True
        c1 = launch(load_ext)
        ext_id = _discover_ext_id(c1, expected=expected_id, wait_s=45.0)
        c1.close()
    if not ext_id:
        if expected_id:
            # The ID is deterministic from the manifest key, so a slow SW
            # registration on a cold first launch must not kill the whole
            # two-launch flow.  Proceed with the known ID; the run phase
            # re-confirms the extension is actually live.
            log.warning(
                "install phase: extension target not seen within 45s; "
                "proceeding with deterministic id %s (re-confirmed at run phase)",
                expected_id,
            )
            ext_id = expected_id
        else:
            shutil.rmtree(profile_dir, ignore_errors=True)
            raise ProviderError("Could not discover extension ID in install phase")

    try:
        prefs_path = default_dir / "Preferences"
        prefs = json.loads(prefs_path.read_text())
        settings = prefs.setdefault("extensions", {}).setdefault("settings", {})
        entry = settings.setdefault(ext_id, {})
        entry["incognito"] = True
        entry["state"] = 1
        prefs_path.write_text(json.dumps(prefs))
        log.info("two-launch: incognito granted for %s (post-install)", ext_id)
    except Exception as exc:
        log.warning("two-launch: Preferences edit failed: %s", exc)

    # Debug capture: open the CDP port only on the final (run) launch — the
    # install phase above already closed its Chromium, so the port never races.
    if debug:
        log.info("debug capture: chrome args extended with %s", debug.chrome_args())

    browser_context = launch(load_ext)

    # Handle JavaScript dialogs (alert/confirm/prompt) to prevent unhandled
    # ProtocolError in Playwright's internal Node.js driver (crashes browser context).
    browser_context.on("dialog", lambda dialog: dialog.dismiss())

    # pseudo-yandex: every page (now and in the future) gets the YaBrowser UA +
    # Client Hints. The CDP override set on a page survives navigations of that
    # page; pages opened later get patched on the "page" event.
    if _BROWSER_FLAVOR == "pseudo-yandex":
        browser_context.on("page", _apply_ua_override_to_page)
        _apply_ua_override(browser_context)

    discovered = _discover_ext_id(browser_context, expected=expected_id, wait_s=60.0)
    if discovered:
        ext_id = discovered
    if discovered != expected_id:
        log.warning("extension id %s differs from expected %s", discovered, expected_id)

    # Headless hosts have no UI toggle for incognito access — force it on.
    if browser_context.service_workers:
        try:
            sw = browser_context.service_workers[0]
            sw.evaluate(_PATCH_INCOGNITO_JS)
            sw.evaluate(_PATCH_INCOGNITO_RECHECK_JS)
        except Exception as exc:
            log.warning("incognito patch failed: %s", exc)
        time.sleep(1)

    popup = _open_panel(browser_context, ext_id)
    if popup is None:
        browser_context.close()
        shutil.rmtree(profile_dir, ignore_errors=True)
        raise ProviderError("Could not open the extension panel/popup page")

    handshake = popup.evaluate(_HANDSHAKE_JS, {
        "token": token,
        "scheduleId": schedule_id,
        "instanceId": str(uuid.uuid4()),
        "apiBase": api_base,
    })
    log.info("handshake: %s", json.dumps(handshake))
    if not handshake or not handshake.get("ok"):
        browser_context.close()
        shutil.rmtree(profile_dir, ignore_errors=True)
        raise ProviderError(f"Token handshake failed: {handshake}")

    stored = popup.evaluate(_READ_STORED_JS)
    if not stored:
        browser_context.close()
        shutil.rmtree(profile_dir, ignore_errors=True)
        raise ProviderError("No token found in extension storage after handshake")
    token = stored.get("token") or token
    schedule_id = stored.get("schedule_id") or schedule_id
    log.info(
        "post-handshake: schedule_id=%s token=%s...",
        schedule_id,
        str(token)[:12],
    )

    log.info("waiting for relay connection...")
    time.sleep(5)
    try:
        popup.evaluate(_CLEAR_HEARTBEAT_JS)
    except Exception:
        pass

    status = _poll_online(api_base, token, attempts=8, interval=5)
    log.info("provider status: %s", status)
    if status != "online":
        browser_context.close()
        shutil.rmtree(profile_dir, ignore_errors=True)
        raise ProviderError(f"Extension did not come online: {status}")

    dbg_mgr = None
    dbg_stop = None
    if debug is not None:
        dbg_stop = threading.Event()
        dbg_mgr = provider_debug.start_capture(debug, ext_id, dbg_stop)
        log.info("debug capture manager started" if dbg_mgr else "debug capture manager failed to start")

    # Растягиваем окно на весь экран (Xvfb без WM — флаги не работают, только CDP).
    _maximize_window(popup)

    # --- Настройки плагина (seed в chrome.storage.local) ---
    # Панель: тумблер "Открывать развёрнутым" (open_window_normal) и
    # "Открывать в фокусе" (open_window_focused). ON = true в storage.
    #
    # IMPORTANT: env flags (CEKI_PROVIDER_OPEN_*) only define the INITIAL
    # state. After first seed chrome.storage.local belongs to the user —
    # the plugin panel is the only writer. Writing these unconditionally on
    # every provider start silently reverts the user's toggles (rental
    # windows pop to normal+focused even when the user chose minimized).
    # So seed them only while they are still undefined.
    def _flag(name: str, default: bool) -> bool:
        v = os.environ.get(name)
        return default if v is None else v.lower() in ("1", "true", "yes", "on")

    settings = {
        "open_window_normal": _flag("CEKI_PROVIDER_OPEN_NORMAL", True),
        "open_window_focused": _flag("CEKI_PROVIDER_OPEN_FOCUSED", True),
        "restore_focus_on_rental": _flag("CEKI_PROVIDER_RESTORE_FOCUS_ON_RENTAL", False),
    }
    try:
        res = popup.evaluate(
            "(async (s) => { "
            "const cur = await chrome.storage.local.get(Object.keys(s)); "
            "const seed = {}; "
            "for (const k of Object.keys(s)) { if (cur[k] === undefined) seed[k] = s[k]; } "
            "const seeded = Object.keys(seed); "
            "if (seeded.length) await chrome.storage.local.set(seed); "
            "return JSON.stringify({ seeded }); "
            "})",
            settings,
        )
        log.info("plugin settings seeded: %s", res)
    except Exception as exc:
        log.warning("plugin settings seed failed: %s", exc)

    # Открываем сайдбар плагина (боковую панель) в окне провайдера. Делаем это
    # пока вкладка ещё extension-страница: из service worker sidePanel.open()
    # требует user gesture и будет отклонён.
    try:
        res = popup.evaluate(
            "(async () => { const w = await chrome.windows.getCurrent(); "
            "if (!w || w.id === undefined) return 'ERR no-window'; "
            "await chrome.sidePanel.open({ windowId: w.id }); return 'ok window=' + w.id; })()"
        )
        log.info("sidepanel open attempt: %s", res)
    except Exception as exc:
        log.warning("sidepanel open failed: %s", exc)

    # Стартовая страница вместо UI расширения (idle-вид стрима).
    idle_url = os.environ.get("CEKI_PROVIDER_IDLE_URL", "https://ceki.me")
    try:
        popup.goto(idle_url, wait_until="domcontentloaded", timeout=25_000)
        log.info("idle page shown: %s", idle_url)
    except Exception as exc:
        log.warning("idle page nav failed: %s", exc)

    return ProviderContext(
        browser_context=browser_context,
        profile_dir=profile_dir,
        extension_id=ext_id,
        token=token,
        schedule_id=schedule_id,
        api_base=api_base,
        debug_capture=dbg_mgr,
        debug_stop=dbg_stop,
    )


def _keep_alive(ctx: ProviderContext, stop: threading.Event, timeout: int | None) -> int:
    log.info(
        "provider READY: extension=%s schedule_id=%s — staying alive, auto-accept enabled",
        ctx.extension_id,
        ctx.schedule_id,
    )
    started = time.time()
    last_status = 0.0
    while not stop.is_set():
        elapsed = time.time() - started
        if timeout and elapsed >= timeout:
            log.info("timeout reached (%ss), shutting down", timeout)
            break
        if elapsed - last_status >= 30:
            last_status = elapsed
            status = _browser_status(ctx.api_base, ctx.token)
            log.info("[heartbeat] online=%s elapsed=%ds", status, int(elapsed))
        time.sleep(1)
    return 0


def run_provider(
    *,
    token: str | None = None,
    ext_dir: str | None = None,
    api_base: str | None = None,
    schedule_id: int | None = None,
    timeout: int | None = None,
    verbose: bool = False,
) -> int:
    """Deploy a browser provider and keep it online until stopped.

    Returns a process exit code (0 on clean shutdown).
    """
    _setup_logging(verbose)
    _ensure_display()
    token_value = resolve_token(token)
    ext_dir_value = resolve_ext_dir(ext_dir)
    api_base_value = resolve_api_base(api_base)
    schedule_id_value = schedule_id or _env_int(
        "CEKI_PROVIDER_SCHEDULE_ID", "PROVIDER_SCHEDULE_ID"
    )

    log.info(
        "provider: api_base=%s ext_dir=%s schedule_id=%s",
        api_base_value,
        ext_dir_value,
        schedule_id_value,
    )

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - env-dependent
        raise ProviderError(
            "Playwright is required for provider mode. "
            "Install it with: pip install 'ceki-browser-provider'"
        ) from exc

    stop: threading.Event = threading.Event()
    _install_signal_handlers(stop)

    with sync_playwright() as playwright:
        ctx = _launch_provider(
            playwright,
            token=token_value,
            ext_dir=ext_dir_value,
            api_base=api_base_value,
            schedule_id=schedule_id_value,
        )
        try:
            return _keep_alive(ctx, stop=stop, timeout=timeout)
        finally:
            try:
                ctx.browser_context.close()
            except Exception:
                pass
            if ctx.debug_capture is not None:
                provider_debug.stop_capture(ctx.debug_capture)
            shutil.rmtree(ctx.profile_dir, ignore_errors=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry: ``ceki-provider [--token ...]`` (the Docker CMD target)."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="ceki-provider",
        description=(
            "Deploy a browser provider (Chromium + Ceki extension + token) "
            "and keep it online, auto-accepting rentals"
        ),
    )
    parser.add_argument(
        "--token",
        help="Provider extension token (default: $CEKI_PROVIDER_TOKEN)",
    )
    parser.add_argument(
        "--ext-dir",
        help="Path to the unpacked Ceki extension dist (default: $CEKI_PROVIDER_EXT_DIR)",
    )
    parser.add_argument(
        "--api-url",
        help="Backend API base URL (default: $CEKI_API_URL or https://api.ceki.me)",
    )
    parser.add_argument(
        "--schedule-id",
        type=int,
        help="Browser/schedule ID (default: derived from /api/browser/me)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        help="Run for N seconds then exit (default: run until stopped)",
    )
    parser.add_argument("--verbose", action="store_true", help="Verbose logging")
    args = parser.parse_args(argv)

    try:
        return run_provider(
            token=args.token,
            ext_dir=args.ext_dir,
            api_base=args.api_url,
            schedule_id=args.schedule_id,
            timeout=args.timeout,
            verbose=args.verbose,
        )
    except ProviderError as exc:
        print(f"[ceki-provider] error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
