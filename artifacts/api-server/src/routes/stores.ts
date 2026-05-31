import { Router } from "express";
import fs from "fs";
import path from "path";

const router = Router();

// ── Real data from pipeline ────────────────────────────────────────────────
// process_videos.py + export_dashboard_data.py write this file.
// We reload it on every request so a fresh export is reflected immediately.

// __dirname resolves to artifacts/api-server/dist/ (set by esbuild banner)
// so ../../../ walks back to workspace root
const DATA_FILE = path.resolve(
  __dirname,
  "../../../store-intelligence/data/dashboard_data.json"
);

interface DashboardData {
  stores: { store_id: string; name: string; city: string }[];
  metrics: Record<string, object>;
  funnel: Record<string, object>;
  heatmap: Record<string, object[]>;
  anomalies: Record<string, object[]>;
  traffic: Record<string, object[]>;
}

function loadRealData(): DashboardData | null {
  try {
    if (fs.existsSync(DATA_FILE)) {
      const raw = fs.readFileSync(DATA_FILE, "utf-8");
      return JSON.parse(raw) as DashboardData;
    }
  } catch {
    // fall through to synthetic
  }
  return null;
}

// ── Synthetic fallback ─────────────────────────────────────────────────────

const SYNTHETIC_STORES = [
  { store_id: "STORE_BLR_001", name: "Apex Koramangala", city: "Bengaluru" },
  { store_id: "STORE_BLR_002", name: "Apex Indiranagar", city: "Bengaluru" },
  { store_id: "STORE_MUM_001", name: "Apex BKC", city: "Mumbai" },
];

function seed(n: number) {
  const s = Math.sin(n) * 10000;
  return s - Math.floor(s);
}

function syntheticMetrics(storeId: string) {
  const idx = SYNTHETIC_STORES.findIndex((s) => s.store_id === storeId) + 1;
  const visitors = 180 + Math.round(seed(idx * 7) * 200);
  const convRate = 0.28 + seed(idx * 3) * 0.25;
  const avgDwell = 240000 + Math.round(seed(idx * 5) * 180000);
  return {
    store_id: storeId,
    unique_visitors: visitors,
    conversion_rate: parseFloat(convRate.toFixed(3)),
    avg_dwell_ms: avgDwell,
    total_revenue_inr: parseFloat((visitors * convRate * (320 + seed(idx) * 400)).toFixed(2)),
    footfall_today: visitors + Math.round(seed(idx * 11) * 40),
    staff_count: 4 + (idx * 2),
  };
}

function syntheticFunnel(storeId: string) {
  const idx = SYNTHETIC_STORES.findIndex((s) => s.store_id === storeId) + 1;
  const entered = 300 + Math.round(seed(idx * 7) * 200);
  const browsed = Math.round(entered * (0.7 + seed(idx * 2) * 0.15));
  const billing = Math.round(browsed * (0.45 + seed(idx * 4) * 0.15));
  const converted = Math.round(billing * (0.65 + seed(idx * 6) * 0.2));
  return {
    store_id: storeId,
    entered,
    browsed,
    reached_billing: billing,
    converted,
    avg_basket_inr: parseFloat((380 + seed(idx * 9) * 420).toFixed(2)),
  };
}

function syntheticHeatmap(storeId: string) {
  const idx = SYNTHETIC_STORES.findIndex((s) => s.store_id === storeId) + 1;
  const zones = [
    { zone_id: "ZONE_ENTRANCE", label: "Entrance" },
    { zone_id: "ZONE_APPAREL", label: "Apparel" },
    { zone_id: "ZONE_ELECTRONICS", label: "Electronics" },
    { zone_id: "ZONE_GROCERY", label: "Grocery" },
    { zone_id: "ZONE_BILLING", label: "Billing Queue" },
    { zone_id: "ZONE_EXIT", label: "Exit" },
  ];
  return zones.map((z, i) => ({
    ...z,
    visitor_count: 60 + Math.round(seed(idx * 13 + i) * 160),
    avg_dwell_ms: 90000 + Math.round(seed(idx * 17 + i) * 300000),
    score: parseFloat(seed(idx * 23 + i).toFixed(3)),
  }));
}

function syntheticAnomalies(storeId: string) {
  const idx = SYNTHETIC_STORES.findIndex((s) => s.store_id === storeId) + 1;
  const now = new Date();
  return [
    {
      anomaly_id: `ANO_${storeId}_01`,
      type: "CROWD_SURGE",
      severity: seed(idx * 3) > 0.6 ? "CRITICAL" : "WARN",
      message: `Billing queue at ${Math.round(85 + seed(idx * 7) * 10)}% capacity`,
      detected_at: new Date(now.getTime() - 12 * 60000).toISOString(),
    },
    {
      anomaly_id: `ANO_${storeId}_02`,
      type: "CONVERSION_DROP",
      severity: "WARN",
      message: `Conversion rate ${(0.18 + seed(idx * 5) * 0.05).toFixed(1)}% vs 7-day avg`,
      detected_at: new Date(now.getTime() - 47 * 60000).toISOString(),
    },
  ];
}

function syntheticTraffic(storeId: string) {
  const idx = SYNTHETIC_STORES.findIndex((s) => s.store_id === storeId) + 1;
  const days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
  const result: { hour: number; day: string; visitors: number }[] = [];
  for (let d = 0; d < 7; d++) {
    for (let h = 9; h <= 21; h++) {
      const peak = (h >= 11 && h <= 13) ? 1.5 : (h >= 17 && h <= 19) ? 1.4 : 1.0;
      const weekend = d >= 5 ? 1.3 : 1.0;
      result.push({
        hour: h,
        day: days[d],
        visitors: Math.round((15 + seed(idx * 31 + d * 24 + h) * 40) * peak * weekend),
      });
    }
  }
  return result;
}

// ── Routes ─────────────────────────────────────────────────────────────────

router.get("/stores", (_req, res) => {
  const real = loadRealData();
  res.json(real ? real.stores : SYNTHETIC_STORES);
});

router.get("/stores/:storeId/metrics", (req, res) => {
  const { storeId } = req.params;
  const real = loadRealData();
  if (real?.metrics[storeId]) return res.json(real.metrics[storeId]);
  const idx = SYNTHETIC_STORES.findIndex((s) => s.store_id === storeId);
  if (idx === -1) return res.status(404).json({ error: "Store not found" });
  res.json(syntheticMetrics(storeId));
});

router.get("/stores/:storeId/funnel", (req, res) => {
  const { storeId } = req.params;
  const real = loadRealData();
  if (real?.funnel[storeId]) return res.json(real.funnel[storeId]);
  const idx = SYNTHETIC_STORES.findIndex((s) => s.store_id === storeId);
  if (idx === -1) return res.status(404).json({ error: "Store not found" });
  res.json(syntheticFunnel(storeId));
});

router.get("/stores/:storeId/heatmap", (req, res) => {
  const { storeId } = req.params;
  const real = loadRealData();
  if (real?.heatmap[storeId]) return res.json(real.heatmap[storeId]);
  const idx = SYNTHETIC_STORES.findIndex((s) => s.store_id === storeId);
  if (idx === -1) return res.status(404).json({ error: "Store not found" });
  res.json(syntheticHeatmap(storeId));
});

router.get("/stores/:storeId/anomalies", (req, res) => {
  const { storeId } = req.params;
  const real = loadRealData();
  if (real?.anomalies[storeId]) return res.json(real.anomalies[storeId]);
  const idx = SYNTHETIC_STORES.findIndex((s) => s.store_id === storeId);
  if (idx === -1) return res.status(404).json({ error: "Store not found" });
  res.json(syntheticAnomalies(storeId));
});

router.get("/stores/:storeId/traffic", (req, res) => {
  const { storeId } = req.params;
  const real = loadRealData();
  if (real?.traffic[storeId]) return res.json(real.traffic[storeId]);
  const idx = SYNTHETIC_STORES.findIndex((s) => s.store_id === storeId);
  if (idx === -1) return res.status(404).json({ error: "Store not found" });
  res.json(syntheticTraffic(storeId));
});

export default router;
