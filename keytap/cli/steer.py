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
  steer.py do "tap Chat" "tap Groups" "key back"   several steps in one call (labels only, no money words)
  steer.py flow <dir> [--param k=v] [--package p]   replay a keytap flow in one call (wipes need --allow-clear)
  steer.py see "is a dialog open?"       ask the local vision model, the picture never reaches the caller
  steer.py mark                          remember "now" in logcat
  steer.py logs "regex" [--max 5]        count and sample logcat lines since the mark (clickstream checks)

Every command that prints the screen also takes --find REGEX (only matching lines, numbers unchanged) and
--top (only the topmost menu or sheet; the numbers then refer to it).
Every action takes --quiet: print one line instead of the screen. Numbers are then stale until the next `screen`;
tapping by label still works, it reads the screen first.
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
SAVED_LOG = DATA_DIR / "steer" / "saved_log.txt"
MAX_LABEL = 50
MAX_LINES = 45        # lines printed for a plain screen
STATE_LINES = 150     # lines kept for `tap` and searched by --find
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
    # mCurrentFocus can list two displays (the first may be another app on a secondary display), so ask for the
    # one resumed activity; fall back to the last focus line when nothing is resumed (a notification shade)
    m = FOCUS_RE.search(adb("shell", "dumpsys activity activities | grep topResumedActivity"))
    if not m:
        found = FOCUS_RE.findall(adb("shell", "dumpsys window | grep mCurrentFocus"))
        m = found[-1] if found else None
        return f"{m[0]}/{m[1]}" if m else "unknown"
    return f"{m.group(1)}/{m.group(2)}"


def area(b):
    return (b[2] - b[0]) * (b[3] - b[1]) if b else 0


def overlay_scope(root):
    """(scope, kind): what `--top` keeps. kind is "dialog window" when the app-side window is smaller than the
    screen (a system dialog such as an ANR box is its own window and the app window can be missing from the dump),
    "top layer" when a clickable scrim covers the window and sits after full-size content (a popup menu or sheet
    drawn inside the window), else "" for the whole screen. With several app windows the smallest is taken."""
    wins = [w for w in root if w.get("package") != "com.android.systemui"] or list(root)
    win = min(wins, key=lambda w: area(parse_bounds(w.get("bounds", "")))) if len(wins) > 1 else wins[0]
    boxes = [parse_bounds(w.get("bounds", "")) for w in root if parse_bounds(w.get("bounds", ""))]
    screen = area((min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))) if boxes else 0
    if screen and area(parse_bounds(win.get("bounds", ""))) < screen * 0.95:
        return win, "dialog window"
    full = area(parse_bounds(win.get("bounds", ""))) * 0.95

    def covers(n):
        return area(parse_bounds(n.get("bounds", ""))) >= full

    scrim = None
    for parent in win.iter("node"):
        kids = list(parent)
        for i, k in enumerate(kids):
            if i and len(k) and k.get("clickable") == "true" and covers(k) and any(covers(e) for e in kids[:i]):
                scrim = k   # document order, so the last one is the topmost
    return (list(scrim), "top layer") if scrim is not None else (None, "")


def digest(xml, top=False):
    """([(tappable, label, (cx, cy))], kind) in document order. A tappable container takes the labels of
    everything inside it (a list row reads "Name / last message / time"); plain text is listed once, without a number.
    top=True keeps only the topmost overlay (see overlay_scope), or the whole screen when there is none."""
    root = ET.fromstring(xml)
    scope, kind = overlay_scope(root) if top else (None, "")
    nodes = list(root.iter("node"))
    # the status bar and nav buttons are never what the caller wants, unless nothing else is on screen
    skip = "com.android.systemui" if any(n.get("package") != "com.android.systemui" for n in nodes) else None
    out, seen = [], set()

    def own(n):
        return re.sub(r"\s+", " ", n.get("text") or n.get("content-desc") or "").strip()[:MAX_LABEL]

    rows = [(own(n), parse_bounds(n.get("bounds", ""))) for n in nodes if n.get("package") != skip and own(n)]

    def row_title(b):
        """The top-most text to the left of a control on the same row: a switch has no text, its title sits beside it."""
        h = b[3] - b[1]
        near = [(t, rb) for t, rb in rows if rb and rb[2] <= b[0] + 1 and min(rb[3], b[3]) - max(rb[1], b[1]) >= 0.3 * h]
        return min(near, key=lambda x: x[1][1])[0] if near else ""

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
            if n.get("checkable") == "true" and b:   # a switch or checkbox: show its state, and the title beside it when it has none
                state_ = "on" if n.get("checked") == "true" else "off"
                label = f"[{state_}] {label if label and not label.startswith('[') else row_title(b) or label}".strip()
            if b and b[2] > b[0] and b[3] > b[1] and label:
                out.append((True, label[:MAX_LABEL * 2], ((b[0] + b[2]) // 2, (b[1] + b[3]) // 2)))
            return
        if own(n) and own(n) not in seen:
            seen.add(own(n))
            nb = parse_bounds(n.get("bounds", ""))
            out.append((False, own(n), ((nb[0] + nb[2]) // 2, (nb[1] + nb[3]) // 2) if nb else None))
        for c in n:
            walk(c)

    for start in ([scope] if kind == "dialog window" else scope if scope is not None else [root]):
        walk(start)
    return out, kind


def render(items, fg, find=None, note=""):
    """The screen as text. Tappable lines are numbered over the whole kept list, so `--find` shows the same
    numbers `tap` uses; find keeps only the lines whose label matches the regex."""
    lines, n = [], 0
    for tappable, label, _ in items[:STATE_LINES]:
        if tappable:
            n += 1
            lines.append((f"{n} {label}", label))
        else:
            lines.append((f"  {label}", label))
    head = f"fg {fg}{note}"
    if find:
        try:
            rx = re.compile(find, re.I)
        except re.error as e:
            raise SystemExit(f"bad --find regex: {e}")
        hits = [t for t, l in lines if rx.search(l)]
        return "\n".join([head, *hits, f"  ({len(hits)} of {len(lines)} lines match)"])
    out = [head, *[t for t, _ in lines[:MAX_LINES]]]
    if len(lines) > MAX_LINES:
        out.append(f"  +{len(lines) - MAX_LINES} more")
    return "\n".join(out)


def snapshot(state, quiet_if_same, find=None, top=False):
    """Read the screen, keep it in the state for `tap`, and return the text to print. --top keeps only the topmost
    overlay (the numbers then refer to it); --find only filters what is printed, the numbers stay the screen's own."""
    xml = hierarchy()
    items, kind = digest(xml, top)
    taps = [i for i in items[:STATE_LINES] if i[0]]
    note = (f" [{kind}]" if kind else " [no overlay found: whole screen]") if top else ""
    fg = foreground()
    text = render(items, fg, note=note)
    sig = hashlib.md5(text.encode()).hexdigest()
    # a signature from an old session means the caller has not seen that screen, so print it
    same = quiet_if_same and state.get("sig") == sig and time.time() - state.get("sig_at", 0) < 300
    free = [{"label": l, "xy": xy} for t, l, xy in items[:STATE_LINES] if not t and xy]
    state.update(sig=sig, sig_at=time.time(), taps=[{"label": l, "xy": xy} for _, l, xy in taps], free=free)
    save_state(state)
    return "screen unchanged" if same else (render(items, fg, find, note) if find else text)


def refresh(state):
    """Read the screen into the state without printing it (the numbers and labels `tap` resolves against)."""
    snapshot(state, False)
    state["stale"] = False
    save_state(state)


def act_tap(target, nth, state):
    """Tap by number or label; returns what was tapped. Raises SystemExit with the reason when it cannot."""
    if target.isdigit():
        if state.get("stale") or state.get("taps") is None:
            raise SystemExit("the numbers are out of date (quiet action or no snapshot): run `screen`, or tap by label")
        i = int(target) - 1
        taps = state["taps"]
        if not 0 <= i < len(taps):
            raise SystemExit(f"no element {target}, screen has {len(taps)} tappable")
        hit = taps[i]
    else:
        if state.get("stale") or state.get("taps") is None:
            refresh(state)   # a label is looked up on the screen as it is now, not as it was before a quiet action
        # numbered tappables first, then plain visible text (many rows are not marked clickable but take a tap)
        hits = [t for t in state["taps"] + state.get("free", []) if target.lower() in t["label"].lower()]
        if not hits:
            raise SystemExit(f"no label on screen contains '{target}'")
        if nth > len(hits):
            raise SystemExit(f"only {len(hits)} labels contain '{target}'")
        hit = hits[nth - 1]
    adb("shell", "input", "tap", str(hit["xy"][0]), str(hit["xy"][1]))
    return f"tapped {hit['label'][:40]}"


def act_type(text):
    adb("shell", f"input text {shlex.quote(text.replace(' ', '%s'))}")
    return f"typed {len(text)} chars"


def act_key(key):
    code = KEYS.get(key) or (int(key) if key.isdigit() else None)
    if code is None:
        raise SystemExit(f"unknown key, use one of {', '.join(KEYS)} or a keycode number")
    adb("shell", "input", "keyevent", str(code))
    return f"key {key}"


def act_swipe(direction):
    w, h = map(int, re.search(r"(\d+)x(\d+)", adb("shell", "wm", "size")).groups())
    cx, cy = w // 2, h // 2
    dx, dy = {"up": (0, -h * 0.3), "down": (0, h * 0.3), "left": (-w * 0.3, 0), "right": (w * 0.3, 0)}[direction]
    adb("shell", "input", "swipe", str(cx), str(cy), str(int(cx + dx)), str(int(cy + dy)), "300")
    return f"swiped {direction}"


def act_launch(package):
    # debug tools such as LeakCanary add a second launcher entry to the package, so monkey would pick one at random
    out = adb("shell", "cmd", "package", "query-activities", "--brief", "-a", "android.intent.action.MAIN",
              "-c", "android.intent.category.LAUNCHER", package)
    found = re.findall(rf"^\s*({re.escape(package)}/\S+)\s*$", out, re.M)
    comp = next((c for c in found if "leakcanary" not in c.lower()), found[0] if found else None)
    if comp:
        adb("shell", "am", "start", "-a", "android.intent.action.MAIN", "-c", "android.intent.category.LAUNCHER", "-n", comp)
    else:
        adb("shell", "monkey", "-p", package, "-c", "android.intent.category.LAUNCHER", "1")
    return f"launched {package}"


def finish(a, state, said):
    """After an action: let the screen settle, then print the screen, or with --quiet only one line.
    A quiet action leaves the numbers stale, so the next `tap` by number needs a `screen` first."""
    time.sleep(a.wait)
    if a.quiet:
        state["stale"] = True
        save_state(state)
        print(f"{said}, ok")
        return
    print(said)
    print(snapshot(state, True, a.find, a.top))
    state["stale"] = False
    save_state(state)


def cmd_screen(a, state):
    print(snapshot(state, False, a.find, a.top))
    state["stale"] = False
    save_state(state)


def cmd_tap(a, state):
    finish(a, state, act_tap(a.target, a.nth, state))


def cmd_type(a, state):
    finish(a, state, act_type(a.text))


def cmd_key(a, state):
    finish(a, state, act_key(a.key))


def cmd_swipe(a, state):
    finish(a, state, act_swipe(a.direction))


def cmd_launch(a, state):
    a.wait += 1
    finish(a, state, act_launch(a.package))


def cmd_do(a, state):
    """Several steps in one call. Taps must name a label (the numbers cannot be known for later steps),
    and money or destructive labels are refused, because nobody looks at the screen between steps."""
    from keytap.watchdog.navigator import DENY
    done = 0
    for n, step in enumerate(a.steps, 1):
        try:
            verb, *rest = shlex.split(step)
            if verb == "tap" and rest:
                if rest[0].isdigit():
                    raise SystemExit("batch taps must use a label, not a number")
                if DENY.search(rest[0]):
                    raise SystemExit(f"'{rest[0]}' looks like a money or destructive action: do it as a single `tap`")
                said = act_tap(rest[0], int(rest[1]) if len(rest) > 1 else 1, state)
            elif verb == "type" and rest:
                said = act_type(" ".join(rest))
            elif verb == "key" and rest:
                said = act_key(rest[0])
            elif verb == "swipe" and rest and rest[0] in ("up", "down", "left", "right"):
                said = act_swipe(rest[0])
            elif verb == "launch" and rest:
                said = act_launch(rest[0])
            elif verb == "wait" and rest:
                time.sleep(float(rest[0]))
                said = f"waited {rest[0]}s"
            else:
                raise SystemExit("steps are: tap <label> [nth] | type <text> | key <name> | swipe <dir> | launch <pkg> | wait <s>")
        except SystemExit as e:
            print(f"step {n} '{step}' failed after {done} done: {e}")
            time.sleep(a.wait)
            print(snapshot(state, False, a.find, a.top))
            state["stale"] = False
            save_state(state)
            return 1
        done += 1
        time.sleep(a.wait)
        state["stale"] = True   # the next label is looked up on the new screen
    save_state(state)
    if a.quiet:
        print(f"{done} steps ok")
        return
    print(f"{done} steps ok")
    print(snapshot(state, False, a.find, a.top))
    state["stale"] = False
    save_state(state)


def cmd_flow(a, state):
    """Replay a keytap flow (a folder with actions.json) with keytap's own replayer: one call for what
    would otherwise be many steered steps. Refuses a flow that wipes app data unless --allow-clear."""
    import os

    from keytap.runner import engine, expand
    path = os.path.abspath(a.flow)
    root = expand.find_root(path)
    params = dict(p.split("=", 1) for p in a.param)
    if a.package:
        params["package"] = a.package
    try:
        rec = expand.load_recording(path, root=root, params=params)[0]
    except expand.LoadError as e:
        raise SystemExit(f"cannot load the flow: {e}")
    wipes = [i for i, x in enumerate(rec.actions, 1) if x.type == "clear_data"]
    if wipes and not a.allow_clear:
        raise SystemExit(f"this flow wipes the app's data (clear_data at step {wipes[0]}): the login is lost. Run again with --allow-clear if that is intended")
    opts = engine.Options(quiet=True, retries=a.retries, wait=10.0)
    if not engine.pick_serial(opts):
        raise SystemExit("several phones attached: set ANDROID_SERIAL")
    if state.get("mark"):   # the run clears logcat first: keep what `mark` has seen so far
        saved = SAVED_LOG
        saved.parent.mkdir(parents=True, exist_ok=True)
        with open(saved, "a") as f:
            f.write(adb("logcat", "-d", "-v", "time", "-T", state["mark"], timeout=60))
    out = DATA_DIR / "steer" / "flows" / time.strftime("%Y%m%d_%H%M%S")
    package = engine.package_of(rec, params.get("package", ""))
    summary, results = engine.run_recording(lambda: expand.load_recording(path, root=root, params=params)[0],
                                            opts, str(out), 1, package)
    r = results[0]
    name = os.path.basename(path)
    if r["success"]:
        print(f"flow {name} ok: {r['total_steps']} steps, {r['duration_s']}s, {r['warnings']} warnings")
    else:
        f = r["failure"]
        print(f"flow {name} FAILED at step {f['step']} '{f['action']}': {f['cause']}: {str(f.get('detail') or f.get('error', ''))[:160]}")
        print(f"details: {out}/run_01/failure/")
    state["stale"] = True
    save_state(state)
    if not a.quiet:
        print(snapshot(state, False, a.find, a.top))
        state["stale"] = False
        save_state(state)
    return 0 if r["success"] else 1


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
    state["mark"] = adb("shell", "date '+%m-%d %H:%M:%S.000'").strip()
    SAVED_LOG.unlink(missing_ok=True)
    save_state(state)
    print(f"marked {state['mark']}")


def cmd_logs(a, state):
    if not state.get("mark"):
        raise SystemExit("no mark yet: run `mark` before the flow")
    out = adb("logcat", "-d", "-v", "time", "-T", state["mark"], timeout=60)
    if SAVED_LOG.exists():   # lines seen before a `flow` cleared logcat
        out = SAVED_LOG.read_text() + out
    rx = re.compile(a.pattern)
    hits = [l for l in out.splitlines() if rx.search(l)]
    print(f"{len(hits)} matches since {state['mark']}")
    shown = hits[:a.max] if len(hits) <= a.max else hits[:a.max - 1] + hits[-1:]
    for line in shown:
        print(line[:a.width])


def parser():
    p = argparse.ArgumentParser(prog="steer.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, wait=1.0, screen=True):
        s = sub.add_parser(name)
        s.set_defaults(fn=fn)
        if screen:
            s.add_argument("--find", metavar="REGEX", help="print only the lines whose label matches (numbers stay valid)")
            s.add_argument("--top", action="store_true", help="only the topmost overlay (menu, sheet); the numbers then refer to it")
        if wait:
            s.add_argument("--wait", type=float, default=wait, help="seconds to let the screen settle")
            s.add_argument("--quiet", action="store_true", help="print one line, not the screen")
        return s

    add("screen", cmd_screen, wait=0)
    s = add("tap", cmd_tap)
    s.add_argument("target")
    s.add_argument("nth", nargs="?", type=int, default=1)
    add("type", cmd_type).add_argument("text")
    add("key", cmd_key).add_argument("key")
    add("swipe", cmd_swipe).add_argument("direction", choices=["up", "down", "left", "right"])
    add("launch", cmd_launch).add_argument("package")
    s = add("do", cmd_do)
    s.add_argument("steps", nargs="+", help='e.g. "tap Chat" "tap Groups" "key back"')
    s = add("flow", cmd_flow, wait=0)
    s.add_argument("flow", help="folder with actions.json")
    s.add_argument("--param", action="append", default=[], metavar="K=V")
    s.add_argument("--package", default="", help="fills {{package}}")
    s.add_argument("--retries", type=int, default=1)
    s.add_argument("--allow-clear", action="store_true", help="allow a flow that wipes the app's data")
    s.add_argument("--quiet", action="store_true", help="print one line, not the screen")
    s = add("see", cmd_see, wait=0, screen=False)
    s.add_argument("question")
    s.add_argument("--model", default="gemma4:12b")
    add("mark", cmd_mark, wait=0, screen=False)
    s = add("logs", cmd_logs, wait=0, screen=False)
    s.add_argument("pattern")
    s.add_argument("--max", type=int, default=5)
    s.add_argument("--width", type=int, default=160)
    return p


def main():
    a = parser().parse_args()
    return a.fn(a, load_state()) or 0


if __name__ == "__main__":
    sys.exit(main())
