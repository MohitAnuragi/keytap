import json
import logging
import os
import queue
import string
import subprocess
import sys
import threading
import time

import pygame

import actions
import fonts
from adb import adb, get_device_size, list_devices
from capture import CaptureManager
from capture_scrcpy import ScrcpyCaptureManager
from config import ARGS, CURSOR_MODE, CURSOR_JUMP, ADB_PATH
from element_manager import ElementManager
from grid import GridRenderer
from recorder import ActionRecorder
from replayer import ActionReplayer
from settings import kb

STATUS_H = 24   # height of status bar below the mirror
_PROC    = os.path.join(os.path.dirname(os.path.abspath(__file__)), "palette_proc.py")


class KeyTap:
    def __init__(self, dev_w, dev_h):
        self.dev_w  = dev_w
        self.dev_h  = dev_h
        self.scale  = ARGS.height / dev_h
        self.win_w  = int(dev_w * self.scale)
        self.win_h  = ARGS.height

        # ── Pygame window ─────────────────────────────────────────────────────
        pygame.init()
        self.screen = pygame.display.set_mode((self.win_w, self.win_h + STATUS_H))
        pygame.display.set_caption("keytap")

        # ── Components ────────────────────────────────────────────────────────
        CaptureClass  = ScrcpyCaptureManager if ARGS.backend == "scrcpy" else CaptureManager
        self.capture  = CaptureClass(self.win_w, self.win_h, dev_w, dev_h,
                                     on_status=self._set_status)
        self.renderer = GridRenderer(self.win_w, self.win_h)
        self.renderer.init_fonts()

        self.elements = ElementManager(
            serial=ARGS.serial,
            scale=self.scale,
            on_status=self._set_status,
            on_tap=self._on_elem_tap,
        )
        self.clock = pygame.time.Clock()

        # ── App state ─────────────────────────────────────────────────────────
        self.cursor_row        = 0
        self.cursor_col        = 0
        self.input_buf         = []
        self._status           = "connecting..."
        self._raw_surf         = None    # latest pygame Surface from capture
        self._hier_highlight   = None    # (wx1, wy1, wx2, wy2)
        self._last_shift_t     = 0.0
        self._palette_result_q = queue.Queue()
        self._palette_proc     = None   # pre-warmed palette subprocess
        self._palette_ready    = False  # True once subprocess signals __ready__

        # Package list (for palette)
        self._packages         = []
        self._packages_ready   = False

        # Deeplink history
        self._deeplinks        = []

        # Stats overlay
        self._stats_visible    = False
        self._consumer_fps     = 0.0
        self._consumer_frames  = 0
        self._consumer_t       = 0.0
        self._render_ms        = 0.0

        # Action recorder / replayer
        self.recorder = ActionRecorder()
        self.replayer = ActionReplayer()

        # Network capture (optional - requires mitmproxy + device cert setup)
        from network_recorder import NetworkRecorder
        from network_replayer import NetworkReplayer
        self.net_recorder = NetworkRecorder()
        self.net_replayer = NetworkReplayer()

        # Correlated per-frame timeline (logcat + actions)
        from timeline_recorder import TimelineRecorder
        self.timeline_recorder = TimelineRecorder()
        self._timeline_active  = False

        # Video capture (records mirror stream to MP4 during recording)
        from video_recorder import VideoRecorder
        self.video_recorder = VideoRecorder()
        self._video_active  = False

        # Perfetto system trace (jank analysis)
        from perf_trace import PerfettoCapture
        self.perf_trace = PerfettoCapture()

        # Status font
        self._status_font = self._try_font(11)

    # ── Font helper ───────────────────────────────────────────────────────────

    def _try_font(self, size):
        return fonts.load(size)

    # ── Status ────────────────────────────────────────────────────────────────

    def _set_status(self, msg):
        self._status = msg

    def _idle_status(self):
        if self.elements.active:
            self.elements.idle_status()
            return
        if CURSOR_MODE:
            r, c = self.cursor_row, self.cursor_col
            rl = string.ascii_uppercase[r] if r < 26 else str(r + 1)
            cl = string.ascii_uppercase[c]
            self._set_status(
                f"cursor: {rl}{cl} (row {r+1}, col {c+1})"
                f"  |  arrows=move  Shift=jump{CURSOR_JUMP}  space=tap"
                f"  w/s=scroll  b=back  h=home  r=recents  Esc=quit"
            )
        else:
            buf = ''.join(self.input_buf).upper()
            cols_r = string.ascii_uppercase[:ARGS.cols]
            rows_r = string.ascii_uppercase[:min(ARGS.rows, 26)]
            if len(buf) == 1:
                self._set_status(
                    f"row {buf} selected  ->  type col ({cols_r[0]}-{cols_r[-1]})"
                    f"  |  Backspace=cancel")
            else:
                self._set_status(
                    f"type row ({rows_r[0]}-{rows_r[-1]})"
                    f" then col ({cols_r[0]}-{cols_r[-1]})  |  Esc=quit")

    # ── Coordinate helpers ────────────────────────────────────────────────────

    def _cell_to_dev_rc(self, r, c):
        cw = self.dev_w / ARGS.cols
        ch = self.dev_h / ARGS.rows
        return int(c * cw + cw / 2), int(r * ch + ch / 2)

    def _label_to_dev(self, label):
        r = string.ascii_uppercase.index(label[0].upper())
        c = string.ascii_uppercase.index(label[1].upper())
        if r >= ARGS.rows or c >= ARGS.cols:
            raise ValueError("out of range")
        return self._cell_to_dev_rc(r, c)

    # ── ADB actions ───────────────────────────────────────────────────────────

    def _after_action_recorded(self):
        """Call immediately after any record_* call to feed the timeline."""
        if self.recorder.active and self._timeline_active:
            idx    = self.recorder.action_count - 1
            action = self.recorder._actions[idx]
            self.timeline_recorder.on_action(idx, action)

    def _tap(self, dx, dy):
        if self.recorder.active:
            el = self.elements.element_at(dx, dy)
            if el is not None:
                self.recorder.record_element_tap(el)
            else:
                self.recorder.record_tap(dx, dy)
        else:
            self.recorder.record_tap(dx, dy)
        self._after_action_recorded()
        actions.tap(dx, dy, on_status=self._set_status)

    def _keyevent(self, code, label=""):
        self.recorder.record_keyevent(code, label)
        self._after_action_recorded()
        actions.keyevent(code, label, on_status=self._set_status)

    def _scroll(self, direction):
        cx, cy = self._cell_to_dev_rc(self.cursor_row, self.cursor_col)
        dist = int(self.dev_h * 0.4)
        half = dist // 2
        if direction == 'up':
            self.recorder.record_swipe(cx, max(cy - half, 0), cx, min(cy + half, self.dev_h - 1))
        else:
            self.recorder.record_swipe(cx, min(cy + half, self.dev_h - 1), cx, max(cy - half, 0))
        self._after_action_recorded()
        actions.scroll(cx, cy, self.dev_h, direction, on_status=self._set_status)

    def _on_elem_tap(self, cx, cy, hint, element=None):
        self._set_status(f"tap '{hint}' ({cx},{cy})")
        if element is not None:
            self.recorder.record_element_tap(element)
        else:
            self.recorder.record_tap(cx, cy)
        self._after_action_recorded()
        actions.tap(cx, cy)

    # ── Key handling ──────────────────────────────────────────────────────────

    def _handle_key(self, event):
        """Returns False to quit, True to continue."""
        sym  = event.key
        char = event.unicode.upper() if event.unicode else ""
        cl   = char.lower()
        mods = pygame.key.get_mods()
        step = CURSOR_JUMP if (mods & pygame.KMOD_SHIFT) else 1

        # Quit
        if sym == pygame.K_ESCAPE:
            return False

        # Step-mode replay: Space advances one action
        if self.replayer.active and self.replayer.mode == "step":
            if sym == pygame.K_SPACE:
                self.replayer.advance()
                return True

        # Double-shift → palette
        if sym in (pygame.K_LSHIFT, pygame.K_RSHIFT):
            now = time.time()
            if now - self._last_shift_t < 0.4:
                self._open_palette()
                self._last_shift_t = 0.0
            else:
                self._last_shift_t = now
            return True

        # Element mode toggle (priority)
        if cl == kb.element_mode:
            if self.elements.active and not self.elements._headless:
                # visible → back to headless if recording, else exit fully
                if self.recorder.active:
                    self.elements._headless = True
                else:
                    self.elements.exit()
            else:
                # inactive or headless → go visible (re-fetch for fresh labels)
                self.elements.enter(ref_surf=self._raw_surf, headless=False)
            self._idle_status()
            return True

        if CURSOR_MODE:
            self._handle_cursor(sym, char, cl, step)
        else:
            self._handle_label(sym, char, cl)

        return True

    def _handle_cursor(self, sym, char, cl, step):
        """Arrow-key cursor mode."""
        if self.elements.active:
            if sym == pygame.K_TAB:
                self.elements.toggle_bounds()
            elif sym == pygame.K_BACKSPACE:
                self.elements.clear_buf()
            elif sym == pygame.K_UP:
                self.cursor_row = max(0, self.cursor_row - step)
            elif sym == pygame.K_DOWN:
                self.cursor_row = min(ARGS.rows - 1, self.cursor_row + step)
            elif sym == pygame.K_LEFT:
                self.cursor_col = max(0, self.cursor_col - step)
            elif sym == pygame.K_RIGHT:
                self.cursor_col = min(ARGS.cols - 1, self.cursor_col + step)
            elif sym == pygame.K_SPACE:
                dx, dy = self._cell_to_dev_rc(self.cursor_row, self.cursor_col)
                self._tap(dx, dy)
            elif cl == kb.back:
                self._keyevent(4, "back")
            elif cl == kb.home:
                self._keyevent(3, "home")
            elif cl == kb.recents:
                self._keyevent(187, "recents")
            elif cl == kb.scroll_up:
                self._scroll('up')
            elif cl == kb.scroll_down:
                self._scroll('down')
            else:
                self.elements.handle_char(cl)
            self._idle_status()
            return

        if sym == pygame.K_UP:
            self.cursor_row = max(0, self.cursor_row - step)
        elif sym == pygame.K_DOWN:
            self.cursor_row = min(ARGS.rows - 1, self.cursor_row + step)
        elif sym == pygame.K_LEFT:
            self.cursor_col = max(0, self.cursor_col - step)
        elif sym == pygame.K_RIGHT:
            self.cursor_col = min(ARGS.cols - 1, self.cursor_col + step)
        elif sym == pygame.K_SPACE:
            dx, dy = self._cell_to_dev_rc(self.cursor_row, self.cursor_col)
            self._tap(dx, dy)
            return
        elif cl == kb.back:
            self._keyevent(4, "back"); return
        elif cl == kb.home:
            self._keyevent(3, "home"); return
        elif cl == kb.recents:
            self._keyevent(187, "recents"); return
        elif cl == kb.scroll_up:
            self._scroll('up'); return
        elif cl == kb.scroll_down:
            self._scroll('down'); return
        else:
            return
        self._idle_status()

    def _handle_label(self, sym, char, cl):
        """2-letter label typing mode."""
        if sym == pygame.K_BACKSPACE:
            if self.input_buf:
                self.input_buf.pop()
                self._idle_status()
            return
        if not char.isalpha():
            return
        self.input_buf.append(char)
        if len(self.input_buf) == 1:
            self._idle_status()
            return
        label = ''.join(self.input_buf)
        self.input_buf.clear()
        try:
            dx, dy = self._label_to_dev(label)
            self._tap(dx, dy)
        except ValueError:
            self._set_status(f"'{label}' out of range")

    # ── Palette ───────────────────────────────────────────────────────────────

    def _open_palette(self):
        threading.Thread(target=self._fetch_packages, daemon=True).start()
        data = {
            "mode":                  "palette",
            "packages":              self._packages,
            "clipboard":             self._read_clipboard(),
            "deeplinks":             self._deeplinks,
            "serial":                ARGS.serial,
            "adb_path":              ADB_PATH,
            "capture_bitrate":       self.capture.bitrate,
            "capture_low_latency":   self.capture.low_latency,
            "capture_fps_cap":       self.capture.fps_cap,
            "capture_encode_scale":  self.capture.encode_scale,
            "capture_stats_visible": self._stats_visible,
            "recording_active":      self.recorder.active,
            "trace_active":          self.perf_trace.active,
        }
        if self._palette_proc and self._palette_ready:
            # Fast path: pre-warmed subprocess already has Python + tkinter loaded.
            # Just send data and it shows the window immediately.
            payload = (json.dumps(data) + "\n").encode()
            try:
                self._palette_proc.stdin.write(payload)
                self._palette_proc.stdin.flush()
                return
            except Exception:
                # Process died; fall through to spawn a fresh one.
                self._palette_proc  = None
                self._palette_ready = False
        threading.Thread(target=self._run_proc, args=(data,), daemon=True).start()

    def _run_proc(self, data, emit_result=True):
        payload = (json.dumps(data) + "\n").encode()
        try:
            proc = subprocess.Popen(
                [sys.executable, _PROC],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            proc.stdin.write(payload)
            proc.stdin.close()
            threading.Thread(
                target=self._read_proc_hints, args=(proc.stderr,), daemon=True
            ).start()
            stdout = proc.stdout.read()
            proc.wait(timeout=600)
            self._hier_highlight = None
            if emit_result:
                result = json.loads(stdout.decode().strip()) if stdout.strip() else None
                self._palette_result_q.put(result)
        except Exception as e:
            self._hier_highlight = None
            self._set_status(f"palette error: {e}")

    def _read_proc_hints(self, stderr):
        for raw in stderr:
            line = raw.decode().strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
                if msg.get("action") == "tap":
                    actions.tap(msg["cx"], msg["cy"])
                else:
                    bounds = msg.get("hover")
                    if bounds:
                        s = self.scale
                        x1, y1, x2, y2 = bounds
                        self._hier_highlight = (
                            int(x1 * s), int(y1 * s), int(x2 * s), int(y2 * s)
                        )
                    else:
                        self._hier_highlight = None
            except Exception:
                print(f"[proc stderr] {line}", flush=True)

    def _read_clipboard(self):
        try:
            return subprocess.run(
                ["pbpaste"], capture_output=True, text=True, timeout=1
            ).stdout
        except Exception:
            return ""

    # ── Palette pre-warming ───────────────────────────────────────────────────

    def _preload_palette(self):
        """Spawn palette subprocess hidden. Python + tkinter load in background.
        When user triggers palette, the process is already warm — just send data."""
        try:
            cmd = [sys.executable, _PROC, "--persistent"]
            if ARGS.serial:
                cmd += ["--serial", ARGS.serial]
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self._palette_proc = proc
            threading.Thread(
                target=self._read_persistent_hints, args=(proc.stderr,), daemon=True
            ).start()
            threading.Thread(
                target=self._read_persistent_results, args=(proc.stdout,), daemon=True
            ).start()
        except Exception as e:
            self._set_status(f"palette preload failed: {e}")

    def _read_persistent_hints(self, stderr):
        """Read stderr from the persistent palette process.
        Handles the __ready__ signal and live hints (hover, tap)."""
        for raw in stderr:
            line = raw.decode().strip()
            if not line:
                continue
            if line == "__ready__":
                self._palette_ready = True
                continue
            try:
                msg = json.loads(line)
                if msg.get("action") == "tap":
                    actions.tap(msg["cx"], msg["cy"])
                else:
                    bounds = msg.get("hover")
                    if bounds:
                        s = self.scale
                        x1, y1, x2, y2 = bounds
                        self._hier_highlight = (
                            int(x1 * s), int(y1 * s), int(x2 * s), int(y2 * s)
                        )
                    else:
                        self._hier_highlight = None
            except Exception:
                print(f"[palette stderr] {line}", flush=True)

    def _read_persistent_results(self, stdout):
        """Read results line-by-line from the persistent palette process."""
        for raw in stdout:
            try:
                result = json.loads(raw.decode().strip())
                self._hier_highlight = None
                self._palette_result_q.put(result)  # enqueue even None (close with no action)
            except Exception:
                pass

    def _on_palette_action(self, result):
        t = result.get('type')

        if t == 'close':
            return

        if t == 'tap':
            actions.tap(result['cx'], result['cy'], on_status=self._set_status)

        elif t == 'install-apk':
            path = result.get('path', '')
            if path:
                actions.install_apk(path, on_status=self._set_status)

        elif t == 'input-text':
            text = result['text']
            self.recorder.record_input_text(text)
            actions.input_text(text, on_status=self._set_status)

        elif t == 'clipboard':
            text = result.get('value', '')
            if text:
                self.recorder.record_input_text(text)
                actions.input_text(text, on_status=self._set_status)
            else:
                self._set_status("clipboard empty")

        elif t == 'deeplink':
            url = result.get('value', '')
            self.recorder.record_launch_deeplink(url)
            actions.launch_deeplink(url, on_status=self._set_status)
            self._add_deeplink(url)

        elif t == 'theme-changed':
            self._set_status(f"theme set to {result.get('theme', '?')} – reopen palette to apply")

        elif t == 'capture-settings':
            import settings as _settings_mod
            if 'stats_visible' in result:
                self._stats_visible = bool(result['stats_visible'])
            updates = {k: result[k] for k in ('bitrate', 'low_latency', 'fps_cap', 'encode_scale') if k in result}
            if updates:
                _settings_mod.save_section('capture', updates)
                self.capture.restart_with_settings(**updates)
                mbps = int(self.capture.bitrate) // 1_000_000
                ll  = " + low latency" if self.capture.low_latency else ""
                fps = f" + {self.capture.fps_cap}fps cap" if self.capture.fps_cap > 0 else ""
                self._set_status(f"capture: {mbps}Mbps{ll}{fps} – restarting stream...")

        elif t == 'app-action':
            self._dispatch_app_action(result['pkg'], result['action'])

        elif t == 'insert-wait':
            ms = result.get('ms', 1000)
            self.recorder.record_wait(ms)
            self._after_action_recorded()
            self._set_status(f"inserted wait: {ms}ms")

        elif t == 'start-recording':
            self.recorder.start()
            self.net_recorder.start()
            self.elements.enter(ref_surf=self._raw_surf, headless=True)
            self.timeline_recorder.start(serial=ARGS.serial)
            self._timeline_active = True
            self.video_recorder.start(self.capture.win_w, self.capture.win_h)
            self._video_active = True
            self._set_status("recording...  (double-shift -> Stop Recording to save)")

        elif t == 'stop-recording':
            self._timeline_active = False
            self._video_active    = False
            self.recorder.stop()
            network_capture = self.net_recorder.stop()
            video_src       = self.video_recorder.stop()
            if self.elements._headless:
                self.elements.exit()
            timeline_events = self.timeline_recorder.stop(
                network_flows=network_capture.get("flows", [])
            )
            name = result.get('name', 'recording')
            try:
                path = self.recorder.save(
                    name, ARGS.serial, (self.dev_w, self.dev_h),
                    network_capture=network_capture,
                    timeline_events=timeline_events,
                    video_src_path=video_src,
                )
                has_net      = bool(network_capture.get("flows"))
                has_timeline = bool(timeline_events)
                has_video    = bool(video_src)
                parts = []
                if has_net:      parts.append("network")
                if has_timeline: parts.append("timeline")
                if has_video:    parts.append("video")
                suffix = (" + " + " + ".join(parts)) if parts else ""
                self._set_status(f"saved: {os.path.basename(path)}{suffix}")
            except Exception as e:
                self._set_status(f"save failed: {e}")

        elif t == 'start-trace':
            preset   = result.get('preset', 'light')
            features = [f"mirroring ({ARGS.backend})"]
            if self.recorder.active:
                features.append("action recorder")
            if self._video_active:
                features.append("video recording")
            self._set_status(f"starting {preset} trace...")

            def _start_trace():
                try:
                    self.perf_trace.start(ARGS.serial, preset, features)
                    self._set_status(f"tracing ({preset})...  (double-shift -> STOP TRACE to save)")
                except Exception as e:
                    self._set_status(f"trace start failed: {e}")
            threading.Thread(target=_start_trace, daemon=True).start()

        elif t == 'stop-trace':
            self._set_status("stopping trace...")

            def _stop_trace():
                try:
                    path = self.perf_trace.stop()
                    self._set_status(f"trace saved: {os.path.basename(path)}  (double-shift -> Perf Trace -> Results)")
                except Exception as e:
                    self._set_status(f"trace stop failed: {e}")
            threading.Thread(target=_stop_trace, daemon=True).start()

        elif t == 'heap-snapshot':
            label = result.get('label', '')
            self._set_status("dumping heap...  (app freezes for a few seconds)")

            def _heap_snapshot():
                try:
                    from heap_dump import capture
                    path = capture(ARGS.serial, label)
                    self._set_status(f"heap saved: {os.path.basename(path)}  (double-shift -> Memory Profiler -> Snapshots)")
                except Exception as e:
                    self._set_status(f"heap dump failed: {e}")
            threading.Thread(target=_heap_snapshot, daemon=True).start()

        elif t == 'replay':
            path = result.get('path', '')
            mode = result.get('mode', 'timed')
            try:
                from recording import Recording
                rec = Recording.load(path)
                if rec.network_flows:
                    self.net_replayer.start({
                        "flows": rec.network_flows,
                        "host_tokens": rec.network_host_tokens,
                    })
                self.replayer.start(
                    rec, mode, ARGS.serial,
                    on_status=self._set_status,
                    on_done=self._on_replay_done,
                    device_resolution=(self.dev_w, self.dev_h),
                )
            except Exception as e:
                self._set_status(f"replay error: {e}")

    def _dispatch_app_action(self, pkg, action):
        self._set_status(f"{action} {pkg}")
        if action == "launch":
            self.recorder.record_launch(pkg)
            actions.launch(pkg, on_status=self._set_status)
        elif action == "force-stop":
            actions.force_stop(pkg, on_status=self._set_status)
        elif action == "clear-data":
            actions.clear_data(pkg, on_status=self._set_status)
        elif action == "uninstall":
            actions.uninstall(pkg, on_status=self._set_status)

    def _on_replay_done(self, success, step, error):
        self.net_replayer.stop()
        if success:
            self._set_status(f"replay complete  ({step} actions)")
        else:
            self._set_status(f"replay failed at step {step}: {error}")

    def _refocus_window(self):
        """Return OS focus to the pygame window after palette closes.

        Uses ObjC runtime directly (libobjc.dylib) — always present on macOS,
        no PyObjC install needed. No-op on non-macOS.
        """
        try:
            import ctypes
            import ctypes.util
            if sys.platform != 'darwin':
                return
            libobjc = ctypes.cdll.LoadLibrary(ctypes.util.find_library('objc'))
            libobjc.objc_getClass.restype = ctypes.c_void_p
            libobjc.sel_registerName.restype = ctypes.c_void_p
            libobjc.objc_msgSend.restype = ctypes.c_void_p
            libobjc.objc_msgSend.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            app = libobjc.objc_msgSend(
                libobjc.objc_getClass(b'NSApplication'),
                libobjc.sel_registerName(b'sharedApplication'),
            )
            # activateIgnoringOtherApps: takes a BOOL — use typed wrapper
            fn = ctypes.CFUNCTYPE(
                None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_bool
            )(libobjc.objc_msgSend)
            fn(app, libobjc.sel_registerName(b'activateIgnoringOtherApps:'), True)
        except Exception:
            pass

    # ── Deeplink history ──────────────────────────────────────────────────────

    _DEEPLINK_PATH = os.path.expanduser("~/.keytap_deeplinks.json")

    def _load_deeplinks(self):
        try:
            with open(self._DEEPLINK_PATH) as f:
                self._deeplinks = json.load(f)
        except Exception:
            self._deeplinks = []

    def _add_deeplink(self, url):
        if url in self._deeplinks:
            self._deeplinks.remove(url)
        self._deeplinks.insert(0, url)
        self._deeplinks = self._deeplinks[:20]
        threading.Thread(target=self._save_deeplinks, daemon=True).start()

    def _save_deeplinks(self):
        try:
            with open(self._DEEPLINK_PATH, "w") as f:
                json.dump(self._deeplinks, f)
        except Exception:
            pass

    # ── Package list ──────────────────────────────────────────────────────────

    def _fetch_packages(self):
        result = adb("shell", "pm", "list", "packages")
        self._packages = sorted(
            line.strip()[len("package:"):]
            for line in result.stdout.decode().splitlines()
            if line.strip().startswith("package:")
        )
        self._packages_ready = True

    # ── Draw ──────────────────────────────────────────────────────────────────

    def _draw(self):
        self.screen.fill((0, 0, 0))

        if self._raw_surf is not None:
            frame = self._raw_surf.copy()
            elems = self.elements.elements_for_renderer() if self.elements.active else None
            composited = self.renderer.composite(
                frame,
                self.cursor_row, self.cursor_col,
                self.input_buf,
                elements=elems,
                highlight_bounds=self._hier_highlight,
            )
            self.screen.blit(composited, (0, 0))

        if self.replayer.active and self.replayer.mode == "step":
            self._draw_step_overlay()
        self._draw_status()
        if self._stats_visible:
            self._draw_stats()
        pygame.display.flip()

    def _draw_step_overlay(self):
        from recording import action_label
        rec = self.replayer.recording
        if rec is None:
            return
        all_actions = rec.actions
        total = len(all_actions)
        if total == 0:
            return
        current = self.replayer.current_step
        font = self._status_font
        if not font:
            return

        VISIBLE = 9
        half = VISIBLE // 2
        start = max(0, min(current - half, total - VISIBLE))
        end = min(total, start + VISIBLE)

        line_h = font.get_height() + 3
        pad = 8
        header_h = font.get_height() + 8
        panel_w = 340
        panel_h = (end - start) * line_h + pad + header_h

        x = self.win_w - panel_w - 4
        y = 4

        pygame.draw.rect(self.screen, (18, 18, 18), (x, y, panel_w, panel_h))
        pygame.draw.rect(self.screen, (55, 55, 55), (x, y, panel_w, panel_h), 1)

        header = f"STEP {current + 1}/{total}  Space=advance"
        hs = font.render(header, True, (110, 110, 110))
        self.screen.blit(hs, (x + pad, y + 4))

        ty = y + header_h
        max_chars = (panel_w - pad * 2) // 7  # ~7px per char at size 11

        for i in range(start, end):
            a = all_actions[i]
            is_cur = (i == current)
            prefix = "> " if is_cur else "  "
            if is_cur:
                color = (0, 200, 100)
            elif i < current:
                color = (65, 65, 65)
            else:
                color = (160, 160, 160)
            label = f"{prefix}{i + 1:2}  {action_label(a)}"[:max_chars]
            s = font.render(label, True, color)
            self.screen.blit(s, (x + pad, ty))
            ty += line_h

    def _draw_status(self):
        bar_y = self.win_h
        pygame.draw.rect(self.screen, (26, 26, 26),
                         (0, bar_y, self.win_w, STATUS_H))
        if not self._status_font:
            return

        x = 8
        text_y = bar_y + (STATUS_H - self._status_font.size("X")[1]) // 2

        if self.recorder.active:
            blink_on = int(time.time() * 2) % 2 == 0
            rec_color = (220, 50, 50) if blink_on else (100, 30, 30)
            rec_surf = self._status_font.render(
                f"[REC] {self.recorder.action_count}", True, rec_color
            )
            sep_surf = self._status_font.render("  |  ", True, (136, 136, 136))
            main_surf = self._status_font.render(self._status[:90], True, (136, 136, 136))
            self.screen.blit(rec_surf, (x, text_y))
            x += rec_surf.get_width()
            self.screen.blit(sep_surf, (x, text_y))
            x += sep_surf.get_width()
            self.screen.blit(main_surf, (x, text_y))
        else:
            t = self._status_font.render(self._status[:120], True, (136, 136, 136))
            self.screen.blit(t, (x, text_y))

    def _draw_stats(self):
        s    = self.capture.stats
        font = self._status_font
        if not font:
            return
        lines = [
            ("F1 hide stats",                           (90,  90,  90)),
            (f"producer  {s['producer_fps']:.0f} fps",  (0,  210,  90)),
            (f"dropped   {s['dropped_ps']} /s",         (220, 120,  40) if s['dropped_ps'] else (0, 210, 90)),
            (f"stall     {s['stall_ms']:.0f} ms",       (220, 120,  40) if s['stall_ms'] > 50 else (0, 210, 90)),
            (f"consumer  {self._consumer_fps:.0f} fps", (0,  210,  90)),
            (f"render    {self._render_ms:.1f} ms",     (220, 120,  40) if self._render_ms > 8 else (0, 210, 90)),
        ]
        pad    = 6
        line_h = font.get_height() + 4
        box_w  = 190
        box_h  = len(lines) * line_h + pad * 2
        x      = self.win_w - box_w - 10
        y      = 10
        pygame.draw.rect(self.screen, (12, 12, 12),  (x - pad, y - pad, box_w + pad * 2, box_h))
        pygame.draw.rect(self.screen, (55, 55, 55),  (x - pad, y - pad, box_w + pad * 2, box_h), 1)
        for i, (text, color) in enumerate(lines):
            surf = font.render(text, True, color)
            self.screen.blit(surf, (x, y + i * line_h))

    # ── Main loop ─────────────────────────────────────────────────────────────

    def run(self):
        from settings import save_defaults
        save_defaults()
        self._load_deeplinks()
        threading.Thread(target=self._fetch_packages, daemon=True).start()
        self._preload_palette()
        self.capture.start()

        running = True
        while running:
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    running = False

                elif event.type == pygame.KEYDOWN:
                    if not self._handle_key(event):
                        running = False

            # Drain all queued frames, keep only the latest to stay in sync
            raw_frame = None
            try:
                while True:
                    raw_frame = self.capture.frame_q.get_nowait()
            except queue.Empty:
                pass
            if raw_frame is not None:
                if self._video_active:
                    self.video_recorder.push_frame(raw_frame)
                # Direct bytes -> Surface: skips PIL tobytes() copy, ~2-10ms faster per frame
                self._raw_surf = pygame.image.frombytes(
                    raw_frame, (self.capture.win_w, self.capture.win_h), 'RGB'
                )
                self._idle_status()
                if self.elements.active and not self.elements.loading:
                    if self.elements.check_screen_change(self._raw_surf):
                        self.elements.enter(ref_surf=self._raw_surf,
                                            headless=self.elements._headless)
                # Consumer fps accounting
                self._consumer_frames += 1
                now = time.time()
                if self._consumer_t == 0.0:
                    self._consumer_t = now
                elif now - self._consumer_t >= 1.0:
                    self._consumer_fps = self._consumer_frames / (now - self._consumer_t)
                    self._consumer_frames = 0
                    self._consumer_t = now

            # Process palette subprocess results
            try:
                result = self._palette_result_q.get_nowait()
                if result:
                    self._on_palette_action(result)
                self._refocus_window()
            except queue.Empty:
                pass

            t_draw = time.time()
            self._draw()
            self._render_ms = (time.time() - t_draw) * 1000
            self.clock.tick(120)

        self.capture.stop()
        self.net_recorder.stop()
        self.net_replayer.shutdown()
        pygame.quit()


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    if not ARGS.serial:
        devices = list_devices()
        if not devices:
            print("No devices connected. Please connect a device and try again.")
            sys.exit(1)
        elif len(devices) == 1:
            ARGS.serial = devices[0]
            print(f"Device: {ARGS.serial}")
        else:
            print("Multiple devices found:")
            for i, s in enumerate(devices):
                print(f"  [{i + 1}] {s}")
            while True:
                try:
                    choice = int(input(f"Select device [1-{len(devices)}]: ")) - 1
                    if 0 <= choice < len(devices):
                        ARGS.serial = devices[choice]
                        break
                    print(f"Enter a number between 1 and {len(devices)}")
                except (ValueError, EOFError):
                    print(f"Enter a number between 1 and {len(devices)}")
                except KeyboardInterrupt:
                    print()
                    sys.exit(0)

    print("connecting...")
    try:
        dev_w, dev_h = get_device_size()
    except Exception as e:
        print(f"ADB error: {e}")
        print("Is device connected? Run: adb devices")
        sys.exit(1)

    print(f"device {dev_w}x{dev_h} | mirror {int(dev_w * ARGS.height / dev_h)}x{ARGS.height}")
    KeyTap(dev_w, dev_h).run()
