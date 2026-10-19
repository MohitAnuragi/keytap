import re
import subprocess

from keytap.core.config import ADB_PATH


def parse_bounds(bounds_str):
    nums = re.findall(r'\d+', bounds_str)
    return tuple(int(n) for n in nums) if len(nums) == 4 else None


def adb_shell(cmd, serial=None, u2_dev=None):
    """Run shell command on device. Uses u2 if available (fast), else subprocess ADB."""
    if u2_dev:
        try:
            result = u2_dev.shell(cmd)
            return result.output if hasattr(result, 'output') else str(result)
        except Exception:
            pass
    args = [ADB_PATH]
    if serial:
        args += ['-s', serial]
    args += ['shell', cmd]
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=10)
        return result.stdout
    except Exception:
        return ''
