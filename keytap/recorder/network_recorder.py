"""
NetworkRecorder: captures HTTP/HTTPS flows via mitmproxy during a recording session.

Setup (one-time):
  1. pip install mitmproxy
  2. Run mitmproxy once on host to generate CA cert (~/.mitmproxy/mitmproxy-ca-cert.pem)
  3. Push cert to device and install as user CA (Settings > Security > Install cert)
     OR install as system CA (requires root / Magisk TrustUserCerts module)
  4. For apps with cert pinning, additional bypass is required (Frida / debug build)

Usage:
  rec = NetworkRecorder(scope_host="api.example.com")
  rec.start()                    # sets Android proxy, starts mitmproxy
  # ... user performs actions ...
  flows = rec.stop()             # clears proxy, returns captured flows
"""
import re
import threading
import time
from typing import List, Optional, Tuple

from keytap.device.adb import adb

PROXY_PORT = 8787

# Only capture these content-types - everything else (images, fonts, HTML, JS, CSS) skipped
CAPTURE_CONTENT_TYPES = {"application/json", "application/xml", "text/plain", "text/xml"}

# Response headers to keep for replay - discard the rest
KEEP_RESPONSE_HEADERS = {"content-type", "content-encoding", "transfer-encoding"}

# Response bodies larger than this are dropped (config dumps, A/B experiment payloads, etc.)
MAX_BODY_BYTES = 50_000

# Flows whose path contains any of these substrings are skipped entirely
EXCLUDE_PATH_PATTERNS = ["/litmus/", "/experiments", "/analytics", "/metrics", "/log", "/remoteconfig"]

# Matches JWT tokens in JSON request bodies: "field_name": "eyJ..."
_JWT_RE = re.compile(
    r'"([^"]{0,50}(?:token|access|auth|bearer)[^"]{0,50})"'
    r':\s*"(eyJ[A-Za-z0-9\-_]+\.[A-Za-z0-9\-_]+\.[A-Za-z0-9\-_]*)"',
    re.IGNORECASE,
)

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


def _free_port(port: int):
    """Kill stale external processes holding the port (skips our own PID)."""
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
        pass  # no process on port - fine


# ── mitmproxy addon ───────────────────────────────────────────────────────────

class _FlowCollectorAddon:
    def __init__(self, scope_host: Optional[str], start_t: float):
        self.flows: List[dict] = []
        self._scope_host = scope_host
        self._seq = 0
        self._lock = threading.Lock()
        self._start_t = start_t
        self._req_times: dict = {}   # flow.id -> wall-clock time of request

    def request(self, flow: "mhttp.HTTPFlow"):
        self._req_times[flow.id] = time.time()

    def response(self, flow: "mhttp.HTTPFlow"):
        host = flow.request.pretty_host

        # scope filter
        if self._scope_host and self._scope_host not in host:
            return

        # path exclude filter
        path = flow.request.path
        if any(pat in path for pat in EXCLUDE_PATH_PATTERNS):
            return

        # content-type filter - skip binary/asset responses
        ct = flow.response.headers.get("content-type", "").split(";")[0].strip().lower()
        if ct and not any(ct.startswith(t) for t in CAPTURE_CONTENT_TYPES):
            return

        try:
            req_body = flow.request.get_text(strict=False) or ""
        except Exception:
            req_body = ""
        try:
            resp_body = flow.response.get_text(strict=False) or ""
        except Exception:
            resp_body = ""

        # body size cap - skip oversized responses (config dumps, experiment payloads)
        if len(resp_body.encode("utf-8", errors="replace")) > MAX_BODY_BYTES:
            return

        # strip response headers - keep only what replay needs
        resp_headers = {
            k.lower(): v
            for k, v in flow.response.headers.items()
            if k.lower() in KEEP_RESPONSE_HEADERS
        }

        t_now = time.time()
        t_req_abs = self._req_times.pop(flow.id, t_now)
        t_req_ms  = int((t_req_abs - self._start_t) * 1000)
        t_res_ms  = int((t_now    - self._start_t) * 1000)

        with self._lock:
            self._seq += 1
            self.flows.append({
                "seq": self._seq,
                "method": flow.request.method,
                "url": flow.request.pretty_url,
                "path": flow.request.path,
                "host": host,
                "request_body": req_body,
                "response_status": flow.response.status_code,
                "response_headers": resp_headers,
                "response_body": resp_body,
                "t_request_ms":  t_req_ms,
                "t_response_ms": t_res_ms,
            })


# ── Token extraction ──────────────────────────────────────────────────────────

def _extract_tokens(flows: List[dict]) -> Tuple[List[dict], dict]:
    """
    Scans request bodies for repeated JWT tokens per host.
    Tokens appearing 2+ times for same host are extracted to host_tokens dict.
    Occurrences in request_body replaced with {{TOKEN:field_name}}.

    Returns: (modified_flows, host_tokens)
    host_tokens shape: { "api.example.com": { "access_token": "eyJ..." } }
    """
    # Collect token candidates: host -> field_name -> {value: count}
    candidates: dict = {}
    for f in flows:
        body = f.get("request_body", "")
        if not body:
            continue
        host = f["host"]
        for field_name, token_val in _JWT_RE.findall(body):
            host_map = candidates.setdefault(host, {})
            field_map = host_map.setdefault(field_name, {})
            field_map[token_val] = field_map.get(token_val, 0) + 1

    # Build host_tokens: only extract tokens repeated >=2 times
    host_tokens: dict = {}
    for host, fields in candidates.items():
        for field_name, val_counts in fields.items():
            if max(val_counts.values()) >= 2:
                best = max(val_counts, key=lambda v: val_counts[v])
                host_tokens.setdefault(host, {})[field_name] = best

    if not host_tokens:
        return flows, {}

    # Substitute token values with {{TOKEN:field_name}} in request bodies
    modified = []
    for f in flows:
        f = dict(f)
        host = f["host"]
        body = f.get("request_body", "")
        if body and host in host_tokens:
            for field_name, token_val in host_tokens[host].items():
                body = body.replace(
                    f'"{token_val}"',
                    f'"{{{{TOKEN:{field_name}}}}}"',
                )
            f["request_body"] = body
        modified.append(f)

    return modified, host_tokens


# ── Public class ──────────────────────────────────────────────────────────────

class NetworkRecorder:
    """
    Wraps mitmproxy to collect HTTP/HTTPS flows for a recording session.

    scope_host: if set, only flows whose host contains this string are captured.
                Pass your API domain (e.g. "api.myapp.com") to reduce noise.
    """

    def __init__(self, scope_host: Optional[str] = None):
        self._scope_host = scope_host
        self._addon: Optional[_FlowCollectorAddon] = None
        self._master = None
        self._thread: Optional[threading.Thread] = None
        self.active = False

    def start(self) -> bool:
        """
        Starts mitmproxy on PROXY_PORT and configures Android proxy via ADB.
        Returns False if mitmproxy is not installed.
        """
        if not MITMPROXY_AVAILABLE:
            return False

        _free_port(PROXY_PORT)
        self._start_t = time.time()
        self._addon = _FlowCollectorAddon(self._scope_host, self._start_t)

        def _run():
            import asyncio

            async def _start():
                opts = options.Options(listen_host="0.0.0.0", listen_port=PROXY_PORT)
                self._master = DumpMaster(opts, with_termlog=False, with_dumper=False)
                self._master.addons.add(self._addon)
                await self._master.run()

            try:
                asyncio.run(_start())
            except BaseException:
                pass

        self._thread = threading.Thread(target=_run, daemon=True)
        self._thread.start()
        time.sleep(0.6)  # wait for proxy to bind

        if not self._thread.is_alive():
            return False  # mitmproxy failed to start (port in use, etc.)

        set_device_proxy(PROXY_PORT)
        self.active = True
        return True

    def stop(self) -> dict:
        """
        Clears Android proxy, shuts down mitmproxy.
        Returns {"flows": [...], "host_tokens": {...}}.
        """
        if not self.active:
            return {"flows": [], "host_tokens": {}}
        clear_device_proxy()
        if self._master:
            try:
                self._master.shutdown()
            except RuntimeError:
                pass  # event loop already closed - mitmproxy thread already exited
        self.active = False
        raw_flows = list(self._addon.flows) if self._addon else []
        flows, host_tokens = _extract_tokens(raw_flows)
        return {"flows": flows, "host_tokens": host_tokens}
