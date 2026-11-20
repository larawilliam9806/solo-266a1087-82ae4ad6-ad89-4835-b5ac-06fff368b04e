"""裁剪 API 测试:范围校验、并集、插值、静止记录、缺口/驶出断开、持久化与旧分析。"""
import os
import tempfile
from datetime import datetime

import pytest

# 必须在导入 app 之前指定独立数据库(与 test_api.py 使用各自的库)
os.environ["DATABASE_PATH"] = os.path.join(tempfile.mkdtemp(prefix="cliptest_"), "test.db")

from fastapi.testclient import TestClient

from app import db
from app.main import app

db.init_db()
client = TestClient(app)


def _ts(value: str) -> float:
    """ISO8601 -> 秒级时间戳,便于按秒比较插值结果。"""
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def _net(version="clip-v1"):
    # L 形有向路网:S1 东西(85m) -> S2 南北(111m) -> S3 东西(85m)
    return {
        "version": version,
        "segments": [
            {"segment_id": "S1", "start_lat": 39.900, "start_lon": 116.400,
             "end_lat": 39.900, "end_lon": 116.401, "length_m": 85.0},
            {"segment_id": "S2", "start_lat": 39.900, "start_lon": 116.401,
             "end_lat": 39.901, "end_lon": 116.401, "length_m": 111.0},
            {"segment_id": "S3", "start_lat": 39.901, "start_lon": 116.401,
             "end_lat": 39.901, "end_lon": 116.402, "length_m": 85.0},
        ],
    }


def _traj(points, name=None):
    return {"name": name, "points": [
        {"timestamp": t, "lat": la, "lon": lo} for t, la, lo in points]}


MAIN = [
    ("2026-02-01T08:00:00Z", 39.9000, 116.4000),
    ("2026-02-01T08:00:10Z", 39.9000, 116.4005),
    ("2026-02-01T08:00:20Z", 39.9000, 116.4010),
    ("2026-02-01T08:00:30Z", 39.9005, 116.4010),
    ("2026-02-01T08:00:40Z", 39.9010, 116.4010),
    ("2026-02-01T08:00:50Z", 39.9010, 116.4015),
    ("2026-02-01T08:01:00Z", 39.9010, 116.4020),
]

_created = {}


def setup_module(module):
    r = client.post("/api/networks", json=_net())
    assert r.status_code == 201, r.text
    t = client.post("/api/trajectories", json=_traj(MAIN)).json()
    m = client.post("/api/match", json={"trajectory_id": t["trajectory_id"],
                                        "network_version": "clip-v1"}).json()
    _created["trajectory_id"] = t["trajectory_id"]
    _created["analysis"] = m


def _clip(ranges, analysis_id=None, expect=200):
    payload = {"analysis_id": analysis_id or _created["analysis"]["analysis_id"],
               "ranges": [{"segment_id": s, "start_m": a, "end_m": b}
                          for s, a, b in ranges]}
    r = client.post("/api/clip", json=payload)
    assert r.status_code == expect, r.text
    return r.json()


# ---------------------------------------------------------------- 基本裁剪
def test_clip_full_range_returns_whole_trip():
    """覆盖全部路段的裁剪 = 原行程(逐路段给出里程与时刻)。"""
    res = _clip([("S1", 0, 85), ("S2", 0, 111), ("S3", 0, 85)])
    assert res["piece_count"] == 1
    piece = res["pieces"][0]
    assert [s["segment_id"] for s in piece["segments"]] == ["S1", "S2", "S3"]
    assert piece["enter_time"] == "2026-02-01T08:00:00.000Z"
    assert piece["leave_time"] == "2026-02-01T08:01:00.000Z"
    assert abs(res["distance_m"] - 281.0) < 1.0
    for s in piece["segments"]:
        assert s["start_m"] == 0.0
    assert [s["end_m"] for s in piece["segments"]] == [85.0, 111.0, 85.0]
    # 扁平视图与分组视图一致
    assert [s["segment_id"] for s in res["segments"]] == ["S1", "S2", "S3"]
    assert all(s["piece"] == 0 for s in res["segments"])


def test_clip_single_segment_subrange_interpolates_times():
    """只保留部分里程时,进出时刻按相邻采样点间的行驶里程插值。"""
    res = _clip([("S1", 20, 60)])
    assert res["piece_count"] == 1
    seg = res["pieces"][0]["segments"]
    assert len(seg) == 1
    s = seg[0]
    assert s["segment_id"] == "S1"
    assert s["start_m"] == 20.0 and s["end_m"] == 60.0
    # S1 全长 85m 用时 20s(08:00:00 -> 08:00:20):20m 处 = 4.706s,60m 处 = 14.118s
    assert _ts(s["enter_time"]) == pytest.approx(
        _ts("2026-02-01T08:00:04.706Z"), abs=0.01)
    assert _ts(s["leave_time"]) == pytest.approx(
        _ts("2026-02-01T08:00:14.118Z"), abs=0.01)
    assert res["pieces"][0]["enter_time"] == s["enter_time"]
    assert res["pieces"][0]["leave_time"] == s["leave_time"]


def test_clip_times_are_monotonic():
    res = _clip([("S1", 0, 85), ("S2", 0, 111), ("S3", 0, 85)])
    times = []
    for s in res["segments"]:
        assert s["enter_time"] <= s["leave_time"]
        times += [s["enter_time"], s["leave_time"]]
    assert times == sorted(times)


# ---------------------------------------------------------------- 并集 / 断开
def test_clip_same_segment_ranges_are_unioned():
    """同一路段的多个范围取并集:重叠/相接的区间合并为一段。"""
    res = _clip([("S1", 20, 60), ("S1", 50, 70)])
    assert res["piece_count"] == 1
    segs = res["pieces"][0]["segments"]
    assert len(segs) == 1
    assert segs[0]["start_m"] == 20.0 and segs[0]["end_m"] == 70.0
    assert abs(res["distance_m"] - 50.0) < 1e-6

    # 不相接的两段:同一路段产生两个片段
    res = _clip([("S1", 0, 20), ("S1", 60, 80)])
    assert res["piece_count"] == 2
    assert [(s["start_m"], s["end_m"]) for s in res["pieces"][0]["segments"]] == [(0.0, 20.0)]
    assert [(s["start_m"], s["end_m"]) for s in res["pieces"][1]["segments"]] == [(60.0, 80.0)]


def test_clip_across_segments_keeps_one_piece():
    """连续行驶跨越多个路段、范围也连续时,输出一个连续片段。"""
    res = _clip([("S1", 40, 85), ("S2", 0, 60)])
    assert res["piece_count"] == 1
    segs = res["pieces"][0]["segments"]
    assert [s["segment_id"] for s in segs] == ["S1", "S2"]
    assert (segs[0]["start_m"], segs[0]["end_m"]) == (40.0, 85.0)
    assert (segs[1]["start_m"], segs[1]["end_m"]) == (0.0, 60.0)
    # 片段内首尾相接:上一段离开时刻 = 下一段进入时刻
    assert segs[0]["leave_time"] == segs[1]["enter_time"]


def test_clip_breaks_when_driving_out_of_range():
    """范围只覆盖部分行程:驶出范围处断开,区间外无输出。"""
    res = _clip([("S1", 0, 40)])
    assert res["piece_count"] == 1
    assert res["pieces"][0]["segments"][0]["end_m"] == 40.0
    # S2/S3 不在请求范围内 -> 完全不输出
    assert all(s["segment_id"] == "S1" for s in res["segments"])


def test_clip_empty_result_when_no_intersection():
    """范围与行程无交集时返回空列表(不是错误)。"""
    # 用范围的极小值构造:轨迹从 S1 起点出发,取 S1 上未被经过的... 均有经过。
    # 改用一个只有 S1 起点附近的短轨迹来验证。
    t = client.post("/api/trajectories", json=_traj([
        ("2026-02-01T09:00:00Z", 39.9000, 116.4000),
        ("2026-02-01T09:00:10Z", 39.9000, 116.4005),
    ])).json()
    m = client.post("/api/match", json={"trajectory_id": t["trajectory_id"],
                                        "network_version": "clip-v1"}).json()
    res = _clip([("S2", 50, 100)], analysis_id=m["analysis_id"])
    assert res["piece_count"] == 0
    assert res["pieces"] == [] and res["segments"] == []
    assert res["distance_m"] == 0


# ---------------------------------------------------------------- 缺口
def test_clip_breaks_at_original_gap():
    """原分析有缺口时,两个连续运行段在裁剪结果中保持断开。"""
    split = {
        "version": "clip-split",
        "segments": [
            {"segment_id": "X1", "start_lat": 39.900, "start_lon": 116.400,
             "end_lat": 39.900, "end_lon": 116.401, "length_m": 85.0},
            {"segment_id": "X2", "start_lat": 39.910, "start_lon": 116.410,
             "end_lat": 39.910, "end_lon": 116.411, "length_m": 85.0},
        ],
    }
    assert client.post("/api/networks", json=split).status_code == 201
    pts = [
        ("2026-02-01T10:00:00Z", 39.9000, 116.4002),
        ("2026-02-01T10:00:10Z", 39.9000, 116.4008),
        ("2026-02-01T10:00:20Z", 39.9100, 116.4102),
        ("2026-02-01T10:00:30Z", 39.9100, 116.4108),
    ]
    t = client.post("/api/trajectories", json=_traj(pts)).json()
    m = client.post("/api/match", json={"trajectory_id": t["trajectory_id"],
                                        "network_version": "clip-split"}).json()
    assert len(m["gaps"]) == 1
    res = _clip([("X1", 0, 85), ("X2", 0, 85)], analysis_id=m["analysis_id"])
    assert res["piece_count"] == 2
    assert [s["segment_id"] for s in res["pieces"][0]["segments"]] == ["X1"]
    assert [s["segment_id"] for s in res["pieces"][1]["segments"]] == ["X2"]
    assert res["pieces"][0]["leave_time"] < res["pieces"][1]["enter_time"]
    # 两个片段不相连(原有缺口保留)
    assert not res["pieces"][0]["segments"][-1]["segment_id"] == \
        res["pieces"][1]["segments"][0]["segment_id"]


# ---------------------------------------------------------------- 静止记录
def test_clip_static_records_not_separate_pieces():
    """静止记录不单独成段;停留时间并入所在片段。"""
    pts = [
        ("2026-02-01T11:00:00Z", 39.9000, 116.4000),
        ("2026-02-01T11:00:10Z", 39.9000, 116.4005),
        ("2026-02-01T11:00:20Z", 39.9000, 116.4005),   # 静止
        ("2026-02-01T11:00:30Z", 39.9000, 116.4005),   # 静止
        ("2026-02-01T11:00:40Z", 39.9000, 116.4010),
    ]
    t = client.post("/api/trajectories", json=_traj(pts)).json()
    m = client.post("/api/match", json={"trajectory_id": t["trajectory_id"],
                                        "network_version": "clip-v1"}).json()
    full = _clip([("S1", 0, 85)], analysis_id=m["analysis_id"])
    assert full["piece_count"] == 1
    segs = full["pieces"][0]["segments"]
    assert len(segs) == 1      # 静止记录没有产生额外的零长度路段
    assert segs[0]["enter_time"] == "2026-02-01T11:00:00.000Z"
    assert segs[0]["leave_time"] == "2026-02-01T11:00:40.000Z"
    # 裁剪到静止点之前:离开时刻按行驶里程插值,不受后续停留影响
    res = _clip([("S1", 0, 30)], analysis_id=m["analysis_id"])
    assert res["piece_count"] == 1
    assert res["pieces"][0]["segments"][0]["end_m"] == 30.0
    assert _ts(res["pieces"][0]["leave_time"]) < _ts("2026-02-01T11:00:10.000Z")
    # 裁剪止于静止点:离开时刻取驶出范围内那一刻(停留结束)
    res = _clip([("S1", 0, 42.5)], analysis_id=m["analysis_id"])
    assert res["pieces"][0]["segments"][0]["end_m"] == 42.5
    assert res["pieces"][0]["leave_time"] == "2026-02-01T11:00:30.000Z"


def test_clip_static_only_trip_returns_empty():
    """整段轨迹都在原地静止:没有行驶里程,裁剪结果为空列表。"""
    t = client.post("/api/trajectories", json=_traj([
        ("2026-02-01T12:00:00Z", 39.9000, 116.4005),
        ("2026-02-01T12:00:10Z", 39.9000, 116.4005),
    ])).json()
    m = client.post("/api/match", json={"trajectory_id": t["trajectory_id"],
                                        "network_version": "clip-v1"}).json()
    res = _clip([("S1", 0, 85)], analysis_id=m["analysis_id"])
    assert res["piece_count"] == 0 and res["pieces"] == []


# ---------------------------------------------------------------- 校验
def test_clip_rejects_invalid_ranges_entirely():
    aid = _created["analysis"]["analysis_id"]
    # 路段不存在
    _clip([("NOPE", 0, 10)], expect=422)
    # 起止里程不递增
    _clip([("S1", 60, 20)], expect=422)
    _clip([("S1", 30, 30)], expect=422)
    # 超出路段长度
    _clip([("S1", 0, 90)], expect=422)
    # 负里程
    _clip([("S1", -5, 10)], expect=422)
    # 一个合法 + 一个非法:整次请求拒绝
    _clip([("S1", 0, 20), ("S2", 100, 200)], expect=422)
    # 校验通过后原分析不受影响
    assert _clip([("S1", 0, 20)])["piece_count"] == 1


def test_clip_missing_analysis_404():
    _clip([("S1", 0, 10)], analysis_id=999999, expect=404)


def test_clip_accepts_analysis_in_path_or_query():
    aid = _created["analysis"]["analysis_id"]
    ranges = [{"segment_id": "S1", "start_m": 0, "end_m": 20}]
    r = client.post(f"/api/analyses/{aid}/clip", json={"ranges": ranges})
    assert r.status_code == 200, r.text
    assert r.json()["analysis_id"] == aid
    r = client.post(f"/api/clip?analysis_id={aid}", json=ranges)
    assert r.status_code == 200, r.text
    assert r.json()["analysis_id"] == aid
    # 与路径/查询参数不一致 -> 422
    assert client.post(f"/api/analyses/{aid}/clip",
                       json={"analysis_id": aid + 1, "ranges": ranges}).status_code == 422
    assert client.post(f"/api/clip?analysis_id={aid}",
                       json={"analysis_id": aid + 1, "ranges": ranges}).status_code == 422


def test_clip_accepts_from_to_aliases():
    """start_m/end_m 亦可写作 from_m/to_m(与明细字段名一致)。"""
    aid = _created["analysis"]["analysis_id"]
    r = client.post("/api/clip", json={"analysis_id": aid, "ranges": [
        {"segment_id": "S1", "from_m": 0, "to_m": 20}]})
    assert r.status_code == 200, r.text
    assert r.json()["pieces"][0]["segments"][0]["end_m"] == 20.0


# ---------------------------------------------------------------- 持久化 / 旧分析
def test_clip_works_after_reconnect_without_rematching():
    """裁剪所需明细随分析落库:换连接(模拟重启)后可直接裁剪,无需重做匹配。"""
    aid = _created["analysis"]["analysis_id"]
    fresh = TestClient(app)
    r = fresh.post("/api/clip", json={"analysis_id": aid, "ranges": [
        {"segment_id": "S2", "start_m": 10, "end_m": 90}]})
    assert r.status_code == 200, r.text
    segs = r.json()["pieces"][0]["segments"]
    assert segs[0]["segment_id"] == "S2"
    assert (segs[0]["start_m"], segs[0]["end_m"]) == (10.0, 90.0)


def test_legacy_analysis_without_detail():
    """旧分析没有裁剪明细:原接口仍可查,申请裁剪时提示重新分析。"""
    aid = _created["analysis"]["analysis_id"]
    conn = db.connect()
    try:
        cur = conn.execute(
            "INSERT INTO analyses (trajectory_id, network_id, created_at, total_points, "
            "matched_points, total_distance_m, has_detail) "
            "SELECT trajectory_id, network_id, created_at, total_points, matched_points, "
            "total_distance_m, 0 FROM analyses WHERE id = ?", (aid,))
        legacy_id = cur.lastrowid
        conn.execute(
            "INSERT INTO analysis_segments (analysis_id, seq, segment_id, enter_time, "
            "leave_time, distance_m) SELECT ?, seq, segment_id, enter_time, leave_time, "
            "distance_m FROM analysis_segments WHERE analysis_id = ?", (legacy_id, aid))
        conn.commit()
    finally:
        conn.close()

    # 原接口照常可查
    got = client.get(f"/api/analyses/{legacy_id}")
    assert got.status_code == 200
    assert [p["segment_id"] for p in got.json()["path"]] == ["S1", "S2", "S3"]
    assert client.get("/api/analyses", params={"trajectory_id": _created["trajectory_id"]}
                      ).status_code == 200
    # 申请裁剪 -> 409,提示重新分析
    r = client.post("/api/clip", json={"analysis_id": legacy_id, "ranges": [
        {"segment_id": "S1", "start_m": 0, "end_m": 20}]})
    assert r.status_code == 409
    assert "重新分析" in r.json()["detail"]


def test_new_analysis_persists_detail():
    """新分析保存裁剪所需明细(含起止里程),且与路径一致。"""
    aid = _created["analysis"]["analysis_id"]
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT segment_id, from_m, to_m, enter_time, leave_time FROM analysis_details "
            "WHERE analysis_id = ? ORDER BY seq", (aid,)).fetchall()
    finally:
        conn.close()
    assert rows
    assert {r["segment_id"] for r in rows} == {"S1", "S2", "S3"}
    assert any(r["to_m"] - r["from_m"] > 1 for r in rows)


# ---------------------------------------------------------------- 保存失败 / 旧库迁移
def test_detail_save_failure_leaves_no_analysis(monkeypatch):
    """裁剪明细保存失败时整次分析不落库(同一事务回滚)。"""
    real_connect = db.connect

    class _FailOnDetail:
        def __init__(self, conn):
            self._conn = conn

        def __getattr__(self, name):
            return getattr(self._conn, name)

        def executemany(self, sql, rows):
            if "analysis_details" in sql:
                raise RuntimeError("模拟明细写入失败")
            return self._conn.executemany(sql, rows)

    t = client.post("/api/trajectories", json=_traj(MAIN)).json()
    tid = t["trajectory_id"]
    monkeypatch.setattr(db, "connect", lambda: _FailOnDetail(real_connect()))
    safe = TestClient(app, raise_server_exceptions=False)
    r = safe.post("/api/match", json={"trajectory_id": tid, "network_version": "clip-v1"})
    assert r.status_code == 500
    monkeypatch.setattr(db, "connect", real_connect)

    conn = db.connect()
    try:
        assert conn.execute("SELECT COUNT(*) AS c FROM analyses WHERE trajectory_id = ?",
                            (tid,)).fetchone()["c"] == 0
        assert conn.execute("SELECT COUNT(*) AS c FROM analysis_segments s "
                            "JOIN analyses a ON a.id = s.analysis_id "
                            "WHERE a.trajectory_id = ?", (tid,)).fetchone()["c"] == 0
    finally:
        conn.close()
    # 失败后重试可正常生成分析
    ok = client.post("/api/match", json={"trajectory_id": tid, "network_version": "clip-v1"})
    assert ok.status_code == 201
    assert ok.json()["analysis_id"]


def test_legacy_database_is_migrated(tmp_path, monkeypatch):
    """旧库(analyses 无 has_detail 列、无 analysis_details 表)启动后自动补齐。"""
    import sqlite3

    path = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(path)
    try:
        conn.executescript("""
        CREATE TABLE networks (id INTEGER PRIMARY KEY AUTOINCREMENT,
            version TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL);
        CREATE TABLE segments (id INTEGER PRIMARY KEY AUTOINCREMENT,
            network_id INTEGER NOT NULL, segment_id TEXT NOT NULL,
            start_lat REAL NOT NULL, start_lon REAL NOT NULL,
            end_lat REAL NOT NULL, end_lon REAL NOT NULL, length_m REAL NOT NULL,
            UNIQUE (network_id, segment_id));
        CREATE TABLE trajectories (id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT, created_at TEXT NOT NULL);
        CREATE TABLE trajectory_points (id INTEGER PRIMARY KEY AUTOINCREMENT,
            trajectory_id INTEGER NOT NULL, seq INTEGER NOT NULL,
            ts TEXT NOT NULL, lat REAL NOT NULL, lon REAL NOT NULL);
        CREATE TABLE analyses (id INTEGER PRIMARY KEY AUTOINCREMENT,
            trajectory_id INTEGER NOT NULL, network_id INTEGER NOT NULL,
            created_at TEXT NOT NULL, total_points INTEGER NOT NULL,
            matched_points INTEGER NOT NULL, total_distance_m REAL NOT NULL);
        CREATE TABLE analysis_segments (id INTEGER PRIMARY KEY AUTOINCREMENT,
            analysis_id INTEGER NOT NULL, seq INTEGER NOT NULL, segment_id TEXT NOT NULL,
            enter_time TEXT NOT NULL, leave_time TEXT NOT NULL, distance_m REAL NOT NULL);
        CREATE TABLE analysis_gaps (id INTEGER PRIMARY KEY AUTOINCREMENT,
            analysis_id INTEGER NOT NULL, seq INTEGER NOT NULL, start_time TEXT NOT NULL,
            end_time TEXT NOT NULL, reason TEXT NOT NULL);
        INSERT INTO networks (version, created_at) VALUES ('old', '2026-01-01T00:00:00Z');
        INSERT INTO trajectories (name, created_at) VALUES ('t', '2026-01-01T00:00:00Z');
        INSERT INTO analyses (trajectory_id, network_id, created_at, total_points,
            matched_points, total_distance_m)
            VALUES (1, 1, '2026-01-01T00:00:00Z', 1, 1, 10.0);
        INSERT INTO analysis_segments (analysis_id, seq, segment_id, enter_time,
            leave_time, distance_m)
            VALUES (1, 0, 'S1', '2026-01-01T08:00:00.000Z',
                    '2026-01-01T08:00:10.000Z', 10.0);
        """)
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setenv("DATABASE_PATH", path)
    db.init_db()
    conn = db.connect()
    try:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(analyses)").fetchall()}
        assert "has_detail" in cols
        tables = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()}
        assert "analysis_details" in tables
        # 旧分析仍在,has_detail 为 0(缺少明细) -> 原接口可查、裁剪提示重新分析
        row = conn.execute("SELECT has_detail FROM analyses WHERE id = 1").fetchone()
        assert row["has_detail"] == 0
        assert conn.execute("SELECT COUNT(*) AS c FROM analysis_segments").fetchone()["c"] == 1
    finally:
        conn.close()


def test_analysis_with_no_matched_path_is_clippable_to_empty():
    """新分析即使没有任何匹配路段(全段缺口),也带明细标记,裁剪返回空列表而不是 409。"""
    t = client.post("/api/trajectories", json=_traj([
        ("2026-02-01T13:00:00Z", 40.5000, 117.5000),
        ("2026-02-01T13:00:10Z", 40.5001, 117.5001),
    ])).json()
    m = client.post("/api/match", json={"trajectory_id": t["trajectory_id"],
                                        "network_version": "clip-v1"}).json()
    assert m["path"] == [] and len(m["gaps"]) == 1
    res = _clip([("S1", 0, 10)], analysis_id=m["analysis_id"])
    assert res["piece_count"] == 0 and res["pieces"] == []


# ---------------------------------------------------------------- 引擎单元用例
def _row(segment_id, frm, to, t0, t1, run=0):
    return {"segment_id": segment_id, "from_m": frm, "to_m": to,
            "enter_time": t0, "leave_time": t1, "run_seq": run}


def test_engine_splits_one_detail_into_two_ranges():
    """单条明细同时跨过两段不连续范围:产出两个片段,时刻按里程比例插值。"""
    from app.clip import clip_analysis, merge_intervals

    rows = [_row("S1", 0, 100, "2026-01-01T00:00:00Z", "2026-01-01T00:00:10Z")]
    ranges = {"S1": merge_intervals([(10, 20), (60, 70)])}
    pieces = clip_analysis(rows, ranges)
    assert len(pieces) == 2
    assert [(s["start_m"], s["end_m"]) for s in pieces[0]["segments"]] == [(10, 20)]
    assert [(s["start_m"], s["end_m"]) for s in pieces[1]["segments"]] == [(60, 70)]
    # 10m -> 1s, 20m -> 2s, 60m -> 6s, 70m -> 7s
    assert pieces[0]["segments"][0]["enter_time"].second == 1
    assert pieces[0]["segments"][0]["leave_time"].second == 2
    assert pieces[1]["segments"][0]["enter_time"].second == 6
    assert pieces[1]["segments"][0]["leave_time"].second == 7


def test_engine_breaks_after_leaving_and_returning():
    """相邻明细之间驶出过范围:即使下一段仍在范围内,也另起片段。"""
    from app.clip import clip_analysis

    rows = [
        _row("S1", 0, 30, "2026-01-01T00:00:00Z", "2026-01-01T00:00:03Z"),
        _row("S1", 30, 60, "2026-01-01T00:00:03Z", "2026-01-01T00:00:06Z"),
        _row("S1", 60, 90, "2026-01-01T00:00:06Z", "2026-01-01T00:00:09Z"),
    ]
    pieces = clip_analysis(rows, {"S1": [[0.0, 30.0], [60.0, 90.0]]})
    assert len(pieces) == 2
    assert [(s["start_m"], s["end_m"]) for s in pieces[0]["segments"]] == [(0.0, 30.0)]
    assert [(s["start_m"], s["end_m"]) for s in pieces[1]["segments"]] == [(60.0, 90.0)]


def test_engine_new_run_always_starts_new_piece():
    """run_seq 变化(原分析缺口)总是断开,不因里程恰好相接而合并。"""
    from app.clip import clip_analysis

    rows = [
        _row("S1", 0, 30, "2026-01-01T00:00:00Z", "2026-01-01T00:00:03Z", run=0),
        _row("S1", 30, 60, "2026-01-01T01:00:00Z", "2026-01-01T01:00:03Z", run=1),
    ]
    pieces = clip_analysis(rows, {"S1": [[0.0, 60.0]]})
    assert len(pieces) == 2


def test_engine_ignores_untouched_segments_and_zero_length_rows():
    """未请求的路段不输出;零长度明细不会自成片段。"""
    from app.clip import clip_analysis

    rows = [
        _row("S1", 10, 10, "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
        _row("S1", 10, 20, "2026-01-01T00:00:00Z", "2026-01-01T00:00:01Z"),
        _row("S2", 0, 50, "2026-01-01T00:00:01Z", "2026-01-01T00:00:06Z"),
    ]
    pieces = clip_analysis(rows, {"S1": [[0.0, 20.0]]})
    assert len(pieces) == 1
    assert [s["segment_id"] for s in pieces[0]["segments"]] == ["S1"]
