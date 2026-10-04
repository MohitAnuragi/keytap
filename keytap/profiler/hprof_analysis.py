"""
Content-level analysis of a full HPROF dump (see hprof_dump.py): activity state, RecyclerView rows,
bitmap sources, duplicate strings. Run as a script so its large memory use ends with the process:

    python -m keytap.profiler.hprof_analysis <dump.hprof>     # writes <dump.hprof>.detail.json
"""

import collections
import json
import re
import sys

import numpy as np

from keytap.profiler import hprof_parser as hp

CACHE_VERSION = 2   # bump when the detail.json layout changes; older caches are re-analysed
_URL = re.compile(r"^(https?|content|file|android\.resource|res|asset)://", re.I)
_SKIP_HOLDERS = ("sun.misc.Cleaner", "java.", "libcore.", "dalvik.", "Class:", "obj[]", "prim[]",
                 "android.view.", "android.graphics.RenderNode", "android.util.",
                 "android.graphics.drawable.BitmapDrawable")
_TEXT_CAP, _TEXTS_PER_ROW, _STRINGS_TOP, _STRING_CAP = 80, 12, 300, 600


class Analyzer:
    def __init__(self, path):
        self.h = hp.Hprof(path).parse().build_graph()
        self._sub = {}
        self._vg = {}
        self.by_name = {}
        for c in self.h.instances:
            self.by_name[self.h.class_name(c)] = c

    # ── helpers ───────────────────────────────────────────────────────────────

    def cn(self, oid):
        h = self.h
        if oid in h.inst_by_id:
            return h.class_name(h.inst_by_id[oid][0])
        if oid in h.obj_arrays:
            return "obj[]"
        if oid in h.prim_arrays:
            return "prim[]"
        if oid in h.classes:
            return "Class:" + h.class_name(oid)
        return None

    def is_sub(self, cid, target):
        key = (cid, target)
        if key not in self._sub:
            c, ok = cid, False
            while c and c in self.h.classes:
                if self.h.class_name(c) == target:
                    ok = True
                    break
                c = self.h.classes[c]["super"]
            self._sub[key] = ok
        return self._sub[key]

    def subclasses_with_instances(self, target):
        return [c for c in self.h.instances if self.is_sub(c, target)]

    def array_ids(self, aid, n=None):
        _, cnt, off = self.h.obj_arrays[aid]
        n = cnt if n is None else min(n, cnt)
        return [int(x) for x in np.frombuffer(self.h.m, dtype=">u4", count=n, offset=off) if x] if n else []

    def alist(self, oid):
        h = self.h
        if not oid or oid not in h.inst_by_id:
            return []
        f = h.read(oid)
        arr = f.get("java.util.ArrayList.elementData")
        return self.array_ids(arr, f["java.util.ArrayList.size"]) if arr in h.obj_arrays else []

    def text_of(self, oid):
        h = self.h
        if not oid or oid not in h.inst_by_id:
            return None
        if self.cn(oid) == "java.lang.String":
            return h.java_string(oid)
        for k, v in h.read(oid).items():
            if k.endswith(".mText") and v in h.inst_by_id and self.cn(v) == "java.lang.String":
                return h.java_string(v)
        return None

    def children(self, vid):
        h = self.h
        f = h.read(vid)
        arr = f.get("android.view.ViewGroup.mChildren")
        return self.array_ids(arr, f["android.view.ViewGroup.mChildrenCount"]) if arr in h.obj_arrays else []

    def is_viewgroup(self, cid):
        if cid not in self._vg:
            self._vg[cid] = any(n == "android.view.ViewGroup.mChildren" for n, _ in self.h.flat_fields(cid))
        return self._vg[cid]

    def view_size(self, f):
        return (f["android.view.View.mRight"] - f["android.view.View.mLeft"],
                f["android.view.View.mBottom"] - f["android.view.View.mTop"])

    # ── activities ────────────────────────────────────────────────────────────

    def activities(self):
        out = []
        for cid in self.subclasses_with_instances("android.app.Activity"):
            for oid, _, _ in self.h.instances[cid]:
                f = self.h.read(oid)
                out.append({"cls": self.h.class_name(cid),
                            "destroyed": f.get("android.app.Activity.mDestroyed"),
                            "finished": f.get("android.app.Activity.mFinished"),
                            "resumed": f.get("android.app.Activity.mResumed"),
                            "stopped": f.get("android.app.Activity.mStopped")})
        return out

    # ── RecyclerViews ─────────────────────────────────────────────────────────

    def _texts_in(self, vid, tv_classes, depth=8, out=None):
        out = [] if out is None else out
        h = self.h
        if depth < 0 or vid not in h.inst_by_id:
            return out
        c = h.inst_by_id[vid][0]
        if c in tv_classes:
            t = self.text_of(h.read(vid).get("android.widget.TextView.mText"))
            if t:
                out.append(t[:_TEXT_CAP])
        if self.is_viewgroup(c):
            for ch in self.children(vid):
                self._texts_in(ch, tv_classes, depth - 1, out)
        return out

    def recyclers(self):
        h = self.h
        rv = self.by_name.get("androidx.recyclerview.widget.RecyclerView")
        if rv is None:
            return []
        tv = set(self.subclasses_with_instances("android.widget.TextView"))
        out = []
        for oid, _, _ in h.instances[rv]:
            f = h.read(oid)
            w, ht = self.view_size(f)
            rec = h.read(f["androidx.recyclerview.widget.RecyclerView.mRecycler"])
            pool, p = 0, rec.get("androidx.recyclerview.widget.RecyclerView$Recycler.mRecyclerPool")
            if p in h.inst_by_id:
                sa = h.read(p).get("androidx.recyclerview.widget.RecyclerView$RecycledViewPool.mScrap")
                vals = h.read(sa).get("android.util.SparseArray.mValues") if sa in h.inst_by_id else None
                for sd in (self.array_ids(vals) if vals in h.obj_arrays else []):
                    if sd in h.inst_by_id:
                        pool += len(self.alist(h.read(sd).get(
                            "androidx.recyclerview.widget.RecyclerView$RecycledViewPool$ScrapData.mScrapHeap")))
            rows = []
            for ch in self.children(oid):
                rows.append({"cls": self.cn(ch) or "?",
                             "texts": self._texts_in(ch, tv)[:_TEXTS_PER_ROW]})
            out.append({"id": f["android.view.View.mID"], "w": w, "h": ht,
                        "adapter": self.cn(f["androidx.recyclerview.widget.RecyclerView.mAdapter"]) or "?",
                        "cached": len(self.alist(rec.get("androidx.recyclerview.widget.RecyclerView$Recycler.mCachedViews"))),
                        "pooled": pool,
                        "scrap": len(self.alist(rec.get("androidx.recyclerview.widget.RecyclerView$Recycler.mAttachedScrap"))),
                        "rows": rows})
        return out

    # ── bitmaps ───────────────────────────────────────────────────────────────

    def _request_url(self, oid):
        h = self.h
        for v in h.read(oid).values():
            if isinstance(v, int) and v in h.inst_by_id and self.cn(v) == "java.lang.String":
                s = h.java_string(v)
                if s and _URL.match(s):
                    return s
        return None

    def bitmaps(self):
        h = self.h
        bc = self.by_name.get("android.graphics.Bitmap")
        if bc is None:
            return []
        sr = self.by_name.get("com.bumptech.glide.request.SingleRequest")
        iv = set(self.subclasses_with_instances("android.widget.ImageView"))
        out = []
        for oid, _, _ in h.instances[bc]:
            f = h.read(oid)
            w, ht = f["android.graphics.Bitmap.mWidth"], f["android.graphics.Bitmap.mHeight"]
            start = h.node(oid)
            seen, frontier, url, view, holders = {start}, [start], None, None, []
            for depth in range(7):
                nxt = []
                for i in frontier:
                    for r in h.referrers(i):
                        r = int(r)
                        if r in seen:
                            continue
                        seen.add(r)
                        nxt.append(r)
                        o = int(h.ids[r])
                        c = h.inst_by_id[o][0] if o in h.inst_by_id else None
                        if c is not None and c == sr and url is None:
                            url = self._request_url(o)
                        elif c in iv and view is None:
                            vf = h.read(o)
                            vw, vh = self.view_size(vf)
                            view = {"cls": h.class_name(c), "id": vf["android.view.View.mID"], "w": vw, "h": vh}
                        if depth < 4 and len(holders) < 3:
                            name = self.cn(o)
                            if name and not name.startswith(_SKIP_HOLDERS) and name not in holders \
                                    and "BitmapDrawable" not in name:
                                holders.append(name)
                frontier = nxt
                if len(seen) > 20000:
                    break
            px = w * ht
            out.append({"w": w, "h": ht, "size": px * 4, "url": url, "view": view,
                        "ratio": round(px / (view["w"] * view["h"]), 2) if view and view["w"] * view["h"] else None,
                        "holders": holders})
        out.sort(key=lambda b: -b["size"])
        return out

    # ── duplicate strings ─────────────────────────────────────────────────────

    def strings(self):
        h = self.h
        sc = self.by_name.get("java.lang.String")
        counts, nbytes = collections.Counter(), {}
        for oid, _, _ in h.instances.get(sc, []):
            s, n = h.java_string_info(oid)
            if s:
                counts[s] += 1
                nbytes[s] = n
        dups = [(s, c) for s, c in counts.items() if c > 1]
        dups.sort(key=lambda x: -(x[1] - 1) * nbytes[x[0]])
        return {"distinct": len(counts), "dup_strings": len(dups),
                "extra_copies": sum(c - 1 for _, c in dups),
                "wasted_bytes": sum((c - 1) * nbytes[s] for s, c in dups),
                "top": [{"value": s[:_STRING_CAP], "len": len(s), "bytes": nbytes[s], "count": c}
                        for s, c in dups[:_STRINGS_TOP]]}


def analyze(path):
    a = Analyzer(path)
    result = {"version": CACHE_VERSION, "counts": {"instances": len(a.h.inst_by_id), "classes": len(a.h.classes)},
              "activities": a.activities(), "recyclers": a.recyclers(),
              "bitmaps": a.bitmaps(), "strings": a.strings()}
    with open(path + ".detail.json", "w") as f:
        json.dump(result, f)
    return result


if __name__ == "__main__":
    analyze(sys.argv[1])
