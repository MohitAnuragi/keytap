"""
Live dashboard for test runs, in the browser. It reads data/nightly, suites/*/.runs and `ps`, and can start
and stop a run (the Run panel): it only ever launches suites/gojek_common/run_suite.sh and run_nightly.sh,
one run at a time, from a page that holds a random token (so no other website can trigger a run).

  ./dashboard.py                 then open http://127.0.0.1:8765
  ./dashboard.py --port 9000

Pages: /  the dashboard (what is running, every test, every step, retries, failures)
       /flow  how a test run works, in pictures
"""

import argparse
import glob
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from keytap.core.paths import DATA_DIR, ROOT

PAGES = Path(__file__).parent / "dashboard_pages"
LIVE_S = 120          # a run whose log changed in the last 2 minutes is live
MAX_ATTEMPTS = 3      # nightly default (the config may differ)
_cache = {}
TOKEN = secrets.token_urlsafe(16)   # the page sends it back with every start or stop
LAUNCH = {}                         # the run this server started last: pid, what, log file, started


def _mtime(p):
    try:
        return p.stat().st_mtime
    except OSError:
        return 0


def _birth(p):
    try:
        st = p.stat()
        return getattr(st, "st_birthtime", st.st_mtime)
    except OSError:
        return 0


def _json(p, default=None):
    """A json file, cached by its modification time (finished runs never change)."""
    key = (str(p), _mtime(p))
    if key not in _cache:
        try:
            _cache[key] = json.loads(p.read_text())
        except (OSError, ValueError):
            _cache[key] = default
    return _cache[key]


def rel(p):
    """A path the browser can send back: "data:<under data/>" or <under the repo>."""
    p = Path(p).resolve()
    for base, prefix in ((DATA_DIR, "data:"), (ROOT, "")):
        try:
            return prefix + str(p.relative_to(base.resolve()))
        except ValueError:
            pass
    return str(p)


def unrel(s):
    """Inverse of rel(), refusing anything outside data/ and suites/."""
    p = (DATA_DIR / s[5:]) if s.startswith("data:") else (ROOT / s)
    p = p.resolve()
    ok = [DATA_DIR.resolve(), (ROOT / "suites").resolve()]
    if not any(p == b or b in p.parents for b in ok):
        raise ValueError("outside the data and suites folders")
    return p


# -- processes ---------------------------------------------------------------------------------

def processes():
    out = subprocess.run(["ps", "-axo", "pid=,etime=,command="], capture_output=True, text=True).stdout
    procs = []
    for line in out.splitlines():
        m = re.match(r"\s*(\d+)\s+(\S+)\s+(.*)", line)
        if not m or "dashboard" in m.group(3):
            continue
        kind = ("nightly" if re.search(r"\bnightly\.py\b", m.group(3)) else
                "suite" if re.search(r"\bsuite\.py run\b", m.group(3)) else
                "replay" if re.search(r"\breplay\.py\b", m.group(3)) else "")
        if kind:
            cmd = re.sub(r"^\S*[Pp]ython\S*\s+", "", m.group(3))
            procs.append({"pid": int(m.group(1)), "elapsed": m.group(2), "kind": kind, "cmd": cmd[:160]})
    return procs


# -- one run folder ----------------------------------------------------------------------------

STEP = re.compile(r"^\[REPLAY (\d+)/(\d+)\] (.*)")


def parse_log(path):
    """The steps a replay.log shows so far, with what happened inside each."""
    steps, lines = [], []
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        pass
    for ln in lines:
        m = STEP.match(ln)
        if m:
            steps.append({"n": int(m.group(1)), "total": int(m.group(2)), "label": m.group(3), "waiting": False,
                          "skipped": False, "retries": 0, "retry_max": 0, "retaps": 0, "notes": []})
            continue
        if not steps:
            continue
        s = steps[-1]
        r = re.match(r"^\[RETRY (\d+)/(\d+)\]", ln)
        if ln.startswith("[WAIT]"):
            s["waiting"] = True
        elif ln.startswith("[SKIP]"):
            s["skipped"] = True
            s["notes"].append(ln)
        elif r:
            s["retries"], s["retry_max"] = int(r.group(1)), int(r.group(2))
            s["notes"].append(ln)
        elif ln.startswith("[RETAP"):
            s["retaps"] += 1
            s["notes"].append(ln)
        elif ln.startswith(("[WARN]", "[RETYPE")):
            s["notes"].append(ln)
    return steps, lines[-40:]


def read_run(run_dir):
    """Summary of one run_NN folder, finished or still going."""
    log, res = run_dir / "replay.log", run_dir / "result.json"
    d = {"dir": rel(run_dir), "test": run_dir.parent.name, "run": run_dir.name, "started": _birth(run_dir)}
    j = _json(res)
    if j:
        fails = j.get("failure") or {}
        d.update(state="pass" if j.get("success") else "fail", total=j.get("total_steps", 0),
                 step=j.get("total_steps", 0), duration=j.get("duration_s"), warnings=j.get("warnings", 0),
                 anr=bool(j.get("anr")), failure={k: fails.get(k) for k in ("step", "action", "cause", "detail")} if fails else None,
                 create_groups=sum(1 for s in j.get("steps", []) if "Create group" in s["label"] and s["status"] != "skip"),
                 retried=sum(1 for s in j.get("steps", []) if s.get("retries")))
        shot = run_dir / "failure" / "screenshot.png"
        d["screenshot"] = rel(shot) if shot.exists() else None
        return d
    steps, tail = parse_log(log)
    live = time.time() - _mtime(log) < LIVE_S if log.exists() else time.time() - d["started"] < 30
    cur = steps[-1] if steps else None
    d.update(state="running" if live else "interrupted", total=cur["total"] if cur else 0, step=cur["n"] if cur else 0,
             current=cur, tail=tail, duration=round(time.time() - d["started"]) if live else None,
             create_groups=sum(1 for s in steps if "Create group" in s["label"] and not s["skipped"]),
             retried=sum(1 for s in steps if s["retries"]), warnings=0, anr=False, failure=None, screenshot=None)
    return d


def scan_out(out_dir):
    """Every run in a suite output folder (<test>/run_NN), oldest first."""
    runs = []
    if out_dir.is_dir():
        for t in out_dir.iterdir():
            if t.is_dir() and not t.name.startswith((".", "failures")):
                runs += [read_run(r) for r in t.glob("run_*") if r.is_dir()]
    return sorted(runs, key=lambda r: r["started"])


# -- suites ------------------------------------------------------------------------------------

def suite_tests(root):
    """[(name, description)] in the suite's order."""
    try:
        from keytap.cli.suite import list_items, load_suite
        suite = load_suite(str(root))
        items = list_items(str(root), suite["tests"])
    except Exception:
        return [], {}
    out = []
    for it in items:
        h = _json(Path(it["path"]) / "actions.json", {}) or {}
        out.append((it["name"], (h.get("description") or "")[:240]))
    return out, suite


def read_audit(night):
    ev = []
    try:
        for ln in (night / "audit.jsonl").read_text().splitlines():
            try:
                ev.append(json.loads(ln))
            except ValueError:
                pass
    except OSError:
        pass
    return ev


def read_night(night_id):
    night = DATA_DIR / "nightly" / night_id
    events = read_audit(night)
    results = _json(night / "results.json")
    alive = any(p["kind"] == "nightly" for p in processes())
    start = next((e for e in events if e["event"] == "start"), {})
    ended = any(e["event"] == "end" for e in events)
    suites = []
    now = None
    groups = 0
    for sp in start.get("suites", []):
        root = ROOT / sp
        sname = root.name
        tests, suite = suite_tests(root)
        reuse = {e["test"]: e["skipped_needs"] for e in events if e["event"] == "reuse" and e.get("suite") == sname}
        skipped = {e["test"]: e.get("why") for e in events if e["event"] == "skip" and e.get("suite") == sname}
        rows, current_idx = [], -1
        for i, (name, desc) in enumerate(tests):
            if (night / sname / name).is_dir():
                current_idx = i
        for i, (name, desc) in enumerate(tests):
            evs = [e for e in events if e["event"] == "attempt" and e.get("suite") == sname and e["test"] == name]
            attempts, running_run = [], None
            adirs = sorted((night / sname / name).glob("attempt_*")) if (night / sname / name).is_dir() else []
            for ad in adirs:
                n = int(ad.name.split("_")[1])
                ev = next((e for e in evs if e["n"] == n), None)
                runs = scan_out(ad)
                for r in runs:
                    groups += r["create_groups"]
                    if r["state"] == "running":
                        running_run = {**r, "attempt": n, "in_test": name}
                own = next((r for r in runs if r["test"] == name and r["state"] in ("pass", "fail")), None)
                others_failed = any(r["state"] == "fail" for r in runs if r is not own)
                astatus = ev["status"] if ev else "running"
                if own and ev:   # the test's own run is the truth, whatever the supervisor recorded
                    astatus = "fail" if own["state"] == "fail" else "warn" if others_failed else "pass"
                attempts.append({"n": n, "status": astatus, "supervisor_status": ev["status"] if ev else None,
                                 "cause": (ev or {}).get("cause", ""),
                                 "detail": (ev or {}).get("detail", ""), "step": (ev or {}).get("step"),
                                 "action": (ev or {}).get("action", ""), "runs": runs,
                                 "reused": reuse.get(name) if n == 1 else None})
            fin = next((r for r in (results or {}).get("results", []) if r["suite"] == sname and r["test"] == name), None)
            if fin and not attempts:
                status = fin["status"]
            elif name in skipped:
                status = "skipped_policy"
            elif not attempts:
                status = "pending" if alive else "not_run"
            else:
                last = attempts[-1]
                if last["status"] == "running":
                    status = "running" if alive else "stopped"
                elif last["status"] in ("pass", "warn"):
                    status = last["status"] if last["status"] == "warn" else "recovered" if len(attempts) > 1 else "pass"
                elif i == current_idx and alive and len(attempts) < MAX_ATTEMPTS and not ended:
                    status = "between_attempts"
                else:
                    status = "fail" if (ended or i < current_idx or len(attempts) >= MAX_ATTEMPTS) else "stopped"
            if running_run and status in ("running",):
                now = {"suite": sname, "test": name, "desc": desc, **running_run}
            rows.append({"name": name, "desc": desc, "status": status, "attempts": attempts, "reused": reuse.get(name),
                         "needs": (suite.get("needs") or {}).get(name, []), "skip_why": skipped.get(name)})
        suites.append({"name": sname, "package": suite.get("package", ""), "tests": rows})
    counts = {}
    for s in suites:
        for t in s["tests"]:
            counts[t["status"]] = counts.get(t["status"], 0) + 1
    return {"kind": "night", "id": night_id, "started": start.get("t"), "serial": start.get("serial"), "alive": alive,
            "ended": ended, "abort": next((e.get("why") for e in events if e["event"] == "abort"), ""),
            "builds": (results or {}).get("builds") or {e["suite"]: e["build"] for e in events if e["event"] == "build"},
            "duration": (results or {}).get("duration_s"), "begun": _birth(night), "suites": suites, "counts": counts,
            "now": now, "events": events[-300:], "groups_created": groups, "max_attempts": MAX_ATTEMPTS}


def read_suite_run(suite, run_id):
    root = ROOT / "suites" / suite
    out = root / ".runs" / run_id
    tests, cfg = suite_tests(root)
    alive = any(p["kind"] in ("suite", "replay") for p in processes())
    summary = _json(out / "summary.json")
    runs = scan_out(out)
    by = {}
    for r in runs:
        by.setdefault(r["test"], []).append(r)
    rows, now, groups = [], None, 0
    for name, desc in tests:
        rs = by.get(name, [])
        groups += sum(r["create_groups"] for r in rs)
        if not rs:
            status = "pending" if alive and not summary else "not_run"
        elif any(r["state"] == "running" for r in rs):
            status = "running"
            now = {"suite": suite, "test": name, "desc": desc, "attempt": 1, "in_test": name, **next(r for r in rs if r["state"] == "running")}
        elif any(r["state"] == "interrupted" for r in rs):
            status = "stopped"
        else:
            status = "pass" if all(r["state"] == "pass" for r in rs) else "fail"
        rows.append({"name": name, "desc": desc, "status": status, "needs": (cfg.get("needs") or {}).get(name, []),
                     "reused": None, "skip_why": None,
                     "attempts": [{"n": 1, "status": status, "cause": "", "detail": "", "step": None, "action": "", "runs": rs, "reused": None}] if rs else []})
    counts = {}
    for t in rows:
        counts[t["status"]] = counts.get(t["status"], 0) + 1
    return {"kind": "run", "id": f"{suite}/{run_id}", "started": run_id, "serial": (summary or {}).get("serial"),
            "alive": alive and not summary, "ended": bool(summary), "abort": "", "builds": {suite: cfg.get("package", "")},
            "duration": (summary or {}).get("duration_s"), "begun": _birth(out),
            "suites": [{"name": suite, "package": cfg.get("package", ""), "tests": rows}], "counts": counts,
            "now": now, "events": [], "groups_created": groups, "max_attempts": 1}


# -- starting and stopping a run -----------------------------------------------------------------

def nightly_configs():
    found = sorted(glob.glob(str(ROOT / "nightly*.json")) + glob.glob(str(ROOT / "suites" / "*" / "nightly*.json")))
    return [os.path.relpath(p, ROOT) for p in found]


def options():
    suites = []
    sd = ROOT / "suites"
    for s in sorted(sd.iterdir()) if sd.is_dir() else []:
        if (s / "suite.json").is_file() and s.name != "gojek_common":
            tests, cfg = suite_tests(s)
            if tests:
                suites.append({"name": s.name, "package": cfg.get("package", ""), "needs": cfg.get("needs") or {},
                               "tests": [{"name": n, "desc": d} for n, d in tests]})
    return {"token": TOKEN, "suites": suites, "configs": nightly_configs()}


def launch(req):
    """Start one run through the same scripts you would type. Raises ValueError with a plain reason."""
    mine = LAUNCH.get("proc")
    # our own launch counts at once: `ps` does not show a process for a moment after it starts
    if (mine and mine.poll() is None) or any(p["kind"] in ("nightly", "suite", "replay") for p in processes()):
        raise ValueError("A run is already going on the phone. Stop it first.")
    opts = options()
    if req.get("kind") == "nightly":
        if req.get("config") not in opts["configs"]:
            raise ValueError("Unknown nightly config.")
        cmd, what = ["bash", "suites/gojek_common/run_nightly.sh", req["config"]], "nightly " + req["config"]
    elif req.get("kind") == "suite":
        suite = next((s for s in opts["suites"] if s["name"] == req.get("suite")), None)
        if not suite:
            raise ValueError("Unknown suite.")
        names = req.get("tests") or []
        if not isinstance(names, list) or any(n not in {t["name"] for t in suite["tests"]} for n in names):
            raise ValueError("Unknown test in the selection.")
        repeat = req.get("repeat", 1)
        if not isinstance(repeat, int) or not 1 <= repeat <= 10:
            raise ValueError("Repeat must be a number from 1 to 10.")
        cmd = ["bash", "suites/gojek_common/run_suite.sh", suite["name"], *names] + (["--exact"] if names else []) + ["--repeat", str(repeat)]
        what = f"{suite['name']}: {', '.join(names) if names else 'all tests'}" + (f" x{repeat}" if repeat > 1 else "")
    else:
        raise ValueError("Nothing to start.")
    logdir = DATA_DIR / "dashboard"
    logdir.mkdir(parents=True, exist_ok=True)
    log = logdir / (time.strftime("%Y%m%d_%H%M%S") + ".log")
    with open(log, "w") as f:
        p = subprocess.Popen(cmd, cwd=ROOT, stdout=f, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
    LAUNCH.clear()
    LAUNCH.update(pid=p.pid, what=what, log=str(log), started=time.time(), proc=p)
    return {"pid": p.pid, "what": what}


def stop():
    """SIGTERM to the whole run (its scripts restore the phone's animation settings on the way out)."""
    procs = [p for p in processes() if p["kind"] in ("nightly", "suite", "replay")]
    p = LAUNCH.get("proc")
    if not procs and not (p and p.poll() is None):
        raise ValueError("Nothing is running.")
    if p and p.poll() is None:
        os.killpg(p.pid, signal.SIGTERM)
        return {"stopped": LAUNCH["what"]}
    for pr in procs:   # a run started from a terminal: stop just that process
        os.kill(pr["pid"], signal.SIGTERM)
    return {"stopped": ", ".join(pr["kind"] for pr in procs)}


def launch_info():
    if not LAUNCH:
        return None
    p = LAUNCH["proc"]
    code = p.poll()
    tail = []
    try:
        tail = Path(LAUNCH["log"]).read_text(errors="replace").splitlines()[-12:]
    except OSError:
        pass
    return {"what": LAUNCH["what"], "pid": LAUNCH["pid"], "started": LAUNCH["started"], "exit": code, "tail": tail}


def overview():
    nights, runs = [], []
    nd = DATA_DIR / "nightly"
    if nd.is_dir():
        for n in sorted(nd.iterdir(), reverse=True)[:15]:
            res = _json(n / "results.json")
            c = {}
            for r in (res or {}).get("results", []):
                c[r["status"]] = c.get(r["status"], 0) + 1
            nights.append({"id": n.name, "begun": _birth(n), "mtime": _mtime(n / "audit.jsonl"), "finished": bool(res),
                           "counts": c, "duration": (res or {}).get("duration_s")})
    sd = ROOT / "suites"
    if sd.is_dir():
        for s in sd.iterdir():
            rd = s / ".runs"
            if rd.is_dir():
                for r in rd.iterdir():
                    if r.is_dir():
                        sm = _json(r / "summary.json")
                        runs.append({"suite": s.name, "id": r.name, "begun": _birth(r), "mtime": _mtime(r),
                                     "finished": bool(sm), "totals": (sm or {}).get("totals"), "duration": (sm or {}).get("duration_s")})
    runs.sort(key=lambda r: r["mtime"], reverse=True)
    procs = processes()
    if any(p["kind"] == "nightly" for p in procs) and nights:
        default = {"kind": "night", "id": nights[0]["id"]}
    elif any(p["kind"] in ("suite", "replay") for p in procs) and runs:
        default = {"kind": "run", "suite": runs[0]["suite"], "id": runs[0]["id"]}
    elif nights and (not runs or nights[0]["mtime"] >= runs[0]["mtime"]):
        default = {"kind": "night", "id": nights[0]["id"]}
    elif runs:
        default = {"kind": "run", "suite": runs[0]["suite"], "id": runs[0]["id"]}
    else:
        default = None
    return {"now": time.time(), "processes": procs, "nights": nights, "runs": runs[:25], "default": default,
            "launch": launch_info()}


def steps_of(run_dir):
    """Every step of one run, for the detail panel."""
    j = _json(run_dir / "result.json")
    if j:
        steps = [{"n": s["step"], "label": s["label"], "status": s["status"], "ms": s["ms"], "retries": s.get("retries", 0),
                  "origin": (s.get("origin") or "").split(" <- ")[0], "error": s.get("error")} for s in j["steps"]]
        return {"done": True, "steps": steps, "failure": j.get("failure"), "anr": bool(j.get("anr")), "tail": []}
    steps, tail = parse_log(run_dir / "replay.log")
    out = []
    for i, s in enumerate(steps):
        last = i == len(steps) - 1
        out.append({"n": s["n"], "label": s["label"], "status": "running" if last else "skip" if s["skipped"] else "done",
                    "ms": None, "retries": s["retries"], "origin": "", "error": None, "waiting": s["waiting"] and last,
                    "notes": s["notes"][-3:], "retaps": s["retaps"]})
    return {"done": False, "steps": out, "failure": None, "anr": False, "tail": tail}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _local_only(self):
        """Only this machine's own page may start or stop a run (blocks other websites and DNS rebinding)."""
        host = (self.headers.get("Host") or "").split(":")[0]
        origin = self.headers.get("Origin")
        if host not in ("127.0.0.1", "localhost") or (origin and urlparse(origin).hostname not in ("127.0.0.1", "localhost")):
            raise ValueError("Not allowed from this page.")
        if not secrets.compare_digest(self.headers.get("X-Dash-Token") or "", TOKEN):
            raise ValueError("Missing or wrong token. Reload the dashboard.")

    def do_POST(self):
        u = urlparse(self.path)
        try:
            self._local_only()
            n = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(min(n, 20000)) or b"{}")
            if u.path == "/api/run":
                return self._send(200, json.dumps(launch(req)))
            if u.path == "/api/stop":
                return self._send(200, json.dumps(stop()))
            return self._send(404, json.dumps({"error": "not found"}))
        except (ValueError, KeyError, OSError) as e:
            return self._send(400, json.dumps({"error": str(e)}))

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path in ("/", "/index.html"):
                return self._send(200, (PAGES / "index.html").read_bytes(), "text/html; charset=utf-8")
            if u.path in ("/flow", "/flow.html"):
                return self._send(200, (PAGES / "flow.html").read_bytes(), "text/html; charset=utf-8")
            if u.path == "/api/overview":
                return self._send(200, json.dumps(overview()))
            if u.path == "/api/options":
                return self._send(200, json.dumps(options()))
            if u.path == "/api/view":
                if q.get("kind") == "night":
                    return self._send(200, json.dumps(read_night(os.path.basename(q["id"]))))
                return self._send(200, json.dumps(read_suite_run(os.path.basename(q["suite"]), os.path.basename(q["id"]))))
            if u.path == "/api/steps":
                d = unrel(q["dir"])
                if not d.name.startswith("run_"):
                    raise ValueError("not a run folder")
                return self._send(200, json.dumps(steps_of(d)))
            if u.path == "/file":
                p = unrel(q["p"])
                if p.suffix != ".png":
                    raise ValueError("png only")
                return self._send(200, p.read_bytes(), "image/png")
            return self._send(404, json.dumps({"error": "not found"}))
        except (ValueError, KeyError, OSError) as e:
            return self._send(400, json.dumps({"error": str(e)}))


def main():
    ap = argparse.ArgumentParser(prog="dashboard.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)   # this machine only
    print(f"dashboard: http://127.0.0.1:{args.port}   (Ctrl+C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
