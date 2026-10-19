# Requirements

## Python dependencies

| Package | Required | Notes |
|---|---|---|
| `Pillow` | Yes | Image processing for screen mirror and grid overlay |
| `uiautomator2` | No | Element overlay mode (`e` key), memory watchdog, view hierarchy |

## Install

```bash
pip install Pillow
pip install uiautomator2  # optional
python -m uiautomator2 init  # one-time: installs agent APK on device
```

## System tools

| Tool | Required | Notes |
|---|---|---|
| `adb` | Yes | Android Debug Bridge — Android SDK platform-tools |
| `scrcpy` | No | Higher frame rate mirror (falls back to screencap without it) |
| `ffmpeg` | No | Required alongside scrcpy for video decoding |

### macOS install

```bash
brew install android-platform-tools
brew install scrcpy
brew install ffmpeg
```

### Linux install

```bash
sudo apt install adb scrcpy ffmpeg
```

## Python version

Python 3.9+ required (uses `sys.stdlib_module_names` for detection and `str.removeprefix`).
