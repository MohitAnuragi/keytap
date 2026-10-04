#!/opt/homebrew/bin/python3
"""
Test suites made of recordings: reusable flows (login), tests with assertions, one command to run
them all, results in folders. A suite is a folder you commit next to the app.

  ./suite.py init <dir> --package com.example.app
  ./suite.py add <dir> <recording> --as test|flow
  ./suite.py check <dir>
  ./suite.py run <dir> [test ...] [--tag smoke] [--login always|once|never]
  ./suite.py show <dir> [run]
  ./suite.py checks                   the assertions you can use
"""

import argparse
import glob
import json
import os
import re
import shutil
import sys
import time
import xml.etree.ElementTree as ET

from keytap.runner import engine

EPILOG = """suite folder:
  suite.json                 package, login, before_each, tests, defaults
  flows/<name>/actions.json  reusable pieces; a test includes one with {"type": "run", "flow": "flows/login"}
  tests/<name>/actions.json  one test per folder; header keys: tags, description, login, login_params, before_each
  .runs/<time>/              results of each run (gitignored): summary.json, junit.xml, failures/, one folder per test

suite.json:
  {"package": "com.example.app",
   "login":   {"flow": "flows/login", "default": "once", "check": {"content_desc": "Home"}},
   "before_each": "restart",                 restart (force-stop and launch) or none
   "tests": "tests/*",                       a glob (alphabetical) or a list (run in that order)
   "defaults": {"wait": 10, "retries": 1}}   same as the replay.py flags

login policy, per test (header "login") or suite default:
  once     log in only when the phone is not logged in; the session is kept between tests.
           "check" is a selector that is visible only when logged in; without it the runner
           trusts its own record of the login. It waits up to "check_timeout" seconds (default 20,
           a cold start) for it before deciding the phone is logged out
  always   wipe the app's data, then log in before this test
  never    no login (tests of the logged-out screens)

results (<dir>/.runs/<time>/):
  summary.json    every test: status, cause, detail, failure folder, screenshot
  junit.xml       for CI; failed tests carry the screenshot as an attachment
  failures/       the screenshot of each failed test, <test>.png
  <test>/         summary.json and run_NN/ (result.json, replay.log, failure/), like replay.py

exit code:  0 all passed   1 a test failed or was flaky   2 phone, uiautomator2 or a file was the problem
"""

SECRET_WORDS = re.compile(r"password|passwd|passcode|\bpin\b|otp|secret", re.I)
SELECTOR_KEYS = ("resource_id", "content_desc", "text", "index")


class SuiteError(Exception):
    pass


# ── Reading a suite ───────────────────────────────────────────────────────────

def _slug(name):
    return re.sub(r"[^\w-]+", "_", name).strip("_") or "item"


def _read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        raise SuiteError(f"cannot read {path}: {e}")


def load_suite(root):
    path = os.path.join(root, "suite.json")
    if not os.path.isfile(path):
        raise SuiteError(f"{root} has no suite.json (create one with: ./suite.py init {root} --package <app>)")
    data = _read_json(path)
    if not data.get("package"):
        raise SuiteError('suite.json needs a "package"')
    suite = {"tests": "tests/*", "before_each": "restart", "defaults": {}, "login": {}, **data}
    if suite["before_each"] not in ("restart", "none"):
        raise SuiteError('suite.json "before_each" must be "restart" or "none"')
    login = suite["login"]
    if login and login.get("default", "once") not in ("once", "always", "never"):
        raise SuiteError('suite.json login "default" must be once, always or never')
    return suite


def list_items(root, spec):
    """Folders with an actions.json: a glob (alphabetical) or a list (in that order)."""
    paths = [os.path.join(root, p) for p in spec] if isinstance(spec, list) else sorted(glob.glob(os.path.join(root, spec)))
    return [{"name": os.path.basename(p), "path": p} for p in paths
            if os.path.isfile(os.path.join(p, "actions.json"))]


def list_flows(root):
    return list_items(root, "flows/*")


# ── Lint ──────────────────────────────────────────────────────────────────────

def lint(data, kind):
    """Warnings for a test or flow (the raw actions.json content)."""
    warns, acts = [], data.get("actions", [])
    for i, a in enumerate(acts, 1):
        t, sel = a.get("type"), a.get("selector") or {}
        if t == "tap":
            warns.append(f"step {i}: coordinate tap breaks on another screen size; use an element_tap "
                         "(./replay.py --dump shows the id)")
        if t in ("element_tap", "scroll_to", "assert") and sel.get("text") \
                and not (sel.get("resource_id") or sel.get("content_desc")) and re.search(r"\d", sel["text"]):
            warns.append(f"step {i}: text selector {sel['text']!r} has digits, it may change between runs")
        if t == "input_text" and i > 1 and "{{" not in (a.get("text") or ""):
            prev = acts[i - 2].get("selector") or {}
            if SECRET_WORDS.search(" ".join(str(prev.get(k, "")) for k in SELECTOR_KEYS[:3])):
                warns.append(f"step {i}: looks like a secret typed in plain text; use {{{{env:NAME}}}}")
    if kind == "test" and not any(a.get("type") == "assert" for a in acts):
        warns.append("no assert step: a pass only proves every step ran, not that the result is right")
    return warns


# ── init / add / check / checks ───────────────────────────────────────────────

def cmd_init(args):
    root = os.path.abspath(args.dir)
    if os.path.exists(os.path.join(root, "suite.json")):
        print(f"{root}/suite.json already exists")
        return 2
    for sub in ("flows", "tests"):
        os.makedirs(os.path.join(root, sub), exist_ok=True)
    suite = {"package": args.package or "com.example.app", "before_each": "restart",
             "tests": "tests/*", "defaults": {"wait": 10, "retries": 1}}
    with open(os.path.join(root, "suite.json"), "w") as f:
        json.dump(suite, f, indent=2)
    with open(os.path.join(root, ".gitignore"), "a") as f:
        f.write(".runs/\n")
    print(f"created {root}")
    if not args.package:
        print('set "package" in suite.json (or re-run with --package)')
    print("next: ./suite.py add <dir> <recording> --as flow   (a login flow), then --as test")
    return 0


def cmd_add(args):
    root = os.path.abspath(args.dir)
    try:
        load_suite(root)
    except SuiteError as e:
        print(e)
        return 2
    src = engine.resolve_recording(args.recording)
    if src is None:
        return 2
    actions_json = os.path.join(src, "actions.json") if os.path.isdir(src) else src
    data = _read_json(actions_json)
    name = _slug(args.name or data.get("name", "item"))
    dest = os.path.join(root, "tests" if args.kind == "test" else "flows", name)
    if os.path.exists(dest):
        print(f"{dest} already exists, pass --name to pick another name")
        return 2
    data.pop("video_path", None)
    data["name"] = name
    if args.kind == "test":
        data.setdefault("tags", [])
        data.setdefault("description", "")
    os.makedirs(dest)
    with open(os.path.join(dest, "actions.json"), "w") as f:
        json.dump(data, f, indent=2)
    print(f"added {args.kind} {os.path.relpath(dest, root)}  ({len(data.get('actions', []))} steps; network, video and timeline are not copied)")
    for w in lint(data, args.kind):
        print(f"  warn  {w}")
    return 0


def cmd_check(args):
    from keytap.runner.expand import LoadError, load_recording
    from keytap.recorder.recording import check_actions_file
    root = os.path.abspath(args.dir)
    try:
        suite = load_suite(root)
    except SuiteError as e:
        print(e)
        return 2
    errors = warns = 0

    def report(level, label, msg):
        print(f"  {level:<5} {label}: {msg}")

    login_flow = (suite["login"] or {}).get("flow")
    if login_flow and not os.path.isfile(os.path.join(root, login_flow, "actions.json")):
        report("ERROR", "suite.json", f'login flow "{login_flow}" not found')
        errors += 1
    if isinstance(suite["tests"], list):
        for p in suite["tests"]:
            if not os.path.isfile(os.path.join(root, p, "actions.json")):
                report("ERROR", "suite.json", f'test "{p}" not found')
                errors += 1
    items = [("flow", i) for i in list_flows(root)] + [("test", i) for i in list_items(root, suite["tests"])]
    if not any(k == "test" for k, _ in items):
        report("ERROR", "suite.json", f'no tests match "{suite["tests"]}"')
        errors += 1
    for kind, item in items:
        label = os.path.relpath(item["path"], root)
        actions_json = os.path.join(item["path"], "actions.json")
        problems = check_actions_file(actions_json)
        if not problems:
            try:
                load_recording(item["path"], root, resolve_env=False, strict=False)
            except LoadError as e:
                problems = [str(e)]
        for p in problems:
            report("ERROR", label, p)
        errors += len(problems)
        if problems:
            continue
        ws = lint(_read_json(actions_json), kind)
        for w in ws:
            report("warn", label, w)
        warns += len(ws)
        if not ws:
            report("ok", label, "")
    print(f"\n{len(items)} files, {errors} errors, {warns} warnings")
    return 1 if errors else 0


def cmd_checks(args):
    from keytap.runner import assertions
    print('assert step: {"type": "assert", "check": "<name>", "selector": {...}, "value": "...", "soft": false}\n'
          "It waits until the check holds (like an element tap), then fails with expected and found.\n"
          '"soft": true only warns. "timeout_ms" fixes the wait. Placeholders {{ts}} and {{env:NAME}} work in value.\n')
    for line in assertions.describe():
        print("  " + line)
    return 0


# ── run ───────────────────────────────────────────────────────────────────────

def _push_screenshot(fdir, out, name):
    """Copy a failure's screenshot to <run>/failures/<name>.png so failures can be browsed in one place."""
    src = os.path.join(fdir, "screenshot.png") if fdir else ""
    if not os.path.isfile(src):
        return None
    dst = os.path.join(out, "failures", f"{name}.png")
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copyfile(src, dst)
    return os.path.relpath(dst, out)


def _is_logged_in(dev, check, timeout_s):
    from keytap.device import lookup
    from keytap.recorder.recording import ElementSelector
    sel = ElementSelector(**{k: v for k, v in check.items() if k in SELECTOR_KEYS})
    deadline = time.time() + timeout_s
    while True:
        try:
            if lookup.find_element(dev, sel) is not None:
                return True
        except Exception:
            pass
        if time.time() >= deadline:
            return False
        time.sleep(0.5)


class Runner:
    def __init__(self, root, suite, opts, args, out, say):
        self.root, self.suite, self.opts, self.args, self.out, self.say = root, suite, opts, args, out, say
        self.logged_in = None          # True, False, or None when unknown (after a failure)
        self.logins = {"ran": 0, "failed": 0}
        self._dev = None

    def dev(self):
        if self._dev is None:
            import uiautomator2 as u2
            self._dev = u2.connect(self.opts.serial) if self.opts.serial else u2.connect()
        return self._dev

    def policy(self, header):
        if not self.suite["login"].get("flow"):
            return "never"
        return self.args.login or header.get("login") or self.suite["login"].get("default", "once")

    def run_login(self, test_name, params):
        """Run the login flow. Returns None on success, else (cause, detail, failure_dir)."""
        from keytap.runner.expand import load_recording
        flow = os.path.join(self.root, self.suite["login"]["flow"])
        self.logins["ran"] += 1
        out = os.path.join(self.out, f"_login_{self.logins['ran']:02d}")
        self.say(f"    login for {test_name} ...")
        summary, results = engine.run_recording(
            lambda: load_recording(flow, self.root, params=params)[0],
            self.opts, out, package=self.suite["package"])
        if summary["failed"] == 0:
            self.logged_in = True
            return None
        self.logins["failed"] += 1
        self.logged_in = False
        f = results[0]["failure"]
        cause = f["cause"] if f["cause"] in engine.INFRA_CAUSES else "login_failed"
        detail = f["detail"] if cause == f["cause"] else f"{f['cause']}: {f['detail']}"
        return cause, detail, os.path.join(out, "run_01", "failure")

    def run_test(self, test):
        from keytap.runner.expand import LoadError, load_recording
        from keytap.device import actions
        pkg, suite = self.suite["package"], self.suite
        result = {"name": test["name"], "status": "fail", "login": "never", "login_ran": False,
                  "duration_s": 0, "runs": 0, "passed": 0, "cause": "", "detail": "", "step": 0,
                  "action": "", "failure": None, "screenshot": None, "warnings": 0}
        started = time.time()
        try:
            rec, header = load_recording(test["path"], self.root)
        except LoadError as e:
            result.update(cause="load_error", detail=str(e))
            return result
        policy = self.policy(header)
        result["login"] = policy
        before_each = header.get("before_each", suite["before_each"])
        check = suite["login"].get("check")
        params = header.get("login_params", {})
        fdir = None

        if policy == "always":
            actions.clear_data(pkg)
            self.logged_in = False
        need = policy == "always" or (policy == "once" and bool(check or self.logged_in is not True))
        if need and before_each == "restart":
            actions.force_stop(pkg)
            actions.launch(pkg)
        if policy == "once" and check:
            need = not _is_logged_in(self.dev(), check, self.suite["login"].get("check_timeout", 20))
        elif policy == "once":
            need = self.logged_in is not True
        if need:
            result["login_ran"] = True
            failed = self.run_login(test["name"], params)
            if failed:
                result.update(cause=failed[0], detail=failed[1], failure=failed[2])
                fdir = failed[2]

        if not result["cause"]:
            opts = self.opts
            out = os.path.join(self.out, test["name"])
            repeat = self.args.repeat
            summary, results = engine.run_recording(
                lambda: load_recording(test["path"], self.root)[0], opts, out, repeat=repeat,
                package=pkg, reset="stop" if before_each == "restart" else None)
            result.update(runs=summary["runs"], passed=summary["passed"],
                          warnings=sum(r.get("warnings", 0) for r in results))
            result["status"] = ("pass" if summary["failed"] == 0 else "flaky" if summary["passed"] else "fail")
            bad = next((i for i, r in enumerate(results) if not r["success"]), None)
            if bad is not None:
                f = results[bad]["failure"]
                result.update(cause=f["cause"], detail=f["detail"], step=f["step"], action=f["action"])
                fdir = os.path.join(out, f"run_{bad + 1:02d}", "failure")
            if any(a.type == "clear_data" for a in rec.actions):
                self.logged_in = False
            if result["status"] != "pass":
                self.logged_in = None   # unknown now: verify with the login check, or log in again

        result["duration_s"] = round(time.time() - started, 1)
        if fdir and os.path.isdir(fdir):
            result["failure"] = os.path.relpath(fdir, self.out)
            result["screenshot"] = _push_screenshot(fdir, self.out, test["name"])
        return result


def _write_junit(path, suite_name, results, duration, out):
    failures = [r for r in results if r["status"] != "pass"]
    errors = [r for r in failures if r["cause"] in engine.INFRA_CAUSES]
    root = ET.Element("testsuites")
    ts = ET.SubElement(root, "testsuite", name=suite_name, tests=str(len(results)),
                       failures=str(len(failures) - len(errors)), errors=str(len(errors)), time=f"{duration:.1f}")
    for r in results:
        tc = ET.SubElement(ts, "testcase", classname=suite_name, name=r["name"], time=f"{r['duration_s']:.1f}")
        if r["status"] == "pass":
            continue
        kind = "error" if r["cause"] in engine.INFRA_CAUSES else "failure"
        msg = f"{r['cause']}: {r['detail']}" if r["status"] != "flaky" else \
            f"flaky, {r['runs'] - r['passed']} of {r['runs']} runs failed: {r['cause']}: {r['detail']}"
        el = ET.SubElement(tc, kind, message=msg, type=r["cause"] or "failed")
        el.text = f"step {r['step']} {r['action']}".strip() + (f"\n{os.path.join(out, r['failure'])}" if r["failure"] else "")
        if r["screenshot"]:
            ET.SubElement(tc, "system-out").text = f"[[ATTACHMENT|{os.path.join(out, r['screenshot'])}]]"
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)


def cmd_run(args):
    root = os.path.abspath(args.dir)
    try:
        suite = load_suite(root)
        tests = list_items(root, suite["tests"])
    except SuiteError as e:
        print(e)
        return 2
    if args.names:
        tests = [t for t in tests if any(n.lower() in t["name"].lower() for n in args.names)]
    if args.tag:
        tests = [t for t in tests if args.tag in (_read_json(os.path.join(t["path"], "actions.json")).get("tags") or [])]
    if not tests:
        print("no tests selected")
        return 2
    from keytap.recorder.recording import check_actions_file
    bad = [(t, p) for t in tests for p in [check_actions_file(os.path.join(t["path"], "actions.json"))] if p]
    if bad:
        for t, problems in bad:
            print(f"{t['name']} has {len(problems)} problem(s):")
            for line in problems:
                print("  - " + line)
        print("fix these first (./suite.py check <dir>)")
        return 2

    d = suite["defaults"]
    pick = lambda cli, key, fallback: cli if cli is not None else d.get(key, fallback)
    opts = engine.Options(serial=args.serial, mode=pick(args.mode, "mode", "timed"),
                          wait=pick(args.wait, "wait", 10.0), retries=pick(args.retries, "retries", 0),
                          quiet=True)   # the runner prints one line per test itself
    if not engine.pick_serial(opts):
        return 2
    from keytap.device import actions
    actions.set_sync(True)

    started_at = time.strftime("%Y%m%d_%H%M%S")
    out = os.path.abspath(args.out or os.path.join(root, ".runs", started_at))
    os.makedirs(out, exist_ok=True)
    say = (lambda *a, **k: None) if args.quiet else print
    runner = Runner(root, suite, opts, args, out, say)
    name = os.path.basename(root)
    say(f"suite {name}: {len(tests)} test(s) on {opts.serial or 'the attached phone'}")
    t0 = time.time()
    results = []
    for test in tests:
        res = runner.run_test(test)
        results.append(res)
        extra = f"  {res['cause']}: {res['detail']}" if res["status"] != "pass" else ""
        say(f"  {test['name']:<30} {res['status']:<5} {res['duration_s']:>6.1f}s{extra}")
    duration = round(time.time() - t0, 1)

    causes = {}
    for r in results:
        if r["cause"]:
            causes[r["cause"]] = causes.get(r["cause"], 0) + 1
    totals = {"tests": len(results), "passed": sum(r["status"] == "pass" for r in results),
              "flaky": sum(r["status"] == "flaky" for r in results),
              "failed": sum(r["status"] == "fail" for r in results)}
    summary = {"suite": name, "package": suite["package"], "started_at": started_at,
               "duration_s": duration, "serial": opts.serial or "", "totals": totals,
               "causes": causes, "logins": runner.logins, "tests": results}
    with open(os.path.join(out, "summary.json"), "w") as f:
        json.dump(summary, f, indent=2)
    _write_junit(os.path.join(out, "junit.xml"), name, results, duration, out)

    print(f"\n{totals['passed']}/{totals['tests']} passed"
          + (f", {totals['flaky']} flaky" if totals["flaky"] else "")
          + (f", {totals['failed']} failed" if totals["failed"] else "")
          + f"   ({duration}s, {runner.logins['ran']} login(s))")
    for r in results:
        if r["status"] != "pass":
            print(f"  {r['name']}: {r['cause']}: {r['detail']}")
            if r["screenshot"]:
                print(f"    screenshot: {os.path.join(out, r['screenshot'])}")
    print(f"results: {out}")
    if any(c in causes for c in engine.INFRA_CAUSES):
        return 2
    return 0 if totals["passed"] == totals["tests"] else 1


# ── show ──────────────────────────────────────────────────────────────────────

def cmd_show(args):
    runs_dir = os.path.join(os.path.abspath(args.dir), ".runs")
    runs = sorted(os.listdir(runs_dir)) if os.path.isdir(runs_dir) else []
    if args.list or not runs:
        print("\n".join("  " + r for r in reversed(runs)) if runs else "no runs yet")
        return 0
    run = args.run or runs[-1]
    path = os.path.join(runs_dir, run, "summary.json")
    if not os.path.isfile(path):
        print(f"no run '{run}'. Runs: {', '.join(runs)}")
        return 2
    s = _read_json(path)
    t = s["totals"]
    print(f"{s['suite']}  {s['started_at']}  {t['passed']}/{t['tests']} passed, {t['flaky']} flaky, {t['failed']} failed  ({s['duration_s']}s)")
    for r in s["tests"]:
        extra = f"  {r['cause']}: {r['detail']}" if r["status"] != "pass" else ""
        print(f"  {r['name']:<30} {r['status']:<5} {r['duration_s']:>6.1f}s  login {r['login']}{' (ran)' if r['login_ran'] else ''}{extra}")
        if r["screenshot"]:
            print(f"    screenshot: {os.path.join(runs_dir, run, r['screenshot'])}")
    return 0


def main():
    ap = argparse.ArgumentParser(prog="suite.py", formatter_class=argparse.RawDescriptionHelpFormatter,
                                 description="Test suites made of keytap recordings.", epilog=EPILOG)
    sub = ap.add_subparsers(dest="cmd", metavar="command")

    p = sub.add_parser("init", help="create suite.json, flows/ and tests/")
    p.add_argument("dir")
    p.add_argument("--package", help="the app's package name")
    p.set_defaults(fn=cmd_init)

    p = sub.add_parser("add", help="copy a recording into the suite (only its actions.json)")
    p.add_argument("dir")
    p.add_argument("recording", help="part of a recording name, \"latest\", or a path")
    p.add_argument("--as", dest="kind", choices=["test", "flow"], required=True)
    p.add_argument("--name", help="name of the new folder (default: the recording's name)")
    p.set_defaults(fn=cmd_add)

    p = sub.add_parser("check", help="validate every file, follow run steps, warn about fragile steps")
    p.add_argument("dir")
    p.set_defaults(fn=cmd_check)

    p = sub.add_parser("run", help="run the tests, write results to <dir>/.runs/<time>/")
    p.add_argument("dir")
    p.add_argument("names", nargs="*", help="only tests whose name contains one of these")
    p.add_argument("--tag", help="only tests with this tag")
    p.add_argument("--repeat", type=int, default=1, metavar="N", help="run each test N times (default: 1)")
    p.add_argument("--login", choices=["once", "always", "never"], help="force one login policy for every test")
    p.add_argument("--serial", metavar="ID", help="adb serial (default: the only attached phone)")
    p.add_argument("--mode", choices=["fast", "timed"], help="default: timed, or suite.json defaults")
    p.add_argument("--wait", type=float, metavar="SECONDS", help="extra element wait (default: 10)")
    p.add_argument("--retries", type=int, metavar="N", help="extra attempts for a failed step (default: 0)")
    p.add_argument("--quiet", action="store_true", help="print only the final summary")
    p.add_argument("--out", metavar="DIR", help="results folder (default: <dir>/.runs/<time>)")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("show", help="summary of the latest run, or a named one")
    p.add_argument("dir")
    p.add_argument("run", nargs="?", help="run folder name, see --list")
    p.add_argument("--list", action="store_true", help="list the runs")
    p.set_defaults(fn=cmd_show)

    p = sub.add_parser("checks", help="list the assertions for assert steps")
    p.set_defaults(fn=cmd_checks)

    args = ap.parse_args()
    if not args.cmd:
        ap.print_help()
        return 0
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
