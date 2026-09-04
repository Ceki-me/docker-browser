"""Provider-side debug capture (opt-in, launcher only).

When ``CEKI_PROVIDER_DEBUG_LOG`` is set the launcher additionally:

  * starts Chromium with ``--remote-debugging-port`` (a fixed port, default
    9333) so the extension service-worker / extension pages can be attached
    over the DevTools Protocol;
  * runs a small capture thread that discovers chrome-extension targets for
    the provider extension id, attaches to each over CDP and appends their
    console/exception log lines to a file (with timestamps);
  * pings the extension service-worker main thread every
    ``CEKI_PROVIDER_DEBUG_SW_PING`` seconds (Runtime.evaluate on the SW
    target) and logs ``SW-UNRESPONSIVE`` if the SW stops answering — this
    distinguishes "service worker hung" from "page/tab capture wedged",
    the two failure modes behind silent browser freezes.

Env:
    CEKI_PROVIDER_DEBUG_LOG          enable; file path to append to
                                     ("1" / "true" -> /var/log/ceki-provider/ext-console.log)
    CEKI_PROVIDER_DEBUG_PORT         CDP port (default 9333)
    CEKI_PROVIDER_DEBUG_SW_PING      SW liveness ping interval in seconds (default 30)
    CEKI_PROVIDER_DEBUG_SW_TIMEOUT   seconds to wait for a ping ack (default 15)

The module is pure-opt-in: every public entry point is a no-op when the env
var is unset, so a normal provider launch carries zero overhead and never
imports the websocket stack.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger("ceki.provider.debug")

_DEFAULT_LOG = "/var/log/ceki-provider/ext-console.log"
_DEFAULT_PORT = 9333
_DEFAULT_PING = 30.0
_DEFAULT_PING_TIMEOUT = 15.0

_write_lock = threading.Lock()


@dataclass
class DebugConfig:
    log_path: Path
    port: int = _DEFAULT_PORT
    ping_interval: float = _DEFAULT_PING
    ping_timeout: float = _DEFAULT_PING_TIMEOUT

    def chrome_args(self) -> list[str]:
        return [f"--remote-debugging-port={self.port}"]


def config_from_env() -> DebugConfig | None:
    raw = os.environ.get("CEKI_PROVIDER_DEBUG_LOG", "").strip()
    if not raw:
        return None
    if raw in ("1", "true", "yes"):
        raw = _DEFAULT_LOG
    path = Path(raw)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:  # pragma: no cover - env-dependent
        log.warning("debug log dir unavailable (%s); debug capture disabled", exc)
        return None
    try:
        port = int(os.environ.get("CEKI_PROVIDER_DEBUG_PORT", _DEFAULT_PORT))
        ping = float(os.environ.get("CEKI_PROVIDER_DEBUG_SW_PING", _DEFAULT_PING))
        pto = float(os.environ.get("CEKI_PROVIDER_DEBUG_SW_TIMEOUT", _DEFAULT_PING_TIMEOUT))
    except ValueError:
        return None
    cfg = DebugConfig(log_path=path, port=port, ping_interval=ping, ping_timeout=pto)
    log.info("provider debug capture enabled -> %s (cdp port %s)", path, port)
    return cfg


def write_line(cfg: DebugConfig, line: str) -> None:
    try:
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        with _write_lock:
            with open(cfg.log_path, "a", encoding="utf-8") as fh:
                fh.write(f"[{ts}] {line}\n")
    except OSError:
        pass


def _http_json(url: str, timeout: float = 5.0) -> Any | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# per-target asyncio capture
# ---------------------------------------------------------------------------

def _arg_text(arg: dict) -> str:
    if "value" in arg:
        v = arg["value"]
        s = json.dumps(v, ensure_ascii=False)
        return s if len(s) <= 500 else s[:500] + "…"
    d = arg.get("description")
    return (d[:500] + "…") if d and len(d) > 500 else (d or "")


def _handle_event(cfg: DebugConfig, kind: str, target: str, msg: dict) -> None:
    method = msg.get("method")
    params = msg.get("params") or {}
    if method == "Runtime.consoleAPICalled":
        args = " ".join(_arg_text(a) for a in params.get("args", []))
        write_line(cfg, f"[{target}] console.{params.get('type')} {args}")
    elif method == "Runtime.exceptionThrown":
        ed = params.get("exceptionDetails") or {}
        text = ed.get("text", "")
        desc = (ed.get("exception") or {}).get("description", "")
        loc = f'{ed.get("url", "")}:{ed.get("lineNumber", "?")}:{ed.get("columnNumber", "?")}'
        write_line(cfg, f"[{target}] exception {text} {desc[:400]} @ {loc}")
    elif method == "Log.entryAdded":
        entry = params.get("entry") or {}
        write_line(
            cfg,
            f"[{target}] log.{entry.get('level')} {entry.get('source')} "
            f"{entry.get('text', '')[:400]}",
        )


def _sw_capture(cfg: DebugConfig, ws_url: str, target: str, stop: threading.Event) -> None:
    """Attach to one extension target and stream console until ws closes."""
    try:
        import asyncio

        import websockets

        async def run() -> None:
            async with websockets.connect(ws_url, max_size=4_000_000, ping_interval=None) as ws:
                async def cmd(mid: int, method: str, params: dict | None = None) -> None:
                    await ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))

                await cmd(1, "Runtime.enable")
                await cmd(2, "Log.enable")
                write_line(cfg, f"[{target}] attached (cdp)")
                mid = 100
                next_ping = time.monotonic() + cfg.ping_interval
                while not stop.is_set():
                    wait = max(0.05, next_ping - time.monotonic())
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=wait)
                    except asyncio.TimeoutError:
                        # liveness probe
                        mid += 1
                        expect = mid
                        try:
                            await cmd(expect, "Runtime.evaluate",
                                      {"expression": "1", "returnByValue": True})
                        except Exception as exc:  # pragma: no cover
                            write_line(cfg, f"[{target}] ping send failed: {exc}")
                            return
                        deadline = time.monotonic() + cfg.ping_timeout
                        ok = False
                        while time.monotonic() < deadline and not stop.is_set():
                            try:
                                raw = await asyncio.wait_for(
                                    ws.recv(),
                                    timeout=max(0.05, deadline - time.monotonic()),
                                )
                            except asyncio.TimeoutError:
                                break
                            try:
                                obj = json.loads(raw)
                            except Exception:
                                continue
                            if obj.get("id") == expect:
                                ok = True
                                break
                            if obj.get("method"):
                                _handle_event(cfg, "sw", target, obj)
                        if not ok:
                            write_line(cfg, f"[{target}] SW-UNRESPONSIVE "
                                             f"(no ping ack in {cfg.ping_timeout:.0f}s)")
                        next_ping = time.monotonic() + cfg.ping_interval
                        continue
                    try:
                        obj = json.loads(raw)
                    except Exception:
                        continue
                    if obj.get("method"):
                        _handle_event(cfg, "sw" if "sw" in target or "background" in target else "page",
                                      target, obj)

        asyncio.run(run())
    except Exception as exc:  # ws closed / target gone / import miss
        write_line(cfg, f"[{target}] capture ended: {type(exc).__name__} {exc}")


# ---------------------------------------------------------------------------
# manager thread: discover chrome-extension targets and fan out to captures
# ---------------------------------------------------------------------------

class _CaptureManager(threading.Thread):
    def __init__(self, cfg: DebugConfig, ext_id: str, stop: threading.Event) -> None:
        super().__init__(name="ceki-debug-capture", daemon=True)
        self.cfg = cfg
        self.ext_id = ext_id
        self.stop = stop
        self._active: dict[str, threading.Thread] = {}
        self._lock = threading.Lock()

    def _targets(self) -> list[dict]:
        data = _http_json(f"http://127.0.0.1:{self.cfg.port}/json/list")
        out: list[dict] = []
        if not isinstance(data, list):
            return out
        prefix = f"chrome-extension://{self.ext_id}/"
        for t in data:
            url = t.get("url", "") or ""
            if url.startswith(prefix) and t.get("webSocketDebuggerUrl"):
                out.append({"id": str(t.get("id", url)), "ws": t["webSocketDebuggerUrl"],
                            "url": url})
        return out

    def run(self) -> None:  # noqa: D401 - thread body
        write_line(self.cfg, f"capture manager started (ext={self.ext_id})")
        while not self.stop.is_set():
            for t in self._targets():
                tid = t["id"]
                with self._lock:
                    if tid in self._active and self._active[tid].is_alive():
                        continue
                tag = "sw" if ("background" in t["url"] or "service_worker" in t["url"]) else "page"
                th = threading.Thread(
                    target=_sw_capture,
                    args=(self.cfg, t["ws"], tag + ":" + tid[:8], self.stop),
                    daemon=True,
                    name=f"ceki-dbg-{tid[:8]}",
                )
                th.start()
                with self._lock:
                    self._active[tid] = th
            # reap finished threads
            with self._lock:
                self._active = {k: v for k, v in self._active.items() if v.is_alive()}
            self.stop.wait(3.0)


def start_capture(cfg: DebugConfig, ext_id: str, stop: threading.Event) -> _CaptureManager | None:
    if cfg is None:
        return None
    mgr = _CaptureManager(cfg, ext_id, stop)
    mgr.start()
    return mgr


def stop_capture(mgr: _CaptureManager | None) -> None:
    if mgr is None:
        return
    mgr.stop.set()
