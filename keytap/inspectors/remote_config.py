from keytap.inspectors.shared_prefs import list_files, read_file, parse_xml

_RC_KEYWORDS = ("config", "remote", "firebase")


def fetch(pkg):
    items = []
    try:
        files = [f for f in list_files(pkg)
                 if any(kw in f.lower() for kw in _RC_KEYWORDS)]
        for fname in files:
            xml = read_file(pkg, fname)
            items.extend(parse_xml(xml))
        if not items:
            items = [("(no remote config files found)", "try Shared Prefs for full list")]
    except Exception as e:
        items = [("error", str(e))]
    return items
