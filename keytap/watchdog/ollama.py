"""
Make sure the local model server (Ollama) is up before a run that wants the model.

  ensure(model)  ->  (True, "")  or  (False, "why it cannot be used")

It starts `ollama serve` if nothing answers, checks that the model is already pulled (it never downloads
one by itself, they are several GB) and loads the model into memory so the first real call is not slow.
A run that cannot get the model carries on without it.
"""

import json
import os
import shutil
import subprocess
import time
import urllib.request

from keytap.core.paths import DATA_DIR

BASE = "http://localhost:11434"


def _get(path, timeout=2):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        return json.load(r)


def _models():
    try:
        return [m["name"] for m in _get("/api/tags")["models"]]
    except Exception:
        return None   # not answering


def ensure(model, say=print, start_wait_s=40):
    names = _models()
    if names is None:
        exe = shutil.which("ollama") or "/opt/homebrew/bin/ollama"
        if not os.path.exists(exe):
            return False, "Ollama is not installed"
        say("starting Ollama...")
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(DATA_DIR / "ollama.log", "ab") as log:
            subprocess.Popen([exe, "serve"], stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True)
        deadline = time.time() + start_wait_s
        while names is None and time.time() < deadline:
            time.sleep(1)
            names = _models()
        if names is None:
            return False, f"Ollama did not answer within {start_wait_s}s (see {DATA_DIR / 'ollama.log'})"
    if model not in names and f"{model}:latest" not in names:
        return False, f"model {model} is not pulled. Run: ollama pull {model}"
    try:   # an empty prompt loads the model and keeps it in memory
        req = urllib.request.Request(BASE + "/api/generate", json.dumps({"model": model, "prompt": "", "keep_alive": "30m"}).encode(),
                                     {"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=180).read()
    except Exception as e:
        say(f"could not preload {model} ({type(e).__name__}), it will load on first use")
    return True, ""
