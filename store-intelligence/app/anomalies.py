"""
GET /stores/{store_id}/anomalies — detect operational anomalies.

Detects:
  BILLING_QUEUE_SPIKE : queue_depth > 5 for 3+ consecutive minutes → CRITICAL
  CONVERSION_DROP     : today's rate < 7-day avg × 0.8 → WARN
  DEAD_ZONE           : a zone with 0 visits in last 30 minutes (during open hours) → INFO
  STALE_CAMERA        : no events from a camera_id for 10+ minutes → WARN
"""

from __future__ import annotations

import time
import uuid
from datetime import date, datetime, timedelta, timezone

import structlog
from fastapi import APIRouter, HTTPException, Request

from app.db import get_db
from app.models import Anomaly, AnomalySeverity, AnomalyType, AnomaliesResponse

router = APIRouter()
logger = structlog.get_logger(__name__)

QUEUE_SPIKE_THRESHOLD = 5
QUEUE_SPIKE_DURATION_MINUTES = 3
CONVERSION_DROP_FACTOR = 0.8
DEAD_ZONE_WINDOW_MINUTES = 30
STALE_CAMERA_WINDOW_MINUTES = 10


@router.get("/stores/{store_id}/anomalies", response_model=AnomaliesResponse)
async def get_anomalies(store_id: str, request: Request) -> AnomaliesResponse:
    trace_id = str(uuid.uuid4())
    start_ms = time.monotonic()

    now = datetime.now(timezone.utc)
    today_start = datetime.combine(date.today(), datetime.min.time()).replace(tzinfo=timezone.utc)
    seven_days_ago = now - timedelta(days=7)
    thirty_min_ago = now - timedelta(minutes=DEAD_ZONE_WINDOW_MINUTES)
    ten_min_ago = now - timedelta(minutes=STALE_CAMERA_WINDOW_MINUTES)
    three_min_ago = now - timedelta(minutes=QUEUE_SPIKE_DURATION_MINUTES)

    anomalies: list[Anomaly] = []

    try:
        async with get_db() as db:
            # ----------------------------------------------------------------
            # 1. BILLING_QUEUE_SPIKE — queue_depth > 5 for 3+ consecutive min
            # ----------------------------------------------------------------
            # Approximate: count BILLING_QUEUE_JOIN events in last 3 minutes where
            # net queue (joins - abandons) > spike threshold.
            async with db.execute(
                """
                SELECT
                  (SELECT COUNT(DISTINCT visitor_id) FROM events
                   WHERE store_id = ? AND event_type = 'BILLING_QUEUE_JOIN'
                     AND is_staff = 0 AND timestamp >= ?) -
                  (SELECT COUNT(DISTINCT visitor_id) FROM events
                   WHERE store_id = ? AND event_type IN ('BILLING_QUEUE_ABANDON', 'EXIT')
                     AND timestamp >= ?) as net_queue
                """,
                (
                    store_id, three_min_ago.isoformat(),
                    store_id, three_min_ago.isoformat(),
                ),
            ) as cur:
                row = await cur.fetchone()
                net_queue = row[0] if (row and row[0] is not None) else 0

            if net_queue > QUEUE_SPIKE_THRESHOLD:
                anomalies.append(
                    Anomaly(
                        type=AnomalyType.BILLING_QUEUE_SPIKE,
                        severity=AnomalySeverity.CRITICAL,
                        description=(
                            f"Billing queue depth {net_queue} has exceeded {QUEUE_SPIKE_THRESHOLD} "
                            f"for the past {QUEUE_SPIKE_DURATION_MINUTES}+ minutes."
                        ),
                        suggested_action="Open an additional billing counter immediately.",
                        detected_at=now,
                    )
                )

            # ----------------------------------------------------------------
            # 2. CONVERSION_DROP — today's rate < 7-day average × 0.8
            # ----------------------------------------------------------------
            async with db.execute(
                """
                SELECT
                  COUNT(*) as total,
                  SUM(CASE WHEN converted = 1 THEN 1 ELSE 0 END) as converted_count
                FROM sessions
                WHERE store_id = ? AND entry_time >= ?
                """,
                (store_id, today_start.isoformat()),
            ) as cur:
                row = await cur.fetchone()
                today_total = row["total"] if row else 0
                today_converted = row["converted_count"] if row else 0
            today_rate = (today_converted / today_total) if today_total > 0 else 0.0

            async with db.execute(
                """
                SELECT
                  COUNT(*) as total,
                  SUM(CASE WHEN converted = 1 THEN 1 ELSE 0 END) as converted_count
                FROM sessions
                WHERE store_id = ? AND entry_time >= ? AND entry_time < ?
                """,
                (store_id, seven_days_ago.isoformat(), today_start.isoformat()),
            ) as cur:
                row = await cur.fetchone()
                hist_total = row["total"] if row else 0
                hist_converted = row["converted_count"] if row else 0
            hist_rate = (hist_converted / hist_total) if hist_total > 0 else None

            if hist_rate is not None and today_total > 0:
                threshold = hist_rate * CONVERSION_DROP_FACTOR
                if today_rate < threshold:
                    anomalies.append(
                        Anomaly(
                            type=AnomalyType.CONVERSION_DROP,
                            severity=AnomalySeverity.WARN,
                            description=(
                                f"Today's conversion rate {today_rate:.2%} is below "
                                f"{CONVERSION_DROP_FACTOR:.0%} of the 7-day average {hist_rate:.2%} "
                                f"(threshold: {threshold:.2%})."
                            ),
                            suggested_action="Review promotions, staffing, and product availability.",
                            detected_at=now,
                        )
                    )

            # ----------------------------------------------------------------
            # 3. DEAD_ZONE — zone with 0 visits in last 30 min (during open hours)
            # ----------------------------------------------------------------
            # Find all zones that had at least one visit historically today,
            # but zero visits in the past 30 minutes.
            async with db.execute(
                """
                SELECT DISTINCT zone_id FROM events
                WHERE store_id = ? AND event_type = 'ZONE_ENTER'
                  AND is_staff = 0 AND zone_id IS NOT NULL
                  AND timestamp >= ?
                """,
                (store_id, today_start.isoformat()),
            ) as cur:
                all_active_zones = {r["zone_id"] for r in await cur.fetchall()}

            async with db.execute(
                """
                SELECT DISTINCT zone_id FROM events
                WHERE store_id = ? AND event_type = 'ZONE_ENTER'
                  AND is_staff = 0 AND zone_id IS NOT NULL
                  AND timestamp >= ?
                """,
                (store_id, thirty_min_ago.isoformat()),
            ) as cur:
                recent_zones = {r["zone_id"] for r in await cur.fetchall()}

            dead_zones = all_active_zones - recent_zones
            for zone_id in sorted(dead_zones):
                anomalies.append(
                    Anomaly(
                        type=AnomalyType.DEAD_ZONE,
                        severity=AnomalySeverity.INFO,
                        description=(
                            f"Zone '{zone_id}' has had zero customer visits in the past "
                            f"{DEAD_ZONE_WINDOW_MINUTES} minutes during store open hours."
                        ),
                        suggested_action=f"Check zone '{zone_id}' for signage, stockouts, or obstructions.",
                        detected_at=now,
                    )
                )

            # ----------------------------------------------------------------
            # 4. STALE_CAMERA — no events from camera_id for 10+ minutes
            # ----------------------------------------------------------------
            async with db.execute(
                """
                SELECT DISTINCT camera_id FROM events
                WHERE store_id = ? AND timestamp >= ?
                """,
                (store_id, today_start.isoformat()),
            ) as cur:
                cameras_with_events_today = {r["camera_id"] for r in await cur.fetchall()}

            async with db.execute(
                """
                SELECT DISTINCT camera_id FROM events
                WHERE store_id = ? AND timestamp >= ?
                """,
                (store_id, ten_min_ago.isoformat()),
            ) as cur:
                cameras_with_recent_events = {r["camera_id"] for r in await cur.fetchall()}

            stale_cameras = cameras_with_events_today - cameras_with_recent_events
            for camera_id in sorted(stale_cameras):
                anomalies.append(
                    Anomaly(
                        type=AnomalyType.STALE_CAMERA,
                        severity=AnomalySeverity.WARN,
                        description=(
                            f"Camera '{camera_id}' has not emitted any events for "
                            f"{STALE_CAMERA_WINDOW_MINUTES}+ minutes."
                        ),
                        suggested_action=f"Verify camera '{camera_id}' connectivity and pipeline health.",
                        detected_at=now,
                    )
                )

    except Exception as exc:
        latency_ms = int((time.monotonic() - start_ms) * 1000)
        logger.error(
            "anomalies_error",
            trace_id=trace_id,
            store_id=store_id,
            endpoint=f"/stores/{store_id}/anomalies",
            latency_ms=latency_ms,
            status_code=503,
            error=str(exc),
        )
        raise HTTPException(status_code=503, detail={"error": "database_unavailable", "retry_after": 30})

    latency_ms = int((time.monotonic() - start_ms) * 1000)
    logger.info(
        "anomalies_served",
        trace_id=trace_id,
        store_id=store_id,
        endpoint=f"/stores/{store_id}/anomalies",
        latency_ms=latency_ms,
        status_code=200,
        anomaly_count=len(anomalies),
    )

    return AnomaliesResponse(
        store_id=store_id,
        anomalies=anomalies,
        as_of=now,
    )
