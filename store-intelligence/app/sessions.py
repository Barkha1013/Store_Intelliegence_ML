"""
Session management helpers — maintain the sessions table from event stream.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone
from typing import Optional

import aiosqlite

from app.models import StoreEvent


def _session_id(store_id: str, visitor_id: str) -> str:
    raw = f"{store_id}:{visitor_id}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


async def upsert_session(db: aiosqlite.Connection, event: StoreEvent) -> None:
    """
    Maintain the sessions table based on incoming events.

    - ENTRY / REENTRY → create or reopen session
    - EXIT           → close session (set exit_time)
    - Others         → no session change
    """
    if event.event_type not in ("ENTRY", "REENTRY", "EXIT"):
        return

    session_id = _session_id(event.store_id, event.visitor_id)
    ts = event.timestamp
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    ts_str = ts.isoformat()

    if event.event_type in ("ENTRY", "REENTRY"):
        await db.execute(
            """
            INSERT INTO sessions (session_id, store_id, visitor_id, entry_time, converted, basket_value_inr)
            VALUES (?, ?, ?, ?, 0, NULL)
            ON CONFLICT(session_id) DO UPDATE SET
                entry_time = excluded.entry_time,
                exit_time  = NULL,
                converted  = 0
            """,
            (session_id, event.store_id, event.visitor_id, ts_str),
        )
    elif event.event_type == "EXIT":
        await db.execute(
            """
            UPDATE sessions SET exit_time = ?
            WHERE session_id = ? AND exit_time IS NULL
            """,
            (ts_str, session_id),
        )


async def mark_conversion(
    db: aiosqlite.Connection,
    store_id: str,
    visitor_id: str,
    basket_value_inr: float,
) -> None:
    session_id = _session_id(store_id, visitor_id)
    await db.execute(
        """
        UPDATE sessions SET converted = 1, basket_value_inr = ?
        WHERE session_id = ?
        """,
        (basket_value_inr, session_id),
    )
