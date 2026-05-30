"""
GET /stores/{store_id}/metrics — real-time store metrics for today (UTC).
No caching — queries DB live on each request.
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, date, timezone

import structlog
from fastapi import APIRouter, HTTPException, Request

from app.db import get_db
from app.models import MetricsResponse

router = APIRouter()
logger = structlog.get_logger(__name__)


@router.get("/stores/{store_id}/metrics", response_model=MetricsResponse)
async def get_metrics(store_id: str, request: Request) -> MetricsResponse:
    trace_id = str(uuid.uuid4())
    start_ms = time.monotonic()

    today_start = datetime.combine(date.today(), datetime.min.time()).replace(tzinfo=timezone.utc)
    today_start_str = today_start.isoformat()

    try:
        async with get_db() as db:
            # Unique customer visitors today (exclude staff)
            async with db.execute(
                """
                SELECT COUNT(DISTINCT visitor_id) as cnt
                FROM events
                WHERE store_id = ?
                  AND event_type IN ('ENTRY', 'REENTRY')
                  AND is_staff = 0
                  AND timestamp >= ?
                """,
                (store_id, today_start_str),
            ) as cur:
                row = await cur.fetchone()
                unique_visitors = row["cnt"] if row else 0

            # Conversion rate: sessions with a purchase / total sessions today
            async with db.execute(
                """
                SELECT
                    COUNT(*) as total,
                    SUM(CASE WHEN converted = 1 THEN 1 ELSE 0 END) as converted_count
                FROM sessions
                WHERE store_id = ?
                  AND entry_time >= ?
                """,
                (store_id, today_start_str),
            ) as cur:
                row = await cur.fetchone()
                total_sessions = row["total"] if row else 0
                converted_sessions = row["converted_count"] if row else 0
            conversion_rate = (converted_sessions / total_sessions) if total_sessions > 0 else 0.0

            # Average dwell per zone (customer-only)
            async with db.execute(
                """
                SELECT zone_id, AVG(dwell_ms) as avg_dwell
                FROM events
                WHERE store_id = ?
                  AND event_type = 'ZONE_DWELL'
                  AND is_staff = 0
                  AND zone_id IS NOT NULL
                  AND timestamp >= ?
                GROUP BY zone_id
                """,
                (store_id, today_start_str),
            ) as cur:
                rows = await cur.fetchall()
            avg_dwell_per_zone = {r["zone_id"]: round(r["avg_dwell"], 2) for r in rows if r["zone_id"]}

            # Current queue depth: active BILLING_QUEUE_JOIN minus completed/abandoned
            async with db.execute(
                """
                SELECT
                    (SELECT COUNT(DISTINCT visitor_id) FROM events
                     WHERE store_id = ? AND event_type = 'BILLING_QUEUE_JOIN'
                       AND is_staff = 0 AND timestamp >= ?) -
                    (SELECT COUNT(DISTINCT visitor_id) FROM events
                     WHERE store_id = ? AND event_type = 'BILLING_QUEUE_ABANDON'
                       AND timestamp >= ?) as queue_depth
                """,
                (store_id, today_start_str, store_id, today_start_str),
            ) as cur:
                row = await cur.fetchone()
                current_queue_depth = max(0, row[0] if (row and row[0] is not None) else 0)

            # Abandonment rate: visitors who joined queue but abandoned / total who joined
            async with db.execute(
                """
                SELECT COUNT(DISTINCT visitor_id) as joined
                FROM events
                WHERE store_id = ? AND event_type = 'BILLING_QUEUE_JOIN'
                  AND is_staff = 0 AND timestamp >= ?
                """,
                (store_id, today_start_str),
            ) as cur:
                row = await cur.fetchone()
                joined_queue = row["joined"] if row else 0

            async with db.execute(
                """
                SELECT COUNT(DISTINCT visitor_id) as abandoned
                FROM events
                WHERE store_id = ? AND event_type = 'BILLING_QUEUE_ABANDON'
                  AND timestamp >= ?
                """,
                (store_id, today_start_str),
            ) as cur:
                row = await cur.fetchone()
                abandoned_queue = row["abandoned"] if row else 0

            abandonment_rate = (abandoned_queue / joined_queue) if joined_queue > 0 else 0.0

    except Exception as exc:
        latency_ms = int((time.monotonic() - start_ms) * 1000)
        logger.error(
            "metrics_error",
            trace_id=trace_id,
            store_id=store_id,
            endpoint=f"/stores/{store_id}/metrics",
            latency_ms=latency_ms,
            status_code=503,
            error=str(exc),
        )
        raise HTTPException(status_code=503, detail={"error": "database_unavailable", "retry_after": 30})

    latency_ms = int((time.monotonic() - start_ms) * 1000)
    logger.info(
        "metrics_served",
        trace_id=trace_id,
        store_id=store_id,
        endpoint=f"/stores/{store_id}/metrics",
        latency_ms=latency_ms,
        status_code=200,
    )

    return MetricsResponse(
        store_id=store_id,
        unique_visitors=unique_visitors,
        conversion_rate=round(conversion_rate, 4),
        avg_dwell_per_zone=avg_dwell_per_zone,
        current_queue_depth=current_queue_depth,
        abandonment_rate=round(abandonment_rate, 4),
        as_of=datetime.now(timezone.utc),
    )
