"""
GET /health — system health check.

Always returns HTTP 200. db_status reflects connectivity, not the HTTP code.
If DB is unavailable, returns {status: "degraded", db_status: "unavailable"}.
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import structlog
from fastapi import APIRouter, Request

from app.db import get_db, get_uptime
from app.models import HealthResponse

router = APIRouter()
logger = structlog.get_logger(__name__)

STALE_FEED_THRESHOLD_MINUTES = 10


@router.get("/health", response_model=HealthResponse)
async def health_check(request: Request) -> HealthResponse:
    trace_id = str(uuid.uuid4())
    start_ms = time.monotonic()
    now = datetime.now(timezone.utc)
    stale_threshold = now - timedelta(minutes=STALE_FEED_THRESHOLD_MINUTES)

    db_status = "connected"
    last_event_per_store: dict[str, Optional[str]] = {}
    stale_feeds: list[str] = []

    try:
        async with get_db() as db:
            async with db.execute(
                """
                SELECT store_id, MAX(timestamp) as last_ts
                FROM events
                GROUP BY store_id
                """
            ) as cur:
                rows = await cur.fetchall()

        for row in rows:
            sid = row["store_id"]
            ts_str = row["last_ts"]
            last_event_per_store[sid] = ts_str

            if ts_str:
                try:
                    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    if ts < stale_threshold:
                        stale_feeds.append(sid)
                except ValueError:
                    stale_feeds.append(sid)
            else:
                stale_feeds.append(sid)

    except Exception as exc:
        db_status = "unavailable"
        logger.warning(
            "health_db_unavailable",
            trace_id=trace_id,
            error=str(exc),
        )

    status = "ok" if (db_status == "connected" and not stale_feeds) else "degraded"

    latency_ms = int((time.monotonic() - start_ms) * 1000)
    logger.info(
        "health_checked",
        trace_id=trace_id,
        endpoint="/health",
        latency_ms=latency_ms,
        status_code=200,
        db_status=db_status,
        status=status,
    )

    return HealthResponse(
        status=status,
        last_event_per_store=last_event_per_store,
        stale_feeds=stale_feeds,
        db_status=db_status,
        uptime_seconds=get_uptime(),
    )
