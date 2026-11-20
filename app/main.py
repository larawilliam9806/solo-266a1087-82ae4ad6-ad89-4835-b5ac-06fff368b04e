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
  POST /api/clip              按分析路网版本的路段里程范围裁剪行程,返回连续片段
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import Body, FastAPI, HTTPException, Query

from . import db
from .clip import clip_analysis, merge_intervals
from .matcher import NetworkIndex, Segment, TrackPoint
from .schemas import ClipRange, ClipRequest, MatchRequest, NetworkCreate, TrajectoryCreate
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
            "matched_points, total_distance_m, has_detail) VALUES (?, ?, ?, ?, ?, ?, 1)",
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
        # 裁剪所需明细与分析在同一事务内落库:保存失败则整次分析都不生成
        conn.executemany(
            "INSERT INTO analysis_details (analysis_id, seq, run_seq, segment_id, "
            "enter_time, leave_time, from_m, to_m) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [(aid, k, d["run_seq"], d["segment_id"], d["enter_time"], d["leave_time"],
              d["from_m"], d["to_m"])
             for k, d in enumerate(result["detail"])],
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
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


# ------------------------------------------------------------------ 轨迹裁剪
def _ranges_from_body(body) -> list[ClipRange]:
    """请求体既可以是 ``{"analysis_id", "ranges"}``,也可以是直接的范围列表。"""
    if isinstance(body, list):
        return body
    return body.ranges


def _check_ranges(ranges: list[ClipRange], lengths: dict[str, float],
                  analysis_id: int, version: str) -> dict[str, list[list[float]]]:
    """校验范围并返回按路段分组的区间(尚未合并)。

    任一路段不存在、起止里程不递增或超出路段长度,整次请求拒绝,不做部分裁剪。
    """
    if not ranges:
        raise HTTPException(422, "至少需要一个路段里程范围")
    by_segment: dict[str, list[tuple[float, float]]] = {}
    for r in ranges:
        if r.segment_id not in lengths:
            raise HTTPException(
                422, f"路段 '{r.segment_id}' 不存在于分析 {analysis_id} 所用的路网版本 '{version}'")
        if not r.end_m > r.start_m:
            raise HTTPException(
                422, f"路段 '{r.segment_id}' 的范围非法:end_m({r.end_m}) 必须大于 start_m({r.start_m})")
        if r.end_m > lengths[r.segment_id] + 1e-6:
            raise HTTPException(
                422, f"路段 '{r.segment_id}' 的范围 [{r.start_m}, {r.end_m}] "
                     f"超出路段长度 {lengths[r.segment_id]}m")
        by_segment.setdefault(r.segment_id, []).append((r.start_m, r.end_m))
    return {sid: merge_intervals(rs) for sid, rs in by_segment.items()}


def _run_clip(analysis_id: int, ranges: list[ClipRange]) -> dict:
    conn = db.connect()
    try:
        a = conn.execute(
            "SELECT a.id, a.has_detail, a.trajectory_id, n.version AS network_version "
            "FROM analyses a JOIN networks n ON n.id = a.network_id WHERE a.id = ?",
            (analysis_id,)
        ).fetchone()
        if not a:
            raise HTTPException(404, f"分析 {analysis_id} 不存在")
        if not a["has_detail"]:
            raise HTTPException(
                409, f"分析 {analysis_id} 缺少裁剪所需明细(可能由旧版本生成),请重新分析后再申请裁剪")
        seg_rows = conn.execute(
            "SELECT s.segment_id, s.length_m FROM segments s "
            "JOIN analyses a ON a.network_id = s.network_id WHERE a.id = ?", (a["id"],)
        ).fetchall()
        lengths = {r["segment_id"]: r["length_m"] for r in seg_rows}
        merged = _check_ranges(ranges, lengths, a["id"], a["network_version"])
        detail_rows = conn.execute(
            "SELECT seq, run_seq, segment_id, enter_time, leave_time, from_m, to_m "
            "FROM analysis_details WHERE analysis_id = ? ORDER BY seq", (a["id"],)
        ).fetchall()
    finally:
        conn.close()

    pieces = clip_analysis([dict(r) for r in detail_rows], merged)

    out_pieces: list[dict] = []
    flat_segments: list[dict] = []
    for p in pieces:
        segments = []
        for s in p["segments"]:
            item = {
                "segment_id": s["segment_id"],
                # 裁剪后的里程(距路段起点,米);from_m/to_m 为同义别名
                "start_m": round(s["start_m"], 3),
                "end_m": round(s["end_m"], 3),
                "from_m": round(s["start_m"], 3),
                "to_m": round(s["end_m"], 3),
                "distance_m": round(s["end_m"] - s["start_m"], 3),
                "enter_time": fmt_ts(s["enter_time"]),
                "leave_time": fmt_ts(s["leave_time"]),
            }
            segments.append(item)
            flat_segments.append({"piece": p["seq"], "seq": len(flat_segments),
                                  **{k: v for k, v in item.items() if k not in ("from_m", "to_m")}})
        out_pieces.append({
            "seq": p["seq"],
            "enter_time": segments[0]["enter_time"],
            "leave_time": segments[-1]["leave_time"],
            "distance_m": round(sum(s["distance_m"] for s in segments), 3),
            "segments": segments,
        })

    return {"analysis_id": a["id"], "trajectory_id": a["trajectory_id"],
            "network_version": a["network_version"],
            "piece_count": len(out_pieces),
            "distance_m": round(sum(p["distance_m"] for p in out_pieces), 3),
            # 连续片段:同一片段内路段首尾相接;片段之间因驶出范围或原有缺口而断开
            "pieces": out_pieces,
            # 同一内容的扁平视图:按原行程顺序逐路段列出裁剪后的里程与时刻
            "segments": flat_segments}


@app.post("/api/clip")
def clip(body: ClipRequest | list[ClipRange] = Body(...),
         analysis_id: int | None = Query(default=None, description="分析编号(请求体已含时可省略)")):
    """按分析路网版本的路段里程范围裁剪行程(分析编号也可放在路径中)。

    请求体为 ``{"analysis_id": ..., "ranges": [
    {"segment_id": "...", "start_m": ..., "end_m": ...}, ...]}``;也可直接提交范围列表,
    此时分析编号可通过查询参数 ``?analysis_id=`` 指定。``start_m`` / ``end_m``
    (别名 ``from_m`` / ``to_m``)为距路段起点的里程,单位米。

    - 路段必须存在于该分析所用的路网版本;起止里程须递增且落在路段长度内,
      任一范围非法则整次请求被拒绝,不做部分裁剪。
    - 同一路段的多个范围取并集;驶出范围或遇到原分析中的缺口即断开为多个片段。
    - 按原行程顺序返回连续片段,逐路段给出裁剪后的起止里程与进出 UTC 时刻;
      裁剪端点时刻按原分析中相邻采样点间的行驶里程插值;静止记录不单独成段。
    - 无交集时返回空列表;旧分析缺少明细时返回 409,提示重新分析。
    """
    if isinstance(body, ClipRequest):
        if analysis_id is not None and body.analysis_id not in (None, analysis_id):
            raise HTTPException(422, "请求体中的 analysis_id 与查询参数不一致")
        aid = body.analysis_id if body.analysis_id is not None else analysis_id
    else:
        aid = analysis_id
    if aid is None:
        raise HTTPException(422, "缺少 analysis_id:请在请求体中提供,或通过查询参数 ?analysis_id= 指定")
    return _run_clip(aid, _ranges_from_body(body))


@app.post("/api/analyses/{analysis_id}/clip")
def clip_analysis_endpoint(analysis_id: int,
                           body: ClipRequest | list[ClipRange] = Body(...)):
    """同 ``POST /api/clip``,分析编号取自路径。"""
    if isinstance(body, ClipRequest) and body.analysis_id not in (None, analysis_id):
        raise HTTPException(422, "路径中的 analysis_id 与请求体不一致")
    return _run_clip(analysis_id, _ranges_from_body(body))


@app.get("/health")
def health():
    return {"status": "ok"}