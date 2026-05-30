# Store Intelligence — Architecture & Design

## Architecture Overview

Store Intelligence is a four-stage pipeline that transforms raw CCTV footage into actionable retail analytics. The system is designed to be modular so each stage can be scaled, replaced, or debugged independently.

The first stage is **Detection and Tracking**. Raw video clips are processed frame by frame using YOLOv8n for person detection and ByteTrack for multi-object tracking within a single camera view. Each detected person is assigned a persistent track ID across the clip. A separate Re-ID layer handles cross-camera deduplication: when the same person appears on two cameras within a 30-second window, their embedding matches a record in the global registry and they are assigned the same `visitor_id` rather than generating a second one. Staff are filtered out at this stage using a combination of bounding-box aspect ratio and torso colour histogram matched against the reference uniform HSV range from `store_layout.json`.

The second stage is **Event Emission**. Each meaningful state change — a person crossing the entry line, entering a zone, dwelling longer than a threshold, joining the billing queue — is serialised as a structured event and appended to an append-only JSONL file on disk. The JSONL is the authoritative pipeline output. Every timestamp is derived deterministically from `clip_start_datetime + (frame_number / fps)` seconds; the system clock is never used. This makes event replay and debugging reproducible.

The third stage is the **Intelligence API**. A FastAPI application with aiosqlite reads from SQLite and exposes six endpoints. Ingest is fully idempotent — the events table uses `event_id` as a primary key with `INSERT OR IGNORE`, so running the pipeline twice produces the same DB state. The sessions table is maintained as a side-effect of ingest: ENTRY/REENTRY events open sessions, EXIT events close them, and POS data marks conversions. All customer-facing metrics filter `WHERE is_staff = FALSE` at the SQL layer.

The fourth stage is the **Live Dashboard**. A Rich terminal UI polls the metrics, heatmap, and anomaly endpoints every two seconds. A background replay thread reads `events.jsonl` and posts events to the ingest endpoint at 10× real-time speed, so the dashboard reflects simulated live traffic without needing a live camera feed.

## Data Flow: Frame to API Response

```
CCTV clip (MP4)
    │
    ▼
YOLOv8n detection (per frame, 15 fps target)
    │  bounding boxes + confidence scores
    ▼
ByteTrack (per camera)
    │  track_id assigned, maintained across frames
    ▼
Re-ID registry (cross-camera, 30s window)
    │  visitor_id: VIS_{sha256(store_id + track_id + start_frame)[:6]}
    ▼
Staff classifier (aspect ratio + torso HSV)
    │  is_staff flag set
    ▼
Zone resolver (point-in-polygon, per-frame centroid)
    │  zone_id assigned from store_layout.json polygons
    ▼
Event state machine
    │  ENTRY, ZONE_ENTER/EXIT/DWELL, BILLING_QUEUE_JOIN/ABANDON, EXIT, REENTRY
    ▼
JSONL emission (append-only, thread-safe)
    │  events.jsonl on shared Docker volume
    ▼
POST /events/ingest (batch up to 500, idempotent)
    │  SQLite: events + sessions tables
    ▼
GET /stores/{id}/metrics|funnel|heatmap|anomalies
    │  live SQL queries, no caching
    ▼
GET /health
Dashboard + consumers
```

## Synthetic Event: STORE_IDLE

When no detections are observed for 5 or more consecutive minutes, the pipeline emits a `STORE_IDLE` synthetic event. This extends the base event schema with:

- `event_type = "STORE_IDLE"` (new enum value, validated by Pydantic)
- `visitor_id = "SYNTHETIC"` (constant sentinel, not a real visitor)
- `dwell_ms` = idle duration in milliseconds
- `metadata.idle_duration_seconds` = duration as an integer (schema extension via `model_config: extra = "allow"`)

The purpose is operational: a burst of STORE_IDLE events from a camera triggers the `STALE_CAMERA` anomaly, alerting operations that a feed may have died. The visitor_id sentinel ensures STORE_IDLE events are trivially excluded from all customer metrics by the `WHERE is_staff = FALSE AND visitor_id != 'SYNTHETIC'` filter pattern — though in practice the `is_staff = FALSE` filter is sufficient since these events carry no customer-facing data.

## AI-Assisted Decisions

### Decision 1: Timestamp derivation strategy

**What I asked:** Should event timestamps use system time at detection, or be derived from clip metadata?

**What the AI suggested:** Derive timestamps as `clip_start_datetime + (frame_number / fps)` seconds, loaded from `store_layout.json["clips"][camera_id]["start_time"]`. This makes events temporally correct even when processing clips hours or days after capture, and makes the entire pipeline deterministic and reproducible.

**My decision:** Agreed and adopted without modification. This was the obviously correct choice for a batch-processing pipeline where clips may be processed far from real-time. The alternative (system time) would produce wildly incorrect timestamps for historical analysis.

### Decision 2: Re-entry vs new ENTRY event type

**What I asked:** When the same person re-enters the store, should we emit a new ENTRY (treating them as a fresh visit) or a REENTRY (reusing their visitor_id)?

**What the AI suggested:** Emit REENTRY with the same visitor_id. This keeps the funnel correct: a visitor who browses, leaves, then returns is one person in the conversion funnel, not two. Using a distinct event type also lets downstream systems decide whether to count re-visits separately for dwell or zone analytics.

**My decision:** Agreed. The critical insight is that the sessions table and funnel endpoint use `COUNT(DISTINCT visitor_id)`, so re-entries naturally deduplicate. The REENTRY event type also provides a signal for marketing analysis (e.g., "high intent — browsed twice before purchase").

### Decision 3: Partial ingest success vs all-or-nothing

**What I asked:** For POST /events/ingest, if 3 of 500 events are malformed, should the whole batch fail or should the 497 valid events be ingested?

**What the AI suggested:** Partial success — ingest valid events, return structured errors for invalid ones. The response body carries `{ingested, duplicates, errors: [{event_id, reason}]}`. This is the correct behaviour for an idempotent batch endpoint where the pipeline produces hundreds of events per clip and a single schema violation in one event should not block the rest.

**My decision:** Agreed. The counterargument (all-or-nothing for transactional consistency) does not apply here because events are append-only and the idempotency guarantee already handles replays safely.

## Known Limitations and Graceful Degradation

**Re-ID quality:** The current Re-ID implementation uses a lightweight cosine distance on bounding-box geometry and torso HSV histograms. For a production deployment with tightly overlapping camera views, this will produce false positives (two similar-looking customers treated as one person). The architecture is designed to swap in a proper OSNet model (torchreid) at the `extract_embedding` method without changing any downstream code.

**SQLite at scale:** SQLite with WAL mode handles the read-heavy workload well for a single-node deployment. It becomes a bottleneck above ~100 concurrent API consumers or when the events table grows beyond ~10M rows. The DB layer is isolated behind `get_db()` context manager — swapping to PostgreSQL requires only changing the connection string and replacing `aiosqlite` with `asyncpg`.

**Pipeline processing time:** Processing 5 stores × 3 cameras × 20-minute clips at 15 fps takes approximately 45–90 minutes on CPU (YOLOv8n). On a single GPU this drops to under 10 minutes. The pipeline is embarrassingly parallel across clips — each `(store_id, camera_id)` pair is independent.

**Staff classification accuracy:** The aspect-ratio + colour-histogram classifier is a heuristic. It will misclassify customers wearing similar colours to staff. The `store_layout.json` HSV range must be calibrated per store and per season. A false positive (customer classified as staff) will silently exclude that customer from all metrics.

**DB unavailable:** If SQLite is inaccessible, all query endpoints return HTTP 503 with `{"error": "database_unavailable", "retry_after": 30}`. The `/health` endpoint always returns HTTP 200, with `db_status: "unavailable"` and `status: "degraded"`, so monitoring systems do not need to distinguish 503 from a healthy "no data yet" state.
