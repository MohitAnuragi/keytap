# keytap SDK — Roadmap

In-app debug session recorder SDK. Developer enables a flag for a user; that user records a session and exports a dump file; developer debugs from it. Companion to the keytap desktop tool — same workflow, self-serve from inside the app.

---

## Concept

```
Developer enables flag (remote config / hidden trigger)
        ↓
User sees [REC] UI in app
        ↓
User records session → taps Stop & Export
        ↓
ZIP dump shared via OS share sheet (email, Slack, ticket)
        ↓
Developer runs process_dump.py → structured network + log timeline
```

---

## Architecture

### Two-process model (LeakCanary pattern)

Same APK, separate Android process for screen recording. Isolates MediaCodec CPU/memory from main app.

```
Main app process
├── OkHttp interceptor  → appends raw entries to internal file
├── Timber/log capture  → appends log lines to internal file
└── on START: acquires MediaProjection token → IPC to RecorderService

Companion process (:recorder)
├── RecorderService (foreground, mediaProjection type)
├── Receives token → MediaCodec pipeline → screen.mp4
└── on STOP: reads network + log files → packages ZIP → share sheet
```

Manifest declaration (only change needed to split process):
```xml
<service
    android:name=".recorder.RecorderService"
    android:process=":recorder"
    android:foregroundServiceType="mediaProjection" />
```

### Data flow

- Network + logs: file-based IPC — main process appends to internal storage, companion reads on stop. No streaming IPC.
- Screen recording: companion process owns it entirely after receiving token.
- ZIP packaging: companion process, triggered by stop signal.

---

## Core Modules

| Module | Responsibility | Est. LOC |
|--------|---------------|----------|
| `SessionManager` | start/stop state machine, flag check, UI trigger | ~200 |
| `NetworkInterceptor` | OkHttp interceptor, body capture, size/type gate, raw append | ~300 |
| `LogCapture` | Timber tree / custom logger → ring buffer → file append | ~150 |
| `ScreenRecorder` | MediaProjection token acquisition, passes to companion | ~200 |
| `RecorderService` | Companion process service, MediaCodec pipeline, MP4 write | ~400 |
| `RawBuffer` | ConcurrentLinkedQueue, memory cap, eviction policy | ~150 |
| `Exporter` | ZIP builder, metadata.json, raw format writer | ~300 |
| `Redactor` | Strip Authorization header, configurable field rules | ~200 |
| `RecordingUI` | Floating [REC] indicator, stop button, share sheet trigger | ~300 |
| `Config` | SDK init, scope filters, size caps, flag interface | ~150 |

**Android v1 total: ~2350 LOC, ~15 files**

---

## Dump Format

```
session_dump_<userid>_<timestamp>.zip
├── metadata.json     # app version, device model, OS version, session start/end timestamps
├── network.jsonl     # one raw entry per request, newline-delimited, chronological
├── logs.txt          # timestamped log lines (app logs only — no system logcat)
└── screen.mp4        # optional, only if user approves MediaProjection permission
```

### Raw capture strategy

No JSON serialization during session. No redaction on device (except Authorization header strip). All structuring, correlation, and sanitization deferred to host-side processing script.

Network entry (raw, tab-delimited line + length-prefixed body block):
```
<epoch_ms>\t<method>\t<url>\t<status>\t<body_size_bytes>\n<body>
```

---

## Enablement

Two mechanisms, both supported:

1. **Remote flag** — Firebase Remote Config / LaunchDarkly targeting rule per `user_id`. App checks on launch, shows "Record session" in Settings. Zero special builds.
2. **Hidden trigger** — Long press on app version string in Settings. Works on any build, no server needed.

SDK exposes a `DebugRecorder.isEnabled(): Boolean` hook — caller decides how to evaluate the flag. SDK does not depend on any specific remote config library.

---

## Performance Constraints

### On-path costs (unavoidable)

- **Response body double-buffer**: OkHttp body stream is consume-once. Must read bytes, rebuild response. ~1-3ms per intercepted response. Gated by content-type + size check before reading.
- **Log ring buffer write**: `ConcurrentLinkedQueue.add()` ~100ns. Not measurable.

### Off-path (zero request-path cost)

- File append, ZIP packaging, all processing — background thread / companion process.
- Screen recording: hardware MediaCodec, same as any third-party screen recorder app. Not additional overhead introduced by SDK.

### Size gates (applied before body read)

```kotlin
if (!contentType.startsWith("application/json")) return response   // skip binary
if (contentLength > 50_000) return response                        // skip large payloads
```

---

## Host-Side Processing Tool

`process_dump.py` — runs on developer's machine, not shipped in SDK.

```
process_dump.py
├── parser.py       # raw format → structured objects
├── redactor.py     # configurable PII/token rules, JWT scrubber
├── har_export.py   # → .har (opens in Charles / Proxyman / browser devtools)
├── timeline.py     # correlate network calls + log lines by timestamp
└── cli.py          # argparse entrypoint
```

```bash
python process_dump.py session_dump_20261001.zip --redact --format har
python process_dump.py session_dump_20261001.zip --redact --format timeline
```

Est. ~800-1000 LOC Python. Reusable regardless of app platform.

---

## Milestones

### v1 — Android, internal use

- [ ] `NetworkInterceptor` + raw file writer
- [ ] `LogCapture` Timber tree + ring buffer
- [ ] `ScreenRecorder` + `RecorderService` (companion process)
- [ ] MediaProjection token IPC flow
- [ ] `Exporter` ZIP builder
- [ ] `Redactor` Authorization header strip
- [ ] `RecordingUI` floating indicator + share sheet
- [ ] `SessionManager` + `Config` init API
- [ ] `process_dump.py` host tool (parser + HAR export + timeline)
- [ ] Sample app integration

**Est. effort: 4-5 weeks solo**

### v2 — Publishable SDK

- [ ] Stable public API surface, `@Experimental` gating for unstable parts
- [ ] Maven Central publishing (signing, POM, Sonatype)
- [ ] Configurable redaction rules (not just Authorization header)
- [ ] `process_dump.py` — PII/JWT scrubber, configurable field rules
- [ ] Instrumented tests (interceptor, exporter, redactor)
- [ ] README + integration guide

**Est. effort: +2 weeks**

### v3 — Multi-platform

- [ ] iOS: ReplayKit screen recording, URLSession/Alamofire interceptor (Swift)
- [ ] Flutter: Dart plugin + platform channels to Android/iOS native
- [ ] React Native module

**Est. effort: +8-10 weeks per platform**

---

## Key Constraints

- **Android 10+**: foreground service must be started *before* requesting MediaProjection permission, not after. Start service first (shows notification), then trigger permission dialog, pass token immediately in `onActivityResult`.
- **Android 14**: stricter `mediaProjection` foreground service type enforcement. `SecurityException` if service not already in foreground when `getMediaProjection()` is called.
- **No system logcat**: `READ_LOGS` is a privileged permission, not grantable to regular apps on Android 8+. SDK captures app logs only via Timber tree or custom logger hook.
- **No cert pinning bypass needed**: OkHttp interceptor is in-process — captures plaintext before the TLS layer. Works with cert-pinned apps without Frida or debug builds.
- **Raw dump contains credentials**: Authorization headers stripped on-device. All other redaction deferred to `process_dump.py`. Sharing guidance: secure channel only (no public upload).

---

## Out of Scope (v1)

- Crash/ANR auto-trigger (record session around a crash)
- Cloud upload / dashboard (dump is a local file, shared manually)
- SQLite / SharedPrefs snapshot in dump
- Replay (dump is read-only; replay is keytap desktop tool's job)
- iOS, Flutter, React Native
