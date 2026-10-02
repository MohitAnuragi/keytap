"""
Palette subprocess: native macOS tkinter window.
Reads one JSON line from stdin, writes one JSON line to stdout on close.

Input JSON keys:
  mode      : "palette" | "viewer"
  packages  : [str, ...]          (palette mode)
  clipboard : str                 (palette mode)
  deeplinks : [str, ...]          (palette mode)
  serial    : str | null          (palette mode)
  title     : str                 (viewer mode)
  items     : [[key,val], ...]    (viewer mode - key/value pairs)

Output JSON:
  null                            cancelled
  {"type": "launch",        "pkg": str}
  {"type": "force-stop",    "pkg": str}
  {"type": "clear-data",    "pkg": str}
  {"type": "uninstall",     "pkg": str}
  {"type": "app-action",    "pkg": str, "action": str}
  {"type": "input-text",    "text": str}
  {"type": "view-hierarchy"}
  {"type": "memory"}
  {"type": "install-apk"}
  {"type": "dev-options"}
  {"type": "clipboard"}
  {"type": "deeplink",      "value": str}
"""
import json
import logging
import os
import re
import sys
import threading
import time
import tkinter as tk
import tkinter.filedialog as filedialog

_LOG_FILE = os.path.expanduser("~/.keytap_debug.log")
_log = logging.getLogger("keytap.palette")

# ── Feature flags ─────────────────────────────────────────────────────────────

SHOW_MEMORY = False   # disabled until memory inspector is fully implemented

# ── Colors (defaults; overridden by active theme at PaletteWindow init) ────────

C = {
    'bg':      '#1c1c1e',
    'bg2':     '#2c2c2e',
    'fg':      '#f0f0f0',
    'dim':     '#888888',
    'accent':  '#00c864',
    'sel':     '#3a3a3c',
    'sel_fg':  '#00c864',
    'border':  '#3a3a3c',
}

FONT_MONO  = ("Menlo", 13)
FONT_SMALL = ("Menlo", 11)

# ── Built-in commands ─────────────────────────────────────────────────────────

BUILT_IN = [
    ("  View Hierarchy    – browse UI tree",      "view-hierarchy"),
    *([("  Memory Watchdog   – live RAM stats",   "memory")] if SHOW_MEMORY else []),
    ("  Input Text        – send to device",      "input-text"),
    ("  Dev Options       – toggle ADB settings", "dev-options"),
    ("  Install APK       – install from file",   "install-apk"),
    ("  Theme             – switch color theme",  "theme-switcher"),
    ("  Capture Settings  – stream quality",        "capture-settings"),
    ("  Action Recorder   – record & replay flows", "action-recorder"),
    ("  Perf Trace        – record & analyze jank",  "perf-trace"),
]

_BITRATE_PRESETS = [
    ("2 Mbps  – lowest lag",       "2000000"),
    ("4 Mbps  – low quality",      "4000000"),
    ("8 Mbps  – medium quality",   "8000000"),
    ("16 Mbps – good quality",     "16000000"),
    ("20 Mbps – default",          "20000000"),
    ("40 Mbps – maximum quality",  "40000000"),
]

_FPS_PRESETS = [
    ("Off  – native device fps",        0),
    ("15 fps  – low bandwidth",         15),
    ("24 fps  – film-like",             24),
    ("30 fps  – balanced (recommended)", 30),
]

_ENCODE_SCALE_PRESETS = [
    ("100%  – native (default)",         1.0),
    ("75%   – smaller I-frames",         0.75),
    ("50%   – half res, ~4x less stall", 0.5),
    ("25%   – thumbnail quality",        0.25),
]

APP_ACTIONS = [
    ("  Launch",        "launch"),
    ("  Force Stop",    "force-stop"),
    ("  Clear Data",    "clear-data"),
    ("  Uninstall",     "uninstall"),
    ("  Shared Prefs",  "shared-prefs"),
    ("  Remote Config", "remote-config"),
    ("  Litmus",        "litmus"),
    ("  Permissions",   "permissions"),
]

_DEEPLINK_RE = re.compile(r'^[a-zA-Z][a-zA-Z0-9+\-.]*://.+')


# ── Palette window ────────────────────────────────────────────────────────────

class PaletteWindow:
    W, H = 540, 480

    def __init__(self, root, data):
        self.root    = root
        self.data    = data
        self.result  = None
        self.state   = "search"    # search | actions | input | viewer | hierarchy
        self.sel_pkg = None

        self._adb_path = data.get("adb_path", "adb")
        self._serial   = data.get("serial") or None

        # Apply active theme to C before building UI
        try:
            import theme_manager
            t = theme_manager.get_active_theme()
            C.update({
                'bg':     t['bg'],
                'bg2':    t['bg_input'],
                'fg':     t['fg'],
                'dim':    t['fg_dim'],
                'accent': t['accent'],
                'sel':    t['sel_bg'],
                'sel_fg': t['accent'],
                'border': t['grid_line'],
            })
        except Exception:
            pass

        # viewer state
        self._viewer_all          = []   # all formatted display strings (unfiltered)
        self._viewer_raw_items    = []   # raw (label, state) pairs, parallel to _viewer_all
        self._viewer_shown_idx    = []   # _viewer_all index for each listbox row (after filter)
        self._viewer_on_enter     = None # callable(raw_idx) or None for read-only viewers
        self._viewer_on_back      = None # callable() for Esc one level up, None = back to search

        # perf trace state
        self._trace_path   = None
        self._trace_frames = []
        self._trace_warns  = []
        self._trace_sort   = "dur"       # dur | time

        # actions state
        self._action_items       = []   # set by _enter_actions; init here avoids trace race

        # input context: distinguishes what the input state is collecting
        self._input_context = "input-text"

        # replay: holds selected recording info between viewer transitions
        self._selected_recording = None
        self._manage_recording   = None

        # hierarchy state
        self._hier_nav_stack     = []   # [(nodes, label), ...]
        self._hier_current_nodes = []
        self._hier_current_label = "root"
        self._hier_all_flat      = []
        self._hier_shown         = []

        # ── Packages + built-ins ──────────────────────────────────────────────
        self._pkgs = data.get("packages", [])
        self._deeplinks = data.get("deeplinks", [])
        self._clipboard = data.get("clipboard", "")

        # Build full item list: (display, type, value)
        self._all_items = []
        for label, vtype in BUILT_IN:
            self._all_items.append((label, vtype, None))
        # Clipboard
        clip = self._clipboard.strip()
        if clip:
            short = clip[:60] + ("..." if len(clip) > 60 else "")
            self._all_items.append((f"  Clipboard        – {short}", "clipboard", clip))
        # Deeplinks
        for dl in self._deeplinks:
            short = dl[:60] + ("..." if len(dl) > 60 else "")
            self._all_items.append((f"  Deeplink         – {short}", "deeplink", dl))
        # Packages
        for pkg in self._pkgs:
            self._all_items.append((f"  {pkg}", "pkg", pkg))

        self._shown = list(self._all_items)

        # Pin [STOP RECORDING] at top while a recording is active.
        if data.get("recording_active"):
            self._all_items.insert(0, ("  [STOP RECORDING]", "stop-recording", None))
            self._shown = list(self._all_items)
        if data.get("trace_active"):
            self._all_items.insert(0, ("  [STOP TRACE]", "stop-trace", None))
            self._shown = list(self._all_items)

        # ── Build UI ──────────────────────────────────────────────────────────
        root.title("keytap")
        root.geometry(f"{self.W}x{self.H}")
        root.configure(bg=C['bg'])
        root.resizable(True, True)
        root.attributes("-topmost", True)

        # Search bar frame
        bar_frame = tk.Frame(root, bg=C['bg2'], pady=0)
        bar_frame.pack(fill="x", padx=0, pady=0)

        self._prompt = tk.Label(bar_frame, text=">", bg=C['bg2'], fg=C['accent'],
                                font=FONT_MONO, padx=8, pady=10)
        self._prompt.pack(side="left")

        self._var = tk.StringVar()
        self._entry = tk.Entry(bar_frame, textvariable=self._var,
                               bg=C['bg2'], fg=C['fg'],
                               insertbackground=C['fg'],
                               font=FONT_MONO, relief="flat", bd=0)
        self._entry.pack(side="left", fill="x", expand=True, pady=10, padx=(0, 8))
        self._entry.focus_set()

        # Separator
        tk.Frame(root, bg=C['border'], height=1).pack(fill="x")

        # List
        list_frame = tk.Frame(root, bg=C['bg'])
        list_frame.pack(fill="both", expand=True, padx=0, pady=0)

        scrollbar = tk.Scrollbar(list_frame, orient="vertical", bg=C['bg'],
                                 troughcolor=C['bg'], width=6)
        self._lb = tk.Listbox(list_frame, bg=C['bg'], fg=C['fg'],
                              selectbackground=C['sel'], selectforeground=C['sel_fg'],
                              font=FONT_MONO, relief="flat", bd=0,
                              activestyle="none", highlightthickness=0,
                              yscrollcommand=scrollbar.set)
        scrollbar.config(command=self._lb.yview)
        scrollbar.pack(side="right", fill="y")
        self._lb.pack(fill="both", expand=True)

        # Footer
        tk.Frame(root, bg=C['border'], height=1).pack(fill="x")
        self._footer = tk.Label(root, text="↑↓=move  Enter=select  Esc=close",
                                bg=C['bg2'], fg=C['dim'],
                                font=FONT_SMALL, anchor="w", padx=12, pady=6)
        self._footer.pack(fill="x")

        # Populate list
        self._refresh_search()

        # ── Bindings ──────────────────────────────────────────────────────────
        self._var.trace_add("write", self._on_type)
        # Bind Up/Down on entry to stop event reaching root (macOS fires both)
        self._entry.bind("<Up>",   self._on_up)
        self._entry.bind("<Down>", self._on_down)
        self._lb.bind("<<ListboxSelect>>", self._on_lb_select)
        root.bind("<Return>",        self._on_enter)
        root.bind("<Escape>",        self._on_escape)
        root.bind("<Command-r>",     self._on_refresh)
        root.bind("<Command-s>",     self._on_sort)
        root.protocol("WM_DELETE_WINDOW", self._on_close)

        # Select first item
        if self._lb.size() > 0:
            self._lb.selection_set(0)
            self._lb.activate(0)

    # ── List management ───────────────────────────────────────────────────────

    def _set_items(self, items):
        self._lb.delete(0, "end")
        for label, *_ in items:
            self._lb.insert("end", label)
        if items:
            self._lb.selection_set(0)
            self._lb.activate(0)

    def _selected_idx(self):
        sel = self._lb.curselection()
        return sel[0] if sel else None

    def _move(self, delta):
        idx = self._selected_idx()
        if idx is None:
            idx = 0
        else:
            idx = max(0, min(self._lb.size() - 1, idx + delta))
        self._lb.selection_clear(0, "end")
        self._lb.selection_set(idx)
        self._lb.activate(idx)
        self._lb.see(idx)
        if self.state == "hierarchy":
            self._send_hover_at(idx)

    # ── Search state ──────────────────────────────────────────────────────────

    def _on_type(self, *_):
        if self.state == "search":
            self._refresh_search()
        elif self.state == "actions":
            self._refresh_actions()
        elif self.state == "input":
            pass  # raw text, no filtering
        elif self.state == "viewer":
            self._refresh_viewer()
        elif self.state == "hierarchy":
            self._filter_hierarchy(self._var.get())

    def _refresh_search(self):
        q = self._var.get().lower().strip()
        # Check if it looks like a deeplink being typed
        if _DEEPLINK_RE.match(self._var.get()):
            self._shown = [("  Send deeplink: " + self._var.get(), "deeplink-new", self._var.get())]
        elif q:
            self._shown = [(l, t, v) for (l, t, v) in self._all_items if q in l.lower()]
        else:
            self._shown = list(self._all_items)
        self._set_items(self._shown)
        self._footer.config(
            text=f"{len(self._shown)} results  |  ↑↓=move  Enter=select  Esc=close"
        )

    def _refresh_actions(self):
        q = self._var.get().lower().strip()
        if q:
            self._shown = [(l, t, v) for (l, t, v) in self._action_items if q in l.lower()]
        else:
            self._shown = list(self._action_items)
        self._set_items(self._shown)

    # ── Events ────────────────────────────────────────────────────────────────

    def _on_enter(self, _=None):
        idx = self._selected_idx()
        if idx is None:
            return
        if self.state == "search":
            label, vtype, vvalue = self._shown[idx]
            if vtype == "pkg":
                self._enter_actions(vvalue)
            elif vtype == "input-text":
                self._enter_input()
            elif vtype == "clipboard":
                self._emit({"type": "clipboard", "value": vvalue})
            elif vtype == "deeplink":
                self._emit({"type": "deeplink", "value": vvalue})
            elif vtype == "deeplink-new":
                self._emit({"type": "deeplink", "value": vvalue})
            elif vtype == "view-hierarchy":
                self._enter_hierarchy_loading()
            elif vtype == "dev-options":
                self._enter_viewer("dev-options")
            elif vtype == "install-apk":
                self._pick_and_emit_apk()
            elif vtype == "theme-switcher":
                self._enter_themes()
            elif vtype == "capture-settings":
                self._enter_capture_settings()
            elif vtype == "action-recorder":
                self._enter_recorder()
            elif vtype == "stop-recording":
                self._enter_stop_recording_name()
            elif vtype == "perf-trace":
                self._enter_trace()
            else:
                self._emit({"type": vtype})
        elif self.state == "actions":
            label, vtype, vvalue = self._shown[idx]
            if vtype in ("launch", "force-stop", "clear-data", "uninstall"):
                self._emit({"type": "app-action", "pkg": self.sel_pkg, "action": vtype})
            elif vtype in ("shared-prefs", "remote-config", "litmus", "permissions"):
                self._enter_viewer(vtype, pkg=self.sel_pkg)
            else:
                self._emit({"type": "app-action", "pkg": self.sel_pkg, "action": vtype})
        elif self.state == "viewer":
            if self._viewer_on_enter:
                self._viewer_on_enter(idx)
        elif self.state == "capture-settings":
            label, vtype, vvalue = self._shown[idx]
            if vtype == "cap-bitrate":
                self._emit({"type": "capture-settings", "bitrate": vvalue})
            elif vtype == "cap-latency":
                cur_ll = bool(self.data.get("capture_low_latency", False))
                self._emit({"type": "capture-settings", "low_latency": not cur_ll})
            elif vtype == "cap-fps":
                self._emit({"type": "capture-settings", "fps_cap": vvalue})
            elif vtype == "cap-encode-scale":
                self._emit({"type": "capture-settings", "encode_scale": vvalue})
            elif vtype == "cap-stats":
                cur_sv = bool(self.data.get("capture_stats_visible", False))
                self._emit({"type": "capture-settings", "stats_visible": not cur_sv})
        elif self.state == "input":
            text = self._var.get().strip()
            if text:
                if self._input_context == "stop-recording":
                    self._emit({"type": "stop-recording", "name": text})
                elif self._input_context == "insert-wait":
                    try:
                        ms = int(float(text) * 1000)
                        self._emit({"type": "insert-wait", "ms": ms})
                    except ValueError:
                        pass  # invalid input, stay open
                else:
                    self._emit({"type": "input-text", "text": text})
        elif self.state == "hierarchy":
            if idx is None or idx >= len(self._hier_shown):
                return
            node = self._hier_shown[idx]
            children = node.get("children", [])
            if children:
                self._hier_nav_stack.append(
                    (self._hier_current_nodes, self._hier_current_label)
                )
                rid = node.get("resource_id", "")
                text = node.get("text", "")
                cls = node.get("class_name", "")
                self._hier_current_nodes = children
                self._hier_current_label = rid or (f'"{text[:20]}"' if text else cls)
                self._var.set("")
                self._hier_show_level()
            elif node.get("cx") or node.get("cy"):
                # Fire tap without closing palette — send via stderr side-channel
                msg = {"action": "tap", "cx": node["cx"], "cy": node["cy"]}
                print(json.dumps(msg), file=sys.stderr, flush=True)

    def _on_escape(self, _=None):
        if self.state == "hierarchy":
            if self._var.get():
                self._var.set("")
                self._hier_show_level()
            elif self._hier_nav_stack:
                self._hier_current_nodes, self._hier_current_label = \
                    self._hier_nav_stack.pop()
                self._hier_show_level()
            else:
                self._send_hover(None)
                self._enter_search()
        elif self.state == "viewer" and self._viewer_on_back:
            self._viewer_on_back()
        elif self.state in ("actions", "input", "viewer", "capture-settings"):
            self._enter_search()
        else:
            self._emit(None)

    def _on_up(self, _=None):
        self._move(-1)
        return "break"

    def _on_down(self, _=None):
        self._move(1)
        return "break"

    def _on_refresh(self, _=None):
        if self.state == "hierarchy":
            self._enter_hierarchy_loading()
        return "break"

    def _on_sort(self, _=None):
        if self.state == "viewer" and self._viewer_on_enter == self._on_trace_frame:
            self._trace_sort = "time" if self._trace_sort == "dur" else "dur"
            self._show_trace_frames()
        return "break"

    def _on_close(self):
        self._emit(None)

    # ── State transitions ─────────────────────────────────────────────────────

    def _enter_search(self):
        self.state   = "search"
        self.sel_pkg = None
        self._viewer_on_back = None
        self._var.set("")
        self._prompt.config(text=">")
        self._refresh_search()
        self._footer.config(text="↑↓=move  Enter=select  Esc=close")

    def _enter_actions(self, pkg):
        self.state    = "actions"
        self.sel_pkg  = pkg
        self._var.set("")
        short = pkg if len(pkg) <= 38 else "..." + pkg[-35:]
        self._prompt.config(text=f"{short}  >")
        self._action_items = [(l, t, None) for l, t in APP_ACTIONS]
        self._shown = list(self._action_items)
        self._set_items(self._shown)
        self._footer.config(text="↑↓=move  Enter=execute  Esc=back")

    def _enter_input(self):
        self.state = "input"
        self._input_context = "input-text"
        self._var.set("")
        self._prompt.config(text="text >")
        self._lb.delete(0, "end")
        self._lb.insert("end", "  (type text and press Enter to send to device)")
        self._footer.config(text="Enter=send to device  Esc=back")

    # ── Hierarchy state ───────────────────────────────────────────────────────

    _FETCH_TIMEOUT = 20  # seconds before fetch is considered hung

    def _enter_hierarchy_loading(self):
        _log.debug("_enter_hierarchy_loading called")
        self.state = "hierarchy"
        self._hier_nav_stack = []
        self._hier_current_nodes = []
        self._hier_current_label = "root"
        self._hier_all_flat = []
        self._hier_shown = []
        self._var.set("")
        self._prompt.config(text="View Hierarchy  >")
        self._lb.delete(0, "end")
        self._lb.insert("end", "  loading...")
        self._footer.config(text="fetching UI hierarchy from device...")
        _log.debug("starting _bg_fetch_hierarchy thread")
        threading.Thread(target=self._bg_fetch_hierarchy, daemon=True).start()

    def _bg_fetch_hierarchy(self):
        import concurrent.futures
        import time as _t
        _t0 = _t.time()
        dev = _get_u2_dev(self._serial)
        _log.debug("hierarchy fetch start, u2 preconnected=%s", dev is not None)
        try:
            from inspectors.hierarchy import dump_hierarchy_tree
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                fut = ex.submit(dump_hierarchy_tree, self._serial, dev)
                root_node, flat_nodes = fut.result(timeout=self._FETCH_TIMEOUT)
            _log.debug("hierarchy fetch done in %.2fs, %d nodes", _t.time() - _t0, len(flat_nodes))
            self.root.after(0, lambda: self._show_hierarchy(root_node, flat_nodes))
        except concurrent.futures.TimeoutError:
            _log.warning("hierarchy fetch timed out after %.2fs", _t.time() - _t0)
            self.root.after(0, lambda: self._show_fetch_error("timed out — device not responding"))
        except Exception as e:
            _log.error("hierarchy fetch error after %.2fs: %s", _t.time() - _t0, e)
            self.root.after(0, lambda: self._show_fetch_error(str(e)))

    def _show_hierarchy(self, root_node, flat_nodes):
        self._hier_all_flat = flat_nodes
        children = root_node.get("children", [])
        self._hier_current_nodes = children if children else [root_node]
        self._hier_current_label = "root"
        self._var.set("")
        self._hier_show_level()

    def _hier_node_label(self, node):
        cls   = node.get("class_name", "?")
        text  = node.get("text", "")
        rid   = node.get("resource_id", "")
        n_ch  = len(node.get("children", []))
        label = f"  {cls}"
        if text:
            label += f'  "{text[:30]}"'
        if rid:
            label += f"  [{rid}]"
        if n_ch:
            label += f"  ({n_ch})"
        elif node.get("clickable"):
            label += "  ·tap"
        return label

    def _hier_show_level(self):
        self._hier_shown = list(self._hier_current_nodes)
        self._lb.delete(0, "end")
        for n in self._hier_shown:
            self._lb.insert("end", self._hier_node_label(n))
        if self._hier_shown:
            self._lb.selection_set(0)
            self._lb.activate(0)
            self._lb.see(0)
            self._send_hover_at(0)
        self._hier_update_footer()

    def _hier_update_footer(self, searching=False):
        path_parts = [lbl for _, lbl in self._hier_nav_stack] + [self._hier_current_label]
        path = " > ".join(path_parts[-4:])
        n    = len(self._hier_shown)
        scope = " (search)" if searching else ""
        self._footer.config(
            text=f"{path}  |  {n} nodes{scope}  Enter=expand/tap  Esc=up"
        )

    def _filter_hierarchy(self, query):
        ql = query.strip().lower()
        if not ql:
            self._hier_shown = list(self._hier_current_nodes)
        else:
            self._hier_shown = [
                n for n in self._hier_all_flat
                if ql in n.get("text", "").lower()
                or ql in n.get("resource_id", "").lower()
                or ql in n.get("class_name", "").lower()
                or ql in n.get("content_desc", "").lower()
            ]
        self._lb.delete(0, "end")
        for n in self._hier_shown:
            self._lb.insert("end", self._hier_node_label(n))
        if self._hier_shown:
            self._lb.selection_set(0)
            self._lb.activate(0)
            self._lb.see(0)
            self._send_hover_at(0)
        else:
            self._send_hover(None)
        self._hier_update_footer(searching=bool(ql))

    def _on_lb_select(self, _=None):
        if self.state == "hierarchy":
            idx = self._selected_idx()
            if idx is not None:
                self._send_hover_at(idx)

    def _send_hover_at(self, idx):
        if idx < len(self._hier_shown):
            self._send_hover(self._hier_shown[idx].get("bounds"))
        else:
            self._send_hover(None)

    def _send_hover(self, bounds):
        msg = {"hover": list(bounds) if bounds else None}
        print(json.dumps(msg), file=sys.stderr, flush=True)

    def _pick_and_emit_apk(self):
        path = filedialog.askopenfilename(
            title="Select APK to install",
            filetypes=[("APK files", "*.apk"), ("All files", "*.*")],
        )
        if path:
            self._emit({"type": "install-apk", "path": path})
        # else: user cancelled, stay open

    def _enter_viewer(self, fetch_type, pkg=None):
        short = pkg.split(".")[-1] if pkg else ""
        display = {
            "shared-prefs":  f"Shared Prefs – {short}",
            "remote-config": f"Remote Config – {short}",
            "litmus":        f"Litmus – {short}",
            "permissions":   f"Permissions – {short}",
            "dev-options":   "Dev Options",
        }.get(fetch_type, fetch_type)

        # Set the Enter callback now — each interactive inspector owns its action.
        # Read-only inspectors leave _viewer_on_enter = None.
        if fetch_type == "dev-options":
            self._viewer_on_enter = self._toggle_dev_option
        elif fetch_type == "permissions":
            self._viewer_on_enter = lambda idx, _pkg=pkg: self._toggle_permission(idx, _pkg)
        else:
            self._viewer_on_enter = None

        _log.debug("_enter_viewer called: %s", fetch_type)
        self.state = "viewer"
        self._viewer_raw_items = []
        self._var.set("")
        self._prompt.config(text=f"{display}  >")
        self._lb.delete(0, "end")
        self._lb.insert("end", "  Loading...")
        self._footer.config(text="Loading...  Esc=back")
        _log.debug("starting _bg_fetch_viewer thread: %s", fetch_type)
        threading.Thread(
            target=self._bg_fetch_viewer, args=(fetch_type, pkg), daemon=True
        ).start()

    def _show_fetch_error(self, msg):
        self._lb.delete(0, "end")
        self._lb.insert("end", f"  error: {msg}")
        self._footer.config(text=f"fetch failed — Esc=back")

    def _bg_fetch_viewer(self, fetch_type, pkg=None):
        import concurrent.futures
        import time as _t
        _t0 = _t.time()
        _log.debug("viewer fetch start: %s", fetch_type)
        interactive = False

        def _do_fetch():
            if fetch_type == "shared-prefs":
                from inspectors import shared_prefs as _m
                return _m.fetch(pkg), f"Shared Prefs – {pkg}", False
            elif fetch_type == "remote-config":
                from inspectors import remote_config as _m
                return _m.fetch(pkg), f"Remote Config – {pkg}", False
            elif fetch_type == "litmus":
                from inspectors import litmus as _m
                items, _ = _m.fetch(pkg)
                return items, f"Litmus – {pkg}", False
            elif fetch_type == "permissions":
                from inspectors import permissions as _m
                return _m.fetch(pkg), f"Permissions – {pkg}", True
            elif fetch_type == "dev-options":
                from inspectors import dev_options as _m
                return _m.fetch(self._serial), "Dev Options", True
            else:
                return [("?", f"unsupported: {fetch_type}")], fetch_type, False

        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                fut = ex.submit(_do_fetch)
                raw_items, label, interactive = fut.result(timeout=self._FETCH_TIMEOUT)
            _log.debug("viewer fetch done in %.2fs, %d items", _t.time() - _t0, len(raw_items))
        except concurrent.futures.TimeoutError:
            _log.warning("viewer fetch timed out after %.2fs", _t.time() - _t0)
            self.root.after(0, lambda: self._show_fetch_error("timed out — device not responding"))
            return
        except Exception as e:
            _log.error("viewer fetch error after %.2fs: %s", _t.time() - _t0, e)
            raw_items, label, interactive = [("error", str(e))], fetch_type, False
        # Format tuples as display strings
        bracket = fetch_type == "dev-options"
        items = []
        for it in raw_items:
            if isinstance(it, (list, tuple)) and len(it) == 2:
                sep = f"  [{it[1]}]" if bracket else f"  =  {it[1]}"
                items.append(f"  {it[0]}{sep}")
            else:
                items.append(f"  {it}")

        self.root.after(0, lambda ri=raw_items, lbl=label, its=items, ia=interactive:
                        self._show_viewer_items(lbl, its, ri, ia))

    def _show_viewer_items(self, title, items, raw_items=None, interactive=False):
        self._viewer_all       = list(items)
        self._viewer_raw_items = list(raw_items) if raw_items else []
        self._prompt.config(text=f"{title}  >")
        self._viewer_shown_idx = list(range(len(items)))
        self._lb.delete(0, "end")
        for line in items:
            self._lb.insert("end", line)
        hint = "Enter=toggle  " if interactive else ""
        self._footer.config(
            text=f"{len(items)} items  |  ↑↓=move  {hint}Esc=back"
        )
        if items:
            self._lb.selection_set(0)
            self._lb.activate(0)

    def _refresh_viewer(self):
        q = self._var.get().lower()
        self._viewer_shown_idx = [
            i for i, line in enumerate(self._viewer_all)
            if not q or q in line.lower()
        ]
        self._lb.delete(0, "end")
        for i in self._viewer_shown_idx:
            self._lb.insert("end", self._viewer_all[i])
        hint = "Enter=toggle  " if self._viewer_on_enter else ""
        self._footer.config(
            text=f"{len(self._viewer_shown_idx)} items  |  ↑↓=move  {hint}Esc=back"
        )

    # ── Dev options toggle ────────────────────────────────────────────────────

    def _resolve_raw_idx(self, lb_idx):
        """Map listbox position → _viewer_all / _viewer_raw_items index."""
        if lb_idx < len(self._viewer_shown_idx):
            return self._viewer_shown_idx[lb_idx]
        return lb_idx  # fallback (unfiltered)

    def _toggle_dev_option(self, lb_idx):
        raw_idx = self._resolve_raw_idx(lb_idx)
        if raw_idx >= len(self._viewer_raw_items):
            return
        label, _ = self._viewer_raw_items[raw_idx]
        self._lb.delete(lb_idx)
        self._lb.insert(lb_idx, f"  {label}  [...]")
        self._lb.selection_set(lb_idx)
        self._lb.activate(lb_idx)
        serial = self._serial
        def _do():
            try:
                from inspectors import dev_options as _m
                new_state = _m.toggle_by_label(label, serial)
            except Exception as e:
                new_state = f"err: {e}"
            self._viewer_raw_items[raw_idx] = (label, new_state)
            display = f"  {label}  [{new_state}]"
            self._viewer_all[raw_idx] = display
            self.root.after(0, lambda: self._update_lb_item(lb_idx, display))
        threading.Thread(target=_do, daemon=True).start()

    def _update_lb_item(self, lb_idx, text):
        self._lb.delete(lb_idx)
        self._lb.insert(lb_idx, text)
        self._lb.selection_set(lb_idx)
        self._lb.activate(lb_idx)

    def _toggle_permission(self, lb_idx, pkg):
        raw_idx = self._resolve_raw_idx(lb_idx)
        if raw_idx >= len(self._viewer_raw_items):
            return
        perm, _ = self._viewer_raw_items[raw_idx]
        self._lb.delete(lb_idx)
        self._lb.insert(lb_idx, f"  {perm}  =  ...")
        self._lb.selection_set(lb_idx)
        self._lb.activate(lb_idx)
        serial = self._serial
        def _do():
            try:
                from inspectors import permissions as _m
                new_state = _m.toggle(perm, pkg, serial)
            except Exception as e:
                new_state = f"err: {e}"
            self._viewer_raw_items[raw_idx] = (perm, new_state)
            display = f"  {perm}  =  {new_state}"
            self._viewer_all[raw_idx] = display
            self.root.after(0, lambda: self._update_lb_item(lb_idx, display))
        threading.Thread(target=_do, daemon=True).start()

    # ── Action Recorder ───────────────────────────────────────────────────────

    def _enter_recorder(self):
        items = [
            ("  Record          – start a new recording", "record"),
            ("  Replay          – run a saved recording", "replay"),
            ("  Manage          – delete / view recordings", "manage"),
        ]
        if self.data.get("recording_active"):
            items.insert(0, ("  [Insert Wait]   – add a timed pause to recording", "insert-wait"))
        self._viewer_raw_items = [t for _, t in items]
        self._viewer_all       = [l for l, _ in items]
        self._viewer_shown_idx = list(range(len(items)))
        self._viewer_on_enter  = self._on_recorder_action
        self.state = "viewer"
        self._var.set("")
        self._prompt.config(text="Action Recorder  >")
        self._lb.delete(0, "end")
        for label, _ in items:
            self._lb.insert("end", label)
        self._footer.config(text="Enter=select  Esc=back")
        self._lb.selection_set(0)
        self._lb.activate(0)

    def _on_recorder_action(self, lb_idx):
        raw_idx = self._resolve_raw_idx(lb_idx)
        if raw_idx >= len(self._viewer_raw_items):
            return
        action = self._viewer_raw_items[raw_idx]
        if action == "record":
            self._emit({"type": "start-recording"})
        elif action == "replay":
            self._enter_recorder_replay()
        elif action == "manage":
            self._enter_recorder_manage()
        elif action == "insert-wait":
            self._enter_recorder_insert_wait()

    def _enter_recorder_replay(self):
        try:
            from recording import Recording
            recordings = Recording.list_all()
        except Exception:
            recordings = []

        if not recordings:
            self._viewer_raw_items = []
            self._viewer_all       = ["  No recordings found"]
            self._viewer_shown_idx = [0]
            self._viewer_on_enter  = None
        else:
            self._viewer_raw_items = recordings
            self._viewer_all = [
                f"  {r['name']}  –  {r['recorded_at'][:16]}  ({r['action_count']} steps)"
                for r in recordings
            ]
            self._viewer_shown_idx = list(range(len(recordings)))
            self._viewer_on_enter  = self._on_recording_selected

        self.state = "viewer"
        self._var.set("")
        self._prompt.config(text="Replay  >")
        self._lb.delete(0, "end")
        for line in self._viewer_all:
            self._lb.insert("end", line)
        n = len(recordings)
        self._footer.config(text=f"{n} recordings  |  Enter=select  Esc=back")
        if self._viewer_all:
            self._lb.selection_set(0)
            self._lb.activate(0)

    def _on_recording_selected(self, lb_idx):
        raw_idx = self._resolve_raw_idx(lb_idx)
        if raw_idx >= len(self._viewer_raw_items):
            return
        self._selected_recording = self._viewer_raw_items[raw_idx]
        modes = [
            ("  Timed  – replay at original speed", "timed"),
            ("  Fast   – 300ms gap between actions", "fast"),
            ("  Step   – press Space to advance",    "step"),
        ]
        self._viewer_raw_items = [m for _, m in modes]
        self._viewer_all       = [l for l, _ in modes]
        self._viewer_shown_idx = list(range(len(modes)))
        self._viewer_on_enter  = self._on_mode_selected
        self._var.set("")
        self._prompt.config(text="Replay mode  >")
        self._lb.delete(0, "end")
        for label, _ in modes:
            self._lb.insert("end", label)
        self._footer.config(text="Enter=select  Esc=back")
        self._lb.selection_set(0)
        self._lb.activate(0)

    def _on_mode_selected(self, lb_idx):
        raw_idx = self._resolve_raw_idx(lb_idx)
        if raw_idx >= len(self._viewer_raw_items) or not self._selected_recording:
            return
        mode = self._viewer_raw_items[raw_idx]
        self._emit({"type": "replay", "path": self._selected_recording["path"], "mode": mode})

    def _enter_stop_recording_name(self):
        self.state = "input"
        self._input_context = "stop-recording"
        self._var.set("")
        self._prompt.config(text="recording name >")
        self._lb.delete(0, "end")
        self._lb.insert("end", "  type a name, then press Enter to save")
        self._footer.config(text="Enter=save  Esc=cancel")

    def _enter_recorder_insert_wait(self):
        self.state = "input"
        self._input_context = "insert-wait"
        self._var.set("")
        self._prompt.config(text="wait (seconds) >")
        self._lb.delete(0, "end")
        self._lb.insert("end", "  e.g. 2.5 inserts a 2500ms pause at this point")
        self._footer.config(text="Enter=insert  Esc=back")

    def _enter_recorder_manage(self):
        try:
            from recording import Recording
            recordings = Recording.list_all()
        except Exception:
            recordings = []

        if not recordings:
            self._viewer_raw_items = []
            self._viewer_all       = ["  No recordings found"]
            self._viewer_shown_idx = [0]
            self._viewer_on_enter  = None
        else:
            self._viewer_raw_items = recordings
            self._viewer_all = [
                f"  {r['name']}  –  {r['recorded_at'][:16]}  ({r['action_count']} steps)"
                for r in recordings
            ]
            self._viewer_shown_idx = list(range(len(recordings)))
            self._viewer_on_enter  = self._on_manage_recording_selected

        self.state = "viewer"
        self._var.set("")
        self._prompt.config(text="Manage Recordings  >")
        self._lb.delete(0, "end")
        for line in self._viewer_all:
            self._lb.insert("end", line)
        hint = "Enter=select  " if recordings else ""
        self._footer.config(text=f"{len(recordings)} recordings  |  {hint}Esc=back")
        if self._viewer_all:
            self._lb.selection_set(0)
            self._lb.activate(0)

    def _on_manage_recording_selected(self, lb_idx):
        raw_idx = self._resolve_raw_idx(lb_idx)
        if raw_idx >= len(self._viewer_raw_items):
            return
        self._manage_recording = self._viewer_raw_items[raw_idx]
        sub = [
            ("  View Actions  – preview all steps in this recording", "view-actions"),
            ("  Delete        – permanently remove this recording",   "delete"),
        ]
        self._viewer_raw_items = [a for _, a in sub]
        self._viewer_all       = [l for l, _ in sub]
        self._viewer_shown_idx = list(range(len(sub)))
        self._viewer_on_enter  = self._on_manage_action
        self._var.set("")
        name = self._manage_recording.get("name", "?")
        self._prompt.config(text=f"{name}  >")
        self._lb.delete(0, "end")
        for label, _ in sub:
            self._lb.insert("end", label)
        self._footer.config(text="Enter=select  Esc=back")
        self._lb.selection_set(0)
        self._lb.activate(0)

    def _on_manage_action(self, lb_idx):
        raw_idx = self._resolve_raw_idx(lb_idx)
        if raw_idx >= len(self._viewer_raw_items) or not self._manage_recording:
            return
        action = self._viewer_raw_items[raw_idx]
        rec = self._manage_recording
        if action == "delete":
            try:
                import os as _os
                _os.remove(rec["path"])
            except Exception:
                pass
            self._manage_recording = None
            self._enter_recorder_manage()
        elif action == "view-actions":
            self._show_recording_actions(rec)

    def _show_recording_actions(self, rec):
        try:
            from recording import Recording, action_label
            r = Recording.load(rec["path"])
            items = [f"  {i + 1:3}  {action_label(a)}" for i, a in enumerate(r.actions)]
        except Exception as e:
            items = [f"  error: {e}"]
        self._viewer_raw_items = []
        self._viewer_all       = items
        self._viewer_shown_idx = list(range(len(items)))
        self._viewer_on_enter  = None
        self._var.set("")
        self._prompt.config(text=f"{rec.get('name', '?')}  >")
        self._lb.delete(0, "end")
        for line in items:
            self._lb.insert("end", line)
        self._footer.config(text=f"{len(items)} actions  |  Esc=back")
        if items:
            self._lb.selection_set(0)
            self._lb.activate(0)

    # ── Theme switcher ────────────────────────────────────────────────────────

    def _enter_themes(self):
        self.state = "viewer"
        self._viewer_on_enter = self._apply_theme
        self._var.set("")
        self._prompt.config(text="Theme  >")
        try:
            import theme_manager
            themes = theme_manager.list_themes()
            active = theme_manager.active_name()
        except Exception:
            themes = []
            active = ""
        self._viewer_raw_items = themes
        self._viewer_all       = [f"  {'* ' if t == active else '  '}{t}" for t in themes]
        self._viewer_shown_idx = list(range(len(themes)))
        self._lb.delete(0, "end")
        for line in self._viewer_all:
            self._lb.insert("end", line)
        self._footer.config(text=f"{len(themes)} themes  |  Enter=apply  Esc=back")
        if themes:
            self._lb.selection_set(0)
            self._lb.activate(0)

    def _apply_theme(self, lb_idx):
        raw_idx = self._resolve_raw_idx(lb_idx)
        if raw_idx >= len(self._viewer_raw_items):
            return
        name = self._viewer_raw_items[raw_idx]
        try:
            import theme_manager
            theme_manager.set_theme(name)
        except Exception:
            pass
        self._emit({"type": "theme-changed", "theme": name})

    # ── Perf Trace ────────────────────────────────────────────────────────────

    def _set_trace_viewer(self, prompt, rows, footer, on_enter=None, on_back=None):
        """rows: [(label, raw)]. raw=None rows are informational and ignored on Enter."""
        self._viewer_raw_items = [r for _, r in rows]
        self._viewer_all       = [l for l, _ in rows]
        self._viewer_shown_idx = list(range(len(rows)))
        self._viewer_on_enter  = on_enter
        self._viewer_on_back   = on_back
        self.state = "viewer"
        self._var.set("")
        self._prompt.config(text=f"{prompt}  >")
        self._lb.delete(0, "end")
        for label, _ in rows:
            self._lb.insert("end", label)
        self._footer.config(text=footer)
        if rows:
            self._lb.selection_set(0)
            self._lb.activate(0)

    def _enter_trace(self):
        self._trace_path = None
        if self.data.get("trace_active"):
            rows = [("  [Stop trace]     – stop and save", "stop")]
        else:
            rows = [
                ("  Start (light)   – frame timeline, low overhead", "light"),
                ("  Start (full)    – adds CPU scheduling, higher overhead", "full"),
            ]
        rows.append(("  Results         – janky frames in saved traces", "results"))
        self._set_trace_viewer("Perf Trace", rows, "Enter=select  Esc=back",
                               on_enter=self._on_trace_action)

    def _on_trace_action(self, lb_idx):
        raw_idx = self._resolve_raw_idx(lb_idx)
        if raw_idx >= len(self._viewer_raw_items):
            return
        action = self._viewer_raw_items[raw_idx]
        if action in ("light", "full"):
            self._emit({"type": "start-trace", "preset": action})
        elif action == "stop":
            self._emit({"type": "stop-trace"})
        elif action == "results":
            self._enter_trace_results()

    def _enter_trace_results(self):
        try:
            from perf_trace import list_traces
            traces = list_traces()
        except Exception:
            traces = []
        if not traces:
            self._set_trace_viewer("Traces", [("  No traces found", None)],
                                   "Esc=back", on_back=self._enter_trace)
            return
        rows = []
        for t in traces:
            tags = []
            if t.get("debuggable"):
                tags.append("debuggable")
            if (t.get("sdk") or 99) < 31:
                tags.append("sdk<31")
            tag = f"  [{', '.join(tags)}]" if tags else ""
            rows.append((f"  {t.get('started_at', '')[:16]}  {t.get('preset', ''):<5} "
                         f"{t.get('duration_s', 0):>6.1f}s  {t.get('package', '') or '-'}{tag}", t))
        self._set_trace_viewer("Traces", rows,
                               f"{len(rows)} traces  |  Enter=analyze  Esc=back",
                               on_enter=self._on_trace_selected, on_back=self._enter_trace)

    def _on_trace_selected(self, lb_idx):
        raw_idx = self._resolve_raw_idx(lb_idx)
        if raw_idx >= len(self._viewer_raw_items):
            return
        meta = self._viewer_raw_items[raw_idx]
        self._trace_path = meta["path"]
        self._trace_meta = meta
        self._set_trace_viewer("Janky frames", [("  analyzing trace...  (first run downloads trace_processor)", None)],
                               "Esc=back", on_back=self._enter_trace_results)
        threading.Thread(target=self._bg_analyze, args=(meta["path"],), daemon=True).start()

    def _bg_analyze(self, path):
        err = None
        try:
            import trace_queries
            frames, warns = trace_queries.analyze(path)
        except Exception as e:
            frames, warns, err = [], [], str(e)
        self.root.after(0, lambda: self._on_trace_analyzed(path, frames, warns, err))

    def _on_trace_analyzed(self, path, frames, warns, err):
        if self._trace_path != path:
            return   # user navigated away while analyzing
        if err:
            self._set_trace_viewer("Janky frames", [(f"  error: {err[:90]}", None)],
                                   "Esc=back", on_back=self._enter_trace_results)
            return
        meta = self._trace_meta
        if meta.get("debuggable"):
            warns.append("debuggable build: timings are pessimistic, prefer a profileable build")
        if meta.get("features"):
            warns.append("captured with: " + ", ".join(meta["features"]))
        self._trace_frames = frames
        self._trace_warns  = warns
        self._show_trace_frames()

    @staticmethod
    def _fmt_rel(ns):
        s = max(0, ns) / 1e9
        return f"{int(s // 60):02d}:{s % 60:04.1f}"

    def _show_trace_frames(self):
        frames = sorted(self._trace_frames,
                        key=(lambda f: -f["dur"]) if self._trace_sort == "dur"
                        else (lambda f: f["rel_ts"]))
        rows = [(f"  ! {w}", None) for w in self._trace_warns]
        if not frames:
            rows.append(("  no janky frames found", None))
        for f in frames:
            rows.append((f"  {self._fmt_rel(f['rel_ts']):>8}  {f['dur'] / 1e6:6.1f}ms  "
                         f"{f['jank_type'][:24]:<24}  {f['process'][-28:]}", f))
        self._set_trace_viewer(
            "Janky frames", rows,
            f"{len(frames)} janky frames  |  sort: {self._trace_sort} (Cmd+S)  |  Enter=slices  Esc=back",
            on_enter=self._on_trace_frame, on_back=self._enter_trace_results)

    def _on_trace_frame(self, lb_idx):
        raw_idx = self._resolve_raw_idx(lb_idx)
        if raw_idx >= len(self._viewer_raw_items):
            return
        frame = self._viewer_raw_items[raw_idx]
        if not isinstance(frame, dict):
            return
        self._set_trace_viewer("Frame slices", [("  querying slices...", None)],
                               "Esc=back", on_back=self._show_trace_frames)
        threading.Thread(target=self._bg_slices, args=(frame,), daemon=True).start()

    def _bg_slices(self, frame):
        path = self._trace_path
        err = None
        try:
            import trace_queries
            slices = trace_queries.frame_slices(path, frame)
        except Exception as e:
            slices, err = [], str(e)
        self.root.after(0, lambda: self._on_trace_slices(path, frame, slices, err))

    def _on_trace_slices(self, path, frame, slices, err):
        if self._trace_path != path or self._viewer_on_back != self._show_trace_frames:
            return
        head = (f"  frame @ {self._fmt_rel(frame['rel_ts'])}  {frame['dur'] / 1e6:.1f}ms  "
                f"{frame['jank_type']}  {frame['process']}")
        rows = [(head, None)]
        if err:
            rows.append((f"  error: {err[:90]}", None))
        elif not slices:
            rows.append(("  no app slices in this window (try the full preset, or the app emits no trace sections)", None))
        for s in slices:
            rows.append((f"  {s['dur'] / 1e6:7.1f}ms  {s['thread'][:12]:<12} d{s['depth']}  {s['name'][:60]}", None))
        self._set_trace_viewer("Frame slices", rows,
                               "longest main-thread / RenderThread slices  |  Esc=back",
                               on_back=self._show_trace_frames)

    def _enter_capture_settings(self):
        self.state = "capture-settings"
        self._var.set("")
        self._prompt.config(text="capture  >")
        self._footer.config(text="Enter=apply  Esc=back")
        cur_br  = str(self.data.get("capture_bitrate", "20000000"))
        cur_ll  = bool(self.data.get("capture_low_latency", False))
        cur_fps = int(self.data.get("capture_fps_cap", 0))
        items = []
        for label, val in _BITRATE_PRESETS:
            dot = "●" if val == cur_br else " "
            items.append((f"  {dot}  Bitrate: {label}", "cap-bitrate", val))
        dot = "●" if cur_ll else " "
        ll_str = "ON" if cur_ll else "OFF"
        items.append((f"  {dot}  Low Latency: {ll_str}", "cap-latency", "toggle"))
        for label, val in _FPS_PRESETS:
            dot = "●" if val == cur_fps else " "
            items.append((f"  {dot}  FPS Cap: {label}", "cap-fps", val))
        cur_es = float(self.data.get("capture_encode_scale", 1.0))
        for label, val in _ENCODE_SCALE_PRESETS:
            dot = "●" if val == cur_es else " "
            items.append((f"  {dot}  Encode Size: {label}", "cap-encode-scale", val))
        cur_sv = bool(self.data.get("capture_stats_visible", False))
        dot = "●" if cur_sv else " "
        sv_str = "ON" if cur_sv else "OFF"
        items.append((f"  {dot}  Stats Overlay: {sv_str}", "cap-stats", "toggle"))
        self._shown = items
        self._set_items(items)

    # ── Emit ──────────────────────────────────────────────────────────────────

    def _emit(self, result):
        self.result = result
        if getattr(self.root, '_persistent', False):
            # Persistent mode: hide window and write result line to stdout.
            # The process stays alive for the next open.
            print(json.dumps(result), flush=True)
            self.root.withdraw()
        else:
            self.root.destroy()


# ── Viewer window ─────────────────────────────────────────────────────────────

class ViewerWindow:
    W, H = 600, 500

    def __init__(self, root, data):
        self.root   = root
        self.result = None
        title = data.get("title", "Viewer")
        items = data.get("items", [])   # [[key, val], ...] or [str, ...]

        root.title(f"keytap – {title}")
        root.geometry(f"{self.W}x{self.H}")
        root.configure(bg=C['bg'])
        root.resizable(True, True)
        root.attributes("-topmost", True)

        # Search bar
        bar = tk.Frame(root, bg=C['bg2'])
        bar.pack(fill="x")
        tk.Label(bar, text=">", bg=C['bg2'], fg=C['accent'],
                 font=FONT_MONO, padx=8, pady=8).pack(side="left")
        self._var = tk.StringVar()
        tk.Entry(bar, textvariable=self._var, bg=C['bg2'], fg=C['fg'],
                 insertbackground=C['fg'], font=FONT_MONO,
                 relief="flat", bd=0).pack(side="left", fill="x",
                                           expand=True, pady=8, padx=(0,8))
        tk.Frame(root, bg=C['border'], height=1).pack(fill="x")

        # List (key – value)
        lf = tk.Frame(root, bg=C['bg'])
        lf.pack(fill="both", expand=True)
        sb = tk.Scrollbar(lf, orient="vertical", bg=C['bg'], width=6)
        self._lb = tk.Listbox(lf, bg=C['bg'], fg=C['fg'],
                              selectbackground=C['sel'], selectforeground=C['sel_fg'],
                              font=FONT_SMALL, relief="flat", bd=0,
                              activestyle="none", highlightthickness=0,
                              yscrollcommand=sb.set)
        sb.config(command=self._lb.yview)
        sb.pack(side="right", fill="y")
        self._lb.pack(fill="both", expand=True)

        tk.Frame(root, bg=C['border'], height=1).pack(fill="x")
        tk.Label(root, text=f"{title}  |  Esc=close",
                 bg=C['bg2'], fg=C['dim'],
                 font=FONT_SMALL, anchor="w", padx=12, pady=6).pack(fill="x")

        # Normalise items
        self._all = []
        for it in items:
            if isinstance(it, (list, tuple)) and len(it) == 2:
                k, v = str(it[0]), str(it[1])
                self._all.append(f"  {k}  =  {v}")
            else:
                self._all.append(f"  {it}")

        self._refresh()
        self._var.trace_add("write", lambda *_: self._refresh())

        root.bind("<Escape>", lambda _: self._close())
        root.protocol("WM_DELETE_WINDOW", self._close)

    def _refresh(self):
        q = self._var.get().lower()
        self._lb.delete(0, "end")
        for line in self._all:
            if not q or q in line.lower():
                self._lb.insert("end", line)

    def _close(self):
        self.root.destroy()


# ── Persistent mode ───────────────────────────────────────────────────────────

_u2_dev = None  # cached uiautomator2 device
_u2_lock = threading.Lock()


def _get_u2_dev(serial):
    """Return a live u2 device, reconnecting if the cached one is stale."""
    global _u2_dev
    with _u2_lock:
        if _u2_dev is not None:
            try:
                _u2_dev.info  # lightweight health-check (HTTP ping to ATX agent)
                return _u2_dev
            except Exception:
                _log.debug("u2 device stale, reconnecting...")
                _u2_dev = None
        try:
            import uiautomator2 as u2
            _u2_dev = u2.connect(serial) if serial else u2.connect()
            _log.debug("u2 connected, serial=%r", serial)
            return _u2_dev
        except Exception as e:
            _log.warning("u2 connect failed: %s", e)
            return None


def _preconnect_u2(serial):
    _get_u2_dev(serial)  # warms the cache; result stored in _u2_dev by _get_u2_dev


def _setup_logging():
    logging.basicConfig(
        filename=_LOG_FILE,
        level=logging.DEBUG,
        format="%(asctime)s %(name)-22s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )


def _persistent_main():
    """Keep process alive. On each JSON line from stdin, re-open the palette.
    Python + tkinter are already loaded, so the window appears with no startup lag.
    Results are written line-by-line to stdout; __ready__ signals warm state via stderr."""
    _setup_logging()
    root = tk.Tk()
    root.withdraw()          # start hidden
    root._persistent = True  # signals _emit() to hide instead of destroy

    # Pre-connect u2 in background — serial passed via --serial CLI arg
    try:
        from config import ARGS as _cfg_args
        _serial = _cfg_args.serial
    except Exception:
        _serial = None
    threading.Thread(target=_preconnect_u2, args=(_serial,), daemon=True).start()

    sys.stderr.write("__ready__\n")
    sys.stderr.flush()

    palette_holder = [None]

    def _do_open(data):
        # Destroy previous palette widgets so __init__ can rebuild cleanly.
        for widget in root.winfo_children():
            widget.destroy()
        root.deiconify()
        palette_holder[0] = PaletteWindow(root, data)

    def _stdin_loop():
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                root.after(0, lambda d=data: _do_open(d))
            except Exception:
                pass

    threading.Thread(target=_stdin_loop, daemon=True).start()
    root.mainloop()


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    _setup_logging()
    if "--persistent" in sys.argv:
        _persistent_main()
        return

    raw = sys.stdin.readline()
    if not raw.strip():
        print("null")
        return

    data = json.loads(raw)
    mode = data.get("mode", "palette")

    root = tk.Tk()

    if mode == "viewer":
        w = ViewerWindow(root, data)
        root.mainloop()
        # viewer doesn't return an action
        print("null")
    else:
        w = PaletteWindow(root, data)
        root.mainloop()
        print(json.dumps(w.result))

    sys.stdout.flush()


if __name__ == "__main__":
    main()
