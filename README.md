# 车辆轨迹匹配 API

基于 **FastAPI + SQLite** 的地图匹配(Map Matching)服务:登记带版本的有向路网,提交带 UTC 时刻的轨迹,将轨迹点吸附到 50 米内的候选路段,结合路网连通性用 HMM + Viterbi 生成全局最优的连续路径,无法连通或超距的区间标记为缺口。

## 快速启动

### Docker Compose(推荐)

```bash
docker compose up --build
```

服务监听 `http://localhost:8000`,SQLite 数据持久化在命名卷 `traj-data`(`/data/app.db`)。交互式文档见 `http://localhost:8000/docs`。

### 本地开发

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/uvicorn app.main:app --reload
```

运行测试:

```bash
.venv/bin/python -m pytest tests/ -q
```

## 匹配算法

1. **候选生成**:每个 GPS 点投影到 50 米(`MATCH_RADIUS_M`)以内的有向路段,取投影点作为吸附位置(每点最多保留 8 个最近候选)。
2. **连通性**:候选 a → b 的路径距离 = a 沿路段到终点 + 有向图最短路径(Dijkstra,按源点缓存)+ b 在目标路段上的偏移。端点坐标一致(1e-6 度内)即视为同一图节点;方向不可逆行。
3. **全局最优(HMM + Viterbi)**:
   - 发射概率:点到路段距离的高斯似然(σ = 15m);
   - 转移概率:`exp(-|路网路径距离 − 两点球面直线距离| / 30m)`,不连通则禁止。
   - 同一时刻存在多个候选时,取全局概率最大的路径而非逐点最近邻,保证路径连续。
4. **连续路径展开**:相邻匹配点之间按最短路径补全中间路段,生成完整连续路径;进入/离开时刻按里程比例在相邻采样点时刻间插值。
5. **缺口**:
   - `out_of_range` — 点距所有路段超过 50 米,无候选;
   - `disconnected` — 前后点均有候选,但有向路网不连通,无法生成连续路径。

## 版本语义

- 路网按版本登记,版本号全局唯一,登记后**不可变**;修订路网 = 登记新版本。
- 每次匹配显式指定 `network_version`,分析全程只读取该版本的快照,**不混用版本**;分析期间登记新版本不影响进行中的匹配。
- 分析结果连同版本号一起落库,路网修订后旧分析仍可按原版本查询(`GET /api/analyses?network_version=...`)。

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/networks` | 登记带版本的有向路网(路段编号、起终点坐标、长度) |
| GET | `/api/networks` | 列出路网版本 |
| GET | `/api/networks/{version}` | 查看某版本路网(含全部路段) |
| POST | `/api/trajectories` | 提交轨迹(UTC 时刻 + 坐标,按时间排序存储) |
| GET | `/api/trajectories/{id}` | 查看轨迹 |
| POST | `/api/match` | 在指定版本上匹配,返回匹配路段、进入/离开时刻、里程、缺口 |
| GET | `/api/analyses` | 查询历史分析(可按 `network_version`、`trajectory_id` 过滤) |
| GET | `/api/analyses/{id}` | 查看分析结果 |
| POST | `/api/clip` | 按分析的**路网版本**填写路段里程范围裁剪行程(见下) |
| POST | `/api/analyses/{id}/clip` | 同上,分析编号放在路径中 |
| GET | `/health` | 健康检查 |

### 示例

```bash
# 1. 登记路网 v1
curl -X POST localhost:8000/api/networks -H 'Content-Type: application/json' -d '{
  "version": "v1",
  "segments": [
    {"segment_id": "S1", "start_lat": 39.900, "start_lon": 116.400,
     "end_lat": 39.900, "end_lon": 116.401, "length_m": 85.0},
    {"segment_id": "S2", "start_lat": 39.900, "start_lon": 116.401,
     "end_lat": 39.901, "end_lon": 116.401, "length_m": 111.0}
  ]
}'

# 2. 提交轨迹(时刻必须为带时区的 UTC ISO8601)
curl -X POST localhost:8000/api/trajectories -H 'Content-Type: application/json' -d '{
  "name": "trip-1",
  "points": [
    {"timestamp": "2026-01-01T08:00:00Z", "lat": 39.9000, "lon": 116.4000},
    {"timestamp": "2026-01-01T08:00:10Z", "lat": 39.9000, "lon": 116.4008},
    {"timestamp": "2026-01-01T08:00:20Z", "lat": 39.9008, "lon": 116.4010}
  ]
}'

# 3. 匹配(轨迹 1,路网版本 v1)
curl -X POST localhost:8000/api/match -H 'Content-Type: application/json' \
  -d '{"trajectory_id": 1, "network_version": "v1"}'
```

响应示例:

```json
{
  "analysis_id": 1,
  "trajectory_id": 1,
  "network_version": "v1",
  "total_points": 7,
  "matched_points": 6,
  "total_distance_m": 183.0,
  "path": [
    {"seq": 0, "segment_id": "S1", "enter_time": "2026-01-01T08:00:00.000Z",
     "leave_time": "2026-01-01T08:00:20.000Z", "distance_m": 85.0}
  ],
  "gaps": [
    {"seq": 0, "start_time": "2026-01-01T08:00:30.000Z",
     "end_time": "2026-01-01T08:00:50.000Z", "reason": "out_of_range"}
  ]
}
```

## 轨迹裁剪

养护单位向承包方交付巡检数据时,往往只提供合同范围内的行程。裁剪 API 以一次**已有分析**为基础,
按该分析所用路网版本的有向路段编号与里程范围,从原行程中截取范围内的部分。

```bash
# 裁剪分析 1:S1 的 20–60 米 + S2 的 0–80 米(S1 的范围可重复填写,自动取并集)
curl -X POST localhost:8000/api/clip -H 'Content-Type: application/json' -d '{
  "analysis_id": 1,
  "ranges": [
    {"segment_id": "S1", "start_m": 20, "end_m": 60},
    {"segment_id": "S2", "start_m": 0,  "end_m": 80}
  ]
}'
```

```json
{
  "analysis_id": 1,
  "trajectory_id": 1,
  "network_version": "v1",
  "piece_count": 2,
  "distance_m": 120.0,
  "pieces": [
    {"seq": 0, "enter_time": "2026-01-01T08:00:04.705Z",
     "leave_time": "2026-01-01T08:00:14.117Z", "distance_m": 40.0,
     "segments": [
       {"segment_id": "S1", "start_m": 20.0, "end_m": 60.0,
        "from_m": 20.0, "to_m": 60.0, "distance_m": 40.0,
        "enter_time": "2026-01-01T08:00:04.705Z",
        "leave_time": "2026-01-01T08:00:14.117Z"}
     ]},
    {"seq": 1, "enter_time": "2026-01-01T08:00:20.000Z",
     "leave_time": "2026-01-01T08:00:34.414Z", "distance_m": 80.0,
     "segments": [
       {"segment_id": "S2", "start_m": 0.0, "end_m": 80.0,
        "from_m": 0.0, "to_m": 80.0, "distance_m": 80.0,
        "enter_time": "2026-01-01T08:00:20.000Z",
        "leave_time": "2026-01-01T08:00:34.414Z"}
     ]}
  ],
  "segments": [
    {"piece": 0, "seq": 0, "segment_id": "S1", "start_m": 20.0, "end_m": 60.0,
     "distance_m": 40.0, "enter_time": "2026-01-01T08:00:04.705Z",
     "leave_time": "2026-01-01T08:00:14.117Z"},
    {"piece": 1, "seq": 1, "segment_id": "S2", "start_m": 0.0, "end_m": 80.0,
     "distance_m": 80.0, "enter_time": "2026-01-01T08:00:20.000Z",
     "leave_time": "2026-01-01T08:00:34.414Z"}
  ]
}
```

上例中 S1 只取到 60 米即驶出范围,故 S1 的后半段与 S2 分成两个片段。

规则:

- **范围**按分析所用路网版本填写:有向路段编号 + 距**路段起点**的起止里程(米),
  起止里程须递增且落在 `[0, 路段长度]` 内;路段不存在、里程非法或越界时**整次请求被拒绝**
  (422),不会产出部分裁剪结果。
- **同一路段的多个范围取并集**(重叠或相接的区间合并);不同路段的范围各自独立。
- **连续片段**:结果按原行程顺序给出;同一片段内的路段首尾相接,片段之间因
  **驶出范围**或**原有缺口**而断开。`pieces` 为分组视图,`segments` 为同一内容的扁平视图。
- **时刻**:裁剪端点的进入/离开时刻,按原分析中相邻采样点之间的**行驶里程比例插值**,
  与原分析的时间口径一致。
- **静止记录**不单独成段;若车辆在范围内停留,停留时间计入所在片段的离开时刻。
- **无交集**时返回空列表(`piece_count: 0`)——这是正常结果,不是错误。
- 请求体也可直接提交范围列表,此时分析编号通过路径或 `?analysis_id=` 给出;
  `start_m`/`end_m` 亦可写作 `from_m`/`to_m`。

### 裁剪明细与持久化

- 匹配时除路径与缺口外,还会把**裁剪所需明细**(未按路段合并的细粒度片段:路段编号、
  起止里程、进出时刻,以及标识连续运行段的 `run_seq`)与分析写在**同一事务**内落库
  (`analysis_details` 表)。明细写入失败则整次分析都不生成,便于整体重试。
- 明细随分析持久化,服务重启后**可直接裁剪,无需重做匹配**。
- **旧分析**(本功能上线前生成、没有明细)仍可按原接口 `GET /api/analyses/{id}` 查询;
  对其申请裁剪会返回 **409**,提示重新分析。旧库在启动时自动补列建表,无需手工迁移。

## 项目结构```
app/
  main.py      FastAPI 路由与持久化
  matcher.py   HMM + Viterbi 匹配引擎(候选、有向图最短路、缺口检测、裁剪明细)
  clip.py      轨迹裁剪(范围并集、里程插值、驶出范围/缺口断开)
  geo.py       球面距离、点到线段投影
  db.py        SQLite 连接与建表
  schemas.py   请求校验(Pydantic)
  util.py      UTC 时间处理
tests/test_api.py   端到端测试(连续路径、两类缺口、多候选全局最优、版本隔离)
tests/test_clip.py  裁剪测试(范围校验、并集、插值、静止记录、缺口/驶出断开、持久化、旧分析)
Dockerfile / docker-compose.yml
```

## 配置

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE_PATH` | `./data/app.db`(容器内 `/data/app.db`) | SQLite 数据库文件路径 |

匹配参数(`app/matcher.py` 顶部常量):吸附半径 50m、发射 σ 15m、转移衰减 β 30m、每点最大候选数 8。
