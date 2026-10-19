"""
Font loader compatible with Python 3.14 + pygame.
pygame.font and pygame.freetype are both broken (circular import via sysfont).
pygame._freetype (the C extension) works and is used directly.
Returns objects with pygame.font-compatible API: .render() and .size().
"""
from pygame import _freetype

_PATHS = [
    "/System/Library/Fonts/Menlo.ttc",
    "/System/Library/Fonts/Monaco.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
]


class _Adapter:
    """Wraps pygame._freetype.Font to match pygame.font.Font API."""

    def __init__(self, ft):
        self._ft = ft

    def render(self, text, antialias, color):
        """pygame.font.Font.render(text, aa, color) → Surface"""
        surf, _ = self._ft.render(text or " ", color)
        return surf

    def size(self, text):
        """pygame.font.Font.size(text) → (w, h)"""
        r = self._ft.get_rect(text or " ")
        return r.width, r.height

    def get_height(self):
        return int(self._ft.size)


def load(size) -> "_Adapter | None":
    """Load a monospace font at `size`. Returns None only if freetype itself is broken."""
    try:
        _freetype.init()
    except Exception:
        return None

    for path in _PATHS:
        try:
            ft = _freetype.Font(path, size)
            return _Adapter(ft)
        except Exception:
            pass

    # Built-in default font (always bundled with pygame)
    try:
        ft = _freetype.Font(None, size)
        return _Adapter(ft)
    except Exception:
        return None
