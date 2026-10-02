from adb import adb


def fetch(pkg):
    """Returns [(permission, 'GRANTED'|'DENIED')]"""
    items = []
    try:
        out = adb("shell", "dumpsys", "package", pkg).stdout.decode()
        for line in out.splitlines():
            s = line.strip()
            if ": granted=" in s:
                perm, rest = s.split(": granted=", 1)
                granted = rest.split(",")[0].strip() == "true"
                items.append((perm.strip(), "GRANTED" if granted else "DENIED"))
    except Exception as e:
        items = [("error", str(e))]
    return items


def toggle(perm, pkg, serial=None):
    """Grant or revoke a permission. Returns new state string."""
    items = fetch(pkg)
    current = next((s for p, s in items if p == perm), None)
    try:
        if current == "GRANTED":
            adb("shell", "pm", "revoke", pkg, perm)
            return "DENIED"
        else:
            adb("shell", "pm", "grant", pkg, perm)
            return "GRANTED"
    except Exception as e:
        return f"err: {e}"


def set_all(pkg, grant):
    """Grant or revoke every permission; skips ones pm refuses (non-runtime)."""
    for perm, state in fetch(pkg):
        if perm == "error" or (state == "GRANTED") == grant:
            continue
        try:
            adb("shell", "pm", "grant" if grant else "revoke", pkg, perm)
        except Exception:
            pass
