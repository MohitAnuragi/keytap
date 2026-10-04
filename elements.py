import logging
import xml.etree.ElementTree as ET

try:
    import uiautomator2 as u2
    HAS_U2 = True
except ImportError:
    HAS_U2 = False

from utils import parse_bounds

_LOG = logging.getLogger(__name__)

# Layout container class suffixes — deprioritised vs leaf widgets during dedup
_LAYOUT_SUFFIXES = (
    'Layout', 'ViewGroup', 'RecyclerView', 'ScrollView',
    'CoordinatorLayout', 'AppBarLayout', 'CollapsingToolbarLayout',
)


def _is_layout(class_name: str) -> bool:
    return any(class_name.endswith(s) for s in _LAYOUT_SUFFIXES)


# reserved: e=exit, b=back, h=home, r=recents, w/s=scroll
_CHARS = [c for c in 'abcdefghijklmnopqrstuvwxyz' if c not in 'behrsw']  # 20 chars
LABEL_CHARS = frozenset(_CHARS)
LABELS = [a + b for a in _CHARS for b in _CHARS]  # 400 two-char labels


def dump_elements(serial=None):
    """Return list of interactive elements from UI hierarchy.

    Each element: {label, cx, cy, x1, y1, x2, y2, text, resource_id, clickable}
    Raises RuntimeError if uiautomator2 not installed.
    """
    if not HAS_U2:
        raise RuntimeError(
            "uiautomator2 not installed. Run: pip install uiautomator2 "
            "then: python -m uiautomator2 init"
        )

    d = u2.connect(serial) if serial else u2.connect()
    xml_raw = d.dump_hierarchy()
    xml_str = xml_raw if isinstance(xml_raw, str) else xml_raw.decode()

    return parse_elements(xml_str)


def parse_elements(xml_str):
    """Interactive elements (clickable or focusable) of a hierarchy dump, in document order,
    with the selector index described in dump_elements. Raises ET.ParseError on bad XML."""
    root = ET.fromstring(xml_str)
    elements = []
    bounds_index = {}  # bounds -> index in elements; used to replace layouts with widgets

    seen = {}  # (kind, value) -> nodes so far with that identifier, any node, document order

    for node in root.iter('node'):
        pos = {}
        for kind, attr in (('resource_id', 'resource-id'), ('content_desc', 'content-desc'), ('text', 'text')):
            value = node.get(attr, '')
            if value:
                pos[kind] = seen.get((kind, value), 0)
                seen[(kind, value)] = pos[kind] + 1

        clickable = node.get('clickable') == 'true'
        focusable = node.get('focusable') == 'true'
        if not (clickable or focusable):
            continue

        bounds = parse_bounds(node.get('bounds', ''))
        if not bounds:
            continue
        x1, y1, x2, y2 = bounds
        if x2 <= x1 or y2 <= y1:
            continue

        class_name  = node.get('class', '')
        resource_id = node.get('resource-id', '')
        text        = node.get('text', '')
        content_desc = node.get('content-desc', '')

        el = {
            'label':        '',   # assigned below
            'cx':           (x1 + x2) // 2,
            'cy':           (y1 + y2) // 2,
            'x1': x1, 'y1': y1, 'x2': x2, 'y2': y2,
            'text':         text,
            'resource_id':  resource_id,
            'content_desc': content_desc,
            'class_name':   class_name,
            'clickable':    clickable,
            '_pos':         pos,
        }

        if bounds in bounds_index:
            idx      = bounds_index[bounds]
            existing = elements[idx]
            if _is_layout(existing['class_name']) and not _is_layout(class_name):
                # Upgrade: replace container with leaf widget at same bounds
                _LOG.debug(
                    "bounds upgrade: replacing layout '%s' (id=%s) with widget '%s' (id=%s) at %s",
                    existing['class_name'], existing['resource_id'],
                    class_name, resource_id, bounds,
                )
                el['label'] = existing['label']
                elements[idx] = el
            # else: existing is already a widget (or both layouts) — keep it
        else:
            if len(elements) >= len(LABELS):
                break
            el['label'] = LABELS[len(elements)]
            bounds_index[bounds] = len(elements)
            elements.append(el)

    # index: which of several same-identifier elements this is, using the identifier replay tries first
    for el in elements:
        pos = el.pop('_pos')
        kind = next((k for k in ('resource_id', 'content_desc', 'text') if el[k]), None)
        total = seen.get((kind, el[kind]), 0) if kind else 0
        el['index'] = pos[kind] if total > 1 else None

    _LOG.debug("dump_elements: %d elements found", len(elements))
    return elements
