"""End-to-end test: provider-WS silent-socket watchdog → reconnect.

This exercises the FULL `ProviderWsClient.run()` loop against a real (local)
websocket relay server:

  1. client connects, sends welcome, receives relay pings, answers pong
  2. relay goes silent (TCP stays open, no frames) — the half-open socket case
  3. the idle watchdog force-closes the socket (transport.abort())
  4. run() reconnects to the relay
  5. pings flow again → provider is online again

Reproduces the prod outage (43664/43665/43666/6341): half-open WS after a
network blip, provider never reconnects, offline for hours. The watchdog
fixes it by reconnecting within CEKI_DAEMON_RELAY_IDLE_TIMEOUT.
"""
import asyncio
import json
import os
import sys
from contextlib import asynccontextmanager
from unittest.mock import MagicMock, patch

import pytest
import websockets

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ceki_browser_provider.daemon import DaemonConfig, ProviderWsClient, SpawnManager

RELAY_TOKEN = "test-token"


class FakeRelay:
    """Minimal relay: accepts a provider, pings it, can go silent."""

    def __init__(self, ping_interval: float = 0.05):
        self.ping_interval = ping_interval
        self.silent = False
        self.connections = 0
        self.welcomes = 0
        self.pongs = 0
        self._server = None
        self.port = None

    async def _periodic_ping(self, ws) -> None:
        try:
            while True:
                if not self.silent:
                    await ws.send(json.dumps({"type": "ping", "ts": asyncio.get_event_loop().time()}))
                await asyncio.sleep(self.ping_interval)
        except websockets.exceptions.ConnectionClosed:
            pass
        except asyncio.CancelledError:
            pass

    async def handler(self, ws) -> None:
        self.connections += 1
        ping_task = asyncio.create_task(self._periodic_ping(ws))
        try:
            async for raw in ws:
                data = json.loads(raw)
                mtype = data.get("type")
                if mtype == "welcome":
                    self.welcomes += 1
                elif mtype == "pong":
                    self.pongs += 1
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            ping_task.cancel()

    async def start(self) -> None:
        self._server = await websockets.serve(
            self.handler,
            "127.0.0.1",
            0,
            subprotocols=[f"bearer.{RELAY_TOKEN}"],
        )
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()


async def _wait_until(cond, timeout: float = 4.0, desc: str = "condition") -> None:
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while not cond():
        if loop.time() >= deadline:
            raise AssertionError(f"timeout waiting for: {desc}")
        await asyncio.sleep(0.02)


@asynccontextmanager
async def _run_client(relay_url: str, idle_timeout: str = "0.2", reconnect_delay: float = 0.05):
    """Create client + start run() within the env patch so relay_url resolves."""
    with patch.dict(
        os.environ,
        {"CEKI_WS_URL": relay_url, "CEKI_DAEMON_RELAY_IDLE_TIMEOUT": idle_timeout},
        clear=False,
    ):
        cfg = DaemonConfig(
            token=RELAY_TOKEN,
            schedule_id=40870,
            api_base="https://test.api",
            ext_dir="/tmp/test-ext",
            daemon_port=17894,
            width=1920,
            height=1080,
            storage_key="test",
            persist=False,
            max_sessions=1,
            cdps=[9200],
            displays=[101],
        )
        cfg.relay_idle_timeout = float(idle_timeout)
        spawner = SpawnManager(cfg)
        router = MagicMock()
        router.active_session_id.return_value = None
        client = ProviderWsClient(cfg, spawner, router)
        # Speed up reconnect backoff so the test doesn't wait 1s+ per attempt.
        client._reconnect_delay = reconnect_delay
        task = asyncio.create_task(client.run())
        try:
            yield client
        finally:
            client.stop()
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


@pytest.mark.asyncio
async def test_silent_socket_reconnect_e2e():
    relay = FakeRelay(ping_interval=0.05)
    await relay.start()
    relay_url = f"ws://127.0.0.1:{relay.port}/ws/provider"

    async with _run_client(relay_url, idle_timeout="0.2") as _client:
        # 1) Initial connect + welcome + at least one pong.
        await _wait_until(
            lambda: relay.connections >= 1 and relay.welcomes >= 1,
            desc="initial connect+welcome",
        )
        await _wait_until(lambda: relay.pongs >= 1, desc="first pong")

        # 2) Relay goes silent — TCP stays open, no frames (half-open socket).
        relay.silent = True
        pongs_at_silence = relay.pongs

        # 3) Watchdog must force-close and the client must reconnect.
        #    (Abort → server sees the connection die → client re-dials →
        #    connections grows.)
        await _wait_until(
            lambda: relay.connections >= 2,
            desc="watchdog abort + reconnect",
        )

        # 4) Relay speaks again — client on the NEW connection must pong.
        relay.silent = False
        await _wait_until(
            lambda: relay.pongs >= pongs_at_silence + 1,
            desc="pong after reconnect",
        )

        # 5) Stable: with pings flowing, the watchdog must NOT flap the link.
        stable_connections = relay.connections
        await asyncio.sleep(0.3)  # > idle_timeout (0.2), but pings keep arriving
        assert relay.connections == stable_connections, (
            "live connection must not be closed by the watchdog"
        )

    await relay.stop()


@pytest.mark.asyncio
async def test_no_reconnect_while_relay_alive():
    """Watchdog must never close a healthy link (regression guard)."""
    relay = FakeRelay(ping_interval=0.02)
    await relay.start()
    relay_url = f"ws://127.0.0.1:{relay.port}/ws/provider"

    async with _run_client(relay_url, idle_timeout="0.15") as _client:
        await _wait_until(
            lambda: relay.connections >= 1 and relay.welcomes >= 1,
            desc="initial connect",
        )
        # Let it run several watchdog windows with pings flowing.
        await asyncio.sleep(0.5)
        assert relay.connections == 1, "healthy link must stay on one connection"
        assert relay.pongs > 0, "client must be answering pings"

    await relay.stop()