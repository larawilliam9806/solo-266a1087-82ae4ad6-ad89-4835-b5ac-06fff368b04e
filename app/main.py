"""FastAPI 入口:车辆轨迹匹配 API。

接口一览:
  POST /api/networks            登记带版本的有向路网(版本唯一,登记后不可变)
  GET  /api/networks          列出路网版本
  GET  /api/networks/{v}      查看某版本路网(含路段)
  POST /api/trajectories      提交轨迹(UTC 时刻 + 坐标)
  GET  /api/trajectories/{id} 查看轨迹
  POST /api/match             在指定路网版本上执行匹配,生成连续路径与缺口
  GET  /api/analyses          按路网版本 / 轨迹查询历史分析
  GET  /            /api/analyses/{id} 查看分析结果(匹配路段、进入/离开时刻、里程、缺口原因)
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query

from . import db
from .matcher import NetworkIndex, Segment, TrackPoint
from .schemas import MatchRequest, NetworkCreate, TrajectoryCreate
from .util import fmt_ts, parse_ts, utcnow


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    yield


app = FastAPI(title="车辆轨迹匹配 API", version="1.0.0", lifespan=lifespan)

# 路网版本快照缓存:(db_path, network_id) -> NetworkIndex
# 路网版本登记后不可变,因此缓存安全;匹配全程只使用同一快照,不路网修订不影响进行中的分析。
_index_cache: dict[tuple[str, int], NetworkIndex] = {}


def get_index(network_id: int, seg_rows) -> NetworkIndex:
    key = (db.db_path(), network_id)
    idx = _index_cache.get(key)
    if idx is None:
        segments = [
            Segment(r["segment_id"], r["start_lat"], r["start_lon"],
                    r["end_lat"], r["end_lon"], r["length_m"])
            for r in seg_rows
        ]
        idx = NetworkIndex(segments)
        _index_cache[key] = idx
    return idx


# ------------------------------------------------------------------ 路网
@app.post("/api/networks", status_code=201)
def create_network(body: NetworkCreate):
    """登记一个带版本的有向路网。版本号全局唯一;同版本重复登记返回 409。"""
    conn = db.connect()
    try:
        if conn.execute("SELECT 1 FROM networks WHERE version = ?", (body.version,)).fetchone():
            raise HTTPException(409, f"路网版本 '{body.version}' 已存在;请使用新的版本号登记修订")
        now = fmt_ts(utcnow())
        cur = conn.execute("INSERT INTO networks (version, created_at) VALUES (?, ?)",
                           (body.version, now))
        nid = cur.lastrowid
        conn.executemany(
            "INSERT INTO segments (network_id, segment_id, start_lat, start_lon, "
            "end_lat, end_lon, length_m) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(nid, s.segment_id, s.start_lat, s.start_lon, s.end_lat, s.end_lon, s.length_m)
             for s in body.segments],
        )
        conn.commit()
        return {"network_id": nid, "version": body.version,
                "segment_count": len(body.segments), "created_at": now}
    finally:
        conn.close()


@app.get("/api/networks")
def list_networks():
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT n.id, n.version, n.created_at, COUNT(s.id) AS segment_count "
            "FROM networks n LEFT JOIN segments s ON s.network_id = n.id "
            "GROUP BY n.id ORDER BY n.id"
        ).fetchall()
        return {"networks": [dict(r) for r in rows]}
    finally:
        conn.close()


@app.get("/api/networks/{version}")
def get_network(version: str):
    conn = db.connect()
    try:
        net = conn.execute("SELECT * FROM networks WHERE version = ?", (version,)).fetchone()
        if not net:
            raise HTTPException(404, f"路网版本 '{version}' 不存在")
        segs = conn.execute(
            "SELECT segment_id, start_lat, start_lon, end_lat, end_lon, length_m "
            "FROM segments WHERE network_id = ? ORDER BY id", (net["id"],)
        ).fetchall()
        return {"network_id": net["id"], "version": net["version"],
                "created_at": net["created_at"],
                "segments": [dict(s) for s in segs]}
    finally:
        conn.close()


# ------------------------------------------------------------------ 轨迹
@app.post("/api/trajectories", status_code=201)
def create_trajectory(body: TrajectoryCreate):
    """提交轨迹。点按 UTC 时刻升序排列后存储。"""
    order = sorted(range(len(body.points)),
                   key=lambda i: (body.points[i].timestamp, i))
    now = fmt_ts(utcnow())
    conn = db.connect()
    try:
        cur = conn.execute("INSERT INTO trajectories (name, created_at) VALUES (?, ?)",
                           (body.name, now))
        tid = cur.lastrowid
        conn.executemany(
            "INSERT INTO trajectory_points (trajectory_id, seq, ts, lat, lon) VALUES (?, ?, ?, ?, ?)",
            [(tid, seq, fmt_ts(body.points[i].timestamp), body.points[i].lat, body.points[i].lon)
             for seq, i in enumerate(order)],
        )
        conn.commit()
        return {"trajectory_id": tid, "name": body.name,
                "point_count": len(body.points), "created_at": now}
    finally:
        conn.close()


@app.get("/api/trajectories/{trajectory_id}")
def get_trajectory(trajectory_id: int):
    conn = db.connect()
    try:
        traj = conn.execute("SELECT * FROM trajectories WHERE id = ?", (trajectory_id,)).fetchone()
        if not traj:
            raise HTTPException(404, f"轨迹 {trajectory_id} 不存在")
        pts = conn.execute(
            "SELECT seq, ts AS timestamp, lat, lon FROM trajectory_points "
            "WHERE trajectory_id = ? ORDER BY seq", (trajectory_id,)
        ).fetchall()
        return {"trajectory_id": traj["id"], "name": traj["name"],
                "created_at": traj["created_at"],
                "points": [dict(p) for p in pts]}
    finally:
        conn.close()


# ------------------------------------------------------------------ 匹配分析
@app.post("/api/match", status_code=201)
def run_match(body: MatchRequest):
    """在指定路网版本上执行轨迹匹配。

    一次分析只读取该版本的路网快照,不混用其他版本;结果连同版本号一起落库,
    之后路网修订不影响本次分析。
    """
    conn = db.connect()
    try:
        traj = conn.execute("SELECT * FROM trajectories WHERE id = ?",
                            (body.trajectory_id,)).fetchone()
        if not traj:
            raise HTTPException(404, f"轨迹 {body.trajectory_id} 不存在")
        net = conn.execute("SELECT * FROM networks WHERE version = ?",
                           (body.network_version,)).fetchone()
        if not net:
            raise HTTPException(404, f"路网版本 '{body.network_version}' 不存在")
        # 一次性读入该版本快照:分析期间不混用版本
        seg_rows = conn.execute(
            "SELECT * FROM segments WHERE network_id = ? ORDER BY id", (net["id"],)
        ).fetchall()
        pt_rows = conn.execute(
            "SELECT ts, lat, lon FROM trajectory_points WHERE trajectory_id = ? ORDER BY seq",
            (traj["id"],)
        ).fetchall()
    finally:
        conn.close()

    index = get_index(net["id"], seg_rows)
    points = [TrackPoint(parse_ts(r["ts"]), r["lat"], r["lon"]) for r in pt_rows]
    result = index.match(points)

    now = fmt_ts(utcnow())
    conn = db.connect()
    try:
        cur = conn.execute(
            "INSERT INTO analyses (trajectory_id, network_id, created_at, total_points, "
            "matched_points, total_distance_m) VALUES (?, ?, ?, ?, ?, ?)",
            (traj["id"], net["id"], now, result["total_points"],
             result["matched_points"], result["total_distance_m"]),
        )
        aid = cur.lastrowid
        conn.executemany(
            "INSERT INTO analysis_segments (analysis_id, seq, segment_id, enter_time, "
            "leave_time, distance_m) VALUES (?, ?, ?, ?, ?, ?)",
            [(aid, p["seq"], p["segment_id"], p["enter_time"], p["leave_time"], p["distance_m"])
             for p in result["path"]],
        )
        conn.executemany(
            "INSERT INTO analysis_gaps (analysis_id, seq, start_time, end_time, reason) "
            "VALUES (?, ?, ?, ?, ?)",
            [(aid, g["seq"], g["start_time"], g["end_time"], g["reason"])
             for g in result["gaps"]],
        )
        conn.commit()
    finally:
        conn.close()

    return {"analysis_id": aid, "trajectory_id": traj["id"],
            "network_version": net["version"], "created_at": now, **result}


@app.get("/api/analyses")
def list_analyses(network_version: str | None = Query(default=None),
                  trajectory_id: int | None = Query(default=None)):
    """查询历史分析;可按原路网版本过滤(路网修订后旧分析仍可查)。"""
    sql = ("SELECT a.id AS analysis_id, a.trajectory_id, n.version AS network_version, "
           "a.created_at, a.total_points, a.matched_points, a.total_distance_m "
           "FROM analyses a JOIN networks n ON n.id = a.network_id")
    cond, args = [], []
    if network_version is not None:
        cond.append("n.version = ?")
        args.append(network_version)
    if trajectory_id is not None:
        cond.append("a.trajectory_id = ?")
        args.append(trajectory_id)
    if cond:
        sql += " WHERE " + " AND ".join(cond)
    sql += " ORDER BY a.id"
    conn = db.connect()
    try:
        rows = conn.execute(sql, args).fetchall()
        return {"analyses": [dict(r) for r in rows]}
    finally:
        conn.close()


@app.get("/api/analyses/{analysis_id}")
def get_analysis(analysis_id: int):
    conn = db.connect()
    try:
        a = conn.execute(
            "SELECT a.*, n.version AS network_version FROM analyses a "
            "JOIN networks n ON n.id = a.network_id WHERE a.id = ?", (analysis_id,)
        ).fetchone()
        if not a:
            raise HTTPException(404, f"分析 {analysis_id} 不存在")
        segs = conn.execute(
            "SELECT seq, segment_id, enter_time, leave_time, distance_m "
            "FROM analysis_segments WHERE analysis_id = ? ORDER BY seq", (analysis_id,)
        ).fetchall()
        gaps = conn.execute(
            "SELECT seq, start_time, end_time, reason "
            "FROM analysis_gaps WHERE analysis_id = ? ORDER BY seq", (analysis_id,)
        ).fetchall()
        return {"analysis_id": a["id"], "trajectory_id": a["trajectory_id"],
                "network_version": a["network_version"], "created_at": a["created_at"],
                "total_points": a["total_points"], "matched_points": a["matched_points"],
                "total_distance_m": a["total_distance_m"],
                "path": [dict(s) for s in segs],
                "gaps": [dict(g) for g in gaps]}
    finally:
        conn.close()


@app.get("/health")
def health():
    return {"status": "ok"}
