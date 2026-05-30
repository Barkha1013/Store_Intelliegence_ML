"""
Database setup and connection management using aiosqlite.
"""

from __future__ import annotations

import os
import time
from contextlib import asynccontextmanager
from typing import AsyncGenerator

import aiosqlite

_start_time = time.time()


def get_db_path() -> str:
    """Read DB_PATH dynamically so test fixtures can override os.environ at runtime."""
    return os.environ.get("DB_PATH", "/data/store_intelligence.db")


def get_uptime() -> float:
    return time.time() - _start_time


CREATE_EVENTS_TABLE = """
CREATE TABLE IF NOT EXISTS events (
    event_id     TEXT PRIMARY KEY,
    store_id     TEXT NOT NULL,
    camera_id    TEXT NOT NULL,
    visitor_id   TEXT NOT NULL,
    event_type   TEXT NOT NULL,
    timestamp    TEXT NOT NULL,
    zone_id      TEXT,
    dwell_ms     INTEGER,
    is_staff     INTEGER NOT NULL DEFAULT 0,
    confidence   REAL NOT NULL,
    metadata     TEXT,
    ingested_at  TEXT NOT NULL
);
"""

CREATE_SESSIONS_TABLE = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id       TEXT PRIMARY KEY,
    store_id         TEXT NOT NULL,
    visitor_id       TEXT NOT NULL,
    entry_time       TEXT NOT NULL,
    exit_time        TEXT,
    converted        INTEGER NOT NULL DEFAULT 0,
    basket_value_inr REAL
);
"""

CREATE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_events_store_ts ON events(store_id, timestamp);",
    "CREATE INDEX IF NOT EXISTS idx_events_visitor ON events(visitor_id);",
    "CREATE INDEX IF NOT EXISTS idx_events_camera ON events(camera_id);",
    "CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type);",
    "CREATE INDEX IF NOT EXISTS idx_sessions_store ON sessions(store_id);",
    "CREATE INDEX IF NOT EXISTS idx_sessions_visitor ON sessions(visitor_id);",
]


async def init_db(db: aiosqlite.Connection) -> None:
    await db.execute(CREATE_EVENTS_TABLE)
    await db.execute(CREATE_SESSIONS_TABLE)
    for idx in CREATE_INDEXES:
        await db.execute(idx)
    await db.commit()


@asynccontextmanager
async def get_db() -> AsyncGenerator[aiosqlite.Connection, None]:
    db_path = get_db_path()
    parent = os.path.dirname(db_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA journal_mode=WAL;")
        await db.execute("PRAGMA foreign_keys=ON;")
        await init_db(db)
        yield db
