"""
SQL queries over a Perfetto trace via trace_processor (pip install perfetto).
First use downloads the trace_processor binary, so it needs network once.
"""

_JANK_SQL = """
SELECT a.ts - (SELECT start_ts FROM trace_bounds) AS rel_ts,
       a.ts AS ts, a.dur AS dur, a.jank_type AS jank_type,
       COALESCE(a.layer_name, '') AS layer, a.upid AS upid,
       COALESCE(p.name, '') AS process, COALESCE(p.pid, 0) AS pid
FROM actual_frame_timeline_slice a
LEFT JOIN process p USING (upid)
WHERE a.jank_type IS NOT NULL AND a.jank_type NOT IN ('None', 'Unspecified', '')
ORDER BY a.dur DESC
LIMIT 500
"""

_SLICE_SQL = """
SELECT s.name AS name, s.dur AS dur, t.name AS thread, s.depth AS depth
FROM slice s
JOIN thread_track tt ON s.track_id = tt.id
JOIN thread t USING (utid)
WHERE t.upid = {upid}
  AND (t.tid = {pid} OR t.name = 'RenderThread')
  AND s.ts < {end} AND s.ts + s.dur > {start}
ORDER BY s.dur DESC
LIMIT {limit}
"""


def _open(path):
    try:
        from perfetto.trace_processor import TraceProcessor
    except ImportError:
        raise RuntimeError("missing dependency: pip install perfetto")
    return TraceProcessor(trace=path)


def analyze(path):
    """Returns (janky_frames, warnings). Frames sorted worst first; times in ns."""
    tp = _open(path)
    try:
        frames = [
            {"rel_ts": r.rel_ts, "ts": r.ts, "dur": r.dur, "jank_type": r.jank_type,
             "layer": r.layer, "upid": r.upid, "process": r.process, "pid": r.pid}
            for r in tp.query(_JANK_SQL)
        ]
        warnings = []
        total = next(iter(tp.query("SELECT COUNT(*) AS n FROM actual_frame_timeline_slice"))).n
        if total == 0:
            warnings.append("no frame timeline data (needs Android 12+ and frametimeline source)")
        for r in tp.query("SELECT name, SUM(value) AS v FROM stats "
                          "WHERE severity = 'data_loss' AND value > 0 GROUP BY name"):
            warnings.append(f"dropped data: {r.name}={int(r.v)} (increase buffer or use light preset)")
        return frames, warnings
    finally:
        tp.close()


def frame_slices(path, frame, limit=12):
    """Longest main-thread and RenderThread slices overlapping one janky frame."""
    tp = _open(path)
    try:
        sql = _SLICE_SQL.format(
            upid=int(frame["upid"] or 0), pid=int(frame["pid"] or 0),
            start=int(frame["ts"]), end=int(frame["ts"] + frame["dur"]), limit=int(limit),
        )
        return [{"name": r.name, "dur": r.dur, "thread": r.thread, "depth": r.depth}
                for r in tp.query(sql)]
    finally:
        tp.close()
