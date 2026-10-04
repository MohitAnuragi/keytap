"""
User settings + keybindings loaded from <keytap folder>/data/settings.json.

Edit that file to override any default. Missing keys fall back to defaults.

Example data/settings.json:
{
    "keybindings": {
        "back": "b",
        "home": "h"
    },
    "capture": {
        "bitrate": "4000000"
    }
}
"""

import json
import os
from pathlib import Path

# ── Defaults ─────────────────────────────────────────────────────────────────

_DEFAULTS = {
    # Keybindings: action -> key string (case-insensitive, e.g. "b", "space", "Tab")
    # Special: "shift+shift" means double-tap Shift (palette trigger)
    "keybindings": {
        "quit":           "Escape",
        "palette":        "shift+shift",   # double-tap Shift
        "tap":            "space",
        "back":           "b",
        "home":           "h",
        "recents":        "r",
        "scroll_up":      "w",
        "scroll_down":    "s",
        "element_mode":   "e",
        "toggle_bounds":  "Tab",
        "cancel":         "BackSpace",
    },
    # Capture pipeline
    "capture": {
        "bitrate":        "20000000",
        "low_latency":    False,
        "fps_cap":        0,        # 0 = no cap (native device fps)
        "encode_scale":   1.0,      # screenrecord --size scale vs native resolution
    },
    # Display / grid (can also be set via CLI args, args take priority)
    "display": {
        "height":         800,
        "cols":           10,
        "rows":           32,
        "cursor_mode":    True,   # True = arrow-key cursor, False = 2-letter labels
        "cursor_jump":    5,      # Shift+arrow jumps this many cells
    },
}

# ── Settings file path ───────────────────────────────────────────────────────

from keytap.core.paths import DATA_DIR as SETTINGS_DIR, SETTINGS_FILE


def _deep_merge(base, override):
    """Merge override into base, returning new dict. Non-destructive."""
    result = dict(base)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def _load():
    if not SETTINGS_FILE.exists():
        return dict(_DEFAULTS)
    try:
        with open(SETTINGS_FILE) as f:
            user = json.load(f)
        return _deep_merge(_DEFAULTS, user)
    except Exception as e:
        print(f"[keytap] warning: could not load {SETTINGS_FILE}: {e}")
        return dict(_DEFAULTS)


def save_defaults():
    """Write default settings file if it doesn't exist yet."""
    SETTINGS_DIR.mkdir(exist_ok=True)
    if not SETTINGS_FILE.exists():
        with open(SETTINGS_FILE, "w") as f:
            json.dump(_DEFAULTS, f, indent=4)
        print(f"[keytap] created default settings at {SETTINGS_FILE}")


# ── Public API ────────────────────────────────────────────────────────────────

_settings = _load()


def get(section, key, fallback=None):
    """Get a single setting value. E.g. get('keybindings', 'back') -> 'b'"""
    return _settings.get(section, {}).get(key, fallback)


def section(name):
    """Get an entire section as a dict. E.g. section('keybindings')"""
    return dict(_settings.get(name, {}))


def save_section(section_name, updates):
    """Persist updates to one section and refresh in-memory settings."""
    SETTINGS_DIR.mkdir(exist_ok=True)
    try:
        with open(SETTINGS_FILE) as f:
            on_disk = json.load(f)
    except Exception:
        on_disk = {}
    on_disk.setdefault(section_name, {}).update(updates)
    with open(SETTINGS_FILE, "w") as f:
        json.dump(on_disk, f, indent=4)
    global _settings
    _settings.setdefault(section_name, {}).update(updates)


def reload():
    """Reload settings from disk (useful if user edited file at runtime)."""
    global _settings
    _settings = _load()


# ── Keybindings convenience ──────────────────────────────────────────────────

class Keybindings:
    """Resolved keybindings. Access via kb.back, kb.home, etc."""

    def __init__(self):
        self._map = section("keybindings")

    def key_for(self, action):
        """Return the key string for an action, or None."""
        return self._map.get(action)

    def action_for(self, key_str):
        """Return action name for a pressed key, or None.
        key_str should be normalized: e.g. 'b', 'space', 'Escape'.
        Does NOT handle double-shift - caller manages that.
        """
        key_lower = key_str.lower()
        for action, bound_key in self._map.items():
            if bound_key.lower() == "shift+shift":
                continue  # special case handled by caller
            if bound_key.lower() == key_lower:
                return action
        return None

    # Shortcut properties for the most-used bindings
    @property
    def quit(self):      return self._map.get("quit", "Escape")
    @property
    def tap(self):       return self._map.get("tap", "space")
    @property
    def back(self):      return self._map.get("back", "b").lower()
    @property
    def home(self):      return self._map.get("home", "h").lower()
    @property
    def recents(self):   return self._map.get("recents", "r").lower()
    @property
    def scroll_up(self): return self._map.get("scroll_up", "w").lower()
    @property
    def scroll_down(self): return self._map.get("scroll_down", "s").lower()
    @property
    def element_mode(self): return self._map.get("element_mode", "e").lower()
    @property
    def toggle_bounds(self): return self._map.get("toggle_bounds", "Tab")
    @property
    def cancel(self):    return self._map.get("cancel", "BackSpace")


kb = Keybindings()
