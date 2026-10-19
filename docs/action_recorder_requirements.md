# Action Recorder — Requirements

## Overview

Record sequences of device actions (taps, swipes, text input, app launches) and replay them on demand. Accessible via command palette.

---

## UX Flow

### Recording

1. Palette → "Action Recorder" → "Start Recording"
2. Palette closes. Status bar shows blinking `[REC]` indicator.
3. User performs actions normally (taps, swipes, element taps, key events, input text, launches).
4. Double-shift → palette opens with `[STOP RECORDING]` pinned as top entry (highlighted).
5. Select "Stop Recording" → inline name prompt in palette.
6. Saved to `data/recordings/<name>_<timestamp>.json`.

### Replay

1. Palette → "Action Recorder" → "Replay"
2. Lists all recordings in `data/recordings/` by name + date.
3. Select recording → preview action list shown in palette.
4. Choose replay mode: Timed / Fast / Step.
5. Replay runs. Status bar shows `[REPLAY 3/12] element_tap: btn_login`.
6. On finish or error: status bar reports outcome.

---

## Replay Modes

| Mode | Behavior |
|------|----------|
| Timed | Honors recorded `t` offsets (real-time, default) |
| Fast | Fixed 300ms between each action |
| Step | Press `Space` to advance one action at a time |

---

## Action JSON Format

File: `data/recordings/<name>_<timestamp>.json`

```json
{
  "name": "login_flow",
  "recorded_at": "2026-09-27T10:00:00",
  "serial": "emulator-5554",
  "device_resolution": [1080, 2400],
  "actions": [
    {
      "t": 0,
      "type": "element_tap",
      "selector": {
        "resource_id": "com.example.app:id/btn_login",
        "content_desc": "Login button",
        "text": "Login",
        "class": "android.widget.Button"
      },
      "fallback_coords": [540, 1400],
      "reliable": true
    },
    {
      "t": 1200,
      "type": "input_text",
      "text": "user@test.com"
    },
    {
      "t": 2000,
      "type": "tap",
      "x": 540,
      "y": 1800
    },
    {
      "t": 2800,
      "type": "swipe",
      "x1": 540, "y1": 1800,
      "x2": 540, "y2": 400,
      "duration": 300
    },
    {
      "t": 3500,
      "type": "launch",
      "package": "com.example.app"
    },
    {
      "t": 4000,
      "type": "launch_deeplink",
      "url": "example://home"
    },
    {
      "t": 4500,
      "type": "keyevent",
      "keycode": 4,
      "label": "BACK"
    }
  ]
}
```

---

## Action Types

| Type | Fields | Source |
|------|--------|--------|
| `element_tap` | `selector`, `fallback_coords`, `reliable` | ElementManager label tap |
| `tap` | `x`, `y` | Raw tap (no element context) |
| `swipe` | `x1`, `y1`, `x2`, `y2`, `duration` | actions.swipe |
| `input_text` | `text` | actions.input_text |
| `launch` | `package` | actions.launch |
| `launch_deeplink` | `url` | actions.launch_deeplink |
| `keyevent` | `keycode`, `label` | actions.keyevent |

Clipboard paste: not recorded (ignored by design).

---

## Element Selector Resolution (Replay)

For `element_tap`, resolution order on replay:

1. `resource_id` — exact match, most stable
2. `content_desc` — stable if accessibility labels set
3. `text` — works for static labels
4. `fallback_coords` — last resort, emits `[WARN]` in status bar

`reliable: false` is set at record time when resource_id, content_desc, and text are all empty (only coords available). Replay warns on these steps.

On replay, each `element_tap` does a fresh u2 hierarchy dump to find the current element position — handles scroll offset and minor layout shifts.

---

## Error Policy (Replay)

- Element not found + `reliable: true` → abort replay, report which step failed
- Element not found + `reliable: false` → use fallback_coords, warn, continue
- Action fails (ADB error) → abort and report

---

## Storage

- Directory: `data/recordings/`
- Filename: `<name>_<YYYYMMDD_HHMMSS>.json`
- No size limit enforced; user manages files manually

---

## Constraints

- Coordinates recorded in device pixel space (raw ADB coords), not pygame screen coords
- Recording warns on replay if `device_resolution` mismatches current device
- No coordinate scaling on resolution mismatch (warn only, user decides)
- Clipboard paste during recording: not captured

---

## Architecture

New files:
- `keytap/recorder/recorder.py` — `ActionRecorder` class: start/stop recording, event ingestion, save to disk
- `keytap/recorder/replayer.py` — `ActionReplayer` class: load recording, execute actions with mode/delay logic
- `keytap/recorder/recording.py` — dataclasses: `Recording`, `Action`, `ElementSelector`

Modified files:
- `keytap/device/actions.py` — emit events to `ActionRecorder` if recording active (observer hook)
- `keytap/ui/element_manager.py` — emit `element_tap` events with full selector on label tap
- `keytap/ui/palette_proc.py` — add "Action Recorder" top-level entry; show `[STOP RECORDING]` pin when recording; replay mode file picker + mode selector
- `keytap/ui/app.py` — hold `ActionRecorder` and `ActionReplayer` instances; wire palette results

---

## Out of Scope (v1)

- Loop / repeat count
- Pre-flight app launch before replay
- In-palette JSON editor
- Screenshot checkpoints / assertions
- Coordinate scaling on resolution mismatch
