# keytap

Keyboard-driven Android device controller. Mirror your Android screen and control it entirely from your Mac keyboard — no mouse, no touch.

## What it does

- Live screen mirror via scrcpy (falls back to ADB screencap)
- Grid overlay: divide screen into cells, navigate with arrow keys, tap with Space
- Element mode (`e`): dump UI hierarchy, overlay labeled bounding boxes, tap by typing 2-char label
- Command palette (`Shift+Shift`): fuzzy search packages, launch/force-stop/clear/uninstall apps
- Device shortcuts: `b`=back, `h`=home, `r`=recents, `w`/`s`=scroll
- View hierarchy explorer with live bound preview
- Memory Watchdog: GC pressure + process death risk from live ADB stats
- Shared prefs, remote config, permission viewer/toggler
- Clipboard paste, deep link launcher, screenshot

## Requirements

### System tools

| Tool | Install |
|---|---|
| Python 3.9+ | `brew install python` |
| ADB | Android SDK platform-tools, or `brew install android-platform-tools` |
| scrcpy | `brew install scrcpy` (optional, improves frame rate) |
| ffmpeg | `brew install ffmpeg` (required if using scrcpy) |

### Python packages

See [requirements.md](requirements.md).

## Setup

```bash
git clone https://github.com/your-username/keytap.git
cd keytap
pip install Pillow
pip install uiautomator2   # optional, enables element mode
python -m uiautomator2 init  # one-time device setup for element mode
```

## Usage

```bash
# connect device first
adb devices

# basic
python app.py

# specify device
python app.py --serial emulator-5554

# custom grid
python app.py --cols 12 --rows 28 --height 900
```

## Keybindings

### Navigation (cursor mode)
| Key | Action |
|---|---|
| Arrow keys | Move cursor one cell |
| Shift+Arrow | Jump 5 cells |
| Space | Tap at cursor |

### Device
| Key | Action |
|---|---|
| `b` | Back |
| `h` | Home |
| `r` | Recent apps |
| `w` | Scroll up |
| `s` | Scroll down |

### Modes
| Key | Action |
|---|---|
| `e` | Toggle element overlay mode |
| `Shift+Shift` | Open command palette |
| Esc | Exit current mode |

## Args

| Flag | Default | Description |
|---|---|---|
| `--serial` | auto | ADB device serial |
| `--cols` | 10 | Grid columns |
| `--rows` | 32 | Grid rows |
| `--height` | 800 | Mirror window height (px) |

## Contributors
* [@sidsharma2002](https://github.com/sidsharma2002/)
* [@MohitAnuragi](https://github.com/MohitAnuragi/)



## License

MIT
