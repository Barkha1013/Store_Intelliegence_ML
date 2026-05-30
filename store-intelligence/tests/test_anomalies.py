# PROMPT: Write pytest tests for the anomalies endpoint covering: queue spike
# threshold detection (net queue > 5 for 3+ consecutive minutes → CRITICAL),
# conversion drop vs 7-day average (today < 7-day avg × 0.8 → WARN),
# and dead zone detection (zone with 0 visits in last 30 minutes → INFO).
# Use httpx.AsyncClient with ASGI transport.
# CHANGES MADE:
#   - Isolated each test with per-test tmp SQLite DB via tmp_db fixture.
#   - Timestamps for 7-day-history events are injected as ISO strings pre-dated 1-6 days ago.
#   - Dead-zone test uses "today" events first to register the zone, then leaves the
#     30-minute window empty, checking the anomaly appears.

from __future__ import annotations

import os
import uuid
from datetime import datetime, date, timedelta, timezone

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

import store_intelligence_conftest  # noqa: F401

from app.main import create_app


def _ts(days_ago: int = 0, hour: int = 10, minute: int = 0) -> str:
    d = date.today() - timedelta(days=days_ago)
    dt = datetime(d.year, d.month, d.day, hour, minute, 0, tzinfo=timezone.utc)
    return dt.isoformat().replace("+00:00", "Z")


def _event(
    store_id: str,
    visitor_id: str,
    event_type: str,
    days_ago: int = 0,
    hour: int = 10,
    minute: int = 0,
    zone_id: str | None = None,
    dwell_ms: int | None = None,
    is_staff: bool = False,
    camera_id: str = "CAM_ENTRY_01",
) -> dict:
    return {
        "event_id": str(uuid.uuid4()),
        "store_id": store_id,
        "camera_id": camera_id,
        "visitor_id": visitor_id,
        "event_type": event_type,
        "timestamp": _ts(days_ago, hour, minute),
        "zone_id": zone_id,
        "dwell_ms": dwell_ms,
        "is_staff": is_staff,
        "confidence": 0.9,
        "metadata": {"queue_depth": None, "sku_zone": None, "session_seq": None},
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def tmp_db(tmp_path):
    db_file = str(tmp_path / "anomaly_test.db")
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
# 1. BILLING_QUEUE_SPIKE — net queue > 5 for 3+ minutes → CRITICAL
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_queue_spike_critical(client):
    """Ingesting 6 BILLING_QUEUE_JOIN events in the last 3 minutes triggers CRITICAL."""
    store_id = "STORE_SPIKE_01"
    events = []

    # 6 distinct visitors join queue in the last 1 minute
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    for i in range(6):
        vid = f"VIS_{i:06x}"
        events.append({
            "event_id": str(uuid.uuid4()),
            "store_id": store_id,
            "camera_id": "CAM_BILL_01",
            "visitor_id": vid,
            "event_type": "BILLING_QUEUE_JOIN",
            "timestamp": (now - timedelta(seconds=30 + i * 5)).isoformat().replace("+00:00", "Z"),
            "zone_id": "BILLING",
            "dwell_ms": None,
            "is_staff": False,
            "confidence": 0.9,
            "metadata": {"queue_depth": i + 1, "sku_zone": None, "session_seq": None},
        })

    r = await client.post("/events/ingest", json={"events": events})
    assert r.status_code == 200

    r = await client.get(f"/stores/{store_id}/anomalies")
    assert r.status_code == 200
    body = r.json()

    spikes = [a for a in body["anomalies"] if a["type"] == "BILLING_QUEUE_SPIKE"]
    assert len(spikes) >= 1, f"Expected BILLING_QUEUE_SPIKE, got: {body['anomalies']}"
    assert spikes[0]["severity"] == "CRITICAL"


@pytest.mark.anyio
async def test_queue_spike_not_triggered_below_threshold(client):
    """Only 4 queue joins (< 5 threshold) must NOT trigger a spike anomaly."""
    store_id = "STORE_SPIKE_02"
    now = datetime.now(timezone.utc)
    from datetime import timedelta

    events = [
        {
            "event_id": str(uuid.uuid4()),
            "store_id": store_id,
            "camera_id": "CAM_BILL_01",
            "visitor_id": f"VIS_{i:06x}",
            "event_type": "BILLING_QUEUE_JOIN",
            "timestamp": (now - timedelta(seconds=30 + i * 5)).isoformat().replace("+00:00", "Z"),
            "zone_id": "BILLING",
            "dwell_ms": None,
            "is_staff": False,
            "confidence": 0.9,
            "metadata": {"queue_depth": i + 1, "sku_zone": None, "session_seq": None},
        }
        for i in range(4)
    ]

    r = await client.post("/events/ingest", json={"events": events})
    assert r.status_code == 200

    r = await client.get(f"/stores/{store_id}/anomalies")
    assert r.status_code == 200
    body = r.json()

    spikes = [a for a in body["anomalies"] if a["type"] == "BILLING_QUEUE_SPIKE"]
    assert len(spikes) == 0, f"Unexpected spike with only 4 queue joins"


# ---------------------------------------------------------------------------
# 2. CONVERSION_DROP — today < 7-day avg × 0.8 → WARN
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_conversion_drop_warn(client):
    """
    Seed 7 days of history with 50% conversion, then today with 20%
    (< 50% × 0.8 = 40%) → should detect CONVERSION_DROP WARN.
    """
    store_id = "STORE_DROP_01"
    events = []

    # 7 days ago to 1 day ago: 10 visitors each day, 5 converted (50%)
    for days_ago in range(1, 8):
        for i in range(10):
            vid = f"VIS_{days_ago:02d}{i:04x}"
            events.append(_event(store_id, vid, "ENTRY", days_ago=days_ago, hour=9, minute=i))

    r = await client.post("/events/ingest", json={"events": events})
    assert r.status_code == 200

    # Mark 5 sessions per historical day as converted by patching sessions directly
    import aiosqlite
    db_path = os.environ["DB_PATH"]
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        # Mark the first 35 sessions as converted (5 per day × 7 days)
        async with db.execute("SELECT session_id FROM sessions ORDER BY entry_time LIMIT 35") as cur:
            rows = await cur.fetchall()
        for row in rows:
            await db.execute(
                "UPDATE sessions SET converted = 1, basket_value_inr = 500.0 WHERE session_id = ?",
                (row[0],),
            )
        await db.commit()

    # Today: 10 visitors, only 2 converted (20%)
    today_events = []
    for i in range(10):
        vid = f"VIS_td{i:04x}"
        today_events.append(_event(store_id, vid, "ENTRY", days_ago=0, hour=10, minute=i * 5))

    r = await client.post("/events/ingest", json={"events": today_events})
    assert r.status_code == 200

    # Mark only 2 today sessions converted
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT session_id FROM sessions WHERE entry_time >= date('now') ORDER BY entry_time LIMIT 2"
        ) as cur:
            rows = await cur.fetchall()
        for row in rows:
            await db.execute(
                "UPDATE sessions SET converted = 1, basket_value_inr = 300.0 WHERE session_id = ?",
                (row[0],),
            )
        await db.commit()

    r = await client.get(f"/stores/{store_id}/anomalies")
    assert r.status_code == 200
    body = r.json()

    drops = [a for a in body["anomalies"] if a["type"] == "CONVERSION_DROP"]
    assert len(drops) >= 1, f"Expected CONVERSION_DROP, got: {body['anomalies']}"
    assert drops[0]["severity"] == "WARN"


# ---------------------------------------------------------------------------
# 3. DEAD_ZONE — zone with 0 visits in last 30 min → INFO
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_dead_zone_detection(client):
    """
    A zone that was active today but has had 0 visits in the last 30 minutes
    should be reported as DEAD_ZONE INFO.
    """
    store_id = "STORE_DEAD_01"
    from datetime import timedelta

    now = datetime.now(timezone.utc)

    # Zone was visited 2 hours ago but not in the last 30 minutes
    old_ts = (now - timedelta(hours=2)).isoformat().replace("+00:00", "Z")

    events = [
        {
            "event_id": str(uuid.uuid4()),
            "store_id": store_id,
            "camera_id": "CAM_FLOOR_01",
            "visitor_id": "VIS_old001",
            "event_type": "ZONE_ENTER",
            "timestamp": old_ts,
            "zone_id": "ELECTRONICS",
            "dwell_ms": None,
            "is_staff": False,
            "confidence": 0.9,
            "metadata": {"queue_depth": None, "sku_zone": None, "session_seq": None},
        }
    ]

    r = await client.post("/events/ingest", json={"events": events})
    assert r.status_code == 200

    r = await client.get(f"/stores/{store_id}/anomalies")
    assert r.status_code == 200
    body = r.json()

    dead_zones = [a for a in body["anomalies"] if a["type"] == "DEAD_ZONE"]
    assert len(dead_zones) >= 1, f"Expected DEAD_ZONE anomaly, got: {body['anomalies']}"
    assert dead_zones[0]["severity"] == "INFO"
    assert "ELECTRONICS" in dead_zones[0]["description"]


@pytest.mark.anyio
async def test_no_dead_zone_when_recently_visited(client):
    """A zone visited in the last 5 minutes must NOT be flagged as DEAD_ZONE."""
    store_id = "STORE_DEAD_02"
    from datetime import timedelta

    now = datetime.now(timezone.utc)
    recent_ts = (now - timedelta(minutes=5)).isoformat().replace("+00:00", "Z")

    events = [
        {
            "event_id": str(uuid.uuid4()),
            "store_id": store_id,
            "camera_id": "CAM_FLOOR_01",
            "visitor_id": "VIS_recent1",
            "event_type": "ZONE_ENTER",
            "timestamp": recent_ts,
            "zone_id": "FASHION",
            "dwell_ms": None,
            "is_staff": False,
            "confidence": 0.9,
            "metadata": {"queue_depth": None, "sku_zone": None, "session_seq": None},
        }
    ]

    r = await client.post("/events/ingest", json={"events": events})
    assert r.status_code == 200

    r = await client.get(f"/stores/{store_id}/anomalies")
    assert r.status_code == 200
    body = r.json()

    fashion_dead = [
        a for a in body["anomalies"]
        if a["type"] == "DEAD_ZONE" and "FASHION" in a.get("description", "")
    ]
    assert len(fashion_dead) == 0, "Zone visited 5 min ago should not be DEAD_ZONE"
