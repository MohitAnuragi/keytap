# keytap Replay Stability - Roadmap

Goal: replay recorded journeys reliably and headless, so they can run as nightly smoke and regression tests on a home device farm (real phones attached to a small always-on host, with a VPS for scheduling, artifacts and analysis).

Findings below come from reading `replayer.py`, `recorder.py`, `recording.py`, `network_recorder.py` and `network_replayer.py`. Items marked "unverified" were not tested.

---

## Shipped (first batch)

Headless run: `python replay.py <recording> --serial <id> --repeat 10 --reset clear`

- **`replay.py`:** no window. Checks the recording with `check_actions_file`, wakes the phone, optionally force-stops (`--reset stop`) or clears the app (`--reset clear`) before each run, and runs the recording `--repeat N` times. Writes `data/runs/<name>_<time>/run_NN/result.json` (per-step status, ms, retries), `summary.json` (passes, which steps fail, causes, flaky flag, and timings: whole run and per step min/median/max, retries and failures, over the runs that reached each step) and a `replay.log`. `--network` serves the recorded flows. Exit code: 0 all passed, 1 a step failed, 2 phone or uiautomator2 unreachable.
- **Actions run inline during replay** (`actions.set_sync`). Before, every action was a fire-and-forget thread, so `force_stop`, `clear_data` and `launch` could overlap. The GUI replay gets this too.
- **Element wait follows the recording:** for an `element_tap`, replay waits as long as the user waited before that tap in the recording (the gap since the previous action, capped at 60s), then keeps looking for `--wait` more seconds (default 10). The status line shows `[WAIT] not there after the recorded Xs, trying up to Ys more` when it enters the second phase. In timed mode the gap is already slept before the step, so only the extra seconds apply. `timeout_ms` on a step replaces both with one fixed wait; `retries` gives extra attempts. A lookup call can block while the app is busy (seen: the Chat tab step took 16-21s on a cold start), so the deadline is checked between calls and is a soft limit.
- **Selector `index`:** recorded automatically when several on-screen elements share the identifier replay looks up first. Replay passes it to uiautomator2 as `instance`. Absent when the identifier is unique. Recordings made before this keep working (first match).
- **`scroll_to` action:** hand-written, `{"type": "scroll_to", "selector": {...}, "direction": "down"}`. Swipes up to 8 times until the element is on screen. It does not tap, so follow it with an `element_tap`.
- **Failure bundle** (`failure_bundle.py`), in `run_NN/failure/`: `screenshot.png`, `hierarchy.xml`, `logcat.txt` (last 300 lines) and `failure.json` with the step, error, cause code, foreground app and the ids and texts on screen. Cause codes: `device_unavailable`, `app_crash`, `wrong_app_in_foreground`, `element_not_found`, `action_failed`.
- **`launch` picks the app's launcher activity** (`actions.launcher_activity`). Debug tools such as LeakCanary add a second launcher entry to the same package (`leakcanary.internal.activity.LeakLauncherActivity` on GoHost staging), and `monkey -p <pkg> -c LAUNCHER 1` picked one at random, so a launch sometimes opened LeakCanary. It now queries the launcher activities, skips LeakCanary and runs `am start -n`. Falls back to monkey if the query finds nothing. Also applies to the palette's launch action.
- **Validator** knows `scroll_to`, selector `index`, `timeout_ms`, `retries` and `direction`.

Not done yet: wait-for-stable, screen fingerprints, network matching fixes, popup handler. Run a real journey with `--repeat 10` first and let the failures say which one is next.

Checked with a fake device only (adb and uiautomator2 stubbed). Not yet run on a real phone: the index lookup (`instance`), `scroll_to` swipes, `wm dismiss-keyguard`, and the logcat crash match.

---

## Context

- The VPS has no `/dev/kvm` (`modprobe kvm_intel` fails with `Operation not supported`), so the Android emulator is ruled out there. Devices are real phones at home.
- Replay today runs inside the GUI app: `app.py` starts `ActionReplayer` and `NetworkReplayer`, and results go to a status bar. A nightly run needs a headless CLI.
- Not yet checked: whether `actions.py` (used by the replayer) imports GUI modules. Check before building the CLI.

---

## Gaps in replay (`replayer.py`)

1. **Time-based, not state-based.** Timed mode sleeps for the recorded gap, capped at 10s. Fast mode sleeps a fixed 300ms. Only `element_tap` waits for anything. `tap`, `swipe`, `launch`, `launch_deeplink` and `keyevent` never wait for the screen to be ready. Any slow load makes the next step fire early.
2. **Element wait is a hardcoded 2s**, not configurable per step.
3. **Ambiguous selectors.** `_find_element` takes the first match of `resourceId`, then `description`, then `text`. In a list, repeated ids make replay tap the first row, not the recorded one. `ElementSelector` has no index, parent or sibling context. Text selectors on dynamic content (names, balances, timestamps) break when data changes.
4. **Fallback coordinates rarely help.** `reliable` is true whenever any identifier was recorded, so a missing identified element fails after 2s. Fallback coordinates are used only when there was no identifier.
5. **No assertions.** A step passes if its action ran. A tap that lands on the wrong screen is noticed only when a later element is missing.
6. **Coordinate taps and swipes depend on resolution and content.** The replayer only warns on a resolution mismatch. Swipes are raw coordinates, with no scroll-until-visible.
7. **No handling of unexpected UI:** permission dialogs, promos, rating prompts, ads, system dialogs. No optional-step flag, no dismiss rule.
8. **Stops at the first failed step**, with no per-step retry and no distinction between a flaky miss and a real failure.
9. **No failure artifacts.** `on_done` gets a step number and an error string only.
10. **uiautomator2 connection is fragile.** If `u2.connect` fails, `_u2_dev` is set to `None` silently and the failure shows later as "element not found". No reconnect after an adb drop, no device health check before the run.

## Gaps in recording (`recorder.py`, `recording.py`)

11. **No starting state recorded:** screen, login state, permissions, dev options, locale, orientation, whether data was cleared. Replay cannot reset or verify these.
12. **No per-step snapshot** of the expected screen. This is what fingerprints and baselines need.
13. **`wait` is a fixed duration only.** No wait-for-element, wait-for-gone or wait-for-screen.
14. **`input_text` is naive:** no focus check, does not clear existing text, stores text in plain form (including passwords and OTPs).
15. **Only actions made through keytap are recorded.** Touches made directly on the phone are missed.
16. **The validator is structural only.** `check_actions_file` cannot tell whether a selector exists on any screen.

---

## 1. Wait-for-stable before every step

Poll the app's view hierarchy until two consecutive dumps match, with a per-step timeout (default about 10s). Replaces the sleeps.

Exceptions and how to handle them:

**The screen never goes stable**
- Shimmer or skeleton loaders, spinners, Lottie, video, auto-scrolling banners, countdown or OTP-resend timers, clocks, live tickers, maps, chat with incoming messages.
- Compare a normalized dump: app package only, volatile attributes (bounds and similar) dropped, plus a per-step ignore list.
- On timeout, do not fail. Proceed with an "unstable" warning if the target element is present.

**The screen is stable too early**
- A static loading state (skeleton, spinner with no changing attribute) looks stable. A transition can be caught before it starts, so the old screen is "stable" twice.
- Stability alone is not enough. Also require a positive condition: the target element is present, no `ProgressBar` or loading ids on screen, and optionally no in-flight proxy requests.
- For actions that should change screens, wait for the first change from the pre-action dump (short timeout, 1-2s), then for stability.

**The dump is blind or expensive**
- Flutter (`gochat-flutter`): without semantics enabled the tree is nearly empty, so it looks permanently stable and says nothing. Compose screens need `testTagsAsResourceId` for ids. WebViews are often sparse. For these, fall back to a fixed delay or a screenshot-region comparison.
- A heavy screen (long RecyclerView) can take 0.5-1s per dump. Poll at a sensible interval and dump once per poll.
- uiautomator dump can fail or hang during animations. Treat a dump error as "not yet", not as failure.

**System noise**
- Keyboard show and hide, status-bar clock, heads-up notifications, toasts and snackbars that vanish. Filter to the app's package.
- Provide a per-step `no_wait` option for steps that assert on transient UI.

---

## 2. Screen fingerprint per step

Shape, stored on the action, hand-editable and checked by `check_actions_file`:

```json
"expect": {"activity": "com.x.HomeActivity", "ids": ["home_feed", "bottom_nav"], "min_match": 1.0}
```

**Build it from several replays, not the single recording.** A bake command (or the Claude skill) replays the journey N times, dumps the app-package hierarchy after each step, and then:

1. Keeps only ids present in every run, which removes transient ones.
2. Drops ids that appear on almost every screen (toolbar, bottom nav), since they do not distinguish screens.
3. Keeps the top 5-8 distinctive ids per step, weighted by how rarely they occur across the recording's steps.
4. Adds the resumed activity (`dumpsys` or u2 `app_current`). In single-activity apps it is the same everywhere, so the ids carry the signal.
5. Skips steps where nothing should change: `input_text`, scrolling.

**Check at replay.** After the action and the stability wait, test the activity and required ids. On mismatch, keep polling until the step timeout (the screen may still be loading), then fail with the cause attributed to this step:

```
step 6 wrong screen: expected [home_feed, bottom_nav], missing [home_feed]
closest recorded screen: step 3 (login). tap probably did nothing.
```

The "closest recorded screen" line compares the actual ids against every fingerprint in the recording.

**Fallbacks.** For Flutter or Compose screens with no ids, use a few stable `content-desc` or text labels, or a perceptual hash of a masked, downscaled screenshot. A popup shows up as a mismatch, which is also the trigger for the popup handler.

---

## 3. Network replay

### What exists

- `NetworkRecorder` (mitmproxy) captures flows in "full dump" mode and stores them in `network.json`.
- On replay, `app.py` (`replay` branch, about line 640) starts `NetworkReplayer` when the recording has flows. It sets the device proxy (`adb reverse` plus `settings put global http_proxy`) and serves recorded responses.
- `_extract_tokens` only rewrites request bodies, which replay ignores, so it does not affect replay.

### Gaps

1. **Match key is method plus path only.** Query string and request body are ignored. A `POST /graphql` or any single-endpoint API returns responses in order regardless of body. `?page=2` gets whichever response is next.
2. **Sequential round-robin with wrap-around** (`entries[idx % len(entries)]`). Extra calls (retries, polling) get the first response again. A different call order gives wrong responses.
3. **Unmatched requests pass through to the real backend**, silently. A run can be half mocked and half live, with no report of which calls were not covered.
4. **The recorder drops responses before storing them:** content types outside a whitelist, bodies over `MAX_BODY_BYTES`, excluded paths, out-of-scope hosts. Those calls always hit the real network on replay.
5. **Interception limits (unverified on the GoHost QA build).** It relies on the global `http_proxy` setting and a trusted CA. Apps that bypass the system proxy, pin certificates, or do not trust user CAs (Android 7+ unless configured), plus websockets, gRPC and HTTP/3, will not be captured or mocked.
6. **It lives inside the GUI app.** mitmproxy runs as a thread in the keytap process, started from `app.py`. A headless CLI has to start and stop it itself. `adb reverse` assumes the phone is attached to that host.
7. **Mocked responses return instantly,** with no recorded latency. Good for determinism, but hides timing bugs.
8. Actions-only recordings have no flows, so they replay against the live backend.

### Fixes

- Match on method, path and a normalized query (ignoring volatile params), plus a request-body hash or key subset for POSTs.
- Stop wrapping: after the last recorded response, repeat the last one, or flag it.
- Strict mode for CI: block or fail unmatched requests, and write an "unmatched calls" list into every run report.
- Move proxy start and stop into the headless CLI.

Fix network matching before relying on it for smoke assertions. Wrong mocks look exactly like app bugs.

---

## Other improvements

- **Richer selectors:** record `index` among same-id siblings, the nearest labeled parent and neighboring text. Prefer stable ids over text. Mark text selectors on dynamic content as unreliable at record time.
- **Per-step options in the action:** `timeout_ms`, `optional`, `retries`, `wait_for` (an element to wait for before the action), `no_wait`.
- **Popup handler:** a global rule list. If a known dialog appears (permission prompt, rating, promo), dismiss it and retry the step.
- **Failure bundle** written to a run folder: screenshot, hierarchy dump, logcat tail, step index, selector used.
- **Start-state block** in the recording and a reset step in replay: force-stop, clear data, re-login, apply recorded permissions and dev options, check resolution.
- **u2 health:** check before the run, reconnect on failure, and report infrastructure errors separately from test failures.
- **Scroll-to-element action** instead of raw swipes when the target is a list item.
- **Mask secrets** in recorded `input_text`.

---

## Headless CLI

`python replay.py <recording> --serial <id> --out <run folder>`

- No GUI imports (verify `actions.py` first).
- Exit code nonzero on a failed step.
- JSON result per step: status, duration, unstable flag, matched fingerprint, retries.
- Failure bundle on any failed step.
- Starts and stops the network replayer itself, with strict mode as a flag.
- Health check before the run: `adb devices`, battery level, screen on, free storage. Skip and report a phone that fails it as an infrastructure problem.

---

## Build order

1. Headless CLI with exit code and per-step JSON result.
2. Wait-for-stable with the exceptions above.
3. Failure bundle.
4. Fingerprint bake and the replay assertion.
5. Network matching fixes, strict mode and the unmatched-calls report.
6. Per-step options, popup handler, start-state block and reset.
7. Richer selectors and scroll-to-element.
8. A Claude skill that runs the bake loop, proposes ignore rules and selector fixes, and promotes a recording to "stable" only after N consecutive passes. Pass or fail stays deterministic. An LLM is never the judge in CI.

## Open questions

- Does `actions.py` import any GUI module? Decides how much work the headless CLI is.
- Does network interception work on the GoHost QA build (proxy honored, CA trusted, pinning)?
- Does the QA build's DexGuard block rooted or instrumented environments (affects uiautomator2)?
- Is a company app and test account on personal hardware allowed by employer policy?
- Login and OTP: is there a test account that skips OTP?
- Per-device baselines: screen size and Android version change hierarchies, so fingerprints and ignore lists are stored per device model.
