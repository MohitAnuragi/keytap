# keytap — Roadmap

## Control

- **Long press** — hold `Space` triggers long-press (`adb shell input swipe x y x y 800ms`)
- **Pinch / zoom** — `+`/`-` keys fire two-finger swipe pair via ADB
- **Pull to refresh** — single key, swipes down from screen center
- **Macro recorder** — record tap/swipe sequence, replay with one key; great for login/onboarding flows
- **Screen search (OCR)** — Cmd+F scans current frame with pytesseract, jump cursor to matched text

## Inspect

- **Logcat viewer** — filtered logcat stream inside palette, type to grep by tag/package
- **Crash detector** — watches logcat for ANR/fatal, auto-pops crash summary + stacktrace in palette
- **SQLite browser** — list app DBs, show tables, run SELECT queries (extends shared prefs pattern)
- **Storage inspector** — browse `/data/data/<pkg>/files/` tree via `adb run-as`
- **App info** — quick view of versionName, versionCode, targetSDK, install date, APK size

## Debug

- **Network proxy toggle** — set/unset `adb shell settings put global http_proxy` for Charles/Proxyman
- **Dev options toggles** — overdraw, layout bounds, animation scale → all via `adb shell settings`
- **ADB shell REPL** — type arbitrary shell commands from palette, output shown inline
- **Feature flag editor** — filter SharedPrefs to boolean-only keys, toggle directly from list

## Productivity

- **Text snippet library** — saved reusable strings (test emails, phone numbers, OTPs), searchable, paste to device
- **Multi-device broadcast** — send same tap/input to all connected devices simultaneously
- **Screenshot to clipboard** — `adb exec-out screencap -p` → macOS clipboard, single key

## Performance

- **Palette subprocess pre-warm** *(done)* — spawn `palette_proc.py --persistent` at startup so tkinter + Python are already loaded; double-shift sends JSON to stdin instead of spawning a fresh process; eliminates 500-800ms open latency down to ~30-60ms
- **Package list disk cache** — cache `adb shell pm list packages` output to `data/packages.json` with mtime TTL; read from disk on startup (~5ms) instead of waiting for ADB (~200-500ms); refresh async in background
- **Clipboard pre-fetch** — pre-fetch clipboard value on a 5s timer so `_read_clipboard()` returns immediately at palette open instead of blocking the main thread for 50-100ms
- **u2 connection cache** — keep a module-level `u2.Device` reference in `elements.py` and reuse across `e` presses instead of calling `u2.connect(serial)` fresh each time; saves 100-300ms per hierarchy dump
- **Font load cache** — add a `_cache: dict` in `fonts.py` so repeated `fonts.load(size)` calls return a cached surface instead of re-opening the TTF file from disk each call
- **FPS cap reduction** — `clock.tick(120)` in `app.py` spins at 120 FPS against a ~30 FPS capture source; drop to `clock.tick(60)` to halve render-loop CPU with no visible difference
- **Capture bitrate from settings** — `capture.py` hardcodes `BITRATE = "8000000"` and never reads `settings.py` `capture.bitrate`; wire them together so the setting actually takes effect
- **Element renderer cache** — `elements_for_renderer()` rebuilds the full element list every frame in element mode; cache result and invalidate only when element state changes to cut per-frame CPU
