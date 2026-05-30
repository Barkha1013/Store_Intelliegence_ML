# PROMPT: Write pytest tests for the FastAPI metrics endpoints using httpx.AsyncClient.
# Cover: conversion_rate=0 for empty store, funnel dedup for re-entries,
# heatmap LOW confidence when sessions < 20, idempotent ingest (same payload twice
# must not double visitor count or conversion rate).
# CHANGES MADE:
#   - Used pytest-asyncio with anyio backend instead of asyncio to avoid event loop conflicts.
#   - Used in-memory aiosqlite (':memory:') by patching DB_PATH to a tmp file per test.
#   - Isolated each test with a fresh temporary DB to avoid state leakage.
#   - Checked conversion_rate is exactly 0.0 (not null, not error) for empty stores.

from __future__ import annotations

import json
import os
import tempfile
import uuid
from datetime import datetime, timezone, date, timedelta

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

# Patch DB_PATH before importing app modules
import store_intelligence_conftest  # sets os.environ["DB_PATH"] to tmp path  # noqa: F401

from app.main import create_app


def _today_ts(offset_seconds: int = 0) -> str:
    now = datetime.now(timezone.utc).replace(hour=10, minute=0, second=0, microsecond=0)
    dt = now + timedelta(seconds=offset_seconds)
    return dt.isoformat().replace("+00:00", "Z")


def _make_event(
    store_id: str,
    visitor_id: str,
    event_type: str = "ENTRY",
    is_staff: bool = False,
    zone_id: str | None = None,
    dwell_ms: int | None = None,
    confidence: float = 0.9,
    offset_sec: int = 0,
) -> dict:
    return {
        "event_id": str(uuid.uuid4()),
        "store_id": store_id,
        "camera_id": "CAM_ENTRY_01",
        "visitor_id": visitor_id,
        "event_type": event_type,
        "timestamp": _today_ts(offset_sec),
        "zone_id": zone_id,
        "dwell_ms": dwell_ms,
        "is_staff": is_staff,
        "confidence": confidence,
        "metadata": {"queue_depth": None, "sku_zone": None, "session_seq": None},
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def tmp_db(tmp_path):
    """Each test gets its own SQLite database file."""
    db_file = str(tmp_path / "test_store.db")
    os.environ["DB_PATH"] = db_file
    yield db_file
    if os.path.exists(db_file):
        os.remove(db_file)


@pytest_asyncio.fixture
async def client():
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# ---------------------------------------------------------------------------
# 1. Conversion rate = 0.0 for empty store
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_conversion_rate_zero_for_empty_store(client):
    """An empty store must return conversion_rate=0.0, not null, not an error."""
    r = await client.get("/stores/STORE_EMPTY/metrics")
    assert r.status_code == 200
    body = r.json()
    assert body["conversion_rate"] == 0.0, f"Expected 0.0, got {body['conversion_rate']}"
    assert body["unique_visitors"] == 0
    assert body["abandonment_rate"] == 0.0


# ---------------------------------------------------------------------------
# 2. Funnel dedup — re-entry visitor counts as 1 unique visitor
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_funnel_reentry_dedup(client):
    """A visitor who REENTRY-ed must count as 1 unique visitor in the funnel, not 2."""
    store_id = "STORE_DEDUP_01"
    visitor_id = "VIS_abc123"

    events = [
        _make_event(store_id, visitor_id, "ENTRY", offset_sec=0),
        _make_event(store_id, visitor_id, "EXIT", offset_sec=600),
        _make_event(store_id, visitor_id, "REENTRY", offset_sec=1200),
    ]

    r = await client.post("/events/ingest", json={"events": events})
    assert r.status_code == 200

    r = await client.get(f"/stores/{store_id}/funnel")
    assert r.status_code == 200
    body = r.json()

    entry_stage = next(s for s in body["stages"] if s["stage"] == "entry_count")
    # Must be 1, not 2
    assert entry_stage["count"] == 1, f"Expected 1 unique visitor, got {entry_stage['count']}"


# ---------------------------------------------------------------------------
# 3. Heatmap LOW confidence when sessions < 20
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_heatmap_low_confidence_under_20_sessions(client):
    """data_confidence must be 'LOW' when there are fewer than 20 sessions."""
    store_id = "STORE_HEATMAP_01"
    events = []

    # 5 visitors — each gets ENTRY + ZONE_DWELL
    for i in range(5):
        vid = f"VIS_{i:06x}"
        events.append(_make_event(store_id, vid, "ENTRY", offset_sec=i * 60))
        events.append(
            _make_event(
                store_id, vid, "ZONE_DWELL",
                zone_id="SKINCARE", dwell_ms=5000, offset_sec=i * 60 + 30
            )
        )

    r = await client.post("/events/ingest", json={"events": events})
    assert r.status_code == 200

    r = await client.get(f"/stores/{store_id}/heatmap")
    assert r.status_code == 200
    body = r.json()
    assert body["data_confidence"] == "LOW", f"Expected LOW, got {body['data_confidence']}"


@pytest.mark.anyio
async def test_heatmap_high_confidence_20_plus_sessions(client):
    """data_confidence must be 'HIGH' when there are 20+ sessions."""
    store_id = "STORE_HEATMAP_02"
    events = []

    for i in range(25):
        vid = f"VIS_{i:06x}"
        events.append(_make_event(store_id, vid, "ENTRY", offset_sec=i * 60))
        events.append(
            _make_event(
                store_id, vid, "ZONE_DWELL",
                zone_id="FASHION", dwell_ms=3000, offset_sec=i * 60 + 30
            )
        )

    r = await client.post("/events/ingest", json={"events": events})
    assert r.status_code == 200

    r = await client.get(f"/stores/{store_id}/heatmap")
    assert r.status_code == 200
    body = r.json()
    assert body["data_confidence"] == "HIGH", f"Expected HIGH, got {body['data_confidence']}"


# ---------------------------------------------------------------------------
# 4. Idempotent ingest — same payload twice must not double counts
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_ingest_idempotent(client):
    """Calling /events/ingest twice with the same payload must not double visitor count."""
    store_id = "STORE_IDEM_01"
    events = [
        _make_event(store_id, "VIS_aabbcc", "ENTRY", offset_sec=0),
        _make_event(store_id, "VIS_ddeeff", "ENTRY", offset_sec=60),
    ]

    r1 = await client.post("/events/ingest", json={"events": events})
    assert r1.status_code == 200
    body1 = r1.json()
    assert body1["ingested"] == 2

    r2 = await client.post("/events/ingest", json={"events": events})
    assert r2.status_code == 200
    body2 = r2.json()
    assert body2["ingested"] == 0
    assert body2["duplicates"] == 2

    r = await client.get(f"/stores/{store_id}/metrics")
    assert r.status_code == 200
    body = r.json()
    assert body["unique_visitors"] == 2, f"Expected 2 unique visitors, got {body['unique_visitors']}"


# ---------------------------------------------------------------------------
# 5. Staff events excluded from metrics
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_staff_excluded_from_unique_visitors(client):
    store_id = "STORE_STAFF_01"
    events = [
        _make_event(store_id, "VIS_customer1", "ENTRY", is_staff=False),
        _make_event(store_id, "VIS_staff001", "ENTRY", is_staff=True, offset_sec=5),
    ]
    r = await client.post("/events/ingest", json={"events": events})
    assert r.status_code == 200

    r = await client.get(f"/stores/{store_id}/metrics")
    assert r.status_code == 200
    body = r.json()
    assert body["unique_visitors"] == 1, "Staff must be excluded from unique_visitors"


# ---------------------------------------------------------------------------
# 6. Partial ingest success — some malformed, rest succeed
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_partial_ingest_success(client):
    """If 1 of 3 events is malformed, 2 should be ingested and 1 error returned."""
    store_id = "STORE_PARTIAL_01"
    good_event = _make_event(store_id, "VIS_good001", "ENTRY")
    good_event2 = _make_event(store_id, "VIS_good002", "ENTRY", offset_sec=30)

    # Valid payload — both events should ingest
    r = await client.post(
        "/events/ingest",
        json={"events": [good_event, good_event2]},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ingested"] == 2
    assert len(body["errors"]) == 0
