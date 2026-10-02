"""
SQL queries over a Java heap snapshot (see heap_dump.py) via trace_processor.
Class tables are cached next to the snapshot so reopening and diffing are instant.
"""

import json
import os

_DOMINATOR = "include perfetto module android.memory.heap_graph.dominator_tree;"

_NAME = "COALESCE(c.deobfuscated_name, c.name)"

# Retained size counts an object only when its dominator is a different class,
# so nested same-class objects (e.g. a View inside a View) are not double counted.
_CLASSES_SQL = _DOMINATOR + f"""
SELECT c.id AS cid, {_NAME} AS name, COUNT(*) AS cnt,
       SUM(o.self_size + o.native_size) AS shallow,
       SUM(CASE WHEN p.type_id IS NULL OR p.type_id != o.type_id
                THEN d.dominated_size_bytes + d.dominated_native_size_bytes ELSE 0 END) AS retained
FROM heap_graph_object o
JOIN heap_graph_class c ON o.type_id = c.id
JOIN heap_graph_dominator_tree d ON d.id = o.id
LEFT JOIN heap_graph_object p ON p.id = d.idom_id
WHERE o.reachable
GROUP BY c.id
ORDER BY retained DESC
"""

_INSTANCES_SQL = _DOMINATOR + """
SELECT o.id AS id, o.self_size + o.native_size AS size,
       d.dominated_size_bytes + d.dominated_native_size_bytes AS retained,
       o.root_distance AS rd
FROM heap_graph_object o
JOIN heap_graph_dominator_tree d USING (id)
WHERE o.type_id = {cid} AND o.reachable
ORDER BY retained DESC
LIMIT {limit}
"""

# Pixel memory is native_size on API 26+. Width/height are the only primitive fields the dump keeps.
_BITMAPS_SQL = """
SELECT o.id AS id, o.self_size + o.native_size AS size,
       MAX(CASE WHEN p.field_name LIKE '%.mWidth'  THEN p.int_value END) AS w,
       MAX(CASE WHEN p.field_name LIKE '%.mHeight' THEN p.int_value END) AS h
FROM heap_graph_object o
JOIN heap_graph_class c ON c.id = o.type_id AND c.name = 'android.graphics.Bitmap'
LEFT JOIN heap_graph_object_data od ON od.id = o.object_data_id
LEFT JOIN heap_graph_primitive p ON p.field_set_id = od.field_set_id
WHERE o.reachable
GROUP BY o.id
ORDER BY size DESC
"""

# One hop towards the GC root: any referrer one step closer to a root.
_HOP_SQL = f"""
SELECT r.owner_id AS owner, r.field_name AS field, {_NAME} AS cname
FROM heap_graph_reference r
JOIN heap_graph_object p ON p.id = r.owner_id
JOIN heap_graph_class c ON c.id = p.type_id
WHERE r.owned_id = {{cur}} AND p.root_distance = {{dist}} AND p.reachable
LIMIT 1
"""


def _open(path):
    try:
        from perfetto.trace_processor import TraceProcessor
    except ImportError:
        raise RuntimeError("missing dependency: pip install perfetto")
    return TraceProcessor(trace=path)


def classes(path):
    """Returns (rows, warnings). Rows: dicts cid/name/cnt/shallow/retained, largest retained first."""
    cache = path + ".classes.json"
    if os.path.isfile(cache) and os.path.getmtime(cache) >= os.path.getmtime(path):
        with open(cache) as f:
            return json.load(f)

    tp = _open(path)
    try:
        rows = [{"cid": r.cid, "name": r.name, "cnt": r.cnt,
                 "shallow": r.shallow, "retained": r.retained}
                for r in tp.query(_CLASSES_SQL)]
        warnings = []
        if not rows:
            raise RuntimeError("no heap graph in this snapshot")
        for r in tp.query("SELECT name, SUM(value) AS v FROM stats "
                          "WHERE severity = 'data_loss' AND value > 0 GROUP BY name"):
            warnings.append(f"heap may be truncated: {r.name}={int(r.v)}")
    finally:
        tp.close()
    with open(cache, "w") as f:
        json.dump([rows, warnings], f)
    return rows, warnings


_GRAPH_SQL = _DOMINATOR + """
SELECT o.id AS id, COALESCE(d.idom_id, -1) AS idom, o.type_id AS tid,
       o.self_size + o.native_size AS size,
       d.dominated_size_bytes + d.dominated_native_size_bytes AS ret,
       d.dominated_obj_count AS cnt
FROM heap_graph_object o
JOIN heap_graph_dominator_tree d USING (id)
WHERE o.reachable
"""

_HOLDS_FIELD_CAP  = 100   # owned fields shown
_HOLDS_SHARED_CAP = 30    # shared fields shown
_HOLDS_CLASS_CAP  = 12


def _graph(path):
    """Per-object arrays (dominator parent, class, size, retained size, dominated count) plus a
    children index, cached beside the snapshot. SQL recursion over the dominator tree is far too
    slow (seconds per thousand nodes), so subtrees are walked in numpy/Python instead."""
    import numpy as np
    cache = path + ".graph.npz"
    if os.path.isfile(cache) and os.path.getmtime(cache) >= os.path.getmtime(path):
        return dict(np.load(cache))
    tp = _open(path)
    try:
        rows = list(tp.query(_GRAPH_SQL))
    finally:
        tp.close()
    n = max(r.id for r in rows) + 1
    g = {"idom": np.full(n, -1, np.int64), "tid": np.full(n, -1, np.int64),
         "size": np.zeros(n, np.int64), "ret": np.zeros(n, np.int64), "cnt": np.zeros(n, np.int64)}
    for r in rows:
        g["idom"][r.id], g["tid"][r.id], g["size"][r.id] = r.idom, r.tid, r.size
        g["ret"][r.id], g["cnt"][r.id] = r.ret, r.cnt
    g["order"] = np.argsort(g["idom"], kind="stable")
    g["start"] = np.searchsorted(g["idom"][g["order"]], np.arange(n + 1), side="left")
    np.savez(cache, **g)
    return g


def _subtree(g, root):
    """Every object the root dominates, including itself."""
    import numpy as np
    order, start = g["order"], g["start"]
    out, frontier = [], np.array([root], np.int64)
    while frontier.size:
        out.append(frontier)
        kids = [order[start[p]:start[p + 1]] for p in frontier]
        frontier = np.concatenate(kids) if kids else frontier[:0]
    return np.concatenate(out)


def holds(path, obj_id):
    """What one object keeps alive. Returns dict:
    head: id/cname/size/retained/objs; fields: direct references (field, cname, id, size,
    excl = size only this object keeps alive, 0 if shared); more: fields not shown;
    by_class: everything it exclusively retains grouped by class."""
    import numpy as np
    g = _graph(path)
    obj_id = int(obj_id)
    sub = _subtree(g, obj_id)
    tids = g["tid"][sub]
    counts = np.bincount(tids)
    sizes = np.bincount(tids, weights=g["size"][sub]).astype(np.int64)
    top = np.argsort(-sizes)[:_HOLDS_CLASS_CAP]

    tp = _open(path)
    try:
        refs = [(r.f, int(r.oid)) for r in tp.query(
            "SELECT field_name AS f, owned_id AS oid FROM heap_graph_reference "
            f"WHERE owner_id = {obj_id} AND owned_id IS NOT NULL")]
        refs = [(f, o) for f, o in refs if o < len(g["tid"]) and g["tid"][o] >= 0]
        want = {int(g["tid"][obj_id])} | {int(t) for t in top} | {int(g["tid"][o]) for _, o in refs}
        names = {r.id: r.name for r in tp.query(
            f"SELECT c.id AS id, {_NAME} AS name FROM heap_graph_class c "
            f"WHERE c.id IN ({','.join(map(str, want))})")}
    finally:
        tp.close()

    by_child = {}   # several fields can point at one object (mLayout / mSavedLayout)
    for f, o in refs:
        name = f.rsplit(".", 1)[-1] or "[]"
        if o in by_child:
            if name not in by_child[o]["field"].split(", "):
                by_child[o]["field"] += ", " + name
            continue
        owned = int(g["idom"][o]) == obj_id
        by_child[o] = {"field": name, "cname": names[int(g["tid"][o])], "id": o,
                       "size": int(g["size"][o]), "excl": int(g["ret"][o]) if owned else 0}
    fields = list(by_child.values())
    own = sorted((x for x in fields if x["excl"]), key=lambda x: -x["excl"])
    shared = sorted((x for x in fields if not x["excl"]), key=lambda x: -x["size"])
    return {
        "head": {"id": obj_id, "cname": names[int(g["tid"][obj_id])], "size": int(g["size"][obj_id]),
                 "retained": int(g["ret"][obj_id]), "objs": int(g["cnt"][obj_id])},
        "owned": own[:_HOLDS_FIELD_CAP], "owned_more": max(0, len(own) - _HOLDS_FIELD_CAP),
        "shared": shared[:_HOLDS_SHARED_CAP], "shared_more": max(0, len(shared) - _HOLDS_SHARED_CAP),
        "by_class": [{"cname": names[int(t)], "cnt": int(counts[t]), "size": int(sizes[t])} for t in top if counts[t]],
    }


def diff(old_rows, new_rows):
    """Per-class growth from old to new, largest retained growth first. Unchanged classes dropped."""
    old = {r["name"]: r for r in old_rows}
    out = []
    for n in new_rows:
        o = old.get(n["name"], {"cnt": 0, "shallow": 0, "retained": 0})
        d = {"cid": n["cid"], "name": n["name"], "cnt": n["cnt"] - o["cnt"],
             "shallow": n["shallow"] - o["shallow"], "retained": n["retained"] - o["retained"]}
        if d["cnt"] or d["shallow"] or d["retained"]:
            out.append(d)
    new_names = {n["name"] for n in new_rows}
    for o in old_rows:
        if o["name"] not in new_names:
            out.append({"cid": None, "name": o["name"], "cnt": -o["cnt"],
                        "shallow": -o["shallow"], "retained": -o["retained"]})
    out.sort(key=lambda d: -d["retained"])
    return out


def instances(path, cid, limit=50):
    """Largest instances of one class by retained size."""
    tp = _open(path)
    try:
        return [{"id": r.id, "size": r.size, "retained": r.retained, "rd": r.rd}
                for r in tp.query(_INSTANCES_SQL.format(cid=int(cid), limit=int(limit)))]
    finally:
        tp.close()


def bitmaps(path):
    """Every live Bitmap, largest first: dicts id/size/w/h/bpp/dup.
    bpp is bytes per pixel (4 = ARGB_8888); dup is how many bitmaps share the same size."""
    tp = _open(path)
    try:
        rows = [{"id": r.id, "size": r.size, "w": r.w or 0, "h": r.h or 0}
                for r in tp.query(_BITMAPS_SQL)]
    finally:
        tp.close()
    same = {}
    for r in rows:
        same[(r["w"], r["h"])] = same.get((r["w"], r["h"]), 0) + 1
    for r in rows:
        px = r["w"] * r["h"]
        r["bpp"] = round(r["size"] / px) if px else 0
        r["dup"] = same[(r["w"], r["h"])]
    return rows


def ref_path(path, obj_id):
    """Shortest reference path from a GC root to one object.
    Returns (root_type, hops, complete); hops run root side first as (class, field_to_next)."""
    tp = _open(path)
    try:
        head = next(iter(tp.query(
            f"SELECT o.root_distance AS rd, {_NAME} AS cname FROM heap_graph_object o "
            f"JOIN heap_graph_class c ON c.id = o.type_id WHERE o.id = {int(obj_id)}")))
        hops = [(head.cname, "")]
        cur, dist = int(obj_id), head.rd
        while dist > 0:
            r = next(iter(tp.query(_HOP_SQL.format(cur=cur, dist=dist - 1))), None)
            if r is None:
                return "", hops[::-1], False
            hops.append((r.cname, r.field.rsplit(".", 1)[-1]))
            cur, dist = r.owner, dist - 1
        root = next(iter(tp.query(f"SELECT root_type AS rt FROM heap_graph_object WHERE id = {cur}"))).rt
        return (root or "").replace("ROOT_", ""), hops[::-1], True
    finally:
        tp.close()
