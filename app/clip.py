"""轨迹裁剪:按分析明细在路段里程范围内裁剪行程。

输入是一次分析落库的细粒度明细(未按路段合并的连续片段,含起止里程与时刻),
以及按该分析所用路网版本填写的路段里程范围。输出按原行程顺序排列的连续片段:

- 同一路段的多个范围先取并集;
- 只保留范围内的行程,裁剪端点的时刻按原分析中相邻采样点间的行驶里程插值;
- 驶出范围、或遇到原分析中的缺口(run 变化)即断开为新的片段;
- 静止记录(里程不增加的明细)本身不单独成段,但与前后行驶连续时并入同一片段,
  停留在范围内的时间也计入该片段的进出时刻;
- 没有任何交集时返回空列表。

明细行字段: ``segment_id`` / ``enter_time`` / ``leave_time`` / ``from_m`` /
``to_m`` / ``run_seq``;``run_seq`` 标识原行程中的一个连续运行段(缺口处变化)。
"""
from __future__ import annotations

from datetime import datetime, timedelta

EPS = 1e-6          # 里程容差(米),用于判断两个里程是否相等
CONTIG_EPS = 0.01   # 相邻片段视为首尾相接的里程容差(米;明细里程落库保留 3 位小数)
BACKTRACK_M = 1.0   # 同一路段内允许的微小回退(米),仍并入同一条


def merge_intervals(intervals: list[tuple[float, float]]) -> list[list[float]]:
    """同一路段的范围取并集(重叠或首尾相接的区间合并为一段)。"""
    merged: list[list[float]] = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1] + EPS:
            if end > merged[-1][1]:
                merged[-1][1] = end
        else:
            merged.append([start, end])
    return merged


def _intersections(frm: float, to: float,
                   intervals: list[list[float]]) -> list[tuple[float, float]]:
    """明细片段 [frm, to] 与范围内各区间按里程升序求交。"""
    out: list[tuple[float, float]] = []
    for start, end in intervals:
        lo, hi = max(start, frm), min(end, to)
        if hi - lo > EPS:
            out.append((lo, hi))
    return out


def _in_range(offset: float, intervals: list[list[float]]) -> bool:
    return any(start - EPS <= offset <= end + EPS for start, end in intervals)


def _interp(t0: datetime, t1: datetime, frac: float) -> datetime:
    """按里程比例在相邻采样点时刻之间插值。"""
    if frac <= 0.0 or t1 <= t0:
        return t0
    if frac >= 1.0:
        return t1
    return t0 + timedelta(seconds=(t1 - t0).total_seconds() * frac)


def clip_analysis(rows: list[dict],
                  ranges_by_segment: dict[str, list[list[float]]]) -> list[dict]:
    """裁剪明细行,返回连续片段列表。

    ``rows`` 必须按分析内顺序排列;每行含 segment_id、enter_time、leave_time、
    from_m、to_m、run_seq(时刻可为 datetime 或 ISO8601 字符串)。返回的每个片段含
    ``seq`` 与按行驶顺序排列的 ``segments``。
    """
    pieces: list[dict] = []
    current: dict | None = None
    prev_run: int | None = None
    prev_in_range = False   # 上一条明细是否一直处于范围内(驶到终点/停留在范围内)

    for row in rows:
        segment_id = row["segment_id"]
        frm, to = float(row["from_m"]), float(row["to_m"])
        t0, t1 = _as_dt(row["enter_time"]), _as_dt(row["leave_time"])
        run_seq = row.get("run_seq")
        intervals = ranges_by_segment.get(segment_id) or []
        continues = (
            current is not None
            and prev_run == run_seq
            and prev_in_range
        )

        if to - frm <= EPS:
            # 静止记录:不产生独立片段,也不打断连续性。
            if not _in_range(frm, intervals):
                prev_in_range = False  # 停在范围外,后续行程不再与之前连续
                continue
            segs = current["segments"] if continues else None
            if segs:
                # 与前后行驶连续:仅把停留时间并入片段的离开时刻
                if t1 > segs[-1]["leave_time"]:
                    segs[-1]["leave_time"] = t1
            else:
                # 尚未成段(如轨迹开头先静止,或刚从范围外驶入):先记一条零长度片段,
                # 随后的行驶片段会与之合并;若始终未行驶则最终丢弃。
                current = {"seq": len(pieces), "segments": [_new_seg(segment_id, frm, frm, t0, t1)]}
                pieces.append(current)
            prev_in_range = True
            prev_run = run_seq
            continue

        span = to - frm
        portions = _intersections(frm, to, intervals)
        segs = current["segments"] if continues else None
        for j, (a, b) in enumerate(portions):
            enter = _interp(t0, t1, (a - frm) / span)
            leave = _interp(t0, t1, (b - frm) / span)
            # 本行首段且与上一条连续(上一行驶到终点、本行从起点就在范围内)时续用当前片段;
            # 其余情况说明中途驶出过范围,另起一段连续片段。
            if not (j == 0 and continues and abs(a - frm) <= CONTIG_EPS):
                current = {"seq": len(pieces), "segments": []}
                pieces.append(current)
                segs = current["segments"]
            if (segs and segs[-1]["segment_id"] == segment_id
                    and a <= segs[-1]["end_m"] + BACKTRACK_M):
                # 同一片段内相邻的同一路段:合并为一条(路网展开的续段、裁剪造成的截断)
                _extend(segs[-1], b, leave)
            else:
                segs.append(_new_seg(segment_id, a, b, enter, leave))

        if not portions and continues and segs:
            # 车辆在范围终点停留后驶出:驶出瞬间(t0 位于该明细起点)仍属范围内,
            # 把这段停留时间并入片段,离开时刻取该明细的起点时刻。
            if _in_range(frm, intervals) and abs(segs[-1]["end_m"] - frm) <= CONTIG_EPS:
                _extend(segs[-1], frm, t0)

        prev_in_range = bool(portions) and abs(portions[-1][1] - to) <= CONTIG_EPS
        prev_run = run_seq

    # 丢弃零长度的路段与片段(纯静止记录不单独成段),并按顺序重新编号
    out: list[dict] = []
    for p in pieces:
        segs = [s for s in p["segments"] if s["end_m"] - s["start_m"] > EPS]
        if segs:
            out.append({"seq": len(out), "segments": segs})
    return out


def _new_seg(segment_id: str, start_m: float, end_m: float,
             enter: datetime, leave: datetime) -> dict:
    return {"segment_id": segment_id, "start_m": start_m, "end_m": end_m,
            "enter_time": enter, "leave_time": leave}


def _extend(seg: dict, end_m: float, leave: datetime) -> None:
    """延长路段片段:终点里程取较远者,离开时刻取较晚者。"""
    if end_m > seg["end_m"]:
        seg["end_m"] = end_m
    if leave > seg["leave_time"]:
        seg["leave_time"] = leave


def _as_dt(value) -> datetime:
    if isinstance(value, datetime):
        return value
    from .util import parse_ts
    return parse_ts(value)
