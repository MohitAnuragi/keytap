"""
LogcatCapture: background logcat reader with per-tag filtering.
Parses threadtime format into structured events timestamped relative to start().
"""

import logging
import re
import subprocess
import threading
import time
from typing import List, Optional

from config import ADB_PATH

_LOG = logging.getLogger(__name__)

# threadtime format: "MM-DD HH:MM:SS.mmm  PID  TID L TAG  : message"
_LOGCAT_RE = re.compile(
    r'^\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\.\d+\s+\d+\s+\d+\s+([A-Z])\s+(.+?)\s*:\s*(.*)'
)

# Sensible defaults — crashes, ANRs, jank only. No network tags (too noisy).
# Add "OkHttp:D" / "Retrofit:D" to DEFAULT_TAGS below for network logs.
DEFAULT_TAGS = [
    "AndroidRuntime:E",   # crashes / uncaught exceptions
    "ActivityManager:W",  # activity lifecycle warnings, ANRs
    "Choreographer:W",    # frame drops / jank (>16ms frames)
]


class LogcatCapture:

    def __init__(self):
        self._events: List[dict] = []
        self._proc = None
        self._thread = None
        self._start_t = 0.0
        self._running = False
        self._lock = threading.Lock()

    def start(self, serial: Optional[str] = None, tags: Optional[List[str]] = None):
        """Start background logcat capture.

        tags: list of "TAG:LEVEL" strings e.g. ["OkHttp:D", "MyApp:V"].
              None = use DEFAULT_TAGS.
              [] = capture everything (*:V) — warning: very noisy.
        """
        self._events.clear()
        self._start_t = time.time()
        self._running = True

        active_tags = DEFAULT_TAGS if tags is None else tags

        cmd = [ADB_PATH]
        if serial:
            cmd += ["-s", serial]
        # -T 1: skip old buffered entries; -v threadtime: structured timestamps
        cmd += ["logcat", "-v", "threadtime", "-T", "1"]
        if active_tags:
            cmd += active_tags + ["*:S"]   # silence anything not in tags list

        _LOG.debug("logcat start: %s", " ".join(cmd))
        self._proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        self._thread = threading.Thread(target=self._read, daemon=True)
        self._thread.start()

    def _read(self):
        try:
            for line in self._proc.stdout:
                if not self._running:
                    break
                line = line.rstrip()
                if not line:
                    continue
                t_ms = int((time.time() - self._start_t) * 1000)
                m = _LOGCAT_RE.match(line)
                if m:
                    level = m.group(1)
                    tag   = m.group(2).strip()
                    msg   = m.group(3)
                else:
                    level, tag, msg = "?", "?", line

                with self._lock:
                    self._events.append({
                        "t":     t_ms,
                        "type":  "log",
                        "level": level,
                        "tag":   tag,
                        "msg":   msg,
                    })
        except Exception:
            pass

    def stop(self) -> List[dict]:
        """Stop capture and return all collected log events."""
        self._running = False
        if self._proc:
            try:
                self._proc.terminate()
            except Exception:
                pass
            self._proc = None
        with self._lock:
            events = list(self._events)
        _LOG.debug("logcat_capture: %d events collected", len(events))
        return events
