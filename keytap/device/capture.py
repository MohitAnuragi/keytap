import subprocess
import threading
import queue
import time

from PIL import Image

from keytap.core.config import ARGS, HAS_FFMPEG
from keytap.device.adb import adb, take_screencap
from keytap.core.settings import section as _s


class CaptureManager:
    """h264 pipe capture: screenrecord -> ffmpeg -> raw RGB24 bytes.

    Falls back to per-frame screencap if ffmpeg is missing.
    frame_q always contains raw bytes (RGB24, win_w * win_h * 3).
    Callers convert to pygame Surface with pygame.image.frombytes().
    """

    def __init__(self, win_w, win_h, dev_w, dev_h, on_status):
        self.win_w        = win_w
        self.win_h        = win_h
        self.dev_w        = dev_w
        self.dev_h        = dev_h
        self._on_status   = on_status   # callable(str), must be thread-safe
        self.frame_q      = queue.Queue(maxsize=1)
        self._running     = True
        self._adb_proc    = None
        cap = _s("capture")
        self.bitrate      = str(cap.get("bitrate", "8000000"))
        self.low_latency  = bool(cap.get("low_latency", False))
        self.fps_cap      = int(cap.get("fps_cap", 0))
        self.encode_scale = float(cap.get("encode_scale", 1.0))

        # Live stats (read by main thread, written by capture thread — GIL safe)
        self.stats       = {"producer_fps": 0.0, "dropped_ps": 0, "stall_ms": 0.0}
        self._stat_prod  = 0
        self._stat_drop  = 0
        self._stat_t     = 0.0

    def start(self):
        if HAS_FFMPEG:
            threading.Thread(target=self._h264_loop, daemon=True).start()
        else:
            self._on_status("ffmpeg missing - using screencap (slow)")
            threading.Thread(target=self._screencap_loop, daemon=True).start()

    def stop(self):
        self._running = False

    def restart_with_settings(self, bitrate=None, low_latency=None, fps_cap=None, encode_scale=None):
        """Apply new capture settings and restart the stream immediately."""
        if bitrate is not None:
            self.bitrate = str(bitrate)
        if low_latency is not None:
            self.low_latency = bool(low_latency)
        if fps_cap is not None:
            self.fps_cap = int(fps_cap)
        if encode_scale is not None:
            self.encode_scale = float(encode_scale)
        if self._adb_proc:
            try:
                self._adb_proc.terminate()
            except Exception:
                pass

    # ── Internal helpers ─────────────────────────────────────────────────────

    def _enqueue(self, frame):
        try:
            self.frame_q.get_nowait()
            self._stat_drop += 1
        except queue.Empty:
            pass
        self.frame_q.put(frame)
        self._stat_prod += 1
        now = time.time()
        if self._stat_t == 0.0:
            self._stat_t = now
        elif now - self._stat_t >= 1.0:
            elapsed = now - self._stat_t
            self.stats["producer_fps"] = round(self._stat_prod / elapsed, 1)
            self.stats["dropped_ps"]   = self._stat_drop
            self._stat_prod = 0
            self._stat_drop = 0
            self._stat_t    = now

    def _adb_args(self):
        """Return base adb arg list with -s serial if set."""
        from keytap.core.config import ADB_PATH
        args = [ADB_PATH]
        if ARGS.serial:
            args += ["-s", ARGS.serial]
        return args

    # ── h264 pipeline ────────────────────────────────────────────────────────

    def _h264_loop(self):
        """Outer restart loop - handles screenrecord 3-min limit."""
        consecutive_failures = 0
        while self._running:
            ok = self._h264_session()
            if ok:
                consecutive_failures = 0
            else:
                consecutive_failures += 1
                if consecutive_failures >= 3:
                    self._on_status("stream failing repeatedly - check device")
                    time.sleep(5)
                    consecutive_failures = 0
                else:
                    time.sleep(1)

    def _h264_session(self):
        """One screenrecord session. Returns True if it ran for a meaningful time."""
        frame_bytes = self.win_w * self.win_h * 3  # RGB24

        adb_cmd = self._adb_args() + ["shell", "screenrecord",
            "--output-format=h264",
            f"--bit-rate={self.bitrate}",
        ]
        if self.encode_scale < 1.0:
            ew = int(self.dev_w * self.encode_scale) & ~1  # must be even
            eh = int(self.dev_h * self.encode_scale) & ~1
            adb_cmd += [f"--size={ew}x{eh}"]
        adb_cmd += ["-"]
        ffmpeg_cmd = ["ffmpeg", "-loglevel", "quiet", "-hwaccel", "videotoolbox"]
        if self.low_latency:
            ffmpeg_cmd += ["-flags", "low_delay", "-fflags", "nobuffer+discardcorrupt",
                           "-probesize", "2048", "-analyzeduration", "100000",
                           "-avioflags", "direct"]
        vf = f"scale={self.win_w}:{self.win_h}"
        if self.fps_cap > 0:
            vf += f",fps={self.fps_cap}"
        ffmpeg_cmd += [
            "-i", "pipe:0",
            "-vf", vf,
            "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1",
        ]

        try:
            adb_proc = subprocess.Popen(
                adb_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            ffmpeg_proc = subprocess.Popen(
                ffmpeg_cmd,
                stdin=adb_proc.stdout,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            self._adb_proc = adb_proc
        except Exception as e:
            self._on_status(f"capture start error: {e}")
            return False

        t_start     = time.time()
        first_frame = True
        frame_count = 0

        try:
            while self._running:
                t0   = time.time()
                data = ffmpeg_proc.stdout.read(frame_bytes)
                stall_ms = (time.time() - t0) * 1000
                self.stats["stall_ms"] = round(
                    0.8 * self.stats["stall_ms"] + 0.2 * stall_ms, 1
                )
                if len(data) != frame_bytes:
                    # Stream ended (device disconnected, 3-min limit, etc.)
                    elapsed = time.time() - t_start
                    if frame_count > 0:
                        self._on_status("stream ended - restarting...")
                    return frame_count > 10  # considered successful if we got frames

                self._enqueue(data)
                frame_count += 1

                if first_frame:
                    elapsed = time.time() - t_start
                    self._on_status(f"streaming  |  first frame {elapsed:.2f}s")
                    first_frame = False

        finally:
            try:
                adb_proc.terminate()
            except Exception:
                pass
            try:
                ffmpeg_proc.terminate()
            except Exception:
                pass

        return frame_count > 10

    # ── Screencap fallback ───────────────────────────────────────────────────

    def _screencap_loop(self):
        import io
        while self._running:
            try:
                png = take_screencap()
                if png and len(png) > 512:
                    img = Image.open(io.BytesIO(png)).resize((self.win_w, self.win_h), Image.BILINEAR).convert('RGB')
                    self._enqueue(img.tobytes())
                else:
                    time.sleep(0.5)
            except Exception as e:
                self._on_status(f"capture error: {e}")
                time.sleep(0.5)
