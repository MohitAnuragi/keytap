---
name: steer-phone
description: Drive an Android phone step by step for a one-off check (run a flow, verify a screen, confirm clickstream events in logcat) with minimal tokens. Use instead of raw uiautomator or adb dumps.
---

# Steer the phone with small output

Never run `uiautomator dump`, `adb shell dumpsys`, `replay.py --dump` or a full `logcat` into the conversation: one hierarchy dump is about 90 KB (25k tokens). Use `steer.py`, whose output is about 150-300 tokens per step.

KEYTAP = the folder that contains `steer.py`: the current folder if `./steer.py` exists, otherwise `__KEYTAP_DIR__`. Run every command from there. Set `ANDROID_SERIAL` when several phones are attached.

## Commands

```
./steer.py screen                       visible text and tappable elements, numbered
./steer.py tap 5                        tap element 5 of the last snapshot, then print the new screen
./steer.py tap "Accept" [2]             tap the first (or 2nd) tappable whose label contains the text
./steer.py type "hello"                 type into the focused field
./steer.py key back|home|enter|recents
./steer.py swipe up|down|left|right     up = see what is below
./steer.py launch <package>
./steer.py do "tap Chat" "tap Groups" "key back"   several steps in one call, prints the final screen
./steer.py flow <dir> [--package p] [--param k=v]   replay a keytap flow (login, open a tab) in one call
./steer.py see "is a dialog open?"      local vision model answers in 1-2 sentences; the image never reaches you
./steer.py mark                         remember "now" in logcat
./steer.py logs "regex" [--max 5]       match count plus a few sample lines since the mark
```

Every action prints the new screen, or `screen unchanged`. Add `--quiet` to any action and it prints one line (`ok`) instead; use it for a step whose result you do not need to read. After a quiet action the numbers are stale: tap by label, or run `screen` first. Numbers are only valid for the latest printed screen: after a screen change, read the new numbers, do not reuse old ones.

## Batching and flows

- `do` runs its steps back to back and stops at the first one that fails, then shows the screen. Taps must use a label, not a number (the numbers of later screens are unknown), and labels with money or destructive words (pay, transfer, confirm, delete, remove, leave, block...) are refused: tap those singly after reading the screen.
- `flow` replays a recording from the suites with keytap's own replayer, for deterministic stretches such as login or "open the Groups tab". It prints one line, `flow <name> ok` or `FAILED at step N`, then the screen unless `--quiet`. Pass `--package <pkg>` when the flow uses `{{package}}`. A flow that wipes the app's data (`clear_data`, as the login flows do) is refused unless you pass `--allow-clear`: ask the user first, because the login is lost. The flow clears logcat when it starts; `mark` and `logs` keep what was seen before.
- Prefer a flow for anything a suite already does, and steer only the new part.

## How to work

1. `screen` once, then act. Do not call `screen` again after an action, its output already holds the result.
2. Prefer `tap "label"` over numbers when the label is unique: it survives a re-render.
3. Clickstream or any logged-event check: `mark`, run the flow, then `logs "<event name or tag>"`. Read the count and the sample, not the whole log. If `0 matches`, widen the regex once before concluding the event did not fire.
4. The screen text is the source of truth for what is on screen. Use `see` only for things the tree cannot say (colors, icons without a label, overlap, whether something is visibly cut off), and treat its answer as a hint. It is unreliable for state such as which tab is selected: on a real screen it named the big Pay button as the selected tab when Chat was. Verify state from the screen text or by acting.
5. Do not tap anything about money or deletion without an explicit yes from the user.
6. Stop and report when `screen` fails with "no hierarchy" (phone missing, locked, or uiautomator busy). Do not start a suite run while steering.
