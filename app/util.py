"""时间处理工具:统一使用 UTC ISO8601('Z' 后缀)。"""
from __future__ import annotations

from datetime import datetime, timezone


def to_utc(dt: datetime) -> datetime:
    """归一化为 UTC aware datetime。调用方需保证输入已带时区。"""
    return dt.astimezone(timezone.utc)


def parse_ts(s: str) -> datetime:
    """解析 ISO8601 时间串(支持 'Z' 后缀)为 UTC aware datetime。"""
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return to_utc(dt)


def fmt_ts(dt: datetime) -> str:
    """格式化为 UTC ISO8601 字符串,如 2026-01-01T08:00:00.000Z。"""
    return to_utc(dt).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
