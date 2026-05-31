"""
export_dashboard_data.py
Reads the SQLite database and writes a JSON file that the Node.js dashboard
API can serve directly — no native SQLite bindings required in Node.

Usage:
    cd store-intelligence
    DB_PATH=data/store_intelligence.db python export_dashboard_data.py

Output: data/dashboard_data.json
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

os.environ.setdefault("DB_PATH", "data/store_intelligence.db")
sys.path.insert(0, str(Path(__file__).parent))

import aiosqlite

DB_PATH = os.environ.get("DB_PATH", "data/store_intelligence.db")
OUTPUT  = Path(__file__).parent / "data" / "dashboard_data.json"


async def main() -> None:
    if not Path(DB_PATH).exists():
        print(f"[ERROR] DB not found: {DB_PATH}")
        sys.exit(1)

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        # ── List of stores with data ─────────────────────────────────────
        async with db.execute(
            "SELECT DISTINCT store_id FROM events ORDER BY store_id"
        ) as cur:
            store_ids = [r[0] for r in await cur.fetchall()]

        stores_out = []
        metrics_out: dict = {}
        funnel_out:  dict = {}
        heatmap_out: dict = {}
        anomalies_out: dict = {}
        traffic_out: dict = {}

        for store_id in store_ids:
            stores_out.append({"store_id": store_id, "name": store_id, "city": "—"})

            # ── Metrics ───────────────────────────────────────────────────
            async with db.execute(
                "SELECT COUNT(DISTINCT visitor_id) FROM events WHERE store_id=? AND is_staff=0",
                (store_id,)
            ) as cur:
                unique_visitors = (await cur.fetchone())[0] or 0

            async with db.execute(
                "SELECT COUNT(*) FROM sessions WHERE store_id=? AND converted=1",
                (store_id,)
            ) as cur:
                converted = (await cur.fetchone())[0] or 0

            async with db.execute(
                "SELECT COUNT(*) FROM sessions WHERE store_id=?",
                (store_id,)
            ) as cur:
                total_sessions = (await cur.fetchone())[0] or 1

            async with db.execute(
                "SELECT AVG(dwell_ms) FROM events WHERE store_id=? AND event_type='ZONE_DWELL'",
                (store_id,)
            ) as cur:
                avg_dwell = (await cur.fetchone())[0] or 0

            async with db.execute(
                "SELECT SUM(basket_value_inr) FROM sessions WHERE store_id=? AND converted=1",
                (store_id,)
            ) as cur:
                revenue = (await cur.fetchone())[0] or 0.0

            async with db.execute(
                "SELECT COUNT(*) FROM events WHERE store_id=? AND event_type='ENTRY' AND is_staff=0",
                (store_id,)
            ) as cur:
                footfall = (await cur.fetchone())[0] or 0

            async with db.execute(
                "SELECT COUNT(DISTINCT visitor_id) FROM events WHERE store_id=? AND is_staff=1",
                (store_id,)
            ) as cur:
                staff_count = (await cur.fetchone())[0] or 0

            metrics_out[store_id] = {
                "store_id": store_id,
                "unique_visitors": unique_visitors,
                "conversion_rate": round(converted / total_sessions, 4) if total_sessions else 0.0,
                "avg_dwell_ms": int(avg_dwell),
                "total_revenue_inr": round(revenue, 2),
                "footfall_today": footfall,
                "staff_count": staff_count,
            }

            # ── Funnel ────────────────────────────────────────────────────
            async with db.execute(
                "SELECT COUNT(DISTINCT visitor_id) FROM events WHERE store_id=? AND event_type='ENTRY' AND is_staff=0",
                (store_id,)
            ) as cur:
                entered = (await cur.fetchone())[0] or 0

            async with db.execute(
                """SELECT COUNT(DISTINCT visitor_id) FROM events
                   WHERE store_id=? AND event_type='ZONE_ENTER' AND is_staff=0""",
                (store_id,)
            ) as cur:
                browsed = (await cur.fetchone())[0] or 0

            async with db.execute(
                """SELECT COUNT(DISTINCT visitor_id) FROM events
                   WHERE store_id=? AND event_type='BILLING_QUEUE_JOIN' AND is_staff=0""",
                (store_id,)
            ) as cur:
                billing = (await cur.fetchone())[0] or 0

            async with db.execute(
                "SELECT AVG(basket_value_inr) FROM sessions WHERE store_id=? AND converted=1",
                (store_id,)
            ) as cur:
                avg_basket = (await cur.fetchone())[0] or 0.0

            funnel_out[store_id] = {
                "store_id": store_id,
                "entered": entered,
                "browsed": browsed,
                "reached_billing": billing,
                "converted": converted,
                "avg_basket_inr": round(avg_basket, 2),
            }

            # ── Heatmap ───────────────────────────────────────────────────
            async with db.execute(
                """SELECT zone_id,
                          COUNT(DISTINCT visitor_id) AS visitor_count,
                          AVG(dwell_ms) AS avg_dwell
                   FROM events
                   WHERE store_id=? AND zone_id IS NOT NULL AND event_type='ZONE_DWELL'
                   GROUP BY zone_id
                   ORDER BY visitor_count DESC""",
                (store_id,)
            ) as cur:
                zone_rows = await cur.fetchall()

            max_visitors = max((r["visitor_count"] for r in zone_rows), default=1) or 1
            heatmap_out[store_id] = [
                {
                    "zone_id": r["zone_id"],
                    "label": r["zone_id"].replace("_", " ").title(),
                    "visitor_count": r["visitor_count"],
                    "avg_dwell_ms": int(r["avg_dwell"] or 0),
                    "score": round(r["visitor_count"] / max_visitors, 3),
                }
                for r in zone_rows
            ]

            # ── Anomalies ─────────────────────────────────────────────────
            now = datetime.now(timezone.utc).isoformat()
            anomalies: list[dict] = []

            # Crowd surge: count visitors in billing zone
            async with db.execute(
                """SELECT COUNT(DISTINCT visitor_id) FROM events
                   WHERE store_id=? AND zone_id LIKE '%BILLING%' AND event_type='BILLING_QUEUE_JOIN'""",
                (store_id,)
            ) as cur:
                queue_count = (await cur.fetchone())[0] or 0

            if queue_count > 50:
                anomalies.append({
                    "anomaly_id": f"ANO_{store_id}_CROWD",
                    "type": "CROWD_SURGE",
                    "severity": "CRITICAL" if queue_count > 100 else "WARN",
                    "message": f"{queue_count} billing queue joins detected — peak congestion",
                    "detected_at": now,
                })

            # Conversion drop
            conv_rate = metrics_out[store_id]["conversion_rate"]
            if conv_rate < 0.20:
                anomalies.append({
                    "anomaly_id": f"ANO_{store_id}_CONV",
                    "type": "CONVERSION_DROP",
                    "severity": "WARN",
                    "message": f"Conversion rate {conv_rate:.1%} is below 20% threshold",
                    "detected_at": now,
                })

            # Dwell spike: look for zone with dwell > 5 min avg
            for zone in heatmap_out[store_id]:
                if zone["avg_dwell_ms"] > 300_000:
                    anomalies.append({
                        "anomaly_id": f"ANO_{store_id}_{zone['zone_id']}",
                        "type": "DWELL_SPIKE",
                        "severity": "INFO",
                        "message": f"{zone['label']}: avg dwell {zone['avg_dwell_ms']//60000}m {(zone['avg_dwell_ms']%60000)//1000}s — above 5 min baseline",
                        "detected_at": now,
                    })
                    break

            if not anomalies:
                anomalies.append({
                    "anomaly_id": f"ANO_{store_id}_OK",
                    "type": "NORMAL",
                    "severity": "INFO",
                    "message": "No anomalies detected in this recording period",
                    "detected_at": now,
                })
            anomalies_out[store_id] = anomalies

            # ── Traffic (hourly from timestamps) ─────────────────────────
            async with db.execute(
                """SELECT timestamp FROM events
                   WHERE store_id=? AND event_type='ENTRY' AND is_staff=0
                   ORDER BY timestamp""",
                (store_id,)
            ) as cur:
                ts_rows = await cur.fetchall()

            from collections import defaultdict
            DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
            traffic_map: dict[tuple[str, int], int] = defaultdict(int)
            for r in ts_rows:
                try:
                    ts = datetime.fromisoformat(r[0].replace("Z", "+00:00"))
                    day = DAYS[ts.weekday()]
                    traffic_map[(day, ts.hour)] += 1
                except Exception:
                    pass

            traffic_out[store_id] = [
                {"day": day, "hour": hour, "visitors": count}
                for (day, hour), count in sorted(traffic_map.items())
            ]

        output = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "stores": stores_out,
            "metrics": metrics_out,
            "funnel": funnel_out,
            "heatmap": heatmap_out,
            "anomalies": anomalies_out,
            "traffic": traffic_out,
        }

        OUTPUT.write_text(json.dumps(output, indent=2))
        print(f"✓ Exported {len(stores_out)} store(s) → {OUTPUT}")
        for sid in store_ids:
            m = metrics_out[sid]
            print(f"  {sid}: {m['unique_visitors']} visitors, {m['conversion_rate']:.1%} conversion, ₹{m['total_revenue_inr']:,.0f} revenue")


if __name__ == "__main__":
    asyncio.run(main())
