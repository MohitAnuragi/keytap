# keytap

Control your Android device entirely from your keyboard. No mouse, no Android Studio, no background daemons. Pure Python + ADB.

Built for mobile developers and QA engineers who want fast, scriptable device control without leaving the terminal.

---

## Features

### Control
- **Grid navigation** — screen divided into a cell grid, move with arrow keys, tap with Space
- **Fast jump** — Shift+Arrow jumps 5 cells at once
- **Element tap** — press `e` to overlay labeled bounding boxes on every interactive element, type a 2-char label to tap it precisely

### Inspect
- **Layout Inspector** — live bounding box overlay on real UI elements with class, resource ID, and text labels
- **View Hierarchy Explorer** — browse the full UI tree from the command palette, scroll through nodes, and see live bound highlights on screen as you navigate

### Record & Replay
- **Action Recorder** — record any sequence of taps, swipes, and text inputs, then replay them precisely on any device
- **Network Capture** — optionally intercepts HTTP/HTTPS traffic during recording via mitmproxy; replay serves the exact same responses so UI state is reproduced without live network calls

### Debug
- **Memory Watchdog** — real-time GC pressure gauge and process death risk indicator for the foreground app, updated every 2s
- **Shared Prefs Viewer** — browse and search all shared preference keys for any installed package
- **Remote Config Viewer** — inspect remote config keys and values live, with search
- **Permissions Manager** — view and toggle runtime permissions for any package

### Command Palette (double Shift)
- Fuzzy search all installed packages
- **App actions**: launch, force stop, clear data, uninstall
- **Deep link launcher** — fire any `scheme://path` directly to the device
- **Clipboard paste** — paste Mac clipboard text into any device input field
- **Screenshot** — save a timestamped screenshot to your desktop

---

## Install

### One-liner (recommended)

```bash
curl -fsSL https://raw.githubusercontent.com/sidsharma2002/keytap/development/bootstrap.sh | bash
```

Clones the repo to `~/keytap`, installs Python dependencies, and registers the `keytap` command in your shell. Open a new terminal and run:

```bash
keytap
```

### Manual

```bash
git clone https://github.com/sidsharma2002/keytap.git
cd keytap
bash install.sh
```

### System requirements

- **adb** — `brew install android-platform-tools`
- **ffmpeg** — `brew install ffmpeg` (optional, used by screenrecord fallback)
- Python 3.10+

### Network capture setup (optional, one-time)

Enables keytap to intercept and replay HTTP/HTTPS traffic during recording. Debug builds only.

1. `install.sh` installs mitmproxy and generates the CA cert at `~/.mitmproxy/mitmproxy-ca-cert.pem` automatically.

2. Push the cert to your device and install it:
   ```bash
   adb push ~/.mitmproxy/mitmproxy-ca-cert.pem /sdcard/mitmproxy-ca.pem
   # On device: Settings > Security > Install certificate > CA certificate
   ```

3. Your app's `network_security_config.xml` must trust user CAs in debug builds:
   ```xml
   <debug-overrides>
       <trust-anchors>
           <certificates src="user" />
       </trust-anchors>
   </debug-overrides>
   ```

Once set up, network capture is automatic — keytap intercepts traffic during recording and serves mocked responses during replay. No extra flags needed.

---

## Usage

Connect a device, then:

```bash
keytap                          # default (scrcpy backend, best fps)
keytap --serial emulator-5554   # target a specific device
keytap --height 1000            # taller mirror window
keytap --backend screenrecord   # fallback capture
```

---

## Which thing do I use?

| Thing | What it is | Use when |
|---|---|---|
| `keytap.py` (`keytap`) | Interactive keyboard control of the phone, plus the recorder | Recording a new flow, or poking at the phone by hand |
| Recording | One saved sequence of steps (taps, text, asserts) in an `actions.json` | It is the raw material: record it, then edit the JSON |
| `replay.py` | Runs one recording headless, no window | A quick one-off: play this flow now (login aliases use it) |
| Flow (`suites/<x>/flows/`) | A recording meant to be reused, e.g. login or dismiss popups. Tests call it with a `run` step | The same steps are needed in many tests |
| Test (`suites/<x>/tests/`) | A recording with assertions; it passes or fails | You want a check that something works |
| Suite (`suite.py`, `suite.json`) | A folder of tests and flows plus defaults (retries, login policy, restart between tests) | Running several tests together and getting a pass/fail report |
| Run (`data/runs/...`) | Output folder of one execution: `result.json`, plus screenshot, hierarchy and logcat on failure | Debugging why something failed |
| `dashboard.py` | Local web page (http://127.0.0.1:8765) that follows runs live: what is running, every test and step, retries, failures, plus a "how a run works" page. Its Run panel can start and stop a suite run or a nightly (one at a time, through `run_suite.sh` and `run_nightly.sh`, behind a confirm dialog) | You want to see what a nightly or suite run is doing, or why it failed |
| `nightly.py` | Unattended overnight loop over the suites in `nightly.json`, with phone and battery checks and a morning report | Running everything while you sleep |

In short: build with the recorder, try a flow with `replay.py`, run a feature's tests with `suite.py run`, run everything unattended with `nightly.py`.

Turn the phone's animations off before replaying. With them on, steps that wait for a screen transition fail with `element_not_found`.

### Step options (in `actions.json`)

Optional fields on a step. A selector is the same object an `element_tap` uses, e.g. `{"text": "Groups"}`.

| Option | On | What it does | Use when |
|---|---|---|---|
| `"when": {selector}` | any step or `run` step | Runs only if the element shows within `when_timeout_ms` (default 8000), else the step is skipped and shown as `skip`. On a `run` step it guards the whole flow, decided once at its start | A screen only appears sometimes (OTP prompt, first-run intro). When the element is absent you pay the full timeout, so keep it short |
| `"when_else": [{selector}]` | next to a `when` | Say what shows instead when the `when` element is not coming. Whichever shows first decides, so a skipped step costs about a second, not the whole timeout. Ties go to `when` | A step needs a short wait to be safe but an absent screen should not cost the full timeout (optional onboarding screens) |
| `"unless": {selector}` | any step or `run` step | The opposite: runs only if the element is NOT on screen (polls 2 s) | Skip recovery steps when you are already where you want to be |
| `"expect": {selector}` | tap steps | Taps, then retaps until the selector appears | A tap gets dropped during a transition. Only for taps that are safe to repeat, never create or send |
| `"verify": true` | `input_text` | Checks the typed text is on screen, clears and retypes (up to 2 times) if not | Typed text arrives short or empty |
| `"needs"` | `suite.json` | `{"card_test": ["seed_test"]}`: `suite.py run` adds the prerequisite tests and runs them first; `suite.py check` errors if they are listed after | One test creates data another needs |
| `"blockers"` | `suite.json` | List of selectors of dialogs that may cover the app (`[{"text": "Not now"}, {"resource_id": "android:id/aerr_wait"}]`). When a step fails and is about to be retried, one screen read is checked against all of them, the ones found are tapped (up to 6 in a row), and then the step retries. Costs nothing when nothing fails | Surprise dialogs and popups, without a wait-and-check step for each one in every flow |
| `"reuse_ok"` | `suite.json` | List of tests that only read what their `needs` test left behind. `nightly.py` skips the prerequisite for them (`suite.py run --skip-needs seed_test`) while the seed passed earlier tonight and nothing that could change its state ran since; a retry always reseeds | Several tests read one seed and the nightly runs each test on its own |

**Model help** (`suite.py run --llm [MODEL] [--llm-scope dialogs|navigate]`, or the dashboard's "Local model help"): when a step fails and the `blockers` explain nothing, the local model (Ollama, started if needed, the model must already be pulled) looks at the screen, topmost layer first, and may act. `dialogs` closes dialogs with their own buttons only; `navigate` also allows back, scrolling and any tap that is not about money. At most 2 calls per test and 3 actions per call, and a test that only passes after the model acted is reported as helped ("Passed with help"). In `nightly.json`, `"steer": "dialogs"` turns it on for the night.

Placeholders in any string of an action: `{{param}}` from a `run` step, `{{ts}}` a per-run timestamp, `{{package}}` the `"package"` in `suite.json` (a `run` step's own `package` param wins), `{{env:NAME}}` an environment variable (kept out of files, shown as `***` in logs).

---

## Keybindings

| Key | Action |
|---|---|
| Arrow keys | Move cursor |
| Shift+Arrow | Jump 5 cells |
| Space | Tap at cursor |
| `e` | Toggle element overlay |
| `b` | Back |
| `h` | Home |
| `r` | Recent apps |
| `w` / `s` | Scroll up / down |
| Shift+Shift | Open command palette |
| Esc | Exit current mode |

---

## Code layout

```
keytap.py  replay.py  suite.py  nightly.py  dashboard.py     commands, thin wrappers (run them from the keytap folder)
install.sh  bootstrap.sh           install and one-liner setup
docs/                              roadmaps and design notes
skills/                            the harden-recording and harden-suite Claude skills (install.sh copies it to ~/.claude)
data/  suites/                     your recordings, traces, heap dumps and test suites (gitignored)
keytap/                            the Python package
  core/       config, paths, settings, utils, fonts
  device/     adb, actions, screen capture, logcat, element lookup
  recorder/   recording format, recorder, replayer, network capture and replay, timeline
  runner/     headless engine, run steps and placeholders, assertions, failure bundles
  profiler/   perf traces, heap snapshots, HPROF parsing and analysis
  inspectors/ permissions, developer options, shared prefs, remote config, memory
  ui/         the window, palette, element overlay, themes, timeline viewer
  cli/        the replay and suite commands
```

Modules start each other with `python -m keytap.<package>.<module>` (see `keytap/core/paths.py`), so run everything from the repo folder or through the wrappers.

## License

MIT
