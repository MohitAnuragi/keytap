"""
Nightly supervisor: runs suite tests unattended, recovers from failures with fixed rules, and writes a
morning report. No LLM yet; every decision is logged to audit.jsonl so a model can take over the
"what next" step later.

nightly.json:
  {"suites": ["suites/gopay_smoke"],   paths, relative to the keytap folder
   "serial": null,                     adb serial, default: the only attached phone
   "max_hours": 7, "min_battery": 20,  stop the night at these limits
   "max_attempts": 3,                  runs of one failing test, the first included
   "max_infra": 3,                     phone problems in a row before the night is aborted
   "test_timeout_min": 30,
   "allow_steps": [],                  clear_data, permission, dev_option: skipped unless listed here
   "model": null}                      Ollama model for failure triage and crash summaries, e.g. "gemma4:12b"

Results: data/nightly/<time>/{report.md, results.json, audit.jsonl, <suite>/<test>/attempt_NN/}.
"""

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from keytap.core.paths import DATA_DIR, ROOT
from keytap.runner import engine
from keytap.watchdog import llm

DEFAULTS = {"suites": [], "serial": None, "max_hours": 7, "min_battery": 20, "max_attempts": 3,
            "max_infra": 3, "test_timeout_min": 30, "allow_steps": [], "model": None}
UNSAFE_STEPS = ("clear_data", "permission", "dev_option")   # change the phone or wipe the account
INFRA = engine.INFRA_CAUSES + ("runner_error",)


def load_config(path):
    with open(path) as f:
        cfg = {**DEFAULTS, **json.load(f)}
    if not cfg["suites"]:
        raise ValueError(f'{path} needs "suites"')
    return cfg


def crash_signature(logcat_path):
    """(signature, excerpt) of the first crash in a logcat.txt: exception class and top frame, so the
    same crash in two tests groups together. ("", "") when there is none."""
    try:
        text = Path(logcat_path).read_text(errors="replace")
    except OSError:
        return "", ""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if "FATAL EXCEPTION" not in line:
            continue
        body = [re.sub(r"^.*?AndroidRuntime:\s?", "", l).rstrip()
                for l in lines[i + 1:i + 60] if "AndroidRuntime" in l]
        exc = next((b for b in body if b and not b.startswith("Process:")), "")
        frame = next((b.strip() for b in body if b.strip().startswith("at ")), "")
        return f"{exc.split(':')[0]} {frame}".strip(), "\n".join(body[:10])
    m = re.search(r"ANR in ([\w.]+)", text)
    return (f"ANR in {m.group(1)}", m.group(0)) if m else ("", "")


def preflight(root, suite, test, cfg):
    """Why this test must not run unattended, or "" when it may."""
    from keytap.runner.expand import LoadError, load_recording
    try:
        rec, header = load_recording(test["path"], root)
    except LoadError as e:
        return f"cannot load: {e}"
    allowed = set(cfg["allow_steps"])
    used = {a.type for a in rec.actions} & (set(UNSAFE_STEPS) - allowed)
    login = header.get("login") or suite["login"].get("default", "once")
    if login == "always" and "clear_data" not in allowed:
        used.add("login: always (wipes app data)")
    return ", ".join(sorted(used))


def reusable_needs(suite, test_name, fresh):
    """The prerequisites of this test that may be skipped: it is in "reuse_ok" (it only reads their state)
    and each one passed earlier tonight with nothing since that could have changed what it left behind."""
    if test_name not in suite.get("reuse_ok", []):
        return []
    return [n for n in suite.get("needs", {}).get(test_name, []) if n in fresh]


def fresh_after(suite, test_name, status, fresh):
    """Which prerequisites' state is still fresh once this test has finished. Anything that is not a seed
    and not in "reuse_ok" (a login, a test that consumes invitations) or that failed starts the chain over."""
    needs = suite.get("needs", {})
    if status not in ("pass", "recovered"):
        return set()
    if test_name in {n for v in needs.values() for n in v}:
        return {test_name}
    if test_name in suite.get("reuse_ok", []):
        return fresh | set(needs.get(test_name, []))   # a retry runs with its needs, so they are fresh again too
    return set()


def run_test(suite_dir, name, out, cfg, skip=()):
    """One run of one test through suite.py. Returns the test's entry from its summary.json.
    skip: prerequisite tests suite.py must not add first (see reusable_needs)."""
    cmd = [sys.executable, "suite.py", "run", suite_dir, name, "--exact", "--repeat", "1", "--quiet", "--out", out]
    if skip:
        cmd += ["--skip-needs", ",".join(skip)]
    if cfg["serial"]:
        cmd += ["--serial", cfg["serial"]]
    fail = {"status": "fail", "step": 0, "action": "", "failure": None, "screenshot": None}
    try:
        p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=cfg["test_timeout_min"] * 60)
    except subprocess.TimeoutExpired:
        return {**fail, "cause": "timeout", "detail": f"no result after {cfg['test_timeout_min']} min"}
    path = os.path.join(out, "summary.json")
    if not os.path.isfile(path):
        return {**fail, "cause": "runner_error", "detail": (p.stdout + p.stderr).strip()[-300:]}
    with open(path) as f:
        tests = json.load(f)["tests"]
    # suite.py runs the prerequisites (needs) first, so the requested test is not always tests[0]
    mine = next((t for t in tests if t["name"] == name), tests[-1])
    bad = next((t for t in tests if t is not mine and t["status"] == "fail"), None)
    if bad:   # a failed prerequisite is the root cause, and the test after it ran on missing data
        return {**bad, "detail": f'prerequisite {bad["name"]} failed: {bad.get("detail", "")}'[:300]}
    return mine


def build_id(package):
    from keytap.device.adb import adb
    out = adb("shell", "dumpsys", "package", package).stdout.decode(errors="replace")
    name, code = re.search(r"versionName=(\S+)", out), re.search(r"versionCode=(\d+)", out)
    return f"{name.group(1) if name else '?'} ({code.group(1) if code else '?'})"


def run_night(cfg, dry_run=False):
    from keytap.cli.suite import list_items, load_suite
    from keytap.core import config
    from keytap.device import actions

    suites = []
    for p in cfg["suites"]:
        root = os.path.abspath(os.path.join(ROOT, p))
        suite = load_suite(root)
        suites.append((root, suite, list_items(root, suite["tests"])))

    if dry_run:
        for root, suite, tests in suites:
            print(f"{os.path.basename(root)}  ({suite['package']})")
            fresh = set()   # assumes every test passes
            for t in tests:
                why = preflight(root, suite, t, cfg)
                reuse = reusable_needs(suite, t["name"], fresh)
                print(f"  {t['name']:<32} " + (f"SKIP: {why}" if why else
                      "run" + (f" (reuses {', '.join(reuse)})" if reuse else "")))
                if not why:
                    fresh = fresh_after(suite, t["name"], "pass", fresh)
        return 0

    night = DATA_DIR / "nightly" / time.strftime("%Y%m%d_%H%M%S")
    night.mkdir(parents=True)
    log = open(night / "audit.jsonl", "a")

    def audit(event, **kw):
        log.write(json.dumps({"t": time.strftime("%H:%M:%S"), "event": event, **kw}) + "\n")
        log.flush()

    opts = engine.Options(serial=cfg["serial"])
    if not engine.pick_serial(opts):
        return 2
    config.ARGS.serial = opts.serial
    cfg = {**cfg, "serial": opts.serial}
    actions.set_sync(True)
    started = time.time()
    deadline = started + cfg["max_hours"] * 3600
    results, builds, abort, infra_streak = [], {}, "", 0
    audit("start", serial=opts.serial, suites=cfg["suites"])

    for root, suite, tests in suites:
        sname, pkg = os.path.basename(root), suite["package"]
        builds[sname] = build_id(pkg)
        audit("build", suite=sname, build=builds[sname])
        fresh = set()   # seed tests whose state is still fresh, see reusable_needs
        for test in tests:
            r = {"suite": sname, "test": test["name"], "status": "not_run", "cause": "", "detail": "", "attempts": []}
            results.append(r)
            if abort:
                continue
            why = preflight(root, suite, test, cfg)
            if why:
                r.update(status="skipped_policy", detail=why)
                audit("skip", suite=sname, test=test["name"], why=why)
                continue
            for n in range(1, cfg["max_attempts"] + 1):
                if time.time() > deadline:
                    abort = f"time budget of {cfg['max_hours']} h used up"
                    break
                health = engine.device_ready(opts.serial)
                if health is not None and health["battery"] is not None and health["battery"] < cfg["min_battery"]:
                    abort = f"battery {health['battery']}% is below {cfg['min_battery']}%"
                    break
                if health is None:
                    res = {"status": "fail", "cause": "device_unavailable", "detail": "no device online",
                           "step": 0, "action": "", "failure": None, "screenshot": None}
                else:
                    out = str(night / sname / test["name"] / f"attempt_{n:02d}")
                    skip = reusable_needs(suite, test["name"], fresh) if n == 1 else []   # a retry reseeds
                    if skip:
                        audit("reuse", suite=sname, test=test["name"], skipped_needs=skip)
                    res = run_test(root, test["name"], out, cfg, skip)
                    if res["failure"]:
                        res["failure"] = os.path.join(out, res["failure"])
                    if res["screenshot"]:
                        res["screenshot"] = os.path.join(out, res["screenshot"])
                att = {"n": n, **{k: res[k] for k in ("status", "cause", "detail", "step", "action", "failure", "screenshot")}}
                if res["cause"] == "app_crash" and res["failure"]:
                    att["crash"], att["crash_excerpt"] = crash_signature(os.path.join(res["failure"], "logcat.txt"))
                r["attempts"].append(att)
                audit("attempt", suite=sname, test=test["name"], **att)
                if res["status"] == "pass":
                    break
                infra = res["cause"] in INFRA
                infra_streak = infra_streak + 1 if infra else 0
                if infra_streak >= cfg["max_infra"]:
                    abort = f"{infra_streak} phone problems in a row ({res['cause']})"
                    break
                prev = r["attempts"][-2] if n > 1 else None
                if prev and not infra and all(prev[k] == att[k] for k in ("cause", "step", "action")):
                    audit("stop_identical", suite=sname, test=test["name"], attempts=n)
                    break   # the same failure twice: more attempts only burn the night
                if n < cfg["max_attempts"]:
                    recover(audit, cfg, pkg, att, sname, test["name"])
            if r["attempts"]:
                last = r["attempts"][-1]
                r["status"] = "pass" if last["status"] == "pass" else "fail"
                if r["status"] == "pass" and len(r["attempts"]) > 1:
                    r["status"] = "recovered"
                if r["status"] == "fail":
                    r.update(cause=last["cause"], detail=last["detail"])
                    if cfg["model"] and last["failure"]:
                        r["triage"] = with_llm(audit, f"triage {sname}/{test['name']}", lambda: llm.triage(
                            cfg["model"], json.loads(Path(last["failure"], "failure.json").read_text())))
                    if cfg["model"] and last["cause"] == "element_not_found" and last["failure"]:
                        r["proposal"] = propose_fix(audit, cfg, test, sname, night, last)
            if r["attempts"]:
                fresh = fresh_after(suite, test["name"], r["status"], fresh)
            if abort:
                audit("abort", why=abort)

    crashes = {a["crash"]: a["crash_excerpt"] for r in results for a in r["attempts"] if a.get("crash")}
    crash_summaries = {}
    if cfg["model"]:
        for sig, excerpt in crashes.items():
            crash_summaries[sig] = with_llm(audit, f"crash {sig}", lambda: llm.summarize_crash(cfg["model"], excerpt))

    duration = round(time.time() - started)
    summary = {"started": time.strftime("%Y-%m-%d %H:%M", time.localtime(started)), "duration_s": duration,
               "serial": opts.serial or "", "builds": builds, "abort": abort, "results": results,
               "crash_summaries": crash_summaries}
    (night / "results.json").write_text(json.dumps(summary, indent=2))
    (night / "report.md").write_text(render_report(summary))
    audit("end", duration_s=duration, abort=abort)
    counts = {s: sum(r["status"] == s for r in results) for s in ("pass", "recovered", "fail", "skipped_policy", "not_run")}
    print(" ".join(f"{k}={v}" for k, v in counts.items()) + (f"\nABORTED: {abort}" if abort else ""))
    print(f"report: {night / 'report.md'}")
    return 2 if abort else 1 if counts["fail"] else 0


def with_llm(audit, what, call):
    """One model call. A model that is down or answers badly must never stop the night. Returns the
    answer, or None."""
    t = time.time()
    try:
        answer, raw = call()
    except Exception as e:
        audit("llm_error", what=what, error=str(e)[:200])
        return None
    audit("llm", what=what, seconds=round(time.time() - t, 1), tokens=raw.get("eval_count"), answer=answer)
    return answer


def propose_fix(audit, cfg, test, sname, night, last):
    """A fix proposal for an element that was not found, or None when the model or the replay broke."""
    from keytap.watchdog import repair
    t = time.time()
    try:
        failure = json.loads(Path(last["failure"], "failure.json").read_text())
        p = repair.propose(cfg, test, failure, str(night / sname / test["name"] / "proposal"))
    except Exception as e:
        audit("proposal_error", suite=sname, test=test["name"], error=str(e)[:200])
        return None
    audit("proposal", suite=sname, test=test["name"], seconds=round(time.time() - t, 1), **{k: p[k] for k in p if k in ("skipped", "validation", "fragile", "why")})
    return p


NAVIGABLE = ("element_not_found", "wrong_app_in_foreground", "action_failed")
NAVIGATE_GOAL = ("Get {pkg} into the foreground with nothing on top of it. Close any error, ANR, permission or info screen or "
                 "dialog (Close, Wait, OK, Allow). Do not tap anything else.")


def recover(audit, cfg, package, att, suite, test):
    """Between two attempts. Phone problems get a pause. A screen problem goes to the local model, which
    tries to get the app back in front (closing an ANR or permission dialog). An app restart is the fallback."""
    from keytap.device import actions
    infra = att["cause"] in INFRA
    navigate = bool(cfg["model"]) and att["cause"] in NAVIGABLE
    audit("recover", suite=suite, test=test, action="wait" if infra else "navigate" if navigate else "restart_app")
    if infra:
        time.sleep(30)
        return
    if navigate:
        try:
            import uiautomator2 as u2
            from keytap.watchdog import navigator
            dev = u2.connect(cfg["serial"]) if cfg["serial"] else u2.connect()
            res = navigator.run(cfg["model"], dev, cfg["serial"], NAVIGATE_GOAL.format(pkg=package), package,
                                navigator.in_app(package), max_steps=6)
            audit("navigate", suite=suite, test=test, verified=res["verified"], seconds=res["seconds"],
                  steps=[f"{s.get('action')}({s.get('target', '')[:30]})" for s in res["steps"]])
            if res["verified"]:
                return
        except Exception as e:
            audit("recover_error", action="navigate", error=str(e)[:200])
    try:
        actions.force_stop(package)
        actions.launch(package)
        time.sleep(3)
    except Exception as e:
        audit("recover_error", action="restart_app", error=str(e)[:200])


def render_report(s):
    rs = s["results"]
    by = lambda status: [r for r in rs if r["status"] == status]
    L = [f"# Nightly run {s['started']}", "",
         f"{s['duration_s'] // 60} min on {s['serial'] or 'the attached phone'}. "
         + " ".join(f"{k}: {v}." for k, v in s["builds"].items()), ""]
    if s["abort"]:
        L += [f"**Night aborted: {s['abort']}**", ""]
    L += ["| status | count |", "|---|---|"]
    L += [f"| {st} | {len(by(st))} |" for st in ("pass", "recovered", "fail", "skipped_policy", "not_run")]

    if by("fail"):
        L += ["", "## Failed"]
        for r in by("fail"):
            a = r["attempts"][-1]
            L += [f"- **{r['suite']}/{r['test']}**: {r['cause']}: {r['detail']}",
                  f"  - {len(r['attempts'])} attempt(s), step {a['step']} {a['action']}".rstrip()]
            t = r.get("triage")
            L += [f"  - model says: {t['category']}: {t['summary']}"] if t else []
            p = r.get("proposal")
            if p and p.get("skipped"):
                L += [f"  - no fix proposed: {p['skipped']}"]
            elif p:
                L += [f"  - **proposed fix** (not applied): step {p['step']} selector, {p['why']}",
                      f"    - old: `{json.dumps({k: v for k, v in p['old'].items() if v and k != 'class_name'})}`",
                      f"    - new: `{json.dumps({k: v for k, v in p['new'].items() if v and k != 'class_name'})}`",
                      f"    - replayed 3 times with the fix: {p['validation']}"]
                L += ["    - **fragile**: the new selector contains a time, date or message preview, so it will break again; a stable selector is needed"] if p["fragile"] else []
                L += [f"    - to apply: `cp {p['patched']} {p['target']}`"]
            L += [f"  - bundle: {a['failure']}"] if a["failure"] else []
            L += [f"  - screenshot: {a['screenshot']}"] if a["screenshot"] else []

    crashes = {}
    for r in rs:
        for a in r["attempts"]:
            if a.get("crash"):
                c = crashes.setdefault(a["crash"], {"tests": set(), "excerpt": a["crash_excerpt"], "n": 0})
                c["tests"].add(f"{r['suite']}/{r['test']}")
                c["n"] += 1
    if crashes:
        L += ["", "## Crashes (grouped by exception and top frame)"]
        for sig, c in sorted(crashes.items(), key=lambda kv: -kv[1]["n"]):
            L += [f"- **{sig}** x{c['n']} in {', '.join(sorted(c['tests']))}"]
            L += [f"  - model says: {s['crash_summaries'][sig]}"] if s["crash_summaries"].get(sig) else []
            L += ["  ```", *("  " + l for l in c["excerpt"].splitlines()), "  ```"]

    if by("recovered"):
        L += ["", "## Recovered (failed, then passed on a later attempt: flaky or a one-off)"]
        for r in by("recovered"):
            first = r["attempts"][0]
            L += [f"- {r['suite']}/{r['test']}: attempt 1 {first['cause']}: {first['detail']} (passed on attempt {len(r['attempts'])})"]
    if by("skipped_policy"):
        L += ["", "## Skipped (unsafe to run unattended)"]
        L += [f"- {r['suite']}/{r['test']}: {r['detail']}" for r in by("skipped_policy")]
    if by("not_run"):
        L += ["", "## Not run (night aborted)"]
        L += [f"- {r['suite']}/{r['test']}" for r in by("not_run")]
    return "\n".join(L) + "\n"
