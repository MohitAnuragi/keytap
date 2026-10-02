"""
PerfettoCapture: on-device Perfetto system trace (frame timeline + atrace).
Records to a file on the device (no live streaming over USB), pulls it on stop
into ~/.keytap/traces/ with a JSON sidecar describing how it was captured.
"""

import json
import logging
import os
import re
import subprocess
import time
from typing import List, Optional

from config import ADB_PATH
from utils import adb_shell

_LOG = logging.getLogger(__name__)

TRACES_DIR   = os.path.expanduser("~/.keytap/traces")
DEVICE_TRACE = "/data/misc/perfetto-traces/keytap.perfetto-trace"

# Safety net: perfetto ends the session itself if keytap dies before stop().
MAX_DURATION_MS = 180_000

PRESETS = {
    # frame timeline + view/gfx/input slices, no per-context-switch events
    "light": {"buffer_kb": 65536,  "events": ["task/task_newtask", "task/task_rename"]},
    # adds CPU scheduling: shows running vs runnable vs blocked time, higher overhead
    "full":  {"buffer_kb": 131072, "events": ["task/task_newtask", "task/task_rename",
                                              "sched/sched_switch", "sched/sched_wakeup",
                                              "sched/sched_waking"]},
}

_ATRACE_CATEGORIES = ["view", "gfx", "input", "am", "wm"]
_FOCUS_RE = re.compile(r'u\d+\s+([\w.]+)/')


def _adb(serial, *cmd, input=None, timeout=15):
    full = [ADB_PATH]
    if serial:
        full += ["-s", serial]
    full += list(cmd)
    return subprocess.run(full, capture_output=True, input=input, timeout=timeout)


def build_config(preset: str, package: Optional[str]) -> str:
    p = PRESETS[preset]
    events = "\n".join(f'    ftrace_events: "{e}"' for e in p["events"])
    cats   = "\n".join(f'    atrace_categories: "{c}"' for c in _ATRACE_CATEGORIES)
    return f"""buffers {{ size_kb: {p["buffer_kb"]} fill_policy: DISCARD }}
duration_ms: {MAX_DURATION_MS}
data_sources {{ config {{ name: "android.surfaceflinger.frametimeline" }} }}
data_sources {{ config {{ name: "linux.process_stats" process_stats_config {{ scan_all_processes_on_start: true }} }} }}
data_sources {{ config {{ name: "linux.ftrace" ftrace_config {{
{events}
{cats}
    atrace_apps: "{package or '*'}"
}} }} }}
"""


def foreground_package(serial) -> str:
    m = _FOCUS_RE.search(adb_shell("dumpsys window | grep mCurrentFocus", serial))
    return m.group(1) if m else ""


def list_traces() -> List[dict]:
    """Saved traces, newest first. Each dict is the sidecar plus 'path'."""
    if not os.path.isdir(TRACES_DIR):
        return []
    out = []
    for f in os.listdir(TRACES_DIR):
        if not f.endswith(".json"):
            continue
        try:
            with open(os.path.join(TRACES_DIR, f)) as fh:
                meta = json.load(fh)
        except Exception:
            continue
        path = os.path.join(TRACES_DIR, f[:-5] + ".perfetto-trace")
        if os.path.isfile(path):
            meta["path"] = path
            out.append(meta)
    out.sort(key=lambda m: m.get("started_at", ""), reverse=True)
    return out


class PerfettoCapture:

    def __init__(self):
        self.active = False
        self._pid = None
        self._serial = None
        self._meta = {}
        self._start_t = 0.0

    def start(self, serial: Optional[str], preset: str = "light",
              features: Optional[List[str]] = None) -> None:
        """Start tracing on the device. Raises RuntimeError with a user-facing message."""
        if self.active:
            raise RuntimeError("trace already running")
        package = foreground_package(serial)
        sdk = adb_shell("getprop ro.build.version.sdk", serial).strip()
        debuggable = False
        if package:
            flags = adb_shell(f"dumpsys package {package} | grep -E 'flags='", serial)
            debuggable = "DEBUGGABLE" in flags

        res = _adb(serial, "shell", "perfetto", "-c", "-", "--txt",
                   "-o", DEVICE_TRACE, "--background",
                   input=build_config(preset, package).encode())
        m = re.search(r'\d+', res.stdout.decode(errors="replace"))
        if res.returncode != 0 or not m:
            err = (res.stderr or res.stdout).decode(errors="replace").strip()
            raise RuntimeError(f"perfetto failed to start: {err[:120] or 'no pid returned'}")

        self._pid    = m.group(0)
        self._serial = serial
        self._start_t = time.time()
        self._meta = {
            "preset":     preset,
            "serial":     serial or "",
            "sdk":        int(sdk) if sdk.isdigit() else None,
            "package":    package,
            "debuggable": debuggable,
            "features":   features or [],
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        self.active = True
        _LOG.debug("perfetto started pid=%s preset=%s pkg=%s", self._pid, preset, package)

    def stop(self) -> str:
        """Stop tracing, pull the file. Returns local path. Raises RuntimeError on failure."""
        if not self.active:
            raise RuntimeError("no trace running")
        self.active = False
        serial = self._serial
        _adb(serial, "shell", "kill", "-TERM", self._pid)
        for _ in range(30):   # wait up to 15s for perfetto to flush and exit
            if _adb(serial, "shell", "kill", "-0", self._pid).returncode != 0:
                break
            time.sleep(0.5)

        os.makedirs(TRACES_DIR, exist_ok=True)
        base = os.path.join(TRACES_DIR, f"{time.strftime('%Y%m%d_%H%M%S')}_{self._meta['preset']}")
        path = base + ".perfetto-trace"
        res = _adb(serial, "pull", DEVICE_TRACE, path, timeout=120)
        if res.returncode != 0 or not os.path.isfile(path):
            err = (res.stderr or res.stdout).decode(errors="replace").strip()
            raise RuntimeError(f"pull failed: {err[:120]}")
        _adb(serial, "shell", "rm", "-f", DEVICE_TRACE)

        self._meta["duration_s"] = round(time.time() - self._start_t, 1)
        with open(base + ".json", "w") as f:
            json.dump(self._meta, f, indent=2)
        return path
