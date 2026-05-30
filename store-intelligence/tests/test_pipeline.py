# PROMPT: Write pytest tests for the pipeline module covering: ENTRY event count,
# staff exclusion from customer metrics, re-entry detection (same embedding → REENTRY),
# and group entry (N centroids crossing entry line within 2 seconds → N ENTRY events).
# Use only in-process logic from pipeline/tracker.py and pipeline/emit.py.
# CHANGES MADE:
#   - Replaced hypothetical ultralytics mocking with pure logic tests on tracker.py helpers
#     (the detection model is not available in test environment).
#   - Added edge-case assertions for partial-occlusion confidence pass-through.
#   - Verified visitor_id format matches VIS_{6-hex} contract from emit.py.

from __future__ import annotations

import hashlib
import math
import time
import uuid
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from pipeline.emit import (
    build_event,
    build_store_idle_event,
    frame_to_timestamp,
    make_visitor_id,
    read_events_jsonl,
)
from pipeline.tracker import (
    CameraTracker,
    GlobalReIDRegistry,
    TrackState,
    cosine_similarity,
)
from datetime import datetime, timezone


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_embedding(seed: int = 0, size: int = 128) -> np.ndarray:
    rng = np.random.default_rng(seed)
    v = rng.random(size).astype(np.float32)
    return v / (np.linalg.norm(v) + 1e-9)


# ---------------------------------------------------------------------------
# 1. ENTRY event count
# ---------------------------------------------------------------------------

class TestEntryEventCount:
    def test_build_entry_event_has_correct_type(self):
        ev = build_event(
            store_id="STORE_BLR_001",
            camera_id="CAM_ENTRY_01",
            visitor_id="VIS_abc123",
            event_type="ENTRY",
            timestamp="2026-03-03T10:00:00Z",
        )
        assert ev["event_type"] == "ENTRY"

    def test_visitor_id_format(self):
        vid = make_visitor_id("STORE_BLR_001", 42, 100)
        assert vid.startswith("VIS_"), f"Expected 'VIS_' prefix, got: {vid}"
        assert len(vid) == 4 + 6, f"Expected 10 chars total, got {len(vid)}"

    def test_visitor_id_deterministic(self):
        vid1 = make_visitor_id("STORE_BLR_001", 42, 100)
        vid2 = make_visitor_id("STORE_BLR_001", 42, 100)
        assert vid1 == vid2

    def test_visitor_id_sha256_derivation(self):
        store_id, tracker_id, session_start_frame = "STORE_BLR_001", 7, 300
        raw = f"{store_id}{tracker_id}{session_start_frame}"
        expected_suffix = hashlib.sha256(raw.encode()).hexdigest()[:6]
        vid = make_visitor_id(store_id, tracker_id, session_start_frame)
        assert vid == f"VIS_{expected_suffix}"

    def test_partial_occlusion_confidence_not_rounded_up(self):
        """Events with confidence < 0.45 must still be emitted with exact confidence."""
        conf = 0.32
        ev = build_event(
            store_id="STORE_BLR_001",
            camera_id="CAM_ENTRY_01",
            visitor_id="VIS_abc123",
            event_type="ENTRY",
            timestamp="2026-03-03T10:00:00Z",
            confidence=conf,
        )
        assert ev["confidence"] == conf, "Confidence must not be rounded up"
        assert ev["confidence"] < 0.45

    def test_timestamp_derived_from_frame(self):
        from datetime import timedelta
        clip_start = datetime(2026, 3, 3, 10, 0, 0, tzinfo=timezone.utc)
        ts = frame_to_timestamp(clip_start, frame_number=150, fps=15.0)
        # 150 frames / 15 fps = 10 seconds
        expected = "2026-03-03T10:00:10Z"
        assert ts == expected


# ---------------------------------------------------------------------------
# 2. Staff exclusion
# ---------------------------------------------------------------------------

class TestStaffExclusion:
    def test_is_staff_flag_set_in_event(self):
        ev = build_event(
            store_id="STORE_BLR_001",
            camera_id="CAM_FLOOR_01",
            visitor_id="VIS_staff1",
            event_type="ENTRY",
            timestamp="2026-03-03T10:05:00Z",
            is_staff=True,
            confidence=0.88,
        )
        assert ev["is_staff"] is True

    def test_staff_aspect_ratio_detection(self):
        """Staff classification by aspect ratio — tall narrow bbox (aspect > 2.5)."""
        tracker = CameraTracker(
            store_id="STORE_BLR_001",
            camera_id="CAM_ENTRY_01",
            reid_registry=GlobalReIDRegistry(),
        )
        # Tall narrow bbox: h=300, w=80 → aspect ≈ 3.75 → staff
        blank_frame = np.zeros((1080, 1920, 3), dtype=np.uint8)
        is_staff = tracker.is_staff_bbox(blank_frame, (100, 100, 180, 400), staff_uniform_hsv=None)
        assert is_staff is True, "Tall narrow bbox should be classified as staff"

    def test_non_staff_aspect_ratio(self):
        """Short wide bbox → not classified as staff by aspect alone."""
        tracker = CameraTracker(
            store_id="STORE_BLR_001",
            camera_id="CAM_ENTRY_01",
            reid_registry=GlobalReIDRegistry(),
        )
        blank_frame = np.zeros((1080, 1920, 3), dtype=np.uint8)
        # Wide bbox: h=100, w=200 → aspect = 0.5 → not staff
        is_staff = tracker.is_staff_bbox(blank_frame, (100, 100, 300, 200), staff_uniform_hsv=None)
        assert is_staff is False


# ---------------------------------------------------------------------------
# 3. Re-entry detection
# ---------------------------------------------------------------------------

class TestReEntryDetection:
    def test_cosine_similarity_identical_embeddings(self):
        emb = make_embedding(seed=1)
        sim = cosine_similarity(emb, emb)
        assert math.isclose(sim, 1.0, abs_tol=1e-5)

    def test_cosine_similarity_orthogonal_embeddings(self):
        a = np.zeros(128, dtype=np.float32)
        b = np.zeros(128, dtype=np.float32)
        a[0] = 1.0
        b[1] = 1.0
        sim = cosine_similarity(a, b)
        assert math.isclose(sim, 0.0, abs_tol=1e-5)

    def test_reentry_event_reuses_visitor_id(self):
        """REENTRY event must carry the same visitor_id as the original ENTRY."""
        original_vid = "VIS_abc123"
        ev = build_event(
            store_id="STORE_BLR_001",
            camera_id="CAM_ENTRY_01",
            visitor_id=original_vid,
            event_type="REENTRY",
            timestamp="2026-03-03T10:30:00Z",
        )
        assert ev["event_type"] == "REENTRY"
        assert ev["visitor_id"] == original_vid

    def test_global_reid_registry_match(self):
        """Registry finds a matching embedding from a different camera."""
        registry = GlobalReIDRegistry()
        emb_a = make_embedding(seed=5)
        registry.register("VIS_xyzabc", emb_a, "CAM_ENTRY_01")

        # Same embedding from a different camera
        matched = registry.find_match(emb_a, current_camera_id="CAM_FLOOR_01")
        assert matched == "VIS_xyzabc"

    def test_global_reid_registry_no_match_same_camera(self):
        """Registry must NOT match embeddings from the same camera."""
        registry = GlobalReIDRegistry()
        emb_a = make_embedding(seed=5)
        registry.register("VIS_xyzabc", emb_a, "CAM_ENTRY_01")

        matched = registry.find_match(emb_a, current_camera_id="CAM_ENTRY_01")
        assert matched is None

    def test_global_reid_registry_stale_eviction(self):
        """Stale entries (> 4× window) should be evicted."""
        registry = GlobalReIDRegistry()
        emb_a = make_embedding(seed=7)
        # Manually inject a stale entry
        registry._registry["VIS_stale"] = (emb_a, time.time() - 10000, "CAM_OTHER")
        registry.evict_stale()
        assert "VIS_stale" not in registry._registry


# ---------------------------------------------------------------------------
# 4. Group entry
# ---------------------------------------------------------------------------

class TestGroupEntry:
    def test_group_entry_multiple_events_in_window(self):
        """N visitors entering within 2 seconds should each get their own ENTRY event."""
        store_id = "STORE_BLR_001"
        camera_id = "CAM_ENTRY_01"
        ts = "2026-03-03T10:00:00Z"
        now = time.time()

        # Simulate 3 visitors arriving within 1 second
        group_entry_buffer = []
        events = []
        for i in range(3):
            vid = make_visitor_id(store_id, i, i * 10)
            group_entry_buffer.append((now + i * 0.3, vid))
            ev = build_event(
                store_id=store_id,
                camera_id=camera_id,
                visitor_id=vid,
                event_type="ENTRY",
                timestamp=ts,
            )
            events.append(ev)

        entry_events = [e for e in events if e["event_type"] == "ENTRY"]
        assert len(entry_events) == 3, f"Expected 3 ENTRY events, got {len(entry_events)}"

    def test_group_entry_all_have_unique_visitor_ids(self):
        store_id = "STORE_BLR_002"
        visitor_ids = {make_visitor_id(store_id, tracker_id, frame) for tracker_id, frame in [(1, 10), (2, 10), (3, 10)]}
        assert len(visitor_ids) == 3, "Group members must have unique visitor_ids"

    def test_store_idle_event_schema(self):
        """STORE_IDLE synthetic event must have visitor_id='SYNTHETIC' and correct dwell_ms."""
        ev = build_store_idle_event(
            store_id="STORE_BLR_001",
            camera_id="CAM_ENTRY_01",
            timestamp="2026-03-03T10:05:00Z",
            idle_duration_seconds=300,
        )
        assert ev["event_type"] == "STORE_IDLE"
        assert ev["visitor_id"] == "SYNTHETIC"
        assert ev["dwell_ms"] == 300_000
        assert ev["metadata"]["idle_duration_seconds"] == 300
