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
import sys
import time

from keytap.runner import engine
from keytap.runner.engine import print_list as _print_list, resolve_recording as _resolve   # also used by suite.py and tests


EPILOG = """examples:
  ./replay.py                                  list the recordings
  ./replay.py --dump                           what is on the phone's screen now (find ids for selectors)
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

failure causes:  device_unavailable, load_error, app_crash, wrong_app_in_foreground, assertion_failed,
                 element_not_found, action_failed

many recordings with assertions and login handling: ./suite.py --help

exit code:  0 all runs passed   1 a step failed   2 phone or uiautomator2 unreachable, bad recording or name
"""


def _dump(args):
    """Print what is on the phone's screen: app, activity, texts and the tappable elements as
    selectors that can be pasted into actions.json. Read-only, nothing is tapped."""
    from keytap.runner.failure_bundle import _FOCUS_RE, _screen
    from keytap.core.utils import adb_shell
    if not engine.pick_serial(args):
        return 2
    try:
        import uiautomator2 as u2
        dev = u2.connect(args.serial) if args.serial else u2.connect()
        xml = dev.dump_hierarchy()
    except Exception as e:
        print(f"cannot read the screen: {type(e).__name__}: {e}")
        return 2
    m = _FOCUS_RE.search(adb_shell("dumpsys window | grep mCurrentFocus", args.serial))
    screen = _screen(xml if isinstance(xml, str) else xml.decode(errors="replace"))
    print(f"screen: {m.group(1)} / {m.group(2)}" if m else "screen: unknown app")
    print("texts: " + " | ".join(t if len(t) <= 40 else t[:40] + "..." for t in screen["texts"]))
    print("tappable (selectors for actions.json; index only when several share the identifier):")
    for item in screen["clickable"]:
        print("  " + json.dumps(item, ensure_ascii=False))
    if not screen["clickable"]:
        print("  (none found)")
    return 0


def main():
    ap = argparse.ArgumentParser(
        prog="replay.py", formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Headless replay of a keytap recording on a connected phone.",
        epilog=EPILOG)
    ap.add_argument("recording", nargs="?",
                    help='part of a recording name, its folder name, "latest", or a path. '
                         "With no name, the recordings are listed.")
    ap.add_argument("--list", action="store_true", help="list the recordings and exit")
    ap.add_argument("--dump", action="store_true",
                    help="print what is on the phone's screen now: app, texts and tappable elements "
                         "as selectors to paste into actions.json. Read-only, taps nothing.")
    run = ap.add_argument_group("how to run")
    run.add_argument("--repeat", type=int, default=1, metavar="N",
                     help="run the recording N times in a row (default: 1)")
    run.add_argument("--serial", metavar="ID",
                     help="adb serial of the phone (default: the only attached phone)")
    run.add_argument("--mode", choices=["fast", "timed"], default="fast",
                     help="timed: keep the pauses of the recording, each capped at 10s. "
                          "fast: 0.3s between steps (default: fast)")
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

    from keytap.runner.expand import LoadError, load_recording
    from keytap.core.paths import RUNS_DIR
    from keytap.recorder.recording import Recording, check_actions_file

    if args.dump:
        return _dump(args)
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

    def load():
        return load_recording(path)[0]

    try:
        rec = load()
    except LoadError as e:
        print(f"cannot load {path}: {e}")
        return 2
    package = engine.package_of(rec, args.package)
    if args.reset and not package:
        print("--reset needs a package: pass --package, or record a launch step")
        return 2
    if not engine.pick_serial(args):
        return 2

    from keytap.device import actions
    actions.set_sync(True)

    safe = re.sub(r"[^\w-]+", "_", rec.name).strip("_") or "run"
    out = args.out or str(RUNS_DIR / f"{safe}_{time.strftime('%Y%m%d_%H%M%S')}")

    net = None
    if args.network:
        if rec.network_flows:
            from keytap.recorder.network_replayer import NetworkReplayer
            net = NetworkReplayer()
        else:
            print("--network: this recording has no network flows, running against the live backend")

    opts = engine.Options(serial=args.serial, mode=args.mode, wait=args.wait,
                          retries=args.retries, quiet=args.quiet)
    summary, _ = engine.run_recording(load, opts, out, repeat=args.repeat, package=package,
                                      reset=args.reset, net=net)
    if net:
        net.shutdown()
    engine.print_summary(summary, out, args.quiet)
    return engine.exit_code(summary)


if __name__ == "__main__":
    sys.exit(main())
