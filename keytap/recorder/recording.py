"""
Data structures for recorded action sequences.
"""
import json
import os
import shutil
import time
from dataclasses import dataclass, field, asdict
from typing import List, Optional

from keytap.core.paths import RECORDINGS_DIR as _RECORDINGS_DIR

RECORDINGS_DIR = str(_RECORDINGS_DIR)


def _compact(d):
    """Strip None values from a dict (shallow)."""
    return {k: v for k, v in d.items() if v is not None}


@dataclass
class ElementSelector:
    resource_id: str = ""
    content_desc: str = ""
    text: str = ""
    class_name: str = ""
    index: Optional[int] = None   # 0-based position among on-screen matches; set only when several match


@dataclass
class Action:
    t: int           # ms since recording start
    type: str        # tap | element_tap | scroll_to | assert | run | swipe | input_text | launch | launch_deeplink | keyevent | wait | force_stop | clear_data | permission | dev_option
    # tap
    x: Optional[int] = None
    y: Optional[int] = None
    # element_tap
    selector: Optional[ElementSelector] = None
    fallback_coords: Optional[List[int]] = None
    reliable: bool = True
    # swipe
    x1: Optional[int] = None
    y1: Optional[int] = None
    x2: Optional[int] = None
    y2: Optional[int] = None
    duration: Optional[int] = None
    # input_text
    text: Optional[str] = None
    # launch | force_stop | clear_data
    package: Optional[str] = None
    # launch_deeplink
    url: Optional[str] = None
    # keyevent; label also names a dev_option
    keycode: Optional[int] = None
    label: Optional[str] = None
    # permission (package + permission, or "*" for all) and dev_option: the state it was set to
    permission: Optional[str] = None
    state: Optional[str] = None
    # replay options, hand-edited: element wait in ms (element_tap, default 10s), extra attempts
    # when a step fails (default 0), scroll_to direction (down | up)
    timeout_ms: Optional[int] = None
    retries: Optional[int] = None
    direction: Optional[str] = None
    # assert step: check name (see assertions.py), value to compare, soft = warn instead of fail
    check: Optional[str] = None
    value: Optional[str] = None
    soft: Optional[bool] = None
    # element_tap only: skip the step when the element does not show up within timeout_ms
    optional: Optional[bool] = None
    # run step (expanded before replay, see expand.py): flow folder and its params
    flow: Optional[str] = None
    params: Optional[dict] = None
    # set while loading: secret input is masked in logs; origin names the flow step it came from
    secret: Optional[bool] = None
    origin: Optional[str] = None

    def to_dict(self):
        d = asdict(self)
        if self.selector:
            d["selector"] = _compact(asdict(self.selector))
        return _compact(d)


@dataclass
class Recording:
    name: str
    recorded_at: str
    serial: str
    device_resolution: List[int]
    actions: List[Action] = field(default_factory=list)
    network_flows: List[dict] = field(default_factory=list)
    network_host_tokens: dict = field(default_factory=dict)
    # correlated per-frame timeline (actions + logcat + network, sorted by t)
    timeline: List[dict] = field(default_factory=list)
    video_path: Optional[str] = None   # relative path to screen.mp4 inside recording dir

    def save(self, screenshots: Optional[dict] = None,
             video_src_path: Optional[str] = None) -> str:
        """
        Saves recording as a directory:
          <keytap folder>/data/recordings/{name}_{ts}/
            actions.json
            network.json        (only if network_flows non-empty)
            timeline.json       (only if timeline non-empty)
            screen.mp4          (only if video_src_path provided)
        Returns the directory path.
        """
        os.makedirs(RECORDINGS_DIR, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        safe = "".join(c if c.isalnum() or c in "-_ " else "_" for c in self.name)
        dir_path = os.path.join(RECORDINGS_DIR, f"{safe}_{ts}")
        os.makedirs(dir_path, exist_ok=True)

        # Move video from temp location into recording dir
        if video_src_path and os.path.exists(video_src_path):
            dest = os.path.join(dir_path, "screen.mp4")
            shutil.move(video_src_path, dest)
            self.video_path = "screen.mp4"

        actions_data = {
            "name": self.name,
            "recorded_at": self.recorded_at,
            "serial": self.serial,
            "device_resolution": self.device_resolution,
            "actions": [a.to_dict() for a in self.actions],
        }
        if self.video_path:
            actions_data["video_path"] = self.video_path
        with open(os.path.join(dir_path, "actions.json"), "w") as f:
            json.dump(actions_data, f, indent=2)

        if self.network_flows:
            network_data = {
                "host_tokens": self.network_host_tokens,
                "flows": self.network_flows,
            }
            with open(os.path.join(dir_path, "network.json"), "w") as f:
                json.dump(network_data, f, indent=2)

        if self.timeline:
            with open(os.path.join(dir_path, "timeline.json"), "w") as f:
                json.dump({"events": self.timeline}, f, indent=2)

        return dir_path

    @classmethod
    def load(cls, path: str) -> "Recording":
        """
        Loads a recording from:
          - a directory (new format): reads actions.json + network.json
          - a .json file (legacy format): reads single file
        """
        if os.path.isdir(path):
            return cls._load_dir(path)
        return cls._load_legacy(path)

    @classmethod
    def _load_dir(cls, dir_path: str) -> "Recording":
        with open(os.path.join(dir_path, "actions.json")) as f:
            data = json.load(f)

        network_path = os.path.join(dir_path, "network.json")
        network_flows: List[dict] = []
        network_host_tokens: dict = {}
        if os.path.exists(network_path):
            with open(network_path) as f:
                net = json.load(f)
            network_flows = net.get("flows", [])
            network_host_tokens = net.get("host_tokens", {})

        return cls(
            name=data["name"],
            recorded_at=data["recorded_at"],
            serial=data.get("serial", ""),
            device_resolution=data.get("device_resolution", [0, 0]),
            actions=cls._parse_actions(data.get("actions", [])),
            network_flows=network_flows,
            network_host_tokens=network_host_tokens,
        )

    @classmethod
    def _load_legacy(cls, path: str) -> "Recording":
        with open(path) as f:
            data = json.load(f)
        return cls(
            name=data["name"],
            recorded_at=data["recorded_at"],
            serial=data.get("serial", ""),
            device_resolution=data.get("device_resolution", [0, 0]),
            actions=cls._parse_actions(data.get("actions", [])),
            network_flows=data.get("network_flows", []),
            network_host_tokens=data.get("network_host_tokens", {}),
        )

    @staticmethod
    def _parse_actions(raw: list) -> "List[Action]":
        actions = []
        for a in raw:
            sel = ElementSelector(**a["selector"]) if a.get("selector") else None
            actions.append(Action(
                t=a["t"],
                type=a["type"],
                x=a.get("x"), y=a.get("y"),
                selector=sel,
                fallback_coords=a.get("fallback_coords"),
                reliable=a.get("reliable", True),
                x1=a.get("x1"), y1=a.get("y1"),
                x2=a.get("x2"), y2=a.get("y2"),
                duration=a.get("duration"),
                text=a.get("text"),
                package=a.get("package"),
                url=a.get("url"),
                keycode=a.get("keycode"),
                label=a.get("label"),
                permission=a.get("permission"),
                state=a.get("state"),
                timeout_ms=a.get("timeout_ms"),
                retries=a.get("retries"),
                direction=a.get("direction"),
                check=a.get("check"), value=a.get("value"), soft=a.get("soft"),
                optional=a.get("optional"),
                flow=a.get("flow"), params=a.get("params"),
                secret=a.get("secret"), origin=a.get("origin"),
            ))
        return actions

    @classmethod
    def list_all(cls) -> List[dict]:
        """Return [{path, name, recorded_at, action_count, has_network, has_timeline}] sorted newest first."""
        if not os.path.isdir(RECORDINGS_DIR):
            return []
        results = []
        for entry in os.listdir(RECORDINGS_DIR):
            full = os.path.join(RECORDINGS_DIR, entry)
            try:
                if os.path.isdir(full):
                    actions_path = os.path.join(full, "actions.json")
                    if not os.path.exists(actions_path):
                        continue
                    with open(actions_path) as f:
                        d = json.load(f)
                    results.append({
                        "path": full,
                        "name": d.get("name", entry),
                        "recorded_at": d.get("recorded_at", ""),
                        "action_count": len(d.get("actions", [])),
                        "has_network": os.path.exists(os.path.join(full, "network.json")),
                        "has_timeline": os.path.exists(os.path.join(full, "timeline.json")),
                    })
                elif entry.endswith(".json"):
                    # legacy single-file recording
                    with open(full) as f:
                        d = json.load(f)
                    results.append({
                        "path": full,
                        "name": d.get("name", entry),
                        "recorded_at": d.get("recorded_at", ""),
                        "action_count": len(d.get("actions", [])),
                        "has_network": bool(d.get("network_flows")),
                        "has_timeline": False,
                    })
            except Exception:
                pass
        results.sort(key=lambda r: r["recorded_at"], reverse=True)
        return results


def action_label(action: "Action") -> str:
    """Short human-readable label for an action (status bar, step overlay, viewers)."""
    t = action.type
    if t == "element_tap":
        sel = action.selector
        name = sel and (sel.resource_id or sel.content_desc or sel.text)
        if name and "/" in name:
            name = name.split("/")[-1]  # strip package prefix from resource_id
        return f"element_tap: {name or '?'}"
    elif t == "scroll_to":
        sel = action.selector
        name = sel and (sel.resource_id or sel.content_desc or sel.text)
        if name and "/" in name:
            name = name.split("/")[-1]
        return f"scroll_to: {name or '?'} ({action.direction or 'down'})"
    elif t == "assert":
        sel = action.selector
        name = (sel and (sel.resource_id or sel.content_desc or sel.text)) or ""
        if "/" in name:
            name = name.split("/")[-1]
        value = f" {action.value!r}" if action.value is not None else ""
        return f"assert {action.check} {name}{value}".replace("  ", " ").strip()
    elif t == "run":
        return f"run: {action.flow}"
    elif t == "tap":
        return f"tap ({action.x}, {action.y})"
    elif t == "swipe":
        return f"swipe ({action.x1},{action.y1})->({action.x2},{action.y2})"
    elif t == "input_text":
        preview = "***" if action.secret else repr((action.text or "")[:20])
        return f"input_text: {preview}"
    elif t == "launch":
        pkg = (action.package or "").split(".")[-1]
        return f"launch: {pkg}"
    elif t == "launch_deeplink":
        return f"deeplink: {(action.url or '')[:30]}"
    elif t == "keyevent":
        return f"keyevent: {action.label or action.keycode}"
    elif t == "wait":
        return f"wait: {action.duration}ms"
    elif t == "permission":
        what = "ALL" if action.permission == "*" else (action.permission or "?").split(".")[-1]
        return f"permission: {what} -> {(action.state or '?').lower()}"
    elif t == "dev_option":
        return f"dev_option: {action.label} -> {action.state}"
    elif t == "force_stop":
        return f"force_stop: {(action.package or '').split('.')[-1]}"
    elif t == "clear_data":
        return f"clear_data: {(action.package or '').split('.')[-1]}"
    return t


# ── actions.json validation (used after hand-editing in vim) ──────────────────

_REQUIRED = {
    "tap":             ("x", "y"),
    "swipe":           ("x1", "y1", "x2", "y2"),
    "input_text":      ("text",),
    "launch":          ("package",),
    "force_stop":      ("package",),
    "clear_data":      ("package",),
    "launch_deeplink": ("url",),
    "keyevent":        ("keycode",),
    "wait":            ("duration",),
    "element_tap":     ("selector",),
    "scroll_to":       ("selector",),
    "assert":          ("check",),
    "run":             ("flow",),
    "permission":      ("package", "permission", "state"),
    "dev_option":      ("label", "state"),
}
_SELECTOR_KEYS = {"resource_id", "content_desc", "text", "class_name", "index"}


def _is_count(v):
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


def check_actions_file(path: str) -> List[str]:
    """Problems that would break loading or replaying this actions.json. Empty list = valid."""
    try:
        with open(path) as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        return [f"not valid JSON: {e.msg} (line {e.lineno}, column {e.colno})"]
    except OSError as e:
        return [f"cannot read file: {e}"]
    if not isinstance(data, dict):
        return ["top level must be a JSON object"]
    problems = []
    for key in ("name", "recorded_at"):
        if key not in data:
            problems.append(f'missing "{key}"')
    acts = data.get("actions")
    if not isinstance(acts, list):
        return problems + ['"actions" must be a list']
    prev_t = 0
    for i, a in enumerate(acts, 1):
        where = f"action {i}"
        if not isinstance(a, dict):
            problems.append(f"{where}: must be an object")
            continue
        t, typ = a.get("t"), a.get("type")
        if not isinstance(t, int) or isinstance(t, bool) or t < 0:
            problems.append(f'{where}: "t" must be a whole number of milliseconds')
        elif t < prev_t:
            problems.append(f'{where}: "t" ({t}) is earlier than the previous action ({prev_t})')
        else:
            prev_t = t
        if typ not in _REQUIRED:
            problems.append(f'{where}: unknown type "{typ}" (use one of: {", ".join(sorted(_REQUIRED))})')
            continue
        for field_name in _REQUIRED[typ]:
            if a.get(field_name) in (None, ""):
                problems.append(f'{where} ({typ}): needs "{field_name}"')
        if typ == "permission" and a.get("state") not in (None, "GRANTED", "DENIED"):
            problems.append(f'{where} (permission): "state" must be "GRANTED" or "DENIED"')
        if typ == "assert" and a.get("check"):
            from keytap.runner.assertions import CHECKS
            spec = CHECKS.get(a["check"])
            if spec is None:
                problems.append(f'{where}: unknown check "{a["check"]}" (use one of: {", ".join(sorted(CHECKS))})')
            else:
                for field_name in spec["needs"]:
                    if a.get(field_name) in (None, "", {}):
                        problems.append(f'{where} (assert {a["check"]}): needs "{field_name}"')
        if typ == "run" and a.get("params") is not None and not isinstance(a["params"], dict):
            problems.append(f'{where} (run): "params" must be an object')
        if typ in ("element_tap", "scroll_to", "assert") and isinstance(a.get("selector"), dict):
            sel = a["selector"]
            extra = set(sel) - _SELECTOR_KEYS
            if extra:
                problems.append(f'{where}: unknown selector keys {sorted(extra)} (allowed: {sorted(_SELECTOR_KEYS)})')
            if "index" in sel and not _is_count(sel["index"]):
                problems.append(f'{where}: selector "index" must be a whole number, 0 or more')
            fb = a.get("fallback_coords")
            has_fallback = typ == "element_tap" and isinstance(fb, list) and len(fb) == 2
            if not (sel.get("resource_id") or sel.get("content_desc") or sel.get("text")) and not has_fallback:
                hint = ' (or "fallback_coords": [x, y] to tap by position)' if typ == "element_tap" else ""
                problems.append(f"{where}: selector needs resource_id, content_desc or text{hint}")
        elif typ in ("element_tap", "scroll_to", "assert") and a.get("selector") is not None:
            problems.append(f'{where}: "selector" must be an object')
        for opt in ("timeout_ms", "retries"):
            if opt in a and not _is_count(a[opt]):
                problems.append(f'{where}: "{opt}" must be a whole number, 0 or more')
        if a.get("direction") not in (None, "up", "down"):
            problems.append(f'{where}: "direction" must be "up" or "down"')
    return problems


if __name__ == "__main__":
    import sys
    if len(sys.argv) == 3 and sys.argv[1] == "--check":
        issues = check_actions_file(sys.argv[2])
        if issues:
            print(f"actions.json has {len(issues)} problem(s):")
            for line in issues:
                print("  - " + line)
            sys.exit(1)
        print("actions.json is valid.")
    else:
        print("usage: python -m keytap.recorder.recording --check <actions.json>   (from the keytap folder)")
        sys.exit(2)
