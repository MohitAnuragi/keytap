"""
Goal-driven navigator: the local model looks at the screen and picks ONE action from a fixed list per
step; code validates and runs it. The model's "done" is only a claim: the caller's verify() reads the
phone to decide success.

Safety: a tap must name a clickable element on screen. Policy "dialogs" also needs its text on
TAP_WORDS (dialog buttons); policy "social" allows any element except money words (DENY). The text to
type is passed in by the caller, never chosen by the model.
"""

import json
import re
import time
import xml.etree.ElementTree as ET

from keytap.watchdog import llm

ACTIONS = ["tap", "focus_input", "type_message", "back", "home", "open_app", "wait", "swipe_up", "swipe_down", "done", "give_up"]
SOCIAL_ONLY = ("swipe_up", "swipe_down")   # scrolling is only for the wide "social" policy, never the dialogs one
TAP_WORDS = {"allow", "while using the app", "only this time", "don't allow", "deny", "not now", "later",
             "skip", "ok", "got it", "close", "close app", "wait", "dismiss", "no thanks", "cancel"}
DENY = re.compile(r"pay|transfer|send money|top ?up|bayar|kirim uang|confirm|purchase|buy|checkout|withdraw|cash|"
                  r"bill|tagihan|pulsa|qris|scan|invest|loan|pinjam|delete|remove|leave|block|report|log ?out|sign ?out", re.I)
BLOCKERS = ("anr detected", "isn't responding", "keeps stopping", "not responding", "has stopped")
NAV_BAR_IDS = ("com.android.systemui:id/back", "com.android.systemui:id/home", "com.android.systemui:id/recent_apps")

SYSTEM = """You steer an Android phone toward a goal, one action per reply. You get the goal, the screen and your recent steps.
Actions:
- tap: press an element. target must be copied exactly from a clickable text or content_desc on the screen.
- focus_input: tap the text input field (screen.input_fields lists the ones on screen; they have no label so tap cannot reach them).
- type_message: type the prepared message into the focused input field (use focus_input first).
- back: press the Back key (closes dialogs, shades, menus, goes up one screen).
- home: press the Home key.
- open_app: launch the goal app.
- swipe_up: scroll the content up, to see what is below (only when the policy allows it).
- swipe_down: scroll the content down, to see what is above, or pull a sheet down.
- wait: the screen is still loading.
- done: the goal is reached.
- give_up: no action here can reach the goal.
If a screenshot is attached, look at it first: a dialog, sheet or popup covering the app is easy to see there.
Rules: look at foreground.package first. An error, crash, ANR or permission dialog or screen must be closed before anything else (Close, Wait, OK, Allow). Never tap anything about money. If a step was rejected or changed nothing, do not repeat it: try something different."""

SCHEMA = {"type": "object", "required": ["action", "target", "reason"],
          "properties": {"action": {"enum": ACTIONS}, "target": {"type": "string"}, "reason": {"type": "string"}}}


def observe(dev, serial):
    from keytap.device import lookup
    from keytap.runner.failure_bundle import _hierarchy, _screen
    pkg, activity = lookup.foreground(serial)
    xml = _hierarchy(dev)
    s = _screen(xml)
    clickable = [c for c in s["clickable"] if c.get("resource_id") not in NAV_BAR_IDS]
    try:   # input fields often have no text, description or id, so the element list cannot show them
        inputs = [{"focused": n.get("focused") == "true", "text": n.get("text", "")}
                  for n in ET.fromstring(xml).iter("node") if n.get("class") == "android.widget.EditText"]
    except ET.ParseError:
        inputs = []
    top_texts, top_clickable = _top_layer(xml)
    return {"foreground": {"package": pkg, "activity": activity}, "texts": top_texts or s["texts"],
            "clickable": top_clickable or clickable, "input_fields": inputs, "xml": xml}


def _top_layer(xml, limit_texts=25, limit_clickable=30):
    """What the screen shows, topmost first. The dump lists views bottom to top, so a dialog or sheet comes
    last: reading the first 12 texts sees only the screen underneath it. A text counts as tappable when it or
    a parent is clickable (a button's label is often a plain text view inside the clickable container)."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return [], []
    parent = {c: p for p in root.iter() for c in p}

    def tappable(n):
        while n is not None:
            if n.get("clickable") == "true":
                return True
            n = parent.get(n)
        return False

    texts, clickable, seen = [], [], set()
    for n in reversed(list(root.iter("node"))):
        label = (n.get("text") or n.get("content-desc") or "").strip()
        if not label or len(label) > 80 or n.get("package") == "com.android.systemui":
            continue
        if label not in texts and len(texts) < limit_texts:
            texts.append(label)
        if tappable(n) and (n.get("text"), n.get("content-desc")) not in seen and len(clickable) < limit_clickable:
            seen.add((n.get("text"), n.get("content-desc")))
            # label only: find_element tries resource_id first, and buttons often share one generic id,
            # so including it taps the first such button on the screen, not the one that was named
            item = {k: v for k, v in (("text", n.get("text")), ("content_desc", n.get("content-desc"))) if v}
            clickable.append(item)
    return texts, clickable


def prompt_view(obs):
    return llm.clip({k: obs[k] for k in ("foreground", "texts", "clickable", "input_fields")})


def blocked(obs):
    """An error screen or dialog is on top (the app's own ANR screen, a system 'isn't responding')."""
    return any(b in json.dumps(prompt_view(obs)).lower() for b in BLOCKERS)


def in_app(pkg):
    """verify(): the goal app is focused and nothing like an ANR screen is on top."""
    return lambda dev, obs: obs["foreground"]["package"] == pkg and not blocked(obs)


def text_posted(marker):
    """verify(): the marker shows in a view that is not an input field, so the message was sent, not just typed."""
    def check(dev, obs):
        try:
            for n in ET.fromstring(obs["xml"]).iter("node"):
                if n.get("class") != "android.widget.EditText" and marker in (n.get("text", "") + n.get("content-desc", "")):
                    return True
        except ET.ParseError:
            pass
        return False
    return check


def _label(item):
    return (item.get("text") or item.get("content_desc") or "").strip()


def execute(action, dev, goal_pkg, clickable, policy, message):
    """Run one validated action. Returns "" or the reason it was rejected."""
    from keytap.device import actions, lookup
    from keytap.recorder.recording import ElementSelector
    kind, target = action["action"], action["target"].strip().lower()
    if kind == "tap":
        item = next((c for c in clickable if _label(c).lower() == target), None)
        if item is None:
            return f"'{action['target']}' is not a clickable element on screen"
        if policy == "dialogs" and target not in TAP_WORDS:
            return f"'{action['target']}' is not a dialog button I may press"
        if policy == "social" and (DENY.search(_label(item)) or not target):
            return f"'{action['target']}' is blocked by the money/destructive word list"
        el = lookup.find_element(dev, ElementSelector(**{k: item[k] for k in ("resource_id", "content_desc", "text", "index") if k in item}))
        if el is None:
            return "element vanished"
        el.click()
    elif kind == "focus_input":
        field = dev(className="android.widget.EditText")
        if not field.exists:
            return "there is no input field on screen"
        field.click()
    elif kind == "type_message":
        if not message:
            return "there is no message to type"
        actions.input_text(message)
    elif kind in SOCIAL_ONLY:
        if policy != "social":
            return "scrolling is not allowed here"
        w, h = dev.window_size()
        y0, y1 = (0.75, 0.3) if kind == "swipe_up" else (0.3, 0.75)
        dev.swipe(w // 2, int(h * y0), w // 2, int(h * y1), 0.3)
    elif kind == "back":
        actions.keyevent(4, "BACK")
    elif kind == "home":
        actions.keyevent(3, "HOME")
    elif kind == "open_app":
        actions.launch(goal_pkg)
    elif kind == "wait":
        pass
    time.sleep(2)
    return ""


def screenshot_b64(dev, width=540):
    """The screen as a small JPEG for a vision model (about 60 KB, 700 prompt tokens)."""
    import base64
    import io
    img = dev.screenshot().convert("RGB")
    img.thumbnail((width, 1200))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=70)
    return base64.b64encode(buf.getvalue()).decode()


def run(model, dev, serial, goal, goal_pkg, verify, policy="dialogs", message=None, max_steps=8, vision=False):
    """Steer toward the goal. Returns {"steps", "model_done", "verified", "rejected", "seconds", "llm_s", "prompt_tokens"}."""
    t0, history, steps, rejected, model_done, llm_s, tokens = time.time(), [], [], 0, False, 0.0, []
    obs = observe(dev, serial)
    for n in range(1, max_steps + 1):
        prompt = json.dumps({"goal": goal, "goal_package": goal_pkg, "screen": prompt_view(obs), "recent_steps": history[-4:]})
        t = time.time()
        try:
            answer, raw = llm.chat(model, SYSTEM, prompt, SCHEMA, images=[screenshot_b64(dev)] if vision else None)
        except Exception as e:
            steps.append({"n": n, "error": str(e)[:120]})
            break
        llm_s += time.time() - t
        tokens.append(raw.get("prompt_eval_count"))
        step = {"n": n, "fg": obs["foreground"]["package"], **{k: answer[k] for k in ("action", "target", "reason")}}
        if answer["action"] in ("done", "give_up"):
            model_done = answer["action"] == "done"
            steps.append(step)
            break
        last = history[-1] if history else {}
        if (answer["action"], answer["target"]) == (last.get("action"), last.get("target")):
            why = "you just did exactly this, do something different"
        else:
            why = execute(answer, dev, goal_pkg, obs["clickable"], policy, message) if answer["action"] in ACTIONS else "unknown action"
        step["rejected"] = why
        rejected += bool(why)
        steps.append(step)
        after = observe(dev, serial)
        result = why or ("ok" if prompt_view(after) != prompt_view(obs) else "ok but the screen did not change")
        history.append({"action": answer["action"], "target": answer["target"], "result": result})
        obs = after
    return {"steps": steps, "model_done": model_done, "verified": verify(dev, observe(dev, serial)), "rejected": rejected,
            "seconds": round(time.time() - t0, 1), "llm_s": round(llm_s, 1), "prompt_tokens": tokens}
