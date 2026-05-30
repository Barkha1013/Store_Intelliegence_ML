"""
assertions.py — 10 test assertions the API must pass.

Run as part of CI: pytest assertions.py -v

These tests spin up the FastAPI app in-process using httpx.AsyncClient
and verify the core contract requirements from the spec.
"""

from __future__ import annotations

import os
import tempfile
import uuid
from datetime import datetime, timezone, timedelta

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport


# ---------------------------------------------------------------------------
# DB isolation
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolated_db(tmp_path):
    db_file = str(tmp_path / "assert_test.db")
    os.environ["DB_PATH"] = db_file
    yield db_file
    if os.path.exists(db_file):
        os.remove(db_file)


@pytest_asyncio.fixture
async def client():
    from app.main import create_app
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_ts(offset_sec: int = 0) -> str:
    dt = datetime.now(timezone.utc) + timedelta(seconds=offset_sec)
    return dt.isoformat().replace("+00:00", "Z")


def _event(
    store_id: str,
    visitor_id: str,
    event_type: str = "ENTRY",
    is_staff: bool = False,
    zone_id: str | None = None,
    dwell_ms: int | None = None,
    confidence: float = 0.9,
    offset_sec: int = 0,
    camera_id: str = "CAM_ENTRY_01",
    queue_depth: int | None = None,
) -> dict:
    return {
        "event_id": str(uuid.uuid4()),
        "store_id": store_id,
        "camera_id": camera_id,
        "visitor_id": visitor_id,
        "event_type": event_type,
        "timestamp": _now_ts(offset_sec),
        "zone_id": zone_id,
        "dwell_ms": dwell_ms,
        "is_staff": is_staff,
        "confidence": confidence,
        "metadata": {"queue_depth": queue_depth, "sku_zone": None, "session_seq": None},
    }


# ---------------------------------------------------------------------------
# Assertion 1: POST /events/ingest returns 200 and correct ingested count
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_assertion_1_ingest_returns_200(client):
    events = [_event("STORE_BLR_001", f"VIS_{i:06x}") for i in range(5)]
    r = await client.post("/events/ingest", json={"events": events})
    assert r.status_code == 200
    body = r.json()
    assert body["ingested"] == 5
    assert body["duplicates"] == 0
    assert body["errors"] == []


# ---------------------------------------------------------------------------
# Assertion 2: Ingest is idempotent — same payload twice, duplicates counted
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_assertion_2_ingest_idempotent(client):
    events = [_event("STORE_BLR_002", "VIS_idem01")]
    r1 = await client.post("/events/ingest", json={"events": events})
    assert r1.json()["ingested"] == 1

    r2 = await client.post("/events/ingest", json={"events": events})
    assert r2.json()["duplicates"] == 1
    assert r2.json()["ingested"] == 0

    # Metrics must show 1 visitor, not 2
    r3 = await client.get("/stores/STORE_BLR_002/metrics")
    assert r3.status_code == 200
    assert r3.json()["unique_visitors"] == 1


# ---------------------------------------------------------------------------
# Assertion 3: Staff events excluded from unique_visitors
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_assertion_3_staff_excluded_from_metrics(client):
    events = [
        _event("STORE_BLR_001", "VIS_cust01", is_staff=False),
        _event("STORE_BLR_001", "VIS_staff1", is_staff=True, offset_sec=10),
    ]
    await client.post("/events/ingest", json={"events": events})
    r = await client.get("/stores/STORE_BLR_001/metrics")
    assert r.status_code == 200
    assert r.json()["unique_visitors"] == 1


# ---------------------------------------------------------------------------
# Assertion 4: conversion_rate = 0.0 (not null, not error) for empty store
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_assertion_4_conversion_rate_zero_for_empty_store(client):
    r = await client.get("/stores/STORE_NONEXISTENT/metrics")
    assert r.status_code == 200
    body = r.json()
    assert body["conversion_rate"] == 0.0
    assert body["unique_visitors"] == 0


# ---------------------------------------------------------------------------
# Assertion 5: visitor_id format — VIS_ + 6 hex chars
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_assertion_5_visitor_id_format():
    from pipeline.emit import make_visitor_id
    import hashlib

    store_id, tracker_id, start_frame = "STORE_BLR_001", 42, 300
    vid = make_visitor_id(store_id, tracker_id, start_frame)
    assert vid.startswith("VIS_")
    assert len(vid) == 10  # "VIS_" (4) + 6 hex chars
    suffix = vid[4:]
    assert all(c in "0123456789abcdef" for c in suffix)

    # Verify derivation
    raw = f"{store_id}{tracker_id}{start_frame}"
    expected = hashlib.sha256(raw.encode()).hexdigest()[:6]
    assert vid == f"VIS_{expected}"


# ---------------------------------------------------------------------------
# Assertion 6: Timestamp derived from frame number, not system time
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_assertion_6_timestamp_from_frame():
    from pipeline.emit import frame_to_timestamp
    from datetime import timedelta

    clip_start = datetime(2026, 3, 3, 14, 22, 0, tzinfo=timezone.utc)
    ts = frame_to_timestamp(clip_start, frame_number=150, fps=15.0)
    # 150 / 15 = 10 seconds
    expected_dt = clip_start + timedelta(seconds=10)
    assert ts == expected_dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Assertion 7: Partial confidence — confidence < 0.45 not rounded up
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_assertion_7_partial_occlusion_confidence(client):
    conf = 0.32  # below 0.45 threshold
    event = _event("STORE_BLR_001", "VIS_partial", confidence=conf)
    r = await client.post("/events/ingest", json={"events": [event]})
    assert r.status_code == 200
    assert r.json()["ingested"] == 1
    # Verify the confidence was stored exactly
    import aiosqlite
    db_path = os.environ["DB_PATH"]
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT confidence FROM events WHERE visitor_id = ?", ("VIS_partial",)
        ) as cur:
            row = await cur.fetchone()
    assert row is not None
    assert abs(row["confidence"] - conf) < 1e-6, f"Expected {conf}, got {row['confidence']}"


# ---------------------------------------------------------------------------
# Assertion 8: /health always returns 200 (even with no data)
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_assertion_8_health_always_200(client):
    r = await client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["db_status"] in ("connected", "unavailable")
    assert body["status"] in ("ok", "degraded")
    assert "uptime_seconds" in body


# ---------------------------------------------------------------------------
# Assertion 9: Funnel uses sessions — re-entries count as 1 unique visitor
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_assertion_9_funnel_reentry_dedup(client):
    store_id = "STORE_FUNNEL_01"
    vid = "VIS_funnel1"
    events = [
        _event(store_id, vid, "ENTRY", offset_sec=0),
        _event(store_id, vid, "EXIT", offset_sec=600),
        _event(store_id, vid, "REENTRY", offset_sec=1200),
    ]
    await client.post("/events/ingest", json={"events": events})

    r = await client.get(f"/stores/{store_id}/funnel")
    assert r.status_code == 200
    stages = {s["stage"]: s["count"] for s in r.json()["stages"]}
    assert stages["entry_count"] == 1, f"Expected 1, got {stages['entry_count']}"


# ---------------------------------------------------------------------------
# Assertion 10: Heatmap normalised_score is 0-100 and highest zone = 100
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_assertion_10_heatmap_normalised_score(client):
    store_id = "STORE_HEAT_01"
    events = []
    # Zone A: 5 visits, Zone B: 2 visits → A should score 100, B ~40
    for i in range(5):
        events.append(_event(store_id, f"VIS_{i:06x}", "ZONE_DWELL", zone_id="ZONE_A", dwell_ms=5000, offset_sec=i * 60))
    for i in range(2):
        events.append(_event(store_id, f"VIS_b{i:05x}", "ZONE_DWELL", zone_id="ZONE_B", dwell_ms=3000, offset_sec=i * 60))

    await client.post("/events/ingest", json={"events": events})

    r = await client.get(f"/stores/{store_id}/heatmap")
    assert r.status_code == 200
    zones = {z["zone_id"]: z for z in r.json()["zones"]}
    assert "ZONE_A" in zones
    assert zones["ZONE_A"]["normalised_score"] == 100.0
    for z in r.json()["zones"]:
        assert 0 <= z["normalised_score"] <= 100
