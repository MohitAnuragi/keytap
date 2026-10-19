import subprocess
import re

from keytap.core.config import ADB_PATH, ARGS


def list_devices():
    """Return list of online device serials from `adb devices`."""
    out = subprocess.run([ADB_PATH, "devices"], capture_output=True, timeout=10).stdout.decode()
    devices = []
    for line in out.splitlines()[1:]:  # skip "List of devices attached"
        parts = line.strip().split()
        if len(parts) >= 2 and parts[1] == "device":
            devices.append(parts[0])
    return devices


def adb(*cmd):
    full = [ADB_PATH]
    if ARGS.serial:
        full += ["-s", ARGS.serial]
    full += list(cmd)
    return subprocess.run(full, capture_output=True, timeout=15)


def get_device_size():
    out = adb("shell", "wm", "size").stdout.decode()
    m = re.search(r'(\d+)x(\d+)', out)
    if not m:
        return 1080, 2400
    w, h = int(m.group(1)), int(m.group(2))
    return (w, h) if w < h else (h, w)


def take_screencap():
    return adb("exec-out", "screencap", "-p").stdout
