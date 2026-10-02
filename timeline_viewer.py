"""
timeline_viewer.py -- video-editor-style timeline for keytap recordings.

Usage:
    python timeline_viewer.py [recording_dir]

Keyboard:
    Left/Right  -- prev/next action
    Space       -- play/pause
    Drag sash   -- resize timeline height (PanedWindow sash)
"""
import json
import os
import sys
import tempfile
import time
import tkinter as tk
from tkinter import filedialog, scrolledtext
from urllib.parse import urlparse, parse_qs

try:
    import av
    import numpy as np
    _AV_OK = True
except ImportError:
    _AV_OK = False

# ── palette (px0 / Tokyo Night inspired) ─────────────────────────────────────

BG      = "#0d0d0f"    # near-black
SIDE    = "#111114"    # panels
FG      = "#e0e0e0"    # high-contrast off-white — readable on any brightness
DIM     = "#9e9e9e"    # secondary labels — lighter grey, still clearly readable
BTN     = "#1c1c20"    # button bg
SEL_BG  = "#dca561"    # highlight bg — bright amber, stands out
SEL_FG  = "#111111"    # highlight text — dark on bright bg, always readable
BORDER  = "#222228"    # dividers

FONT    = ("Menlo", 12)
FONT_SM = ("Menlo", 10)

C_ACTION   = "#e0a060"   # amber — primary accent
C_NET_REQ  = "#7e9cd8"   # blue-steel
C_NET_OK   = "#76946a"   # muted green
C_NET_ERR  = "#c34043"   # muted red
C_NET_OPEN = "#dca561"   # warm orange (no response)
C_LOG_E    = "#c34043"
C_LOG_W    = "#dca561"
C_LOG_I    = "#52524e"   # dimmed — log info is noise
C_LOG_D    = "#333330"
C_PLAYHEAD       = "#e0a060"   # amber — matches action accent
C_RULER_BG       = "#090909"
C_RULER_FG       = "#888880"   # axis tick labels
C_ACTION_BAR     = "#1e3358"   # muted navy — action step bars
C_ACTION_BAR_SEL = "#3a5a9e"   # brighter when selected
C_CRASH          = "#c34043"   # red crash marker

# ── timeline geometry ─────────────────────────────────────────────────────────

TL_LABEL_W   = 72    # left row-label column width
TL_RIGHT     = 10    # right margin
RULER_H      = 22
ACTION_Y     = RULER_H + 8
ACTION_H     = 18
NET_BASE_Y   = ACTION_Y + ACTION_H + 6
NET_LANE_H   = 14
NET_LANE_GAP = 2
LOG_ABOVE    = 8
LOG_DOT_R    = 3
MIN_BAR_W    = 4

_TMP_FRAME = os.path.join(tempfile.gettempdir(), "keytap_frame.ppm")


# ── data loading ──────────────────────────────────────────────────────────────

def load_recording(dir_path: str):
    tl_path = os.path.join(dir_path, "timeline.json")
    if not os.path.exists(tl_path):
        raise FileNotFoundError(f"No timeline.json in {dir_path}")
    with open(tl_path) as f:
        events = json.load(f).get("events", [])

    name = os.path.basename(dir_path)
    ap = os.path.join(dir_path, "actions.json")
    if os.path.exists(ap):
        with open(ap) as f:
            d = json.load(f)
        name = d.get("name", name)

    video_path = None
    mp4 = os.path.join(dir_path, "screen.mp4")
    if os.path.exists(mp4):
        video_path = mp4

    return events, name, video_path


def total_duration_ms(events, video_path=None):
    dur = 0
    if video_path and _AV_OK:
        try:
            c = av.open(video_path)
            if c.duration:
                dur = int(c.duration / 1000)
            c.close()
        except Exception:
            pass
    if not dur and events:
        dur = max(e.get("t", 0) for e in events) + 2000
    return max(dur, 5000)


# ── network flow pairing ──────────────────────────────────────────────────────

def build_net_flows(events):
    req_by_id = {}
    req_no_id = []
    res_by_id = {}
    res_no_id = []

    for e in events:
        fid = e.get("flow_id")
        if e.get("type") == "network_req":
            if fid:
                req_by_id[fid] = e
            else:
                req_no_id.append(e)
        elif e.get("type") == "network_res":
            if fid:
                res_by_id[fid] = e
            else:
                res_no_id.append(e)

    flows = []
    for fid, req in req_by_id.items():
        flows.append(_make_flow(req, res_by_id.pop(fid, None)))
    for res in res_by_id.values():
        flows.append(_make_flow(None, res))

    used = set()
    for req in req_no_id:
        matched = None
        for i, res in enumerate(res_no_id):
            if i in used:
                continue
            if (res.get("method") == req.get("method") and
                    res.get("url") == req.get("url") and
                    res.get("t", 0) >= req.get("t", 0)):
                matched = res
                used.add(i)
                break
        flows.append(_make_flow(req, matched))

    flows.sort(key=lambda f: f["t_req"])
    lane_ends = []
    for f in flows:
        t_s = f["t_req"]
        t_e = f["t_res"] if f["t_res"] is not None else t_s + 100
        placed = False
        for i, end_t in enumerate(lane_ends):
            if t_s >= end_t:
                f["lane"] = i
                lane_ends[i] = t_e
                placed = True
                break
        if not placed:
            f["lane"] = len(lane_ends)
            lane_ends.append(t_e)

    return flows


def _make_flow(req, res):
    t_req  = req.get("t", 0) if req else (res.get("t", 0) if res else 0)
    t_res  = res.get("t") if res else None
    status = res.get("status") if res else None
    try:
        color = C_NET_OK if int(status) < 400 else C_NET_ERR
    except (TypeError, ValueError):
        color = C_NET_OPEN if t_res is None else C_NET_OK
    return {
        "t_req":   t_req,
        "t_res":   t_res,
        "method":  (req or res or {}).get("method", "?"),
        "url":     (req or res or {}).get("url", "?"),
        "status":  status,
        "color":   color,
        "req_evt": req,
        "res_evt": res,
        "lane":    0,
    }


# ── video frame extraction ────────────────────────────────────────────────────

def frame_at_ms(video_path: str, t_ms: int):
    if not _AV_OK:
        return None
    container = None
    try:
        container = av.open(video_path)
        stream = container.streams.video[0]
        t_target_s = t_ms / 1000.0
        try:
            container.seek(t_ms * 1000, backward=True)
        except Exception:
            try:
                container.seek(0)
            except Exception:
                pass
        best = None
        for frame in container.decode(stream):
            best = frame
            if frame.time >= t_target_s:
                break
        if best is None:
            return None
        arr = best.to_ndarray(format="rgb24")
        H, W = arr.shape[:2]
        if H > 560:
            scale = max(1, H // 560)
            arr = arr[::scale, ::scale]
            H, W = arr.shape[:2]
        ppm = f"P6\n{W} {H}\n255\n".encode() + arr.tobytes()
        with open(_TMP_FRAME, "wb") as fh:
            fh.write(ppm)
        return tk.PhotoImage(file=_TMP_FRAME)
    except Exception:
        return None
    finally:
        if container:
            try:
                container.close()
            except Exception:
                pass


# ── formatting helpers ────────────────────────────────────────────────────────

def fmt_ms(t_ms: int) -> str:
    return f"{t_ms / 1000:.1f}s"


def fmt_ms_stamp(t_ms: int) -> str:
    mins = t_ms // 60000
    secs = (t_ms % 60000) / 1000
    return f"{mins}:{secs:06.3f}"


def fmt_url(url: str, max_len: int = 52) -> str:
    try:
        parsed = urlparse(url)
        path = parsed.path or "/"
        if parsed.query and len(parsed.query) <= 24:
            path = path + "?" + parsed.query
    except Exception:
        path = url
    if len(path) > max_len:
        head = 20
        tail = max_len - head - 1
        path = path[:head] + "\u2026" + path[-tail:]
    return path


def fmt_event(evt):
    t   = fmt_ms(evt.get("t", 0))
    typ = evt.get("type", "?")
    if typ == "action":
        at   = evt.get("action_type", "?")
        hint = evt.get("hint", "")
        return (f"{t}   {at}  {hint}", "action")
    elif typ == "log":
        lvl = evt.get("level", "?")
        tag = {"E": "log_E", "W": "log_W", "I": "log_I"}.get(lvl, "log_D")
        return (f"{t}   {lvl}/{evt.get('tag','?')}: {evt.get('msg','')}", tag)
    elif typ == "network_req":
        path = fmt_url(evt.get("url", "?"))
        return (f"{t}   {evt.get('method','?')}  {path}", "net_req")
    elif typ == "network_res":
        path = fmt_url(evt.get("url", "?"))
        dur  = evt.get("duration_ms")
        dur_s = f"  ({dur}ms)" if dur is not None else ""
        st   = evt.get("status", "?")
        tag  = "net_err" if _is_err_status(st) else "net_res"
        return (f"{t}   {st}  {evt.get('method','?')}  {path}{dur_s}", tag)
    return (f"{t}   {typ}", "log_D")


def _is_err_status(st):
    try:
        return int(st) >= 400
    except (TypeError, ValueError):
        return False


# ── timeline canvas ───────────────────────────────────────────────────────────

class _TimelineCanvas(tk.Canvas):

    def __init__(self, parent, events, net_flows, total_ms,
                 on_seek, on_select_event, **kw):
        n = max((f["lane"] for f in net_flows), default=-1) + 1 if net_flows else 1
        self._n_lanes = max(n, 1)
        super().__init__(parent, bg=SIDE, highlightthickness=0, **kw)

        self._events    = events
        self._net_flows = net_flows
        self._total_ms  = max(total_ms, 1)
        self._on_seek   = on_seek
        self._on_select = on_select_event
        self._playhead  = 0
        self._selected  = None

        self._actions = sorted(
            [e for e in events if e.get("type") == "action"],
            key=lambda e: e.get("t", 0),
        )
        self._logs = sorted(
            [e for e in events if e.get("type") == "log"],
            key=lambda e: e.get("t", 0),
        )
        self._crashes = [
            e for e in self._logs
            if e.get("level") == "E" and (
                "FATAL" in (e.get("msg") or "") or
                e.get("tag", "") in ("AndroidRuntime", "CRASH")
            )
        ]

        self.bind("<Configure>", lambda _: self._draw())
        self.bind("<Button-1>",  self._on_click)
        self.bind("<B1-Motion>", self._on_drag)

    def set_playhead(self, t_ms: int):
        self._playhead = t_ms
        self._draw()

    def _t2x(self, t_ms):
        w = self.winfo_width() or 800
        return TL_LABEL_W + (t_ms / self._total_ms) * (w - TL_LABEL_W - TL_RIGHT)

    def _x2t(self, x):
        w = self.winfo_width() or 800
        usable = max(w - TL_LABEL_W - TL_RIGHT, 1)
        ratio = (x - TL_LABEL_W) / usable
        return int(max(0, min(self._total_ms, ratio * self._total_ms)))

    def _on_click(self, e):
        hits = self.find_overlapping(e.x - 5, e.y - 5, e.x + 5, e.y + 5)
        for item in hits:
            for tag in self.gettags(item):
                if tag.startswith("net:"):
                    idx = int(tag[4:])
                    self._selected = ("net", idx)
                    self._draw()
                    if self._on_select:
                        t = self._net_flows[idx]["t_req"]
                        self._on_select(self._net_flows[idx], t)
                    return
                if tag.startswith("act:"):
                    idx = int(tag[4:])
                    self._selected = ("act", idx)
                    self._draw()
                    if self._on_select:
                        t = self._actions[idx].get("t", 0)
                        self._on_select(self._actions[idx], t)
                    return
        self._seek_to(self._x2t(e.x))

    def _on_drag(self, e):
        self._seek_to(self._x2t(e.x))

    def _seek_to(self, t_ms):
        self._playhead = t_ms
        self._draw()
        if self._on_seek:
            self._on_seek(t_ms)

    def _draw(self):
        self.delete("all")
        self._draw_ruler()
        self._draw_actions()
        self._draw_network()
        self._draw_logs()
        self._draw_row_labels()
        self._draw_playhead()

    def _draw_ruler(self):
        w = self.winfo_width() or 800
        self.create_rectangle(0, 0, w, RULER_H, fill=C_RULER_BG, outline="")
        usable    = w - TL_LABEL_W - TL_RIGHT
        px_per_ms = usable / self._total_ms if self._total_ms else 1
        for iv in [200, 500, 1000, 2000, 5000, 10000, 30000]:
            if iv * px_per_ms >= 50:
                tick_ms = iv
                break
        else:
            tick_ms = 30000
        t = 0
        while t <= self._total_ms:
            x = self._t2x(t)
            self.create_line(x, RULER_H - 5, x, RULER_H, fill=C_RULER_FG)
            label = f"{t / 1000:.0f}s" if t % 1000 == 0 else f"{t / 1000:.1f}s"
            self.create_text(x, RULER_H // 2, text=label, fill=C_RULER_FG,
                             font=FONT_SM, anchor=tk.CENTER)
            t += tick_ms
        for evt in self._crashes:
            xc = self._t2x(evt.get("t", 0))
            self.create_rectangle(xc - 1, 0, xc + 1, RULER_H, fill=C_CRASH, outline="")
            self.create_text(xc + 3, RULER_H - 2, text="CRASH", fill=C_CRASH,
                             font=("Menlo", 8, "bold"), anchor=tk.SW)

    def _draw_actions(self):
        prev_label_x = -100
        for i, evt in enumerate(self._actions):
            t = evt.get("t", 0)
            x = self._t2x(t)
            y = ACTION_Y
            is_sel = self._selected == ("act", i)
            r    = 9 if is_sel else 7
            fill = "#f0c882" if is_sel else C_ACTION
            self.create_polygon(
                [x, y, x + r, y + r, x, y + 2 * r, x - r, y + r],
                fill=fill, outline=BORDER, width=1,
                tags=(f"act:{i}",),
            )
            label = (evt.get("hint") or evt.get("action_type") or "A")[:12]
            if x - prev_label_x > 60:
                self.create_text(x, y - 4, text=label, fill=C_ACTION,
                                 font=FONT_SM, anchor=tk.S,
                                 tags=(f"act:{i}",))
                prev_label_x = x

    def _draw_network(self):
        for i, flow in enumerate(self._net_flows):
            y  = NET_BASE_Y + flow["lane"] * (NET_LANE_H + NET_LANE_GAP)
            x1 = self._t2x(flow["t_req"])
            x2 = (max(x1 + MIN_BAR_W, self._t2x(flow["t_res"]))
                  if flow["t_res"] is not None else x1 + MIN_BAR_W)
            is_sel = self._selected == ("net", i)
            self.create_rectangle(
                x1, y, x2, y + NET_LANE_H,
                fill=flow["color"],
                outline=FG if is_sel else BORDER,
                width=1,
                tags=(f"net:{i}",),
            )
            if flow["t_res"] is None:
                self.create_line(x2, y + NET_LANE_H // 2,
                                 x2 + 8, y + NET_LANE_H // 2,
                                 fill=C_NET_OPEN, arrow=tk.LAST,
                                 tags=(f"net:{i}",))
            if x2 - x1 > 32:
                self.create_text(x1 + 4, y + NET_LANE_H // 2,
                                 text=flow.get("method", "")[:4],
                                 fill=BG, font=FONT_SM, anchor=tk.W,
                                 tags=(f"net:{i}",))

    def _draw_logs(self):
        net_h = self._n_lanes * (NET_LANE_H + NET_LANE_GAP)
        log_y = NET_BASE_Y + net_h + LOG_ABOVE + LOG_DOT_R
        for evt in self._logs:
            lvl = evt.get("level", "?")
            if lvl not in ("E", "W"):
                continue
            x     = self._t2x(evt.get("t", 0))
            color = C_LOG_E if lvl == "E" else C_LOG_W
            self.create_oval(x - LOG_DOT_R, log_y - LOG_DOT_R,
                             x + LOG_DOT_R, log_y + LOG_DOT_R,
                             fill=color, outline="")

    def _draw_row_labels(self):
        lx = TL_LABEL_W - 6
        h  = self.winfo_height() or 200
        # vertical separator
        self.create_line(TL_LABEL_W, RULER_H, TL_LABEL_W, h,
                         fill=BORDER, width=1)
        self.create_text(lx, ACTION_Y + ACTION_H // 2,
                         text="ACTIONS", fill=DIM,
                         font=("Menlo", 8), anchor=tk.E)
        net_mid = NET_BASE_Y + (self._n_lanes * (NET_LANE_H + NET_LANE_GAP)) // 2
        self.create_text(lx, net_mid,
                         text="NETWORK", fill=DIM,
                         font=("Menlo", 8), anchor=tk.E)
        net_h = self._n_lanes * (NET_LANE_H + NET_LANE_GAP)
        log_y = NET_BASE_Y + net_h + LOG_ABOVE + LOG_DOT_R
        self.create_text(lx, log_y,
                         text="LOGS", fill=DIM,
                         font=("Menlo", 8), anchor=tk.E)

    def _draw_playhead(self):
        x = self._t2x(self._playhead)
        h = self.winfo_height() or 200
        self.create_line(x, 0, x, h, fill=C_PLAYHEAD, width=1)
        self.create_polygon([x - 5, 0, x + 5, 0, x, 9],
                            fill=C_PLAYHEAD, outline="")


# ── main viewer ───────────────────────────────────────────────────────────────

class TimelineViewer:

    def __init__(self, root: tk.Tk, dir_path: str):
        self.root        = root
        self._events     = []
        self._net_flows  = []
        self._total_ms   = 60000
        self._video_path = None
        self._playhead   = 0
        self._playing    = False
        self._play_wall  = 0.0
        self._play_start = 0
        self._img        = None
        self._tl_canvas  = None
        self._tl_frame   = None   # bottom pane of PanedWindow
        self._selected_event = None  # flow dict or event dict (last explicit selection)
        self._row_to_event: dict = {}  # text line_no → event dict
        self._network_raw: dict = {}   # flow_id → raw flow from network.json
        self._ev_filter      = "all"
        self._filter_tabs: dict = {}
        self.lbl_ev_window   = None
        self._net_card       = None
        self._net_card_key   = None   # tracks what's currently rendered in the card
        self._txt_window_key = None   # (t_start, t_end, filter, query) — skip rebuild when same
        self._search_query   = ""

        root.title("keytap  |  timeline")
        root.configure(bg=BG)
        root.minsize(1000, 680)
        root.bind("<Left>",   lambda _: self._prev_action())
        root.bind("<Right>",  lambda _: self._next_action())
        root.bind("<space>",  lambda _: self._toggle_play())
        root.bind("<Return>", lambda _: self._seek_to_selected())

        self._build_ui()
        self._load(dir_path)

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        # Top bar
        bar = tk.Frame(self.root, bg=SIDE, pady=6)
        bar.pack(fill=tk.X, side=tk.TOP)

        self.lbl_title = tk.Label(bar, text="", bg=SIDE, fg=FG,
                                  font=("Menlo", 13, "bold"))
        self.lbl_title.pack(side=tk.LEFT, padx=12)

        self.lbl_time = tk.Label(bar, text="0.0s / 0.0s", bg=SIDE, fg=DIM, font=FONT)
        self.lbl_time.pack(side=tk.LEFT, padx=8)

        self.btn_play = tk.Button(bar, text="▶  Play", command=self._toggle_play,
                                  bg=BTN, fg=FG, relief=tk.FLAT,
                                  padx=10, pady=4, font=FONT)
        self.btn_play.pack(side=tk.RIGHT, padx=4)

        for text, fn in [("Next Action ▶", self._next_action),
                         ("◀ Prev Action", self._prev_action)]:
            tk.Button(bar, text=text, command=fn,
                      bg=BTN, fg=FG, relief=tk.FLAT,
                      padx=10, pady=4, font=FONT).pack(side=tk.RIGHT, padx=2)

        tk.Frame(self.root, bg=BORDER, height=1).pack(fill=tk.X, side=tk.TOP)

        # Vertical PanedWindow: main content (top) | timeline (bottom)
        paned = tk.PanedWindow(
            self.root, orient=tk.VERTICAL,
            bg=BORDER,
            sashwidth=5,
            sashrelief=tk.FLAT,
            sashpad=0,
            showhandle=False,
        )
        paned.pack(fill=tk.BOTH, expand=True)

        # ── top pane: video + events ─────────────────────────────────────────
        main = tk.Frame(paned, bg=BG)
        paned.add(main, minsize=200, stretch="always")

        vid_outer = tk.Frame(main, bg=BG)
        vid_outer.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=8, pady=8)
        self.lbl_frame = tk.Label(vid_outer, bg=SIDE, text="no frame",
                                  fg=DIM, font=FONT)
        self.lbl_frame.pack(fill=tk.BOTH, expand=True)

        tk.Frame(main, bg=BORDER, width=1).pack(side=tk.LEFT, fill=tk.Y)

        ev_outer = tk.Frame(main, bg=BG, width=440)
        ev_outer.pack(side=tk.RIGHT, fill=tk.Y)
        ev_outer.pack_propagate(False)

        ev_header = tk.Frame(ev_outer, bg=SIDE, pady=6)
        ev_header.pack(fill=tk.X)
        tk.Label(ev_header, text="Events", bg=SIDE, fg=FG,
                 font=("Menlo", 11, "bold"), padx=12).pack(side=tk.LEFT)
        self.lbl_ev_window = tk.Label(ev_header, text="", bg=SIDE, fg=DIM,
                                      font=FONT_SM)
        self.lbl_ev_window.pack(side=tk.RIGHT, padx=12)
        tk.Frame(ev_outer, bg=BORDER, height=1).pack(fill=tk.X)

        # filter tabs
        tab_row = tk.Frame(ev_outer, bg=SIDE, pady=5, padx=4)
        tab_row.pack(fill=tk.X)
        for key, label in [("all", "All"), ("actions", "Actions"),
                            ("network", "Network"), ("logs", "Logs")]:
            lbl = tk.Label(tab_row, text=label, bg=SIDE, fg=DIM,
                           font=FONT_SM, padx=8, pady=2, cursor="hand2")
            lbl.pack(side=tk.LEFT, padx=2)
            lbl.bind("<Button-1>", lambda e, k=key: self._set_filter(k))
            self._filter_tabs[key] = lbl
        tk.Frame(ev_outer, bg=BORDER, height=1).pack(fill=tk.X)

        # search bar
        search_row = tk.Frame(ev_outer, bg=SIDE, padx=8, pady=5)
        search_row.pack(fill=tk.X)
        tk.Label(search_row, text="\u2315", bg=SIDE, fg=DIM,
                 font=("Menlo", 11)).pack(side=tk.LEFT)
        self._search_var = tk.StringVar()
        self._search_entry = tk.Entry(
            search_row, textvariable=self._search_var,
            bg=BTN, fg=DIM, insertbackground=FG,
            relief=tk.FLAT, font=FONT_SM, bd=0,
        )
        self._search_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 0))
        self._search_placeholder = "search endpoints, logs, actions..."
        self._search_entry.insert(0, self._search_placeholder)
        self._search_entry.bind("<FocusIn>",  self._on_search_focus_in)
        self._search_entry.bind("<FocusOut>", self._on_search_focus_out)
        self._search_var.trace_add("write", self._on_search_change)
        self._search_btn_clear = tk.Label(
            search_row, text="\u00d7", bg=SIDE, fg=DIM,
            font=("Menlo", 11), cursor="hand2",
        )
        self._search_btn_clear.bind("<Button-1>", self._on_search_clear)
        tk.Frame(ev_outer, bg=BORDER, height=1).pack(fill=tk.X)

        # inline network card — always packed, empty = zero height
        self._net_card = tk.Frame(ev_outer, bg=BTN, padx=12, pady=0)
        self._net_card.pack(fill=tk.X)

        self.txt = tk.Text(
            ev_outer, bg=SIDE, fg=FG, font=FONT_SM,
            relief=tk.FLAT, wrap=tk.CHAR,
            state=tk.DISABLED, cursor="arrow",
            padx=12, pady=10, spacing1=1, spacing3=2,
        )
        sb = tk.Scrollbar(ev_outer, command=self.txt.yview,
                          bg=BTN, troughcolor=SIDE)
        self.txt.configure(yscrollcommand=sb.set)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.txt.pack(fill=tk.BOTH, expand=True)

        for tag, fg_color, bold in [
            ("action",  C_ACTION,  True),
            ("net_req", C_NET_REQ, False),
            ("net_res", C_NET_OK,  False),
            ("net_err", C_NET_ERR, False),
            ("log_E",   C_LOG_E,   False),
            ("log_W",   C_LOG_W,   False),
            ("log_I",   C_LOG_I,   False),
            ("log_D",   C_LOG_D,   False),
            ("dim",     DIM,       False),
            ("ts",      DIM,       False),
        ]:
            fnt = ("Menlo", 10, "bold") if bold else FONT_SM
            self.txt.tag_configure(tag, foreground=fg_color, font=fnt)
        self.txt.tag_configure("cur_bg",
                               background=SEL_BG,
                               foreground=SEL_FG,
                               font=("Menlo", 10, "bold"))
        self.txt.tag_raise("cur_bg")
        self.txt.bind("<Button-1>",        self._on_txt_click)
        self.txt.bind("<Double-Button-1>", self._on_txt_double_click)

        # ── bottom pane: timeline ─────────────────────────────────────────────
        self._tl_frame = tk.Frame(paned, bg=SIDE)
        paned.add(self._tl_frame, minsize=70, height=140, stretch="never")

    def _build_timeline(self):
        for w in self._tl_frame.winfo_children():
            w.destroy()
        self._tl_canvas = None

        # legend strip
        leg = tk.Frame(self._tl_frame, bg=SIDE, pady=3)
        leg.pack(fill=tk.X, side=tk.TOP)
        acts = [e for e in self._events if e.get("type") == "action"]
        dur_s = f"{self._total_ms / 1000:.0f}s"
        tk.Label(leg, text=f"Timeline  0s - {dur_s}  ·  full recording",
                 bg=SIDE, fg=DIM, font=("Menlo", 9), padx=12).pack(side=tk.LEFT)
        for dot_col, lbl in reversed([
            (C_NET_OK,   "2\u00d7\u00d7"),
            (C_NET_ERR,  "error"),
            (C_NET_OPEN, "pending"),
            (C_LOG_W,    "warn"),
            (C_LOG_E,    "error"),
        ]):
            tk.Label(leg, text=lbl, bg=SIDE, fg=DIM,
                     font=("Menlo", 9)).pack(side=tk.RIGHT, padx=(0, 6))
            tk.Label(leg, text="\u25cf", bg=SIDE, fg=dot_col,
                     font=("Menlo", 9)).pack(side=tk.RIGHT, padx=(6, 0))

        tk.Frame(self._tl_frame, bg=BORDER, height=1).pack(fill=tk.X)

        self._tl_canvas = _TimelineCanvas(
            self._tl_frame, self._events, self._net_flows, self._total_ms,
            on_seek=self._seek,
            on_select_event=self._on_select_event,
        )
        self._tl_canvas.pack(fill=tk.BOTH, expand=True)

    # ── load ──────────────────────────────────────────────────────────────────

    def _load(self, dir_path: str):
        try:
            events, name, video_path = load_recording(dir_path)
        except Exception as e:
            self.lbl_title.configure(text=str(e))
            return

        self._events     = events
        self._video_path = video_path if (_AV_OK and video_path) else None
        self._net_flows  = build_net_flows(events)
        self._total_ms   = total_duration_ms(events, video_path)
        self._selected_event  = None
        self._txt_window_key  = None
        self._net_card_key    = None
        self._network_raw = {}
        net_path = os.path.join(dir_path, "network.json")
        if os.path.exists(net_path):
            try:
                with open(net_path) as f:
                    data = json.load(f)
                flows = data if isinstance(data, list) else data.get("flows", [])
                self._network_raw = {
                    str(fl.get("id") or fl.get("seq")): fl
                    for fl in flows
                    if fl.get("id") or fl.get("seq")
                }
            except Exception:
                pass

        self.lbl_title.configure(text=name)
        if not _AV_OK:
            self.lbl_frame.configure(text="install PyAV to enable video\n(pip install av)")

        self._build_timeline()
        self._seek(0)

    # ── seek / nav / play ─────────────────────────────────────────────────────

    def _seek(self, t_ms: int):
        self._playhead = max(0, min(self._total_ms, int(t_ms)))
        self.lbl_time.configure(
            text=f"{self._playhead / 1000:.1f}s / {self._total_ms / 1000:.1f}s"
        )
        if self._tl_canvas:
            self._tl_canvas.set_playhead(self._playhead)
        self._update_frame()
        self._update_events()

    def _prev_action(self):
        self._selected_event = None
        acts = sorted([e for e in self._events if e.get("type") == "action"],
                      key=lambda e: e.get("t", 0))
        for a in reversed(acts):
            if a.get("t", 0) < self._playhead - 50:
                self._seek(a["t"])
                return

    def _next_action(self):
        self._selected_event = None
        acts = sorted([e for e in self._events if e.get("type") == "action"],
                      key=lambda e: e.get("t", 0))
        for a in acts:
            if a.get("t", 0) > self._playhead + 50:
                self._seek(a["t"])
                return

    def _toggle_play(self):
        if self._playing:
            self._playing = False
            self.btn_play.configure(text="▶  Play")
        else:
            if self._playhead >= self._total_ms:
                self._playhead = 0
            self._playing    = True
            self._play_wall  = time.time()
            self._play_start = self._playhead
            self.btn_play.configure(text="⏸  Pause")
            self._tick()

    def _tick(self):
        if not self._playing:
            return
        elapsed = int((time.time() - self._play_wall) * 1000)
        t = self._play_start + elapsed
        if t >= self._total_ms:
            self._seek(self._total_ms)
            self._playing = False
            self.btn_play.configure(text="▶  Play")
            return
        self._seek(t)
        self.root.after(40, self._tick)

    def _on_select_event(self, item, t_ms: int):
        self._selected_event = item
        self._seek(t_ms)

    # ── frame display ─────────────────────────────────────────────────────────

    def _update_frame(self):
        if self._video_path:
            img = frame_at_ms(self._video_path, self._playhead)
            if img is None:
                self.lbl_frame.configure(image="",
                                         text=f"no frame @ {fmt_ms(self._playhead)}")
                self._img = None
            else:
                self._img = img
                self.lbl_frame.configure(image=img, text="")
            return

        best = None
        for e in self._events:
            if (e.get("type") == "action" and
                    e.get("t", 0) <= self._playhead and e.get("screenshot")):
                best = e["screenshot"]
        if best and os.path.exists(best):
            try:
                img = tk.PhotoImage(file=best)
                if img.height() > 560:
                    factor = max(1, img.height() // 560)
                    img = img.subsample(factor, factor)
                self._img = img
                self.lbl_frame.configure(image=img, text="")
                return
            except Exception:
                pass
        self.lbl_frame.configure(image="", text="no frame")
        self._img = None

    # ── events panel ─────────────────────────────────────────────────────────

    def _set_filter(self, key: str):
        self._ev_filter = key
        self._update_events()

    def _on_search_focus_in(self, _event):
        if self._search_var.get() == self._search_placeholder:
            self._search_entry.configure(fg=FG)
            self._search_var.set("")

    def _on_search_focus_out(self, _event):
        if not self._search_var.get().strip():
            self._search_entry.configure(fg=DIM)
            self._search_var.set(self._search_placeholder)

    def _on_search_change(self, *_):
        raw = self._search_var.get()
        q = "" if raw == self._search_placeholder else raw.strip()
        self._search_query = q
        # show/hide clear button
        if q:
            self._search_btn_clear.pack(side=tk.RIGHT)
        else:
            self._search_btn_clear.pack_forget()
        self._txt_window_key = None  # force full rebuild
        self._update_events()

    def _on_search_clear(self, _event=None):
        self._search_entry.configure(fg=DIM)
        self._search_var.set(self._search_placeholder)
        self._search_query = ""
        self._search_btn_clear.pack_forget()
        self._txt_window_key = None
        self._update_events()

    def _show_net_card(self, flow):
        for w in self._net_card.winfo_children():
            w.destroy()
        req    = flow.get("req_evt") or {}
        res    = flow.get("res_evt") or {}
        method = req.get("method") or flow.get("method", "?")
        url    = req.get("url")    or flow.get("url", "?")
        status = res.get("status") or flow.get("status")
        t_req  = flow.get("t_req", req.get("t", 0))
        t_res  = flow.get("t_res")

        top = tk.Frame(self._net_card, bg=BTN)
        top.pack(fill=tk.X, pady=(6, 2))
        m_col = C_NET_ERR if _is_err_status(status) else C_NET_REQ
        tk.Label(top, text=method, bg=BTN, fg=m_col,
                 font=("Menlo", 10, "bold")).pack(side=tk.LEFT)
        tk.Label(top, text=f"  {fmt_url(url, max_len=34)}", bg=BTN, fg=FG,
                 font=("Menlo", 10)).pack(side=tk.LEFT)
        tk.Button(top, text="\u00d7", bg=BTN, fg=DIM, relief=tk.FLAT,
                  font=("Menlo", 11), cursor="hand2",
                  command=self._hide_net_card).pack(side=tk.RIGHT)

        dur_text = "-"
        if t_res is not None:
            d = t_res - t_req
            dur_text = f"{d / 1000:.2f} s" if d >= 1000 else f"{d} ms"
        elif t_req is not None:
            dur_text = f"> {(self._total_ms - t_req) / 1000:.2f} s"

        meta = tk.Frame(self._net_card, bg=BTN, pady=2)
        meta.pack(fill=tk.X)
        for col, (hdr, val) in enumerate([
            ("STATUS",   str(status) if status else "none"),
            ("START",    fmt_ms_stamp(t_req) if t_req is not None else "-"),
            ("DURATION", dur_text),
            ("SIZE",     "-"),
        ]):
            cf = tk.Frame(meta, bg=BTN, padx=8)
            cf.grid(row=0, column=col, sticky="w")
            tk.Label(cf, text=hdr, bg=BTN, fg=DIM,
                     font=("Menlo", 8)).pack(anchor="w")
            vfg = C_NET_ERR if hdr == "STATUS" and _is_err_status(status) else FG
            tk.Label(cf, text=val, bg=BTN, fg=vfg,
                     font=("Menlo", 9, "bold")).pack(anchor="w")

        if t_res is None and t_req is not None:
            note = tk.Frame(self._net_card, bg="#181820", padx=8, pady=4)
            note.pack(fill=tk.X, pady=(4, 0))
            tk.Label(note, text="NO RESP", bg="#181820", fg=C_NET_OPEN,
                     font=("Menlo", 8, "bold")).pack(side=tk.LEFT)
            tk.Label(note, text="  Request sent \u2014 no response captured.",
                     bg="#181820", fg=DIM, font=("Menlo", 9),
                     wraplength=280, justify=tk.LEFT).pack(side=tk.LEFT)

        btn_row = tk.Frame(self._net_card, bg=BTN, pady=5)
        btn_row.pack(fill=tk.X)
        tk.Button(btn_row, text="Seek to start",
                  bg=BORDER, fg=FG, relief=tk.FLAT, padx=6, pady=2,
                  font=("Menlo", 9), cursor="hand2",
                  command=lambda: self._seek(t_req or 0)).pack(side=tk.LEFT, padx=(0, 4))
        fid = req.get("flow_id")
        raw = self._network_raw.get(str(fid)) if fid else None
        if raw:
            tk.Button(btn_row, text="Request / response \u2197",
                      bg=BORDER, fg=FG, relief=tk.FLAT, padx=6, pady=2,
                      font=("Menlo", 9), cursor="hand2",
                      command=lambda: self._open_flow_popup(flow, raw)).pack(side=tk.LEFT)

        tk.Frame(self._net_card, bg=BORDER, height=1).pack(fill=tk.X, pady=(6, 0))

    def _hide_net_card(self):
        self._net_card_key = None
        self._selected_event = None
        for w in self._net_card.winfo_children():
            w.destroy()

    def _update_events(self):
        selected = self._selected_event
        acts = sorted([e for e in self._events if e.get("type") == "action"],
                      key=lambda e: e.get("t", 0))
        act_step = {id(a): i + 1 for i, a in enumerate(acts)}

        t_start, t_end = 0, self._total_ms
        for i, a in enumerate(acts):
            if a.get("t", 0) <= self._playhead:
                t_start = a.get("t", 0)
                t_end   = (acts[i + 1].get("t", self._total_ms)
                           if i + 1 < len(acts) else self._total_ms)

        window = sorted(
            [e for e in self._events if t_start <= e.get("t", 0) <= t_end],
            key=lambda e: e.get("t", 0),
        )

        counts = {
            "all":     len(window),
            "actions": sum(1 for e in window if e.get("type") == "action"),
            "network": sum(1 for e in window
                          if e.get("type") in ("network_req", "network_res")),
            "logs":    sum(1 for e in window if e.get("type") == "log"),
        }

        if self.lbl_ev_window:
            self.lbl_ev_window.configure(
                text=f"window {t_start / 1000:.1f}s \u2013 {t_end / 1000:.1f}s"
            )

        label_map = {"all": "All", "actions": "Actions",
                     "network": "Network", "logs": "Logs"}
        for key, lbl in self._filter_tabs.items():
            lbl.configure(
                text=f"\u25cf {label_map[key]}  {counts[key]}",
                fg=FG if key == self._ev_filter else DIM,
                bg=BORDER if key == self._ev_filter else SIDE,
            )

        # net card — only rebuild when selection identity changes
        if selected is not None and "req_evt" in selected:
            new_key = id(selected)
            if new_key != self._net_card_key:
                self._net_card_key = new_key
                self._show_net_card(selected)
        elif (selected is not None and
              selected.get("type") in ("network_req", "network_res")):
            new_key = id(selected)
            if new_key != self._net_card_key:
                self._net_card_key = new_key
                pseudo = {
                    "req_evt": selected if selected.get("type") == "network_req" else None,
                    "res_evt": selected if selected.get("type") == "network_res" else None,
                    "method":  selected.get("method"),
                    "url":     selected.get("url"),
                    "t_req":   selected.get("t", 0),
                    "t_res":   None,
                    "status":  selected.get("status"),
                }
                self._show_net_card(pseudo)
        else:
            if self._net_card_key is not None:
                self._net_card_key = None
                self._hide_net_card()

        def matches(e):
            if self._ev_filter == "all":     return True
            t = e.get("type", "")
            if self._ev_filter == "actions": return t == "action"
            if self._ev_filter == "network": return t in ("network_req", "network_res")
            if self._ev_filter == "logs":    return t == "log"
            return True

        def matches_search(e):
            q = self._search_query.lower()
            if not q:
                return True
            typ = e.get("type", "")
            if typ in ("network_req", "network_res"):
                return (q in (e.get("url") or "").lower() or
                        q in (e.get("method") or "").lower())
            if typ == "log":
                return (q in (e.get("msg") or "").lower() or
                        q in (e.get("tag") or "").lower())
            if typ == "action":
                return (q in (e.get("hint") or "").lower() or
                        q in (e.get("action_type") or "").lower())
            return True

        filtered = [e for e in window if matches(e) and matches_search(e)]

        sel_ids = set()
        if selected is not None:
            if "req_evt" in selected:
                if selected.get("req_evt"): sel_ids.add(id(selected["req_evt"]))
                if selected.get("res_evt"): sel_ids.add(id(selected["res_evt"]))
            else:
                sel_ids.add(id(selected))

        # ── skip full rebuild when only selection/highlight changed ───────────
        window_key = (t_start, t_end, self._ev_filter, self._search_query)
        if window_key == self._txt_window_key:
            # tag-only update — no delete/reinsert, no flicker
            self.txt.configure(state=tk.NORMAL)
            self.txt.tag_remove("cur_bg", "1.0", tk.END)
            scroll_line = None
            for line_no, evt in self._row_to_event.items():
                is_sel = id(evt) in sel_ids
                near   = abs(evt.get("t", 0) - self._playhead) <= 400
                hl     = is_sel if selected is not None else near
                if hl:
                    self.txt.tag_add("cur_bg", f"{line_no}.0", f"{line_no + 1}.0")
                    if scroll_line is None or is_sel:
                        scroll_line = line_no
            self.txt.configure(state=tk.DISABLED)
            if scroll_line is not None:
                self.txt.see(f"{scroll_line}.0")
            return

        # ── full rebuild (window or filter changed) ───────────────────────────
        self._txt_window_key = window_key
        self._row_to_event = {}
        self.txt.configure(state=tk.NORMAL)
        self.txt.delete("1.0", tk.END)
        scroll_line = None
        row_line = 1

        for evt in filtered:
            t      = evt.get("t", 0)
            typ    = evt.get("type", "?")
            is_sel = id(evt) in sel_ids
            near   = abs(t - self._playhead) <= 400
            hl     = is_sel if selected is not None else near

            self._row_to_event[row_line] = evt
            if is_sel:
                scroll_line = row_line
            elif near and scroll_line is None:
                scroll_line = row_line

            def ins(text, *tags, _hl=hl):
                all_tags = tags + (("cur_bg",) if _hl else ())
                self.txt.insert(tk.END, text, all_tags)

            ins(f"  {fmt_ms_stamp(t)}  ", "ts")

            if typ == "action":
                at   = (evt.get("action_type") or "?").upper()[:5]
                hint = evt.get("hint", "")
                step = act_step.get(id(evt))
                ins(at, "action")
                ins(f"  {hint[:40]}", "dim")
                if step:
                    ins(f"   step {step}", "dim")

            elif typ in ("network_req", "network_res"):
                method = evt.get("method", "?")
                path   = fmt_url(evt.get("url", "?"), max_len=26)
                if typ == "network_req":
                    ins("\u2192 ", "dim")
                    ins(method, "net_req")
                    ins(f"  {path}", "dim")
                else:
                    st   = evt.get("status")
                    btag = "net_err" if _is_err_status(st) else "net_res"
                    dur  = evt.get("duration_ms")
                    ins("\u2190 ", "dim")
                    ins(method, btag)
                    ins(f"  {path}", "dim")
                    if st:
                        dur_s = f"  {dur}ms" if dur else ""
                        ins(f"  {st}{dur_s}", btag)

            elif typ == "log":
                lvl  = evt.get("level", "?")
                ltag = {"E": "log_E", "W": "log_W", "I": "log_I"}.get(lvl, "dim")
                tag_name = evt.get("tag", "")
                msg  = (evt.get("msg") or "")[:50]
                desc = f"{tag_name}: {msg}" if tag_name else msg
                ins(lvl, ltag)
                ins(f"  {desc}", "dim")

            self.txt.insert(tk.END, "\n")
            row_line += 1

        self.txt.configure(state=tk.DISABLED)
        if scroll_line is not None:
            self.txt.see(f"{scroll_line}.0")


    # ── right panel interaction ───────────────────────────────────────────────

    def _on_txt_click(self, _event):
        # after_idle so tkinter has resolved the click position
        self.txt.after_idle(self._handle_txt_click)

    def _handle_txt_click(self):
        try:
            idx = self.txt.index(tk.INSERT)
        except Exception:
            return
        line = int(idx.split(".")[0])
        evt = self._row_to_event.get(line)
        if evt is not None:
            self._selected_event = evt
            self._update_events()  # highlight only, no seek

    def _on_txt_double_click(self, _event):
        self.txt.after_idle(self._handle_txt_double)
        return "break"  # prevent default word-selection

    def _handle_txt_double(self):
        try:
            idx = self.txt.index(tk.INSERT)
        except Exception:
            return
        line = int(idx.split(".")[0])
        evt = self._row_to_event.get(line)
        if evt is not None:
            self._selected_event = evt
            self._on_expand()

    def _seek_to_selected(self):
        sel = self._selected_event
        if sel is None:
            return
        if "req_evt" in sel:
            t = sel.get("t_req", (sel.get("req_evt") or {}).get("t", 0))
        else:
            t = sel.get("t", 0)
        self._seek(t)

    def _on_expand(self):
        """Open network detail popup for the currently selected event."""
        sel = self._selected_event
        if sel is None:
            return
        if "req_evt" in sel:
            req = sel.get("req_evt") or {}
            res = sel.get("res_evt") or {}
            fid = req.get("flow_id") or res.get("flow_id")
            url = req.get("url") or res.get("url", "")
        elif sel.get("type") in ("network_req", "network_res"):
            fid = sel.get("flow_id")
            url = sel.get("url", "")
        else:
            return
        # primary: match by flow_id
        raw = self._network_raw.get(str(fid)) if fid else None
        # fallback: match by URL
        if raw is None and url and self._network_raw:
            for fl in self._network_raw.values():
                fl_url = (fl.get("url") or
                          (fl.get("request") or {}).get("url") or "")
                if fl_url == url:
                    raw = fl
                    break
        self._open_flow_popup(sel, raw)

    def _open_flow_popup(self, sel, raw_flow):
        popup = tk.Toplevel(self.root)
        popup.title("Network Flow")
        popup.configure(bg=BG)
        popup.geometry("760x540")
        popup.bind("<Escape>", lambda _: popup.destroy())
        popup.bind("<Return>", lambda _: popup.destroy())

        txt = scrolledtext.ScrolledText(
            popup, bg=SIDE, fg=FG, font=FONT,
            relief=tk.FLAT, padx=16, pady=12,
            wrap=tk.CHAR,
        )
        txt.pack(fill=tk.BOTH, expand=True, padx=0, pady=0)

        txt.tag_configure("hdr",    foreground=DIM,      font=("Menlo", 10))
        txt.tag_configure("method", foreground=C_ACTION, font=("Menlo", 13, "bold"))
        txt.tag_configure("ok",     foreground=C_NET_OK, font=("Menlo", 12, "bold"))
        txt.tag_configure("err",    foreground=C_NET_ERR, font=("Menlo", 12, "bold"))
        txt.tag_configure("key",    foreground=C_NET_REQ, font=FONT)
        txt.tag_configure("val",    foreground=FG,        font=FONT)
        txt.tag_configure("body",   foreground=FG,        font=("Menlo", 11))
        txt.tag_configure("dim",    foreground=DIM,       font=FONT_SM)

        RULE = "\u2500" * 62 + "\n"

        # extract summary fields
        if "req_evt" in sel:
            req_evt = sel.get("req_evt") or {}
            res_evt = sel.get("res_evt") or {}
            method  = req_evt.get("method", "?")
            url     = req_evt.get("url", "?")
            status  = sel.get("status")
            t_req   = sel.get("t_req", 0)
            t_res   = sel.get("t_res")
        else:
            method = sel.get("method", "?")
            url    = sel.get("url", "?")
            status = sel.get("status")
            t_req  = sel.get("t", 0)
            t_res  = None

        duration = f"  {t_res - t_req}ms" if t_res is not None else ""
        status_tag = "err" if _is_err_status(status) else "ok"

        txt.insert(tk.END, f"{method}  ", "method")
        txt.insert(tk.END, f"{url}\n", "val")
        txt.insert(tk.END, f"{status or '(pending)'}{duration}\n", status_tag)
        txt.insert(tk.END, f"{fmt_ms(t_req)}\n\n", "dim")
        txt.insert(tk.END, RULE, "dim")

        def _insert_headers(h):
            if isinstance(h, list):
                for item in h:
                    if isinstance(item, (list, tuple)) and len(item) == 2:
                        txt.insert(tk.END, f"  {item[0]}: ", "key")
                        txt.insert(tk.END, f"{item[1]}\n", "val")
            elif isinstance(h, dict):
                for k, v in h.items():
                    txt.insert(tk.END, f"  {k}: ", "key")
                    txt.insert(tk.END, f"{v}\n", "val")

        if raw_flow:
            req_obj = raw_flow.get("request") or {}
            res_obj = raw_flow.get("response") or {}
            req_hdrs = raw_flow.get("request_headers") or req_obj.get("headers")
            req_body = (raw_flow.get("request_body") or
                        req_obj.get("body") or req_obj.get("content"))
            res_hdrs = raw_flow.get("response_headers") or res_obj.get("headers")
            res_body = (raw_flow.get("response_body") or
                        res_obj.get("body") or res_obj.get("content"))
        else:
            # fall back to inline data embedded in the timeline events
            req_data = (sel.get("req_evt") or {}) if "req_evt" in sel else (
                sel if sel.get("type") == "network_req" else {})
            res_data = (sel.get("res_evt") or {}) if "req_evt" in sel else (
                sel if sel.get("type") == "network_res" else {})
            req_hdrs = req_data.get("headers") or req_data.get("request_headers")
            req_body = req_data.get("body") or req_data.get("request_body")
            res_hdrs = res_data.get("headers") or res_data.get("response_headers")
            res_body = res_data.get("body") or res_data.get("response_body")

        # ── REQUEST ──────────────────────────────────────────────────────────
        txt.insert(tk.END, "REQUEST\n", "hdr")
        # query params always available from URL
        try:
            parsed = urlparse(url)
            txt.insert(tk.END, f"  {parsed.scheme}://{parsed.netloc}{parsed.path}\n", "val")
            params = parse_qs(parsed.query, keep_blank_values=True)
            if params:
                txt.insert(tk.END, "\n  Query params\n", "hdr")
                for k, vs in params.items():
                    txt.insert(tk.END, f"  {k}: ", "key")
                    txt.insert(tk.END, f"{', '.join(vs)}\n", "val")
        except Exception:
            pass
        if req_hdrs:
            txt.insert(tk.END, "\n  Headers\n", "hdr")
            _insert_headers(req_hdrs)
        if req_body:
            txt.insert(tk.END, "\n" + _format_body(req_body), "body")

        # ── RESPONSE ─────────────────────────────────────────────────────────
        txt.insert(tk.END, "\n" + RULE, "dim")
        txt.insert(tk.END, "RESPONSE\n", "hdr")
        # status + duration always available from res_evt
        res_data_evt = (sel.get("res_evt") or {}) if "req_evt" in sel else (
            sel if sel.get("type") == "network_res" else {})
        res_status  = (res_data_evt.get("status") or status or
                       (raw_flow.get("response_status") if raw_flow else None))
        res_dur     = res_data_evt.get("duration_ms")
        if res_status:
            stag = "err" if _is_err_status(res_status) else "ok"
            dur_s = f"  {res_dur}ms" if res_dur else ""
            txt.insert(tk.END, f"  {res_status}{dur_s}\n", stag)
        else:
            txt.insert(tk.END, "  (no response captured)\n", "dim")
        if res_hdrs:
            txt.insert(tk.END, "\n  Headers\n", "hdr")
            _insert_headers(res_hdrs)
        if res_body:
            txt.insert(tk.END, "\n" + _format_body(res_body), "body")
        elif not res_hdrs:
            txt.insert(tk.END, "  Body not captured.\n", "dim")

        txt.configure(state=tk.DISABLED)
        popup.focus_set()


# ── helpers ───────────────────────────────────────────────────────────────────

def _format_body(body) -> str:
    if isinstance(body, (dict, list)):
        try:
            return json.dumps(body, indent=2, ensure_ascii=False) + "\n"
        except Exception:
            return str(body) + "\n"
    if isinstance(body, str):
        try:
            return json.dumps(json.loads(body), indent=2, ensure_ascii=False) + "\n"
        except Exception:
            return body + "\n"
    return str(body) + "\n"


# ── entry point ───────────────────────────────────────────────────────────────

def main():
    root = tk.Tk()
    if len(sys.argv) > 1:
        dir_path = sys.argv[1]
    else:
        from paths import RECORDINGS_DIR
        dir_path = filedialog.askdirectory(title="Select recording directory",
                                           initialdir=str(RECORDINGS_DIR))
        if not dir_path:
            root.destroy()
            return
    TimelineViewer(root, dir_path)
    root.mainloop()


if __name__ == "__main__":
    main()
