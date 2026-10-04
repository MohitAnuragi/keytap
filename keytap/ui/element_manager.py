"""
ElementManager: element overlay state machine.
Handles fetching, label input, change detection.
"""

import logging
import threading
import time

import pygame

from keytap.device.elements import LABEL_CHARS, _is_layout, dump_elements

_LOG = logging.getLogger(__name__)


class ElementManager:

    def __init__(self, serial, scale, on_status, on_tap):
        """
        serial     : ADB serial (or None)
        scale      : device-to-window scale factor
        on_status  : callable(str) - thread-safe status callback
        on_tap     : callable(cx, cy, hint) - fires when element is tapped
        """
        self._serial    = serial
        self._scale     = scale
        self._on_status = on_status
        self._on_tap    = on_tap

        self.active      = False
        self.loading     = False
        self.show_bounds = False
        self._elements   = []
        self._buf        = []
        self._ref_surf   = None   # pygame Surface snapshot when last fetched
        self._last_dump_t = 0.0
        self._headless   = False  # True when active for recording only (no visual)

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def enter(self, ref_surf=None, headless=False):
        """Start element mode. ref_surf: current frame pygame Surface for change detection.
        headless=True: populate elements for recording lookups but show no labels."""
        if self.loading:
            return
        self.loading = True
        self._headless = headless
        if not self.active and not headless:
            self._on_status("dumping UI hierarchy...")
        threading.Thread(target=self._fetch, args=(ref_surf,), daemon=True).start()

    def exit(self):
        self.active      = False
        self.loading     = False
        self.show_bounds = False
        self._headless   = False
        self._elements   = []
        self._buf        = []

    def toggle_bounds(self):
        self.show_bounds = not self.show_bounds

    # ── Input ─────────────────────────────────────────────────────────────────

    def handle_char(self, char):
        """
        Feed one character. Returns True if consumed (do not pass to grid input).
        Fires on_tap when a matching 2-char label is entered.
        """
        char = char.lower()
        if char not in LABEL_CHARS:
            return False

        self._buf.append(char)

        if len(self._buf) == 1:
            matches = [el for el in self._elements if el['label'][0] == char]
            if not matches:
                self._on_status(f"no element '{char}...'")
                self._buf.clear()
            else:
                self._on_status(f"'{char}...' — press second key  |  Backspace=cancel")
            return True

        if len(self._buf) == 2:
            label = ''.join(self._buf)
            self._buf.clear()
            el = next((e for e in self._elements if e['label'] == label), None)
            if el:
                hint = el['text'] or el['resource_id'] or label
                self._on_tap(el['cx'], el['cy'], hint, el)
            else:
                self._on_status(f"no element '{label}'")
            return True

        return False

    def clear_buf(self):
        self._buf.clear()

    # ── Renderer data ────────────────────────────────────────────────────────

    def element_at(self, dx: int, dy: int):
        """Return best element whose bounds contain device point (dx, dy), or None.

        Ranking: leaf widget < layout container; then smallest area.
        This ensures a Button inside a ConstraintLayout wins over the container.
        """
        matches = [
            el for el in self._elements
            if el['x1'] <= dx <= el['x2'] and el['y1'] <= dy <= el['y2']
        ]
        if not matches:
            _LOG.debug("element_at (%d,%d): no candidates", dx, dy)
            return None

        best = min(matches, key=lambda e: (
            _is_layout(e.get('class_name', '')),          # 0=widget wins, 1=layout loses
            (e['x2'] - e['x1']) * (e['y2'] - e['y1']),   # smaller area wins among ties
        ))

        _LOG.debug(
            "element_at (%d,%d): %d candidates → picked class=%s id='%s' text='%s'",
            dx, dy, len(matches),
            best.get('class_name', '?'),
            best.get('resource_id', ''),
            best.get('text', ''),
        )
        return best

    def elements_for_renderer(self):
        """Return list of element dicts shaped for GridRenderer.composite()."""
        if self._headless:
            return []
        s   = self._scale
        buf = self._buf
        out = []
        for el in self._elements:
            if not buf:
                display_label, active = el['label'], True
            elif el['label'][0] == buf[0]:
                display_label, active = el['label'], True
            else:
                display_label, active = el['label'], False
            out.append({
                **el,
                'wx1': int(el['x1'] * s),
                'wy1': int(el['y1'] * s),
                'wx2': int(el['x2'] * s),
                'wy2': int(el['y2'] * s),
                'display_label': display_label,
                'active':        active,
                'show_bounds':   self.show_bounds,
            })
        return out

    def idle_status(self):
        if self._headless:
            return
        n = len(self._elements)
        bounds_hint = "Tab=hide bounds" if self.show_bounds else "Tab=show bounds"
        self._on_status(
            f"element mode: {n} elements  |  type 2-char label to tap"
            f"  {bounds_hint}  e=exit"
        )

    # ── Screen change detection ───────────────────────────────────────────────

    def check_screen_change(self, frame_surf):
        """Returns True if screen changed enough to warrant re-fetching elements."""
        if self._ref_surf is None:
            return False
        if time.time() - self._last_dump_t < 0.5:
            return False

        sw, sh = 16, 36  # tiny comparison size - fast enough in Python
        try:
            small   = pygame.transform.scale(frame_surf,    (sw, sh))
            ref_s   = pygame.transform.scale(self._ref_surf, (sw, sh))
            s_bytes = pygame.image.tostring(small, "RGB")
            r_bytes = pygame.image.tostring(ref_s, "RGB")
        except Exception:
            return False

        n = sw * sh
        diffs = 0
        for i in range(0, len(s_bytes), 3):
            if (abs(s_bytes[i]   - r_bytes[i]) +
                abs(s_bytes[i+1] - r_bytes[i+1]) +
                abs(s_bytes[i+2] - r_bytes[i+2])) > 30:
                diffs += 1
        return diffs / n > 0.08

    # ── Internal ─────────────────────────────────────────────────────────────

    def _fetch(self, ref_surf):
        try:
            els = dump_elements(self._serial)
        except Exception as ex:
            self.loading = False
            if not self._headless:
                self._on_status(f"element dump failed: {ex}")
            return

        self._elements    = els
        self.loading      = False
        self.active       = True
        self._ref_surf    = ref_surf.copy() if ref_surf is not None else None
        self._last_dump_t = time.time()
        self._buf.clear()
