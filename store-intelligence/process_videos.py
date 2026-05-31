"""
process_videos.py — Drop your CCTV recordings into data/videos/, then run this.

Usage:
    cd store-intelligence
    python process_videos.py

What it does:
  1. Scans data/videos/ for any .mp4 / .avi / .mov / .mkv files
  2. Assigns each to a camera ID (CAM_01 through CAM_05)
  3. Runs YOLOv8n detection + ByteTrack on each clip
  4. Writes events to data/events.jsonl
  5. Ingests events into the API (if running on localhost:8000)
"""

from __future__ import annotations

import json
import os
import sys
import subprocess
from datetime import datetime, timezone
from pathlib import Path

# ── Config ──────────────────────────────────────────────────────────────────

STORE_ID     = "STORE_BLR_001"
VIDEOS_DIR   = Path(__file__).parent / "data" / "videos"
CLIPS_DIR    = Path(__file__).parent / "data" / "clips" / STORE_ID
OUTPUT_JSONL = Path(__file__).parent / "data" / "events.jsonl"
LAYOUT_PATH  = Path(__file__).parent / "data" / "store_layout.json"
DB_PATH      = Path(__file__).parent / "data" / "store_intelligence.db"
API_URL      = os.environ.get("API_URL", "http://localhost:8000")

SUPPORTED_EXTS = {".mp4", ".avi", ".mov", ".mkv", ".MP4", ".AVI", ".MOV", ".MKV"}

CAMERA_IDS = [
    "CAM_ENTRY_01",
    "CAM_FLOOR_01",
    "CAM_BILL_01",
    "CAM_FLOOR_02",
    "CAM_FLOOR_03",
]

# ── Helpers ──────────────────────────────────────────────────────────────────

def find_videos() -> list[Path]:
    videos = sorted(
        p for p in VIDEOS_DIR.iterdir()
        if p.is_file() and p.suffix in SUPPORTED_EXTS
    )
    return videos


def symlink_clips(videos: list[Path]) -> list[tuple[str, Path]]:
    """
    Create symlinks from data/clips/STORE_BLR_001/<camera_id>.mp4
    pointing to each uploaded video file.
    Returns list of (camera_id, clip_path) pairs.
    """
    CLIPS_DIR.mkdir(parents=True, exist_ok=True)
    pairs = []
    for i, video in enumerate(videos):
        cam_id = CAMERA_IDS[i] if i < len(CAMERA_IDS) else f"CAM_EXTRA_{i:02d}"
        clip_path = CLIPS_DIR / f"{cam_id}.mp4"
        # Remove stale symlink/file
        if clip_path.exists() or clip_path.is_symlink():
            clip_path.unlink()
        clip_path.symlink_to(video.resolve())
        pairs.append((cam_id, clip_path))
        print(f"  {video.name}  →  {cam_id}")
    return pairs


def update_layout_cameras(pairs: list[tuple[str, Path]]) -> None:
    """Patch store_layout.json so the pipeline knows about all cameras."""
    with open(LAYOUT_PATH, encoding="utf-8") as f:
        layout = json.load(f)

    store = layout["stores"][STORE_ID]
    start_time = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    cameras: dict = {}
    for cam_id, _ in pairs:
        role = (
            "entry"   if "ENTRY" in cam_id else
            "billing" if "BILL"  in cam_id else
            "floor"
        )
        cameras[cam_id] = {"start_time": start_time, "role": role}

    store["cameras"] = cameras
    layout["stores"][STORE_ID] = store

    with open(LAYOUT_PATH, "w", encoding="utf-8") as f:
        json.dump(layout, f, indent=2)

    print(f"  Updated store_layout.json with {len(cameras)} cameras")


def run_pipeline() -> bool:
    """Run the detect.py pipeline on all clips."""
    env = {
        **os.environ,
        "PYTHONPATH": str(Path(__file__).parent),
        "DB_PATH": str(DB_PATH),
    }
    cmd = [
        sys.executable, "-m", "pipeline.detect",
        "--layout", str(LAYOUT_PATH),
        "--clips",  str(CLIPS_DIR.parent),   # parent of STORE_BLR_001/
        "--pos",    str(Path(__file__).parent / "data" / "pos_transactions.csv"),
        "--output", str(OUTPUT_JSONL),
        "--model",  "yolov8n.pt",
    ]
    print(f"\n[pipeline] Running: {' '.join(cmd)}\n")
    result = subprocess.run(cmd, env=env)
    return result.returncode == 0


def ingest_into_sqlite() -> None:
    """Ingest events.jsonl directly into SQLite — no HTTP server needed."""
    import asyncio
    import aiosqlite as _aiosq

    if not OUTPUT_JSONL.exists():
        print("[ingest] No events file, skipping")
        return

    events = []
    with open(OUTPUT_JSONL, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    pass

    if not events:
        print("[ingest] No events to ingest")
        return

    os.environ["DB_PATH"] = str(DB_PATH)

    async def _ingest() -> None:
        sys.path.insert(0, str(Path(__file__).parent))
        from app.main import create_app  # type: ignore
        from httpx import AsyncClient, ASGITransport  # type: ignore

        app = create_app()
        batch_size = 200
        ingested = 0
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            for i in range(0, len(events), batch_size):
                chunk = events[i : i + batch_size]
                r = await client.post("/events/ingest", json={"events": chunk})
                body = r.json()
                ingested += body.get("ingested", 0)
        print(f"[ingest] Done — ingested {ingested} events into {DB_PATH}")

    asyncio.run(_ingest())


def export_json() -> None:
    """Export SQLite data to JSON for the Node.js dashboard API."""
    export_script = Path(__file__).parent / "export_dashboard_data.py"
    if not export_script.exists():
        print("[export] export_dashboard_data.py not found, skipping")
        return
    env = {**os.environ, "DB_PATH": str(DB_PATH), "PYTHONPATH": str(Path(__file__).parent)}
    result = subprocess.run([sys.executable, str(export_script)], env=env)
    if result.returncode != 0:
        print("[export] Export failed — check output above")


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    print("=" * 60)
    print("  Store Intelligence — Video Processing")
    print("=" * 60)

    # 1. Find videos
    videos = find_videos()
    if not videos:
        print(f"\n[ERROR] No video files found in {VIDEOS_DIR}")
        print("  Drop your .mp4 / .avi / .mov files into:")
        print(f"  {VIDEOS_DIR}")
        sys.exit(1)

    print(f"\n[1/4] Found {len(videos)} video(s):")
    for v in videos:
        size_mb = v.stat().st_size / 1_048_576
        print(f"  {v.name}  ({size_mb:.1f} MB)")

    # 2. Symlink to clips dir
    print(f"\n[2/4] Mapping to camera IDs:")
    pairs = symlink_clips(videos)

    # 3. Update layout
    print(f"\n[3/4] Patching store_layout.json ...")
    update_layout_cameras(pairs)

    # 4. Run pipeline
    print(f"\n[4/4] Running YOLOv8 detection pipeline ...")
    ok = run_pipeline()
    if not ok:
        print("\n[ERROR] Pipeline failed — check output above")
        sys.exit(1)

    # 5. Ingest into SQLite directly (no HTTP server needed)
    ingest_into_sqlite()

    # 6. Export JSON for the Node.js dashboard API
    export_json()

    print("\n✓ Done! Refresh the dashboard to see real data.")
    print(f"  Events written to: {OUTPUT_JSONL}")


if __name__ == "__main__":
    main()
