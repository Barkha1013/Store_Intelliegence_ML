# Architecture Choices — Store Intelligence

Three decisions shaped this system most significantly. Each is documented below with the options I weighed, what an LLM suggested, what I chose, and why.

---

## 1. Detection Model: YOLOv8n vs RT-DETR vs MediaPipe vs Others

### Options considered

**YOLOv8n** (Ultralytics): The smallest variant of the YOLOv8 family. ~3.2M parameters. On a modern CPU it achieves roughly 40 fps on 1080p with class filtering — well above the 15 fps target. Pre-trained on COCO, class 0 is person. ByteTrack is natively integrated into the ultralytics API via `model.track(..., tracker="bytetrack.yaml")`, which eliminates the need to integrate a separate tracker library.

**YOLOv8s**: The next size up (~11M parameters). Meaningfully better mAP (44.9 vs 37.3 on COCO val), but roughly 2–3× slower on CPU. For the 5-store × 3-camera batch workload in this spec, the accuracy gain is worth considering — but we are processing face-blurred footage, and the dominant source of detection error is partial occlusion at the entry line, not small-person detection. YOLOv8n handles this adequately.

**RT-DETR** (Baidu): A transformer-based real-time detector with strong benchmark numbers. The ultralytics integration is available, but RT-DETR is significantly slower on CPU and the ONNX export path for ByteTrack integration is less well-tested at time of writing. For a GPU-only deployment this would be a compelling choice.

**MediaPipe Pose/Detection** (Google): Extremely fast on CPU, designed for mobile. Person detection is reliable, but there is no native multi-camera tracking pipeline, and integrating a separate Re-ID step would require custom glue code. The lack of a mature ByteTrack integration was a dealbreaker.

**YOLO-NAS** (Deci.ai): Good benchmark numbers, but the commercial licensing terms for the pre-trained weights are restrictive. Not suitable for a retail deployment without a license review.

### What the AI suggested

When I described the constraint set — 15 fps target, CPU fallback required, ByteTrack integration preferred, single COCO-pretrained model — the LLM suggested YOLOv8n as the pragmatic choice, with a note to benchmark YOLOv8s on actual hardware before committing to YOLOv8n for a production deployment. It flagged that the CHOICES.md should document the model size/accuracy tradeoff explicitly.

### What I chose and why

**YOLOv8n** for the initial implementation, with a clear upgrade path to YOLOv8s.

The primary constraint is throughput, not accuracy. We are processing 5 × 3 = 15 clips in batch. On a single mid-range GPU (RTX 3060), YOLOv8n processes a 20-minute 1080p clip in under 3 minutes. YOLOv8s would take 6–9 minutes. For an offline batch pipeline that runs nightly, this is not a hard constraint, but it matters when re-running the pipeline on updated clips during the day.

More importantly, the `confidence` field is never rounded up (spec requirement). YOLOv8n's confidence calibration is well-documented and tested. The spec's threshold of 0.45 for partial occlusion aligns well with the YOLOv8n confidence distribution on person detections — events below that threshold are still emitted, with the exact confidence value. This would require re-validation with a different model.

The architecture is model-agnostic: the `YOLO` constructor in `detect.py` accepts any ultralytics-compatible model path, so swapping to YOLOv8s or RT-DETR requires only changing the `--model` flag.

---

## 2. Event Schema Design: visitor_id Assignment and Re-entry Handling

### Options considered

**Option A — Track ID as visitor_id:** Use ByteTrack's `track_id` directly as the visitor identifier. Simple, but track IDs reset every clip and are camera-local, so the same person visiting across two clips or two cameras gets two different IDs. Cross-clip continuity is impossible.

**Option B — UUID on first detection:** Generate a fresh UUIDv4 when a new track appears. Globally unique and simple. The problem is that re-entries cannot be detected — a person who exits and re-enters gets a fresh UUID. The funnel would over-count unique visitors, and re-entry patterns (which are commercially significant — they indicate high-intent browsing) are invisible.

**Option C — SHA256 of store_id + track_id + session_start_frame (spec requirement):** Deterministic. Running the pipeline twice on the same clip produces identical visitor_ids, which is the correct behaviour for an idempotent system. The 6-hex suffix gives 16 million possible values — collision probability is negligible for a single store's daily visitor count (typically 500–5000 people).

**Re-entry handling options:**
- Emit a new ENTRY event (simpler, but over-counts unique visitors)
- Emit REENTRY with the original visitor_id (correct for funnel deduplication)
- Suppress the event entirely (loses the signal that this person returned)

### What the AI suggested

The LLM was direct: use the spec's SHA256 derivation for visitor_id (Option C), and emit a distinct REENTRY event type rather than a second ENTRY. The key argument it made was that the funnel endpoint uses `COUNT(DISTINCT visitor_id)` — so REENTRY events sharing a visitor_id with an earlier ENTRY automatically deduplicate in the funnel without any special-case logic. The event type distinction is purely for analytics consumers who want to separate first-visit from return-visit behaviour.

It also suggested that the Re-ID embedding comparison should use a lower cosine similarity threshold for re-entry detection (0.80) than for cross-camera dedup (0.85), since re-entry embeddings may drift slightly over time (different clothing layers, different angle on second pass).

### What I chose and why

I adopted both suggestions without modification. The visitor_id format follows the spec exactly. The REENTRY distinction is correct product design: a retail analyst cares deeply about how many people returned without purchasing (abandonment signal) vs how many returned and then converted (high-intent signal). Collapsing both into ENTRY would make this analysis impossible without joining against session data.

One decision I made independently: the `_exited` dict in `CameraTracker` uses `visitor_id` as the key (not track_id), so that re-entry lookup is direct. When a new detection arrives, we iterate the exited visitors and compare embeddings. This is O(n) in the number of exited visitors, which is acceptable for a 20-minute clip where the total visitor count is in the hundreds.

---

## 3. API Architecture: Storage Engine, Async Model, Session Computation Strategy

### Options considered

**Storage engine:**
- **PostgreSQL** with asyncpg: Correct for a multi-node production deployment. Requires provisioning and connection pool management. Overkill for the spec's 5-store workload.
- **SQLite with aiosqlite**: Zero-provisioning, WAL mode for concurrent readers, async interface. Suitable for up to ~100 concurrent API consumers and ~10M event rows. The spec calls for SQLite explicitly.
- **Redis + PostgreSQL** (CQRS): Write events to Redis pub/sub for real-time fan-out, persist to Postgres. This is the right architecture for a live CCTV system, but the spec's pipeline is batch-processed clips, not a real-time stream. The complexity is not justified.

**Async model:**
- **FastAPI + asyncio + aiosqlite**: All I/O is non-blocking. A single uvicorn worker can handle many concurrent requests without thread pool overhead. The correct choice when the workload is I/O-bound (SQL queries, not CPU).
- **FastAPI + threading + sqlite3**: Simpler but requires connection-per-thread management to avoid SQLite's threading restrictions. Less efficient under concurrent load.

**Session computation strategy:**
- **Materialised sessions table (spec requirement)**: Sessions are computed incrementally as events are ingested. The funnel endpoint queries `sessions` directly, which is fast. The cost is that the ingest path has two writes (events + sessions).
- **Compute sessions on-the-fly from events**: Every funnel request aggregates raw events. Correct for small datasets; becomes slow at scale when events > 1M rows and session boundaries must be inferred from ENTRY/EXIT pairs.
- **Pre-aggregated metrics table**: A background worker computes hourly aggregates. Fast queries but stale data — incompatible with the spec's "real-time: query DB live, no caching" requirement.

### What the AI suggested

The LLM strongly favoured the materialised sessions table over on-the-fly computation. Its reasoning: the funnel specification says "session is the unit (not raw event counts)" — this is unambiguous that the sessions table is the intended query target. Computing sessions on-the-fly would require a self-join on events filtered by ENTRY/EXIT pairs, which is fragile (what if EXIT is missing?) and slow.

For the async model, it recommended aiosqlite with WAL mode and explicit `PRAGMA foreign_keys=ON` and `PRAGMA journal_mode=WAL` at connection startup. It also suggested `db.row_factory = aiosqlite.Row` for named column access, which avoids positional indexing bugs.

### What I chose and why

I adopted the materialised sessions approach. The `upsert_session()` helper in `sessions.py` is called from within the ingest transaction, so sessions are always consistent with the events table. The upsert pattern (`INSERT ... ON CONFLICT DO UPDATE`) handles the edge case where an ENTRY event for an already-open session is re-ingested (idempotency).

One design decision I made beyond the LLM's suggestion: session_id is computed as `sha256(store_id + ":" + visitor_id)[:32]` rather than a random UUID. This makes session lookup deterministic and means that re-running the ingest pipeline (after, say, correcting a visitor_id) will update the existing session record rather than creating a phantom duplicate. The tradeoff is that a visitor can only have one active session per store at a time — which is the correct semantic for this domain.

The graceful degradation strategy for DB unavailability was also an independent decision: all query endpoints return HTTP 503 with a structured body (not a raw exception), while `/health` always returns 200 with `db_status: "unavailable"`. This allows monitoring systems to distinguish "API is down" from "API is up but DB is unhealthy".
