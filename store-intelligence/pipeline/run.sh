#!/usr/bin/env bash
# run.sh — single command to process all clips → /data/events.jsonl
# Usage: bash pipeline/run.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

export PYTHONPATH="$PROJECT_ROOT:${PYTHONPATH:-}"

LAYOUT="${STORE_LAYOUT:-/data/store_layout.json}"
CLIPS="${CLIPS_DIR:-/data/clips}"
POS="${POS_CSV:-/data/pos_transactions.csv}"
OUTPUT="${EVENTS_JSONL:-/data/events.jsonl}"
MODEL="${YOLO_MODEL:-yolov8n.pt}"

echo "[run.sh] Starting pipeline..."
echo "  Layout : $LAYOUT"
echo "  Clips  : $CLIPS"
echo "  POS    : $POS"
echo "  Output : $OUTPUT"
echo "  Model  : $MODEL"

python -m pipeline.detect \
  --layout "$LAYOUT" \
  --clips  "$CLIPS" \
  --pos    "$POS" \
  --output "$OUTPUT" \
  --model  "$MODEL"

echo "[run.sh] Pipeline complete. Events written to $OUTPUT"

# Ingest events into the API if it's running
API_URL="${API_URL:-http://api:8000}"
INGEST_ENDPOINT="$API_URL/events/ingest"
BATCH_SIZE=500

echo "[run.sh] Ingesting events into $INGEST_ENDPOINT ..."

python - <<'PYEOF'
import json, sys, os, requests, math

jsonl = os.environ.get("EVENTS_JSONL", "/data/events.jsonl")
api   = os.environ.get("API_URL", "http://api:8000")
url   = f"{api}/events/ingest"
batch = int(os.environ.get("BATCH_SIZE", 500))

if not os.path.exists(jsonl):
    print(f"[ingest] No events file at {jsonl}, skipping ingest.")
    sys.exit(0)

events = []
with open(jsonl) as f:
    for line in f:
        line = line.strip()
        if line:
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                pass

total = len(events)
print(f"[ingest] {total} events found, sending in batches of {batch}...")

ingested = 0
errors   = 0
for i in range(0, total, batch):
    chunk = events[i : i + batch]
    try:
        r = requests.post(url, json={"events": chunk}, timeout=30)
        body = r.json()
        ingested += body.get("ingested", 0)
        errors   += len(body.get("errors", []))
    except Exception as exc:
        print(f"[ingest] batch {i//batch} failed: {exc}")

print(f"[ingest] Done — ingested={ingested}, errors={errors}")
PYEOF

echo "[run.sh] All done."
