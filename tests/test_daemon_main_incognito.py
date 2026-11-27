"""Main-profile vs incognito multi-rent isolation (daemon mode).

One agent token can occupy several slots of the same browser concurrently
(relay ev 9094 lets a renter create multiple parallel sessions on one
schedule; ev 14954 distinguishes them by `slot`). Each session's `match`
carries a `profile_mode` (decided by the backend, relay just passes it
through):

- `main`      → persistent profile keyed by `billable_type:billable_id`
                (stable across rents of the same owner/agent), stored under
                the persist dir when the container runs with persist=1.
- `incognito` → ephemeral per-session profile keyed by session_id, stored
                under the (tmpfs) session dir even in a persist container.

These tests prove, without real Chrome:

- `_storage_key_for` resolves the right key per profile_mode + billable;
- `_profile_path` places main vs incognito in different dirs (persist=1);
- two concurrent sessions (main + incognito, same renter token) get distinct
  instances: different session_id, display, cdp_port, storage_key, profile_dir;
- a main rent of the same billable owner REUSES the same profile dir across
  rents (persistence); another billable owner gets a different dir;
- an incognito rent NEVER reuses across rents — keyed by session_id.
"""
import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ceki_browser_provider.daemon import DaemonConfig, SpawnManager


def make_spawner(max_sessions=4, persist=False):
    cfg = DaemonConfig(
        token="test-token",
        schedule_id=40870,
        api_base="https://test.api",
        ext_dir="/tmp/test-ext",
        daemon_port=17894,
        width=1920,
        height=1080,
        storage_key="billable_type:billable_id",
        persist=persist,
        session_dir="/tmp/ceki-sessions",
        persist_session_dir="/tmp/ceki-sessions-persist",
        max_sessions=max_sessions,
        cdps=[9200, 9201, 9202, 9203],
        displays=[101, 102, 103, 104],
    )
    with patch.dict(
        os.environ,
        {"CEKI_DAEMON_CONNECT_TIMEOUT": "90", "CEKI_DAEMON_BOOT_STAGGER_S": "0"},
        clear=False,
    ):
        return SpawnManager(cfg)


# ── _storage_key_for ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "params,expected",
    [
        # main + billable fields → composite key from payload
        ({"profile_mode": "main", "billable_type": "user", "billable_id": 5}, "user:5"),
        ({"profile_mode": "main", "billable_type": "agent", "billable_id": 7}, "agent:7"),
        # main + only billable_id (no type) → falls back to session_id
        ({"profile_mode": "main", "billable_id": 5}, "sess-main"),
        # main + neither → falls back to session_id
        ({"profile_mode": "main"}, "sess-main"),
        # incognito / unset → always ephemeral session_id
        ({"profile_mode": "incognito", "billable_type": "user", "billable_id": 5}, "sess-incog"),
        ({"billable_type": "user", "billable_id": 5}, "sess-unset"),
        ({}, "sess-empty"),
    ],
)
def test_storage_key_for(params, expected):
    sesid = expected if expected.startswith("sess-") else "sess-other"
    spawner = make_spawner()
    assert spawner._storage_key_for(sesid, params) == expected


# ── _profile_path ────────────────────────────────────────────────────────────


def test_profile_path_main_goes_to_persist_dir_when_persist():
    spawner = make_spawner(persist=True)
    p = spawner._profile_path("user:5", "main")
    assert p.startswith("/tmp/ceki-sessions-persist/")
    assert p.endswith("user_5")  # unsafe chars sanitized


def test_profile_path_incognito_goes_to_session_dir_even_when_persist():
    spawner = make_spawner(persist=True)
    p = spawner._profile_path("sess-x", "incognito")
    assert p.startswith("/tmp/ceki-sessions/")
    assert p.endswith("sess-x")


def test_profile_path_unset_mode_goes_to_session_dir():
    spawner = make_spawner(persist=True)
    assert spawner._profile_path("sess-x", None).startswith("/tmp/ceki-sessions/")
    assert spawner._profile_path("sess-x", "").startswith("/tmp/ceki-sessions/")


def test_profile_path_no_persist_main_goes_to_session_dir():
    """Without persist=1, even main rents use the (tmpfs) session dir."""
    spawner = make_spawner(persist=False)
    p = spawner._profile_path("user:5", "main")
    assert p.startswith("/tmp/ceki-sessions/")


# ── main vs incognito concurrent (same renter token) ─────────────────────────


class TestMainIncognitoConcurrent:
    """Two parallel sessions — one main, one incognito — from the same renter."""

    def test_concurrent_main_and_incognito_get_distinct_instances(self):
        spawner = make_spawner(persist=True)
        with patch.object(spawner, "_spawn_and_handshake"):
            main = spawner.ensure("sess-main", {
                "profile_mode": "main",
                "billable_type": "user",
                "billable_id": 5,
            })
            incog = spawner.ensure("sess-incog", {
                "profile_mode": "incognito",
                "billable_type": "user",
                "billable_id": 5,
            })

        assert main is not None and incog is not None
        assert main.session_id != incog.session_id
        # Distinct CDP / display (two Chromes, no sharing)
        assert main.cdp_port != incog.cdp_port
        assert main.display != incog.display
        # Distinct storage keys (persist vs ephemeral)
        assert main.storage_key == "user:5"
        assert incog.storage_key == "sess-incog"
        # Distinct profile dirs (persist vs tmpfs)
        assert main.profile_dir != incog.profile_dir
        assert main.profile_dir.startswith("/tmp/ceki-sessions-persist/")
        assert incog.profile_dir.startswith("/tmp/ceki-sessions/")
        # profile_mode recorded on the instance
        assert main.profile_mode == "main"
        assert incog.profile_mode == "incognito"

    def test_main_profile_persists_across_rents_of_same_billable(self):
        """main rent of the same user reuses the same profile dir."""
        spawner = make_spawner(persist=True)
        with patch.object(spawner, "_spawn_and_handshake"):
            a = spawner.ensure("sess-A", {"profile_mode": "main", "billable_type": "user", "billable_id": 5})
            spawner.destroy("sess-A", "ended")
            b = spawner.ensure("sess-B", {"profile_mode": "main", "billable_type": "user", "billable_id": 5})

        assert a is not None and b is not None
        assert a.profile_dir == b.profile_dir, (
            "main rent of the same billable owner must reuse the persistent profile"
        )
        assert a.storage_key == b.storage_key == "user:5"

    def test_main_profile_differs_for_other_billable(self):
        """A different billable owner must NOT share the main profile dir."""
        spawner = make_spawner(persist=True)
        with patch.object(spawner, "_spawn_and_handshake"):
            a = spawner.ensure("sess-A", {"profile_mode": "main", "billable_type": "user", "billable_id": 5})
            b = spawner.ensure("sess-B", {"profile_mode": "main", "billable_type": "user", "billable_id": 6})

        assert a is not None and b is not None
        assert a.profile_dir != b.profile_dir
        assert a.storage_key == "user:5"
        assert b.storage_key == "user:6"

    def test_main_agent_does_not_share_user_profile(self):
        """`agent` billable is distinct from `user` — separate profile dirs."""
        spawner = make_spawner(persist=True)
        with patch.object(spawner, "_spawn_and_handshake"):
            a = spawner.ensure("sess-A", {"profile_mode": "main", "billable_type": "user", "billable_id": 5})
            b = spawner.ensure("sess-B", {"profile_mode": "main", "billable_type": "agent", "billable_id": 5})

        assert a is not None and b is not None
        assert a.profile_dir != b.profile_dir
        assert a.storage_key == "user:5"
        assert b.storage_key == "agent:5"

    def test_incognito_never_reuses_across_rents(self):
        """incognito profile is ephemeral — keyed by session_id, no reuse."""
        spawner = make_spawner(persist=True)
        with patch.object(spawner, "_spawn_and_handshake"):
            a = spawner.ensure("sess-A", {"profile_mode": "incognito", "billable_type": "user", "billable_id": 5})
            spawner.destroy("sess-A", "ended")
            b = spawner.ensure("sess-B", {"profile_mode": "incognito", "billable_type": "user", "billable_id": 5})

        assert a is not None and b is not None
        assert a.profile_dir != b.profile_dir  # different session → different profile
        assert a.storage_key == "sess-A"
        assert b.storage_key == "sess-B"