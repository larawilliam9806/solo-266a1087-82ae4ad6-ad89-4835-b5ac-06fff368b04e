"""SQLite 持久化层。

设计要点:
- 路网按版本注册,版本一旦创建即不可变;修订路网 = 注册新版本。
- 分析结果(匹配路径 + 缺口)按分析时的路网版本落库,
  因此路网修订后,旧分析仍可按原版本查询。
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

DEFAULT_DB_PATH = str(Path(__file__).resolve().parent.parent / "data" / "app.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS networks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    version     TEXT NOT NULL UNIQUE,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS segments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    network_id  INTEGER NOT NULL REFERENCES networks(id),
    segment_id  TEXT NOT NULL,
    start_lat   REAL NOT NULL,
    start_lon   REAL NOT NULL,
    end_lat     REAL NOT NULL,
    end_lon     REAL NOT NULL,
    length_m    REAL NOT NULL,
    UNIQUE (network_id, segment_id)
);

CREATE TABLE IF NOT EXISTS trajectories (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trajectory_points (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    trajectory_id   INTEGER NOT NULL REFERENCES trajectories(id),
    seq             INTEGER NOT NULL,
    ts              TEXT NOT NULL,   -- UTC ISO8601
    lat             REAL NOT NULL,
    lon             REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS analyses (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    trajectory_id    INTEGER NOT NULL REFERENCES trajectories(id),
    network_id       INTEGER NOT NULL REFERENCES networks(id),
    created_at       TEXT NOT NULL,
    total_points     INTEGER NOT NULL,
    matched_points   INTEGER NOT NULL,
    total_distance_m REAL NOT NULL,
    has_detail       INTEGER NOT NULL DEFAULT 0  -- 1 = 裁剪所需明细已随分析保存
);

CREATE TABLE IF NOT EXISTS analysis_segments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    analysis_id INTEGER NOT NULL REFERENCES analyses(id),
    seq         INTEGER NOT NULL,
    segment_id  TEXT NOT NULL,
    enter_time  TEXT NOT NULL,
    leave_time  TEXT NOT NULL,
    distance_m  REAL NOT NULL
);

-- 裁剪所需明细:未按路段合并的细粒度片段,含起止里程与进入/离开时刻。
-- run_seq 标识原行程中的一个连续运行段;缺口处 run_seq 变化,裁剪时据此断开。
-- 旧分析(本表无记录)仍可按原接口查询,但不能裁剪。
CREATE TABLE IF NOT EXISTS analysis_details (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    analysis_id INTEGER NOT NULL REFERENCES analyses(id),
    seq         INTEGER NOT NULL,
    run_seq     INTEGER NOT NULL,
    segment_id  TEXT NOT NULL,
    enter_time  TEXT NOT NULL,
    leave_time  TEXT NOT NULL,
    from_m      REAL NOT NULL,
    to_m        REAL NOT NULL,
    UNIQUE (analysis_id, seq)
);

CREATE TABLE IF NOT EXISTS analysis_gaps (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    analysis_id INTEGER NOT NULL REFERENCES analyses(id),
    seq         INTEGER NOT NULL,
    start_time  TEXT NOT NULL,
    end_time    TEXT NOT NULL,
    reason      TEXT NOT NULL   -- out_of_range | disconnected
);
"""


def db_path() -> str:
    return os.environ.get("DATABASE_PATH", DEFAULT_DB_PATH)


def connect() -> sqlite3.Connection:
    path = db_path()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    conn = connect()
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        # 旧库先补列(新库由 SCHEMA 直接建出该列);旧分析生成于裁剪功能上线前,
        # 没有裁剪明细,has_detail 默认 0,申请裁剪时提示重新分析。
        if _table_exists(conn, "analyses"):
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(analyses)").fetchall()}
            if "has_detail" not in cols:
                conn.execute("ALTER TABLE analyses ADD COLUMN has_detail INTEGER NOT NULL DEFAULT 0")
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone() is not None
