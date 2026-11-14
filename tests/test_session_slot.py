"""Multi-rent slot tests (ev 14954).

Slot semantics live at the SDK + relay boundary (rentSchema slot 1..64, dedup by
(browser, slot)). On the provider side a slot is just a distinct session: the
daemon spawns a separate Chrome instance per session_id, so slot 1 and slot 2
are two instances on the same provider and a re-rent of the same slot targets
the same live session (the relay resumes it, the daemon reuses the instance).

These tests exercise the provider-side invariants without real Chrome:

- slot=1 and slot=2 → two distinct instances/sessions on one provider;
- re-rent of the same slot (same session_id) → same instance (no re-spawn);
- slot out of range is rejected by the SDK/relay schema.
"""
import asyncio
import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ceki_browser_provider.daemon import DaemonConfig, SpawnManager

# SDK slot bounds — the relay schema enforces the same 1..64 range.
SLOT_MIN = 1
SLOT_MAX = 64


def make_spawner(max_sessions=8):
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
        cdps=list(range(9200, 9200 + max_sessions)),
        displays=list(range(101, 101 + max_sessions)),
    )
    with patch.dict(
        os.environ,
        {"CEKI_DAEMON_CONNECT_TIMEOUT": "90", "CEKI_DAEMON_BOOT_STAGGER_S": "0"},
        clear=False,
    ):
        return SpawnManager(cfg)


class TestSlotToSession:
    def test_slot_1_and_slot_2_are_distinct_sessions(self):
        """Two rents of the same browser with different slots → distinct instances."""
        spawner = make_spawner()
        with patch.object(spawner, "_spawn_and_handshake"):
            s1 = spawner.ensure("sess-1", {})   # slot 1's session
            s2 = spawner.ensure("sess-2", {})   # slot 2's session
        assert s1 is not None and s2 is not None
        assert s1.session_id != s2.session_id
        assert s1.cdp_port != s2.cdp_port, "different slots must get different Chrome instances"
        assert len(spawner.active()) == 2

    def test_repeat_same_slot_reuses_same_session(self):
        """Re-renting the same slot (same session_id) finds the live instance — no re-spawn."""
        spawner = make_spawner()
        with patch.object(spawner, "_spawn_and_handshake") as spawn_mock:
            first = spawner.ensure("sess-2", {})
            assert spawn_mock.call_count == 1
            # The relay resumes the SAME session_id for the same (browser, slot)
            # → ensure() finds the live instance and reuses it (no new spawn).
            again = spawner.ensure("sess-2", {})
            assert again is first
            assert spawn_mock.call_count == 1, "same slot must not re-spawn a Chrome"
            assert len(spawner.active()) == 1

    def test_slot_bounds_rejected(self):
        """The SDK rejects slot outside 1..64 before it reaches the provider."""
        import ceki_sdk  # noqa: F401  (probe availability)

        from ceki_sdk._client import Client

        # Client.rent validates slot range before building the WS message.
        client = Client(
            api_key="k",
            relay_url="wss://relay.ceki.me/ws/agent",
            api_url="https://api.ceki.me",
            chat_url="https://chat.ceki.me/api/chat",
            reconnect=False,
        )
        import asyncio as _asyncio
        from unittest.mock import AsyncMock

        client._ws = AsyncMock()

        async def _check():
            with pytest.raises(ValueError):
                await client.rent(schedule_id=1, slot=0)
            with pytest.raises(ValueError):
                await client.rent(schedule_id=1, slot=65)
            with pytest.raises(ValueError):
                await client.rent(schedule_id=1, slot=SLOT_MAX + 1)

        _asyncio.run(_check())

    def test_slot_bounds_accepted(self):
        """slot 1 and 64 are accepted by the SDK (no ValueError from the range check)."""
        import asyncio as _asyncio

        from ceki_sdk._client import Client

        client = Client(
            api_key="k",
            relay_url="wss://relay.ceki.me/ws/agent",
            api_url="https://api.ceki.me",
            chat_url="https://chat.ceki.me/api/chat",
            reconnect=False,
        )

        async def _check():
            from ceki_sdk._exceptions import ConnectionLost

            for slot in (1, 64):
                try:
                    # _ws_send raises ConnectionLost (no live WS) — the point is
                    # that the slot range check does NOT raise ValueError first.
                    await client.rent(schedule_id=1, slot=slot)
                except (ConnectionLost, TimeoutError):
                    continue  # slot check passed; connection failure is unrelated

        _asyncio.run(_check())
