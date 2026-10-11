"""Regression tests for the stale-session rebind bug.

When a rent's extension never connects and the relay closes the session
(session.ended arrives while the extension's presence-WS is still down):

- OLD: _deliver() dropped the message and returned WITHOUT destroy() — the
  instance (Xvfb + Chrome) stayed in SpawnManager._instances for up to
  CEKI_DAEMON_CONNECT_TIMEOUT (90s), and match_ws() "first-free" then parked
  the NEXT rent's extension on that dead instance → the new rent never got an
  extension and the relay timed it out (rent#2/#3 of a quick sequence fail).

- NEW:
  1. _deliver() destroys the instance on session.ended/session.kill even when
     the extension never connected.
  2. match_ws() first-free skips instances whose extension-connect window has
     already expired (not ready + older than connect timeout).
"""
import asyncio
import os
import sys
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ceki_browser_provider.daemon import DaemonConfig, Instance, SpawnManager


def make_spawner(max_sessions=4, connect_timeout="90"):
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
        cdps=[9200, 9201, 9202, 9203],
        displays=[101, 102, 103, 104],
    )
    with patch.dict(
        os.environ,
        {"CEKI_DAEMON_CONNECT_TIMEOUT": connect_timeout, "CEKI_DAEMON_BOOT_STAGGER_S": "0"},
        clear=False,
    ):
        return SpawnManager(cfg)


def seed_instance(spawner, sid, *, ready=False, ws_state=None, age=0.0):
    """Insert an instance directly (as spawn does) with controllable age."""
    inst = Instance(
        session_id=sid,
        storage_key=f"key-{sid}",
        profile_dir=f"/tmp/prof-{sid}",
        display=spawner.cfg.displays[0],
        cdp_port=spawner.cfg.cdps[0],
        profile_mode="incognito",
    )
    inst.created_at = time.time() - age
    inst.ready = ready
    if ws_state is not None:
        ws = MagicMock()
        ws.state = ws_state
        inst.ws = ws
    with spawner._lock:
        spawner._instances[sid] = inst
    return inst


class TestDeliverSessionEndedDestroysUnconnectedInstance:
    """Fix 1: _deliver must destroy an instance whose extension never connected."""

    @pytest.mark.asyncio
    async def test_session_ended_before_connect_destroys_instance(self):
        from ceki_browser_provider.daemon import ProviderWsClient

        spawner = make_spawner()
        inst = seed_instance(spawner, "dead-session")  # ws=None, ready=False, fresh
        daemon = ProviderWsClient(spawner.cfg, spawner, MagicMock())

        with patch.object(spawner, "destroy", wraps=spawner.destroy) as spy:
            await daemon._deliver("dead-session", {"type": "session_ended", "session_id": "dead-session"})

        assert "dead-session" not in spawner._instances, (
            "session.ended for an unconnected instance must destroy it "
            "(it was previously left to leak until the watchdog)"
        )
        assert spy.called

    @pytest.mark.asyncio
    async def test_session_kill_before_connect_destroys_instance(self):
        """session.kill (backend reaper) must also destroy an unconnected instance."""
        from ceki_browser_provider.daemon import ProviderWsClient

        spawner = make_spawner()
        seed_instance(spawner, "killed-session")
        daemon = ProviderWsClient(spawner.cfg, spawner, MagicMock())

        with patch.object(spawner, "destroy", wraps=spawner.destroy) as spy:
            await daemon._deliver("killed-session", {"type": "session.kill", "session_id": "killed-session"})

        assert "killed-session" not in spawner._instances
        assert spy.called

    @pytest.mark.asyncio
    async def test_connected_session_ended_still_destroys(self):
        """Connected sessions keep the normal destroy-on-ended path (no regression)."""
        from ceki_browser_provider.daemon import ProviderWsClient

        spawner = make_spawner()
        ws = AsyncMock()
        ws.state = 1  # OPEN
        inst = seed_instance(spawner, "connected", ready=True, ws_state=1)
        inst.ws = ws
        daemon = ProviderWsClient(spawner.cfg, spawner, MagicMock())

        with patch.object(spawner, "destroy", wraps=spawner.destroy) as spy:
            await daemon._deliver("connected", {"type": "session_ended", "session_id": "connected"})

        assert ws.send.called, "ended must still be delivered to the connected extension"
        assert "connected" not in spawner._instances, "connected session must also be destroyed"
        assert spy.called


class TestMatchWsSkipsStaleInstance:
    """Fix 2: match_ws first-free must not bind to a stale (connect-window-expired) instance."""

    def test_first_free_binds_fresh_unconnected_instance(self):
        """A freshly spawned instance (age < timeout) is a legitimate target."""
        spawner = make_spawner()
        fresh = seed_instance(spawner, "fresh", age=20.0)
        ws = MagicMock()

        matched = spawner.match_ws(ws)

        assert matched is fresh, "fresh instance should be bound"

    def test_first_free_skips_stale_unconnected_instance(self):
        """An instance older than connect timeout (still not ready) must be skipped."""
        spawner = make_spawner(connect_timeout="30")
        stale = seed_instance(spawner, "stale", age=120.0)
        fresh = seed_instance(spawner, "fresh", age=5.0)
        ws = MagicMock()

        matched = spawner.match_ws(ws)

        assert matched is fresh, (
            "first-free must skip the stale instance and bind the fresh one"
        )
        assert stale.ws is None, "stale instance must not receive the extension"
        assert fresh.ws is ws

    def test_first_free_only_stale_returns_none(self):
        """If the only free instance is stale, do not park the extension on it."""
        spawner = make_spawner(connect_timeout="30")
        stale = seed_instance(spawner, "stale", age=120.0)
        ws = MagicMock()

        matched = spawner.match_ws(ws)

        assert matched is None, "must not bind to a stale instance"
        assert stale.ws is None

    def test_ready_instance_is_never_skipped(self):
        """Fix 2 must not break reconnect for a connected (ready) instance."""
        spawner = make_spawner()
        ready = seed_instance(spawner, "ready", ready=True, ws_state=1)  # OPEN
        ws = MagicMock()

        matched = spawner.match_ws(ws)

        assert matched is ready, "ready instance with a live WS must be rebindable"

    def test_by_session_id_binds_exact_instance(self):
        """When the extension sends ?session_id=, it must bind to that exact instance."""
        spawner = make_spawner(connect_timeout="30")
        stale = seed_instance(spawner, "stale", age=120.0)  # stale, would be skipped
        target = seed_instance(spawner, "target", age=5.0)
        ws = MagicMock()

        matched = spawner.match_ws(ws, session_id="target")

        assert matched is target, "explicit session_id must win over stale-skipping"
        assert stale.ws is None
        assert target.ws is ws

    def test_by_session_id_without_stale_skips_unknown(self):
        """An explicit session_id for a non-existent session falls through to first-free."""
        spawner = make_spawner(connect_timeout="30")
        fresh = seed_instance(spawner, "fresh", age=5.0)
        ws = MagicMock()

        matched = spawner.match_ws(ws, session_id="does-not-exist")

        assert matched is fresh, "unknown explicit sid should fall back to first-free"

    def test_live_ws_instance_is_not_stolen_by_first_free(self):
        """first-free must NOT rebind an instance that has a live (OPEN) ws."""
        spawner = make_spawner(connect_timeout="30")
        live = seed_instance(spawner, "live", ready=True, ws_state=1)
        fresh = seed_instance(spawner, "fresh", age=5.0)
        ws = MagicMock()

        matched = spawner.match_ws(ws)

        assert matched is fresh, "live instance must keep its WS; fresh is the target"
        assert live.ws is not ws, "open WS must not be stolen"


class TestBootSlotResetsOnIdle:
    """Fix 3: _boot_slot must reset when no instances are in flight.

    Without the reset the stagger counter grows forever (slot 13 → 24s,
    slot 14 → 26s, …) and a rent after idle sleeps that long BEFORE spawning,
    while the extension's presence-WS waits on the same lock — the relay's
    connect timeout fires and the rent is lost.
    """

    def test_boot_slot_resets_between_sequential_rents(self):
        """ensure() resets the stagger to 0 when nothing is in flight, then
        increments — a rent after idle starts at slot 1, not slot N+1."""
        spawner = make_spawner()
        with patch.object(spawner, "_spawn_and_handshake") as spawn_mock:
            # First rent: slot 1.
            assert spawner.ensure("a", {}) is not None
            assert spawner._boot_slot == 1
            spawner.destroy("a", "ended")
            # Second rent immediately: still in-flight? no → reset then inc → 1.
            assert spawner.ensure("b2", {}) is not None
            assert spawner._boot_slot == 1, "sequential rents must not accumulate stagger"
            spawner.destroy("b2", "ended")

    def test_parallel_spawns_still_stagger(self):
        """Concurrent in-flight instances keep staggering (no reset while busy)."""
        spawner = make_spawner()
        with patch.object(spawner, "_spawn_and_handshake"):
            assert spawner.ensure("a", {}) is not None
            assert spawner._boot_slot == 1
            assert spawner.ensure("b", {}) is not None
            assert spawner._boot_slot == 2, "parallel burst increments"
            # 'a' ends while 'b' still spawning → slot must NOT reset.
            spawner.destroy("a", "ended")
            assert spawner.ensure("c", {}) is not None
            assert spawner._boot_slot == 3, "must keep staggering while b is busy"
            spawner.destroy("b", "ended")
            spawner.destroy("c", "ended")