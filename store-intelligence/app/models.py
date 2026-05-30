"""
Pydantic event schema + response models for the Store Intelligence system.
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class EventType(str, enum.Enum):
    ENTRY = "ENTRY"
    EXIT = "EXIT"
    ZONE_ENTER = "ZONE_ENTER"
    ZONE_EXIT = "ZONE_EXIT"
    ZONE_DWELL = "ZONE_DWELL"
    BILLING_QUEUE_JOIN = "BILLING_QUEUE_JOIN"
    BILLING_QUEUE_ABANDON = "BILLING_QUEUE_ABANDON"
    REENTRY = "REENTRY"
    STORE_IDLE = "STORE_IDLE"  # synthetic — emitted when no detections for 5+ min


class AnomalySeverity(str, enum.Enum):
    INFO = "INFO"
    WARN = "WARN"
    CRITICAL = "CRITICAL"


class AnomalyType(str, enum.Enum):
    BILLING_QUEUE_SPIKE = "BILLING_QUEUE_SPIKE"
    CONVERSION_DROP = "CONVERSION_DROP"
    DEAD_ZONE = "DEAD_ZONE"
    STALE_CAMERA = "STALE_CAMERA"


class DataConfidence(str, enum.Enum):
    LOW = "LOW"
    HIGH = "HIGH"


# ---------------------------------------------------------------------------
# Core event schema
# ---------------------------------------------------------------------------

class EventMetadata(BaseModel):
    queue_depth: Optional[int] = None
    sku_zone: Optional[str] = None
    session_seq: Optional[int] = None

    model_config = {"extra": "allow"}


class StoreEvent(BaseModel):
    event_id: UUID
    store_id: str
    camera_id: str
    visitor_id: str
    event_type: EventType
    timestamp: datetime
    zone_id: Optional[str] = None
    dwell_ms: Optional[int] = None
    is_staff: bool = False
    confidence: float = Field(..., ge=0.0, le=1.0)
    metadata: EventMetadata = Field(default_factory=EventMetadata)

    @field_validator("visitor_id")
    @classmethod
    def visitor_id_format(cls, v: str) -> str:
        if not v.startswith("VIS_") and v != "SYNTHETIC":
            raise ValueError("visitor_id must start with 'VIS_' or be 'SYNTHETIC'")
        return v

    @field_validator("confidence")
    @classmethod
    def confidence_not_rounded_up(cls, v: float) -> float:
        # Store exactly as provided — never round up (enforced by caller)
        return v

    model_config = {"use_enum_values": True}


# ---------------------------------------------------------------------------
# Ingest request / response
# ---------------------------------------------------------------------------

class IngestRequest(BaseModel):
    events: list[StoreEvent] = Field(..., max_length=500)


class IngestError(BaseModel):
    event_id: str
    reason: str


class IngestResponse(BaseModel):
    ingested: int
    duplicates: int
    errors: list[IngestError]


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

class MetricsResponse(BaseModel):
    store_id: str
    unique_visitors: int
    conversion_rate: float
    avg_dwell_per_zone: dict[str, float]
    current_queue_depth: int
    abandonment_rate: float
    as_of: datetime


# ---------------------------------------------------------------------------
# Funnel
# ---------------------------------------------------------------------------

class FunnelStage(BaseModel):
    stage: str
    count: int
    drop_off_pct: Optional[float] = None  # None for the first stage


class FunnelResponse(BaseModel):
    store_id: str
    stages: list[FunnelStage]
    as_of: datetime


# ---------------------------------------------------------------------------
# Heatmap
# ---------------------------------------------------------------------------

class ZoneHeatmap(BaseModel):
    zone_id: str
    visit_frequency: int
    avg_dwell_ms: int
    normalised_score: float  # 0-100


class HeatmapResponse(BaseModel):
    store_id: str
    zones: list[ZoneHeatmap]
    data_confidence: DataConfidence
    as_of: datetime


# ---------------------------------------------------------------------------
# Anomaly
# ---------------------------------------------------------------------------

class Anomaly(BaseModel):
    type: AnomalyType
    severity: AnomalySeverity
    description: str
    suggested_action: str
    detected_at: datetime


class AnomaliesResponse(BaseModel):
    store_id: str
    anomalies: list[Anomaly]
    as_of: datetime


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

class HealthResponse(BaseModel):
    status: str  # "ok" | "degraded"
    last_event_per_store: dict[str, Optional[str]]
    stale_feeds: list[str]
    db_status: str  # "connected" | "unavailable"
    uptime_seconds: float
