"""
Running recordings on the phone. Shared by replay.py (one recording) and suite.py (many), so both
write the same result files: <out>/summary.json and <out>/run_NN/{result.json, replay.log, failure/}.
"""

import json
import os
import re
import statistics
import threading
import time
from dataclasses import dataclass

INFRA_CAUSES = ("device_unavailable", "load_error")   # the phone or the recording file, not the app


@dataclass
class Options:
    serial: str = None
    mode: str = "timed"
    wait: float = 10.0      # extra seconds to keep looking for an element after the recorded wait
    retries: int = 0
    quiet: bool = False


# ── Choosing a recording and a phone ──────────────────────────────────────────

def pick_serial(opts):
    """Use the only attached phone when no serial is given. False when several are attached."""
    if opts.serial:
        return True
    from adb import list_devices
    devices = list_devices()
    if len(devices) == 1:
        opts.serial = devices[0]
        import config
        config.ARGS.serial = opts.serial   # adb() reads the serial from here
    elif devices:
        print("several phones attached, pass --serial: " + ", ".join(devices))
        return False
    return True


def print_list(items, folder=False):
    """Recordings, newest first. folder=True shows the folder name, which is what to type when
    several recordings share a name."""
    for i in items:
        label = os.path.basename(i["path"]) if folder else i["name"]
        when = i["recorded_at"][:16].replace("T", " ")
        net = "  network" if i["has_network"] else ""
        print(f"  {label:<44} {when}  {i['action_count']:>3} steps{net}")
    if not items:
        print("  (none)")


def resolve_recording(query):
    """Recording path for a path, "latest", or part of a name. When that is not exactly one
    recording, prints the choices and returns None."""
    from recording import Recording
    if os.path.exists(query):
        return query
    items = Recording.list_all()
    if query == "latest" and items:
        return items[0]["path"]
    q = query.lower()
    hits = [i for i in items if q in os.path.basename(i["path"]).lower() or q in i["name"].lower()]
    exact = [i for i in hits if query in (i["name"], os.path.basename(i["path"]))]
    if len(exact) == 1:
        return exact[0]["path"]
    if len(hits) == 1:
        return hits[0]["path"]
    if hits:
        print(f"'{query}' matches {len(hits)} recordings, be more specific:")
        print_list(hits, folder=True)
    else:
        print(f"no recording matches '{query}'. Recordings:")
        print_list(items)
    return None


def package_of(rec, override=""):
    return override or next((a.package for a in rec.actions if a.package), "")


# ── One run ───────────────────────────────────────────────────────────────────

def device_ready(serial):
    """Wake the screen and check the phone is online. Returns {'battery': n} or None."""
    from adb import adb, list_devices
    from utils import adb_shell
    devices = list_devices()
    if not devices or (serial and serial not in devices):
        return None
    adb("shell", "input", "keyevent", "KEYCODE_WAKEUP")
    adb("shell", "wm", "dismiss-keyguard")
    m = re.search(r"level:\s*(\d+)", adb_shell("dumpsys battery", serial))
    return {"battery": int(m.group(1)) if m else None}


def reset_state(rec, package, how):
    """Force-stop the app (how="stop"), or also wipe its data and login (how="clear")."""
    import actions
    actions.force_stop(package)
    if how == "clear":
        actions.clear_data(package)
    if rec.actions and rec.actions[0].type not in ("launch", "launch_deeplink"):
        actions.launch(package)
        time.sleep(3)


def run_once(rec, opts, run_dir, package, health, say):
    from adb import adb, get_device_size
    from failure_bundle import _crash, capture
    from replayer import ActionReplayer

    os.makedirs(run_dir, exist_ok=True)
    done, outcome = threading.Event(), {}

    def on_done(success, step, error):
        outcome.update(success=success, step=step, error=error)
        done.set()

    log = open(os.path.join(run_dir, "replay.log"), "w")

    def on_status(msg):
        say("  " + msg, flush=True)
        log.write(msg + "\n")

    try:
        adb("logcat", "-c")   # so a crash found afterwards happened during this run
    except Exception:
        pass
    rep = ActionReplayer()
    rep.extra_wait_s = opts.wait
    rep.default_retries = opts.retries
    started = time.time()
    rep.start(rec, opts.mode, opts.serial, on_status, on_done, device_resolution=get_device_size())
    done.wait()
    log.close()

    success, step, error = outcome["success"], outcome["step"], outcome["error"]
    label = rep.results[-1]["label"] if rep.results else ""
    if rep.results and rep.results[-1].get("origin"):
        label += f"  [{rep.results[-1]['origin']}]"
    if success:   # implicit assertion: the app must not have crashed or hung during the run
        try:
            logcat = adb("logcat", "-d", "-t", "2000", "-v", "threadtime").stdout.decode(errors="replace")
        except Exception:
            logcat = ""
        crash = _crash(logcat, package)
        if crash:
            success, step, label = False, len(rec.actions), "(after the last step)"
            error = f"the app crashed during the run: {crash}"

    result = {"recording": rec.name, "serial": opts.serial or "",
              "success": success, "total_steps": len(rec.actions),
              "battery": health["battery"], "warnings": sum(r["status"] == "warn" for r in rep.results),
              "duration_s": round(time.time() - started, 1), "steps": rep.results}
    if not success:
        result["failure"] = capture(os.path.join(run_dir, "failure"), step, label, error,
                                    rep.u2_device, opts.serial, package)
    with open(os.path.join(run_dir, "result.json"), "w") as f:
        json.dump(result, f, indent=2)
    return result


def _failed(cause, detail):
    return {"success": False, "steps": [],
            "failure": {"step": 0, "action": "", "cause": cause, "detail": detail}}


def run_recording(load, opts, out, repeat=1, package="", reset=None, net=None):
    """Run a recording `repeat` times, each into out/run_NN, and write out/summary.json.
    load() returns a fresh Recording per run, so {{ts}} differs between runs.
    Returns (summary, results)."""
    from expand import LoadError
    say = (lambda *a, **k: None) if opts.quiet else print
    os.makedirs(out, exist_ok=True)
    results, name = [], ""
    for n in range(1, repeat + 1):
        try:
            rec = load()
        except LoadError as e:
            say(f"  cannot load the recording: {e}")
            results.append(_failed("load_error", str(e)))
            break
        name = rec.name
        say(f"run {n}/{repeat}: {rec.name}", flush=True)
        health = device_ready(opts.serial)
        if health is None:
            say("  device not reachable")
            results.append(_failed("device_unavailable", "no device online"))
            break
        if reset:
            reset_state(rec, package, reset)
        use_net = net is not None and bool(rec.network_flows)
        if use_net:
            net.start({"flows": rec.network_flows, "host_tokens": rec.network_host_tokens})
        try:
            result = run_once(rec, opts, os.path.join(out, f"run_{n:02d}"), package, health, say)
        finally:
            if use_net:
                net.stop()
        results.append(result)
        verdict = "pass" if result["success"] else "FAIL: " + result["failure"]["cause"]
        say(f"  {verdict}  ({result['duration_s']}s)", flush=True)
    summary = summarize(name, results)
    with open(os.path.join(out, "summary.json"), "w") as f:
        json.dump(summary, f, indent=2)
    return summary, results


# ── Summaries ─────────────────────────────────────────────────────────────────

def _spread(values, unit):
    return {f"min_{unit}": min(values), f"median_{unit}": statistics.median(values), f"max_{unit}": max(values)}


def timings(results):
    """Whole-run and per-step timings over the runs that got that far (a failed run stops early)."""
    ran = [r for r in results if r.get("steps")]
    steps = []
    for i in range(max((len(r["steps"]) for r in ran), default=0)):
        hits = [r["steps"][i] for r in ran if len(r["steps"]) > i]
        steps.append({"step": i + 1, "label": hits[0]["label"], "runs": len(hits),
                      **_spread([h["ms"] for h in hits], "ms"),
                      "retries": sum(h["retries"] for h in hits),
                      "failed": sum(h["status"] == "fail" for h in hits),
                      "warned": sum(h["status"] == "warn" for h in hits)})
    total = _spread([r["duration_s"] for r in ran], "s") if ran else {}
    return total, steps


def summarize(name, results):
    passed = sum(r["success"] for r in results)
    failed_steps, causes = {}, {}
    for r in results:
        f = r.get("failure")
        if f:
            key = f"step {f['step']} {f['action']}".strip()
            failed_steps[key] = failed_steps.get(key, 0) + 1
            causes[f["cause"]] = causes.get(f["cause"], 0) + 1
    total, steps = timings(results)
    return {"recording": name, "runs": len(results), "passed": passed,
            "failed": len(results) - passed, "flaky": 0 < passed < len(results),
            "failed_steps": failed_steps, "causes": causes, "total": total, "steps": steps}


def print_summary(summary, out, quiet=False):
    print(f"\n{summary['passed']}/{summary['runs']} passed" + ("  (flaky)" if summary["flaky"] else ""))
    if summary["steps"] and not quiet:
        t = summary["total"]
        print(f"total {t['min_s']} / {t['median_s']} / {t['max_s']} s  (min / median / max)")
        for st in summary["steps"]:
            print(f"  {st['step']}. {st['label'][:38]:<38} {st['min_ms']:>6} {st['median_ms']:>7.0f} {st['max_ms']:>6} ms")
    for key, count in summary["failed_steps"].items():
        print(f"  {count}x {key}")
    for cause, count in summary["causes"].items():
        print(f"  cause {cause}: {count}")
    print(f"results: {out}")


def exit_code(summary):
    """0 all runs passed, 1 a step failed, 2 the phone or the recording file was the problem."""
    if any(c in summary["causes"] for c in INFRA_CAUSES):
        return 2
    return 0 if summary["failed"] == 0 else 1
