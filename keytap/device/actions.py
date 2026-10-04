"""
ADB action helpers. All fire-and-forget (run in daemon threads).
Call any function with on_status=callable to get a status string back.
"""

import os
import re
import threading
import time

from keytap.device.adb import adb


_local = threading.local()


def set_sync(on: bool):
    """Run this thread's actions inline instead of in a daemon thread. The replayer turns this
    on so each step finishes before the next one starts."""
    _local.sync = on


def _run(fn):
    if getattr(_local, "sync", False):
        fn()
    else:
        threading.Thread(target=fn, daemon=True).start()

def run_async(fn):
    """Public alias for fire-and-forget thread dispatch."""
    threading.Thread(target=fn, daemon=True).start()


# ── Device input ─────────────────────────────────────────────────────────────

def tap(dx, dy, on_status=None):
    if on_status:
        on_status(f"tap ({dx},{dy})")
    _run(lambda: adb("shell", "input", "tap", str(dx), str(dy)))


def keyevent(code, label="", on_status=None):
    if on_status:
        on_status(f"keyevent: {label}")
    _run(lambda: adb("shell", "input", "keyevent", str(code)))


def swipe(x1, y1, x2, y2, duration=300, label="swipe", on_status=None):
    if on_status:
        on_status(f"{label} ({x1},{y1})->({x2},{y2})")
    _run(lambda: adb("shell", "input", "swipe",
                     str(x1), str(y1), str(x2), str(y2), str(duration)))


_last_scroll_t = [0.0]
SCROLL_COOLDOWN = 0.4


def scroll(cx, cy, dev_h, direction, on_status=None):
    """Throttled scroll. direction: 'up' | 'down'"""
    now = time.time()
    if now - _last_scroll_t[0] < SCROLL_COOLDOWN:
        return
    _last_scroll_t[0] = now
    dist = int(dev_h * 0.4)
    half = dist // 2
    if direction == 'up':
        swipe(cx, max(cy - half, 0), cx, min(cy + half, dev_h - 1),
              label="scroll up", on_status=on_status)
    else:
        swipe(cx, min(cy + half, dev_h - 1), cx, max(cy - half, 0),
              label="scroll down", on_status=on_status)


def input_text(text, on_status=None):
    if on_status:
        on_status("input text")
    _run(lambda: adb("shell", "input", "text", text.replace(" ", "%s")))


def launch_deeplink(url, on_status=None):
    if on_status:
        on_status(f"launch {url}")
    _run(lambda: adb("shell", "am", "start", "-a",
                     "android.intent.action.VIEW", "-d", url))


# ── App management ───────────────────────────────────────────────────────────

def launcher_activity(pkg):
    """The app's own launcher activity as 'pkg/Activity', or None. Debug tools such as LeakCanary
    add a second launcher entry to the same package, so monkey would pick one at random."""
    out = adb("shell", "cmd", "package", "query-activities", "--brief",
              "-a", "android.intent.action.MAIN", "-c", "android.intent.category.LAUNCHER",
              pkg).stdout.decode(errors="replace")
    found = re.findall(rf"^\s*({re.escape(pkg)}/\S+)\s*$", out, re.M)
    return next((a for a in found if "leakcanary" not in a.lower()), found[0] if found else None)


def launch(pkg, on_status=None):
    if on_status:
        on_status(f"launch {pkg}")

    def _do():
        comp = launcher_activity(pkg)
        if comp:
            adb("shell", "am", "start", "-a", "android.intent.action.MAIN",
                "-c", "android.intent.category.LAUNCHER", "-n", comp)
        else:
            adb("shell", "monkey", "-p", pkg, "-c", "android.intent.category.LAUNCHER", "1")
    _run(_do)


def force_stop(pkg, on_status=None):
    if on_status:
        on_status(f"force-stop {pkg}")
    _run(lambda: adb("shell", "am", "force-stop", pkg))


def clear_data(pkg, on_status=None):
    if on_status:
        on_status(f"clear-data {pkg}")
    _run(lambda: adb("shell", "pm", "clear", pkg))


def set_permission(pkg, permission, state, on_status=None):
    """Set one permission (or "*" for all) to GRANTED or DENIED. Idempotent, safe to replay."""
    if on_status:
        on_status(f"{state.lower()} {permission if permission != '*' else 'all permissions'} for {pkg}")

    def _do():
        if permission == "*":
            from keytap.inspectors import permissions
            permissions.set_all(pkg, state == "GRANTED")
        else:
            adb("shell", "pm", "grant" if state == "GRANTED" else "revoke", pkg, permission)
    _run(_do)


def set_dev_option(label, state, on_status=None):
    """Set a Dev Options entry to the exact state it was recorded with."""
    if on_status:
        on_status(f"dev option {label} -> {state}")

    def _do():
        from keytap.core.config import ARGS
        from keytap.inspectors import dev_options
        dev_options.set_by_label(label, state, ARGS.serial)
    _run(_do)


def uninstall(pkg, on_status=None):
    if on_status:
        on_status(f"uninstall {pkg}")
    _run(lambda: adb("shell", "pm", "uninstall", pkg))


# ── APK install ───────────────────────────────────────────────────────────────

def install_apk(path, on_status=None):
    name = os.path.basename(path)
    if on_status:
        on_status(f"installing {name}...")
    def _do():
        result = adb("install", "-r", path)
        out = (result.stdout + result.stderr).decode(errors="replace")
        if on_status:
            if "Success" in out:
                on_status(f"installed {name}")
            else:
                lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
                on_status(lines[-1] if lines else "install failed")
    _run(_do)


