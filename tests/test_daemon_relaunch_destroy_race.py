"""Regression tests for the relaunch/destroy race in _spawn_and_handshake.

Sequential live rents (rent -> close -> rent) leaked one Chrome per cycle:
the relaunch guard (5b) checks `still_alive` ONCE before recreating the
Xvfb/Chrome pair, then runs a second (slow, up to ~40s) handshake. If
session_ended lands AGAIN during that second handshake, destroy() pops the
instance from `_instances`, but the just-relaunched pair is never killed —
it orphans a Chrome that holds a CDP/display slot and wedges the next rent's
spawn (boot stagger climbs, extension never connects, watchdog reaps).

FIX: after the relaunch handshake, re-check `still_alive`; if the instance was
destroyed meanwhile, `_kill_group(inst)` the relaunched pair and return.
"""
import os
import sys
import tempfile
import time
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ceki_browser_provider.daemon import DaemonConfig, Instance, SpawnManager


def make_spawner():
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
        max_sessions=4,
        cdps=[9200, 9201, 9202, 9203],
        displays=[101, 102, 103, 104],
        policy_installed_ext=True,  # skip _patched_ext_dir / ext flags
    )
    with patch.dict(os.environ, {"CEKI_DAEMON_BOOT_STAGGER_S": "0"}, clear=False):
        return SpawnManager(cfg)


def make_instance(spawner, sid):
    inst = Instance(
        session_id=sid,
        storage_key=f"key-{sid}",
        profile_dir=tempfile.mkdtemp(prefix=f"prof-{sid}-"),
        display=spawner.cfg.displays[0],
        cdp_port=spawner.cfg.cdps[0],
        profile_mode="incognito",
    )
    # handshake must see ws not open so we enter the relaunch branch
    inst.ws = None
    with spawner._lock:
        spawner._instances[sid] = inst
    return inst


def _popen(pid):
    p = MagicMock()
    p.pid = pid
    return p


class TestRelaunchDestroysInstanceMidHandshake:
    """When session_ended lands during the relaunch handshake, the
    just-relaunched Xvfb/Chrome pair must be reaped, not orphaned."""

    def _setup(self, destroy_in_handshake):
        """Shared setup. Returns (spawner, inst)."""
        spawner = make_spawner()
        sid = "12345"
        inst = make_instance(spawner, sid)

        # launch order in _spawn_and_handshake:
        #   _launch_xvfb x2 (first + relaunch xvfb2)
        #   _launch_chrome x3 (install proc1, run proc2, relaunch proc2)
        chromes = iter([_popen(1001), _popen(1002), _popen(2002)])
        xvfbs = iter([_popen(1000), _popen(2001)])
        spawner._launch_chrome = MagicMock(side_effect=lambda *a, **k: next(chromes))
        spawner._launch_xvfb = MagicMock(side_effect=lambda *a, **k: next(xvfbs))
        spawner._discover_ext_id = MagicMock(return_value="extid")
        spawner._maybe_start_debug_capture = MagicMock()

        call_no = 0

        def fake_handshake(inst):
            nonlocal call_no
            call_no += 1
            # First call: no-op (ws stays None -> enters relaunch branch).
            # Second call (inside relaunch): simulate session_ended landing by
            # removing the instance from _instances while we are inside it.
            if destroy_in_handshake and call_no == 2:
                with spawner._lock:
                    spawner._instances.pop(inst.session_id, None)

        spawner._handshake = MagicMock(side_effect=fake_handshake)
        spawner._kill_group = MagicMock(wraps=spawner._kill_group)
        spawner._kill_proc_group = MagicMock()
        spawner._wait_exit = MagicMock()
        return spawner, inst

    def test_destroy_during_relaunch_handshake_reaps_pair(self):
        spawner, inst = self._setup(destroy_in_handshake=True)
        with patch("ceki_browser_provider.daemon._pid_alive", return_value=True), \
             patch("ceki_browser_provider.daemon.time.sleep"):  # skip X-socket waits
            spawner._spawn_and_handshake(inst, {})

        # The instance must have been reaped (killed) rather than orphaned.
        assert spawner._kill_group.called, (
            "expected _kill_group to reap the relaunched pair after mid-handshake destroy"
        )
        # And the instance is gone from the registry.
        with spawner._lock:
            assert inst.session_id not in spawner._instances

    def test_no_destroy_still_returns_normally(self):
        """Sanity: without a mid-handshake destroy, no spurious reap occurs."""
        spawner, inst = self._setup(destroy_in_handshake=False)
        with patch("ceki_browser_provider.daemon._pid_alive", return_value=True), \
             patch("ceki_browser_provider.daemon.time.sleep"):
            spawner._spawn_and_handshake(inst, {})

        # Instance survives (not destroyed) -> no reap.
        assert not spawner._kill_group.called
        with spawner._lock:
            assert inst.session_id in spawner._instances


class TestRespawnGuard:
    """Relay double-match right after session_ended must NOT spawn an orphan.

    When a rent ends and the renter immediately re-rents the same schedule,
    the relay can deliver a second `match` for the same sid while the old
    session is still unwinding. `ensure()` would spawn a fresh pair that no
    second session_ended ever closes (relay already considers the sid ended);
    the extension connects fine (ready=True) so the watchdog never reaps it —
    it leaks, holding a CDP/display slot for the next rent.
    """

    def test_recently_ended_blocks_spawn(self):
        spawner = make_spawner()
        sid = "99999"
        # Simulate a just-ended session.
        with spawner._lock:
            spawner._recently_ended[sid] = time.time()

        # Without the guard this would fall through to a real (slow, failing)
        # spawn; mock the spawn so the assertion isolates the guard itself.
        with patch("ceki_browser_provider.daemon.time.sleep"), \
             patch.object(spawner, "_spawn_and_handshake") as spawn_mock:
            got = spawner.ensure(sid, {})

        assert got is None, "ensure() must not spawn an orphan during respawn guard"
        spawn_mock.assert_not_called()
        with spawner._lock:
            assert sid not in spawner._instances

    def test_guard_expires_allows_spawn(self):
        spawner = make_spawner()
        sid = "88888"
        with spawner._lock:
            # Marked ended long ago -> guard window elapsed.
            spawner._recently_ended[sid] = time.time() - 1000

        with patch("ceki_browser_provider.daemon.time.sleep"), \
             patch.object(spawner, "_spawn_and_handshake") as spawn_mock:
            got = spawner.ensure(sid, {})

        assert got is not None, "ensure() must spawn after the guard window elapses"
        spawn_mock.assert_called_once()
        with spawner._lock:
            assert sid in spawner._instances