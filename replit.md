# Store Intelligence

Offline retail analytics pipeline for Apex Retail — transforms raw CCTV footage into real-time store metrics.

## Run & Operate

### Quick Start (Docker)
```bash
cd store-intelligence
cp .env.example .env
docker compose up --build
# Then: docker compose exec pipeline bash pipeline/run.sh
```

### Running Tests
```bash
cd store-intelligence
pytest tests/ -v                   # unit + integration tests (62 tests)
pytest assertions.py -v            # 10 spec assertions (CI gate)
```

### API Server (dev, outside Docker)
```bash
cd store-intelligence
DB_PATH=/tmp/dev.db uvicorn app.main:app --port 8000 --reload
```

### Dashboard
```bash
python store-intelligence/dashboard/live.py STORE_BLR_002 --api http://localhost:8000
```

## Stack

- Python 3.11, FastAPI, aiosqlite (async SQLite), Pydantic v2, structlog (JSON logs)
- Detection: YOLOv8n (ultralytics) + ByteTrack, cross-camera Re-ID via cosine distance
- Docker Compose: api + pipeline + dashboard services on shared `/data` volume
- Tests: pytest + pytest-asyncio, httpx ASGI transport, anyio (asyncio + trio backends)

## Where things live

```
store-intelligence/
├── pipeline/          # YOLOv8 detection + ByteTrack + Re-ID
│   ├── detect.py      # Main detection script
│   ├── tracker.py     # Re-ID registry, CameraTracker state
│   ├── emit.py        # Event builder + JSONL emission
│   └── run.sh         # Process all clips → ingest
├── app/               # FastAPI REST API
│   ├── main.py        # Entrypoint, structlog setup
│   ├── models.py      # Pydantic schemas (source of truth)
│   ├── db.py          # aiosqlite + WAL + dynamic DB_PATH
│   ├── ingestion.py   # POST /events/ingest
│   ├── metrics.py     # GET /stores/{id}/metrics
│   ├── funnel.py      # GET /stores/{id}/funnel
│   ├── heatmap.py     # GET /stores/{id}/heatmap
│   ├── anomalies.py   # GET /stores/{id}/anomalies
│   └── health.py      # GET /health
├── dashboard/live.py  # Rich terminal dashboard (replays events 10×)
├── tests/             # 3 test files, 62 tests total
├── assertions.py      # 10 spec-required CI assertions
├── data/              # Sample store_layout.json, POS CSV, events JSONL
├── docs/DESIGN.md     # Architecture, data flow, AI decisions
├── docs/CHOICES.md    # 3 deep-dive architecture decisions
├── docker-compose.yml
└── Dockerfile
```

- **Event schema source of truth**: `store-intelligence/app/models.py`
- **DB schema**: `store-intelligence/app/db.py`
- **API contracts**: `store-intelligence/app/models.py` (Pydantic response models)

## Architecture decisions

1. **YOLOv8n** chosen for detection (CPU-capable, native ByteTrack integration). Swap `--model` flag for YOLOv8s for higher accuracy.
2. **visitor_id** is deterministic: `VIS_` + `sha256(store_id + tracker_id + session_start_frame)[:6]`. Re-runs produce identical IDs — safe for idempotent ingest.
3. **Sessions materialised at ingest time** — `sessions` table updated as side-effect of every ENTRY/REENTRY/EXIT event. Funnel queries hit `sessions`, not raw events.
4. **Timestamps** are always `clip_start_datetime + (frame_number / fps)` — never system time.
5. **DB_PATH** read dynamically via `get_db_path()` (not module-level constant) so pytest fixtures can override per-test with `os.environ["DB_PATH"]`.

## Product

6 REST endpoints covering real-time metrics, conversion funnel, zone heatmap, anomaly detection, and system health for a retail CCTV analytics pipeline.

## Gotchas

- `DB_PATH` must be read via `get_db_path()`, never as a module-level constant — test fixtures override env at runtime.
- `pytest assertions.py` runs from the `store-intelligence/` directory (needs `PYTHONPATH=.`).
- Pipeline requires `ultralytics` and `cv2`; on CPU-only envs it emits placeholder STORE_IDLE events.

## User preferences

_Populate as you build._
