"""Regression tests for the provider-WS silent-socket watchdog.

When the relay link half-opens (network blip / relay restart / lost FIN), the
client socket stays ESTAB but no frame ever arrives and no exception fires:
``_read_loop`` sits in ``async for`` forever, so the provider never reconnects
and the backend sees it offline for hours (prod 43664/43665/43666/6341:
silent at 12:53, relay pong-timeout at 12:55, still dead 5h later).

The fix: ``ProviderWsClient`` tracks ``_last_rx`` (monotonic ts of the last
relay frame) and runs a ``_idle_watchdog`` task that force-closes the socket
after ``CEKI_DAEMON_RELAY_IDLE_TIMEOUT`` (default 90s = 3× relay ping) of
silence, which makes ``_read_loop`` raise ``ConnectionClosed`` and the normal
reconnect path in ``run()`` take over.
"""
import asyncio
import os
import sys
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ceki_browser_provider.daemon import DaemonConfig, ProviderWsClient, SpawnManager


def make_client(max_sessions=1, idle_timeout="0.05"):
    cfg = DaemonConfig(
        token="test-token",
        schedule_id=40870,
        api_base="https://test.api",
        ext_dir="/tmp/test-ext",
        daemon_port=17894,
        width=1920,
        height=1080,
        storage_key="test",
        persist=False,
        max_sessions=max_sessions,
        cdps=[9200],
        displays=[101],
    )
    with patch.dict(
        os.environ,
        {"CEKI_DAEMON_RELAY_IDLE_TIMEOUT": idle_timeout},
        clear=False,
    ):
        cfg.relay_idle_timeout = float(idle_timeout)
        spawner = SpawnManager(cfg)
        client = ProviderWsClient(cfg, spawner, MagicMock())
        return client


class FakeWS:
    """Minimal async websocket for the watchdog tests."""

    def __init__(self):
        self.state = 1  # OPEN
        self._closed = asyncio.Event()
        self.close_called = False

    async def close(self):
        self.close_called = True
        self.state = 3  # CLOSED
        self._closed.set()

    async def send(self, data):
        pass


@pytest.mark.asyncio
async def test_watchdog_closes_silent_socket():
    """A socket that receives nothing must be force-closed by the watchdog."""
    client = make_client(idle_timeout="0.05")
    ws = FakeWS()

    # Simulate a stale socket: _last_rx is old, no frames will arrive.
    client._last_rx = time.monotonic() - 1.0
    watchdog = asyncio.create_task(client._idle_watchdog(ws))

    # Give the watchdog enough time to notice the silence and close.
    await asyncio.sleep(0.2)
    assert ws.close_called, "watchdog must close a silent socket"
    watchdog.cancel()


@pytest.mark.asyncio
async def test_watchdog_does_not_close_live_socket():
    """A socket that keeps receiving frames must never be closed."""
    client = make_client(idle_timeout="0.05")

    class LiveWS(FakeWS):
        def __init__(self):
            super().__init__()
            self._n = 0
            self._feed = asyncio.Event()

        async def _agen(self):
            while True:
                await self._feed.wait()
                self._feed.clear()
                yield b'{"type":"ping"}'

        def poke(self):
            self._feed.set()

    ws = LiveWS()
    # Fresh socket: _last_rx is now.
    watchdog = asyncio.create_task(client._idle_watchdog(ws))

    # Keep refreshing _last_rx faster than the idle timeout for several windows.
    for _ in range(6):
        client._last_rx = time.monotonic()
        await asyncio.sleep(0.02)  # < idle_timeout (0.05)

    assert not ws.close_called, "live socket must stay open"
    watchdog.cancel()


@pytest.mark.asyncio
async def test_watchdog_honors_env_timeout():
    """The watchdog must read CEKI_DAEMON_RELAY_IDLE_TIMEOUT from env."""
    client = make_client(idle_timeout="0.03")
    ws = FakeWS()
    client._last_rx = time.monotonic() - 1.0
    watchdog = asyncio.create_task(client._idle_watchdog(ws))
    await asyncio.sleep(0.15)
    assert ws.close_called, "watchdog must fire with env timeout"
    watchdog.cancel()


@pytest.mark.asyncio
async def test_read_loop_updates_last_rx():
    """_read_loop must refresh _last_rx on every frame (watchdog relies on it)."""
    client = make_client(idle_timeout="0.05")

    class FramedWS(FakeWS):
        def __init__(self):
            super().__init__()
            self._it = iter([b'{"type":"ping"}', b'{"type":"provider.alive_probe"}'])

        def __aiter__(self):
            return self._agen()

        async def _agen(self):
            for f in self._it:
                yield f
            await asyncio.Event().wait()  # block forever after frames

    ws = FramedWS()
    inbox = asyncio.Queue()
    reader = asyncio.create_task(client._read_loop(ws, inbox))
    await asyncio.sleep(0.05)
    # Both frames consumed → _last_rx advanced.
    assert client._last_rx >= time.monotonic() - 0.1
    # And the queue holds them.
    assert (await asyncio.wait_for(inbox.get(), timeout=0.5))["type"] == "ping"
    assert (await asyncio.wait_for(inbox.get(), timeout=0.5))["type"] == "provider.alive_probe"
    reader.cancel()