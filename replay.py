#!/opt/homebrew/bin/python3
"""
Headless replay of a recording, for nightly smoke runs. No window, no keyboard.

  ./replay.py <name> [--repeat N] [--reset stop|clear] [--retries N] [--wait SECONDS] [--network]
  ./replay.py --list            (or no arguments) shows the recordings

<name> is part of a recording's name, "latest", or a path. The phone is picked automatically
when only one is attached, otherwise pass --serial ID.

Each run writes <out>/run_NN/result.json (per-step status and timing) and, when a step fails,
<out>/run_NN/failure/ (screenshot, hierarchy, logcat, failure.json with the cause).
<out>/summary.json counts passes and which steps fail, so --repeat 10 shows what is flaky.

Exit code: 0 all runs passed, 1 a step failed, 2 the device or uiautomator2 was unreachable.
"""

import argparse
import json
import os
import re
import statistics
import sys
import threading
import time


EPILOG = """examples:
  ./replay.py                                  list the recordings
  ./replay.py commsFE_v2                       run one recording once
  ./replay.py commsFE_v2 --repeat 10           run it 10 times and see what is flaky
  ./replay.py commsFE_v2 --repeat 10 --quiet   final summary only, for scripts and Claude
  ./replay.py latest --reset stop              newest recording, app force-stopped first
  ./replay.py commsFE_v2 --retries 1 --wait 20 forgive one slow step, wait longer for elements

results (in data/runs/<name>_<time>/):
  summary.json              passes, failing steps, causes, timings (min / median / max)
  run_NN/result.json        every step: status, ms, retries, error
  run_NN/replay.log         what the replay printed
  run_NN/failure/           on a failed step: screenshot.png, hierarchy.xml, logcat.txt, failure.json

failure causes:  device_unavailable, app_crash, wrong_app_in_foreground, element_not_found, action_failed

exit code:  0 all runs passed   1 a step failed   2 phone or uiautomator2 unreachable, bad recording or name
"""


def _spread(values, unit):
    return {f"min_{unit}": min(values), f"median_{unit}": statistics.median(values), f"max_{unit}": max(values)}


def _timings(results):
    """Whole-run and per-step timings over the runs that got that far (a failed run stops early)."""
    ran = [r for r in results if r.get("steps")]
    steps = []
    for i in range(max((len(r["steps"]) for r in ran), default=0)):
        hits = [r["steps"][i] for r in ran if len(r["steps"]) > i]
        steps.append({"step": i + 1, "label": hits[0]["label"], "runs": len(hits),
                      **_spread([h["ms"] for h in hits], "ms"),
                      "retries": sum(h["retries"] for h in hits),
                      "failed": sum(h["status"] == "fail" for h in hits)})
    total = _spread([r["duration_s"] for r in ran], "s") if ran else {}
    return total, steps


def _print_list(items, folder=False):
    """Recordings, newest first. folder=True shows the folder name, which is what to type when
    several recordings share a name."""
    for i in items:
        label = os.path.basename(i["path"]) if folder else i["name"]
        when = i["recorded_at"][:16].replace("T", " ")
        net = "  network" if i["has_network"] else ""
        print(f"  {label:<44} {when}  {i['action_count']:>3} steps{net}")
    if not items:
        print("  (none)")


def _resolve(query):
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
        _print_list(hits, folder=True)
    else:
        print(f"no recording matches '{query}'. Recordings:")
        _print_list(items)
    return None


def _package(rec, override):
    if override:
        return override
    return next((a.package for a in rec.actions if a.package), "")


def _device_ready(serial):
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


def _reset(rec, package, how):
    import actions
    actions.force_stop(package)
    if how == "clear":
        actions.clear_data(package)
    if rec.actions and rec.actions[0].type not in ("launch", "launch_deeplink"):
        actions.launch(package)
        time.sleep(3)


def _run_once(rec, args, run_dir, package, health):
    from adb import get_device_size
    from failure_bundle import capture
    from replayer import ActionReplayer

    os.makedirs(run_dir, exist_ok=True)
    done, outcome = threading.Event(), {}

    def on_done(success, step, error):
        outcome.update(success=success, step=step, error=error)
        done.set()

    log = open(os.path.join(run_dir, "replay.log"), "w")

    def on_status(msg):
        if not args.quiet:
            print("  " + msg, flush=True)
        log.write(msg + "\n")

    rep = ActionReplayer()
    rep.extra_wait_s = args.wait
    rep.default_retries = args.retries
    started = time.time()
    rep.start(rec, args.mode, args.serial, on_status, on_done, device_resolution=get_device_size())
    done.wait()
    log.close()

    result = {"recording": rec.name, "serial": args.serial or "",
              "success": outcome["success"], "total_steps": len(rec.actions),
              "battery": health["battery"],
              "duration_s": round(time.time() - started, 1), "steps": rep.results}
    if not outcome["success"]:
        label = rep.results[-1]["label"] if rep.results else ""
        result["failure"] = capture(os.path.join(run_dir, "failure"), outcome["step"], label,
                                    outcome["error"], rep.u2_device, args.serial, package)
    with open(os.path.join(run_dir, "result.json"), "w") as f:
        json.dump(result, f, indent=2)
    return result


def main():
    ap = argparse.ArgumentParser(
        prog="replay.py", formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Headless replay of a keytap recording on a connected phone.",
        epilog=EPILOG)
    ap.add_argument("recording", nargs="?",
                    help='part of a recording name, its folder name, "latest", or a path. '
                         "With no name, the recordings are listed.")
    ap.add_argument("--list", action="store_true", help="list the recordings and exit")
    run = ap.add_argument_group("how to run")
    run.add_argument("--repeat", type=int, default=1, metavar="N",
                     help="run the recording N times in a row (default: 1)")
    run.add_argument("--serial", metavar="ID",
                     help="adb serial of the phone (default: the only attached phone)")
    run.add_argument("--mode", choices=["fast", "timed"], default="timed",
                     help="timed: keep the pauses of the recording, each capped at 10s. "
                          "fast: 0.3s between steps (default: timed)")
    wait = ap.add_argument_group("waiting and retries")
    wait.add_argument("--wait", type=float, default=10.0, metavar="SECONDS",
                      help="element taps wait as long as the recording did, then keep looking this many "
                           "seconds more (default: 10)")
    wait.add_argument("--retries", type=int, default=0, metavar="N",
                      help="extra attempts for a step that failed (default: 0)")
    state = ap.add_argument_group("start state and network")
    state.add_argument("--reset", choices=["stop", "clear"],
                       help="before each run: force-stop the app (stop), or also wipe its data and "
                            "login (clear). Default: no reset. A recording that starts with force_stop "
                            "needs neither.")
    state.add_argument("--package", metavar="PKG",
                       help="app to reset (default: the first package in the recording)")
    state.add_argument("--network", action="store_true",
                       help="serve the recorded network responses (needs a full-dump recording)")
    ap.add_argument("--quiet", action="store_true",
                    help="print only the final summary (progress goes to run_NN/replay.log)")
    ap.add_argument("--out", metavar="DIR", help="results folder (default: data/runs/<name>_<time>)")
    args = ap.parse_args()

    from paths import RUNS_DIR
    from recording import Recording, check_actions_file

    if args.list or not args.recording:
        _print_list(Recording.list_all())
        if not args.list:
            print("\nusage: ./replay.py <name> [--repeat N]   (./replay.py --help for all options)")
        return 0
    path = _resolve(args.recording)
    if path is None:
        return 2
    actions_json = os.path.join(path, "actions.json") if os.path.isdir(path) else path
    problems = check_actions_file(actions_json)
    if problems:
        print(f"{actions_json} has {len(problems)} problem(s):")
        for line in problems:
            print("  - " + line)
        return 2
    rec = Recording.load(path)
    package = _package(rec, args.package)
    if args.reset and not package:
        print("--reset needs a package: pass --package, or record a launch step")
        return 2

    if not args.serial:
        from adb import list_devices
        devices = list_devices()
        if len(devices) == 1:
            args.serial = devices[0]
            import config
            config.ARGS.serial = args.serial   # adb() reads the serial from here
        elif devices:
            print("several phones attached, pass --serial: " + ", ".join(devices))
            return 2

    import actions
    actions.set_sync(True)

    safe = re.sub(r"[^\w-]+", "_", rec.name).strip("_") or "run"
    out = args.out or str(RUNS_DIR / f"{safe}_{time.strftime('%Y%m%d_%H%M%S')}")
    os.makedirs(out, exist_ok=True)

    net = None
    if args.network:
        if rec.network_flows:
            from network_replayer import NetworkReplayer
            net = NetworkReplayer()
        else:
            print("--network: this recording has no network flows, running against the live backend")

    results = []
    for n in range(1, args.repeat + 1):
        if not args.quiet:
            print(f"run {n}/{args.repeat}: {rec.name}", flush=True)
        health = _device_ready(args.serial)
        if health is None:
            print("  device not reachable")
            results.append({"success": False, "steps": [], "failure":
                            {"step": 0, "action": "", "cause": "device_unavailable",
                             "detail": "no device online"}})
            break
        if args.reset:
            _reset(rec, package, args.reset)
        if net:
            net.start({"flows": rec.network_flows, "host_tokens": rec.network_host_tokens})
        try:
            result = _run_once(rec, args, os.path.join(out, f"run_{n:02d}"), package, health)
        finally:
            if net:
                net.stop()
        results.append(result)
        verdict = "pass" if result["success"] else "FAIL: " + result["failure"]["cause"]
        if not args.quiet:
            print(f"  {verdict}  ({result['duration_s']}s)", flush=True)
    if net:
        net.shutdown()

    passed = sum(r["success"] for r in results)
    failed_steps, causes = {}, {}
    for r in results:
        f = r.get("failure")
        if f:
            key = f"step {f['step']} {f['action']}".strip()
            failed_steps[key] = failed_steps.get(key, 0) + 1
            causes[f["cause"]] = causes.get(f["cause"], 0) + 1
    total, steps = _timings(results)
    summary = {"recording": rec.name, "runs": len(results), "passed": passed,
               "failed": len(results) - passed, "flaky": 0 < passed < len(results),
               "failed_steps": failed_steps, "causes": causes,
               "total": total, "steps": steps}
    with open(os.path.join(out, "summary.json"), "w") as f:
        json.dump(summary, f, indent=2)

    print(f"\n{passed}/{len(results)} passed" + ("  (flaky)" if summary["flaky"] else ""))
    if steps and not args.quiet:
        print(f"total {total['min_s']} / {total['median_s']} / {total['max_s']} s  (min / median / max)")
        for st in steps:
            print(f"  {st['step']}. {st['label'][:38]:<38} {st['min_ms']:>6} {st['median_ms']:>7.0f} {st['max_ms']:>6} ms")
    for key, count in failed_steps.items():
        print(f"  {count}x {key}")
    for cause, count in causes.items():
        print(f"  cause {cause}: {count}")
    print(f"results: {out}")
    if "device_unavailable" in causes:
        return 2
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
