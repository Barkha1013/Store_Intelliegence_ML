"""
Event schema + emission to JSONL.

Responsible for constructing StoreEvent objects and writing them to disk
(events.jsonl) for downstream ingestion by the API.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Append to this path from detect.py and tracker.py
EVENTS_JSONL_PATH = os.environ.get("EVENTS_JSONL_PATH", "/data/events.jsonl")

_file_lock = threading.Lock()


# ---------------------------------------------------------------------------
# visitor_id generation
# ---------------------------------------------------------------------------

def make_visitor_id(store_id: str, tracker_id: int, session_start_frame: int) -> str:
    """
    visitor_id = 'VIS_' + first 6 hex chars of sha256(store_id + str(tracker_id) + str(session_start_frame))
    """
    raw = f"{store_id}{tracker_id}{session_start_frame}"
    digest = hashlib.sha256(raw.encode()).hexdigest()
    return f"VIS_{digest[:6]}"


# ---------------------------------------------------------------------------
# Timestamp derivation
# ---------------------------------------------------------------------------

def frame_to_timestamp(clip_start_datetime: datetime, frame_number: int, fps: float) -> str:
    """
    Critical requirement: timestamp = clip_start_datetime + (frame_number / fps) seconds.
    Never uses system time.
    """
    offset_seconds = frame_number / fps
    from datetime import timedelta
    ts = clip_start_datetime + timedelta(seconds=offset_seconds)
    # Force UTC, ISO-8601
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Event builder
# ---------------------------------------------------------------------------

def build_event(
    store_id: str,
    camera_id: str,
    visitor_id: str,
    event_type: str,
    timestamp: str,
    zone_id: Optional[str] = None,
    dwell_ms: Optional[int] = None,
    is_staff: bool = False,
    confidence: float = 1.0,
    queue_depth: Optional[int] = None,
    sku_zone: Optional[str] = None,
    session_seq: Optional[int] = None,
) -> dict:
    """
    Build a raw event dict matching the StoreEvent schema.
    confidence is stored exactly as given — never rounded up.
    """
    return {
        "event_id": str(uuid.uuid4()),
        "store_id": store_id,
        "camera_id": camera_id,
        "visitor_id": visitor_id,
        "event_type": event_type,
        "timestamp": timestamp,
        "zone_id": zone_id,
        "dwell_ms": dwell_ms,
        "is_staff": is_staff,
        "confidence": confidence,  # exact — never round up
        "metadata": {
            "queue_depth": queue_depth,
            "sku_zone": sku_zone,
            "session_seq": session_seq,
        },
    }


def build_store_idle_event(
    store_id: str,
    camera_id: str,
    timestamp: str,
    idle_duration_seconds: int,
) -> dict:
    """
    STORE_IDLE is a synthetic event emitted when no detections occur for 5+ minutes.
    Schema extension: dwell_ms holds idle duration, zone_id is null,
    visitor_id is 'SYNTHETIC', metadata carries idle_duration_seconds.
    """
    return {
        "event_id": str(uuid.uuid4()),
        "store_id": store_id,
        "camera_id": camera_id,
        "visitor_id": "SYNTHETIC",
        "event_type": "STORE_IDLE",
        "timestamp": timestamp,
        "zone_id": None,
        "dwell_ms": idle_duration_seconds * 1000,
        "is_staff": False,
        "confidence": 1.0,
        "metadata": {
            "queue_depth": None,
            "sku_zone": None,
            "session_seq": None,
            "idle_duration_seconds": idle_duration_seconds,
        },
    }


# ---------------------------------------------------------------------------
# Emission
# ---------------------------------------------------------------------------

def emit_event(event: dict, jsonl_path: str = EVENTS_JSONL_PATH) -> None:
    """Thread-safe append to JSONL file."""
    line = json.dumps(event, default=str) + "\n"
    with _file_lock:
        os.makedirs(os.path.dirname(jsonl_path) if os.path.dirname(jsonl_path) else ".", exist_ok=True)
        with open(jsonl_path, "a", encoding="utf-8") as f:
            f.write(line)


def emit_events_batch(events: list[dict], jsonl_path: str = EVENTS_JSONL_PATH) -> None:
    """Batch-append multiple events atomically under a single lock acquisition."""
    lines = [json.dumps(e, default=str) + "\n" for e in events]
    with _file_lock:
        os.makedirs(os.path.dirname(jsonl_path) if os.path.dirname(jsonl_path) else ".", exist_ok=True)
        with open(jsonl_path, "a", encoding="utf-8") as f:
            f.writelines(lines)


def read_events_jsonl(jsonl_path: str = EVENTS_JSONL_PATH) -> list[dict]:
    """Read all events from the JSONL file."""
    path = Path(jsonl_path)
    if not path.exists():
        return []
    events = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return events
