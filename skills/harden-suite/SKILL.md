---
name: harden-suite
description: Build a keytap test suite from recordings, add assertions and login, and harden it until it passes repeatedly.
---

# Build and harden a keytap test suite

Goal: a suite folder whose tests pass N runs in a row and whose passes mean the app did the right thing. keytap decides pass or fail. You read its results and edit files. For fixing one flaky recording, use the `harden-recording` skill; this one is about the suite as a whole.

## Where keytap is

KEYTAP = the folder that contains `suite.py`: the current folder if `./suite.py` exists, otherwise `__KEYTAP_DIR__`. Run every command from there. Suites are folders the user chooses (usually next to the app, not in keytap's git).

## Commands

```
./replay.py --list                              recordings you can add
./suite.py init <dir> --package <pkg>           create suite.json, flows/, tests/
./suite.py add <dir> <recording> --as test|flow copy a recording in
./suite.py check <dir>                          validate, follow run steps, warn about fragile steps
./suite.py run <dir> [test ...] [--tag t] [--repeat N] [--quiet]
./suite.py show <dir> [run]                     summary of a run
./suite.py checks                               the assertions you can use
./replay.py --dump                              what is on the phone's screen now
```

Exit code: 0 all passed, 1 a test failed or was flaky, 2 phone, uiautomator2 or a file is the problem (stop and report). Always pass `--quiet`. Field details and failure causes: the `harden-recording` skill's `reference/` folder, and `./suite.py --help`.

## Before you start

1. Say what runs for real: which tests, how many rounds, what each does (messages sent, orders created, data wiped by `login: always`). Get a yes before the first run. Never run flows that move money or touch production without an explicit yes.
2. Ask before anything that changes the phone beyond the app: `clear_data`, permissions, Settings.
3. Credentials go in `{{env:NAME}}` and the user sets the variable. Never write a password, PIN or OTP into a file. An OTP login cannot be automated: tell the user.

## Phase 1: build

1. Ask which recordings are tests and which are flows (login, onboarding, open-a-chat). Anything every test starts with is a flow.
2. `init`, then `add` each. Do not edit the originals in `data/recordings`.
3. Login: put it in `flows/login`, set `suite.json` `login.flow`, `login.default` (`once` unless the user wants a clean account per test), and `login.check`: a selector visible only when logged in (find it with `./replay.py --dump` on the home screen). Mark the password step `secret` with `{{env:NAME}}`.
4. Order matters: `tests` is a list in run order when tests depend on state, a glob when they do not. Prefer independent tests.
5. `./suite.py check <dir>` and fix every error. Treat warnings as a to-do list.

## Phase 2: assertions

A test with no `assert` only proves the steps ran. Every test needs at least one assertion on the outcome.

1. Per test, run it once, `--dump` the final screen, pick an element that proves the result (stable id first).
2. Add `assert` after the step that causes it. `./suite.py checks` lists the checks. For text a test creates, use `{{ts}}` in both the input and the assertion value so old data cannot satisfy it.
3. Prove it can fail: wrong `value` once, expect `assertion_failed`, restore.
4. A `gone` check passes if the element never appeared: pair it with an earlier `visible`.
5. Unsure assertion: `"soft": true`, tighten after 10 of 10.
6. Never assert on text that changes (balances, times, counts, names).

## Phase 3: harden (at most 5 rounds)

1. `./suite.py run <dir> --repeat 3 --quiet`. It prints per-test status, causes and the results folder.
2. Read small to large, stop when you know the fix: `<dir>/.runs/<time>/summary.json`, then ONE failing test's `failure/failure.json`, then a targeted search of `hierarchy.xml` or `logcat.txt`, the screenshot last.
3. Decide where the fault is before editing:
   - `login_failed`: harden `flows/login`. Every test fails together, so fix it first.
   - Same step fails in several tests: it is in a shared flow. Fix the flow once.
   - One test fails: fix that test's step per the `harden-recording` cause table.
   - Passes alone, fails in the suite: tests share one phone and account. Leftover state, wrong order or a login policy. Use `login: always` or `before_each: restart` for that test, or reorder. Do not paper over it with long waits.
   - `app_crash`, `device_unavailable`: report, edit nothing.
4. ONE edit per round, then `check`, then run again. Re-run only the failing test with `./suite.py run <dir> <test> --repeat 5 --quiet`, then the whole suite once.

## Done

- Whole suite passes 3 rounds in a row, no test flaky (exit 0), and every test has an assertion.
- Or after 5 rounds: stop and report what still fails and why.

## Report to the user

- Suite layout: flows, tests, login policy per test.
- Each edit: file, step, before and after.
- Per test: assertion added and what it proves; pass count.
- What a pass does not prove: anything not asserted, backend state, the other side of a message.
- Weak spots: coordinate taps, `index` selectors, text selectors, `gone` checks, tests that depend on order or on a shared account.

## Do not

- Commit suites to keytap's git. Edit keytap's own code. Change several things in one round.
- Put credentials in files. Weaken an assertion to make a run pass when it found a real bug: report it.
- Re-run without an edit or a reason.
