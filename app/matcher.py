"""地图匹配引擎:HMM + Viterbi 全局最优路径。

匹配流程:
1. 候选生成:每个 GPS 点吸附到 50m 以内的有向路段(投影点)。
2. 连通性:候选对之间的路径距离 = 沿当前路段到终点 + 有向图最短路径 + 目标路段偏移。
3. 全局最优:发射概率用点到路段距离的高斯似然,转移概率用
   |路网路径距离 - 两点直线距离| 的指数衰减,Viterbi 求全局最优,
   从而在同一时刻存在多个候选时仍保持全局连续路径。
4. 缺口:无候选(超距 50m)或路网不连通的区间标记为 gap。

NetworkIndex 是单个路网版本的不可变快照;一次匹配只使用一个版本,
不会在分析期间混用版本。
"""
from __future__ import annotations

import heapq
import math
from dataclasses import dataclass
from datetime import datetime, timedelta

from .geo import haversine_m, project_to_segment
from .util import fmt_ts

MATCH_RADIUS_M = 50.0        # 候选吸附半径
EMISSION_SIGMA_M = 15.0      # 发射概率高斯标准差
TRANSITION_BETA_M = 30.0     # 转移概率指数衰减尺度
MAX_CANDIDATES_PER_POINT = 8
NEG_INF = float("-inf")

GAP_OUT_OF_RANGE = "out_of_range"   # 距所有路段超过 50m,无候选
GAP_DISCONNECTED = "disconnected"   # 有候选但路网不连通,无法生成连续路径


def node_key(lat: float, lon: float) -> tuple[float, float]:
    """端点坐标(约 0.1m 精度)作为图节点键:坐标一致(1e-6 度内)的端点互相连通。"""
    return (round(lat, 6), round(lon, 6))


@dataclass
class Segment:
    segment_id: str
    start_lat: float
    start_lon: float
    end_lat: float
    end_lon: float
    length_m: float

    @property
    def start_node(self) -> tuple[float, float]:
        return node_key(self.start_lat, self.start_lon)

    @property
    def end_node(self) -> tuple[float, float]:
        return node_key(self.end_lat, self.end_lon)


@dataclass
class Candidate:
    seg_idx: int      # 路段下标
    offset_m: float   # 投影点距路段起点的里程(按声明长度缩放)
    dist_m: float     # GPS 点到投影点的距离
    proj_lat: float
    proj_lon: float


@dataclass
class TrackPoint:
    ts: datetime
    lat: float
    lon: float


class NetworkIndex:
    """单个路网版本的不可变快照:路段表 + 有向图 + 最短路缓存。"""

    def __init__(self, segments: list[Segment]):
        self.segments = segments
        # 有向图邻接表:node -> [(to_node, length_m, seg_idx)]
        self.adj: dict[tuple[float, float], list[tuple[tuple[float, float], float, int]]] = {}
        for i, s in enumerate(segments):
            self.adj.setdefault(s.start_node, []).append((s.end_node, s.length_m, i))
        self._sp_cache: dict[tuple[float, float], tuple[dict, dict]] = {}
        # 候选预过滤包围盒
        self._bbox = [
            (min(s.start_lat, s.end_lat), min(s.start_lon, s.end_lon),
             max(s.start_lat, s.end_lat), max(s.start_lon, s.end_lon))
            for s in segments
        ]

    # ------------------------------------------------------------------ 候选
    def candidates(self, lat: float, lon: float, radius: float = MATCH_RADIUS_M) -> list[Candidate]:
        dlat = radius / 111320.0
        dlon = radius / max(1e-6, 111320.0 * math.cos(math.radians(lat)))
        out: list[Candidate] = []
        for i, s in enumerate(self.segments):
            min_la, min_lo, max_la, max_lo = self._bbox[i]
            if lat < min_la - dlat or lat > max_la + dlat or lon < min_lo - dlon or lon > max_lo + dlon:
                continue
            dist, frac, plat, plon = project_to_segment(
                lat, lon, s.start_lat, s.start_lon, s.end_lat, s.end_lon
            )
            if dist <= radius:
                out.append(Candidate(i, frac * s.length_m, dist, plat, plon))
        out.sort(key=lambda c: c.dist_m)
        return out[:MAX_CANDIDATES_PER_POINT]

    # --------------------------------------------------------------- 有向图
    def shortest_from(self, src: tuple[float, float]) -> tuple[dict, dict]:
        """Dijkstra:返回 (dist, prev)。prev[node] = (前驱 node, 经过的 seg_idx)。按源点缓存。"""
        cached = self._sp_cache.get(src)
        if cached is not None:
            return cached
        dist: dict[tuple[float, float], float] = {src: 0.0}
        prev: dict[tuple[float, float], tuple[tuple[float, float], int]] = {}
        pq: list[tuple[float, tuple[float, float]]] = [(0.0, src)]
        while pq:
            d, u = heapq.heappop(pq)
            if d > dist.get(u, math.inf):
                continue
            for v, w, si in self.adj.get(u, ()):
                nd = d + w
                if nd < dist.get(v, math.inf):
                    dist[v] = nd
                    prev[v] = (u, si)
                    heapq.heappush(pq, (nd, v))
        self._sp_cache[src] = (dist, prev)
        return dist, prev

    def connector(self, a: Candidate, b: Candidate) -> tuple[float, list[tuple[int, float, float]]] | None:
        """候选 a -> b 沿有向路网的最短连续路径。

        返回 (总里程, [(seg_idx, from_offset, to_offset), ...]);不连通返回 None。
        """
        sa = self.segments[a.seg_idx]
        sb = self.segments[b.seg_idx]
        if a.seg_idx == b.seg_idx and b.offset_m >= a.offset_m - 1e-9:
            # 同一条有向路段且顺向
            return b.offset_m - a.offset_m, [(a.seg_idx, a.offset_m, b.offset_m)]
        dist, prev = self.shortest_from(sa.end_node)
        if sb.start_node not in dist:
            return None
        total = (sa.length_m - a.offset_m) + dist[sb.start_node] + b.offset_m
        pieces: list[tuple[int, float, float]] = [(a.seg_idx, a.offset_m, sa.length_m)]
        # 重建最短路经过的完整路段
        node = sb.start_node
        middle: list[int] = []
        while node != sa.end_node:
            pnode, si = prev[node]
            middle.append(si)
            node = pnode
        middle.reverse()
        for si in middle:
            pieces.append((si, 0.0, self.segments[si].length_m))
        pieces.append((b.seg_idx, 0.0, b.offset_m))
        return total, pieces

    # --------------------------------------------------------------- Viterbi
    def _viterbi(self, pts: list[TrackPoint], cands: list[list[Candidate]]):
        """返回 (最优候选下标序列, None);若第 k 列整体不可达,返回 (None, k)。"""
        n = len(pts)
        emits = [[-0.5 * (c.dist_m / EMISSION_SIGMA_M) ** 2 for c in cl] for cl in cands]
        dp = list(emits[0])
        backs: list[list[int]] = []
        for k in range(1, n):
            linear = haversine_m(pts[k - 1].lat, pts[k - 1].lon, pts[k].lat, pts[k].lon)
            new = [NEG_INF] * len(cands[k])
            back = [-1] * len(cands[k])
            for j, cb in enumerate(cands[k]):
                best_lp, best_i = NEG_INF, -1
                for i, ca in enumerate(cands[k - 1]):
                    if dp[i] == NEG_INF:
                        continue
                    route = self.connector(ca, cb)
                    if route is None:
                        continue
                    lp = dp[i] - abs(route[0] - linear) / TRANSITION_BETA_M
                    if lp > best_lp:
                        best_lp, best_i = lp, i
                if best_i >= 0:
                    new[j] = best_lp + emits[k][j]
                    back[j] = best_i
            if all(v == NEG_INF for v in new):
                return None, k
            backs.append(back)
            dp = new
        j = max(range(len(dp)), key=lambda x: dp[x])
        path = [j]
        for back in reversed(backs):
            j = back[j]
            path.append(j)
        path.reverse()
        return path, None

    def _match_run(self, points, idxs, cands, pieces):
        """对一段连续有候选的点列做 Viterbi;不连通则断开并递归(断开处记为缺口)。"""
        pts = [points[i] for i in idxs]
        cl = [cands[i] for i in idxs]
        path, fail_col = self._viterbi(pts, cl)
        if path is not None:
            pieces.append((idxs, path))
            return
        # fail_col 列整体不可达:在 fail_col-1 与 fail_col 之间断开
        self._match_run(points, idxs[:fail_col], cands, pieces)
        self._match_run(points, idxs[fail_col:], cands, pieces)

    # --------------------------------------------------------------- 结果组装
    def _build_visits(self, points, idxs, path, cands):
        """把一段匹配结果展开为连续路段序列,并按里程比例插值进入/离开时刻。"""
        pts = [points[i] for i in idxs]
        chosen = [cands[i][p] for i, p in zip(idxs, path)]
        raw: list[tuple[int, datetime, datetime, float]] = []
        if len(chosen) == 1:
            c = chosen[0]
            raw.append((c.seg_idx, pts[0].ts, pts[0].ts, 0.0))
        for k in range(len(chosen) - 1):
            a, b = chosen[k], chosen[k + 1]
            route = self.connector(a, b)
            if route is None:  # 理论上不会发生(Viterbi 已保证连通),防御性跳过
                continue
            _, parts = route
            t0, t1 = pts[k].ts, pts[k + 1].ts
            span = (t1 - t0).total_seconds()
            dists = [max(0.0, to - frm) for _, frm, to in parts]
            total = sum(dists)
            acc, tcur = 0.0, t0
            for (si, frm, to), dd in zip(parts, dists):
                frac = (acc + dd) / total if total > 0 else 0.0
                tn = t0 + timedelta(seconds=span * frac)
                raw.append((si, tcur, tn, dd))
                tcur, acc = tn, acc + dd
        # 丢弃零长度片段(整段都为零时保留一条),再合并相邻同路段
        nonzero = [r for r in raw if r[3] > 1e-6]
        if nonzero:
            raw = nonzero
        elif not raw:
            return []
        merged: list[list] = []
        for si, te, tl, dd in raw:
            if merged and merged[-1][0] == si:
                merged[-1][2] = tl
                merged[-1][3] += dd
            else:
                merged.append([si, te, tl, dd])
        return [tuple(m) for m in merged]

    def match(self, points: list[TrackPoint]) -> dict:
        """对按时间升序的轨迹点列执行匹配,返回路径、缺口与统计。"""
        n = len(points)
        cands = [self.candidates(p.lat, p.lon) for p in points]

        # 1) 按"有候选"切成若干 run,run 内再按连通性切分
        pieces: list[tuple[list[int], list[int]]] = []  # (点下标列表, 候选下标序列)
        i = 0
        while i < n:
            if not cands[i]:
                i += 1
                continue
            j = i
            while j + 1 < n and cands[j + 1]:
                j += 1
            self._match_run(points, list(range(i, j + 1)), cands, pieces)
            i = j + 1

        # 2) 展开为连续路径
        visits: list[tuple] = []
        ranges: list[tuple[int, int]] = []
        for idxs, path in pieces:
            v = self._build_visits(points, idxs, path, cands)
            if v:
                visits.extend(v)
                ranges.append((idxs[0], idxs[-1]))

        # 3) 缺口
        gaps: list[dict] = []
        if not ranges:
            gaps.append({"start_time": fmt_ts(points[0].ts), "end_time": fmt_ts(points[-1].ts),
                         "reason": GAP_OUT_OF_RANGE})
        else:
            if ranges[0][0] > 0:
                gaps.append({"start_time": fmt_ts(points[0].ts),
                             "end_time": fmt_ts(points[ranges[0][0]].ts),
                             "reason": GAP_OUT_OF_RANGE})
            for g in range(len(ranges) - 1):
                p_end, q_start = ranges[g][1], ranges[g + 1][0]
                reason = GAP_DISCONNECTED if q_start == p_end + 1 else GAP_OUT_OF_RANGE
                gaps.append({"start_time": fmt_ts(points[p_end].ts),
                             "end_time": fmt_ts(points[q_start].ts),
                             "reason": reason})
            if ranges[-1][1] < n - 1:
                gaps.append({"start_time": fmt_ts(points[ranges[-1][1]].ts),
                             "end_time": fmt_ts(points[-1].ts),
                             "reason": GAP_OUT_OF_RANGE})

        path_json = [
            {"seq": k,
             "segment_id": self.segments[si].segment_id,
             "enter_time": fmt_ts(te),
             "leave_time": fmt_ts(tl),
             "distance_m": round(dd, 3)}
            for k, (si, te, tl, dd) in enumerate(visits)
        ]
        for k, g in enumerate(gaps):
            g["seq"] = k

        return {
            "total_points": n,
            "matched_points": sum(len(idxs) for idxs, _ in pieces),
            "total_distance_m": round(sum(v[3] for v in visits), 3),
            "path": path_json,
            "gaps": gaps,
        }
