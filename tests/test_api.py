"""端到端 API 测试:路网版本化、轨迹匹配、缺口、版本隔离。"""
import os
import tempfile

# 必须在导入 app 之前指定独立数据库
os.environ["DATABASE_PATH"] = os.path.join(tempfile.mkdtemp(prefix="trajtest_"), "test.db")

from fastapi.testclient import TestClient

from app import db
from app.main import app

db.init_db()
client = TestClient(app)


# ---------------------------------------------------------------- 测试数据
# 路网 v1:L 形有向路网(北京附近坐标)
#   N1(39.900,116.400) --S1--> N2(39.900,116.401) --S2--> N3(39.901,116.401) --S3--> N4(39.901,116.402)
NET_V1 = {
    "version": "v1",
    "segments": [
        {"segment_id": "S1", "start_lat": 39.900, "start_lon": 116.400,
         "end_lat": 39.900, "end_lon": 116.401, "length_m": 85.0},
        {"segment_id": "S2", "start_lat": 39.900, "start_lon": 116.401,
         "end_lat": 39.901, "end_lon": 116.401, "length_m": 111.0},
        {"segment_id": "S3", "start_lat": 39.901, "start_lon": 116.401,
         "end_lat": 39.901, "end_lon": 116.402, "length_m": 85.0},
    ],
}

# 路网 v2:v1 的修订版,路段改名并新增一条
NET_V2 = {
    "version": "v2",
    "segments": [
        {"segment_id": "R1", "start_lat": 39.900, "start_lon": 116.400,
         "end_lat": 39.900, "end_lon": 116.401, "length_m": 85.0},
        {"segment_id": "R2", "start_lat": 39.900, "start_lon": 116.401,
         "end_lat": 39.901, "end_lon": 116.401, "length_m": 111.0},
        {"segment_id": "R3", "start_lat": 39.901, "start_lon": 116.401,
         "end_lat": 39.901, "end_lon": 116.402, "length_m": 85.0},
        {"segment_id": "R4", "start_lat": 39.901, "start_lon": 116.402,
         "end_lat": 39.902, "end_lon": 116.402, "length_m": 111.0},
    ],
}

# 两个互不相连的分量
NET_SPLIT = {
    "version": "split",
    "segments": [
        {"segment_id": "X1", "start_lat": 39.900, "start_lon": 116.400,
         "end_lat": 39.900, "end_lon": 116.401, "length_m": 85.0},
        {"segment_id": "X2", "start_lat": 39.910, "start_lon": 116.410,
         "end_lat": 39.910, "end_lon": 116.411, "length_m": 85.0},
    ],
}

# 平行路段:P1 与 P2 相距约 11m,只有 P1 能接上 P3
NET_PARALLEL = {
    "version": "parallel",
    "segments": [
        {"segment_id": "P1", "start_lat": 39.9000, "start_lon": 116.400,
         "end_lat": 39.9000, "end_lon": 116.401, "length_m": 85.0},
        {"segment_id": "P2", "start_lat": 39.9001, "start_lon": 116.400,
         "end_lat": 39.9001, "end_lon": 116.401, "length_m": 85.0},
        {"segment_id": "P3", "start_lat": 39.9000, "start_lon": 116.401,
         "end_lat": 39.9010, "end_lon": 116.401, "length_m": 111.0},
    ],
}

# 沿 S1->S2->S3 的轨迹(点恰好落在节点上,每 10s 一个点)
TRAJ_MAIN = [
    ("2026-01-01T08:00:00Z", 39.9000, 116.4000),
    ("2026-01-01T08:00:10Z", 39.9000, 116.4005),
    ("2026-01-01T08:00:20Z", 39.9000, 116.4010),
    ("2026-01-01T08:00:30Z", 39.9005, 116.4010),
    ("2026-01-01T08:00:40Z", 39.9010, 116.4010),
    ("2026-01-01T08:00:50Z", 39.9010, 116.4015),
    ("2026-01-01T08:01:00Z", 39.9010, 116.4020),
]


def make_traj(points, name=None):
    return {"name": name,
            "points": [{"timestamp": t, "lat": la, "lon": lo} for t, la, lo in points]}


def post_json(url, payload, expect=201):
    r = client.post(url, json=payload)
    assert r.status_code == expect, r.text
    return r.json()


# ---------------------------------------------------------------- 测试用例
def test_full_match_continuous_path():
    """完整流程:登记路网 -> 提交轨迹 -> 匹配出 S1-S2-S3 连续路径,无缺口。"""
    net = post_json("/api/networks", NET_V1)
    assert net["segment_count"] == 3

    traj = post_json("/api/trajectories", make_traj(TRAJ_MAIN, "morning"))
    tid = traj["trajectory_id"]
    assert traj["point_count"] == 7

    res = post_json("/api/match", {"trajectory_id": tid, "network_version": "v1"})
    assert [p["segment_id"] for p in res["path"]] == ["S1", "S2", "S3"]
    assert res["gaps"] == []
    assert res["matched_points"] == res["total_points"] == 7
    # 里程 ≈ 85 + 111 + 85 = 281
    assert 270.0 <= res["total_distance_m"] <= 290.0
    # 进入/离开时刻单调不减,且首段进入时刻 = 轨迹起点时刻
    times = []
    for p in res["path"]:
        assert p["enter_time"] <= p["leave_time"]
        times += [p["enter_time"], p["leave_time"]]
    assert times == sorted(times)
    assert res["path"][0]["enter_time"] == "2026-01-01T08:00:00.000Z"
    assert res["path"][-1]["leave_time"] == "2026-01-01T08:01:00.000Z"
    # 各段里程与声明长度一致
    assert abs(res["path"][0]["distance_m"] - 85.0) < 1.0
    assert abs(res["path"][1]["distance_m"] - 111.0) < 1.0
    assert abs(res["path"][2]["distance_m"] - 85.0) < 1.0


def test_out_of_range_gap():
    """距所有路段超过 50m 的点 -> out_of_range 缺口。"""
    pts = [
        ("2026-01-01T09:00:00Z", 39.9000, 116.4000),
        ("2026-01-01T09:00:10Z", 39.9000, 116.4008),
        ("2026-01-01T09:00:20Z", 39.9050, 116.4000),   # 偏离约 556m,无候选
        ("2026-01-01T09:00:30Z", 39.9010, 116.4012),
        ("2026-01-01T09:00:40Z", 39.9010, 116.4018),
    ]
    traj = post_json("/api/trajectories", make_traj(pts))
    res = post_json("/api/match", {"trajectory_id": traj["trajectory_id"],
                                   "network_version": "v1"})
    assert res["matched_points"] == 4
    assert len(res["gaps"]) == 1
    gap = res["gaps"][0]
    assert gap["reason"] == "out_of_range"
    assert gap["start_time"] == "2026-01-01T09:00:10.000Z"
    assert gap["end_time"] == "2026-01-01T09:00:30.000Z"
    # 缺口前后各为一段连续路径
    assert res["path"][0]["segment_id"] == "S1"
    assert res["path"][-1]["segment_id"] == "S3"


def test_disconnected_gap():
    """两个不连通的路网分量之间 -> disconnected 缺口。"""
    post_json("/api/networks", NET_SPLIT)
    pts = [
        ("2026-01-01T10:00:00Z", 39.9000, 116.4002),
        ("2026-01-01T10:00:10Z", 39.9000, 116.4008),
        ("2026-01-01T10:00:20Z", 39.9100, 116.4102),
        ("2026-01-01T10:00:30Z", 39.9100, 116.4108),
    ]
    traj = post_json("/api/trajectories", make_traj(pts))
    res = post_json("/api/match", {"trajectory_id": traj["trajectory_id"],
                                   "network_version": "split"})
    assert res["matched_points"] == 4
    assert [p["segment_id"] for p in res["path"]] == ["X1", "X2"]
    assert len(res["gaps"]) == 1
    assert res["gaps"][0]["reason"] == "disconnected"
    assert res["gaps"][0]["start_time"] == "2026-01-01T10:00:10.000Z"
    assert res["gaps"][0]["end_time"] == "2026-01-01T10:00:20.000Z"


def test_global_path_beats_nearest():
    """同一时刻多候选:全局路径优于逐点最近邻。

    轨迹沿 P1/P2 中间线行驶(两者都在 50m 内,且噪声使个别点离 P2 更近),
    随后拐上只有 P1 连通的 P3。匹配结果必须是 P1 -> P3。
    """
    post_json("/api/networks", NET_PARALLEL)
    pts = [
        ("2026-01-01T11:00:00Z", 39.90005, 116.4000),
        ("2026-01-01T11:00:10Z", 39.90007, 116.4003),  # 离 P2 更近(约 3m vs 8m)
        ("2026-01-01T11:00:20Z", 39.90005, 116.4006),
        ("2026-01-01T11:00:30Z", 39.90005, 116.4010),
        ("2026-01-01T11:00:40Z", 39.90050, 116.4010),
        ("2026-01-01T11:00:50Z", 39.90100, 116.4010),
    ]
    traj = post_json("/api/trajectories", make_traj(pts))
    res = post_json("/api/match", {"trajectory_id": traj["trajectory_id"],
                                   "network_version": "parallel"})
    assert [p["segment_id"] for p in res["path"]] == ["P1", "P3"]
    assert res["gaps"] == []


def test_versioning_isolation_and_history():
    """路网修订后:旧分析仍按原版本可查;重新匹配仍可使用旧版本;各分析不混用版本。"""
    traj = post_json("/api/trajectories", make_traj(TRAJ_MAIN, "versioned"))
    tid = traj["trajectory_id"]

    a1 = post_json("/api/match", {"trajectory_id": tid, "network_version": "v1"})
    assert [p["segment_id"] for p in a1["path"]] == ["S1", "S2", "S3"]

    # 路网修订:注册 v2(同名几何,不同路段编号)
    post_json("/api/networks", NET_V2)

    # 旧分析仍可按原版本查询,内容不变
    a1_fetched = client.get(f"/api/analyses/{a1['analysis_id']}").json()
    assert a1_fetched["network_version"] == "v1"
    assert [p["segment_id"] for p in a1_fetched["path"]] == ["S1", "S2", "S3"]

    # 新版本上的分析使用新版本路段
    a2 = post_json("/api/match", {"trajectory_id": tid, "network_version": "v2"})
    assert [p["segment_id"] for p in a2["path"]] == ["R1", "R2", "R3"]

    # 修订后仍可用旧版本重新分析,结果与修订前一致
    a3 = post_json("/api/match", {"trajectory_id": tid, "network_version": "v1"})
    assert [p["segment_id"] for p in a3["path"]] == ["S1", "S2", "S3"]
    assert a3["total_distance_m"] == a1["total_distance_m"]

    # 按版本过滤历史分析
    v1_list = client.get("/api/analyses", params={"network_version": "v1"}).json()["analyses"]
    v2_list = client.get("/api/analyses", params={"network_version": "v2"}).json()["analyses"]
    assert a1["analysis_id"] in [a["analysis_id"] for a in v1_list]
    assert a3["analysis_id"] in [a["analysis_id"] for a in v1_list]
    assert all(a["network_version"] == "v1" for a in v1_list)
    assert [a["analysis_id"] for a in v2_list] == [a2["analysis_id"]]


def test_validation_and_errors():
    # 重复版本 -> 409
    r = client.post("/api/networks", json=NET_V1)
    assert r.status_code == 409
    # 无时区时间 -> 422
    bad = {"points": [{"timestamp": "2026-01-01T08:00:00", "lat": 39.9, "lon": 116.4}]}
    r = client.post("/api/trajectories", json=bad)
    assert r.status_code == 422
    # 非法纬度 -> 422
    bad2 = {"points": [{"timestamp": "2026-01-01T08:00:00Z", "lat": 99.0, "lon": 116.4}]}
    r = client.post("/api/trajectories", json=bad2)
    assert r.status_code == 422
    # 重复路段编号 -> 422
    dup = {"version": "dup", "segments": [NET_V1["segments"][0], NET_V1["segments"][0]]}
    r = client.post("/api/networks", json=dup)
    assert r.status_code == 422
    # 不存在的资源 -> 404
    assert client.post("/api/match", json={"trajectory_id": 99999,
                                           "network_version": "v1"}).status_code == 404
    assert client.post("/api/match", json={"trajectory_id": 1,
                                           "network_version": "nope"}).status_code == 404
    assert client.get("/api/networks/nope").status_code == 404
    assert client.get("/api/analyses/99999").status_code == 404


def test_points_sorted_by_time_and_single_point():
    """乱序提交的点按时间排序;单点轨迹可匹配。"""
    pts = [
        ("2026-01-01T12:00:10Z", 39.9000, 116.4008),
        ("2026-01-01T12:00:00Z", 39.9000, 116.4002),
    ]
    traj = post_json("/api/trajectories", make_traj(pts))
    detail = client.get(f"/api/trajectories/{traj['trajectory_id']}").json()
    assert [p["timestamp"] for p in detail["points"]] == [
        "2026-01-01T12:00:00.000Z", "2026-01-01T12:00:10.000Z"]

    single = post_json("/api/trajectories",
                       make_traj([("2026-01-01T12:30:00Z", 39.9000, 116.4005)]))
    res = post_json("/api/match", {"trajectory_id": single["trajectory_id"],
                                   "network_version": "v1"})
    assert res["matched_points"] == 1
    assert len(res["path"]) == 1
    assert res["path"][0]["segment_id"] == "S1"
    assert res["path"][0]["enter_time"] == res["path"][0]["leave_time"]
    assert res["gaps"] == []
