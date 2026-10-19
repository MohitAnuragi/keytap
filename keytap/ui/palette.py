"""
PaletteWidget: Spotlight-style command palette rendered as a pygame overlay.
Trigger: double-tap Shift.

Usage:
    palette = PaletteWidget(win_w, win_h)
    palette.init_fonts()          # call after pygame.display.set_mode()

    # Open
    palette.open(packages, clipboard_text, deeplink_history, serial)

    # In event loop
    result = palette.handle_event(event)   # returns action dict or None
    if result:
        handle_palette_action(result)

    # In draw loop
    if palette.is_open:
        palette.draw(screen)

    # After async data arrives
    palette.show_viewer(title, items, on_select)
    palette.show_hierarchy(root_node, flat_nodes, on_hover)
    palette.update_viewer_items(items)
"""

import collections
import re
import threading
import time

import pygame

from keytap.core import fonts
from keytap.ui import theme_manager
from keytap.inspectors.memory import MemoryFetcher, adj_label

_DEEPLINK_RE = re.compile(r'^[a-zA-Z][a-zA-Z0-9+\-.]*://.+')

ACTIONS = [
    ("  Launch",        "launch"),
    ("  Force Stop",    "force-stop"),
    ("  Clear Data",    "clear-data"),
    ("  Uninstall",     "uninstall"),
    ("  Shared Prefs",  "shared-prefs"),
    ("  Remote Config", "remote-config"),
    ("  Litmus",        "litmus"),
    ("  Permissions",   "permissions"),
]

# Panel layout constants
_PANEL_W       = 480
_PANEL_H       = 400
_PANEL_W_WIDE  = 700
_PANEL_H_MEM   = 480
_SEARCH_H      = 44
_ITEM_H        = 22
_FOOTER_H      = 36
_LIST_H        = _PANEL_H - _SEARCH_H - 1 - _FOOTER_H  # 319
_LIST_VISIBLE  = _LIST_H // _ITEM_H                      # ~14


def _hx(c):
    """'#1c1c1e' -> (28, 28, 30)"""
    c = c.lstrip('#')
    return tuple(int(c[i:i+2], 16) for i in (0, 2, 4))


class PaletteWidget:
    """Pygame command palette overlay. Not a window — renders onto main screen."""

    def __init__(self, win_w, win_h):
        self._win_w = win_w
        self._win_h = win_h
        self._open  = False
        self._font  = None
        self._font_sm = None

        # ── Shared state ─────────────────────────────────────────────────────
        self._state        = "search"
        self._query        = ""
        self._items        = []      # list of (display, type, value)
        self._selected     = 0
        self._scroll_off   = 0
        self._footer       = ""
        self._selected_pkg = None
        self._packages     = []
        self._pkg_filtered = []
        self._clipboard    = ""
        self._deeplinks    = []
        self._serial       = None

        # ── Viewer state ──────────────────────────────────────────────────────
        self._viewer_items    = []
        self._viewer_shown    = []
        self._viewer_title    = ""
        self._viewer_on_select = None

        # ── Hierarchy state ───────────────────────────────────────────────────
        self._hier_nav_stack     = []
        self._hier_current_nodes = []
        self._hier_current_label = "root"
        self._hier_all_flat      = []
        self._hier_shown         = []
        self._hier_on_hover      = None

        # ── Memory state ─────────────────────────────────────────────────────
        self._mem_stop       = threading.Event()
        self._mem_fetcher    = None
        self._mem_data       = {}
        self._mem_history    = collections.deque(maxlen=60)
        self._java_history   = collections.deque(maxlen=60)

        # Colors (populated on open via theme_manager)
        self._c = {}

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    @property
    def is_open(self):
        return self._open

    def init_fonts(self):
        """Call after pygame.display.set_mode()."""
        self._font    = fonts.load(13)
        self._font_sm = fonts.load(11)

    def open(self, packages, clipboard_text="", deeplink_history=None, serial=None):
        self._packages   = packages
        self._clipboard  = clipboard_text
        self._deeplinks  = deeplink_history or []
        self._serial     = serial
        self._state      = "search"
        self._query      = ""
        self._selected   = 0
        self._scroll_off = 0
        self._load_colors()
        self._filter("")
        self._open = True

    def close(self):
        self._open = False
        self._state = "search"
        self._mem_stop.set()
        if self._hier_on_hover:
            self._hier_on_hover(None)
        self._hier_on_hover = None

    # ── External data injectors ───────────────────────────────────────────────

    def show_viewer(self, title, items, on_select=None):
        self._viewer_items    = items
        self._viewer_title    = title
        self._viewer_on_select = on_select
        self._state           = "viewer"
        self._query           = ""
        self._filter_viewer("")

    def update_viewer_items(self, items):
        saved = self._selected
        self._viewer_items = items
        self._filter_viewer(self._query)
        n = len(self._items)
        self._selected = min(saved, max(0, n - 1))

    def show_hierarchy(self, root_node, flat_nodes, on_hover=None):
        self._hier_nav_stack     = []
        self._hier_all_flat      = flat_nodes
        self._hier_current_nodes = root_node.get('children') or [root_node]
        self._hier_current_label = "root"
        self._hier_on_hover      = on_hover
        self._state = "hierarchy"
        self._query = ""
        self._hier_show_level()

    # ── Event handler ─────────────────────────────────────────────────────────

    def handle_event(self, event):
        """
        Feed a pygame.Event. Returns action dict or None.
        Action dicts: {'type': ..., ...}
        """
        if event.type != pygame.KEYDOWN:
            return None

        key  = event.key
        mods = pygame.key.get_mods()
        uni  = event.unicode

        if self._state == "search":
            return self._ev_search(key, uni)
        elif self._state == "actions":
            return self._ev_actions(key)
        elif self._state == "viewer":
            return self._ev_viewer(key, uni)
        elif self._state == "input":
            return self._ev_input(key, uni)
        elif self._state == "hierarchy":
            return self._ev_hierarchy(key, uni)
        elif self._state == "memory":
            return self._ev_memory(key, mods)
        return None

    # ── State-specific event handlers ─────────────────────────────────────────

    def _ev_search(self, key, uni):
        if key == pygame.K_ESCAPE:
            self.close()
            return {'type': 'close'}
        if key == pygame.K_UP:
            self._move(-1); return None
        if key == pygame.K_DOWN:
            self._move(1);  return None
        if key == pygame.K_RETURN:
            return self._select_search()
        if key == pygame.K_BACKSPACE:
            self._query = self._query[:-1]
            self._filter(self._query)
            return None
        if uni and uni.isprintable():
            self._query += uni
            self._filter(self._query)
        return None

    def _ev_actions(self, key):
        if key == pygame.K_ESCAPE:
            self._back_to_search(); return None
        if key == pygame.K_UP:
            self._move(-1); return None
        if key == pygame.K_DOWN:
            self._move(1);  return None
        if key == pygame.K_RETURN:
            return self._execute_action()
        return None

    def _ev_viewer(self, key, uni):
        if key == pygame.K_ESCAPE:
            if self._selected_pkg:
                self._show_actions(self._selected_pkg)
            else:
                self._back_to_search()
            return None
        if key == pygame.K_UP:
            self._move(-1); return None
        if key == pygame.K_DOWN:
            self._move(1);  return None
        if key == pygame.K_RETURN:
            if self._viewer_on_select and self._selected < len(self._viewer_shown):
                k, v = self._viewer_shown[self._selected]
                self._viewer_on_select(k, v)
            return None
        if key == pygame.K_BACKSPACE:
            self._query = self._query[:-1]
            self._filter_viewer(self._query)
            return None
        if uni and uni.isprintable():
            self._query += uni
            self._filter_viewer(self._query)
        return None

    def _ev_input(self, key, uni):
        if key == pygame.K_ESCAPE:
            self._back_to_search(); return None
        if key == pygame.K_RETURN:
            text = self._query.strip()
            if text:
                self.close()
                return {'type': 'input-text', 'text': text}
            return None
        if key == pygame.K_BACKSPACE:
            self._query = self._query[:-1]; return None
        if uni and uni.isprintable():
            self._query += uni
        return None

    def _ev_hierarchy(self, key, uni):
        if key == pygame.K_ESCAPE:
            if self._query:
                self._query = ""
                self._hier_show_level()
            elif self._hier_nav_stack:
                self._hier_current_nodes, self._hier_current_label = self._hier_nav_stack.pop()
                self._hier_show_level()
            else:
                if self._hier_on_hover:
                    self._hier_on_hover(None)
                self._back_to_search()
            return None
        if key == pygame.K_UP:
            self._move(-1)
            self._hier_hover_selected()
            return None
        if key == pygame.K_DOWN:
            self._move(1)
            self._hier_hover_selected()
            return None
        if key == pygame.K_RETURN:
            return self._select_hierarchy()
        if key == pygame.K_BACKSPACE:
            self._query = self._query[:-1]
            self._filter_hierarchy(self._query)
            return None
        if uni and uni.isprintable():
            self._query += uni
            self._filter_hierarchy(self._query)
        return None

    def _ev_memory(self, key, mods):
        if key == pygame.K_ESCAPE:
            self._mem_exit(); return None
        if key == pygame.K_r and (mods & pygame.KMOD_META):
            self._mem_restart(); return None
        return None

    # ── Search / filter ───────────────────────────────────────────────────────

    def _filter(self, query):
        q  = query.strip()
        ql = q.lower()
        virtual = []

        if _DEEPLINK_RE.match(q):
            virtual.append((f"  Launch: {q}", "deeplink", q))
            pkg_list = []
        else:
            def _va(label, vtype, val=""):
                virtual.append((label, vtype, val))
            if not ql or ql in "view hierarchy":
                _va("  View Hierarchy — browse UI tree", "view-hierarchy")
            if not ql or ql in "memory watchdog":
                _va("  Memory Watchdog — live RAM stats", "memory-watchdog")
            if not ql or ql in "input text":
                _va("  Input Text — type to send to device", "input-text")
            if not ql or ql in "theme":
                _va("  Theme — switch color theme", "theme-switcher")
            if not ql or ql in "developer options":
                _va("  Developer Options — toggle ADB debug settings", "dev-options")
            if not ql or ql in "install apk":
                _va("  Install APK — install from file", "install-apk")
            if ql and ql in "wifi":
                _va("  WiFi — toggle", "quick-toggle", "wifi")
            if ql and ql in "dark mode":
                _va("  Dark Mode — toggle", "quick-toggle", "dark_mode")
            if ql and ql in "mobile data":
                _va("  Mobile Data — toggle", "quick-toggle", "mobile_data")
            if ql and ql in "clipboard":
                preview = (self._clipboard[:60].replace('\n', ' ')
                           if self._clipboard else "(empty)")
                _va(f"  Clipboard — {preview}", "clipboard", self._clipboard)
            if ql and ql in "deeplink":
                for url in self._deeplinks[:10]:
                    _va(f"  Link: {url}", "deeplink", url)
            pkg_list = (
                [p for p in self._packages if ql in p.lower()] if ql
                else list(self._packages)
            )

        self._pkg_filtered = pkg_list
        self._items = virtual + [(f"  {p}", "pkg", p) for p in pkg_list[:60]]
        total = len(self._items)
        self._footer = (f"{total} results  |  Enter=select  Esc=close"
                        if total else "no match")
        self._selected   = 0
        self._scroll_off = 0

    def _filter_viewer(self, query):
        ql = query.strip().lower()
        self._viewer_shown = [
            (k, v) for k, v in self._viewer_items
            if not ql or ql in k.lower() or ql in str(v).lower()
        ]
        self._items = [(f"  {k}  =  {v}", "viewer-item", i)
                      for i, (k, v) in enumerate(self._viewer_shown)]
        hint = "  Enter=expand" if self._viewer_on_select else ""
        n = len(self._viewer_shown)
        self._footer = (f"{self._viewer_title} — {n} keys"
                        f"  |  type to search{hint}  Esc=back")
        self._selected   = 0
        self._scroll_off = 0

    # ── Selection / navigation ────────────────────────────────────────────────

    def _move(self, delta):
        n = len(self._items)
        if not n:
            return
        self._selected = max(0, min(n - 1, self._selected + delta))
        # Keep selected visible
        if self._selected < self._scroll_off:
            self._scroll_off = self._selected
        elif self._selected >= self._scroll_off + _LIST_VISIBLE:
            self._scroll_off = self._selected - _LIST_VISIBLE + 1

    def _back_to_search(self):
        self._state        = "search"
        self._selected_pkg = None
        self._query        = ""
        self._filter("")

    def _show_actions(self, pkg):
        self._state        = "actions"
        self._selected_pkg = pkg
        self._items = [(label, "action", act) for label, act in ACTIONS]
        short = pkg if len(pkg) <= 40 else "..." + pkg[-37:]
        self._footer    = f"{short}  |  Enter=execute  Esc=back"
        self._selected  = 0
        self._scroll_off = 0

    def _select_search(self):
        if not self._items or self._selected >= len(self._items):
            return None
        display, vtype, vvalue = self._items[self._selected]

        if vtype == "pkg":
            self._show_actions(vvalue)
            return None
        if vtype == "input-text":
            self._show_input_mode()
            return None
        if vtype == "view-hierarchy":
            self._enter_loading("View Hierarchy")
            return {'type': 'view-hierarchy'}
        if vtype == "memory-watchdog":
            self._mem_enter()
            return None
        if vtype == "theme-switcher":
            self._show_theme_picker()
            return None
        if vtype == "dev-options":
            self._enter_loading("Dev Options")
            return {'type': 'dev-options'}
        if vtype == "quick-toggle":
            self.close()
            return {'type': 'quick-toggle', 'key': vvalue}
        # deeplink, clipboard, install-apk
        self.close()
        return {'type': vtype, 'value': vvalue}

    def _execute_action(self):
        if not self._items or self._selected >= len(self._items):
            return None
        _, _, action = self._items[self._selected]
        pkg = self._selected_pkg

        if action in ("shared-prefs", "remote-config", "litmus", "permissions"):
            self._enter_loading(action)
            return {'type': 'app-action', 'pkg': pkg, 'action': action}
        self.close()
        return {'type': 'app-action', 'pkg': pkg, 'action': action}

    def _show_input_mode(self):
        self._state  = "input"
        self._query  = ""
        self._items  = [("  (type text and press Enter to send to device)", "hint", "")]
        self._footer = "Type text  |  Enter=send  Esc=back"
        self._selected  = 0
        self._scroll_off = 0

    def _enter_loading(self, label):
        self._state = "viewer"
        self._items = [("  loading...", "hint", "")]
        self._footer = f"{label} — fetching from device..."
        self._selected  = 0
        self._scroll_off = 0

    def _show_theme_picker(self):
        themes  = theme_manager.list_themes()
        current = theme_manager.active_name()
        items   = [(f"* {t.title()}" if t == current else f"  {t.title()}", t)
                   for t in themes]
        def on_select(_, theme_name):
            theme_manager.set_theme(theme_name)
            self._load_colors()
            self.close()
        self.show_viewer("Theme", items, on_select=on_select)

    # ── Hierarchy ─────────────────────────────────────────────────────────────

    def _hier_show_level(self):
        ql = self._query.strip().lower()
        if ql:
            self._hier_shown = [
                n for n in self._hier_all_flat
                if ql in n['text'].lower()
                or ql in n['resource_id'].lower()
                or ql in n['class_name'].lower()
                or ql in n['content_desc'].lower()
            ]
        else:
            self._hier_shown = list(self._hier_current_nodes)

        self._items = [(self._hier_node_label(n), "hier-node", i)
                       for i, n in enumerate(self._hier_shown)]
        self._selected   = 0
        self._scroll_off = 0
        self._hier_hover_selected()
        self._update_hier_footer(searching=bool(ql))

    def _filter_hierarchy(self, query):
        self._hier_show_level()

    def _hier_node_label(self, node):
        cls  = node['class_name'] or '?'
        text = f' "{node["text"][:28]}"' if node['text'] else ''
        rid  = f' [{node["resource_id"]}]' if node['resource_id'] else ''
        n_ch = len(node['children'])
        hint = f' ({n_ch})' if n_ch else (' ·tap' if node['clickable'] else '')
        return f"  {cls}{text}{rid}{hint}"

    def _select_hierarchy(self):
        if not self._hier_shown or self._selected >= len(self._hier_shown):
            return None
        node = self._hier_shown[self._selected]
        if node['children']:
            self._hier_nav_stack.append(
                (self._hier_current_nodes, self._hier_current_label))
            label = (node['resource_id'] or
                     (f'"{node["text"][:20]}"' if node['text'] else node['class_name']))
            self._hier_current_nodes = node['children']
            self._hier_current_label = label
            self._query = ""
            self._hier_show_level()
        elif node.get('cx') or node.get('cy'):
            self.close()
            return {'type': 'tap-hierarchy', 'cx': node['cx'], 'cy': node['cy']}
        return None

    def _hier_hover_selected(self):
        if not self._hier_on_hover:
            return
        if self._hier_shown and self._selected < len(self._hier_shown):
            self._hier_on_hover(self._hier_shown[self._selected].get('bounds'))
        else:
            self._hier_on_hover(None)

    def _update_hier_footer(self, searching=False):
        path_parts = [lbl for _, lbl in self._hier_nav_stack] + [self._hier_current_label]
        path  = " > ".join(path_parts[-4:])
        n     = len(self._hier_shown)
        scope = " (all)" if searching else ""
        self._footer = f"{path}  |  {n} nodes{scope}  Enter=expand/tap  Esc=up"

    # ── Memory watchdog ───────────────────────────────────────────────────────

    def _mem_enter(self):
        self._state = "memory"
        self._items = []
        self._footer = "Cmd+R=refresh  Esc=exit"
        self._mem_data = {}
        self._mem_history.clear()
        self._java_history.clear()
        self._mem_stop.clear()
        self._mem_fetcher = MemoryFetcher(self._serial)
        threading.Thread(target=self._mem_poll, daemon=True).start()

    def _mem_exit(self):
        self._mem_stop.set()
        self._back_to_search()

    def _mem_restart(self):
        self._mem_stop.set()
        self._mem_data = {}
        self._mem_history.clear()
        self._java_history.clear()
        self._mem_stop = threading.Event()
        self._mem_fetcher = MemoryFetcher(self._serial)
        threading.Thread(target=self._mem_poll, daemon=True).start()

    def _mem_poll(self):
        u2_dev = None
        try:
            import uiautomator2 as u2
            u2_dev = u2.connect(self._serial) if self._serial else u2.connect()
        except Exception:
            pass
        while not self._mem_stop.is_set():
            data = self._mem_fetcher.fetch(u2_dev)
            self._mem_data = data
            if data.get('used_pct') is not None:
                self._mem_history.append(data['used_pct'])
            if data.get('java_heap_pct') is not None:
                self._java_history.append(data['java_heap_pct'])
            self._mem_stop.wait(2.0)

    # ── Colors ────────────────────────────────────────────────────────────────

    def _load_colors(self):
        t = theme_manager.get_active_theme()
        self._c = {k: _hx(v) for k, v in t.items() if isinstance(v, str) and v.startswith('#')}

    def _col(self, key, fallback=(200, 200, 200)):
        return self._c.get(key, fallback)

    # ── Drawing ───────────────────────────────────────────────────────────────

    def draw(self, screen, sidebar_x=0):
        if not self._open or not self._font:
            return

        sw = screen.get_width() - sidebar_x
        sh = self._win_h  # mirror height (status bar rendered separately)

        # Sidebar background + left separator line
        pygame.draw.rect(screen, self._col('bg', (28, 28, 30)),
                         (sidebar_x, 0, sw, sh))
        pygame.draw.line(screen, self._col('accent', (0, 200, 100)),
                         (sidebar_x, 0), (sidebar_x, sh - 1))

        # Panel fills sidebar with small margin
        margin = 10
        pw = sw - margin * 2
        if self._state == "memory":
            ph = min(sh - margin * 2, _PANEL_H_MEM)
        else:
            ph = sh - margin * 2

        panel = pygame.Surface((pw, ph))
        panel.fill(self._col('bg', (28, 28, 30)))

        if self._state == "memory":
            self._draw_memory(panel)
        else:
            self._draw_search_bar(panel, pw)
            pygame.draw.line(panel, self._col('grid_line', (50, 50, 50)),
                             (0, _SEARCH_H), (pw, _SEARCH_H))
            self._draw_list(panel, pw)
            self._draw_footer(panel, pw, ph)

        screen.blit(panel, (sidebar_x + margin, margin))

    def _draw_search_bar(self, surf, pw):
        bg  = self._col('bg_input', (44, 44, 46))
        fg  = self._col('fg', (240, 240, 240))
        dim = self._col('fg_dim', (136, 136, 136))
        pygame.draw.rect(surf, bg, (12, 10, pw - 24, _SEARCH_H - 10))

        # ">" prompt
        prompt = self._font.render(">", True, dim)
        surf.blit(prompt, (20, 20))

        # Query text + blinking cursor
        x = 38
        if self._state == "input":
            label = self._query
            hint  = "(type text to send to device)"
            if not label:
                hint_s = self._font.render(hint, True, dim)
                surf.blit(hint_s, (x, 19))
            else:
                t = self._font.render(label, True, fg)
                surf.blit(t, (x, 19))
        elif self._state in ("search", "viewer", "hierarchy"):
            t = self._font.render(self._query, True, fg)
            surf.blit(t, (x, 19))
        else:
            # actions mode - show package name dimmed
            pkg = self._selected_pkg or ""
            t = self._font.render(pkg[:50], True, dim)
            surf.blit(t, (x, 19))

        # Cursor blink (every 500ms)
        if time.time() % 1.0 < 0.5 and self._state in ("search", "viewer", "input", "hierarchy"):
            tw = self._font.size(self._query)[0] if self._query else 0
            cx = x + tw + 1
            pygame.draw.line(surf, fg, (cx, 17), (cx, 17 + 16))

    def _draw_list(self, surf, pw):
        bg      = self._col('bg', (28, 28, 30))
        fg      = self._col('fg', (240, 240, 240))
        fg_dim  = self._col('fg_dim', (136, 136, 136))
        accent  = self._col('accent', (0, 200, 100))
        sel_bg  = self._col('sel_bg', (58, 58, 60))

        y0 = _SEARCH_H + 1

        visible = self._items[self._scroll_off: self._scroll_off + _LIST_VISIBLE]
        for i, (display, vtype, _) in enumerate(visible):
            abs_idx = self._scroll_off + i
            item_y  = y0 + i * _ITEM_H
            is_sel  = abs_idx == self._selected

            if is_sel:
                pygame.draw.rect(surf, sel_bg, (0, item_y, pw, _ITEM_H))
            color = accent if is_sel else (fg if vtype != "hint" else fg_dim)
            t = self._font.render(display[:70], True, color)
            surf.blit(t, (8, item_y + 3))

        # Scrollbar hint
        total = len(self._items)
        if total > _LIST_VISIBLE:
            sb_h   = max(20, int(_LIST_H * _LIST_VISIBLE / total))
            sb_y   = y0 + int(_LIST_H * self._scroll_off / total)
            pygame.draw.rect(surf, fg_dim, (pw - 4, sb_y, 3, sb_h))

    def _draw_footer(self, surf, pw, ph):
        fg_dim = self._col('fg_dim', (136, 136, 136))
        fy = ph - _FOOTER_H + 8
        pygame.draw.line(surf, self._col('grid_line', (50, 50, 50)),
                         (0, ph - _FOOTER_H), (pw, ph - _FOOTER_H))
        t = self._font_sm.render(self._footer[:90], True, fg_dim)
        surf.blit(t, (12, fy))

    def _draw_memory(self, surf):
        """Memory watchdog graph. Mirrors palette.py tkinter Canvas logic."""
        fg      = self._col('fg',       (240, 240, 240))
        fg_dim  = self._col('fg_dim',   (136, 136, 136))
        accent  = self._col('accent',   (0, 200, 100))
        err     = self._col('err',      (255, 68, 68))
        warn    = self._col('warn',     (255, 140, 53))
        info    = self._col('info',     (79, 195, 247))
        bar_bg  = self._col('bar_bg',   (42, 42, 42))
        grid_c  = self._col('grid_line',(51, 51, 51))
        graph_bg = self._col('graph_bg',(17, 17, 17))

        W, H = _PANEL_W_WIDE, _PANEL_H_MEM
        d = self._mem_data
        pad = 16

        # Title
        title = "MEMORY WATCHDOG"
        if d.get('app_pkg'):
            title += f"  —  {d['app_pkg']}"
        t = self._font.render(title, True, fg)
        surf.blit(t, (W // 2 - t.get_width() // 2, 14))

        if not d:
            t = self._font_sm.render("fetching memory stats...", True, fg_dim)
            surf.blit(t, (W // 2 - t.get_width() // 2, 120))
            return
        if d.get('error'):
            t = self._font_sm.render(f"Error: {d['error']}", True, err)
            surf.blit(t, (pad, 120))
            return

        y = 52
        mid = W // 2 - 8
        rx  = mid + 16

        # ── GC Pressure (left) ────────────────────────────────────────────────
        java_pct   = d.get('java_heap_pct') or 0.0
        java_used  = d.get('java_heap_used_mb', 0)
        java_total = d.get('java_heap_total_mb', 0)
        native_mb  = d.get('native_heap_mb', 0)
        gc_col = err if java_pct > 85 else warn if java_pct > 65 else accent

        t = self._font_sm.render("GC PRESSURE", True, fg_dim)
        surf.blit(t, (pad, y))
        y += 18
        bw    = mid - pad - 8
        bfill = int(bw * java_pct / 100)
        pygame.draw.rect(surf, bar_bg, (pad, y, bw, 14))
        if bfill > 0:
            pygame.draw.rect(surf, gc_col, (pad, y, bfill, 14))
        pygame.draw.rect(surf, grid_c, (pad, y, bw, 14), 1)
        pct_t = self._font_sm.render(f"{java_pct:.0f}%", True, gc_col)
        surf.blit(pct_t, (pad + bw + 6, y))
        y += 20
        if java_total:
            t = self._font_sm.render(f"Java Heap: {java_used}/{java_total} MB", True, fg)
            surf.blit(t, (pad, y)); y += 17
        if native_mb:
            t = self._font_sm.render(f"Native Heap: {native_mb} MB", True, fg_dim)
            surf.blit(t, (pad, y)); y += 17

        # ── Death Risk (right) ────────────────────────────────────────────────
        risk  = d.get('death_risk', 'Unknown')
        rc    = _hx(d.get('death_risk_color', '#888888'))
        adj   = d.get('oom_adj')
        rss   = d.get('rss_mb', 0)
        ry    = 52 + 18  # align with GC bar

        t = self._font_sm.render("DEATH RISK", True, fg_dim)
        surf.blit(t, (rx, 52))
        pygame.draw.rect(surf, rc, (rx, ry, 120, 26))
        risk_t = self._font.render(risk.upper(), True, (0,0,0) if risk == "Safe" else (255,255,255))
        surf.blit(risk_t, (rx + 60 - risk_t.get_width() // 2, ry + 5))
        dy = ry + 32
        if adj is not None:
            t = self._font_sm.render(f"adj {adj}  ({adj_label(adj)})", True, fg)
            surf.blit(t, (rx, dy)); dy += 17
        if rss:
            t = self._font_sm.render(f"RSS: {rss} MB", True, fg_dim)
            surf.blit(t, (rx, dy))

        # Divider
        pygame.draw.line(surf, grid_c, (mid, 50), (mid, y + 4))

        # ── System RAM bar ────────────────────────────────────────────────────
        y += 12
        total_mb = d.get('total_mb', 0)
        used_mb  = d.get('used_mb', 0)
        pct      = d.get('used_pct', 0.0)
        sys_col  = err if pct > 80 else warn if pct > 60 else accent

        pygame.draw.line(surf, grid_c, (pad, y), (W - pad, y))
        y += 10
        t = self._font_sm.render("System RAM", True, fg_dim)
        surf.blit(t, (pad, y))
        ram_t = self._font_sm.render(
            f"{used_mb:,} / {total_mb:,} MB  ({pct:.0f}%)", True, fg)
        surf.blit(ram_t, (W - pad - ram_t.get_width(), y))
        y += 16
        sbw   = W - pad * 2
        sfill = int(sbw * pct / 100)
        pygame.draw.rect(surf, bar_bg, (pad, y, sbw, 10))
        if sfill > 0:
            pygame.draw.rect(surf, sys_col, (pad, y, sfill, 10))
        pygame.draw.rect(surf, grid_c, (pad, y, sbw, 10), 1)
        y += 18

        # ── Sparklines ────────────────────────────────────────────────────────
        gh = 44
        for label, hist, line_col in [
            ("System used %", list(self._mem_history), sys_col),
            ("Java heap fill %", list(self._java_history),
             err if self._java_history and self._java_history[-1] > 85
             else warn if self._java_history and self._java_history[-1] > 65
             else info),
        ]:
            if len(hist) < 2:
                continue
            y += 4
            t = self._font_sm.render(label, True, fg_dim)
            surf.blit(t, (pad, y))
            y += 13
            gx0, gx1, gy0, gy1 = pad, W - pad, y, y + gh
            gw = gx1 - gx0
            pygame.draw.rect(surf, graph_bg, (gx0, gy0, gw, gh))
            pygame.draw.rect(surf, grid_c,   (gx0, gy0, gw, gh), 1)
            for pline in (25, 50, 75):
                ly = gy0 + int(gh * (1 - pline / 100))
                pygame.draw.line(surf, bar_bg, (gx0, ly), (gx1, ly))
            n   = len(hist)
            pts = []
            for i, v in enumerate(hist):
                px_ = gx0 + int(i * gw / max(n - 1, 1))
                py_ = gy0 + int(gh * (1 - v / 100))
                pts.append((px_, py_))
            if len(pts) >= 2:
                pygame.draw.lines(surf, line_col, False, pts, 2)
            y += gh + 2

        # Footer
        t = self._font_sm.render("Cmd+R=refresh  Esc=exit", True, fg_dim)
        surf.blit(t, (pad, H - 24))
