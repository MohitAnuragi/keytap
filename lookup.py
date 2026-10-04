"""Finding elements on the phone by selector. Used by the replayer and by assertion checks."""


def _identifiers(selector):
    return [("resourceId", selector.resource_id), ("description", selector.content_desc),
            ("text", selector.text)]


def find_element(dev, selector):
    """First matching uiautomator2 object, or None. Tries resource_id, content_desc, text in that
    order; the recorded index applies to the first identifier that is set."""
    ids = _identifiers(selector)
    primary = next((k for k, v in ids if v), None)
    for key, value in ids:
        if not value:
            continue
        kwargs = {key: value}
        if key == primary and selector.index:
            kwargs["instance"] = selector.index
        try:
            el = dev(**kwargs)
            if el.exists:
                return el
        except Exception:
            continue
    return None


def count_elements(dev, selector):
    """How many elements match the first identifier that is set."""
    for key, value in _identifiers(selector):
        if value:
            return dev(**{key: value}).count
    return 0


def foreground(serial):
    """(package, activity) of the focused window, or ("", "")."""
    from failure_bundle import _FOCUS_RE
    from utils import adb_shell
    m = _FOCUS_RE.search(adb_shell("dumpsys window | grep mCurrentFocus", serial))
    return (m.group(1), m.group(2)) if m else ("", "")
