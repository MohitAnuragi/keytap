# keytap - Implementation Notes

## Screen Mirroring Approach

### What we use: scrcpy → FIFO → ffmpeg → raw RGB frames

**Pipeline:**
```
Android device (H.264 HW encoder)
    ↓ USB / ADB
scrcpy --record=/tmp/keytap_stream.fifo --record-format=mkv --no-video-playback
    ↓ local named pipe (FIFO)
ffmpeg -f matroska -i FIFO -vf scale=WxH -f rawvideo -pix_fmt rgb24 pipe:1
    ↓ stdout
Python background thread → PIL Image.frombytes() → queue
    ↓
tkinter main thread (polls every 16ms) → ImageTk.PhotoImage → canvas
```

### Why this approach

- **scrcpy** uses Android's `MediaCodec` API for hardware H.264 encoding. The display surface is encoded directly on the GPU — no CPU screencap, no PNG compression overhead.
- **FIFO** (named pipe on laptop): scrcpy writes MKV stream to it, ffmpeg reads from it. No device storage used — data never touches the phone's disk.
- **MKV container**: streamable (no seek-back needed for index), unlike MP4.
- **ffmpeg**: handles H.264 decode + scale in one pass, outputs raw RGB24 bytes.
- **`Image.frombytes()`**: zero-decode — raw bytes → PIL Image directly, much faster than `Image.open()`.

### Approaches considered and rejected

| Approach | FPS | Why rejected |
|---|---|---|
| `adb exec-out screencap -p` | 2-5fps | GPU readback + PNG encode + ADB spawn per frame |
| Persistent screencap loop | 4-6fps | Saves ADB spawn but screencap still slow |
| minicap | 10-25fps | No prebuilt `.so` for SDK 34 (Android 14) |
| `screenrecord` → stdout | N/A | Tries to delete output path before writing, blocks special files |
| scrcpy → `/dev/stdout` | N/A | Logs mixed with video data on stdout |

### Fallback

If `scrcpy` or `ffmpeg` is not found in PATH, keytap falls back to the `adb exec-out screencap -p` loop (~3fps).

## Architecture

- **`_start_stream()`**: detects capabilities, starts appropriate capture thread
- **`_scrcpy_stream()`**: creates FIFO, starts scrcpy + ffmpeg subprocesses, reads raw frames in a loop. Auto-restarts on stream end.
- **`_screencap_loop()`**: fallback ADB loop
- **`_display_loop()`**: runs on tkinter main thread via `root.after(16ms)`. Only does `ImageTk.PhotoImage` + canvas update. Never blocks.
- **`_frame_q`**: `queue.Queue(maxsize=1)` — always keeps only the latest frame, drops stale ones.

## Dependencies

- `scrcpy` — `brew install scrcpy`
- `ffmpeg` — `brew install ffmpeg`
- `Pillow` — `brew install pillow` (or `pip3 install Pillow`)

## Running

```bash
python3 keytap.py
python3 keytap.py --serial DEVICE_SERIAL
python3 keytap.py --cols 4 --rows 10 --height 900
```

## Controls

| Key | Action |
|---|---|
| `f` | Hint mode - grid labels appear |
| `A1`..`H4` | Tap that grid cell |
| `Esc` | Cancel hint / quit |
| arrows | Move cursor (Shift=fast, Option=fine) |
| Space | Tap at cursor |
| `s` + arrow | Swipe in direction |
