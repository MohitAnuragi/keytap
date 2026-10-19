import string

import pygame

from keytap.core import fonts
from keytap.core.config import ARGS, CURSOR_MODE


class GridRenderer:
    """Composites the grid overlay onto a pygame Surface. Stateless except for fonts."""

    def __init__(self, win_w, win_h):
        self._win_w   = win_w
        self._win_h   = win_h
        self.font      = None   # loaded lazily after pygame.display is up
        self.elem_font = None

    def init_fonts(self):
        """Call once after pygame.display.set_mode(). Safe to call multiple times."""
        if self.font is not None:
            return
        fs = max(8, int(min(self._win_w / ARGS.cols, self._win_h / ARGS.rows) // 5))
        self.font      = fonts.load(fs)
        self.elem_font = fonts.load(10)

    # ── Public compositor ────────────────────────────────────────────────────

    def composite(self, frame_surf, cursor_row, cursor_col, input_buf,
                  elements=None, highlight_bounds=None):
        """
        frame_surf  : pygame.Surface (RGB) - will be drawn on in-place, then returned.
        Returns     : the same surface with overlay blitted onto it.
        """
        W, H = frame_surf.get_size()
        overlay = pygame.Surface((W, H), pygame.SRCALPHA)

        cw = W / ARGS.cols
        ch = H / ARGS.rows
        typed       = ''.join(input_buf).upper()
        row_selected = len(typed) == 1

        for r in range(ARGS.rows):
            row_letter = string.ascii_uppercase[r] if r < 26 else None
            row_match  = row_selected and row_letter and row_letter == typed[0]

            for c in range(ARGS.cols):
                x0 = int(c * cw)
                y0 = int(r * ch)
                x1 = int((c + 1) * cw)
                y1 = int((r + 1) * ch)
                cw_ = x1 - x0
                ch_ = y1 - y0
                is_cursor = CURSOR_MODE and r == cursor_row and c == cursor_col
                label = (f"{row_letter}{string.ascii_uppercase[c]}"
                         if row_letter else None)

                if not typed:
                    pygame.draw.rect(overlay, (255, 255, 255, 38),
                                     (x0, y0, cw_ - 1, ch_ - 1), 1)
                    if label and not CURSOR_MODE:
                        self._draw_pill(overlay, x0, y0, label, self.font, active=False)
                elif row_match:
                    pygame.draw.rect(overlay, (255, 215, 0, 28),   (x0, y0, cw_, ch_))
                    pygame.draw.rect(overlay, (255, 215, 0, 200),  (x0, y0, cw_ - 1, ch_ - 1), 1)
                    if label and not CURSOR_MODE:
                        self._draw_pill(overlay, x0, y0, label, self.font, active=True)
                else:
                    pygame.draw.rect(overlay, (255, 255, 255, 15), (x0, y0, cw_ - 1, ch_ - 1), 1)
                    if label and not CURSOR_MODE:
                        self._draw_pill(overlay, x0, y0, label, self.font, active=None)

                if is_cursor:
                    pygame.draw.rect(overlay, (0, 255, 80, 25),  (x0, y0, cw_, ch_))
                    pygame.draw.rect(overlay, (0, 255, 80, 255),
                                     (x0 + 1, y0 + 1, cw_ - 3, ch_ - 3), 2)

        if elements:
            self._draw_elements(overlay, elements)

        if highlight_bounds:
            hx1, hy1, hx2, hy2 = highlight_bounds
            hw, hh = hx2 - hx1, hy2 - hy1
            pygame.draw.rect(overlay, (255, 200, 0, 25),  (hx1, hy1, hw, hh))
            pygame.draw.rect(overlay, (255, 200, 0, 230), (hx1, hy1, hw, hh), 2)

        frame_surf.blit(overlay, (0, 0))
        return frame_surf

    # ── Element overlay ──────────────────────────────────────────────────────

    def _draw_elements(self, overlay, elements):
        show_bounds = elements[0].get('show_bounds', True) if elements else True
        for el in elements:
            x1, y1 = el['wx1'], el['wy1']
            w,  h  = el['wx2'] - x1, el['wy2'] - y1
            if el['active']:
                if show_bounds:
                    pygame.draw.rect(overlay, (0, 210, 255, 22), (x1, y1, w, h))
                    out = (0, 210, 255, 220) if el['clickable'] else (160, 160, 255, 180)
                    pygame.draw.rect(overlay, out, (x1, y1, w, h), 2)
                self._draw_elem_label(overlay, x1, y1, el['display_label'], active=True)
            else:
                if show_bounds:
                    pygame.draw.rect(overlay, (80, 80, 80, 100), (x1, y1, w, h), 1)
                self._draw_elem_label(overlay, x1, y1, el['display_label'], active=False)

    # ── Label / pill drawing ─────────────────────────────────────────────────

    def _draw_pill(self, surf, x0, y0, label, font, active=False):
        if not font:
            return
        pad = 3
        tw, th = font.size(label)
        pw, ph  = tw + pad * 2, th + pad * 2

        if active is True:
            bg, fg, fa = (0, 0, 0, 200), (0, 255, 110), 255
        elif active is False:
            bg, fg, fa = (0, 0, 0, 140), (0, 200, 75),  255
        else:
            bg, fg, fa = (0, 0, 0, 70),  (0, 150, 55),  255

        pill = pygame.Surface((pw, ph), pygame.SRCALPHA)
        pill.fill(bg)
        t = font.render(label, True, fg)
        pill.blit(t, (pad, pad))
        surf.blit(pill, (x0 + 3, y0 + 3))

    def _draw_elem_label(self, surf, x0, y0, label, active=True):
        font = self.elem_font
        if not font:
            return
        pad = 4
        tw, th = font.size(label)
        pw, ph  = tw + pad * 2, th + pad * 2

        bg = (0, 0, 0, 255) if active else (0, 0, 0, 160)
        fg = (255, 230, 0)  if active else (80, 80, 80)

        pill = pygame.Surface((pw, ph), pygame.SRCALPHA)
        pill.fill(bg)
        t = font.render(label, True, fg)
        pill.blit(t, (pad, pad))
        surf.blit(pill, (x0 + 2, y0 + 2))

    # ── Frame conversion helper ──────────────────────────────────────────────

    @staticmethod
    def pil_to_surface(pil_image):
        """Convert PIL RGB Image -> pygame Surface. Used by app until capture produces surfaces."""
        return pygame.image.fromstring(pil_image.tobytes(), pil_image.size, pil_image.mode)
