# Result files

Results folder (printed as `results:`): `data/runs/<name>_<time>/` or the `--out` folder.

```
summary.json
run_NN/result.json     every step: status, ms, retries, error
run_NN/replay.log      what the replay printed
run_NN/failure/        only when a step failed
    failure.json  screenshot.png  hierarchy.xml  logcat.txt
```

## summary.json

`recording`, `runs`, `passed`, `failed`, `flaky` (some passed, some failed), `failed_steps` (`"step 3 element_tap: Chat": count`), `causes` (cause code to count), `total` (`min_s`, `median_s`, `max_s`) and `steps` (per step: `step`, `label`, `runs`, `min_ms`, `median_ms`, `max_ms`, `retries`, `failed`).

A failed run stops at its first failing step, so later steps have a smaller `runs` count.

## failure.json

`step` (1-based), `action` (label), `error`, `package`, `cause`, `detail`, `foreground` (`package`, `activity`), `screen` and `files`.

`screen`:
- `ids`: resource ids on screen, package prefix removed.
- `texts`: visible texts.
- `clickable`: up to 30 tappable elements, each with full `resource_id`, `content_desc`, `text` (omitted when longer than 80 characters) and `index` when several share the identifier. These can be pasted into a selector.

## Cause codes (first match wins)

| cause | meaning |
|---|---|
| `device_unavailable` | phone or uiautomator2 not reachable. Infrastructure |
| `app_crash` | `FATAL EXCEPTION` or ANR for the app in the last 300 logcat lines |
| `wrong_app_in_foreground` | another app is on screen. `foreground` says which |
| `assertion_failed` | an `assert` step did not hold in time. `error` has the check, the expected value and what was found |
| `login_failed` | (suites) the login flow failed before the test ran. `detail` has the underlying cause |
| `load_error` | (suites) a file problem: missing flow, unset `{{env:NAME}}`, a loop of `run` steps. Infrastructure, exit code 2 |
| `element_not_found` | the element is not on the current screen |
| `action_failed` | anything else. Read `error` |

LeakCanary adds a second launcher entry to debug builds. `launch` skips it, but if its screen is in the foreground the cause is `element_not_found` and `foreground.activity` names it.

## Suite results

`<suite>/.runs/<time>/`: `summary.json` (per test: `status` pass, flaky or fail, `login`, `login_ran`, `cause`, `detail`, `step`, `failure` folder, `screenshot`), `junit.xml`, `failures/<test>.png` (the screenshot of each failed test), one folder per test with `summary.json` and `run_NN/` as above, and `_login_NN/` for each login that ran. Read `summary.json` first, then that test's `failure/failure.json`.

A run also fails with `app_crash` when the app crashed during it even if every step passed (implicit check on logcat).
