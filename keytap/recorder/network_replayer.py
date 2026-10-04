"""
NetworkReplayer: serves recorded HTTP/HTTPS flows via mitmproxy during replay.

Match strategy (method + normalized path, sequential):
  - Lookup key: (METHOD, /path/without/query)
  - Multiple responses for same key served round-robin in recorded order
  - Unmatched requests pass through to real network (safe fallback)

Usage:
  rep = NetworkReplayer()
  rep.start(capture)     # loads flows, sets Android proxy (starts mitmproxy once)
  # ... replayer executes actions ...
  rep.stop()             # clears proxy + flows (mitmproxy stays up for next replay)
  rep.shutdown()         # full teardown - call once on app exit
"""
import time
import threading
from typing import List, Optional
from urllib.parse import urlparse

from keytap.device.adb import adb

PROXY_PORT = 8787

try:
    from mitmproxy import options
    from mitmproxy import http as mhttp
    from mitmproxy.tools.dump import DumpMaster
    MITMPROXY_AVAILABLE = True
except ImportError:
    MITMPROXY_AVAILABLE = False


# ── ADB proxy helpers ─────────────────────────────────────────────────────────

def set_device_proxy(port: int):
    """Set device proxy via adb reverse so USB-connected devices work."""
    adb("reverse", f"tcp:{port}", f"tcp:{port}")
    adb("shell", "settings", "put", "global", "http_proxy", f"127.0.0.1:{port}")


def clear_device_proxy():
    adb("shell", "settings", "put", "global", "http_proxy", ":0")


def _free_external_port(port: int):
    """Kill stale processes from OTHER Python sessions holding the port.
    Skips our own PID — mitmproxy runs as a thread inside our process.
    """
    import subprocess
    import os
    our_pid = str(os.getpid())
    try:
        out = subprocess.check_output(
            ["lsof", "-ti", f":{port}"], stderr=subprocess.DEVNULL
        ).decode().strip()
        pids = [p.strip() for p in out.splitlines() if p.strip()]
        external = [p for p in pids if p != our_pid]
        if not external:
            return
        print(f"[replayer] killing stale external PIDs {external} on :{port}")
        for pid in external:
            subprocess.run(["kill", "-9", pid], stderr=subprocess.DEVNULL)
        for _ in range(20):
            time.sleep(0.1)
            still = subprocess.run(
                ["lsof", "-ti", f":{port}"], capture_output=True
            ).stdout.decode().strip()
            if not still:
                break
    except subprocess.CalledProcessError:
        pass  # port already free


# ── mitmproxy addon ───────────────────────────────────────────────────────────

class _FlowServerAddon:
    """
    Intercepts requests and serves recorded responses.
    Flows can be hot-swapped between replays via load_flows() / clear().
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._lookup: dict = {}
        self._cursors: dict = {}

    def load_flows(self, flows: List[dict]):
        lookup: dict = {}
        for f in sorted(flows, key=lambda x: x.get("seq", 0)):
            path = urlparse(f["url"]).path
            key = (f["method"].upper(), path)
            lookup.setdefault(key, []).append(f)
        with self._lock:
            self._lookup = lookup
            self._cursors = {}
        print(f"[replayer] loaded {len(lookup)} routes")

    def clear(self):
        with self._lock:
            self._lookup = {}
            self._cursors = {}

    def request(self, flow: "mhttp.HTTPFlow"):
        path = urlparse(flow.request.pretty_url).path
        key = (flow.request.method.upper(), path)
        with self._lock:
            entries = self._lookup.get(key)
            if not entries:
                print(f"[mitm] PASS  {flow.request.method} {path}")
                return  # pass through

            idx = self._cursors.get(key, 0)
            entry = entries[idx % len(entries)]
            self._cursors[key] = idx + 1

        body = entry.get("response_body", "")
        headers = entry.get("response_headers", {})
        status = entry.get("response_status", 200)

        flow.response = mhttp.Response.make(
            status_code=status,
            content=body.encode("utf-8", errors="replace"),
            headers=headers,
        )
        print(f"[mitm] MOCK  {flow.request.method} {path} -> {status} ({len(body)}b)")


# ── Public class ──────────────────────────────────────────────────────────────

class NetworkReplayer:
    """
    Keeps mitmproxy running for the lifetime of the app.
    start() / stop() hot-swap flows and toggle the device proxy.
    shutdown() does full teardown on app exit.
    """

    def __init__(self):
        self._master = None
        self._addon: Optional[_FlowServerAddon] = None
        self._thread: Optional[threading.Thread] = None
        self.active = False  # True when proxy is set on device

    def _ensure_proxy_running(self) -> bool:
        """Start mitmproxy if not already running. Returns True if up."""
        if self._thread and self._thread.is_alive():
            return True

        _free_external_port(PROXY_PORT)
        self._addon = _FlowServerAddon()

        def _run():
            import asyncio

            async def _start():
                opts = options.Options(listen_host="0.0.0.0", listen_port=PROXY_PORT)
                self._master = DumpMaster(opts, with_termlog=False, with_dumper=False)
                self._master.addons.add(self._addon)
                print(f"[replayer] mitmproxy started on :{PROXY_PORT}")
                await self._master.run()
                print(f"[replayer] mitmproxy exited")

            try:
                asyncio.run(_start())
            except BaseException as e:
                print(f"[replayer] mitmproxy thread exception: {type(e).__name__}: {e}")

        self._thread = threading.Thread(target=_run, daemon=True)
        self._thread.start()
        time.sleep(0.6)

        if not self._thread.is_alive():
            print(f"[replayer] FAILED: mitmproxy did not start")
            return False

        print(f"[replayer] mitmproxy ready on :{PROXY_PORT}")
        return True

    def start(self, network_capture) -> bool:
        """
        Load flows and set Android proxy.
        network_capture: dict {"flows": [...], "host_tokens": {...}}
                         OR plain List[dict] for backward compatibility.
        Returns False if mitmproxy not installed, no flows, or failed to start.
        """
        if isinstance(network_capture, list):
            flows = network_capture
        else:
            flows = network_capture.get("flows", [])

        if not MITMPROXY_AVAILABLE or not flows:
            return False

        if not self._ensure_proxy_running():
            return False

        self._addon.load_flows(flows)
        set_device_proxy(PROXY_PORT)
        self.active = True
        print(f"[replayer] READY: {len(flows)} flows, proxy set on device")
        return True

    def stop(self):
        """Clear flows and remove device proxy. Mitmproxy stays up."""
        if not self.active:
            return
        clear_device_proxy()
        if self._addon:
            self._addon.clear()
        self.active = False
        print(f"[replayer] stopped (proxy cleared, mitmproxy still running)")

    def shutdown(self):
        """Full teardown - call once on app exit."""
        if self.active:
            self.stop()
        if self._master:
            try:
                self._master.shutdown()
            except RuntimeError:
                pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3.0)
        print(f"[replayer] shutdown complete")
