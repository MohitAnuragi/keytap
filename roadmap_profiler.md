# keytap Profiler - Roadmap

On-device profiling driven from the keytap command palette, built on Perfetto. Traces and dumps are pulled to the Mac and analysed with `trace_processor` (`pip install perfetto`).

---

## Perf Trace (shipped)

Frame timeline + atrace capture for jank analysis. See `perf_trace.py` and `trace_queries.py`.

- Capture: `perfetto --detach=keytap`, stop with `perfetto --attach=keytap --stop`. Saved to `~/.keytap/traces/` with a JSON sidecar.
- Do not stop perfetto with `adb shell kill`. It fails with `Permission denied` on some devices (seen on Xiaomi), and the resulting partial file has no frametimeline data.
- Viewer: janky frames ranked worst first, with the longest main-thread and RenderThread slices per frame.

---

## Memory Profiler (v1 shipped)

Find which objects use the most memory, and find leaks. Java heap first.

v1 status: items 1-6 below are implemented (`heap_dump.py`, `heap_queries.py`, Memory Profiler in the palette). Keys in the class view: Cmd+S cycles sort, Cmd+O toggles own/all classes, typing searches all classes. Cmd+D in the snapshot list diffs against the previous snapshot of the same app. Own-code prefix is the first two segments of the package (`com.gojek`). Snapshots are capped at 10 in `~/.keytap/heaps/`. The dump pins the target by pid (`java_hprof` matched nothing with `process_cmdline` on the test device). Dump time on a 42MB heap: about 13s end to end.

### Approach

Java heap dump through Perfetto (`java_hprof` data source) on the foreground app. Same adb and perfetto flow as perf trace, same `trace_processor` SQL, same palette viewer. QA builds are debuggable, which the dump needs.

Palette flow:

1. Palette > Memory Profiler > Heap Snapshot
2. Keytap runs the dump on the foreground app and pulls the file
3. Viewer lists top classes by retained size, with instance count and shallow size
4. Enter on a class lists its largest instances
5. Enter on an instance shows the reference path to the GC root (why it is still alive)
6. A second snapshot enables a diff view: classes that grew in instance count or size

### What it holds (shipped)

Cmd+I on an instance shows what that object keeps alive: its fields with the size only it keeps alive (shared fields marked), plus everything it exclusively retains grouped by class. Enter on a field drills into the child, Esc goes back one level. Backed by a per-snapshot numpy index (`<snapshot>.graph.npz`, built once in about 6s); recursive SQL over the dominator tree was far too slow (18s for 1,193 objects), so subtrees are walked in Python.

### Detail Snapshot (shipped, POC-grade)

Full HPROF via `am dumpheap` (`hprof_dump.py`), read by our own parser (`hprof_parser.py`) and analysed in a subprocess (`hprof_analysis.py`, about 5s, cached as `<dump>.detail.json`). It keeps primitives and string contents that the Perfetto dump drops. Screens: Activities (destroyed or finished but alive), RecyclerViews (id, size, adapter, live rows and their text, cached and pooled holders), Bitmaps (Glide source URL, hosting view size, pixel ratio, OVERSIZED flag), Duplicate strings with values. Both snapshot kinds run inside the palette with a live progress screen. Known limits: URL lookup is a heuristic on Glide request objects (21 of 128 bitmaps on the test build), adapters stay obfuscated without a mapping file, resource ids show as hex, the analysis peaks around 1.6GB for a 112MB dump, and dumps contain user data so they stay local (5 kept in `~/.keytap/hprofs/`). Retained sizes and reference paths still come from the Perfetto snapshot.

### Cheaper layers

- `dumpsys meminfo <pkg>` readout: instant, no trace file. Java, native, graphics and code split, plus Activity and View counts. Good first screen before the heavy dump.
- Memory over time: RSS and heap sampled during a normal perf trace. Shows growth while the app is used.

### Search and filter

- Typing in the viewer filters rows by fully-qualified class name (`com.gojek.food`, `Bitmap`).
- Framework classes (`byte[]`, `String`, `android.*`) drown out app classes. Default view shows only the app's package prefix; one key reveals everything.
- Remember the prefix per app. Default prefix for GoHost apps to be confirmed (`com.gojek`?).

### UX features

v1:

1. Own code vs framework toggle (default: own code only)
2. Reference path to GC root on instance drill-down
3. Sort cycling: retained size, shallow size, instance count (current sort shown in footer)
4. Summary header: heap used and total, Java/native/graphics split, Activity and View counts
5. Dump feedback: warn that the app freezes for a few seconds, progress during dump and parsing
6. Snapshot labels and diff view: name a snapshot at capture time, diff shows `+N instances` and `+X KB` per class sorted by growth

After v1:

7. Leak suspects panel: destroyed `Activity`/`Fragment` instances still alive, `ViewModel`s and singletons holding a `Context`
8. Bitmaps view (shipped): dimensions and size per bitmap, flags for larger-than-screen, unusual bytes per pixel and 3+ bitmaps of the same size; Enter shows who holds it. Opens from the Bitmap row in the class view. View size is not in the dump (only Bitmap width/height are), so oversized-for-its-view cannot be flagged
9. Duplicate detection: identical large `String`s or `byte[]`s
10. Export: copy current view as text, open the raw file in ui.perfetto.dev
11. Snapshot manager: list with label, time, size and app; delete; cap on how many are kept
12. Auto-snapshot every N seconds, diff first against last

### Not in v1

- Native heap (`heapprofd`): different question (which native code allocates), needs stack symbolization. Add only if native leaks matter.
- Retained size for the whole heap: dominator-tree retained size is expensive. Compute lazily for the top N classes only.

### To verify before building

- Exact `trace_processor` table names for heap graphs and dominator trees in the installed `perfetto` package
- That `java_hprof` works on the target device (Xiaomi, Android 14) for a debuggable app
- Dump file size on a real app heap; storage under `~/.keytap/heaps/` with a retention cap

### Open decisions

- Primary use: leak detection by snapshot diff, or "what uses the most memory right now"? Diff view is the extra work.
- Scope: foreground app only (as perf trace) or a package picker. Current choice: foreground app only.
