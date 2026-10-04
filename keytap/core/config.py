import argparse
import os
import shutil

from keytap.core.settings import section as _s


def _find_adb():
    found = shutil.which("adb")
    if found:
        return found
    candidate = os.path.expanduser("~/Library/Android/sdk/platform-tools/adb")
    if os.path.isfile(candidate):
        return candidate
    candidate = os.path.expanduser("~/Android/Sdk/platform-tools/adb")
    if os.path.isfile(candidate):
        return candidate
    return "adb"


ADB_PATH  = _find_adb()
HAS_FFMPEG = shutil.which("ffmpeg") is not None

# Display defaults come from settings.json, can still be overridden by CLI args
_disp = _s("display")
CURSOR_MODE = _disp.get("cursor_mode", True)
CURSOR_JUMP = _disp.get("cursor_jump", 5)


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--serial", default=None,  help="ADB serial (e.g. emulator-5554)")
    p.add_argument("--cols",   type=int,      default=_disp.get("cols", 10))
    p.add_argument("--rows",   type=int,      default=_disp.get("rows", 32))
    p.add_argument("--height", type=int,      default=_disp.get("height", 800),
                   help="mirror window height px")
    p.add_argument("--backend", choices=["screenrecord", "scrcpy"], default="scrcpy",
                   help="capture backend (default: scrcpy)")
    args, _ = p.parse_known_args()
    return args


_ARGS = None


def __getattr__(name):
    """Lazy ARGS: argparse only runs when ARGS is first accessed, not on import."""
    global _ARGS
    if name == 'ARGS':
        if _ARGS is None:
            _ARGS = parse_args()
        return _ARGS
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
