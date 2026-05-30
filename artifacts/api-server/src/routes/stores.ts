import { Router } from "express";

const router = Router();

const STORES = [
  { store_id: "STORE_BLR_001", name: "Apex Koramangala", city: "Bengaluru" },
  { store_id: "STORE_BLR_002", name: "Apex Indiranagar", city: "Bengaluru" },
  { store_id: "STORE_MUM_001", name: "Apex BKC", city: "Mumbai" },
];

function seed(n: number) {
  let s = Math.sin(n) * 10000;
  return s - Math.floor(s);
}

function storeIndex(storeId: string) {
  return STORES.findIndex((s) => s.store_id === storeId);
}

router.get("/stores", (_req, res) => {
  res.json(STORES);
});

router.get("/stores/:storeId/metrics", (req, res) => {
  const { storeId } = req.params;
  const idx = storeIndex(storeId);
  if (idx === -1) return res.status(404).json({ error: "Store not found" });

  const base = idx + 1;
  const visitors = 180 + Math.round(seed(base * 7) * 200);
  const convRate = 0.28 + seed(base * 3) * 0.25;
  const avgDwell = 240000 + Math.round(seed(base * 5) * 180000);
  const revenue = visitors * convRate * (320 + seed(base) * 400);

  res.json({
    store_id: storeId,
    unique_visitors: visitors,
    conversion_rate: parseFloat(convRate.toFixed(3)),
    avg_dwell_ms: avgDwell,
    total_revenue_inr: parseFloat(revenue.toFixed(2)),
    footfall_today: visitors + Math.round(seed(base * 11) * 40),
    staff_count: 4 + (idx * 2),
  });
});

router.get("/stores/:storeId/funnel", (req, res) => {
  const { storeId } = req.params;
  const idx = storeIndex(storeId);
  if (idx === -1) return res.status(404).json({ error: "Store not found" });

  const base = idx + 1;
  const entered = 300 + Math.round(seed(base * 7) * 200);
  const browsed = Math.round(entered * (0.7 + seed(base * 2) * 0.15));
  const billing = Math.round(browsed * (0.45 + seed(base * 4) * 0.15));
  const converted = Math.round(billing * (0.65 + seed(base * 6) * 0.2));
  const basket = 380 + seed(base * 9) * 420;

  res.json({
    store_id: storeId,
    entered,
    browsed,
    reached_billing: billing,
    converted,
    avg_basket_inr: parseFloat(basket.toFixed(2)),
  });
});

router.get("/stores/:storeId/heatmap", (req, res) => {
  const { storeId } = req.params;
  const idx = storeIndex(storeId);
  if (idx === -1) return res.status(404).json({ error: "Store not found" });

  const base = idx + 1;
  const zones = [
    { zone_id: "ZONE_ENTRANCE", label: "Entrance" },
    { zone_id: "ZONE_APPAREL", label: "Apparel" },
    { zone_id: "ZONE_ELECTRONICS", label: "Electronics" },
    { zone_id: "ZONE_GROCERY", label: "Grocery" },
    { zone_id: "ZONE_BILLING", label: "Billing Queue" },
    { zone_id: "ZONE_EXIT", label: "Exit" },
  ];

  const result = zones.map((z, i) => {
    const visitors = 60 + Math.round(seed(base * 13 + i) * 160);
    const dwell = 90000 + Math.round(seed(base * 17 + i) * 300000);
    const score = parseFloat((seed(base * 23 + i)).toFixed(3));
    return { ...z, visitor_count: visitors, avg_dwell_ms: dwell, score };
  });

  res.json(result);
});

router.get("/stores/:storeId/anomalies", (req, res) => {
  const { storeId } = req.params;
  const idx = storeIndex(storeId);
  if (idx === -1) return res.status(404).json({ error: "Store not found" });

  const base = idx + 1;
  const now = new Date();
  const anomalies = [
    {
      anomaly_id: `ANO_${storeId}_01`,
      type: "CROWD_SURGE",
      severity: seed(base * 3) > 0.6 ? "CRITICAL" : "WARN",
      message: `Billing queue occupancy at ${Math.round(85 + seed(base * 7) * 10)}% — above 80% threshold`,
      detected_at: new Date(now.getTime() - 12 * 60000).toISOString(),
    },
    {
      anomaly_id: `ANO_${storeId}_02`,
      type: "CONVERSION_DROP",
      severity: "WARN",
      message: `Today's conversion rate ${(0.18 + seed(base * 5) * 0.05).toFixed(1)}% vs 7-day avg ${(0.32 + seed(base * 2) * 0.05).toFixed(1)}%`,
      detected_at: new Date(now.getTime() - 47 * 60000).toISOString(),
    },
    {
      anomaly_id: `ANO_${storeId}_03`,
      type: "DWELL_SPIKE",
      severity: "INFO",
      message: `Avg dwell in Electronics up ${Math.round(40 + seed(base * 9) * 30)}% vs baseline`,
      detected_at: new Date(now.getTime() - 95 * 60000).toISOString(),
    },
  ];

  res.json(anomalies);
});

router.get("/stores/:storeId/traffic", (req, res) => {
  const { storeId } = req.params;
  const idx = storeIndex(storeId);
  if (idx === -1) return res.status(404).json({ error: "Store not found" });

  const base = idx + 1;
  const days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
  const result: { hour: number; day: string; visitors: number }[] = [];

  for (let d = 0; d < 7; d++) {
    for (let h = 9; h <= 21; h++) {
      const peak = h >= 11 && h <= 13 ? 1.5 : h >= 17 && h <= 19 ? 1.4 : 1.0;
      const weekend = d >= 5 ? 1.3 : 1.0;
      const visitors = Math.round(
        (15 + seed(base * 31 + d * 24 + h) * 40) * peak * weekend
      );
      result.push({ hour: h, day: days[d], visitors });
    }
  }

  res.json(result);
});

export default router;
