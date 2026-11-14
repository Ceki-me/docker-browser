"""Multi-session isolation tests (ev 14954).

The daemon keeps one Chrome instance per session_id (``SpawnManager._instances``,
a ``dict[session_id, Instance]``). With ``CEKI_DAEMON_MAX_SESSIONS >= 3`` a
single provider serves up to 3 parallel rentals, each with its own Xvfb display,
its own CDP port and its own extension presence-WS.

These tests prove, without real Chrome:

- 3 sessions get 3 distinct instances (distinct session_id, display, cdp_port);
- a CDP command addressed to session A is delivered to A's WS only (never to B's
  — per-session WS, no shared global tab);
- ``Page.navigate`` to three sessions in parallel routes each to its own
  instance (no cross-leak);
- destroying one session leaves the other two untouched.
"""
import asyncio
import json
import os
import sys
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ceki_browser_provider.daemon import DaemonConfig, Instance, ProviderWsClient, SpawnManager


def make_spawner(max_sessions=3):
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
        cdps=[9200, 9201, 9202],
        displays=[101, 102, 103],
    )
    with patch.dict(
        os.environ,
        {"CEKI_DAEMON_CONNECT_TIMEOUT": "90", "CEKI_DAEMON_BOOT_STAGGER_S": "0"},
        clear=False,
    ):
        return SpawnManager(cfg)


def seed_instance(spawner, sid, *, ready=True, ws_state=1):
    """Insert an instance directly with an OPEN (or CLOSED) presence-WS."""
    inst = Instance(
        session_id=sid,
        storage_key=f"key-{sid}",
        profile_dir=f"/tmp/prof-{sid}",
        display=spawner.cfg.displays[0],
        cdp_port=spawner.cfg.cdps[0],
        profile_mode="incognito",
    )
    inst.created_at = time.time()
    inst.ready = ready
    ws = AsyncMock()
    ws.state = ws_state
    inst.ws = ws
    with spawner._lock:
        spawner._instances[sid] = inst
    return inst


class TestThreeDistinctInstances:
    """3 parallel sessions → 3 distinct instances with distinct session_id/display/CDP."""

    def test_three_sessions_get_distinct_instances(self):
        spawner = make_spawner(max_sessions=3)
        with patch.object(spawner, "_spawn_and_handshake"):
            a = spawner.ensure("sess-A", {})
            b = spawner.ensure("sess-B", {})
            c = spawner.ensure("sess-C", {})
        assert a is not None and b is not None and c is not None
        # distinct session ids
        assert len({a.session_id, b.session_id, c.session_id}) == 3
        # distinct CDP ports and displays — each session gets its own Chrome
        assert len({a.cdp_port, b.cdp_port, c.cdp_port}) == 3
        assert len({a.display, b.display, c.display}) == 3
        # one Chrome per session, no tab sharing
        assert spawner.active().keys() == {"sess-A", "sess-B", "sess-C"}

    def test_max_sessions_caps_spawns(self):
        spawner = make_spawner(max_sessions=2)
        with patch.object(spawner, "_spawn_and_handshake"):
            assert spawner.ensure("s1", {}) is not None
            assert spawner.ensure("s2", {}) is not None
            assert spawner.ensure("s3", {}) is None  # full → denied
        assert len(spawner.active()) == 2


class TestCdpIsolation:
    """CDP addressed to session A never reaches session B's WS."""

    @pytest.mark.asyncio
    async def test_cdp_routes_to_own_instance_ws_only(self):
        spawner = make_spawner(max_sessions=3)
        inst_a = seed_instance(spawner, "sess-A")
        inst_b = seed_instance(spawner, "sess-B")
        inst_c = seed_instance(spawner, "sess-C")

        daemon = ProviderWsClient(spawner.cfg, spawner, MagicMock())
        msg = {
            "type": "cdp",
            "session_id": "sess-A",
            "id": 1,
            "method": "Runtime.evaluate",
            "params": {"expression": "window.__cekiMarker = 'A'"},
        }
        await daemon._on_message(msg)

        # A's WS got the CDP
        sent_a = json.loads(inst_a.ws.send.await_args[0][0])
        assert sent_a["type"] == "cdp"
        assert sent_a["session_id"] == "sess-A"
        # B's and C's WS must NOT have received it
        assert inst_b.ws.send.await_count == 0
        assert inst_c.ws.send.await_count == 0

    @pytest.mark.asyncio
    async def test_runtime_evaluate_marker_does_not_cross_sessions(self):
        """The unique-marker isolation check: each session's CDP reaches only it."""
        spawner = make_spawner(max_sessions=3)
        inst_a = seed_instance(spawner, "sess-A")
        inst_b = seed_instance(spawner, "sess-B")
        daemon = ProviderWsClient(spawner.cfg, spawner, MagicMock())

        await daemon._on_message({
            "type": "cdp", "session_id": "sess-A", "id": 1,
            "method": "Runtime.evaluate",
            "params": {"expression": "window.__cekiMarker='A'"},
        })
        await daemon._on_message({
            "type": "cdp", "session_id": "sess-B", "id": 2,
            "method": "Runtime.evaluate",
            "params": {"expression": "window.__cekiMarker='B'"},
        })

        a_sent = json.loads(inst_a.ws.send.await_args_list[0][0][0])
        b_sent = json.loads(inst_b.ws.send.await_args_list[0][0][0])
        assert "window.__cekiMarker='A'" in a_sent["params"]["expression"]
        assert "window.__cekiMarker='B'" in b_sent["params"]["expression"]
        # Cross-check: A's expression was NOT sent to B and vice versa
        assert "window.__cekiMarker='B'" not in a_sent["params"]["expression"]
        assert "window.__cekiMarker='A'" not in b_sent["params"]["expression"]

    @pytest.mark.asyncio
    async def test_parallel_page_navigate_no_cross_leak(self):
        """Page.navigate to three sessions in parallel → each gets its own URL."""
        spawner = make_spawner(max_sessions=3)
        inst_a = seed_instance(spawner, "sess-A")
        inst_b = seed_instance(spawner, "sess-B")
        inst_c = seed_instance(spawner, "sess-C")
        daemon = ProviderWsClient(spawner.cfg, spawner, MagicMock())

        async def nav(sid, url, cid):
            await daemon._on_message({
                "type": "cdp", "session_id": sid, "id": cid,
                "method": "Page.navigate", "params": {"url": url},
            })

        await asyncio.gather(
            nav("sess-A", "https://a.example", 1),
            nav("sess-B", "https://b.example", 2),
            nav("sess-C", "https://c.example", 3),
        )

        # Exactly one message per session, each with its own URL
        a_sent = json.loads(inst_a.ws.send.await_args_list[0][0][0])
        b_sent = json.loads(inst_b.ws.send.await_args_list[0][0][0])
        c_sent = json.loads(inst_c.ws.send.await_args_list[0][0][0])
        assert a_sent["params"]["url"] == "https://a.example"
        assert b_sent["params"]["url"] == "https://b.example"
        assert c_sent["params"]["url"] == "https://c.example"
        assert inst_a.ws.send.await_count == 1
        assert inst_b.ws.send.await_count == 1
        assert inst_c.ws.send.await_count == 1


class TestDestroyIsolation:
    """Destroying one session must not tear down the other two."""

    @pytest.mark.asyncio
    async def test_close_one_session_keeps_others_alive(self):
        spawner = make_spawner(max_sessions=3)
        inst_a = seed_instance(spawner, "sess-A")
        inst_b = seed_instance(spawner, "sess-B")
        inst_c = seed_instance(spawner, "sess-C")
        assert len(spawner.active()) == 3

        spawner.destroy("sess-B", "ended")

        remaining = spawner.active()
        assert set(remaining.keys()) == {"sess-A", "sess-C"}
        assert remaining["sess-A"] is inst_a
        assert remaining["sess-C"] is inst_c
        # B's CDP slot returned to the pool, A and C untouched
        assert inst_b.cdp_port in spawner.cfg.cdps

    @pytest.mark.asyncio
    async def test_session_ended_for_one_does_not_affect_others(self):
        """_deliver(session_ended) destroys only that session's instance."""
        spawner = make_spawner(max_sessions=3)
        inst_a = seed_instance(spawner, "sess-A")
        inst_b = seed_instance(spawner, "sess-B")
        inst_c = seed_instance(spawner, "sess-C")
        daemon = ProviderWsClient(spawner.cfg, spawner, MagicMock())

        with patch.object(spawner, "destroy", wraps=spawner.destroy) as spy:
            await daemon._deliver("sess-B", {"type": "session_ended", "session_id": "sess-B"})

        assert spy.call_args.args[0] == "sess-B"
        assert set(spawner.active().keys()) == {"sess-A", "sess-C"}
        # The other sessions' presence-WS never got the session_ended
        assert inst_a.ws.send.await_count == 0
        assert inst_c.ws.send.await_count == 0
