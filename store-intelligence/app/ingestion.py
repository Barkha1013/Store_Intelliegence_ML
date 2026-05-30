"""
POST /events/ingest — batch ingest with dedup, validation, partial success, and structured logging.
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import structlog
from fastapi import APIRouter, Request
from pydantic import ValidationError

from app.db import get_db
from app.models import IngestError, IngestRequest, IngestResponse, StoreEvent
from app.sessions import upsert_session

router = APIRouter()
logger = structlog.get_logger(__name__)


async def _ingest_events_to_db(events: list[StoreEvent]) -> tuple[int, int, list[IngestError]]:
    """
    Core ingestion logic. Returns (ingested_count, duplicate_count, errors).
    Idempotent: inserting the same event_id twice silently ignores the duplicate.
    """
    ingested = 0
    duplicates = 0
    errors: list[IngestError] = []

    async with get_db() as db:
        for event in events:
            try:
                ingested_at = datetime.now(timezone.utc).isoformat()
                metadata_json = event.metadata.model_dump_json()

                await db.execute(
                    """
                    INSERT OR IGNORE INTO events
                      (event_id, store_id, camera_id, visitor_id, event_type,
                       timestamp, zone_id, dwell_ms, is_staff, confidence, metadata, ingested_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(event.event_id),
                        event.store_id,
                        event.camera_id,
                        event.visitor_id,
                        event.event_type,
                        event.timestamp.isoformat() if isinstance(event.timestamp, datetime) else event.timestamp,
                        event.zone_id,
                        event.dwell_ms,
                        1 if event.is_staff else 0,
                        event.confidence,
                        metadata_json,
                        ingested_at,
                    ),
                )
                changes = db.total_changes
                # SQLite INSERT OR IGNORE: if the row existed, total_changes won't increment
                # We check by querying changes on the connection
                row_count = await db.execute(
                    "SELECT changes()"
                )
                async with row_count as cursor:
                    row = await cursor.fetchone()
                    affected = row[0] if row else 0

                if affected == 0:
                    duplicates += 1
                else:
                    ingested += 1
                    # Update sessions table
                    await upsert_session(db, event)

            except Exception as exc:
                errors.append(
                    IngestError(event_id=str(event.event_id), reason=str(exc))
                )

        await db.commit()

    return ingested, duplicates, errors


@router.post("/events/ingest", response_model=IngestResponse)
async def ingest_events(request: Request, body: IngestRequest) -> IngestResponse:
    trace_id = str(uuid.uuid4())
    start_ms = time.monotonic()

    valid_events: list[StoreEvent] = []
    validation_errors: list[IngestError] = []
    store_ids: set[str] = set()

    # Validate each event individually for partial success
    for raw in body.events:
        # raw is already a StoreEvent (validated by IngestRequest)
        valid_events.append(raw)
        store_ids.add(raw.store_id)

    store_id_str = ",".join(sorted(store_ids)) if store_ids else "unknown"

    try:
        ingested, duplicates, db_errors = await _ingest_events_to_db(valid_events)
        all_errors = validation_errors + db_errors
        status_code = 200
    except Exception as exc:
        logger.error(
            "ingest_failed",
            trace_id=trace_id,
            store_id=store_id_str,
            error=str(exc),
        )
        raise

    latency_ms = int((time.monotonic() - start_ms) * 1000)
    logger.info(
        "events_ingested",
        trace_id=trace_id,
        store_id=store_id_str,
        endpoint="/events/ingest",
        latency_ms=latency_ms,
        event_count=len(body.events),
        status_code=status_code,
        ingested=ingested,
        duplicates=duplicates,
        error_count=len(all_errors),
    )

    return IngestResponse(
        ingested=ingested,
        duplicates=duplicates,
        errors=all_errors,
    )
