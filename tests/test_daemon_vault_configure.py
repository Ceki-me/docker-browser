"""Daemon mode tests for vault session.configure routing.

Verifies that daemon.py routes session.configure (with profile) from
relay → extension via session_id, and that the extension's vault
path applies cookies + buffers storage.

App mode = ws-router direct (tests/vault-configure.test.ts).
Daemon mode = relay → daemon → local-ws → extension (this file).
"""
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Import daemon module (relative to docker-browser/src)
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from ceki_browser_provider.daemon import Daemon, DaemonConfig, SpawnManager


@pytest.mark.asyncio
async def test_daemon_routes_session_configure_by_session_id():
    """Daemon must deliver session.configure to the correct Chrome instance
    when session_id is present (same routing as cdp / session_end)."""
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
    )
    from ceki_browser_provider.daemon import ProviderWsClient
    daemon = ProviderWsClient(cfg, MagicMock(), MagicMock())

    # Mock spawner with an active instance for session 'sess-vault'
    mock_inst = MagicMock()
    mock_inst.ws = AsyncMock()
    mock_inst.ws.state = 1  # extension presence-WS connected (deliverable)
    mock_inst.session_id = "sess-vault"
    daemon.spawner.active = MagicMock(return_value={"sess-vault": mock_inst})

    msg = {
        "type": "session.configure",
        "session_id": "sess-vault",
        "profile": {
            "cookies": [{"name": "sid", "value": "abc", "domain": ".ceki.me", "path": "/"}],
            "localStorage": {"https://app.ceki.me": {"key1": "val1"}},
            "sessionStorage": {},
        },
    }

    await daemon._on_message(msg)

    # The message should be delivered to the instance's WS (extension)
    mock_inst.ws.send.assert_awaited_once()
    sent = json.loads(mock_inst.ws.send.await_args[0][0])
    assert sent["type"] == "session.configure"
    assert sent["session_id"] == "sess-vault"
    assert sent["profile"]["cookies"][0]["name"] == "sid"


@pytest.mark.asyncio
async def test_daemon_drops_session_configure_without_session_id():
    """Without session_id the daemon must drop (same as cdp without sid)."""
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
    )
    from ceki_browser_provider.daemon import ProviderWsClient
    daemon = ProviderWsClient(cfg, MagicMock(), MagicMock())
    daemon.spawner.active = MagicMock(return_value={})

    msg = {"type": "session.configure", "profile": {"cookies": []}}
    await daemon._on_message(msg)
    # No instance → nothing delivered; no exception


@pytest.mark.asyncio
async def test_daemon_routes_session_configure_null_profile():
    """profile: null must clear vault state (same as ws-router case)."""
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
    )
    from ceki_browser_provider.daemon import ProviderWsClient
    daemon = ProviderWsClient(cfg, MagicMock(), MagicMock())
    mock_inst = MagicMock()
    mock_inst.ws = AsyncMock()
    mock_inst.ws.state = 1  # connected
    mock_inst.session_id = "sess-clear"
    daemon.spawner.active = MagicMock(return_value={"sess-clear": mock_inst})

    await daemon._on_message({
        "type": "session.configure",
        "session_id": "sess-clear",
        "profile": None,
    })

    mock_inst.ws.send.assert_awaited_once()
    sent = json.loads(mock_inst.ws.send.await_args[0][0])
    assert sent["type"] == "session.configure"
    assert sent["profile"] is None
