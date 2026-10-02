"""
VideoRecorder: encodes the live mirror stream to MP4 during a recording session.

Receives RGB24 frames directly from ScrcpyCaptureManager.frame_q — no extra
ADB screencap calls. Uses PyAV (already a project dep) for H264 encoding.

Usage:
    rec = VideoRecorder()
    rec.start(width=540, height=1200)
    # in main render loop:
    if raw_frame:
        rec.push_frame(raw_frame)
    # on stop:
    tmp_path = rec.stop()   # returns path to temp .mp4, or None
"""
import os
import time
from fractions import Fraction
from pathlib import Path
from typing import Optional

from paths import TMP_VIDEO

try:
    import av
    import numpy as np
    _AV_OK = True
except ImportError:
    _AV_OK = False

_TMP_PATH = str(TMP_VIDEO)


class VideoRecorder:

    def __init__(self):
        self._container = None
        self._stream    = None
        self._start_t   = 0.0
        self._width     = 0
        self._height    = 0
        self.active     = False

    def start(self, width: int, height: int) -> bool:
        """Open MP4 output. Returns False if PyAV unavailable."""
        if not _AV_OK:
            return False
        os.makedirs(os.path.dirname(_TMP_PATH), exist_ok=True)
        if os.path.exists(_TMP_PATH):
            try:
                os.remove(_TMP_PATH)
            except Exception:
                pass

        self._width   = width
        self._height  = height
        self._start_t = time.time()
        try:
            self._container = av.open(_TMP_PATH, mode="w")
            self._stream    = self._container.add_stream("h264", rate=30)
            self._stream.width   = width
            self._stream.height  = height
            self._stream.pix_fmt = "yuv420p"
            # ms-precision timestamps so viewer can seek by action.t directly
            self._stream.codec_context.time_base = Fraction(1, 1000)
            self._stream.codec_context.options["preset"] = "ultrafast"
            self._stream.codec_context.options["g"]      = "60"  # keyframe every 60 frames (~2s) for fast seeking
            self.active = True
            return True
        except Exception:
            self._container = None
            self._stream    = None
            return False

    def push_frame(self, rgb_bytes: bytes):
        """Encode one RGB24 frame. Called from the main render loop — low overhead."""
        if not self.active:
            return
        try:
            t_ms = int((time.time() - self._start_t) * 1000)
            arr  = np.frombuffer(rgb_bytes, dtype=np.uint8).reshape(
                self._height, self._width, 3
            )
            frame = av.VideoFrame.from_ndarray(arr, format="rgb24")
            frame.pts       = t_ms
            frame.time_base = Fraction(1, 1000)
            for pkt in self._stream.encode(frame):
                self._container.mux(pkt)
        except Exception:
            pass

    def stop(self) -> Optional[str]:
        """Flush encoder and close. Returns tmp file path, or None."""
        if not self.active:
            return None
        self.active = False
        try:
            for pkt in self._stream.encode(None):
                self._container.mux(pkt)
            self._container.close()
        except Exception:
            pass
        self._container = None
        self._stream    = None
        return _TMP_PATH if os.path.exists(_TMP_PATH) else None
