import json
import os
import sqlite3 as sqlite
import subprocess
import tempfile

from keytap.device.adb import adb


def fetch(pkg):
    """Returns (items, raw_by_name).

    items = [(key, display_val)]
    raw_by_name = {key: raw_json_str}
    """
    items = []
    raw_by_name = {}
    try:
        result = adb("shell", "run-as", pkg, "cat",
                     f"/data/data/{pkg}/databases/cp-litmus.db")
        if result.returncode != 0 or len(result.stdout) < 100:
            return [("error", "cp-litmus.db not found or unreadable")], {}
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            f.write(result.stdout)
            tmppath = f.name
        try:
            conn = sqlite.connect(tmppath)
            rows = conn.execute(
                "SELECT experiment_name, experiment_value FROM experiment"
            ).fetchall()
            conn.close()
            for name, value_json in rows:
                raw_by_name[name] = value_json
                try:
                    v = json.loads(value_json)
                    variant = v.get("variant", "?")
                    props = v.get("properties", {})
                    props_str = "  |  " + ", ".join(
                        f"{k}: {val}" for k, val in props.items()
                    ) if props else ""
                    items.append((name, f"{variant}{props_str}"))
                except Exception:
                    items.append((name, value_json[:120]))
        finally:
            os.unlink(tmppath)
    except Exception as e:
        items = [("error", str(e))]
    return items, raw_by_name


def open_nano(key, raw_json):
    try:
        pretty = json.dumps(json.loads(raw_json), indent=2)
    except Exception:
        pretty = raw_json
    tmppath = os.path.join(tempfile.gettempdir(), "keytap_litmus_detail.json")
    with open(tmppath, "w") as f:
        f.write(pretty)
    subprocess.Popen([
        "osascript", "-e",
        f'tell application "Terminal" to do script "nano {tmppath}"'
    ])
