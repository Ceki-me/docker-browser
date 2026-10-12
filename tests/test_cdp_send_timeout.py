"""Regression: _cdp_send must not hang when the CDP peer never responds.

Live daemon-chromium (42306) at 3-way parallel load: a spawned Chrome's
extension SW occasionally accepts the CDP WS-upgrade but then never answers
Runtime.evaluate.  The bare ``websockets.connect()`` + ``sock.send()`` had NO
deadline, so ``_handshake`` stalled 30s+, the watchdog reaped the rent, and
the renter got ``CDP Page.navigate timed out``.

Fix: wrap the whole round-trip in ``asyncio.wait_for(..., timeout+2)`` so a
silent peer fails fast and the retry/relaunch path fires on schedule.
"""
import asyncio
import os
import sys
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ceki_browser_provider.daemon import SpawnManager


@pytest.fixture
def spawner():
    return object.__new__(SpawnManager)  # no __init__ needed — we test _cdp_send only


async def _forever():
    while True:
        await asyncio.sleep(3600)


def test_cdp_send_fails_fast_when_peer_never_responds(spawner):
    """A silent CDP peer must not make _cdp_send hang forever."""
    sock = AsyncMock()
    sock.send = AsyncMock()
    # recv never resolves → only the outer wait_for deadline can free us.
    sock.recv.side_effect = lambda: _forever()

    cm = AsyncMock()  # async context manager returned by websockets.connect
    cm.__aenter__ = AsyncMock(return_value=sock)
    cm.__aexit__ = AsyncMock(return_value=False)
    fake_connect = MagicMock(return_value=cm)

    with patch("ceki_browser_provider.daemon.websockets.connect", fake_connect):
        t0 = time.monotonic()
        with pytest.raises(Exception):
            spawner._cdp_send("ws://127.0.0.1:1/devtools/page/x", "Runtime.evaluate", {}, timeout=5)
        elapsed = time.monotonic() - t0
    assert elapsed < 15, f"_cdp_send hung {elapsed:.1f}s (should fail fast ~7s)"


def test_cdp_send_returns_result_when_peer_replies(spawner):
    """Normal path unchanged: a replying CDP target returns its result."""
    sock = AsyncMock()
    sock.send = AsyncMock()
    sock.recv.side_effect = [
        '{"id": 1, "result": {"result": {"value": "ok"}}}',
    ]

    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=sock)
    cm.__aexit__ = AsyncMock(return_value=False)
    fake_connect = MagicMock(return_value=cm)

    with patch("ceki_browser_provider.daemon.websockets.connect", fake_connect):
        res = spawner._cdp_send("ws://127.0.0.1:1/devtools/page/x", "Runtime.evaluate", {}, timeout=5)
    assert res == {"result": {"value": "ok"}}