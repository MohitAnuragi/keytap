# Contributing to keytap

Thanks for your interest in keytap! I'm actively looking for developers to help build it, and every contribution is welcome, from a typo fix to a new inspector.

## Ways to contribute

- **Report bugs.** Open an issue with your device or emulator, Android version, OS, the command you ran, and what happened versus what you expected.
- **Suggest features.** Open an issue describing the problem you want solved before building something large.
- **Fix bugs and improve stability** across devices, emulators, and Android versions.
- **Build features.** New inspectors, recorder and replayer improvements, suite and nightly tooling. The roadmaps in [`docs/`](docs/) are a good source of ideas.
- **Improve docs.** Clearer setup guides, examples, and walkthroughs.
- **Add tests** and improve Linux and Windows support.

## Development setup

Requirements: Python 3.10+, `adb` (`brew install android-platform-tools`), and optionally `ffmpeg`.

```bash
git clone https://github.com/<your-username>/keytap.git
cd keytap
git checkout -b my-change development
bash install.sh
```

Connect a device or start an emulator (`adb devices` should list it), then run from the repo folder:

```bash
python keytap.py
```

Modules start each other with `python -m keytap.<package>.<module>`, so run everything from the repo folder or through the wrappers (`keytap.py`, `replay.py`, `suite.py`, `nightly.py`, `dashboard.py`, `steer.py`).

## Project layout

See [Code layout](README.md#code-layout) in the README. In short, the Python package lives in `keytap/` and is split into `core`, `device`, `recorder`, `runner`, `profiler`, `inspectors`, `ui`, and `cli`. Put new code in the package that matches its job.

## Making changes

1. Branch from `development` (not `main`).
2. Keep each pull request focused on one change.
3. Match the style of the surrounding code: naming, comment density, and idiom.
4. Test against a real device or emulator, and exercise the feature you touched (and anything that shares its code path).
5. Update the README or `docs/` if you change user-visible behavior.
6. Don't commit personal data. `data/` and `suites/` are gitignored for that reason; keep recordings, traces, and credentials out of the repo.

## Commit messages

Write short, descriptive messages in the imperative mood, for example `Fix grid cursor wrapping at screen edge`. Explain the why in the body when it isn't obvious.

## Pull requests

Open the PR against `development` and include:

- What changed and why
- How you tested it (device or emulator, Android version)
- Screenshots or a short recording for UI changes
- Linked issues, if any

I'll review as quickly as I can. Small, focused PRs get merged fastest.

## Not sure where to start?

Open an issue saying what you're interested in and I'll point you at something that fits. Questions are welcome too.

By contributing, you agree that your contributions will be licensed under the project's [MIT License](LICENSE).
