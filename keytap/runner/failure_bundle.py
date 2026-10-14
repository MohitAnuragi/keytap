"""
Failure bundle: what to look at when a replay step fails.

Written to <dir>/: screenshot.png, hierarchy.xml, logcat.txt and failure.json. failure.json holds
the failed step, the error, a cause code with a one-line explanation, and where the app was.

Cause codes (first match wins):
  device_unavailable        phone or uiautomator2 not reachable (infrastructure, not an app bug)
  app_crash                 FATAL EXCEPTION or ANR for the app in the logcat tail
  wrong_app_in_foreground   another app is on screen (launch failed, app closed, a system screen)
  assertion_failed          an assert step did not hold in time; the error has expected and found
  element_not_found         the element is not on the current screen
  action_failed             anything else
"""

import json
import os
import re
import xml.etree.ElementTree as ET

from keytap.device.adb import adb, take_screencap
from keytap.device.elements import parse_elements
from keytap.core.utils import adb_shell

_FOCUS_RE = re.compile(r"u\d+\s+([\w.]+)/([\w.$]+)")
_PNG = b"\x89PNG"
MAX_CLICKABLE = 30   # tappable elements listed in failure.json
MAX_TEXT = 80        # longer texts (chat messages) are left out; they make poor selectors


def _hierarchy(dev) -> str:
    if dev is not None:
        try:
            xml = dev.dump_hierarchy()
            return xml if isinstance(xml, str) else xml.decode(errors="replace")
        except Exception:
            pass
    try:   # plain uiautomator, when u2 is not connected
        out = adb("exec-out", "uiautomator", "dump", "/dev/tty").stdout.decode(errors="replace")
    except Exception:
        return ""
    end = out.find("</hierarchy>")
    return out[:end + len("</hierarchy>")] if end >= 0 else ""


def _screen(xml: str) -> dict:
    """What is on screen: all resource ids and texts, and the tappable elements with the fields
    a replacement selector needs (full resource_id, content_desc, text, index when ambiguous)."""
    ids, texts, clickable = [], [], []
    try:
        for node in ET.fromstring(xml).iter("node"):
            rid = node.get("resource-id", "").split("/")[-1]
            if rid and rid not in ids:
                ids.append(rid)
            txt = node.get("text", "")
            if txt and txt not in texts:
                texts.append(txt)
        for el in parse_elements(xml):
            item = {k: el[k] for k in ("resource_id", "content_desc") if el[k]}
            if el["text"] and len(el["text"]) <= MAX_TEXT:
                item["text"] = el["text"]
            if item:
                if el["index"] is not None:
                    item["index"] = el["index"]
                clickable.append(item)
    except ET.ParseError:
        pass
    return {"ids": ids[:20], "texts": texts[:12], "clickable": clickable[:MAX_CLICKABLE]}


ANR = "ANR in the app"


def _crash(logcat: str, package: str) -> str:
    pkg = re.escape(package) if package else r"[\w.]+"
    if re.search(rf"ANR in {pkg}", logcat):
        return ANR
    if re.search(rf"FATAL EXCEPTION[\s\S]{{0,400}}?Process: {pkg}", logcat):
        return "the app crashed (FATAL EXCEPTION in logcat.txt)"
    return ""


def classify(error: str, package: str, foreground: dict, logcat: str, reachable: bool) -> dict:
    fg = foreground.get("package", "")
    if not reachable or "uiautomator2 unavailable" in error:
        return {"cause": "device_unavailable", "detail": "phone or uiautomator2 not reachable"}
    crash = _crash(logcat, package)
    if crash:
        return {"cause": "app_crash", "detail": crash}
    if package and fg and fg != package:
        return {"cause": "wrong_app_in_foreground",
                "detail": f"{fg} is on screen, expected {package}"}
    if error.startswith("assertion failed"):
        return {"cause": "assertion_failed", "detail": error}
    if error.startswith("element"):
        return {"cause": "element_not_found",
                "detail": f"not on the current screen ({foreground.get('activity') or 'unknown screen'})"}
    return {"cause": "action_failed", "detail": error}


def capture(out_dir: str, step: int, label: str, error: str, dev, serial, package: str = "") -> dict:
    """Collect the bundle after a failed step. Returns the failure.json content."""
    os.makedirs(out_dir, exist_ok=True)
    files = []

    try:
        shot = take_screencap()
    except Exception:
        shot = b""
    if shot.startswith(_PNG):
        with open(os.path.join(out_dir, "screenshot.png"), "wb") as f:
            f.write(shot)
        files.append("screenshot.png")

    xml = _hierarchy(dev)
    if xml:
        with open(os.path.join(out_dir, "hierarchy.xml"), "w") as f:
            f.write(xml)
        files.append("hierarchy.xml")

    try:
        logcat = adb("logcat", "-d", "-t", "300", "-v", "threadtime").stdout.decode(errors="replace")
    except Exception:
        logcat = ""
    if logcat:
        with open(os.path.join(out_dir, "logcat.txt"), "w") as f:
            f.write(logcat)
        files.append("logcat.txt")

    m = _FOCUS_RE.search(adb_shell("dumpsys window | grep mCurrentFocus", serial))
    foreground = {"package": m.group(1), "activity": m.group(2)} if m else {}

    failure = {"step": step, "action": label, "error": error, "package": package}
    failure.update(classify(error, package, foreground, logcat, reachable=bool(shot or logcat or foreground)))
    failure["foreground"] = foreground
    failure["screen"] = _screen(xml)
    failure["files"] = files
    with open(os.path.join(out_dir, "failure.json"), "w") as f:
        json.dump(failure, f, indent=2)
    return failure
