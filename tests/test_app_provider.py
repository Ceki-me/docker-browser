"""Unit tests for the APP-mode launcher (ceki_browser_provider.app).

Covers the pure, testable parts of the app-mode provider: online-status
polling (`_browser_status` / `_poll_online`), env/arg resolution
(`resolve_*`), and the keep-alive heartbeat loop (`_keep_alive`).

The app-mode provider WS is held by the JS extension (which has its own
heartbeat), so none of this covers relay connectivity — that's the daemon's
job. These tests lock in the online-status logic that app mode DOES own.
"""
import json
import os
import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ceki_browser_provider.app import (
    ProviderError,
    _apply_ua_override,
    _browser_binary,
    _browser_status,
    _env_int,
    _ext_id_from_manifest,
    _keep_alive,
    _parse_viewport,
    _poll_online,
    resolve_api_base,
    resolve_ext_dir,
    resolve_token,
)


# ── _browser_status ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "payload,expected",
    [
        ({"online": True}, "online"),
        ({"online": True, "status": "online"}, "online"),
        ({"online": False, "status": "offline"}, "offline"),
        ({"online": False, "status": "starting"}, "starting"),
        ({"status": "online"}, "online"),
        ({"status": "sleeping"}, "sleeping"),
        ({}, "offline"),
    ],
)
def test_browser_status_parses_payloads(payload, expected):
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = payload
    with patch("ceki_browser_provider.app.httpx.get", return_value=resp) as m:
        assert _browser_status("https://api", "tok") == expected
    m.assert_called_once()
    _, kwargs = m.call_args
    assert kwargs["headers"]["Authorization"] == "Bearer tok"
    assert kwargs["timeout"] == 10


def test_browser_status_non_200_returns_offline():
    resp = MagicMock()
    resp.status_code = 500
    with patch("ceki_browser_provider.app.httpx.get", return_value=resp):
        assert _browser_status("https://api", "tok") == "offline"


def test_browser_status_http_error_returns_offline():
    with patch(
        "ceki_browser_provider.app.httpx.get",
        side_effect=httpx.ConnectError("boom"),
    ):
        assert _browser_status("https://api", "tok") == "offline"


# ── _poll_online ─────────────────────────────────────────────────────────────


def test_poll_online_returns_immediately_on_online():
    with patch("ceki_browser_provider.app._browser_status", return_value="online") as bs, \
         patch("ceki_browser_provider.app.time.sleep") as sleep:
        assert _poll_online("https://api", "tok", attempts=8, interval=5) == "online"
    assert bs.call_count == 1
    sleep.assert_not_called()


def test_poll_online_retries_then_offline():
    with patch("ceki_browser_provider.app._browser_status", return_value="offline") as bs, \
         patch("ceki_browser_provider.app.time.sleep") as sleep:
        assert _poll_online("https://api", "tok", attempts=3, interval=0.1) == "offline"
    assert bs.call_count == 3
    assert sleep.call_count == 2  # no sleep after the last attempt


def test_poll_online_succeeds_on_later_attempt():
    statuses = iter(["offline", "offline", "online"])
    with patch("ceki_browser_provider.app._browser_status", side_effect=lambda *a, **k: next(statuses)) as bs, \
         patch("ceki_browser_provider.app.time.sleep"):
        assert _poll_online("https://api", "tok", attempts=5, interval=0.1) == "online"
    assert bs.call_count == 3


# ── resolve_token ────────────────────────────────────────────────────────────


def test_resolve_token_from_arg():
    assert resolve_token("arg-token") == "arg-token"


def test_resolve_token_from_env():
    with patch.dict(os.environ, {"CEKI_PROVIDER_TOKEN": "env-token"}, clear=True):
        assert resolve_token() == "env-token"


def test_resolve_token_prefers_arg_over_env():
    with patch.dict(os.environ, {"CEKI_PROVIDER_TOKEN": "env-token"}, clear=True):
        assert resolve_token("arg-token") == "arg-token"


def test_resolve_token_missing_raises():
    with patch.dict(os.environ, {}, clear=True):
        with pytest.raises(ProviderError):
            resolve_token()


# ── resolve_ext_dir ──────────────────────────────────────────────────────────


def test_resolve_ext_dir_from_explicit(tmp_path):
    ext = tmp_path / "ext"
    ext.mkdir()
    (ext / "manifest.json").write_text("{}")
    assert resolve_ext_dir(str(ext)) == os.path.abspath(str(ext))


def test_resolve_ext_dir_from_env(tmp_path):
    ext = tmp_path / "ext"
    ext.mkdir()
    (ext / "manifest.json").write_text("{}")
    with patch.dict(os.environ, {"CEKI_PROVIDER_EXT_DIR": str(ext)}, clear=True):
        assert resolve_ext_dir() == os.path.abspath(str(ext))


def test_resolve_ext_dir_missing_raises(tmp_path):
    bad = tmp_path / "no-manifest"
    bad.mkdir()
    with patch.dict(os.environ, {}, clear=True):
        with pytest.raises(ProviderError):
            resolve_ext_dir(str(bad))


# ── resolve_api_base ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "explicit,expected",
    [
        ("https://api.ceki.me", "https://api.ceki.me"),
        ("https://api.ceki.me/", "https://api.ceki.me"),
        ("https://api.ceki.me/api", "https://api.ceki.me"),
        ("https://host/api/", "https://host"),
        ("http://localhost:8080/api", "http://localhost:8080"),
    ],
)
def test_resolve_api_base(explicit, expected):
    with patch.dict(os.environ, {}, clear=True):
        assert resolve_api_base(explicit) == expected


def test_resolve_api_base_from_env():
    with patch.dict(os.environ, {"CEKI_API_URL": "https://env.api/api"}, clear=True):
        assert resolve_api_base() == "https://env.api"


# ── _keep_alive ──────────────────────────────────────────────────────────────


def test_keep_alive_heartbeats_every_30s_then_stops():
    """_keep_alive must poll _browser_status every 30s and exit on stop/timeout."""
    ctx = MagicMock()
    ctx.api_base = "https://api"
    ctx.token = "tok"
    ctx.extension_id = "ext"
    ctx.schedule_id = 42
    ctx.profile_dir = "/tmp/nonexistent"

    stop = threading.Event()
    # Virtual clock: first call (started) = 0.0, then 1s per call.
    clock = {"now": -1.0}

    def fake_time():
        clock["now"] += 1.0
        return clock["now"]

    with patch("ceki_browser_provider.app._browser_status", return_value="online") as bs, \
         patch("ceki_browser_provider.app.time.sleep") as sleep, \
         patch("ceki_browser_provider.app.time.time", side_effect=fake_time):
        rc = _keep_alive(ctx, stop=stop, timeout=70)
        assert rc == 0

    # Heartbeats at elapsed 30, 60 → 2 calls, then timeout at 70.
    assert bs.call_count == 2
    sleep.assert_called()


def test_keep_alive_timeout_shuts_down():
    """timeout parameter must terminate the loop."""
    ctx = MagicMock()
    ctx.api_base = "https://api"
    ctx.token = "tok"
    ctx.extension_id = "ext"
    ctx.schedule_id = 1
    ctx.profile_dir = "/tmp/nonexistent"

    stop = threading.Event()
    clock = {"now": -1.0}

    def fake_time():
        clock["now"] += 1.0
        return clock["now"]

    with patch("ceki_browser_provider.app._browser_status", return_value="online") as bs, \
         patch("ceki_browser_provider.app.time.sleep") as sleep, \
         patch("ceki_browser_provider.app.time.time", side_effect=fake_time):
        rc = _keep_alive(ctx, stop=stop, timeout=4)
    assert rc == 0
    # Timeout at 4s is before the first heartbeat (30s) → no status calls.
    assert bs.call_count == 0

# ── _parse_viewport ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("1280x720", (1280, 720)),
        ("1920x1080", (1920, 1080)),
        ("800x600", (800, 600)),
        (None, (1920, 1080)),           # default
        ("", (1920, 1080)),             # empty → default
        ("abc", (1920, 1080)),          # unparseable → default
        ("1280", (1920, 1080)),         # missing height → default
        ("0x720", (1920, 1080)),        # zero width → default
        ("-1x1080", (1920, 1080)),      # negative → default
        ("1280x0", (1920, 1080)),       # zero height → default
        (" 1920 x 1080 ", (1920, 1080)),  # whitespace-tolerant
    ],
)
def test_parse_viewport(raw, expected):
    with patch.dict(os.environ, {}, clear=True):
        assert _parse_viewport(raw) == expected


def test_parse_viewport_lowercases():
    """'X' separator after .lower() — '1920X1080' must parse."""
    assert _parse_viewport("1920X1080") == (1920, 1080)


# ── _env_int ────────────────────────────────────────────────────────────────


def test_env_int_reads_first_set():
    with patch.dict(os.environ, {"A": "5", "B": "7"}, clear=True):
        assert _env_int("A", "B") == 5


def test_env_int_skips_empty_and_missing():
    with patch.dict(os.environ, {"A": "", "B": "7"}, clear=True):
        assert _env_int("A", "B") == 7
    with patch.dict(os.environ, {}, clear=True):
        assert _env_int("A", "B") is None


def test_env_int_ignores_non_integer():
    with patch.dict(os.environ, {"A": "abc", "B": "9"}, clear=True):
        assert _env_int("A", "B") == 9
    with patch.dict(os.environ, {"A": "abc"}, clear=True):
        assert _env_int("A") is None


# ── _ext_id_from_manifest ────────────────────────────────────────────────────


def test_ext_id_from_manifest_derives_predictable_id(tmp_path):
    """Chrome extension ID derivation: sha256(key)[:16] nibbles → a..p."""
    import base64
    import hashlib

    # A known DER public key. ID must equal the standard algorithm.
    key_b64 = "AQIDBAUGBwgJCgsMDQ4PEBESExQVFhcYGRobHB0eHyABAgMEBQYHCAkKCwwNDg8QERITFBUWFxgZGhscHR4fIAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8gAQIDBAUGBwgJCgsMDQ4PEBESExQVFhcYGRobHB0eHyA="
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"key": key_b64}))
    got = _ext_id_from_manifest(str(tmp_path))
    # Recompute expected the same way the code does.
    digest = hashlib.sha256(base64.b64decode(key_b64)).hexdigest()[:32]
    expected = "".join(chr(ord("a") + int(c, 16)) for c in digest)
    assert got == expected
    assert got is not None
    assert len(got) == 32
    assert set(got) <= set("abcdefghijklmnop")


def test_ext_id_from_manifest_no_key_returns_none(tmp_path):
    (tmp_path / "manifest.json").write_text(json.dumps({"name": "x"}))
    assert _ext_id_from_manifest(str(tmp_path)) is None


def test_ext_id_from_manifest_missing_file_returns_none(tmp_path):
    assert _ext_id_from_manifest(str(tmp_path / "nope")) is None


def test_ext_id_from_manifest_bad_key_returns_none(tmp_path):
    (tmp_path / "manifest.json").write_text(json.dumps({"key": "%%%not-base64%%%"}))
    assert _ext_id_from_manifest(str(tmp_path)) is None


# ── _browser_binary ──────────────────────────────────────────────────────────


def test_browser_binary_known_flavor(tmp_path):
    # chromium maps to /usr/bin/chromium (exists on the host) → returned.
    with patch("ceki_browser_provider.app._BROWSER_FLAVOR", "chromium"):
        bin_ = _browser_binary()
        assert bin_ is None or bin_ == "/usr/bin/chromium"


def test_browser_binary_unknown_flavor_returns_none(tmp_path):
    with patch("ceki_browser_provider.app._BROWSER_FLAVOR", "netscape"):
        assert _browser_binary() is None


# ── _apply_ua_override ───────────────────────────────────────────────────────


def test_apply_ua_override_skips_non_pseudo_yandex():
    """Non pseudo-yandex flavor must be a no-op (no page patches)."""
    page = MagicMock()
    context = MagicMock()
    context.pages = [page]
    with patch("ceki_browser_provider.app._BROWSER_FLAVOR", "chromium"):
        _apply_ua_override(context)
    page.context.new_cdp_session.assert_not_called()


def test_apply_ua_override_patches_pages():
    """pseudo-yandex flavor must patch each page via CDP Network domain."""
    page = MagicMock()
    session = MagicMock()
    page.context.new_cdp_session.return_value = session
    context = MagicMock()
    context.pages = [page]
    with patch("ceki_browser_provider.app._BROWSER_FLAVOR", "pseudo-yandex"):
        _apply_ua_override(context)
    session.send.assert_called_once()
    call = session.send.call_args
    assert call[0][0] == "Network.setUserAgentOverride"
    ua_meta = call[0][1]["userAgentMetadata"]
    assert ua_meta["brands"][1]["brand"] == "Yandex"
