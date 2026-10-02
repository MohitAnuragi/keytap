"""
Minimal Android HPROF reader (POC): class table, instance field values including primitives,
string contents. Pass 1 indexes record offsets; field values are decoded lazily on demand.
Format: https://hg.openjdk.org/jdk6/jdk6/jdk/raw-file/tip/src/share/demo/jvmti/hprof/manual.html
plus the Android heap-dump sub-tags (0x89..0x90, 0xFE, 0xC3).
"""

import mmap
import struct
import time
from collections import defaultdict

T_STRING, T_LOAD_CLASS, T_HEAP_DUMP, T_HEAP_DUMP_SEGMENT = 0x01, 0x02, 0x0C, 0x1C

OBJECT, BOOLEAN, CHAR, FLOAT, DOUBLE, BYTE, SHORT, INT, LONG = 2, 4, 5, 6, 7, 8, 9, 10, 11
_SIZE = {BOOLEAN: 1, CHAR: 2, FLOAT: 4, DOUBLE: 8, BYTE: 1, SHORT: 2, INT: 4, LONG: 8}
_FMT = {BOOLEAN: ">?", CHAR: ">H", FLOAT: ">f", DOUBLE: ">d", BYTE: ">b", SHORT: ">h", INT: ">i", LONG: ">q"}

# sub-record sizes that are fixed, in units of (id_size multiples, extra bytes)
_FIXED = {0xFF: (1, 0), 0x01: (2, 0), 0x02: (1, 8), 0x03: (1, 8), 0x04: (1, 4), 0x05: (1, 0),
          0x06: (1, 4), 0x07: (1, 0), 0x08: (1, 8), 0x89: (1, 0), 0x8A: (1, 0), 0x8B: (1, 0),
          0x8D: (1, 0), 0x8E: (1, 8), 0x90: (1, 0), 0xFE: (1, 4)}


class Hprof:
    def __init__(self, path):
        f = open(path, "rb")
        self.m = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
        end = self.m.find(b"\0")
        self.version = self.m[:end].decode()
        self.idsz = struct.unpack_from(">I", self.m, end + 1)[0]
        self._pos0 = end + 1 + 4 + 8
        self._id = ">I" if self.idsz == 4 else ">Q"
        self.strings = {}                       # string id -> (offset, length)
        self.class_name_id = {}                 # class object id -> string id
        self.classes = {}                       # class id -> dict(super, size, fields, statics)
        self.instances = defaultdict(list)      # class id -> [(obj id, data offset, nbytes)]
        self.inst_by_id = {}                    # obj id -> (class id, data offset, nbytes)
        self.prim_arrays = {}                   # obj id -> (type, count, data offset)
        self.obj_arrays = {}                    # obj id -> (elem class id, count, data offset)
        self.roots = 0
        self._flat = {}

    # ── pass 1 ────────────────────────────────────────────────────────────────

    def parse(self):
        m, pos, size = self.m, self._pos0, len(self.m)
        while pos < size:
            tag, _, length = struct.unpack_from(">BII", m, pos)
            body, end = pos + 9, pos + 9 + length
            if tag == T_STRING:
                sid = struct.unpack_from(self._id, m, body)[0]
                self.strings[sid] = (body + self.idsz, length - self.idsz)
            elif tag == T_LOAD_CLASS:
                _, cid, _, nid = struct.unpack_from(">I" + self._id[1:] + "I" + self._id[1:], m, body)
                self.class_name_id[cid] = nid
            elif tag in (T_HEAP_DUMP, T_HEAP_DUMP_SEGMENT):
                self._heap(body, end)
            pos = end
        return self

    def _rid(self, p):
        return struct.unpack_from(self._id, self.m, p)[0]

    def _value_size(self, t):
        return self.idsz if t == OBJECT else _SIZE[t]

    def _heap(self, p, end):
        m, S, idf = self.m, self.idsz, self._id
        while p < end:
            sub = m[p]
            p += 1
            if sub in _FIXED:
                mult, extra = _FIXED[sub]
                p += mult * S + extra
                if sub != 0xFE:
                    self.roots += 1
            elif sub == 0x20:
                cid, _, sup = struct.unpack_from(">" + idf[1:] + "I" + idf[1:], m, p)
                p += S + 4 + S
                p += 5 * S                       # loader, signers, protection domain, 2 reserved
                inst_size = struct.unpack_from(">I", m, p)[0]
                p += 4
                n = struct.unpack_from(">H", m, p)[0]
                p += 2
                for _ in range(n):               # constant pool
                    t = m[p + 2]
                    p += 3 + self._value_size(t)
                n = struct.unpack_from(">H", m, p)[0]
                p += 2
                statics = []
                for _ in range(n):
                    nid, t = self._rid(p), m[p + S]
                    p += S + 1
                    statics.append((nid, t, p))
                    p += self._value_size(t)
                n = struct.unpack_from(">H", m, p)[0]
                p += 2
                fields = []
                for _ in range(n):
                    fields.append((self._rid(p), m[p + S]))
                    p += S + 1
                self.classes[cid] = {"super": sup, "size": inst_size, "fields": fields, "statics": statics}
            elif sub == 0x21:
                oid = self._rid(p)
                cid = self._rid(p + S + 4)
                nbytes = struct.unpack_from(">I", m, p + 2 * S + 4)[0]
                off = p + 2 * S + 8
                self.instances[cid].append((oid, off, nbytes))
                self.inst_by_id[oid] = (cid, off, nbytes)
                p = off + nbytes
            elif sub == 0x22:
                oid = self._rid(p)
                n = struct.unpack_from(">I", m, p + S + 4)[0]
                ecls = self._rid(p + S + 8)
                self.obj_arrays[oid] = (ecls, n, p + 2 * S + 8)
                p += 2 * S + 8 + n * S
            elif sub == 0x23:
                oid = self._rid(p)
                n = struct.unpack_from(">I", m, p + S + 4)[0]
                t = m[p + S + 8]
                self.prim_arrays[oid] = (t, n, p + S + 9)
                p += S + 9 + n * _SIZE[t]
            elif sub == 0xC3:
                p += S + 9
            else:
                raise ValueError(f"unknown heap sub-record 0x{sub:02x} at {p - 1}")

    # ── lazy decoding ─────────────────────────────────────────────────────────

    def string(self, sid):
        off, ln = self.strings[sid]
        return self.m[off:off + ln].decode("utf-8", "replace")

    def class_name(self, cid):
        nid = self.class_name_id.get(cid)
        return self.string(nid).replace("/", ".") if nid in self.strings else f"<class {cid:#x}>"

    def class_by_name(self, name):
        for cid in self.class_name_id:
            if self.class_name(cid) == name:
                return cid
        return None

    def flat_fields(self, cid):
        """Instance fields in dump order: the class's own, then each superclass's."""
        if cid not in self._flat:
            out, c = [], cid
            while c and c in self.classes:
                owner = self.class_name(c)
                out += [(f"{owner}.{self.string(nid)}", t) for nid, t in self.classes[c]["fields"]]
                c = self.classes[c]["super"]
            self._flat[cid] = out
        return self._flat[cid]

    def read(self, oid):
        """Decode one instance: {'Class.field': value}. Object fields come back as object ids."""
        cid, off, _ = self.inst_by_id[oid]
        out, p = {}, off
        for name, t in self.flat_fields(cid):
            if t == OBJECT:
                out[name] = self._rid(p)
                p += self.idsz
            else:
                out[name] = struct.unpack_from(_FMT[t], self.m, p)[0]
                p += _SIZE[t]
        return out

    def java_string(self, oid):
        """Content of a java.lang.String. ART keeps characters in a synthesized array."""
        f = self.read(oid)
        arr = f.get("java.lang.String.value")
        if arr in self.prim_arrays:
            t, n, off = self.prim_arrays[arr]
            raw = bytes(self.m[off:off + n * _SIZE[t]])
            return raw.decode("utf-16-be" if t == CHAR else "latin-1", "replace")
        return None


    # ── reference graph ───────────────────────────────────────────────────────

    def build_graph(self):
        """Dense node ids (sorted object ids) plus forward and reverse edge lists, built with numpy.
        After this: self.ids, self.src/self.dst (node indexes), self.rev_start/self.rev_src (CSR by dst)."""
        import numpy as np
        S = self.idsz
        buf = np.frombuffer(self.m, dtype=np.uint8)
        ids = np.array(sorted(set(self.inst_by_id) | set(self.obj_arrays) | set(self.prim_arrays)
                              | set(self.classes)), dtype=np.int64)
        srcs, dsts = [], []

        def rd(offs):   # big-endian id at each absolute offset
            v = np.zeros(len(offs), np.int64)
            for k in range(S):
                v = (v << 8) | buf[offs + k].astype(np.int64)
            return v

        for cid, lst in self.instances.items():
            deltas, d = [], 0
            for _, t in self.flat_fields(cid):
                if t == OBJECT:
                    deltas.append(d)
                d += self.idsz if t == OBJECT else _SIZE[t]
            if not deltas:
                continue
            oids = np.fromiter((o for o, _, _ in lst), np.int64, len(lst))
            base = np.fromiter((off for _, off, _ in lst), np.int64, len(lst))
            for dl in deltas:
                srcs.append(oids)
                dsts.append(rd(base + dl))
        for oid, (_, n, off) in self.obj_arrays.items():
            if n:
                elems = np.frombuffer(self.m, dtype=">u4" if S == 4 else ">u8", count=n, offset=off).astype(np.int64)
                srcs.append(np.full(n, oid, np.int64))
                dsts.append(elems)
        for cid, c in self.classes.items():                 # static fields hold references too
            for nid, t, p in c["statics"]:
                if t == OBJECT:
                    srcs.append(np.array([cid], np.int64))
                    dsts.append(np.array([self._rid(p)], np.int64))
        src, dst = np.concatenate(srcs), np.concatenate(dsts)
        keep = dst != 0
        src, dst = src[keep], dst[keep]
        si, di = np.searchsorted(ids, src), np.searchsorted(ids, dst)
        ok = (di < len(ids)) & (ids[np.minimum(di, len(ids) - 1)] == dst) & (ids[np.minimum(si, len(ids) - 1)] == src)
        self.ids, self.src, self.dst = ids, si[ok].astype(np.int32), di[ok].astype(np.int32)
        order = np.argsort(self.dst, kind="stable")
        self.rev_src = self.src[order]
        self.rev_start = np.searchsorted(self.dst[order], np.arange(len(ids) + 1))
        return self

    def node(self, oid):
        import numpy as np
        i = int(np.searchsorted(self.ids, oid))
        return i if i < len(self.ids) and self.ids[i] == oid else None

    def referrers(self, idx):
        return self.rev_src[self.rev_start[idx]:self.rev_start[idx + 1]]


def main(path):
    t = time.time()
    h = Hprof(path).parse()
    print(f"{h.version}  id_size={h.idsz}  parsed in {time.time() - t:.1f}s")
    print(f"strings={len(h.strings)} classes={len(h.classes)} instances={len(h.inst_by_id)} "
          f"prim_arrays={len(h.prim_arrays)} obj_arrays={len(h.obj_arrays)} roots={h.roots}")
    return h


if __name__ == "__main__":
    import sys
    main(sys.argv[1])
