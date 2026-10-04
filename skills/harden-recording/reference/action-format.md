# actions.json format

Top level: `{"name", "recorded_at", "serial", "device_resolution", "actions": [...]}`. Each action has `t` (ms since the recording started, non-decreasing) and `type`.

| type | fields |
|---|---|
| `element_tap` | `selector`, `fallback_coords`, `reliable`, `timeout_ms`, `retries` |
| `scroll_to` | `selector`, `direction` (`down` default, or `up`), `retries`. Swipes up to 8 times until the element is on screen. Does not tap |
| `tap` | `x`, `y` (screen pixels) |
| `swipe` | `x1`, `y1`, `x2`, `y2`, `duration` |
| `input_text` | `text` |
| `keyevent` | `keycode`, `label` |
| `launch` | `package` (starts the app's own launcher activity) |
| `launch_deeplink` | `url` |
| `force_stop`, `clear_data` | `package` |
| `permission` | `package`, `permission` (full name or `*`), `state` (`GRANTED` or `DENIED`) |
| `dev_option` | `label`, `state` |
| `wait` | `duration` (ms) |

## selector

`{"resource_id", "content_desc", "text", "class_name", "index"}`

- Lookup order: `resource_id`, then `content_desc`, then `text`. The first one set and found wins.
- `index` is 0-based among the on-screen elements that share the first identifier set. Recording adds it only when several share it. It applies to that first identifier only.
- `class_name` is informational.
- A selector needs at least one of `resource_id`, `content_desc`, `text`.

## Waiting

- `element_tap` waits as long as the user waited before that step in the recording (capped at 60s), then keeps looking `--wait` seconds more (default 10).
- `timeout_ms` on a step replaces both with one fixed wait.
- `retries` is the number of extra attempts after a failed step (default 0). It is not used when uiautomator2 is unreachable.
- `fallback_coords` is only used when the tap was recorded without any identifier (`reliable: false`). It never rescues a missing identified element.

## Mode

`./replay.py` defaults to `--mode timed`: the pauses of the recording are slept first (each capped at 10s). `--mode fast` uses 0.3s between steps.

Validate any edit with `recording.py --check <actions.json>`. `replay.py` also validates before it runs.
