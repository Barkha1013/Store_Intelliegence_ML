# Store Intelligence

Offline retail analytics pipeline for Apex Retail — transforms raw CCTV footage into
real-time store metrics via a 4-stage system: Detection → Event Stream → REST API → Dashboard.

## Quick Start

```bash
git clone <repo>
cd store-intelligence
cp .env.example .env
docker compose up --build
# Then: docker compose exec pipeline bash pipeline/run.sh
```

**API is served at:** `http://localhost:8000`

**Dashboard:** run `python dashboard/live.py STORE_BLR_002`

## Architecture

```
CCTV clips  →  YOLOv8n + ByteTrack  →  events.jsonl  →  FastAPI + SQLite  →  REST API
```

See `docs/DESIGN.md` for full architecture overview, data flow, and AI-assisted design decisions.
See `docs/CHOICES.md` for deep-dives on the three key architecture choices.

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/events/ingest` | Batch ingest up to 500 events (idempotent) |
| `GET`  | `/stores/{id}/metrics` | Unique visitors, conversion rate, queue depth |
| `GET`  | `/stores/{id}/funnel` | Entry → zone visit → billing queue → purchase funnel |
| `GET`  | `/stores/{id}/heatmap` | Per-zone visit frequency and dwell heatmap |
| `GET`  | `/stores/{id}/anomalies` | Queue spikes, conversion drops, dead zones, stale cameras |
| `GET`  | `/health` | System health, stale feeds, DB status |

Interactive docs: `http://localhost:8000/docs`

## Running Tests

```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest tests/ -v --cov=app --cov-report=term-missing
```

Expected coverage: >70%.

Run assertions from the dataset:
```bash
pytest assertions.py -v
```

## Data Directory

Place dataset files at:

```
/data/
├── clips/
│   └── {STORE_ID}/{CAMERA_ID}.mp4
├── store_layout.json
├── pos_transactions.csv
└── sample_events.jsonl
```

## Environment Variables

See `.env.example` for all configurable values. Key ones:

| Variable | Default | Description |
|----------|---------|-------------|
| `DB_PATH` | `/data/store_intelligence.db` | SQLite database path |
| `EVENTS_JSONL` | `/data/events.jsonl` | Pipeline output JSONL |
| `YOLO_MODEL` | `yolov8n.pt` | YOLOv8 model weights (auto-downloaded) |
| `API_URL` | `http://api:8000` | API base URL for pipeline ingest |
| `PORT` | `8000` | API server port |

## Project Structure

```
store-intelligence/
├── pipeline/
│   ├── detect.py       # YOLOv8n + ByteTrack + Re-ID detection script
│   ├── tracker.py      # Re-ID registry and camera tracker state
│   ├── emit.py         # Event schema builder and JSONL emission
│   └── run.sh          # Single command: process all clips → ingest
├── app/
│   ├── main.py         # FastAPI entrypoint + structlog setup
│   ├── models.py       # Pydantic event schema + response models
│   ├── db.py           # aiosqlite connection, table init, WAL mode
│   ├── sessions.py     # Session materialisation helpers
│   ├── ingestion.py    # POST /events/ingest
│   ├── metrics.py      # GET /stores/{id}/metrics
│   ├── funnel.py       # GET /stores/{id}/funnel
│   ├── heatmap.py      # GET /stores/{id}/heatmap
│   ├── anomalies.py    # GET /stores/{id}/anomalies
│   └── health.py       # GET /health
├── dashboard/
│   └── live.py         # Rich terminal dashboard (replays events at 10×)
├── tests/
│   ├── test_pipeline.py   # ENTRY count, staff exclusion, re-entry, group entry
│   ├── test_metrics.py    # Conversion rate, funnel dedup, heatmap confidence, idempotency
│   └── test_anomalies.py  # Queue spike, conversion drop, dead zone
├── docs/
│   ├── DESIGN.md       # Architecture, data flow, AI decisions, limitations
│   └── CHOICES.md      # 3 deep-dive architecture decisions
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
└── .env.example
```

## Key Design Decisions

1. **YOLOv8n** chosen for detection: real-time CPU performance with native ByteTrack integration. YOLOv8s is a drop-in upgrade for higher accuracy.
2. **visitor_id** is deterministic: `VIS_` + `sha256(store_id + tracker_id + session_start_frame)[:6]`. Pipeline re-runs produce identical IDs — safe for idempotent ingest.
3. **Sessions materialised at ingest time**: funnel and conversion queries hit a pre-computed `sessions` table, not raw event counts.
4. **All timestamps** are `clip_start_datetime + (frame_number / fps)` — never system time. Events are temporally correct for historical clip processing.
5. **Partial ingest success**: 497 valid events in a 500-event batch are ingested even if 3 are malformed. Errors are returned per-event.
