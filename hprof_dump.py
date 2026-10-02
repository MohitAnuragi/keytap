"""
Full HPROF dumps of the foreground app via `am dumpheap`. Unlike the Perfetto heap graph these keep
primitive fields and string contents, so they feed the content-level screens (activity state,
RecyclerView rows, bitmap sources, duplicate strings). Each dump is a .hprof in <keytap folder>/data/hprofs/
with a JSON sidecar. Dumps hold everything in app memory, including user data: keep them local.
"""

import json
import os
import re
import struct
import time
from typing import List, Optional

from heap_dump import _meminfo
from paths import HPROFS_DIR as _HPROFS_DIR
from perf_trace import _adb, foreground_package
from utils import adb_shell

HPROFS_DIR  = str(_HPROFS_DIR)
DEVICE_FILE = "/data/local/tmp/keytap.hprof"
MAX_KEEP    = 5            # dumps are 100MB+ each
_END_TAG    = 0x2C         # HEAP_DUMP_END: present only when the dump is complete


def list_snapshots() -> List[dict]:
    """Saved dumps, newest first. Each dict is the sidecar plus 'path'."""
    if not os.path.isdir(HPROFS_DIR):
        return []
    out = []
    for f in os.listdir(HPROFS_DIR):
        if not f.endswith(".json"):
            continue
        try:
            with open(os.path.join(HPROFS_DIR, f)) as fh:
                meta = json.load(fh)
        except Exception:
            continue
        path = os.path.join(HPROFS_DIR, f[:-5] + ".hprof")
        if os.path.isfile(path):
            meta["path"] = path
            out.append(meta)
    out.sort(key=lambda m: m.get("started_at", ""), reverse=True)
    return out


def _trim():
    for meta in list_snapshots()[MAX_KEEP:]:
        base = meta["path"][:-len(".hprof")]
        for ext in (".hprof", ".json", ".hprof.detail.json"):
            try:
                os.remove(base + ext)
            except OSError:
                pass


def _device_size(serial) -> int:
    out = adb_shell(f"stat -c %s {DEVICE_FILE} 2>/dev/null", serial).strip()
    return int(out) if out.isdigit() else 0


def _complete(path) -> bool:
    with open(path, "rb") as f:
        f.seek(-9, os.SEEK_END)
        tag, _, length = struct.unpack(">BII", f.read(9))
    return tag == _END_TAG and length == 0


def capture(serial: Optional[str], label: str = "", progress=None) -> str:
    """Dump the foreground app's heap. Returns local path. Raises RuntimeError.
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
    started = time.strftime("%Y-%m-%dT%H:%M:%S")
    meminfo = _meminfo(serial, package)
    _adb(serial, "shell", "rm", "-f", DEVICE_FILE)

    progress(f"dumping the heap of {package} on the device...")
    res = _adb(serial, "shell", f"am dumpheap {package} {DEVICE_FILE}", timeout=60)
    if res.returncode != 0:
        err = (res.stderr or res.stdout).decode(errors="replace").strip()
        raise RuntimeError(f"dumpheap failed: {err[-120:]}")
    last, stable = -1, 0
    for _ in range(180):                      # am returns before the file is finished
        size = _device_size(serial)
        progress(f"writing dump on the device: {size / (1 << 20):.0f} MB")
        stable = stable + 1 if size and size == last else 0
        if stable >= 2:
            break
        last = size
        time.sleep(1)
    else:
        raise RuntimeError("dumpheap did not finish (is the app debuggable?)")

    os.makedirs(HPROFS_DIR, exist_ok=True)
    slug = re.sub(r"[^\w-]+", "_", label).strip("_")[:30]
    base = os.path.join(HPROFS_DIR, time.strftime("%Y%m%d_%H%M%S") + (f"_{slug}" if slug else ""))
    path = base + ".hprof"
    progress(f"copying the dump to this Mac ({last / (1 << 20):.0f} MB)...")
    res = _adb(serial, "pull", DEVICE_FILE, path, timeout=600)
    _adb(serial, "shell", "rm", "-f", DEVICE_FILE)
    if res.returncode != 0 or not os.path.isfile(path):
        err = (res.stderr or res.stdout).decode(errors="replace").strip()
        raise RuntimeError(f"pull failed: {err[:120]}")
    progress("checking the dump is complete...")
    if not _complete(path):
        os.remove(path)
        raise RuntimeError("dump is incomplete or not a valid HPROF")

    dims = re.findall(r"(\d+)x(\d+)", adb_shell("wm size", serial))
    with open(base + ".json", "w") as f:
        json.dump({"label": label, "package": package, "serial": serial or "",
                   "started_at": started, "size": os.path.getsize(path), "meminfo": meminfo,
                   "screen": [int(dims[-1][0]), int(dims[-1][1])] if dims else None}, f, indent=2)
    _trim()
    return path
