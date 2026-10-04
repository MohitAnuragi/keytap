# Network Recorder POC

Captures HTTP/HTTPS flows during recording and serves them back during replay so the app sees identical responses.

## Architecture

```
Record:  keytap recorder → NetworkRecorder → mitmproxy (port 8787) → device (proxy set via ADB)
                                                      ↓ flows collected
                                              recording.json  (actions + network_flows)

Replay:  keytap replayer → NetworkReplayer → mitmproxy (port 8787, serve mode) → device
                                                      ↑ flows loaded from recording.json
```

## One-time device setup

```bash
# 1. Install mitmproxy on host
pip install mitmproxy

# 2. Generate CA cert (run once, creates ~/.mitmproxy/)
mitmdump --quiet &
kill %1

# 3. Push cert to device
adb push ~/.mitmproxy/mitmproxy-ca-cert.cer /sdcard/
# On device: Settings > Security > Install from storage > pick the cert

# 4. (Optional, root only) Install as system CA for apps that ignore user CAs
# Use Magisk module: MagiskTrustUserCerts
```

## Usage in code

```python
from network_recorder import NetworkRecorder
from network_replayer import NetworkReplayer
from recording import Recording

# --- RECORD ---
rec = NetworkRecorder(scope_host="api.myapp.com")  # None = capture all
rec.start()

# ... user taps, action_recorder records actions ...

flows = rec.stop()
recording = Recording(name="my flow", ..., actions=[...], network_flows=flows)
path = recording.save()

# --- REPLAY ---
recording = Recording.load(path)
rep = NetworkReplayer()
rep.start(recording.network_flows)

# ... replayer executes actions ...

rep.stop()
```

## Match strategy

During replay, incoming requests are matched by `(METHOD, /path)` (query params stripped).
Multiple recorded responses for the same path are served round-robin in recorded order.
Unmatched requests pass through to the real network (safe default).

## Known limitations

| Limitation | Notes |
|---|---|
| Cert pinning | Most production apps reject the mitmproxy CA. Bypass: Frida script, debug build, or Magisk system CA module. |
| HTTP/2 | mitmproxy handles H2 but some apps force H2 with custom TLS. Usually works. |
| Dynamic tokens | Auth tokens in request headers change per session. Match is by path only so this is fine. Tokens in the URL path (e.g. `/users/{id}`) will miss — needs regex path matching (future). |
| Background traffic | scope_host filters noise but analytics/FCM still slip through if scope is None. |
| Response body size | Large binary responses (images, video) inflate the recording JSON. Consider excluding by content-type (future). |

## Future improvements

- Regex/glob path matching for dynamic path segments
- Content-type filter to skip binary responses
- HAR export/import for interop with Charles, Postman
- Integrate NetworkRecorder start/stop into recorder.py `start()`/`stop()` lifecycle
- UI toggle in keytap to enable network capture per session
