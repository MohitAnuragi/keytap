import logging
import subprocess
import time as _time
import xml.etree.ElementTree as ET

_log = logging.getLogger("keytap.hierarchy")

try:
    import uiautomator2 as u2
    HAS_U2 = True
except ImportError:
    HAS_U2 = False

from keytap.core.config import ADB_PATH
from keytap.core.utils import parse_bounds


def _node_to_dict(el):
    cls = el.get('class', '')
    short_cls = cls.rsplit('.', 1)[-1] if '.' in cls else cls

    rid = el.get('resource-id', '')
    if ':id/' in rid:
        rid = rid.split(':id/', 1)[1]

    bounds = parse_bounds(el.get('bounds', ''))
    cx = cy = 0
    if bounds:
        x1, y1, x2, y2 = bounds
        cx = (x1 + x2) // 2
        cy = (y1 + y2) // 2

    return {
        'class_name':   short_cls,
        'text':         el.get('text', '').strip(),
        'resource_id':  rid,
        'content_desc': el.get('content-desc', '').strip(),
        'bounds':       bounds,
        'cx': cx, 'cy': cy,
        'clickable':    el.get('clickable') == 'true',
        'children':     [_node_to_dict(child) for child in el],
    }


def _collect_flat(node, out):
    out.append(node)
    for child in node['children']:
        _collect_flat(child, out)


def dump_hierarchy_tree(serial=None, u2_dev=None):
    """Return (root_dict, flat_node_list). Uses u2 if available, else adb uiautomator dump."""
    _t0 = _time.time()
    _log.debug("dump start HAS_U2=%s serial=%r preconnected=%s", HAS_U2, serial, u2_dev is not None)
    if HAS_U2:
        if u2_dev is None:
            _log.debug("u2.connect() ...")
            u2_dev = u2.connect(serial) if serial else u2.connect()
            _log.debug("u2.connect() done in %.2fs", _time.time() - _t0)
        else:
            _log.debug("using pre-connected u2 device")
        _t1 = _time.time()
        xml_raw = u2_dev.dump_hierarchy()
        _log.debug("dump_hierarchy() done in %.2fs (total %.2fs)", _time.time() - _t1, _time.time() - _t0)
        xml_str = xml_raw if isinstance(xml_raw, str) else xml_raw.decode()
    else:
        cmd = [ADB_PATH]
        if serial:
            cmd += ['-s', serial]
        cmd += ['shell', 'uiautomator', 'dump', '/dev/stdout']
        _log.debug("adb uiautomator dump ...")
        result = subprocess.run(cmd, capture_output=True, timeout=15)
        _log.debug("adb dump done in %.2fs stdout_len=%d", _time.time() - _t0, len(result.stdout))
        xml_str = result.stdout.decode(errors='replace').strip()

    _tp = _time.time()
    root_el = ET.fromstring(xml_str)
    if root_el.tag == 'hierarchy':
        children = list(root_el)
        if len(children) == 1:
            root_node = _node_to_dict(children[0])
        else:
            root_node = {
                'class_name': 'root', 'text': '', 'resource_id': '',
                'content_desc': '', 'bounds': None, 'cx': 0, 'cy': 0,
                'clickable': False,
                'children': [_node_to_dict(c) for c in children],
            }
    else:
        root_node = _node_to_dict(root_el)

    flat = []
    _collect_flat(root_node, flat)
    _log.debug("parse+flatten done in %.2fs, %d nodes (total %.2fs)", _time.time() - _tp, len(flat), _time.time() - _t0)
    return root_node, flat
