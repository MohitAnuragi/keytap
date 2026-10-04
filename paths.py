"""
Where keytap keeps its data: <keytap folder>/data (gitignored), or $KEYTAP_DATA_DIR if set.
On first import, data from the old locations (~/.keytap/ and the loose ~/.keytap*.* files) is
moved here, so nothing is lost and nothing is left scattered in the home folder.
"""

import os
import shutil
from pathlib import Path

ROOT     = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("KEYTAP_DATA_DIR") or ROOT / "data")

RECORDINGS_DIR = DATA_DIR / "recordings"
TRACES_DIR     = DATA_DIR / "traces"
RUNS_DIR       = DATA_DIR / "runs"
HEAPS_DIR      = DATA_DIR / "heaps"
HPROFS_DIR     = DATA_DIR / "hprofs"
SCRCPY_DIR     = DATA_DIR / "scrcpy"
SETTINGS_FILE  = DATA_DIR / "settings.json"
THEME_FILE     = DATA_DIR / "theme.json"
DEEPLINKS_FILE = DATA_DIR / "deeplinks.json"
LOG_FILE       = DATA_DIR / "debug.log"
TMP_VIDEO      = DATA_DIR / "tmp_screen.mp4"


def _move(old: Path, new: Path):
    if old.exists() and not new.exists():
        new.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(old), str(new))


def _migrate():
    home = Path.home()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    legacy = home / ".keytap"
    if legacy.is_dir() and legacy.resolve() != DATA_DIR.resolve():
        for item in legacy.iterdir():
            _move(item, DATA_DIR / item.name)
        try:
            legacy.rmdir()          # only succeeds when nothing was left behind
        except OSError:
            pass
    _move(home / ".keytap_deeplinks.json", DEEPLINKS_FILE)
    _move(home / ".keytap.json", THEME_FILE)
    _move(home / ".keytap_debug.log", LOG_FILE)


try:
    _migrate()
except Exception:                   # never block startup over a failed move
    pass
