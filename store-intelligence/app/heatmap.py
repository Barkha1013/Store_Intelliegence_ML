"""
GET /stores/{store_id}/heatmap — per-zone visit frequency and dwell heatmap.

data_confidence: "LOW" if fewer than 20 sessions in the window, else "HIGH".
normalised_score: 0-100 relative to the busiest zone.
"""

from __future__ import annotations

import time
import uuid
from datetime import date, datetime, timezone

import structlog
from fastapi import APIRouter, HTTPException, Request

from app.db import get_db
from app.models import DataConfidence, HeatmapResponse, ZoneHeatmap

router = APIRouter()
logger = structlog.get_logger(__name__)

LOW_CONFIDENCE_SESSION_THRESHOLD = 20


@router.get("/stores/{store_id}/heatmap", response_model=HeatmapResponse)
async def get_heatmap(store_id: str, request: Request) -> HeatmapResponse:
    trace_id = str(uuid.uuid4())
    start_ms = time.monotonic()

    today_start = datetime.combine(date.today(), datetime.min.time()).replace(tzinfo=timezone.utc)
    today_start_str = today_start.isoformat()

    try:
        async with get_db() as db:
            # Total sessions for confidence threshold
            async with db.execute(
                """
                SELECT COUNT(*) as cnt FROM sessions
                WHERE store_id = ? AND entry_time >= ?
                """,
                (store_id, today_start_str),
            ) as cur:
                row = await cur.fetchone()
                session_count = row["cnt"] if row else 0

            data_confidence = (
                DataConfidence.LOW if session_count < LOW_CONFIDENCE_SESSION_THRESHOLD
                else DataConfidence.HIGH
            )

            # Per-zone: visit frequency and avg dwell (customers only)
            async with db.execute(
                """
                SELECT
                    zone_id,
                    COUNT(*) as visit_frequency,
                    CAST(AVG(dwell_ms) AS INTEGER) as avg_dwell_ms
                FROM events
                WHERE store_id = ?
                  AND event_type = 'ZONE_DWELL'
                  AND is_staff = 0
                  AND zone_id IS NOT NULL
                  AND timestamp >= ?
                GROUP BY zone_id
                ORDER BY visit_frequency DESC
                """,
                (store_id, today_start_str),
            ) as cur:
                rows = await cur.fetchall()

    except Exception as exc:
        latency_ms = int((time.monotonic() - start_ms) * 1000)
        logger.error(
            "heatmap_error",
            trace_id=trace_id,
            store_id=store_id,
            endpoint=f"/stores/{store_id}/heatmap",
            latency_ms=latency_ms,
            status_code=503,
            error=str(exc),
        )
        raise HTTPException(status_code=503, detail={"error": "database_unavailable", "retry_after": 30})

    if not rows:
        latency_ms = int((time.monotonic() - start_ms) * 1000)
        logger.info(
            "heatmap_served_empty",
            trace_id=trace_id,
            store_id=store_id,
            endpoint=f"/stores/{store_id}/heatmap",
            latency_ms=latency_ms,
            status_code=200,
        )
        return HeatmapResponse(
            store_id=store_id,
            zones=[],
            data_confidence=data_confidence,
            as_of=datetime.now(timezone.utc),
        )

    max_freq = max(r["visit_frequency"] for r in rows) or 1

    zones = [
        ZoneHeatmap(
            zone_id=r["zone_id"],
            visit_frequency=r["visit_frequency"],
            avg_dwell_ms=r["avg_dwell_ms"] or 0,
            normalised_score=round(r["visit_frequency"] / max_freq * 100, 2),
        )
        for r in rows
    ]

    latency_ms = int((time.monotonic() - start_ms) * 1000)
    logger.info(
        "heatmap_served",
        trace_id=trace_id,
        store_id=store_id,
        endpoint=f"/stores/{store_id}/heatmap",
        latency_ms=latency_ms,
        status_code=200,
        zone_count=len(zones),
    )

    return HeatmapResponse(
        store_id=store_id,
        zones=zones,
        data_confidence=data_confidence,
        as_of=datetime.now(timezone.utc),
    )
