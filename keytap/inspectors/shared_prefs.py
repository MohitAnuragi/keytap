import xml.etree.ElementTree as ET

from keytap.device.adb import adb


def list_files(pkg):
    result = adb("shell", "run-as", pkg, "ls", f"/data/data/{pkg}/shared_prefs/")
    return [f.strip() for f in result.stdout.decode().splitlines()
            if f.strip().endswith(".xml")]


def read_file(pkg, fname):
    return adb("shell", "run-as", pkg, "cat",
               f"/data/data/{pkg}/shared_prefs/{fname}").stdout.decode()


def parse_xml(xml_str):
    items = []
    try:
        root = ET.fromstring(xml_str)
        for child in root:
            key = child.get("name", "?")
            if child.tag == "string":
                val = child.text or ""
            elif child.tag in ("boolean", "int", "long", "float"):
                val = child.get("value", "?")
            elif child.tag == "set":
                val = "{" + ", ".join(i.text or "" for i in child) + "}"
            else:
                val = f"<{child.tag}>"
            items.append((key, val))
    except Exception as e:
        items.append(("parse error", str(e)))
    return items


def fetch(pkg):
    items = []
    try:
        for fname in list_files(pkg):
            xml = read_file(pkg, fname)
            items.extend(parse_xml(xml))
    except Exception as e:
        items = [("error", str(e))]
    return items
