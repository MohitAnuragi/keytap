"""
steer: drive the phone one step at a time with tiny output, for an AI that looks at the screen between steps.

A raw uiautomator dump is about 90 KB (25k tokens). `screen` prints only what is visible or tappable,
one short line each (about 250 tokens), and every action prints the new screen, or "screen unchanged".
Tappable lines carry a number; `tap 5` uses the numbers of the last snapshot, `tap "text"` matches a label.

  steer.py screen                        what is on screen
  steer.py tap 5 | tap "Accept" [nth]    tap by number or by label (any visible text), then show the result
  steer.py type "hello"                  type into the focused field
  steer.py key back|home|enter|recents   press a key
  steer.py swipe up|down|left|right      up = finger moves up = see what is below
  steer.py launch <package>
  steer.py see "is a dialog open?"       ask the local vision model, the picture never reaches the caller
  steer.py mark                          remember "now" in logcat
  steer.py logs "regex" [--max 5]        count and sample logcat lines since the mark (clickstream checks)

Set ANDROID_SERIAL to pick a phone when several are attached.
"""

import argparse
import hashlib
import json
import re
import shlex
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

from keytap.core.config import ADB_PATH
from keytap.core.paths import DATA_DIR
from keytap.core.utils import parse_bounds

STATE = DATA_DIR / "steer" / "state.json"
MAX_LABEL = 50
MAX_LINES = 45
KEYS = {"back": 4, "home": 3, "enter": 66, "recents": 187, "delete": 67, "tab": 61}
FOCUS_RE = re.compile(r"u\d+\s+([\w.]+)/([\w.$]+)")


def adb(*args, timeout=15):
    return subprocess.run([ADB_PATH, *args], capture_output=True, timeout=timeout).stdout.decode(errors="replace")


def load_state():
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {}


def save_state(state):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state))


def hierarchy():
    try:   # uiautomator2 dumps in about 0.4 s, plain `uiautomator dump` takes about 2.5 s
        import uiautomator2 as u2
        xml = u2.connect().dump_hierarchy()
        return xml if isinstance(xml, str) else xml.decode(errors="replace")
    except Exception:
        pass
    out = adb("exec-out", "uiautomator", "dump", "/dev/tty")
    end = out.find("</hierarchy>")
    if end < 0:
        raise SystemExit("no hierarchy: phone not connected, locked, or uiautomator busy")
    return out[:end + len("</hierarchy>")]


def foreground():
    m = FOCUS_RE.search(adb("shell", "dumpsys window | grep mCurrentFocus"))
    return f"{m.group(1)}/{m.group(2)}" if m else "unknown"


def digest(xml):
    """[(tappable, label, (cx, cy))] in document order. A tappable container takes the labels of everything
    inside it (a list row reads "Name / last message / time"); plain text is listed once, without a number."""
    root = ET.fromstring(xml)
    nodes = list(root.iter("node"))
    # the status bar and nav buttons are never what the caller wants, unless nothing else is on screen
    skip = "com.android.systemui" if any(n.get("package") != "com.android.systemui" for n in nodes) else None
    out, seen = [], set()

    def own(n):
        return re.sub(r"\s+", " ", n.get("text") or n.get("content-desc") or "").strip()[:MAX_LABEL]

    def labels(n):
        if n.get("package") == skip:
            return []
        found = [own(n)] if own(n) else []
        for c in n:
            found += labels(c)
        return found

    def walk(n):
        if n.get("package") == skip:
            return
        is_input = n.get("class") == "android.widget.EditText"
        # a clickable that wraps other clickables (a full-screen scrim around a menu) would swallow them: list the inner ones
        inner = any(d.get("clickable") == "true" or d.get("class") == "android.widget.EditText" for d in n.iter("node") if d is not n)
        if (n.get("clickable") == "true" or is_input) and not inner:
            b = parse_bounds(n.get("bounds", ""))
            label = " / ".join(dict.fromkeys(labels(n))) or (n.get("resource-id") or "").split("/")[-1]
            if is_input:
                label = f"[input] {label}".strip()
            if b and b[2] > b[0] and b[3] > b[1] and label:
                out.append((True, label[:MAX_LABEL * 2], ((b[0] + b[2]) // 2, (b[1] + b[3]) // 2)))
            return
        if own(n) and own(n) not in seen:
            seen.add(own(n))
            nb = parse_bounds(n.get("bounds", ""))
            out.append((False, own(n), ((nb[0] + nb[2]) // 2, (nb[1] + nb[3]) // 2) if nb else None))
        for c in n:
            walk(c)

    walk(root)
    return out


def render(items, fg):
    lines, n = [f"fg {fg}"], 0
    for tappable, label, _ in items[:MAX_LINES]:
        if tappable:
            n += 1
            lines.append(f"{n} {label}")
        else:
            lines.append(f"  {label}")
    if len(items) > MAX_LINES:
        lines.append(f"  +{len(items) - MAX_LINES} more")
    return "\n".join(lines)


def snapshot(state, quiet_if_same):
    xml = hierarchy()
    items = digest(xml)
    taps = [i for i in items[:MAX_LINES] if i[0]]
    text = render(items, foreground())
    sig = hashlib.md5(text.encode()).hexdigest()
    # a signature from an old session means the caller has not seen that screen, so print it
    same = quiet_if_same and state.get("sig") == sig and time.time() - state.get("sig_at", 0) < 300
    free = [{"label": l, "xy": xy} for t, l, xy in items[:MAX_LINES] if not t and xy]
    state.update(sig=sig, sig_at=time.time(), taps=[{"label": l, "xy": xy} for _, l, xy in taps], free=free)
    save_state(state)
    return "screen unchanged" if same else text


def settle(seconds):
    time.sleep(seconds)


def cmd_screen(a, state):
    print(snapshot(state, False))


def cmd_tap(a, state):
    taps = state.get("taps")
    if taps is None:
        raise SystemExit("no snapshot yet: run `screen` first")
    target = a.target
    if target.isdigit():
        i = int(target) - 1
        if not 0 <= i < len(taps):
            raise SystemExit(f"no element {target}, screen has {len(taps)} tappable")
        hit = taps[i]
    else:
        # numbered tappables first, then plain visible text (many rows are not marked clickable but take a tap)
        hits = [t for t in taps + state.get("free", []) if target.lower() in t["label"].lower()]
        if not hits:
            raise SystemExit(f"no label on screen contains '{target}'")
        if a.nth > len(hits):
            raise SystemExit(f"only {len(hits)} labels contain '{target}'")
        hit = hits[a.nth - 1]
    adb("shell", "input", "tap", str(hit["xy"][0]), str(hit["xy"][1]))
    print(f"tapped {hit['label'][:40]}")
    settle(a.wait)
    print(snapshot(state, True))


def cmd_type(a, state):
    adb("shell", f"input text {shlex.quote(a.text.replace(' ', '%s'))}")
    settle(a.wait)
    print(snapshot(state, True))


def cmd_key(a, state):
    code = KEYS.get(a.key) or (int(a.key) if a.key.isdigit() else None)
    if code is None:
        raise SystemExit(f"unknown key, use one of {', '.join(KEYS)} or a keycode number")
    adb("shell", "input", "keyevent", str(code))
    settle(a.wait)
    print(snapshot(state, True))


def cmd_swipe(a, state):
    w, h = map(int, re.search(r"(\d+)x(\d+)", adb("shell", "wm", "size")).groups())
    cx, cy = w // 2, h // 2
    dx, dy = {"up": (0, -h * 0.3), "down": (0, h * 0.3), "left": (-w * 0.3, 0), "right": (w * 0.3, 0)}[a.direction]
    adb("shell", "input", "swipe", str(cx), str(cy), str(int(cx + dx)), str(int(cy + dy)), "300")
    settle(a.wait)
    print(snapshot(state, True))


def cmd_launch(a, state):
    # debug tools such as LeakCanary add a second launcher entry to the package, so monkey would pick one at random
    out = adb("shell", "cmd", "package", "query-activities", "--brief", "-a", "android.intent.action.MAIN",
              "-c", "android.intent.category.LAUNCHER", a.package)
    found = re.findall(rf"^\s*({re.escape(a.package)}/\S+)\s*$", out, re.M)
    comp = next((c for c in found if "leakcanary" not in c.lower()), found[0] if found else None)
    if comp:
        adb("shell", "am", "start", "-a", "android.intent.action.MAIN", "-c", "android.intent.category.LAUNCHER", "-n", comp)
    else:
        adb("shell", "monkey", "-p", a.package, "-c", "android.intent.category.LAUNCHER", "1")
    settle(a.wait + 1)
    print(snapshot(state, True))


def cmd_see(a, state):
    import base64
    import io

    from PIL import Image

    from keytap.watchdog import llm
    img = Image.open(io.BytesIO(subprocess.run([ADB_PATH, "exec-out", "screencap", "-p"], capture_output=True, timeout=15).stdout)).convert("RGB")
    img.thumbnail((540, 1200))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=70)
    system = "You answer one question about an Android screenshot in at most two short sentences. Say only what is visible; if unsure, say so."
    schema = {"type": "object", "required": ["answer"], "properties": {"answer": {"type": "string"}}}
    answer, _ = llm.chat(a.model, system, a.question, schema, images=[base64.b64encode(buf.getvalue()).decode()])
    print(answer["answer"])


def cmd_mark(a, state):
    state["mark"] = adb("shell", "date", "+%m-%d %H:%M:%S.000").strip()
    save_state(state)
    print(f"marked {state['mark']}")


def cmd_logs(a, state):
    if not state.get("mark"):
        raise SystemExit("no mark yet: run `mark` before the flow")
    out = adb("logcat", "-d", "-v", "time", "-T", state["mark"], timeout=60)
    rx = re.compile(a.pattern)
    hits = [l for l in out.splitlines() if rx.search(l)]
    print(f"{len(hits)} matches since {state['mark']}")
    shown = hits[:a.max] if len(hits) <= a.max else hits[:a.max - 1] + hits[-1:]
    for line in shown:
        print(line[:a.width])


def parser():
    p = argparse.ArgumentParser(prog="steer.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, wait=1.0):
        s = sub.add_parser(name)
        s.set_defaults(fn=fn)
        if wait:
            s.add_argument("--wait", type=float, default=wait, help="seconds to let the screen settle")
        return s

    add("screen", cmd_screen, wait=0)
    s = add("tap", cmd_tap)
    s.add_argument("target")
    s.add_argument("nth", nargs="?", type=int, default=1)
    add("type", cmd_type).add_argument("text")
    add("key", cmd_key).add_argument("key")
    add("swipe", cmd_swipe).add_argument("direction", choices=["up", "down", "left", "right"])
    add("launch", cmd_launch).add_argument("package")
    s = add("see", cmd_see, wait=0)
    s.add_argument("question")
    s.add_argument("--model", default="gemma4:12b")
    add("mark", cmd_mark, wait=0)
    s = add("logs", cmd_logs, wait=0)
    s.add_argument("pattern")
    s.add_argument("--max", type=int, default=5)
    s.add_argument("--width", type=int, default=160)
    return p


def main():
    a = parser().parse_args()
    a.fn(a, load_state())
    return 0


if __name__ == "__main__":
    sys.exit(main())
