"""
Re-ID / tracking logic for cross-camera deduplication and re-entry detection.

Strategy: cosine distance on bounding-box trajectory embeddings combined with
colour histogram of the torso region. This avoids a heavy torchreid dependency
while still providing reasonable cross-camera dedup for the dataset scale
(5 stores × 3 cameras × 20-min clips).

Cross-camera dedup window: 30 seconds (configurable via REEID_WINDOW_SECONDS).
Re-ID embedding dimension: 128 (trajectory + HSV histogram).
"""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

REEID_WINDOW_SECONDS: float = 30.0
REEID_COSINE_THRESHOLD: float = 0.85  # similarity >= this → same visitor
IDLE_THRESHOLD_SECONDS: float = 300.0  # 5 minutes → STORE_IDLE


@dataclass
class TrackState:
    tracker_id: int
    visitor_id: str
    store_id: str
    camera_id: str
    session_start_frame: int
    last_seen_frame: int
    last_seen_time: float  # wall-clock for idle detection
    embedding: Optional[np.ndarray]  # 128-d, may be None initially
    zones_visited: list[str] = field(default_factory=list)
    in_billing_zone: bool = False
    billing_entry_time: Optional[float] = None
    is_staff: bool = False
    exited: bool = False
    session_seq: int = 0


class GlobalReIDRegistry:
    """
    Maintains a global registry of Re-ID embeddings for cross-camera dedup.

    On each new detection, we check all recently-seen embeddings from other
    cameras within REEID_WINDOW_SECONDS. If cosine similarity >= threshold,
    we treat it as the same visitor and return their existing visitor_id.
    """

    def __init__(self) -> None:
        # visitor_id → (embedding, last_seen_wall_time, camera_id)
        self._registry: dict[str, tuple[np.ndarray, float, str]] = {}

    def register(
        self,
        visitor_id: str,
        embedding: np.ndarray,
        camera_id: str,
    ) -> None:
        self._registry[visitor_id] = (embedding, time.time(), camera_id)

    def find_match(
        self,
        embedding: np.ndarray,
        current_camera_id: str,
        now: Optional[float] = None,
    ) -> Optional[str]:
        """
        Returns the existing visitor_id if a sufficiently similar embedding
        was seen on a *different* camera within the dedup window.
        Returns None if no match found (= genuinely new visitor on this camera).
        """
        if now is None:
            now = time.time()

        best_sim = -1.0
        best_vid: Optional[str] = None

        for vid, (emb, last_seen, cam_id) in list(self._registry.items()):
            if cam_id == current_camera_id:
                continue
            if now - last_seen > REEID_WINDOW_SECONDS:
                continue
            sim = cosine_similarity(embedding, emb)
            if sim > best_sim:
                best_sim = sim
                best_vid = vid

        if best_sim >= REEID_COSINE_THRESHOLD:
            return best_vid
        return None

    def evict_stale(self, now: Optional[float] = None) -> None:
        if now is None:
            now = time.time()
        stale = [
            vid
            for vid, (_, last_seen, _) in self._registry.items()
            if now - last_seen > REEID_WINDOW_SECONDS * 4
        ]
        for vid in stale:
            del self._registry[vid]


class CameraTracker:
    """
    Per-camera tracking state machine. Wraps ByteTrack output from ultralytics
    and manages:
      - visitor_id assignment (new vs re-entry)
      - cross-camera dedup via GlobalReIDRegistry
      - zone dwell tracking
      - billing queue state
      - idle detection
    """

    def __init__(
        self,
        store_id: str,
        camera_id: str,
        reid_registry: GlobalReIDRegistry,
        fps: float = 15.0,
    ) -> None:
        self.store_id = store_id
        self.camera_id = camera_id
        self.reid_registry = reid_registry
        self.fps = fps

        # tracker_id (ByteTrack) → TrackState
        self._active: dict[int, TrackState] = {}
        # visitor_id → TrackState for re-entry lookup
        self._exited: dict[str, TrackState] = {}
        # queue depth for this camera's billing zone
        self.queue_depth: int = 0

        self._last_detection_wall: float = time.time()

    # ------------------------------------------------------------------
    # Embedding extraction
    # ------------------------------------------------------------------

    def extract_embedding(
        self,
        frame: "np.ndarray",
        bbox: tuple[int, int, int, int],
    ) -> np.ndarray:
        """
        128-d embedding: 64-d bounding-box trajectory features +
        64-d HSV colour histogram of torso region.

        The trajectory component is seeded from bbox geometry (aspect ratio,
        area, centroid normalised by frame dims). In production you'd
        accumulate frames; here we derive a stable per-detection descriptor
        so cross-camera matching works on a single frame.
        """
        x1, y1, x2, y2 = bbox
        h_frame, w_frame = frame.shape[:2] if frame.ndim >= 2 else (1080, 1920)

        cx = (x1 + x2) / 2 / w_frame
        cy = (y1 + y2) / 2 / h_frame
        bw = (x2 - x1) / w_frame
        bh = (y2 - y1) / h_frame
        aspect = bh / (bw + 1e-6)
        area = bw * bh

        geo_feat = np.array([cx, cy, bw, bh, aspect, area], dtype=np.float32)
        geo_feat = np.tile(geo_feat, 64 // len(geo_feat) + 1)[:64]

        # HSV histogram of torso (middle 1/3 of bbox vertically)
        torso_h_color = np.zeros(64, dtype=np.float32)
        if frame.ndim == 3 and frame.size > 0:
            try:
                import cv2  # type: ignore

                torso_y1 = int(y1 + (y2 - y1) * 0.33)
                torso_y2 = int(y1 + (y2 - y1) * 0.66)
                torso = frame[max(0, torso_y1): torso_y2, max(0, x1): x2]
                if torso.size > 0:
                    hsv = cv2.cvtColor(torso, cv2.COLOR_BGR2HSV)
                    hist = cv2.calcHist([hsv], [0], None, [64], [0, 180])
                    hist = hist.flatten().astype(np.float32)
                    norm = hist.sum() + 1e-6
                    torso_h_color = hist / norm
            except Exception:
                pass

        embedding = np.concatenate([geo_feat, torso_h_color])
        norm = np.linalg.norm(embedding) + 1e-6
        return embedding / norm

    # ------------------------------------------------------------------
    # Staff classification
    # ------------------------------------------------------------------

    def is_staff_bbox(
        self,
        frame: "np.ndarray",
        bbox: tuple[int, int, int, int],
        staff_uniform_hsv: Optional[dict] = None,
    ) -> bool:
        """
        Classify staff by:
        1. Bounding-box aspect ratio (staff are typically taller / narrower).
        2. Colour histogram of torso region vs reference uniform HSV range.
        """
        x1, y1, x2, y2 = bbox
        bw = x2 - x1
        bh = y2 - y1
        aspect = bh / (bw + 1e-6)

        # Heuristic: staff bbox is tall and narrow (aspect > 2.5)
        if aspect > 2.5:
            if staff_uniform_hsv is None:
                return True  # No reference — fall back to aspect alone

        if staff_uniform_hsv is None:
            return False

        try:
            import cv2  # type: ignore

            torso_y1 = int(y1 + (y2 - y1) * 0.33)
            torso_y2 = int(y1 + (y2 - y1) * 0.66)
            torso = frame[max(0, torso_y1): torso_y2, max(0, x1): x2]
            if torso.size == 0:
                return False

            hsv = cv2.cvtColor(torso, cv2.COLOR_BGR2HSV)
            h_mean = float(hsv[:, :, 0].mean())
            s_mean = float(hsv[:, :, 1].mean())
            v_mean = float(hsv[:, :, 2].mean())

            h_lo = staff_uniform_hsv.get("h_lo", 0)
            h_hi = staff_uniform_hsv.get("h_hi", 180)
            s_lo = staff_uniform_hsv.get("s_lo", 0)
            v_lo = staff_uniform_hsv.get("v_lo", 0)

            return h_lo <= h_mean <= h_hi and s_mean >= s_lo and v_mean >= v_lo
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Zone resolution
    # ------------------------------------------------------------------

    @staticmethod
    def resolve_zone(
        cx: float,
        cy: float,
        zones: list[dict],
    ) -> Optional[str]:
        """Return the first zone whose polygon contains (cx, cy)."""
        for zone in zones:
            pts = zone.get("polygon", [])
            if not pts:
                # Fallback: use bounding rect
                rect = zone.get("rect", {})
                x1z = rect.get("x1", 0)
                y1z = rect.get("y1", 0)
                x2z = rect.get("x2", 0)
                y2z = rect.get("y2", 0)
                if x1z <= cx <= x2z and y1z <= cy <= y2z:
                    return zone.get("zone_id")
            else:
                if _point_in_polygon(cx, cy, pts):
                    return zone.get("zone_id")
        return None

    # ------------------------------------------------------------------
    # Idle check
    # ------------------------------------------------------------------

    def seconds_since_last_detection(self) -> float:
        return time.time() - self._last_detection_wall

    def mark_detection(self) -> None:
        self._last_detection_wall = time.time()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    a = a.flatten().astype(np.float64)
    b = b.flatten().astype(np.float64)
    denom = (np.linalg.norm(a) + 1e-9) * (np.linalg.norm(b) + 1e-9)
    return float(np.dot(a, b) / denom)


def _point_in_polygon(px: float, py: float, polygon: list[list[float]]) -> bool:
    """Ray-casting algorithm for point-in-polygon."""
    n = len(polygon)
    inside = False
    x, y = px, py
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i][0], polygon[i][1]
        xj, yj = polygon[j][0], polygon[j][1]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi + 1e-9) + xi):
            inside = not inside
        j = i
    return inside
