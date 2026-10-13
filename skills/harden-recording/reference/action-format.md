# actions.json format

Top level: `{"name", "recorded_at", "serial", "device_resolution", "actions": [...]}`. Each action has `t` (ms since the recording started, non-decreasing) and `type`.

| type | fields |
|---|---|
| `element_tap` | `selector`, `fallback_coords`, `reliable`, `timeout_ms`, `retries`, `optional` (skip the step if the element is not on screen within `timeout_ms`; set a short `timeout_ms`, for example 4000) |
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
| `assert` | `check`, `selector` and `value` as the check needs, `timeout_ms`, `soft`. Waits until the check holds, like an element tap. `soft: true` only warns. `./suite.py checks` lists the checks and what each needs |
| `run` | `flow` (folder relative to the suite, for example `flows/login`), `params`. Expanded before the replay, so a failure names the flow step: `login step 4 <- send_message step 1`. Needs a suite folder; flows may nest 3 deep |

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

Validate any edit with `python3 -m keytap.recorder.recording --check <actions.json>` (from the keytap folder). `replay.py` also validates before it runs.

## Placeholders (in any string of an action)

- `{{name}}`: a param from the `run` step (or the test's `login_params`).
- `{{ts}}`: a timestamp, the same for the whole run and different for every run. Use it for unique text.
- `{{env:NAME}}`: an environment variable. An `input_text` that used one is shown as `***` in logs. A missing variable is a `load_error`.

## Test header (top level of a test's actions.json)

`tags`, `description`, `login` (`once`, `always` or `never`), `login_params`, `before_each` (`restart` or `none`).
