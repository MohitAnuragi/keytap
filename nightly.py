#!/opt/homebrew/bin/python3
"""nightly.py: the command lives in keytap.cli.nightly."""
import sys

from keytap.cli.nightly import main

if __name__ == "__main__":
    sys.exit(main())
