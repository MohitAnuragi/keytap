"""
ScrcpyCaptureManager: scrcpy-server capture backend.

Replaces screenrecord with scrcpy-server which hooks SurfaceFlinger
directly, achieving ~30fps vs screenrecord's 2-10fps.

Protocol (raw_stream=true):
  scrcpy-server → abstract socket → adb reverse → TCP → Python socket
  Python socket → PyAV in-process H264 decode → YUV frame → RGB24 → frame_q

Same public interface as CaptureManager.

Usage:
  python keytap.py --backend scrcpy
"""

import socket
import subprocess
import threading
import queue
import time
import urllib.request
from pathlib import Path

import av
import numpy as np
from av.video.reformatter import Interpolation

from keytap.core.config import ARGS, ADB_PATH
from keytap.core.paths import SCRCPY_DIR
from keytap.core.settings import section as _s


SCRCPY_VERSION     = "4.1"
SCRCPY_PORT        = 27183
SCRCPY_DEVICE_PATH = "/data/local/tmp/scrcpy-server.jar"

_CACHE_DIR    = SCRCPY_DIR
_LOCAL_JAR    = _CACHE_DIR / f"scrcpy-server-v{SCRCPY_VERSION}.jar"
_DOWNLOAD_URL = (
    f"https://github.com/Genymobile/scrcpy/releases/download/"
    f"v{SCRCPY_VERSION}/scrcpy-server-v{SCRCPY_VERSION}"
)

# AV_CODEC_FLAG_LOW_DELAY
_LOW_DELAY_FLAG = 0x00080000


class ScrcpyCaptureManager:
    """scrcpy-server capture backend. Drop-in replacement for CaptureManager."""

    def __init__(self, win_w, win_h, dev_w, dev_h, on_status):
        self.win_w        = win_w
        self.win_h        = win_h
        self.dev_w        = dev_w
        self.dev_h        = dev_h
        self._on_status   = on_status
        self.frame_q      = queue.Queue(maxsize=1)
        self._running     = True
        self._server_proc = None
        self._sock        = None

        cap = _s("capture")
        self.bitrate      = str(cap.get("bitrate", "8000000"))
        self.low_latency  = bool(cap.get("low_latency", False))
        self.fps_cap      = int(cap.get("fps_cap", 0))
        self.encode_scale = float(cap.get("encode_scale", 1.0))

        # Live stats (GIL-safe, same shape as CaptureManager)
        self.stats       = {"producer_fps": 0.0, "dropped_ps": 0, "stall_ms": 0.0}
        self._stat_prod  = 0
        self._stat_drop  = 0
        self._stat_t     = 0.0

    def start(self):
        threading.Thread(target=self._scrcpy_loop, daemon=True).start()

    def stop(self):
        self._running = False
        self._kill_server()

    def restart_with_settings(self, bitrate=None, low_latency=None, fps_cap=None, encode_scale=None):
        if bitrate is not None:      self.bitrate = str(bitrate)
        if low_latency is not None:  self.low_latency = bool(low_latency)
        if fps_cap is not None:      self.fps_cap = int(fps_cap)
        if encode_scale is not None: self.encode_scale = float(encode_scale)
        self._kill_server()

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _adb(self):
        args = [ADB_PATH]
        if ARGS.serial:
            args += ["-s", ARGS.serial]
        return args

    def _enqueue(self, frame_bytes):
        try:
            self.frame_q.get_nowait()
            self._stat_drop += 1
        except queue.Empty:
            pass
        self.frame_q.put(frame_bytes)
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

    def _kill_server(self):
        if self._server_proc:
            try:
                self._server_proc.terminate()
            except Exception:
                pass
            self._server_proc = None
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None

    # ── Setup ─────────────────────────────────────────────────────────────────

    def _ensure_jar(self):
        if _LOCAL_JAR.exists():
            return True
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        self._on_status(f"downloading scrcpy-server v{SCRCPY_VERSION}...")
        try:
            urllib.request.urlretrieve(_DOWNLOAD_URL, _LOCAL_JAR)
            self._on_status("scrcpy-server downloaded")
            return True
        except Exception as e:
            self._on_status(f"scrcpy download failed: {e}")
            return False

    def _push_jar(self):
        check = subprocess.run(
            self._adb() + ["shell", f"stat -c %s {SCRCPY_DEVICE_PATH} 2>/dev/null || echo 0"],
            capture_output=True, text=True
        )
        device_size = int((check.stdout.strip() or "0").splitlines()[-1] or "0")
        local_size  = _LOCAL_JAR.stat().st_size
        if device_size == local_size:
            return True
        self._on_status("pushing scrcpy-server to device...")
        r = subprocess.run(
            self._adb() + ["push", str(_LOCAL_JAR), SCRCPY_DEVICE_PATH],
            capture_output=True
        )
        if r.returncode != 0:
            self._on_status("push failed")
            return False
        return True

    def _setup_reverse(self):
        """Remove stale forward, set adb reverse: device abstract → host TCP."""
        subprocess.run(self._adb() + ["forward", "--remove", f"tcp:{SCRCPY_PORT}"],
                       capture_output=True)
        subprocess.run(
            self._adb() + ["reverse", "localabstract:scrcpy", f"tcp:{SCRCPY_PORT}"],
            capture_output=True
        )

    def _start_server(self):
        max_size = int(self.dev_h * self.encode_scale) if self.encode_scale < 1.0 else 0

        server_args = [
            f"CLASSPATH={SCRCPY_DEVICE_PATH}",
            "app_process", "/",
            "com.genymobile.scrcpy.Server", SCRCPY_VERSION,
            "audio=false",
            "control=false",
            "cleanup=false",
            "raw_stream=true",
            f"video_bit_rate={self.bitrate}",
        ]
        if max_size > 0:
            server_args.append(f"max_size={max_size}")
        if self.fps_cap > 0:
            server_args.append(f"max_fps={self.fps_cap}")

        self._server_proc = subprocess.Popen(
            self._adb() + ["shell"] + server_args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    # ── Capture loop ──────────────────────────────────────────────────────────

    def _scrcpy_loop(self):
        if not self._ensure_jar():
            return
        if not self._push_jar():
            return

        consecutive_failures = 0
        while self._running:
            ok = self._scrcpy_session()
            if ok:
                consecutive_failures = 0
            else:
                consecutive_failures += 1
                if consecutive_failures >= 3:
                    self._on_status("scrcpy stream failing - check device")
                    time.sleep(5)
                    consecutive_failures = 0
                else:
                    time.sleep(1)

    def _scrcpy_session(self):
        self._setup_reverse()

        server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            server_sock.bind(("127.0.0.1", SCRCPY_PORT))
            server_sock.listen(1)
        except OSError as e:
            self._on_status(f"scrcpy: port {SCRCPY_PORT} busy: {e}")
            server_sock.close()
            return False

        self._start_server()

        server_sock.settimeout(5.0)
        try:
            conn, _ = server_sock.accept()
        except socket.timeout:
            self._on_status("scrcpy: server did not connect in time")
            self._kill_server()
            server_sock.close()
            return False
        finally:
            server_sock.close()

        conn.settimeout(None)
        self._sock = conn

        try:
            return self._decode_loop(conn)
        except Exception as e:
            if self._running:
                self._on_status(f"scrcpy decode error: {e}")
            return False
        finally:
            self._kill_server()

    def _decode_loop(self, sock):
        """
        In-process H264 decode — mirrors scrcpy's demuxer → decoder pipeline.

        scrcpy C path:
          socket bytes → sc_demuxer (parse AVPackets) → sc_decoder (avcodec_decode)
          → AVFrame (YUV420p) → SDL_UpdateYUVTexture (GPU YUV→RGB)

        Our Python path:
          socket bytes → codec.parse() → codec.decode()
          → AVFrame (YUV420p) → frame.reformat(rgb24, win_w, win_h) [swscale]
          → numpy.tobytes() → frame_q

        No subprocess pipe. decode + scale happens in-process via libav.
        """
        codec = av.CodecContext.create("h264", "r")
        # AV_CODEC_FLAG_LOW_DELAY: disable B-frame reordering → frame available immediately
        codec.flags |= _LOW_DELAY_FLAG
        codec.open()

        t_start     = time.time()
        first_frame = True
        frame_count = 0

        while self._running:
            chunk = sock.recv(65536)
            if not chunk:
                if frame_count > 0:
                    self._on_status("scrcpy stream ended - restarting...")
                break

            t0 = time.time()

            # parse() splits raw H264 Annex B bytes into decodable packets
            packets = codec.parse(chunk)
            for packet in packets:
                # decode() returns list of AVFrames (YUV420p)
                for frame in codec.decode(packet):
                    # swscale: YUV420p → rgb24 at display resolution
                    # LANCZOS: best quality for downscaling (vs default BILINEAR)
                    rgb_frame = frame.reformat(
                        width=self.win_w,
                        height=self.win_h,
                        format="rgb24",
                        interpolation=Interpolation.LANCZOS,
                    )
                    # shape (H, W, 3), C-contiguous → tobytes() = exact frame_bytes
                    self._enqueue(rgb_frame.to_ndarray().tobytes())
                    frame_count += 1

                    if first_frame:
                        elapsed = time.time() - t_start
                        self._on_status(f"scrcpy  |  first frame {elapsed:.2f}s")
                        first_frame = False

            stall_ms = (time.time() - t0) * 1000
            self.stats["stall_ms"] = round(
                0.8 * self.stats["stall_ms"] + 0.2 * stall_ms, 1
            )

        # Flush decoder
        try:
            for frame in codec.decode(None):
                rgb_frame = frame.reformat(
                    width=self.win_w, height=self.win_h, format="rgb24",
                    interpolation=Interpolation.LANCZOS,
                )
                self._enqueue(rgb_frame.to_ndarray().tobytes())
                frame_count += 1
        except Exception:
            pass

        return frame_count > 10
