import { useState, useEffect, useMemo, useRef } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { CSVLink } from "react-csv";
import {
  AreaChart, Area, BarChart, Bar,
  XAxis, YAxis, CartesianGrid, Tooltip as RechartsTooltip, Legend, ResponsiveContainer,
  Cell
} from "recharts";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Badge } from "@/components/ui/badge";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import {
  RefreshCw, ChevronDown, Check,
  Sun, Moon, Download, Printer, Store, MapPin,
  AlertTriangle, Info, AlertCircle
} from "lucide-react";
import { formatDistanceToNow } from "date-fns";

import {
  useListStores,
  useGetStoreMetrics,
  useGetStoreFunnel,
  useGetStoreHeatmap,
  useGetStoreAnomalies,
  useGetStoreTraffic
} from "@workspace/api-client-react";

const CHART_COLORS = {
  blue: "#0079F2",
  purple: "#795EFF",
  green: "#009118",
  red: "#A60808",
  pink: "#ec4899",
};

const CHART_COLOR_LIST = [
  CHART_COLORS.blue,
  CHART_COLORS.purple,
  CHART_COLORS.green,
  CHART_COLORS.red,
  CHART_COLORS.pink,
  "#d97706",
  "#06b6d4"
];

const DATA_SOURCES: string[] = ["App DB", "Computer Vision", "POS"];

const INTERVAL_OPTIONS = [
  { label: "Every 5 min", ms: 5 * 60 * 1000 },
  { label: "Every 15 min", ms: 15 * 60 * 1000 },
  { label: "Every 1 hour", ms: 60 * 60 * 1000 },
  { label: "Every 24 hours", ms: 24 * 60 * 60 * 1000 },
];

function CustomTooltip({ active, payload, label }: any) {
  if (!active || !payload || payload.length === 0) return null;
  return (
    <div style={{ backgroundColor: "#fff", borderRadius: "6px", padding: "10px 14px", border: "1px solid #e0e0e0", color: "#1a1a1a", fontSize: "13px" }}>
      <div style={{ marginBottom: "6px", fontWeight: 500, display: "flex", alignItems: "center", gap: "6px" }}>
        {payload.length === 1 && payload[0].color && payload[0].color !== "#ffffff" && (
          <span style={{ display: "inline-block", width: "10px", height: "10px", borderRadius: "2px", backgroundColor: payload[0].color, flexShrink: 0 }} />
        )}
        {label}
      </div>
      {payload.map((entry: any, index: number) => (
        <div key={index} style={{ display: "flex", alignItems: "center", gap: "8px", marginTop: "3px" }}>
          {payload.length > 1 && entry.color && entry.color !== "#ffffff" && (
            <span style={{ display: "inline-block", width: "10px", height: "10px", borderRadius: "2px", backgroundColor: entry.color, flexShrink: 0 }} />
          )}
          <span style={{ color: "#444" }}>{entry.name}</span>
          <span style={{ marginLeft: "auto", fontWeight: 600 }}>
            {typeof entry.value === "number" ? entry.value.toLocaleString() : entry.value}
          </span>
        </div>
      ))}
    </div>
  );
}

function CustomLegend({ payload }: any) {
  if (!payload || payload.length === 0) return null;
  return (
    <div style={{ display: "flex", flexWrap: "wrap", justifyContent: "center", gap: "8px 16px", fontSize: "13px" }}>
      {payload.map((entry: any, index: number) => (
        <div key={index} style={{ display: "flex", alignItems: "center", gap: "6px" }}>
          <span style={{ display: "inline-block", width: "10px", height: "10px", borderRadius: "2px", backgroundColor: entry.color, flexShrink: 0 }} />
          <span>{entry.value}</span>
        </div>
      ))}
    </div>
  );
}

export default function Dashboard() {
  const queryClient = useQueryClient();
  const [isDark, setIsDark] = useState(false);
  const [selectedStore, setSelectedStore] = useState<string>("STORE_BLR_001");

  const [autoRefresh, setAutoRefresh] = useState(false);
  const [selectedIntervalMs, setSelectedIntervalMs] = useState(5 * 60 * 1000);
  const [dropdownOpen, setDropdownOpen] = useState(false);
  const [isSpinning, setIsSpinning] = useState(false);
  const dropdownRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    document.documentElement.classList.toggle("dark", isDark);
  }, [isDark]);

  useEffect(() => {
    function handleClickOutside(e: MouseEvent) {
      if (dropdownRef.current && !dropdownRef.current.contains(e.target as Node)) {
        setDropdownOpen(false);
      }
    }
    document.addEventListener("mousedown", handleClickOutside);
    return () => document.removeEventListener("mousedown", handleClickOutside);
  }, []);

  const storesQuery = useListStores();
  const metricsQuery = useGetStoreMetrics(selectedStore);
  const funnelQuery = useGetStoreFunnel(selectedStore);
  const heatmapQuery = useGetStoreHeatmap(selectedStore);
  const anomaliesQuery = useGetStoreAnomalies(selectedStore);
  const trafficQuery = useGetStoreTraffic(selectedStore);

  const stores = storesQuery.data || [];
  const metrics = metricsQuery.data;
  const funnelData = funnelQuery.data;
  const heatmapData = heatmapQuery.data || [];
  const anomaliesData = anomaliesQuery.data || [];
  const trafficData = trafficQuery.data || [];

  const loading = 
    storesQuery.isLoading || storesQuery.isFetching ||
    metricsQuery.isLoading || metricsQuery.isFetching ||
    funnelQuery.isLoading || funnelQuery.isFetching ||
    heatmapQuery.isLoading || heatmapQuery.isFetching ||
    anomaliesQuery.isLoading || anomaliesQuery.isFetching ||
    trafficQuery.isLoading || trafficQuery.isFetching;

  useEffect(() => {
    if (loading) {
      setIsSpinning(true);
    } else {
      const t = setTimeout(() => setIsSpinning(false), 600);
      return () => clearTimeout(t);
    }
  }, [loading]);

  useEffect(() => {
    if (!autoRefresh) return;
    const interval = setInterval(() => {
      queryClient.invalidateQueries();
    }, selectedIntervalMs);
    return () => clearInterval(interval);
  }, [autoRefresh, selectedIntervalMs, queryClient]);

  const handleRefresh = () => {
    queryClient.invalidateQueries();
  };

  const lastRefreshedTs = Math.max(
    storesQuery.dataUpdatedAt,
    metricsQuery.dataUpdatedAt,
    funnelQuery.dataUpdatedAt,
    heatmapQuery.dataUpdatedAt,
    anomaliesQuery.dataUpdatedAt,
    trafficQuery.dataUpdatedAt
  );

  const lastRefreshed = lastRefreshedTs ? (() => {
    const d = new Date(lastRefreshedTs);
    const time = d.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit", hour12: true }).toLowerCase();
    const date = d.toLocaleDateString("en-US", { month: "short", day: "numeric" });
    return `${time} on ${date}`;
  })() : null;

  useEffect(() => {
    if (stores.length > 0 && !stores.find(s => s.store_id === selectedStore)) {
      setSelectedStore(stores[0].store_id);
    }
  }, [stores, selectedStore]);

  const funnelChartData = useMemo(() => {
    if (!funnelData) return [];
    return [
      { step: "Entered", count: funnelData.entered, pct: 100 },
      { step: "Browsed", count: funnelData.browsed, pct: Math.round((funnelData.browsed / funnelData.entered) * 100) || 0 },
      { step: "Billing", count: funnelData.reached_billing, pct: Math.round((funnelData.reached_billing / funnelData.entered) * 100) || 0 },
      { step: "Converted", count: funnelData.converted, pct: Math.round((funnelData.converted / funnelData.entered) * 100) || 0 },
    ];
  }, [funnelData]);

  const sortedHeatmapData = useMemo(() => {
    return [...heatmapData].sort((a, b) => b.score - a.score);
  }, [heatmapData]);

  const transformedTrafficData = useMemo(() => {
    const dataByHour: Record<number, any> = {};
    const days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
    for (let i = 0; i < 24; i++) {
      dataByHour[i] = { hour: `${i}:00` };
      days.forEach(d => dataByHour[i][d] = 0);
    }
    trafficData.forEach(pt => {
      if (dataByHour[pt.hour]) {
        dataByHour[pt.hour][pt.day] = pt.visitors;
      }
    });
    return Object.values(dataByHour);
  }, [trafficData]);

  const formatDwellTime = (ms: number) => {
    const totalSecs = Math.floor(ms / 1000);
    const m = Math.floor(totalSecs / 60);
    const s = totalSecs % 60;
    if (m > 0) return `${m} min ${s}s`;
    return `${s}s`;
  };

  const gridColor = isDark ? "rgba(255,255,255,0.08)" : "#e5e5e5";
  const tickColor = isDark ? "#98999C" : "#71717a";

  return (
    <div className="min-h-screen bg-background px-5 py-4 pt-[32px] pb-[32px] pl-[24px] pr-[24px]">
      <div className="max-w-[1400px] mx-auto">
        <div className="mb-4 flex flex-wrap items-start justify-between gap-x-4 gap-y-2">
          <div className="pt-2">
            <h1 className="font-bold text-[32px]">Store Intelligence</h1>
            <p className="text-muted-foreground mt-1.5 text-[14px]">Real-time footfall and conversion tracking</p>
            {DATA_SOURCES.length > 0 && (
              <div className="flex flex-wrap items-center gap-1.5 mt-2">
                <span className="text-[12px] text-muted-foreground shrink-0">Data Sources:</span>
                {DATA_SOURCES.map((source) => (
                  <span
                    key={source}
                    className="text-[12px] font-bold rounded px-2 py-0.5 truncate print:!bg-[rgb(229,231,235)] print:!text-[rgb(75,85,99)]"
                    title={source}
                    style={{
                      maxWidth: "20ch",
                      backgroundColor: isDark ? "rgba(255,255,255,0.1)" : "rgb(229, 231, 235)",
                      color: isDark ? "#c8c9cc" : "rgb(75, 85, 99)",
                    }}
                  >
                    {source}
                  </span>
                ))}
              </div>
            )}
            {lastRefreshed && <p className="text-[12px] text-muted-foreground mt-3">Last refresh: {lastRefreshed}</p>}
          </div>
          <div className="flex items-center gap-3 pt-2 print:hidden">
            <div className="w-[200px]">
              <Select value={selectedStore} onValueChange={setSelectedStore} disabled={stores.length === 0}>
                <SelectTrigger className="h-[26px] text-xs">
                  <SelectValue placeholder="Select a store" />
                </SelectTrigger>
                <SelectContent>
                  {stores.map(store => (
                    <SelectItem key={store.store_id} value={store.store_id}>
                      {store.name} ({store.city})
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            
            <div className="relative" ref={dropdownRef}>
              <div
                className="flex items-center rounded-[6px] overflow-hidden h-[26px] text-[12px]"
                style={{
                  backgroundColor: isDark ? "rgba(255,255,255,0.1)" : "#F0F1F2",
                  color: isDark ? "#c8c9cc" : "#4b5563",
                }}
              >
                <button onClick={handleRefresh} disabled={loading} className="flex items-center gap-1 px-2 h-full hover:bg-black/5 dark:hover:bg-white/10 transition-colors disabled:opacity-50">
                  <RefreshCw className={`w-3.5 h-3.5 ${isSpinning ? "animate-spin" : ""}`} />
                  Refresh
                </button>
                <div className="w-px h-4 shrink-0" style={{ backgroundColor: isDark ? "rgba(255,255,255,0.15)" : "rgba(0,0,0,0.15)" }} />
                <button onClick={() => setDropdownOpen((o) => !o)} className="flex items-center justify-center px-1.5 h-full hover:bg-black/5 dark:hover:bg-white/10 transition-colors">
                  <ChevronDown className="w-3.5 h-3.5" />
                </button>
              </div>
              {dropdownOpen && (
                <div className="absolute right-0 top-full mt-1 w-48 bg-popover border rounded-md shadow-md overflow-hidden z-50 p-1">
                  <div className="flex items-center justify-between px-3 py-2 text-sm">
                    <span className="font-medium text-popover-foreground">Auto-refresh</span>
                    <button
                      onClick={() => setAutoRefresh(!autoRefresh)}
                      className={`w-8 h-4 rounded-full transition-colors relative ${autoRefresh ? 'bg-primary' : 'bg-muted'}`}
                    >
                      <span className={`absolute top-0.5 w-3 h-3 bg-white rounded-full transition-transform ${autoRefresh ? 'left-[18px]' : 'left-0.5'}`} />
                    </button>
                  </div>
                  <div className="h-px bg-border my-1" />
                  {INTERVAL_OPTIONS.map((opt) => (
                    <button
                      key={opt.ms}
                      onClick={() => {
                        setSelectedIntervalMs(opt.ms);
                        setAutoRefresh(true);
                        setDropdownOpen(false);
                      }}
                      className="w-full flex items-center justify-between px-3 py-1.5 text-sm hover:bg-accent hover:text-accent-foreground rounded-sm text-left"
                    >
                      {opt.label}
                      {selectedIntervalMs === opt.ms && <Check className="w-4 h-4" />}
                    </button>
                  ))}
                </div>
              )}
            </div>

            <button
              onClick={() => window.print()}
              disabled={loading}
              className="flex items-center justify-center w-[26px] h-[26px] rounded-[6px] transition-colors disabled:opacity-50"
              style={{ backgroundColor: isDark ? "rgba(255,255,255,0.1)" : "#F0F1F2", color: isDark ? "#c8c9cc" : "#4b5563" }}
              aria-label="Export as PDF"
            >
              <Printer className="w-3.5 h-3.5" />
            </button>
            <button
              onClick={() => setIsDark((d) => !d)}
              className="flex items-center justify-center w-[26px] h-[26px] rounded-[6px] transition-colors"
              style={{ backgroundColor: isDark ? "rgba(255,255,255,0.1)" : "#F0F1F2", color: isDark ? "#c8c9cc" : "#4b5563" }}
              aria-label="Toggle dark mode"
            >
              {isDark ? <Sun className="w-3.5 h-3.5" /> : <Moon className="w-3.5 h-3.5" />}
            </button>
          </div>
        </div>

        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4 mb-4">
          <Card>
            <CardContent className="p-6">
              {loading && !metrics ? (
                <div className="space-y-2"><Skeleton className="h-4 w-24" /><Skeleton className="h-8 w-32" /></div>
              ) : metrics ? (
                <>
                  <p className="text-sm text-muted-foreground">Unique Visitors</p>
                  <p className="text-2xl font-bold mt-1" style={{ color: CHART_COLORS.blue }}>
                    {metrics.unique_visitors.toLocaleString()}
                  </p>
                </>
              ) : (
                <><p className="text-sm text-muted-foreground">Unique Visitors</p><p className="text-2xl font-bold mt-1">--</p></>
              )}
            </CardContent>
          </Card>
          <Card>
            <CardContent className="p-6">
              {loading && !metrics ? (
                <div className="space-y-2"><Skeleton className="h-4 w-24" /><Skeleton className="h-8 w-32" /></div>
              ) : metrics ? (
                <>
                  <p className="text-sm text-muted-foreground">Conversion Rate</p>
                  <p className="text-2xl font-bold mt-1" style={{ color: CHART_COLORS.blue }}>
                    {metrics.conversion_rate.toFixed(1)}%
                  </p>
                </>
              ) : (
                <><p className="text-sm text-muted-foreground">Conversion Rate</p><p className="text-2xl font-bold mt-1">--</p></>
              )}
            </CardContent>
          </Card>
          <Card>
            <CardContent className="p-6">
              {loading && !metrics ? (
                <div className="space-y-2"><Skeleton className="h-4 w-24" /><Skeleton className="h-8 w-32" /></div>
              ) : metrics ? (
                <>
                  <p className="text-sm text-muted-foreground">Avg Dwell Time</p>
                  <p className="text-2xl font-bold mt-1" style={{ color: CHART_COLORS.blue }}>
                    {formatDwellTime(metrics.avg_dwell_ms)}
                  </p>
                </>
              ) : (
                <><p className="text-sm text-muted-foreground">Avg Dwell Time</p><p className="text-2xl font-bold mt-1">--</p></>
              )}
            </CardContent>
          </Card>
          <Card>
            <CardContent className="p-6">
              {loading && !metrics ? (
                <div className="space-y-2"><Skeleton className="h-4 w-24" /><Skeleton className="h-8 w-32" /></div>
              ) : metrics ? (
                <>
                  <p className="text-sm text-muted-foreground">Revenue Today</p>
                  <p className="text-2xl font-bold mt-1" style={{ color: CHART_COLORS.blue }}>
                    ₹{metrics.total_revenue_inr.toLocaleString()}
                  </p>
                </>
              ) : (
                <><p className="text-sm text-muted-foreground">Revenue Today</p><p className="text-2xl font-bold mt-1">--</p></>
              )}
            </CardContent>
          </Card>
        </div>

        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 mb-4">
          <Card>
            <CardHeader className="px-4 pt-4 pb-2 flex-row items-center justify-between space-y-0">
              <CardTitle className="text-base">Conversion Funnel</CardTitle>
              {!loading && funnelChartData.length > 0 && (
                <CSVLink data={funnelChartData} filename="conversion-funnel.csv" className="print:hidden flex items-center justify-center w-[26px] h-[26px] rounded-[6px] transition-colors hover:opacity-80" style={{ backgroundColor: isDark ? "rgba(255,255,255,0.1)" : "#F0F1F2", color: isDark ? "#c8c9cc" : "#4b5563" }} aria-label="Export chart data as CSV">
                  <Download className="w-3.5 h-3.5" />
                </CSVLink>
              )}
            </CardHeader>
            <CardContent>
              {loading && funnelChartData.length === 0 ? <Skeleton className="w-full h-[300px]" /> : (
                <ResponsiveContainer width="100%" height={300} debounce={0}>
                  <BarChart data={funnelChartData} layout="vertical" margin={{ top: 10, right: 30, left: 20, bottom: 5 }}>
                    <CartesianGrid strokeDasharray="3 3" horizontal={true} vertical={false} stroke={gridColor} />
                    <XAxis type="number" tick={{ fontSize: 12, fill: tickColor }} stroke={tickColor} />
                    <YAxis type="category" dataKey="step" tick={{ fontSize: 12, fill: tickColor }} stroke={tickColor} width={80} />
                    <RechartsTooltip content={<CustomTooltip />} isAnimationActive={false} cursor={{ fill: 'rgba(0,0,0,0.05)', stroke: 'none' }} />
                    <Bar dataKey="count" name="Visitors" fill={CHART_COLORS.blue} fillOpacity={0.8} activeBar={{ fillOpacity: 1 }} radius={[0, 4, 4, 0]} isAnimationActive={false}>
                      {funnelChartData.map((entry, index) => (
                        <Cell key={`cell-${index}`} fill={CHART_COLORS.blue} />
                      ))}
                    </Bar>
                  </BarChart>
                </ResponsiveContainer>
              )}
            </CardContent>
          </Card>

          <Card>
            <CardHeader className="px-4 pt-4 pb-2 flex-row items-center justify-between space-y-0">
              <CardTitle className="text-base">Zone Heatmap</CardTitle>
              {!loading && sortedHeatmapData.length > 0 && (
                <CSVLink data={sortedHeatmapData} filename="zone-heatmap.csv" className="print:hidden flex items-center justify-center w-[26px] h-[26px] rounded-[6px] transition-colors hover:opacity-80" style={{ backgroundColor: isDark ? "rgba(255,255,255,0.1)" : "#F0F1F2", color: isDark ? "#c8c9cc" : "#4b5563" }} aria-label="Export chart data as CSV">
                  <Download className="w-3.5 h-3.5" />
                </CSVLink>
              )}
            </CardHeader>
            <CardContent>
              {loading && sortedHeatmapData.length === 0 ? <Skeleton className="w-full h-[300px]" /> : (
                <ResponsiveContainer width="100%" height={300} debounce={0}>
                  <BarChart data={sortedHeatmapData} layout="vertical" margin={{ top: 10, right: 30, left: 30, bottom: 5 }}>
                    <CartesianGrid strokeDasharray="3 3" horizontal={true} vertical={false} stroke={gridColor} />
                    <XAxis type="number" tick={{ fontSize: 12, fill: tickColor }} stroke={tickColor} />
                    <YAxis type="category" dataKey="label" tick={{ fontSize: 12, fill: tickColor }} stroke={tickColor} width={100} />
                    <RechartsTooltip content={<CustomTooltip />} isAnimationActive={false} cursor={{ fill: 'rgba(0,0,0,0.05)', stroke: 'none' }} />
                    <Bar dataKey="visitor_count" name="Visitors" fill={CHART_COLORS.purple} fillOpacity={0.8} activeBar={{ fillOpacity: 1 }} radius={[0, 4, 4, 0]} isAnimationActive={false} />
                  </BarChart>
                </ResponsiveContainer>
              )}
            </CardContent>
          </Card>
        </div>

        <Card className="mb-4">
          <CardHeader className="px-4 pt-4 pb-2 flex-row items-center justify-between space-y-0">
            <CardTitle className="text-base">Hourly Traffic</CardTitle>
            {!loading && transformedTrafficData.length > 0 && (
              <CSVLink data={transformedTrafficData} filename="hourly-traffic.csv" className="print:hidden flex items-center justify-center w-[26px] h-[26px] rounded-[6px] transition-colors hover:opacity-80" style={{ backgroundColor: isDark ? "rgba(255,255,255,0.1)" : "#F0F1F2", color: isDark ? "#c8c9cc" : "#4b5563" }} aria-label="Export chart data as CSV">
                <Download className="w-3.5 h-3.5" />
              </CSVLink>
            )}
          </CardHeader>
          <CardContent>
            {loading && transformedTrafficData.length === 0 ? <Skeleton className="w-full h-[300px]" /> : (
              <ResponsiveContainer width="100%" height={300} debounce={0}>
                <AreaChart data={transformedTrafficData}>
                  <CartesianGrid strokeDasharray="3 3" stroke={gridColor} />
                  <XAxis dataKey="hour" tick={{ fontSize: 12, fill: tickColor }} stroke={tickColor} />
                  <YAxis tick={{ fontSize: 12, fill: tickColor }} stroke={tickColor} />
                  <RechartsTooltip content={<CustomTooltip />} isAnimationActive={false} cursor={{ fill: 'rgba(0,0,0,0.05)', stroke: 'none' }} />
                  <Legend content={<CustomLegend />} />
                  {["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"].map((day, idx) => (
                    <Area 
                      key={day} 
                      type="monotone" 
                      dataKey={day} 
                      name={day} 
                      stroke={CHART_COLOR_LIST[idx % CHART_COLOR_LIST.length]} 
                      fill="none" 
                      strokeWidth={2} 
                      isAnimationActive={false} 
                      activeDot={{ r: 5, strokeWidth: 2 }} 
                    />
                  ))}
                </AreaChart>
              </ResponsiveContainer>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="px-4 pt-4 pb-2">
            <CardTitle className="text-base">Detected Anomalies</CardTitle>
          </CardHeader>
          <CardContent>
            {loading && anomaliesData.length === 0 ? (
              <div className="space-y-2">
                <Skeleton className="h-10 w-full" />
                {[...Array(3)].map((_, i) => <Skeleton key={i} className="h-12 w-full" />)}
              </div>
            ) : anomaliesData.length === 0 ? (
              <div className="py-8 text-center text-muted-foreground text-sm flex flex-col items-center">
                <Check className="w-8 h-8 text-green-500 mb-2 opacity-50" />
                No anomalies detected
              </div>
            ) : (
              <div className="rounded-md border overflow-x-auto">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Type</TableHead>
                      <TableHead>Severity</TableHead>
                      <TableHead className="w-[50%]">Message</TableHead>
                      <TableHead className="text-right">Detected</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {anomaliesData.map((anomaly) => {
                      const sev = anomaly.severity.toUpperCase();
                      return (
                        <TableRow key={anomaly.anomaly_id}>
                          <TableCell className="font-medium">{anomaly.type}</TableCell>
                          <TableCell>
                            <Badge variant={sev === 'CRITICAL' ? 'destructive' : 'outline'} className={
                              sev === 'CRITICAL' ? 'bg-red-100 text-red-800 dark:bg-red-900 dark:text-red-200' :
                              sev === 'WARN' ? 'bg-amber-100 text-amber-800 dark:bg-amber-900 dark:text-amber-200' :
                              'bg-blue-100 text-blue-800 dark:bg-blue-900 dark:text-blue-200'
                            }>
                              {sev}
                            </Badge>
                          </TableCell>
                          <TableCell>{anomaly.message}</TableCell>
                          <TableCell className="text-right text-muted-foreground whitespace-nowrap">
                            {formatDistanceToNow(new Date(anomaly.detected_at), { addSuffix: true })}
                          </TableCell>
                        </TableRow>
                      );
                    })}
                  </TableBody>
                </Table>
              </div>
            )}
          </CardContent>
        </Card>

      </div>
    </div>
  );
}
