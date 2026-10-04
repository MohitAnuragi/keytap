"""
ActionReplayer: executes a Recording on the connected device.

Modes:
  timed  - honor recorded timestamps (real-time replay)
  fast   - fixed 300ms gap between each action
  step   - advance manually by calling advance()
"""
import threading
import time

from keytap.device import actions
from keytap.device import lookup
from keytap.runner.assertions import CHECKS, Context
from keytap.recorder.recording import Action, ElementSelector, Recording, action_label


class ActionReplayer:
    FAST_DELAY_MS = 300
    RETRY_PAUSE_S = 1.0
    MAX_SCROLLS = 8
    MAX_RECORDED_WAIT_S = 60   # a long pause in the recording must not stall a nightly run

    def __init__(self):
        self.active = False
        self.mode = "timed"
        self._stop_event = threading.Event()
        self._step_event = threading.Event()
        self._on_status = None
        self._u2_dev = None
        self._u2_error = ""
        self._serial = None
        self._warn = ""                 # message of a soft assertion that failed in the current step
        self.extra_wait_s = 10.0        # keep looking this long after the recorded wait
        self._recorded_wait_s = 0.0     # wait still owed for the current step (its gap in the recording)
        self.default_retries = 0        # extra attempts when a step has no retries
        self.results = []               # one dict per step run: step, label, status, ms, retries, error
        self._current_step = 0   # 0-indexed; set before _wait_between so overlay reflects waiting step
        self._recording = None   # Recording being replayed; None when idle

    @property
    def current_step(self) -> int:
        return self._current_step

    @property
    def recording(self):
        return self._recording

    @property
    def u2_device(self):
        """The uiautomator2 connection of the last replay, or None."""
        return self._u2_dev

    # ── Public API ────────────────────────────────────────────────────────────

    def start(self, recording: Recording, mode: str, serial: str,
              on_status, on_done, device_resolution=None):
        self.mode = mode
        self.active = True
        self._recording = recording
        self._current_step = 0
        self.results = []
        self._stop_event.clear()
        self._step_event.clear()
        threading.Thread(
            target=self._run,
            args=(recording, serial, on_status, on_done, device_resolution),
            daemon=True,
        ).start()

    def advance(self):
        """Unblock one step in step mode."""
        self._step_event.set()

    def stop(self):
        self._stop_event.set()
        self._step_event.set()  # unblock a waiting step
        self.active = False

    # ── Replay loop ───────────────────────────────────────────────────────────

    def _run(self, recording, serial, on_status, on_done, device_resolution):
        self._on_status = on_status
        self._serial = serial
        actions.set_sync(True)   # each step finishes before the next starts
        total = len(recording.actions)

        if device_resolution and recording.device_resolution != list(device_resolution):
            on_status(
                f"[WARN] recorded on {recording.device_resolution}, "
                f"device is {list(device_resolution)} — coords may miss"
            )

        # Pre-connect u2 once for the whole replay if any step looks up elements
        self._u2_error = ""
        if any(a.type in ("element_tap", "scroll_to", "assert") for a in recording.actions):
            try:
                import uiautomator2 as u2
                self._u2_dev = u2.connect(serial) if serial else u2.connect()
            except Exception as e:
                self._u2_dev = None
                self._u2_error = f"{type(e).__name__}: {e}"
        else:
            self._u2_dev = None

        prev_t = 0
        for i, action in enumerate(recording.actions):
            if self._stop_event.is_set():
                on_status("replay stopped")
                self.active = False
                self._recording = None
                return

            self._current_step = i
            on_status(f"[REPLAY {i + 1}/{total}] {action_label(action)}")
            self._wait_between(action.t, prev_t)

            if self._stop_event.is_set():
                on_status("replay stopped")
                self.active = False
                self._recording = None
                return

            gap_s = min(max(action.t - prev_t, 0) / 1000, self.MAX_RECORDED_WAIT_S)
            # timed mode already slept the gap (up to 10s) before the step
            self._recorded_wait_s = gap_s - (min(gap_s, 10) if self.mode == "timed" else 0)
            prev_t = action.t

            retries = action.retries if action.retries is not None else self.default_retries
            started = time.time()
            ok, error = self._execute_safe(action, serial)
            attempt = 0
            while (not ok and attempt < retries and not self._stop_event.is_set()
                   and not error.startswith("uiautomator2 unavailable")):
                attempt += 1
                on_status(f"[RETRY {attempt}/{retries}] step {i + 1}: {error}")
                time.sleep(self.RETRY_PAUSE_S)
                ok, error = self._execute_safe(action, serial)
            warn, self._warn = self._warn, ""
            entry = {
                "step": i + 1, "label": action_label(action),
                "status": ("warn" if warn else "pass") if ok else "fail",
                "ms": int((time.time() - started) * 1000),
                "retries": attempt, "error": error or warn or None,
            }
            if action.origin:
                entry["origin"] = action.origin
            self.results.append(entry)
            if warn:
                on_status(f"[WARN] step {i + 1}: {warn}")
            if not ok:
                on_status(f"[REPLAY ERROR] step {i + 1}: {error}")
                self.active = False
                self._recording = None
                on_done(success=False, step=i + 1, error=error)
                return

        self.active = False
        self._recording = None
        on_status(f"replay done  ({total} actions)")
        on_done(success=True, step=total, error=None)

    def _wait_between(self, action_t_ms: int, prev_t_ms: int):
        if self.mode == "step":
            self._step_event.wait()
            self._step_event.clear()
        elif self.mode == "fast":
            time.sleep(self.FAST_DELAY_MS / 1000)
        else:  # timed
            gap_s = (action_t_ms - prev_t_ms) / 1000
            if gap_s > 0:
                time.sleep(min(gap_s, 10))  # cap at 10s to guard against huge pauses

    def _execute_safe(self, action: Action, serial: str):
        try:
            return self._execute(action, serial)
        except Exception as e:   # sync actions raise (adb timeout, missing adb) where threads swallowed
            return False, f"{action.type} failed: {type(e).__name__}: {e}"

    def _execute(self, action: Action, serial: str):
        """Dispatch one action. Returns (success, error_msg)."""
        t = action.type
        if t == "tap":
            actions.tap(action.x, action.y)
        elif t == "element_tap":
            return self._execute_element_tap(action, serial)
        elif t == "scroll_to":
            return self._execute_scroll_to(action)
        elif t == "assert":
            return self._execute_assert(action)
        elif t == "run":
            return False, "run step was not expanded (replay it through replay.py or suite.py, inside a suite folder)"
        elif t == "swipe":
            actions.swipe(action.x1, action.y1, action.x2, action.y2,
                          duration=action.duration or 300)
        elif t == "input_text":
            actions.input_text(action.text)
        elif t == "launch":
            actions.launch(action.package)
        elif t == "launch_deeplink":
            actions.launch_deeplink(action.url)
        elif t == "keyevent":
            actions.keyevent(action.keycode, action.label or "")
        elif t == "permission":
            actions.set_permission(action.package, action.permission, action.state)
        elif t == "dev_option":
            actions.set_dev_option(action.label, action.state)
        elif t == "force_stop":
            actions.force_stop(action.package)
        elif t == "clear_data":
            actions.clear_data(action.package)
        elif t == "wait":
            ms = action.duration or 0
            if ms > 0:
                time.sleep(ms / 1000)
        else:
            return False, f"unknown action type: {t}"
        return True, None

    def _unavailable(self) -> str:
        return f"uiautomator2 unavailable: {self._u2_error or 'not connected'}"

    @staticmethod
    def _describe(sel: ElementSelector) -> str:
        return sel.resource_id or sel.content_desc or sel.text or "?"

    def _budget(self, action: Action):
        """(recorded wait, total wait) in seconds for element taps and assertions: as long as the
        user waited in the recording, then extra_wait_s more; timeout_ms replaces both."""
        if action.timeout_ms:
            return 0.0, action.timeout_ms / 1000
        return self._recorded_wait_s, self._recorded_wait_s + self.extra_wait_s

    @staticmethod
    def _waited(recorded_s: float, timeout_s: float) -> str:
        return f"{recorded_s:.1f}s recorded + {timeout_s - recorded_s:g}s more" if recorded_s else f"{timeout_s:g}s"

    def _execute_assert(self, action: Action):
        """Poll the check until it holds or the wait runs out. A soft assertion only warns."""
        spec = CHECKS.get(action.check)
        if spec is None:
            return False, f"unknown check: {action.check}"
        if self._u2_dev is None:
            return False, self._unavailable()
        ctx = Context(self._u2_dev, self._serial)
        recorded_s, timeout_s = self._budget(action)
        deadline = time.time() + timeout_s
        while True:
            try:
                ok, actual = spec["fn"](ctx, action)
            except Exception as e:
                ok, actual = False, f"check error: {type(e).__name__}: {e}"
            if ok:
                return True, None
            if time.time() >= deadline or self._stop_event.is_set():
                break
            time.sleep(0.3)
        sel = action.selector
        what = self._describe(sel) if sel else "the screen"
        msg = f"assertion failed: {action.check} {what}"
        if action.value is not None:
            msg += f" {action.value!r}"
        if actual is not None:
            msg += f" (found {actual!r})"
        msg += f" after {self._waited(recorded_s, timeout_s)}"
        if action.soft:
            self._warn = msg
            return True, None
        return False, msg

    def _execute_element_tap(self, action: Action, serial: str):
        sel = action.selector
        recorded_s, timeout_s = self._budget(action)
        dev = self._u2_dev
        last_exc = None
        if dev is not None:
            el, last_exc = self._wait_for_element(dev, sel, timeout_s, recorded_s)
            if el is not None:
                cx, cy = el.center()
                actions.tap(cx, cy)
                return True, None

        # not found (or no u2): coordinates only for taps recorded without any identifier
        if not action.reliable and action.fallback_coords:
            cx, cy = action.fallback_coords
            if self._on_status:
                why = "u2 unavailable" if dev is None else f"element not found after {timeout_s:g}s wait"
                self._on_status(f"[WARN] {why}, using fallback_coords")
            actions.tap(cx, cy)
            return True, None

        if dev is None:
            return False, self._unavailable()
        if last_exc:
            return False, f"element lookup failed: {last_exc}"
        return False, f"element '{self._describe(sel)}' not found after {self._waited(recorded_s, timeout_s)}"

    def _execute_scroll_to(self, action: Action):
        """Swipe until the element is on screen. Does not tap it."""
        dev = self._u2_dev
        if dev is None:
            return False, self._unavailable()
        sel = action.selector
        down = action.direction != "up"
        w, h = dev.window_size()
        hi, lo = int(h * 0.7), int(h * 0.3)
        for n in range(self.MAX_SCROLLS + 1):
            try:
                if self._find_element(dev, sel) is not None:
                    return True, None
            except Exception as e:
                return False, f"element lookup failed: {e}"
            if n == self.MAX_SCROLLS or self._stop_event.is_set():
                break
            if self._on_status:
                self._on_status(f"[SCROLL {n + 1}/{self.MAX_SCROLLS}] looking for '{self._describe(sel)}'")
            actions.swipe(w // 2, hi if down else lo, w // 2, lo if down else hi)
            time.sleep(0.5)   # let the list settle before looking again
        return False, (f"element '{self._describe(sel)}' not found after "
                       f"{self.MAX_SCROLLS} scrolls {'down' if down else 'up'}")

    def _wait_for_element(self, dev, selector: ElementSelector, timeout_s: float, recorded_s: float = 0.0):
        """Poll for element up to timeout_s. Returns (element_or_None, last_exc_or_None).
        A lookup call can block while the app is busy, so the deadline is checked between calls."""
        start = time.time()
        deadline = start + timeout_s
        extra_noted = recorded_s <= 0
        last_exc = None
        first = True
        while True:
            try:
                el = self._find_element(dev, selector)
                if el is not None:
                    return el, None
            except Exception as e:
                last_exc = e

            if time.time() >= deadline or self._stop_event.is_set():
                break

            if first:
                if self._on_status:
                    self._on_status(f"[WAIT] looking for '{self._describe(selector)}'...")
                first = False

            if not extra_noted and time.time() >= start + recorded_s:
                extra_noted = True
                if self._on_status:
                    self._on_status(f"[WAIT] not there after the recorded {recorded_s:.1f}s, "
                                    f"trying up to {timeout_s - recorded_s:g}s more")

            time.sleep(0.2)

        return None, last_exc

    def _find_element(self, dev, selector: ElementSelector):
        return lookup.find_element(dev, selector)
