"""
ActionReplayer: executes a Recording on the connected device.

Modes:
  timed  - honor recorded timestamps (real-time replay)
  fast   - fixed 300ms gap between each action
  step   - advance manually by calling advance()
"""
import threading
import time

import actions
from recording import Action, ElementSelector, Recording, action_label


class ActionReplayer:
    FAST_DELAY_MS = 300

    def __init__(self):
        self.active = False
        self.mode = "timed"
        self._stop_event = threading.Event()
        self._step_event = threading.Event()
        self._on_status = None
        self._u2_dev = None
        self._current_step = 0   # 0-indexed; set before _wait_between so overlay reflects waiting step
        self._recording = None   # Recording being replayed; None when idle

    @property
    def current_step(self) -> int:
        return self._current_step

    @property
    def recording(self):
        return self._recording

    # ── Public API ────────────────────────────────────────────────────────────

    def start(self, recording: Recording, mode: str, serial: str,
              on_status, on_done, device_resolution=None):
        self.mode = mode
        self.active = True
        self._recording = recording
        self._current_step = 0
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
        total = len(recording.actions)

        if device_resolution and recording.device_resolution != list(device_resolution):
            on_status(
                f"[WARN] recorded on {recording.device_resolution}, "
                f"device is {list(device_resolution)} — coords may miss"
            )

        # Pre-connect u2 once for the whole replay if element_tap actions exist
        has_elem_taps = any(a.type == "element_tap" for a in recording.actions)
        if has_elem_taps:
            try:
                import uiautomator2 as u2
                self._u2_dev = u2.connect(serial) if serial else u2.connect()
            except Exception:
                self._u2_dev = None
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

            prev_t = action.t

            ok, error = self._execute(action, serial)
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

    def _execute(self, action: Action, serial: str):
        """Dispatch one action. Returns (success, error_msg)."""
        t = action.type
        if t == "tap":
            actions.tap(action.x, action.y)
        elif t == "element_tap":
            return self._execute_element_tap(action, serial)
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

    def _execute_element_tap(self, action: Action, serial: str):
        dev = self._u2_dev
        if dev is not None:
            el, last_exc = self._wait_for_element(dev, action.selector)

            if el is not None:
                cx, cy = el.center()
                actions.tap(cx, cy)
                return True, None

            # Not found after wait — try fallback
            if not action.reliable and action.fallback_coords:
                cx, cy = action.fallback_coords
                sel = action.selector
                name = sel.resource_id or sel.content_desc or sel.text or "?"
                msg = "element lookup error" if last_exc else f"element '{name}' not found"
                if self._on_status:
                    self._on_status(f"[WARN] {msg} after 2s wait, using fallback_coords")
                actions.tap(cx, cy)
                return True, None

            if last_exc:
                return False, f"element lookup failed: {last_exc}"
            sel = action.selector
            desc = sel.resource_id or sel.content_desc or sel.text or "?"
            return False, f"element '{desc}' not found after 2s wait"
        else:
            # u2 not connected — fall back to coords if possible
            if not action.reliable and action.fallback_coords:
                cx, cy = action.fallback_coords
                if self._on_status:
                    self._on_status("[WARN] u2 unavailable, using fallback_coords")
                actions.tap(cx, cy)
                return True, None

        sel = action.selector
        desc = sel.resource_id or sel.content_desc or sel.text or "?"
        return False, f"element '{desc}' not found"

    def _wait_for_element(self, dev, selector: ElementSelector, timeout_s: float = 2.0):
        """Poll for element up to timeout_s. Returns (element_or_None, last_exc_or_None)."""
        deadline = time.time() + timeout_s
        last_exc = None
        first = True
        while True:
            try:
                el = self._find_element(dev, selector)
                if el is not None:
                    return el, None
            except Exception as e:
                last_exc = e

            if time.time() >= deadline:
                break

            if first:
                name = selector.resource_id or selector.content_desc or selector.text or "?"
                if self._on_status:
                    self._on_status(f"[WAIT] looking for '{name}'...")
                first = False

            time.sleep(0.2)

        return None, last_exc

    def _find_element(self, dev, selector: ElementSelector):
        """Try selector fields in priority order. Returns u2 UiObject or None."""
        checks = [
            (selector.resource_id, lambda v: dev(resourceId=v)),
            (selector.content_desc, lambda v: dev(description=v)),
            (selector.text,         lambda v: dev(text=v)),
        ]
        for value, factory in checks:
            if not value:
                continue
            try:
                el = factory(value)
                if el.exists:
                    return el
            except Exception:
                continue
        return None
