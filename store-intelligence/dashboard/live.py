"""
dashboard/live.py — Rich terminal dashboard for Store Intelligence.

Updates every 2 seconds by reading from GET /stores/{id}/metrics.
Also replays events.jsonl at 10× speed, POSTing to /events/ingest so the
dashboard reflects real pipeline activity.

Usage:
    python dashboard/live.py STORE_BLR_002
    python dashboard/live.py STORE_BLR_002 --api http://localhost:8000
    python dashboard/live.py STORE_BLR_002 --replay /data/events.jsonl

Dashboard: run `python dashboard/live.py STORE_BLR_002`
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import threading
from datetime import datetime, timezone
from typing import Optional

try:
    import httpx
    from rich.console import Console
    from rich.layout import Layout
    from rich.live import Live
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
    from rich import box
    RICH_AVAILABLE = True
except ImportError:
    RICH_AVAILABLE = False

REFRESH_SECONDS = 2.0
REPLAY_SPEED = 10.0  # replay 10× real-time
BATCH_SIZE = 50  # events per ingest batch during replay


# ---------------------------------------------------------------------------
# API Client
# ---------------------------------------------------------------------------

class APIClient:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")

    def get_metrics(self, store_id: str) -> Optional[dict]:
        try:
            r = httpx.get(f"{self.base_url}/stores/{store_id}/metrics", timeout=5)
            r.raise_for_status()
            return r.json()
        except Exception:
            return None

    def get_heatmap(self, store_id: str) -> Optional[dict]:
        try:
            r = httpx.get(f"{self.base_url}/stores/{store_id}/heatmap", timeout=5)
            r.raise_for_status()
            return r.json()
        except Exception:
            return None

    def get_anomalies(self, store_id: str) -> Optional[dict]:
        try:
            r = httpx.get(f"{self.base_url}/stores/{store_id}/anomalies", timeout=5)
            r.raise_for_status()
            return r.json()
        except Exception:
            return None

    def ingest_events(self, events: list[dict]) -> Optional[dict]:
        try:
            r = httpx.post(
                f"{self.base_url}/events/ingest",
                json={"events": events},
                timeout=10,
            )
            r.raise_for_status()
            return r.json()
        except Exception:
            return None


# ---------------------------------------------------------------------------
# Replay thread
# ---------------------------------------------------------------------------

class ReplayThread(threading.Thread):
    def __init__(self, jsonl_path: str, store_id: str, client: APIClient) -> None:
        super().__init__(daemon=True)
        self.jsonl_path = jsonl_path
        self.store_id = store_id
        self.client = client
        self.ingested_total = 0
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        if not os.path.exists(self.jsonl_path):
            return

        events: list[dict] = []
        with open(self.jsonl_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        e = json.loads(line)
                        if e.get("store_id") == self.store_id or not self.store_id:
                            events.append(e)
                    except json.JSONDecodeError:
                        pass

        if not events:
            return

        # Sort by timestamp
        events.sort(key=lambda e: e.get("timestamp", ""))

        # Compute replay timing from event timestamps
        def parse_ts(ts_str: str) -> float:
            try:
                dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                return dt.timestamp()
            except Exception:
                return 0.0

        start_event_ts = parse_ts(events[0].get("timestamp", ""))
        replay_start_wall = time.time()

        batch: list[dict] = []
        for event in events:
            if self._stop_event.is_set():
                break

            event_ts = parse_ts(event.get("timestamp", ""))
            elapsed_real = (event_ts - start_event_ts) / REPLAY_SPEED
            target_wall = replay_start_wall + elapsed_real
            now = time.time()
            if target_wall > now:
                time.sleep(min(target_wall - now, 1.0))

            batch.append(event)
            if len(batch) >= BATCH_SIZE:
                self.client.ingest_events(batch)
                self.ingested_total += len(batch)
                batch = []

        if batch:
            self.client.ingest_events(batch)
            self.ingested_total += len(batch)


# ---------------------------------------------------------------------------
# Dashboard renderer
# ---------------------------------------------------------------------------

def render_dashboard(
    store_id: str,
    metrics: Optional[dict],
    heatmap: Optional[dict],
    anomalies: Optional[dict],
    last_update: str,
    replayed_events: int,
) -> Layout:
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="body"),
        Layout(name="footer", size=3),
    )
    layout["body"].split_row(
        Layout(name="metrics", ratio=1),
        Layout(name="heatmap", ratio=1),
        Layout(name="anomalies", ratio=1),
    )

    # Header
    layout["header"].update(
        Panel(
            Text(
                f"  Store Intelligence Dashboard  |  Store: {store_id}  |  Updated: {last_update}",
                style="bold white",
                justify="center",
            ),
            style="bold blue",
        )
    )

    # Metrics panel
    if metrics:
        m_table = Table(box=box.SIMPLE, show_header=False, padding=(0, 1))
        m_table.add_column("Metric", style="cyan")
        m_table.add_column("Value", style="bold green")
        m_table.add_row("Unique Visitors", str(metrics.get("unique_visitors", "—")))
        m_table.add_row(
            "Conversion Rate",
            f"{float(metrics.get('conversion_rate', 0)) * 100:.1f}%",
        )
        m_table.add_row("Queue Depth", str(metrics.get("current_queue_depth", "—")))
        m_table.add_row(
            "Abandonment Rate",
            f"{float(metrics.get('abandonment_rate', 0)) * 100:.1f}%",
        )
        dwell = metrics.get("avg_dwell_per_zone", {})
        if dwell:
            top_zone = max(dwell, key=lambda z: dwell[z])
            m_table.add_row("Top Dwell Zone", f"{top_zone} ({dwell[top_zone]:.0f} ms)")
        layout["metrics"].update(Panel(m_table, title="[bold]Metrics", border_style="green"))
    else:
        layout["metrics"].update(
            Panel("[yellow]Waiting for API...[/yellow]", title="[bold]Metrics", border_style="yellow")
        )

    # Heatmap panel
    if heatmap and heatmap.get("zones"):
        h_table = Table(box=box.SIMPLE, show_header=True, padding=(0, 1))
        h_table.add_column("Zone", style="cyan")
        h_table.add_column("Visits", style="green")
        h_table.add_column("Dwell (ms)", style="yellow")
        h_table.add_column("Score", style="bold")
        zones = sorted(heatmap["zones"], key=lambda z: z["normalised_score"], reverse=True)
        for z in zones[:8]:
            bar = "█" * int(z["normalised_score"] / 10)
            h_table.add_row(
                z["zone_id"],
                str(z["visit_frequency"]),
                str(z["avg_dwell_ms"]),
                f"{bar} {z['normalised_score']:.0f}",
            )
        conf = heatmap.get("data_confidence", "?")
        conf_style = "green" if conf == "HIGH" else "yellow"
        layout["heatmap"].update(
            Panel(
                h_table,
                title=f"[bold]Zone Heatmap  [[{conf_style}]{conf}[/{conf_style}]]",
                border_style="blue",
            )
        )
    else:
        layout["heatmap"].update(
            Panel("[yellow]No heatmap data yet[/yellow]", title="[bold]Zone Heatmap", border_style="yellow")
        )

    # Anomalies panel
    severity_colors = {"CRITICAL": "bold red", "WARN": "bold yellow", "INFO": "cyan"}
    if anomalies and anomalies.get("anomalies"):
        a_table = Table(box=box.SIMPLE, show_header=True, padding=(0, 1))
        a_table.add_column("Severity", style="bold")
        a_table.add_column("Type", style="cyan")
        a_table.add_column("Description")
        for a in anomalies["anomalies"]:
            sev = a.get("severity", "INFO")
            style = severity_colors.get(sev, "white")
            a_table.add_row(
                Text(sev, style=style),
                a.get("type", "?"),
                Text(a.get("description", "")[:60], overflow="ellipsis"),
            )
        layout["anomalies"].update(
            Panel(a_table, title="[bold]Anomalies", border_style="red")
        )
    else:
        layout["anomalies"].update(
            Panel("[green]No anomalies detected[/green]", title="[bold]Anomalies", border_style="green")
        )

    # Footer
    layout["footer"].update(
        Panel(
            Text(
                f"  Replayed events: {replayed_events}  |  Press Ctrl+C to exit",
                style="dim",
                justify="center",
            ),
            style="dim",
        )
    )

    return layout


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Store Intelligence Live Dashboard")
    parser.add_argument("store_id", help="Store ID to monitor (e.g. STORE_BLR_002)")
    parser.add_argument("--api", default=os.environ.get("API_URL", "http://localhost:8000"))
    parser.add_argument("--replay", default=os.environ.get("EVENTS_JSONL", "/data/events.jsonl"))
    args = parser.parse_args()

    if not RICH_AVAILABLE:
        print("ERROR: 'rich' and 'httpx' are required. Run: pip install rich httpx")
        sys.exit(1)

    client = APIClient(args.api)
    console = Console()

    # Start replay thread
    replay_thread = ReplayThread(args.replay, args.store_id, client)
    replay_thread.start()

    console.print(f"[bold green]Starting dashboard for store [cyan]{args.store_id}[/cyan]...[/bold green]")
    console.print(f"[dim]API: {args.api}  |  Replay: {args.replay}[/dim]\n")

    with Live(console=console, refresh_per_second=1, screen=True) as live:
        while True:
            now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            m = client.get_metrics(args.store_id)
            h = client.get_heatmap(args.store_id)
            a = client.get_anomalies(args.store_id)

            layout = render_dashboard(
                store_id=args.store_id,
                metrics=m,
                heatmap=h,
                anomalies=a,
                last_update=now,
                replayed_events=replay_thread.ingested_total,
            )
            live.update(layout)
            time.sleep(REFRESH_SECONDS)


if __name__ == "__main__":
    main()
