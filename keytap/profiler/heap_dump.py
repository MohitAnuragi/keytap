"""
Java heap snapshots of the foreground app via Perfetto's java_hprof data source.
Each snapshot is a .pftrace in <keytap folder>/data/heaps/ with a JSON sidecar. The app
freezes for a few seconds while the heap is dumped.
"""

import json
import logging
import os
import re
import time
from typing import List, Optional

from keytap.core.paths import HEAPS_DIR as _HEAPS_DIR
from keytap.profiler.perf_trace import _adb, foreground_package
from keytap.core.utils import adb_shell

_LOG = logging.getLogger(__name__)

HEAPS_DIR   = str(_HEAPS_DIR)
DEVICE_FILE = "/data/misc/perfetto-traces/keytap_heap.pftrace"
DUMP_MS     = 10_000
MAX_KEEP    = 10          # snapshots are tens of MB each
MIN_BYTES   = 100_000     # smaller means java_hprof produced no heap

_MEMINFO_KB = (("Java Heap", "java_kb"), ("Native Heap", "native_kb"),
               ("Graphics", "graphics_kb"), ("TOTAL PSS", "total_pss_kb"))
_MEMINFO_COUNTS = (("Views", "views"), ("Activities", "activities"))


def _config(pid: str) -> str:
    return f"""buffers {{ size_kb: 262144 fill_policy: DISCARD }}
duration_ms: {DUMP_MS}
data_sources {{ config {{ name: "android.java_hprof" java_hprof_config {{ pid: {pid} }} }} }}
"""


def _meminfo(serial, package) -> dict:
    out = adb_shell(f"dumpsys meminfo {package}", serial)
    info = {}
    for key, name in _MEMINFO_KB:
        m = re.search(rf"{key}:\s+(\d+)", out)
        if m:
            info[name] = int(m.group(1))
    for key, name in _MEMINFO_COUNTS:
        m = re.search(rf"\b{key}:\s+(\d+)", out)
        if m:
            info[name] = int(m.group(1))
    return info


def list_snapshots() -> List[dict]:
    """Saved snapshots, newest first. Each dict is the sidecar plus 'path'."""
    if not os.path.isdir(HEAPS_DIR):
        return []
    out = []
    for f in os.listdir(HEAPS_DIR):
        if not f.endswith(".json"):
            continue
        try:
            with open(os.path.join(HEAPS_DIR, f)) as fh:
                meta = json.load(fh)
        except Exception:
            continue
        path = os.path.join(HEAPS_DIR, f[:-5] + ".pftrace")
        if os.path.isfile(path):
            meta["path"] = path
            out.append(meta)
    out.sort(key=lambda m: m.get("started_at", ""), reverse=True)
    return out


def _trim():
    for meta in list_snapshots()[MAX_KEEP:]:
        base = meta["path"][:-len(".pftrace")]
        for ext in (".pftrace", ".json", ".pftrace.classes.json", ".pftrace.graph.npz"):
            try:
                os.remove(base + ext)
            except OSError:
                pass


def capture(serial: Optional[str], label: str = "", progress=None) -> str:
    """Dump the foreground app's Java heap. Returns local path. Raises RuntimeError.
    progress(msg) is called with a short status as the dump moves through its stages."""
    try:
        return _capture(serial, label, progress)
    finally:
        try:   # also when the dump failed or timed out, so nothing is left on the device
            _adb(serial, "shell", "rm", "-f", DEVICE_FILE)
        except Exception:
            pass


def _capture(serial: Optional[str], label: str = "", progress=None) -> str:
    progress = progress or (lambda _msg: None)
    progress("finding the foreground app...")
    package = foreground_package(serial)
    if not package:
        raise RuntimeError("no foreground app found")
    pids = adb_shell(f"pidof {package}", serial).split()
    if not pids:
        raise RuntimeError(f"{package} is not running")
    started = time.strftime("%Y-%m-%dT%H:%M:%S")
    meminfo = _meminfo(serial, package)

    progress(f"recording the heap of {package} (about {DUMP_MS // 1000} seconds)...")
    res = _adb(serial, "shell", "perfetto", "-c", "-", "--txt", "-o", DEVICE_FILE,
               input=_config(pids[0]).encode(), timeout=DUMP_MS // 1000 + 60)
    if res.returncode != 0:
        err = (res.stderr or res.stdout).decode(errors="replace").strip()
        raise RuntimeError(f"perfetto failed: {err[-120:]}")

    os.makedirs(HEAPS_DIR, exist_ok=True)
    slug = re.sub(r"[^\w-]+", "_", label).strip("_")[:30]
    base = os.path.join(HEAPS_DIR, time.strftime("%Y%m%d_%H%M%S") + (f"_{slug}" if slug else ""))
    path = base + ".pftrace"
    progress("copying the snapshot to this Mac...")
    res = _adb(serial, "pull", DEVICE_FILE, path, timeout=300)
    _adb(serial, "shell", "rm", "-f", DEVICE_FILE)
    if res.returncode != 0 or not os.path.isfile(path):
        err = (res.stderr or res.stdout).decode(errors="replace").strip()
        raise RuntimeError(f"pull failed: {err[:120]}")
    size = os.path.getsize(path)
    if size < MIN_BYTES:
        os.remove(path)
        raise RuntimeError(f"no heap captured for {package} (app must be debuggable or profileable)")

    dims = re.findall(r"(\d+)x(\d+)", adb_shell("wm size", serial))   # last = override size if set
    screen = [int(dims[-1][0]), int(dims[-1][1])] if dims else None
    with open(base + ".json", "w") as f:
        json.dump({"label": label, "package": package, "serial": serial or "",
                   "started_at": started, "size": size, "meminfo": meminfo,
                   "screen": screen}, f, indent=2)
    _trim()
    return path
