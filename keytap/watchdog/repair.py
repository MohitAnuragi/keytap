"""
Propose a fix for a test whose element was not found. The local model picks, by number, which element
on screen now is the same thing the step wanted; code builds the new selector from that element, replays
the patched copy in a scratch suite, and flags selectors that will break again (times, dates, last
message previews). Nothing is applied: the patched file and a diff go to the report for a human.
"""

import copy
import difflib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

from keytap.core.paths import ROOT
from keytap.watchdog import llm

SYSTEM = """A UI test step could not find its element because the app changed. You get the step's old selector and a numbered list of the elements on screen now.
Pick the number of the element that is the same thing the step wanted, or -1 if none of them is. Details that change over time (a time, a date, a last message) may differ between old and new.
why: one short sentence."""
SCHEMA = {"type": "object", "required": ["choice", "why"],
          "properties": {"choice": {"type": "integer"}, "why": {"type": "string"}}}
FRAGILE = re.compile(r"\d{1,2}:\d{2}|\b(mon|tue|wed|thu|fri|sat|sun)\w*\b|\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\b"
                     r"|\btoday\b|\byesterday\b|\bYou:", re.I)


def propose(cfg, test, failure, out_dir):
    """A dict describing the proposal, or {"skipped": reason}. test is {"name", "path"} of the suite test."""
    path = Path(test["path"], "actions.json")
    raw = json.loads(path.read_text())
    idx = failure["step"] - 1
    acts = raw["actions"]
    if not 0 <= idx < len(acts) or any(a.get("type") == "run" for a in acts[:idx + 1]):
        return {"skipped": "the failing step comes from a flow, so it belongs to another file"}
    old = acts[idx].get("selector")
    if not old:
        return {"skipped": "the failing step has no selector"}
    clickable = failure.get("screen", {}).get("clickable", [])
    if not clickable:
        return {"skipped": "nothing clickable was on screen"}

    view = {"old_selector": llm.clip({k: v for k, v in old.items() if k != "class_name" and v}),
            "elements": [{"n": i, **llm.clip(c)} for i, c in enumerate(clickable)]}
    answer, _ = llm.chat(cfg["model"], SYSTEM, json.dumps(view), SCHEMA)
    choice = answer["choice"]
    if not isinstance(choice, int) or not 0 <= choice < len(clickable):
        return {"skipped": f"the model found no matching element ({answer['why']})"}

    c = clickable[choice]
    new = {**old, "resource_id": c.get("resource_id", ""), "content_desc": c.get("content_desc", ""), "text": c.get("text", "")}
    new.pop("index", None)
    if "index" in c:
        new["index"] = c["index"]
    if {k: new.get(k) for k in ("resource_id", "content_desc", "text", "index")} == {k: old.get(k) for k in ("resource_id", "content_desc", "text", "index")}:
        return {"skipped": "the model picked the selector the step already has"}

    out = Path(out_dir)
    patched = copy.deepcopy(raw)
    patched["actions"][idx]["selector"] = new
    out.mkdir(parents=True, exist_ok=True)
    (out / "actions.patched.json").write_text(json.dumps(patched, indent=2))
    diff = "".join(difflib.unified_diff(json.dumps(raw, indent=2).splitlines(True), json.dumps(patched, indent=2).splitlines(True),
                                        "actions.json", "actions.patched.json"))
    (out / "patch.diff").write_text(diff)

    result = {"step": failure["step"], "why": answer["why"], "old": old, "new": new, "patched": str(out / "actions.patched.json"),
              "target": str(path), "fragile": bool(FRAGILE.search(" ".join(str(new.get(k, "")) for k in ("content_desc", "text"))))}
    result["validation"] = validate(cfg, test, patched, out)
    return result


def validate(cfg, test, patched, out):
    """Replay the patched test 3 times in a scratch copy of its suite. Returns "N/3 passed" or the reason it could not run."""
    suite = Path(test["path"]).parents[1]   # <suite>/tests/<name>
    scratch = out / "suite"
    shutil.rmtree(scratch, ignore_errors=True)
    scratch.mkdir(parents=True)
    shutil.copy(suite / "suite.json", scratch / "suite.json")
    if (suite / "flows").is_dir():
        shutil.copytree(suite / "flows", scratch / "flows")
    (scratch / "tests" / test["name"]).mkdir(parents=True)
    (scratch / "tests" / test["name"] / "actions.json").write_text(json.dumps(patched, indent=2))
    cmd = [sys.executable, "suite.py", "run", str(scratch), test["name"], "--exact", "--repeat", "3", "--quiet",
           "--out", str(out / "validation")]
    if cfg["serial"]:
        cmd += ["--serial", cfg["serial"]]
    try:
        subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=cfg["test_timeout_min"] * 60)
        t = json.loads((out / "validation" / "summary.json").read_text())["tests"][0]
    except (subprocess.TimeoutExpired, OSError, ValueError, KeyError, IndexError) as e:
        return f"could not run: {e}"
    return f"{t['passed']}/{t['runs']} passed"
