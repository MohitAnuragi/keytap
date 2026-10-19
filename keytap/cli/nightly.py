"""
Unattended overnight run of test suites, with a morning report.

  ./nightly.py nightly.json              run the night (config format: keytap/watchdog/supervisor.py)
  ./nightly.py nightly.json --dry-run    list what would run and what is skipped, touches no phone

exit code:  0 everything passed   1 a test failed   2 the night was aborted (phone, battery, time) or bad config
"""

import argparse
import os
import subprocess
import sys

from keytap.watchdog.supervisor import load_config, run_night


def main():
    ap = argparse.ArgumentParser(prog="nightly.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("config", nargs="?", default="nightly.json")
    ap.add_argument("--serial", help="adb serial, overrides the config")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    try:
        cfg = load_config(args.config)
        if args.serial:
            cfg["serial"] = args.serial
        if not args.dry_run:   # the Mac must not sleep with the night half done
            subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
        return run_night(cfg, dry_run=args.dry_run)
    except (OSError, ValueError) as e:
        print(e)
        return 2


if __name__ == "__main__":
    sys.exit(main())
