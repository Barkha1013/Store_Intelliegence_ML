"""
Main detection + tracking script.

Model: YOLOv8n (justified in CHOICES.md — real-time speed at 15 fps is the
primary constraint; YOLOv8n achieves ~180 fps on a single GPU / ~40 fps CPU,
leaving headroom for Re-ID and zone logic).

Tracking: ByteTrack (built into ultralytics) — one tracker instance per camera.

Usage:
    python pipeline/detect.py --layout /data/store_layout.json --clips /data/clips \
                               --pos /data/pos_transactions.csv --output /data/events.jsonl
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import numpy as np

from pipeline.emit import (
    EVENTS_JSONL_PATH,
    build_event,
    build_store_idle_event,
    emit_events_batch,
    frame_to_timestamp,
    make_visitor_id,
)
from pipeline.tracker import (
    IDLE_THRESHOLD_SECONDS,
    CameraTracker,
    GlobalReIDRegistry,
    TrackState,
    cosine_similarity,
)

try:
    from ultralytics import YOLO  # type: ignore
    import cv2  # type: ignore
    ULTRALYTICS_AVAILABLE = True
except ImportError:
    ULTRALYTICS_AVAILABLE = False


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GROUP_ENTRY_WINDOW_SECONDS = 2.0
REENTRY_COSINE_THRESHOLD = 0.80
BILLING_QUEUE_ABANDON_WINDOW_SECONDS = 300  # 5 minutes
PARTIAL_OCCLUSION_CONFIDENCE_THRESHOLD = 0.45  # events still emitted below this


# ---------------------------------------------------------------------------
# POS transaction loading
# ---------------------------------------------------------------------------

def load_pos_transactions(csv_path: str) -> dict[str, list[dict]]:
    """
    Returns {store_id: [{"timestamp": datetime, "basket_value_inr": float, ...}]}
    """
    result: dict[str, list[dict]] = defaultdict(list)
    if not os.path.exists(csv_path):
        return result
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                ts = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
                result[row["store_id"]].append(
                    {
                        "transaction_id": row.get("transaction_id"),
                        "timestamp": ts,
                        "basket_value_inr": float(row.get("basket_value_inr", 0)),
                    }
                )
            except (KeyError, ValueError):
                continue
    return result


def has_pos_transaction_within(
    store_id: str,
    after_dt: datetime,
    window_seconds: int,
    pos_data: dict[str, list[dict]],
) -> bool:
    txns = pos_data.get(store_id, [])
    deadline = after_dt + timedelta(seconds=window_seconds)
    for txn in txns:
        ts = txn["timestamp"]
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if after_dt <= ts <= deadline:
            return True
    return False


# ---------------------------------------------------------------------------
# Entry line crossing
# ---------------------------------------------------------------------------

def crosses_entry_line(
    prev_cy: Optional[float],
    curr_cy: Optional[float],
    entry_line_y: float,
) -> bool:
    """True if centroid moved from above to below the entry line."""
    if prev_cy is None or curr_cy is None:
        return False
    return prev_cy < entry_line_y <= curr_cy


def crosses_exit_line(
    prev_cy: Optional[float],
    curr_cy: Optional[float],
    exit_line_y: float,
) -> bool:
    """True if centroid moved from below to above the exit line (reversed direction)."""
    if prev_cy is None or curr_cy is None:
        return False
    return prev_cy >= exit_line_y > curr_cy


# ---------------------------------------------------------------------------
# Main processing loop
# ---------------------------------------------------------------------------

def process_clip(
    clip_path: str,
    store_id: str,
    camera_id: str,
    clip_start_datetime: datetime,
    layout: dict,
    pos_data: dict[str, list[dict]],
    model: Any,
    tracker: CameraTracker,
    reid_registry: GlobalReIDRegistry,
    output_jsonl: str = EVENTS_JSONL_PATH,
) -> list[dict]:
    """
    Process a single CCTV clip and return a list of emitted events.
    Events are also appended to output_jsonl.
    """
    events: list[dict] = []

    zones = layout.get("zones", [])
    entry_line_y = layout.get("entry_line_y", 540)
    exit_line_y = layout.get("exit_line_y", 500)
    staff_uniform_hsv = layout.get("staff_uniform_hsv")
    fps = layout.get("fps", 15.0)
    billing_zone_id = layout.get("billing_zone_id", "BILLING")

    # Group-entry tracking: centroids that crossed entry within 2s
    group_entry_buffer: list[tuple[float, str]] = []  # (wall_time, visitor_id)

    # Per-track previous centroid for line crossing
    prev_cy: dict[int, float] = {}

    # Zone dwell start times: (visitor_id, zone_id) → frame_number
    zone_dwell_start: dict[tuple[str, str], int] = {}

    # Billing queue abandon candidates: visitor_id → (exit_frame, is_staff)
    billing_exit_pending: dict[str, tuple[int, bool]] = {}

    frame_number = 0
    last_idle_emit_frame = -1

    if not ULTRALYTICS_AVAILABLE:
        # Emit a placeholder STORE_IDLE if no vision libs available
        ts = frame_to_timestamp(clip_start_datetime, 0, fps)
        ev = build_store_idle_event(store_id, camera_id, ts, 0)
        events.append(ev)
        emit_events_batch(events, output_jsonl)
        return events

    cap = cv2.VideoCapture(clip_path)
    if not cap.isOpened():
        return events

    actual_fps = cap.get(cv2.CAP_PROP_FPS) or fps
    frame_skip = max(1, int(actual_fps / fps))  # process at target fps

    while True:
        ret, frame = cap.read()
        if not ret:
            break

        frame_number += 1
        if frame_number % frame_skip != 0:
            continue

        # --- Idle detection ---
        if tracker.seconds_since_last_detection() >= IDLE_THRESHOLD_SECONDS:
            idle_since_frame = frame_number - int(IDLE_THRESHOLD_SECONDS * fps)
            if last_idle_emit_frame != idle_since_frame:
                ts = frame_to_timestamp(clip_start_datetime, frame_number, fps)
                ev = build_store_idle_event(
                    store_id,
                    camera_id,
                    ts,
                    int(IDLE_THRESHOLD_SECONDS),
                )
                events.append(ev)
                last_idle_emit_frame = idle_since_frame

        # --- YOLOv8 + ByteTrack ---
        results = model.track(
            frame,
            persist=True,
            tracker="bytetrack.yaml",
            classes=[0],  # class 0 = person
            verbose=False,
        )

        if not results or results[0].boxes is None:
            prev_cy = {}
            continue

        tracker.mark_detection()
        boxes = results[0].boxes

        current_track_ids: set[int] = set()

        for box in boxes:
            if box.id is None:
                continue

            track_id = int(box.id.item())
            conf = float(box.conf.item())
            x1, y1, x2, y2 = (int(v) for v in box.xyxy[0].tolist())
            cx = (x1 + x2) / 2
            cy = (y1 + y2) / 2

            current_track_ids.add(track_id)
            timestamp_str = frame_to_timestamp(clip_start_datetime, frame_number, fps)

            # --- Assign/retrieve visitor_id ---
            if track_id not in tracker._active:
                embedding = tracker.extract_embedding(frame, (x1, y1, x2, y2))

                # Cross-camera dedup check
                matched_vid = reid_registry.find_match(embedding, camera_id)

                if matched_vid is not None:
                    # Same visitor from another camera — don't emit ENTRY
                    is_staff = tracker.is_staff_bbox(frame, (x1, y1, x2, y2), staff_uniform_hsv)
                    state = TrackState(
                        tracker_id=track_id,
                        visitor_id=matched_vid,
                        store_id=store_id,
                        camera_id=camera_id,
                        session_start_frame=frame_number,
                        last_seen_frame=frame_number,
                        last_seen_time=time.time(),
                        embedding=embedding,
                        is_staff=is_staff,
                    )
                    tracker._active[track_id] = state
                    reid_registry.register(matched_vid, embedding, camera_id)
                    prev_cy[track_id] = cy
                    continue

                # Re-entry check: same embedding as previously exited visitor?
                reentry_vid: Optional[str] = None
                for ex_vid, ex_state in tracker._exited.items():
                    if ex_state.embedding is not None:
                        sim = cosine_similarity(embedding, ex_state.embedding)
                        if sim >= REENTRY_COSINE_THRESHOLD:
                            reentry_vid = ex_vid
                            break

                visitor_id = make_visitor_id(store_id, track_id, frame_number)
                is_staff = tracker.is_staff_bbox(frame, (x1, y1, x2, y2), staff_uniform_hsv)

                if reentry_vid:
                    # Reuse existing visitor_id
                    visitor_id = reentry_vid
                    del tracker._exited[reentry_vid]
                    ev = build_event(
                        store_id=store_id,
                        camera_id=camera_id,
                        visitor_id=visitor_id,
                        event_type="REENTRY",
                        timestamp=timestamp_str,
                        is_staff=is_staff,
                        confidence=conf,
                        session_seq=0,
                    )
                    events.append(ev)
                else:
                    # New visitor — check for group entry
                    now_wall = time.time()
                    group_entry_buffer = [
                        (t, v)
                        for t, v in group_entry_buffer
                        if now_wall - t <= GROUP_ENTRY_WINDOW_SECONDS
                    ]
                    group_entry_buffer.append((now_wall, visitor_id))

                    ev = build_event(
                        store_id=store_id,
                        camera_id=camera_id,
                        visitor_id=visitor_id,
                        event_type="ENTRY",
                        timestamp=timestamp_str,
                        is_staff=is_staff,
                        confidence=conf,
                        session_seq=0,
                    )
                    events.append(ev)

                state = TrackState(
                    tracker_id=track_id,
                    visitor_id=visitor_id,
                    store_id=store_id,
                    camera_id=camera_id,
                    session_start_frame=frame_number,
                    last_seen_frame=frame_number,
                    last_seen_time=time.time(),
                    embedding=embedding,
                    is_staff=is_staff,
                )
                tracker._active[track_id] = state
                reid_registry.register(visitor_id, embedding, camera_id)

            else:
                state = tracker._active[track_id]
                state.last_seen_frame = frame_number
                state.last_seen_time = time.time()
                state.session_seq += 1

            visitor_id = state.visitor_id
            is_staff = state.is_staff

            # --- Exit line crossing ---
            p_cy = prev_cy.get(track_id)

            if crosses_exit_line(p_cy, cy, exit_line_y):
                ev = build_event(
                    store_id=store_id,
                    camera_id=camera_id,
                    visitor_id=visitor_id,
                    event_type="EXIT",
                    timestamp=timestamp_str,
                    is_staff=is_staff,
                    confidence=conf,
                    session_seq=state.session_seq,
                )
                events.append(ev)
                state.exited = True
                tracker._exited[visitor_id] = state
                del tracker._active[track_id]
                prev_cy.pop(track_id, None)
                continue

            prev_cy[track_id] = cy

            # --- Zone tracking ---
            zone_id = CameraTracker.resolve_zone(cx, cy, zones)

            # Zone enter / exit
            prev_zone = state.zones_visited[-1] if state.zones_visited else None
            if zone_id != prev_zone:
                if prev_zone is not None:
                    # Zone exit + dwell
                    dwell_key = (visitor_id, prev_zone)
                    start_frame = zone_dwell_start.pop(dwell_key, frame_number)
                    dwell_ms = int((frame_number - start_frame) / fps * 1000)
                    ev_exit = build_event(
                        store_id=store_id,
                        camera_id=camera_id,
                        visitor_id=visitor_id,
                        event_type="ZONE_EXIT",
                        timestamp=timestamp_str,
                        zone_id=prev_zone,
                        is_staff=is_staff,
                        confidence=conf,
                        session_seq=state.session_seq,
                    )
                    ev_dwell = build_event(
                        store_id=store_id,
                        camera_id=camera_id,
                        visitor_id=visitor_id,
                        event_type="ZONE_DWELL",
                        timestamp=timestamp_str,
                        zone_id=prev_zone,
                        dwell_ms=dwell_ms,
                        is_staff=is_staff,
                        confidence=conf,
                        session_seq=state.session_seq,
                    )
                    events.extend([ev_exit, ev_dwell])

                    # Billing queue abandon check
                    if prev_zone == billing_zone_id and state.in_billing_zone:
                        state.in_billing_zone = False
                        billing_exit_pending[visitor_id] = (frame_number, is_staff)
                        if tracker.queue_depth > 0:
                            tracker.queue_depth -= 1

                if zone_id is not None:
                    zone_enter_ts = frame_to_timestamp(clip_start_datetime, frame_number, fps)
                    ev_enter = build_event(
                        store_id=store_id,
                        camera_id=camera_id,
                        visitor_id=visitor_id,
                        event_type="ZONE_ENTER",
                        timestamp=zone_enter_ts,
                        zone_id=zone_id,
                        is_staff=is_staff,
                        confidence=conf,
                        session_seq=state.session_seq,
                    )
                    events.append(ev_enter)
                    zone_dwell_start[(visitor_id, zone_id)] = frame_number

                    # Billing queue join
                    if zone_id == billing_zone_id and not is_staff:
                        if tracker.queue_depth > 0:
                            ev_queue = build_event(
                                store_id=store_id,
                                camera_id=camera_id,
                                visitor_id=visitor_id,
                                event_type="BILLING_QUEUE_JOIN",
                                timestamp=zone_enter_ts,
                                zone_id=zone_id,
                                is_staff=is_staff,
                                confidence=conf,
                                queue_depth=tracker.queue_depth,
                                session_seq=state.session_seq,
                            )
                            events.append(ev_queue)
                        state.in_billing_zone = True
                        tracker.queue_depth += 1

                state.zones_visited.append(zone_id)  # type: ignore[arg-type]

            # --- Billing queue abandon retroactive check ---
            for vis_id, (exit_frame, v_is_staff) in list(billing_exit_pending.items()):
                elapsed_frames = frame_number - exit_frame
                elapsed_seconds = elapsed_frames / fps
                if elapsed_seconds >= BILLING_QUEUE_ABANDON_WINDOW_SECONDS:
                    exit_dt = clip_start_datetime + timedelta(seconds=exit_frame / fps)
                    if exit_dt.tzinfo is None:
                        exit_dt = exit_dt.replace(tzinfo=timezone.utc)
                    has_purchase = has_pos_transaction_within(
                        store_id,
                        exit_dt,
                        BILLING_QUEUE_ABANDON_WINDOW_SECONDS,
                        pos_data,
                    )
                    if not has_purchase:
                        abandon_ts = frame_to_timestamp(clip_start_datetime, frame_number, fps)
                        ev_abandon = build_event(
                            store_id=store_id,
                            camera_id=camera_id,
                            visitor_id=vis_id,
                            event_type="BILLING_QUEUE_ABANDON",
                            timestamp=abandon_ts,
                            zone_id=billing_zone_id,
                            is_staff=v_is_staff,
                            confidence=1.0,
                        )
                        events.append(ev_abandon)
                    del billing_exit_pending[vis_id]

    cap.release()
    emit_events_batch(events, output_jsonl)
    return events


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------

def load_layout(layout_path: str) -> dict:
    with open(layout_path, encoding="utf-8") as f:
        return json.load(f)


def main() -> None:
    parser = argparse.ArgumentParser(description="Store Intelligence Detection Pipeline")
    parser.add_argument("--layout", default="/data/store_layout.json")
    parser.add_argument("--clips", default="/data/clips")
    parser.add_argument("--pos", default="/data/pos_transactions.csv")
    parser.add_argument("--output", default=EVENTS_JSONL_PATH)
    parser.add_argument("--model", default="yolov8n.pt", help="YOLOv8 model weights")
    args = parser.parse_args()

    layout = load_layout(args.layout)
    pos_data = load_pos_transactions(args.pos)

    if not ULTRALYTICS_AVAILABLE:
        print("[WARN] ultralytics or cv2 not installed — pipeline will emit placeholder events only.")

    model = YOLO(args.model) if ULTRALYTICS_AVAILABLE else None
    reid_registry = GlobalReIDRegistry()

    clips_dir = Path(args.clips)
    clips_info = layout.get("clips", {})

    stores = layout.get("stores", {})

    # Iterate: store → camera → clip file
    for store_id, store_data in stores.items():
        store_layout = {**layout, **store_data}
        for camera_id, cam_data in store_data.get("cameras", {}).items():
            clip_file = clips_dir / store_id / f"{camera_id}.mp4"
            if not clip_file.exists():
                # Try alternative naming
                clip_file = clips_dir / f"{store_id}_{camera_id}.mp4"
            if not clip_file.exists():
                print(f"[SKIP] No clip found for {store_id}/{camera_id}")
                continue

            start_time_str = clips_info.get(camera_id, {}).get("start_time") or cam_data.get("start_time", "2026-03-03T10:00:00Z")
            clip_start_dt = datetime.fromisoformat(start_time_str.replace("Z", "+00:00"))

            cam_tracker = CameraTracker(
                store_id=store_id,
                camera_id=camera_id,
                reid_registry=reid_registry,
                fps=store_layout.get("fps", 15.0),
            )

            print(f"[INFO] Processing {store_id}/{camera_id} ...")
            events = process_clip(
                clip_path=str(clip_file),
                store_id=store_id,
                camera_id=camera_id,
                clip_start_datetime=clip_start_dt,
                layout=store_layout,
                pos_data=pos_data,
                model=model,
                tracker=cam_tracker,
                reid_registry=reid_registry,
                output_jsonl=args.output,
            )
            print(f"[INFO] {store_id}/{camera_id}: emitted {len(events)} events")

    print(f"[DONE] All clips processed. Events written to {args.output}")


if __name__ == "__main__":
    main()
