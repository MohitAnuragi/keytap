"""
Loading actions.json with `run` steps expanded and placeholders filled in.

  {"type": "run", "flow": "flows/login", "params": {"user": "qa1"}}   include another folder's actions

Placeholders in any string of an action:
  {{name}}       a param given by the run step (or the test's login_params)
  {{ts}}         a timestamp, the same for the whole run, different each run (unique test text)
  {{package}}    the "package" of the suite's suite.json, unless a run step passes its own package param
  {{env:NAME}}   an environment variable, so credentials never live in files. An input_text that
                 used one is marked secret and shown as *** in logs.

"when": {selector} on any step (or run step) runs it only if that element shows up within
"when_timeout_ms" (default 8000), else skips it. On a run step it guards the whole flow, decided once
when the flow starts, so the flow's own taps may make the element vanish.

Flows are folders with an actions.json, found relative to the suite folder (the nearest parent
holding suite.json). Includes may nest 3 deep; loops are an error.
"""

import json
import os
import re
import time

from keytap.recorder.recording import Recording

MAX_DEPTH = 3
_VAR = re.compile(r"\{\{([^{}]+)\}\}")


class LoadError(Exception):
    pass


def find_root(path):
    """Nearest parent folder (or the folder itself) that holds suite.json, or None."""
    p = os.path.abspath(path)
    p = p if os.path.isdir(p) else os.path.dirname(p)
    while True:
        if os.path.isfile(os.path.join(p, "suite.json")):
            return p
        parent = os.path.dirname(p)
        if parent == p:
            return None
        p = parent


def new_vars():
    return {"ts": time.strftime("%Y%m%d-%H%M%S")}


def _subst(value, params, vars_, resolve_env, strict):
    """Fill placeholders in a value. Returns (new value, whether an env var was used)."""
    if isinstance(value, dict):
        out, used = {}, False
        for k, v in value.items():
            out[k], u = _subst(v, params, vars_, resolve_env, strict)
            used = used or u
        return out, used
    if isinstance(value, list):
        out, used = [], False
        for v in value:
            nv, u = _subst(v, params, vars_, resolve_env, strict)
            out.append(nv)
            used = used or u
        return out, used
    if not isinstance(value, str):
        return value, False
    used = False

    def fill(m):
        nonlocal used
        key = m.group(1).strip()
        if key.startswith("env:"):
            used = True
            if not resolve_env:
                return m.group(0)
            name = key[4:]
            if name not in os.environ:
                raise LoadError(f"environment variable {name} is not set (used as {{{{env:{name}}}}})")
            return os.environ[name]
        if key in params:
            return str(params[key])
        if key in vars_:
            return vars_[key]
        if strict:
            raise LoadError(f"unknown placeholder {{{{{key}}}}} (no param, and not ts or env:NAME)")
        return m.group(0)

    return _VAR.sub(fill, value), used


def _read(path):
    actions_json = os.path.join(path, "actions.json") if os.path.isdir(path) else path
    try:
        with open(actions_json) as f:
            return json.load(f), actions_json
    except (OSError, json.JSONDecodeError) as e:
        raise LoadError(f"cannot read {actions_json}: {e}")


def _flow_dir(flow, root):
    if root is None:
        raise LoadError(f'run step "{flow}": run steps need a suite folder (suite.json in a parent folder)')
    path = os.path.normpath(os.path.join(root, flow))
    if not os.path.isfile(os.path.join(path, "actions.json")):
        raise LoadError(f'run step: flow "{flow}" not found (expected {path}/actions.json)')
    return path


def _expand(path, root, params, vars_, resolve_env, strict, stack):
    data, _ = _read(path)
    name = os.path.basename(os.path.normpath(path))
    out, shift = [], 0
    for k, raw in enumerate(data.get("actions", []), 1):
        a, used_env = _subst(raw, params, vars_, resolve_env, strict)
        if used_env and a.get("type") == "input_text":
            a["secret"] = True
        if a.get("type") != "run":
            if stack:
                a["origin"] = f"{name} step {k}"
            a["t"] = a.get("t", 0) + shift
            if a.get("when"):
                a.setdefault("when_span", 1)
            out.append(a)
            continue
        flow = _flow_dir(a.get("flow", ""), root)
        if flow in stack or flow == os.path.normpath(path):
            chain = " -> ".join(os.path.basename(p) for p in stack + (os.path.normpath(path), flow))
            raise LoadError(f"flows include each other in a loop: {chain}")
        if len(stack) >= MAX_DEPTH:
            raise LoadError(f"flows nested deeper than {MAX_DEPTH}: {' -> '.join(os.path.basename(p) for p in stack)}")
        _, inner = _expand(flow, root, {**params, **(a.get("params") or {})}, vars_,
                           resolve_env, strict, stack + (os.path.normpath(path),))
        if a.get("when") and inner:
            if inner[0].get("when"):
                raise LoadError(f'run step "{a["flow"]}": has "when", and its first step has one too')
            inner[0].update(when=a["when"], when_span=len(inner), when_timeout_ms=a.get("when_timeout_ms"))
        start = a.get("t", 0) + shift
        for ia in inner:
            ia["t"] += start
            ia["origin"] = f"{ia['origin']} <- {name} step {k}"
        out.extend(inner)
        shift += max((ia["t"] for ia in inner), default=start) - start   # later steps move back by the flow's length
    return data, out


def load_recording(path, root=None, params=None, vars_=None, resolve_env=True, strict=True):
    """(Recording, header) for a folder or actions.json, run steps expanded, placeholders filled.
    header is the file's top level (name, tags, login, login_params, before_each, description).
    resolve_env=False and strict=False are for `suite.py check`, which has no secrets or params."""
    root = root or find_root(path)
    data, _ = _read(path)
    vars_ = vars_ if vars_ is not None else new_vars()
    if root:
        with open(os.path.join(root, "suite.json")) as f:
            if "package" in (suite := json.load(f)):
                vars_.setdefault("package", suite["package"])
    header, actions = _expand(path, root, params or {}, vars_, resolve_env, strict, ())
    rec = Recording(
        name=data.get("name", os.path.basename(os.path.normpath(path))),
        recorded_at=data.get("recorded_at", ""),
        serial=data.get("serial", ""),
        device_resolution=data.get("device_resolution", [0, 0]),
        actions=Recording._parse_actions(actions),
    )
    return rec, header
