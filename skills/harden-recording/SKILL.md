---
name: harden-recording
description: Harden a flaky keytap action recording by replaying it N times and fixing what breaks.
---

# Harden a keytap recording

Goal: turn a recording into a copy that passes N runs in a row on the connected phone. keytap decides pass or fail. You read its results and edit the copy. You never judge a run by eye.

## Where keytap is

KEYTAP = the folder that contains `replay.py`: the current folder if `./replay.py` exists, otherwise `__KEYTAP_DIR__`. Run every command from there. Recordings live in `$KEYTAP/data/recordings/<folder>/actions.json`.

## Commands

```
./replay.py --list                         recordings (name, date, steps)
./replay.py --dump                         what is on the phone's screen now, read-only
./replay.py <name> --repeat 5 --quiet      run it 5 times, final summary only
/opt/homebrew/bin/python3 recording.py --check <actions.json>    validate an edit
```

Exit code of `replay.py`: 0 all passed, 1 a step failed, 2 phone or uiautomator2 unreachable or bad name. Always pass `--quiet`. Details of the files and fields are in `reference/`.

## Before you start

1. Say what will happen: how many runs, and what they do for real (a message sent, an order created, data cleared). Replays send real traffic from the phone. Get a yes before the first run. Never run a flow that moves money or touches a production account without an explicit yes.
2. Ask before anything that changes the phone beyond tapping the app: `--reset clear`, `clear_data`, `permission` or `dev_option` steps, anything in Settings. Do not change phone settings to make a run pass.
3. Make a working copy, never edit the original:
   `cp -R data/recordings/<folder> data/recordings/<name>_v2`, then set `"name": "<name>_v2"` in the copy's `actions.json`. Next round `_v3`, and so on.
4. Validate the copy with `recording.py --check`.

## The loop (at most 5 rounds)

1. Run `./replay.py <copy> --repeat 5 --quiet`. It prints the pass count, the failing steps, the causes and the results folder.
2. If exit code 2: stop and report. The phone or uiautomator2 is the problem, not the recording.
3. If all passed: go to Done.
4. Read results small to large, and stop as soon as you know the fix:
   1. `<results>/summary.json`: `failed_steps`, `causes`, per-step `max_ms`, `retries`. It covers the whole round, so do not open every `run_NN/result.json`.
   2. ONE failing run's `run_NN/failure/failure.json`: `cause`, `detail`, `error`, `foreground`, `screen.clickable`. Pick one failing run, the others repeat it.
   3. Only if still unclear: search `hierarchy.xml` or `logcat.txt` for the one id, text or `FATAL EXCEPTION` you need. Do not read them whole.
   4. The screenshot last. Images cost the most.
5. Make ONE edit to the copy, for the step that failed most. Validate it. Run the next round.

Step numbers in `failed_steps` ("step 3 ...") are 1-based positions in `actions`.

## Cause to fix

| Cause or symptom | Edit |
|---|---|
| `element_not_found`, element is on screen under another id, text or description | Replace the selector with one from `screen.clickable` |
| A shared id (tab bar, list rows) taps the wrong thing | Add `index`, or better, select by a unique `content_desc` or `text` and clear `resource_id` |
| `element_not_found` and the screen was still loading (step `max_ms` is near its wait) | Set `timeout_ms` on that step: `max_ms` x 1.5, rounded up |
| Passes on rerun, fails now and then | `retries: 1` on that step only |
| Target is off screen in a list | Insert a `scroll_to` step (same selector, `direction`) before the tap |
| A coordinate `tap` | Open that screen, run `./replay.py --dump`, replace it with an `element_tap` using the selector it prints |
| `wrong_app_in_foreground` | Read `foreground`. A launch, dialog or permission prompt got in the way. Report it, and add a step only if the user agrees |
| `app_crash` | Not a recording problem. Report it with the `logcat.txt` lines. Edit nothing |
| `device_unavailable` | Infrastructure. Stop and report |
| `action_failed` | Read `error`. Usually a bad edit or an adb problem |

Selector rules: prefer `resource_id`, then `content_desc`, then `text`. Never use `text` that changes (names, counts, times, balances). `resource_id` is the full `pkg:id/name`. Replay tries `resource_id` first and taps the first match, so to use `content_desc` the `resource_id` must be empty. Keep `t` non-decreasing when inserting steps.

## Done

- 10 of 10 runs pass (`--repeat 10`), or 5 of 5 after the last edit, and no step needed a retry unless you set `retries` on purpose.
- Or after 5 rounds without that: stop and report what still fails and why.

## Report to the user

- Each edit: step number, before and after.
- Final pass count and, per step, median and max time from `summary.json`.
- What a pass does not prove: replay checks that every step ran, not that the result is right (a message really arrived, a screen really changed). Say how the outcome was or was not verified. There is no assertion step yet.
- Remaining weak spots: coordinate taps, steps with a shared id and `index`, text selectors.

## Do not

- Edit the original recording or keytap's own code. If a failure needs a tool change, tell the user.
- Change several steps in one round.
- Re-run to "see if it passes now" without an edit or a reason.
