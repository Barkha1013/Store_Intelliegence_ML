"""
Store Intelligence — Streamlit Dashboard
Reads directly from the SQLite database produced by the pipeline.
Run: cd store-intelligence && streamlit run dashboard/streamlit_app.py
"""

from __future__ import annotations

import os
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

# ── Config ────────────────────────────────────────────────────────────────────

DB_PATH = os.environ.get(
    "DB_PATH",
    str(Path(__file__).parent.parent / "data" / "store_intelligence.db"),
)

st.set_page_config(
    page_title="Store Intelligence",
    page_icon="🏬",
    layout="wide",
)

# ── DB helpers ────────────────────────────────────────────────────────────────

@st.cache_resource
def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def query(sql: str, params: tuple = ()) -> pd.DataFrame:
    conn = get_conn()
    return pd.read_sql_query(sql, conn, params=params)


def scalar(sql: str, params: tuple = ()):
    conn = get_conn()
    cur = conn.execute(sql, params)
    row = cur.fetchone()
    return row[0] if row else None


# ── Store selector ────────────────────────────────────────────────────────────

if not Path(DB_PATH).exists():
    st.error(f"Database not found at `{DB_PATH}`. Run `python process_videos.py` first.")
    st.stop()

store_ids = query("SELECT DISTINCT store_id FROM events ORDER BY store_id")["store_id"].tolist()
if not store_ids:
    st.error("No events found in the database. Run `python process_videos.py` first.")
    st.stop()

col_title, col_store, col_refresh = st.columns([4, 2, 1])
with col_title:
    st.title("🏬 Store Intelligence")
    st.caption("Real-time footfall and conversion tracking")
with col_store:
    store_id = st.selectbox("Store", store_ids, label_visibility="collapsed")
with col_refresh:
    if st.button("↻ Refresh"):
        st.cache_data.clear()

st.divider()

# ── KPI queries ───────────────────────────────────────────────────────────────

unique_visitors = scalar(
    "SELECT COUNT(DISTINCT visitor_id) FROM events WHERE store_id=? AND is_staff=0",
    (store_id,),
) or 0

total_sessions = scalar(
    "SELECT COUNT(*) FROM sessions WHERE store_id=?", (store_id,)
) or 1

converted = scalar(
    "SELECT COUNT(*) FROM sessions WHERE store_id=? AND converted=1", (store_id,)
) or 0

conv_rate = converted / total_sessions

avg_dwell_ms = scalar(
    "SELECT AVG(dwell_ms) FROM events WHERE store_id=? AND event_type='ZONE_DWELL'",
    (store_id,),
) or 0
avg_dwell_s = int(avg_dwell_ms / 1000)

revenue = scalar(
    "SELECT SUM(basket_value_inr) FROM sessions WHERE store_id=? AND converted=1",
    (store_id,),
) or 0.0

footfall = scalar(
    "SELECT COUNT(*) FROM events WHERE store_id=? AND event_type='ENTRY' AND is_staff=0",
    (store_id,),
) or 0

# ── KPI cards ─────────────────────────────────────────────────────────────────

k1, k2, k3, k4, k5 = st.columns(5)
k1.metric("👥 Unique Visitors", f"{unique_visitors:,}")
k2.metric("🚶 Footfall", f"{footfall:,}")
k3.metric("✅ Conversion Rate", f"{conv_rate:.1%}")
k4.metric("⏱️ Avg Dwell", f"{avg_dwell_s}s" if avg_dwell_s < 60 else f"{avg_dwell_s//60}m {avg_dwell_s%60}s")
k5.metric("₹ Revenue", f"₹{revenue:,.0f}")

st.divider()

# ── Row 1: Funnel + Zone Heatmap ─────────────────────────────────────────────

left, right = st.columns(2)

with left:
    st.subheader("Conversion Funnel")

    entered = scalar(
        "SELECT COUNT(DISTINCT visitor_id) FROM events WHERE store_id=? AND event_type='ENTRY' AND is_staff=0",
        (store_id,),
    ) or 0
    browsed = scalar(
        "SELECT COUNT(DISTINCT visitor_id) FROM events WHERE store_id=? AND event_type='ZONE_ENTER' AND is_staff=0",
        (store_id,),
    ) or 0
    billing = scalar(
        "SELECT COUNT(DISTINCT visitor_id) FROM events WHERE store_id=? AND event_type='BILLING_QUEUE_JOIN' AND is_staff=0",
        (store_id,),
    ) or 0

    funnel_df = pd.DataFrame(
        {
            "Stage": ["Entered", "Browsed Zones", "Reached Billing", "Converted"],
            "Visitors": [entered, browsed, billing, converted],
        }
    )

    fig_funnel = go.Figure(
        go.Funnel(
            y=funnel_df["Stage"],
            x=funnel_df["Visitors"],
            textinfo="value+percent initial",
            marker_color=["#4F8EF7", "#6BA3F8", "#87B8F9", "#A3CDFA"],
        )
    )
    fig_funnel.update_layout(margin=dict(l=0, r=0, t=10, b=0), height=280)
    st.plotly_chart(fig_funnel, use_container_width=True)

with right:
    st.subheader("Zone Heatmap")

    zone_df = query(
        """SELECT zone_id,
                  COUNT(DISTINCT visitor_id) AS visitors,
                  ROUND(AVG(dwell_ms)/1000.0, 1)  AS avg_dwell_s
           FROM events
           WHERE store_id=? AND zone_id IS NOT NULL AND event_type='ZONE_DWELL'
           GROUP BY zone_id
           ORDER BY visitors DESC""",
        (store_id,),
    )

    if zone_df.empty:
        # Fall back to ZONE_ENTER if no ZONE_DWELL rows
        zone_df = query(
            """SELECT zone_id,
                      COUNT(DISTINCT visitor_id) AS visitors,
                      0.0 AS avg_dwell_s
               FROM events
               WHERE store_id=? AND zone_id IS NOT NULL AND event_type='ZONE_ENTER'
               GROUP BY zone_id
               ORDER BY visitors DESC""",
            (store_id,),
        )

    if not zone_df.empty:
        zone_df["label"] = zone_df["zone_id"].str.replace("_", " ").str.title()
        fig_heat = px.bar(
            zone_df,
            x="visitors",
            y="label",
            orientation="h",
            color="visitors",
            color_continuous_scale="Blues",
            labels={"visitors": "Visitors", "label": ""},
            custom_data=["avg_dwell_s"],
        )
        fig_heat.update_traces(
            hovertemplate="<b>%{y}</b><br>Visitors: %{x}<br>Avg dwell: %{customdata[0]}s<extra></extra>"
        )
        fig_heat.update_layout(
            margin=dict(l=0, r=0, t=10, b=0),
            height=280,
            coloraxis_showscale=False,
            yaxis=dict(categoryorder="total ascending"),
        )
        st.plotly_chart(fig_heat, use_container_width=True)
    else:
        st.info("No zone dwell data yet.")

st.divider()

# ── Row 2: Hourly Traffic ─────────────────────────────────────────────────────

st.subheader("Hourly Traffic")

entry_df = query(
    "SELECT timestamp FROM events WHERE store_id=? AND event_type='ENTRY' AND is_staff=0 ORDER BY timestamp",
    (store_id,),
)

if not entry_df.empty:
    entry_df["ts"] = pd.to_datetime(entry_df["timestamp"], utc=True)
    entry_df["hour"] = entry_df["ts"].dt.hour
    entry_df["day"] = entry_df["ts"].dt.strftime("%a")

    hourly = (
        entry_df.groupby(["day", "hour"])
        .size()
        .reset_index(name="visitors")
    )

    day_order = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    existing_days = [d for d in day_order if d in hourly["day"].unique()]

    fig_traffic = px.line(
        hourly,
        x="hour",
        y="visitors",
        color="day",
        category_orders={"day": existing_days},
        labels={"hour": "Hour of Day", "visitors": "Visitors", "day": "Day"},
        markers=True,
    )
    fig_traffic.update_layout(
        margin=dict(l=0, r=0, t=10, b=0),
        height=260,
        xaxis=dict(tickmode="linear", tick0=0, dtick=1),
    )
    st.plotly_chart(fig_traffic, use_container_width=True)
else:
    st.info("No entry events to chart.")

st.divider()

# ── Row 3: Anomalies + Event Log ──────────────────────────────────────────────

a_col, e_col = st.columns([1, 2])

with a_col:
    st.subheader("Anomalies")

    anomalies: list[dict] = []

    queue_count = scalar(
        "SELECT COUNT(DISTINCT visitor_id) FROM events WHERE store_id=? AND event_type='BILLING_QUEUE_JOIN'",
        (store_id,),
    ) or 0
    if queue_count > 50:
        anomalies.append(
            {
                "Severity": "⚠️ WARN" if queue_count <= 100 else "🔴 CRITICAL",
                "Type": "Crowd Surge",
                "Detail": f"{queue_count} billing queue joins",
            }
        )

    if conv_rate < 0.20 and total_sessions > 5:
        anomalies.append(
            {
                "Severity": "⚠️ WARN",
                "Type": "Conversion Drop",
                "Detail": f"{conv_rate:.1%} below 20% threshold",
            }
        )

    reentry_count = scalar(
        "SELECT COUNT(*) FROM events WHERE store_id=? AND event_type='REENTRY'",
        (store_id,),
    ) or 0
    if reentry_count > 20:
        anomalies.append(
            {
                "Severity": "ℹ️ INFO",
                "Type": "High Re-entries",
                "Detail": f"{reentry_count} re-entries detected",
            }
        )

    if anomalies:
        st.dataframe(
            pd.DataFrame(anomalies),
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.success("No anomalies detected")

with e_col:
    st.subheader("Recent Events")

    recent = query(
        """SELECT timestamp, event_type, visitor_id, camera_id, zone_id, confidence
           FROM events
           WHERE store_id=?
           ORDER BY timestamp DESC
           LIMIT 50""",
        (store_id,),
    )

    if not recent.empty:
        recent["timestamp"] = pd.to_datetime(recent["timestamp"]).dt.strftime("%H:%M:%S")
        recent["confidence"] = recent["confidence"].map(lambda x: f"{x:.2f}" if x else "—")
        recent["zone_id"] = recent["zone_id"].fillna("—")
        st.dataframe(recent, use_container_width=True, hide_index=True, height=240)
    else:
        st.info("No events yet.")

st.divider()

# ── Footer ────────────────────────────────────────────────────────────────────

total_events = scalar("SELECT COUNT(*) FROM events WHERE store_id=?", (store_id,)) or 0
last_event = scalar(
    "SELECT MAX(timestamp) FROM events WHERE store_id=?", (store_id,)
)

st.caption(
    f"Store: **{store_id}** · Events in DB: **{total_events:,}** · "
    f"Last event: **{last_event or '—'}** · "
    f"DB: `{DB_PATH}`"
)
