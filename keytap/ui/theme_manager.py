import json
import os

from keytap.core.paths import THEME_FILE

_THEMES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "themes")
_CONFIG_PATH = str(THEME_FILE)
_DEFAULT = "dracula"

# Fallback values (current dark theme)
_FALLBACK = {
    "bg":        "#1c1c1e",
    "bg_input":  "#2c2c2e",
    "fg":        "#f0f0f0",
    "fg_dim":    "#888888",
    "accent":    "#00c864",
    "sel_bg":    "#3a3a3c",
    "err":       "#ff4444",
    "warn":      "#ff8c35",
    "info":      "#4fc3f7",
    "graph_bg":  "#111111",
    "grid_line": "#333333",
    "bar_bg":    "#2a2a2a",
}


def _read_config():
    try:
        with open(_CONFIG_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def load_theme(name):
    path = os.path.join(_THEMES_DIR, f"{name}.json")
    try:
        with open(path) as f:
            data = json.load(f)
        return {**_FALLBACK, **data}
    except Exception:
        return dict(_FALLBACK)


def active_name():
    return _read_config().get("theme", _DEFAULT)


def get_active_theme():
    return load_theme(active_name())


def set_theme(name):
    cfg = _read_config()
    cfg["theme"] = name
    with open(_CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)


def list_themes():
    try:
        return sorted(f[:-5] for f in os.listdir(_THEMES_DIR) if f.endswith(".json"))
    except Exception:
        return []
