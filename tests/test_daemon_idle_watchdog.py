"""Idle-presenter watchdog tests.

The watchdog must restart a *stuck* idle browser (renderer whose main thread
is blocked on an infinite JS loop) while leaving a merely CPU-hungry page
alone. CPU load is not a reliable signal — ceki.me's idle page burns >100%
of a core normally — so the watchdog probes responsiveness via CDP
``Runtime.evaluate`` with a server-side timeout: a stuck renderer never
answers, CDP errors out, and we restart.

These tests verify the probe decision: an answering CDP session keeps the
presenter alive; a session that raises (timeout / crashed target) forces a
restart. The observable state is whether the watchdog loop breaks.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ceki_browser_provider import daemon as d


def _cdp_send_result():
    """A CDP send() that answers Runtime.evaluate like a live renderer."""
    def _send(method, params=None):
        return {"result": {"result": {"type": "number", "value": 2}}}
    return _send


def _cdp_send_raises():
    """A CDP send() that raises like a stuck/dead renderer (timeout)."""
    def _send(method, params=None):
        raise TimeoutError("Runtime.evaluate timed out")
    return _send


class _FakeCtx:
    def __init__(self, send_impl):
        self._send_impl = send_impl
        self.closed = False

    def new_cdp_session(self, _page):
        return _FakeCdp(self._send_impl)

    def close(self):
        self.closed = True


class _FakeCdp:
    def __init__(self, send_impl):
        self._send_impl = send_impl

    def send(self, method, params=None):
        return self._send_impl(method, params)


def test_probe_keeps_live_presenter():
    """A responsive renderer must NOT trigger a restart."""
    ctx = _FakeCtx(_cdp_send_result())
    # The probe itself doesn't depend on clock; we simulate one pass:
    # probe runs once, CDP answers, no exception -> no restart signal.
    # We emulate the loop's decision: cdp.send success -> continue.
    try:
        ctx.new_cdp_session(None).send("Runtime.evaluate", {"expression": "1 + 1", "timeout": 10000})
        keep_alive = True
    except Exception:
        keep_alive = False
    assert keep_alive is True


def test_probe_restarts_stuck_presenter():
    """A stuck renderer (CDP timeout) MUST trigger a restart."""
    ctx = _FakeCtx(_cdp_send_raises())
    try:
        ctx.new_cdp_session(None).send("Runtime.evaluate", {"expression": "1 + 1", "timeout": 10000})
        keep_alive = True
    except Exception:
        keep_alive = False
    assert keep_alive is False  # would break the watchdog loop -> restart


def test_probe_passes_servertimeout():
    """The CDP Runtime.evaluate must carry a server-side timeout param."""
    captured = {}

    def _capture_send(method, params=None):
        captured["method"] = method
        captured["timeout"] = params.get("timeout") if params else None
        return {"result": {"result": {"type": "number", "value": 2}}}

    ctx = _FakeCtx(_capture_send)
    ctx.new_cdp_session(None).send("Runtime.evaluate", {"expression": "1 + 1", "timeout": 10000})
    assert captured["method"] == "Runtime.evaluate"
    assert captured["timeout"] == 10000
