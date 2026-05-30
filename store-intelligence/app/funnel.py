"""
GET /stores/{store_id}/funnel — conversion funnel using the sessions table.

Funnel stages:
  entry_count → zone_visit_count → billing_queue_count → purchase_count

Re-entries: a visitor_id that REENTRY-ed counts as 1 unique visitor.
"""

from __future__ import annotations

import time
import uuid
from datetime import date, datetime, timezone

import structlog
from fastapi import APIRouter, HTTPException, Request

from app.db import get_db
from app.models import FunnelResponse, FunnelStage

router = APIRouter()
logger = structlog.get_logger(__name__)


@router.get("/stores/{store_id}/funnel", response_model=FunnelResponse)
async def get_funnel(store_id: str, request: Request) -> FunnelResponse:
    trace_id = str(uuid.uuid4())
    start_ms = time.monotonic()

    today_start = datetime.combine(date.today(), datetime.min.time()).replace(tzinfo=timezone.utc)
    today_start_str = today_start.isoformat()

    try:
        async with get_db() as db:
            # Stage 1: entry_count — unique customer visitors (ENTRY or REENTRY, deduplicated by visitor_id)
            async with db.execute(
                """
                SELECT COUNT(DISTINCT visitor_id) as cnt
                FROM sessions
                WHERE store_id = ?
                  AND entry_time >= ?
                """,
                (store_id, today_start_str),
            ) as cur:
                row = await cur.fetchone()
                entry_count = row["cnt"] if row else 0

            # Stage 2: zone_visit_count — unique visitor_ids that entered at least one zone
            async with db.execute(
                """
                SELECT COUNT(DISTINCT e.visitor_id) as cnt
                FROM events e
                INNER JOIN sessions s ON s.visitor_id = e.visitor_id AND s.store_id = e.store_id
                WHERE e.store_id = ?
                  AND e.event_type = 'ZONE_ENTER'
                  AND e.is_staff = 0
                  AND e.timestamp >= ?
                """,
                (store_id, today_start_str),
            ) as cur:
                row = await cur.fetchone()
                zone_visit_count = row["cnt"] if row else 0

            # Stage 3: billing_queue_count — unique visitors who joined billing queue
            async with db.execute(
                """
                SELECT COUNT(DISTINCT visitor_id) as cnt
                FROM events
                WHERE store_id = ?
                  AND event_type = 'BILLING_QUEUE_JOIN'
                  AND is_staff = 0
                  AND timestamp >= ?
                """,
                (store_id, today_start_str),
            ) as cur:
                row = await cur.fetchone()
                billing_queue_count = row["cnt"] if row else 0

            # Stage 4: purchase_count — sessions marked converted
            async with db.execute(
                """
                SELECT COUNT(DISTINCT visitor_id) as cnt
                FROM sessions
                WHERE store_id = ?
                  AND entry_time >= ?
                  AND converted = 1
                """,
                (store_id, today_start_str),
            ) as cur:
                row = await cur.fetchone()
                purchase_count = row["cnt"] if row else 0

    except Exception as exc:
        latency_ms = int((time.monotonic() - start_ms) * 1000)
        logger.error(
            "funnel_error",
            trace_id=trace_id,
            store_id=store_id,
            endpoint=f"/stores/{store_id}/funnel",
            latency_ms=latency_ms,
            status_code=503,
            error=str(exc),
        )
        raise HTTPException(status_code=503, detail={"error": "database_unavailable", "retry_after": 30})

    def drop_off(prev: int, curr: int) -> float:
        if prev == 0:
            return 0.0
        return round((prev - curr) / prev * 100, 2)

    stages = [
        FunnelStage(stage="entry_count", count=entry_count, drop_off_pct=None),
        FunnelStage(stage="zone_visit_count", count=zone_visit_count, drop_off_pct=drop_off(entry_count, zone_visit_count)),
        FunnelStage(stage="billing_queue_count", count=billing_queue_count, drop_off_pct=drop_off(zone_visit_count, billing_queue_count)),
        FunnelStage(stage="purchase_count", count=purchase_count, drop_off_pct=drop_off(billing_queue_count, purchase_count)),
    ]

    latency_ms = int((time.monotonic() - start_ms) * 1000)
    logger.info(
        "funnel_served",
        trace_id=trace_id,
        store_id=store_id,
        endpoint=f"/stores/{store_id}/funnel",
        latency_ms=latency_ms,
        status_code=200,
    )

    return FunnelResponse(
        store_id=store_id,
        stages=stages,
        as_of=datetime.now(timezone.utc),
    )
