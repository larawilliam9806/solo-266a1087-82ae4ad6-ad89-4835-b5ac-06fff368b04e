"""请求体的 Pydantic 模型与校验。"""
from __future__ import annotations

from datetime import datetime, timezone

from pydantic import AliasChoices, BaseModel, Field, field_validator


class SegmentIn(BaseModel):
    segment_id: str = Field(min_length=1, max_length=128, description="路段编号")
    start_lat: float = Field(ge=-90, le=90)
    start_lon: float = Field(ge=-180, le=180)
    end_lat: float = Field(ge=-90, le=90)
    end_lon: float = Field(ge=-180, le=180)
    length_m: float = Field(gt=0, description="路段长度(米)")


class NetworkCreate(BaseModel):
    version: str = Field(min_length=1, max_length=64, description="路网版本号,全局唯一")
    segments: list[SegmentIn] = Field(min_length=1)

    @field_validator("segments")
    @classmethod
    def unique_segment_ids(cls, v: list[SegmentIn]) -> list[SegmentIn]:
        ids = [s.segment_id for s in v]
        if len(ids) != len(set(ids)):
            raise ValueError("同一版本内 segment_id 不能重复")
        return v


class PointIn(BaseModel):
    timestamp: datetime = Field(description="UTC 时刻,ISO8601,必须带时区(如 2026-01-01T08:00:00Z)")
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)

    @field_validator("timestamp")
    @classmethod
    def must_be_tz_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None or v.utcoffset() is None:
            raise ValueError("timestamp 必须携带时区(UTC 时刻,如 2026-01-01T08:00:00Z)")
        return v.astimezone(timezone.utc)


class TrajectoryCreate(BaseModel):
    name: str | None = Field(default=None, max_length=128)
    points: list[PointIn] = Field(min_length=1)


class MatchRequest(BaseModel):
    trajectory_id: int
    network_version: str = Field(min_length=1, description="指定匹配使用的路网版本;一次分析只使用该版本快照")


class ClipRange(BaseModel):
    """按分析所用路网版本填写的路段里程范围(从路段起点计,单位米)。"""
    segment_id: str = Field(min_length=1, max_length=128, description="有向路段编号")
    start_m: float = Field(
        ge=0, description="范围起点里程(距路段起点,米)",
        validation_alias=AliasChoices("start_m", "from_m", "start_offset_m", "from_offset_m"))
    end_m: float = Field(
        ge=0, description="范围终点里程(距路段起点,米)",
        validation_alias=AliasChoices("end_m", "to_m", "end_offset_m", "to_offset_m"))


class ClipRequest(BaseModel):
    analysis_id: int | None = Field(default=None, description="要裁剪的分析编号(走路径/查询参数时可省略)")
    ranges: list[ClipRange] = Field(min_length=1, description="按分析路网版本填写的路段里程范围",
                                    validation_alias=AliasChoices("ranges", "segments"))
