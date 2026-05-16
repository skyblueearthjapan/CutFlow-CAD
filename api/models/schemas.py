"""Pydantic models exchanged over the CutFlow CAD HTTP API.

All numeric values are in DXF native units (millimeters in our sample set).
Coordinates are returned in raw DXF space (Y axis pointing up); the frontend
is responsible for SVG axis inversion based on ``bounding_box``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Category taxonomy
# ---------------------------------------------------------------------------

EntityCategory = Literal["outer", "hole", "dim", "balloon", "tap", "frame", "other"]
"""SVG class hint used by the frontend (matches ``.ent.<category>``)."""


# ---------------------------------------------------------------------------
# Geometry containers
# ---------------------------------------------------------------------------


class BoundingBox(BaseModel):
    """Axis-aligned bounding box in DXF coordinates."""

    min_x: float
    min_y: float
    max_x: float
    max_y: float


class EntityOut(BaseModel):
    """Single DXF entity serialised for the SVG canvas.

    ``geom`` is intentionally typed as ``dict[str, Any]`` because the shape
    depends on ``type`` (LINE / CIRCLE / ARC / LWPOLYLINE / TEXT / INSERT /
    DIMENSION / HATCH). See ``services.dxf_parser`` for the per-type schema.
    """

    id: str
    type: str
    category: EntityCategory = "other"
    color: int = 256  # 256 = BYLAYER
    layer: str = "0"
    geom: dict[str, Any] = Field(default_factory=dict)


class DeleteCandidates(BaseModel):
    """Entity IDs grouped by the delete-mode checkbox they correspond to."""

    model_config = ConfigDict(populate_by_name=True)

    DIMENSION: list[str] = Field(default_factory=list)
    BALLOON: list[str] = Field(default_factory=list)
    TAP: list[str] = Field(default_factory=list)
    FRAME: list[str] = Field(default_factory=list)


class Stats(BaseModel):
    total: int
    by_category: dict[str, int]


# ---------------------------------------------------------------------------
# Session / file payloads
# ---------------------------------------------------------------------------


class FileMeta(BaseModel):
    file_id: str
    name: str
    size: int
    status: Literal["ready", "error"] = "ready"
    error: str | None = None


class SessionInfo(BaseModel):
    session_id: str
    files: list[FileMeta]
    expires_at: datetime


class FileEntities(BaseModel):
    file_id: str
    name: str
    bounding_box: BoundingBox
    entities: list[EntityOut]
    delete_candidates: DeleteCandidates
    stats: Stats
    units: str = "mm"
    deleted_ids: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Delete / export payloads
# ---------------------------------------------------------------------------


class DeleteRequest(BaseModel):
    entity_ids: list[str] = Field(default_factory=list)


class DeleteResponse(BaseModel):
    deleted_count: int
    remaining: int


# ---------------------------------------------------------------------------
# Outer-detection / offset payloads (Phase 2)
# ---------------------------------------------------------------------------


OuterStatus = Literal["success", "low_confidence", "failed"]


class OuterLoopSummary(BaseModel):
    """Geometric summary of an outer-loop candidate."""

    closed: bool
    segments: int
    lines: int
    arcs: int
    perimeter: float
    area: float
    bounding_box: BoundingBox


class OuterCandidate(BaseModel):
    """Single outer-loop candidate (the runner-ups surfaced to the UI)."""

    loop: list[str] = Field(default_factory=list)
    confidence: float
    area: float
    method: str = "graph"


class OuterDetectionResult(BaseModel):
    """Outer-shape detection response.

    ``status`` collapses confidence into one of:

    * ``success``         — c >= 0.80, frontend can auto-confirm
    * ``low_confidence``  — 0.50 <= c < 0.80, frontend nudges the user
    * ``failed``          — c < 0.50, frontend forces manual selection
    """

    status: OuterStatus
    confidence: float
    method: str = ""
    outer_loop: list[str] = Field(default_factory=list)
    loop_summary: OuterLoopSummary | None = None
    warnings: list[str] = Field(default_factory=list)
    candidates: list[OuterCandidate] = Field(default_factory=list)


class OuterManualRequest(BaseModel):
    """User-supplied entity-id chain claimed to form the outer loop."""

    entity_ids: list[str] = Field(default_factory=list)


class OffsetVertex(BaseModel):
    """LWPOLYLINE vertex with optional bulge."""

    x: float
    y: float
    bulge: float = 0.0


class OffsetLoop(BaseModel):
    """Result polyline of an outer-offset computation."""

    type: Literal["LWPOLYLINE"] = "LWPOLYLINE"
    vertices: list[list[float]] = Field(default_factory=list)
    closed: bool = True


class OffsetRequest(BaseModel):
    """Outer-offset (加工代) request body.

    ``edge_overrides`` keys are 1-based edge labels (``"E1"`` .. ``"En"``) in
    loop traversal order — they add ON TOP of ``default_mm`` rather than
    replacing it (matches the spec's "simple per-edge additive" decision).
    """

    default_mm: float = Field(3.0, ge=0.0, le=200.0)
    edge_overrides: dict[str, float] = Field(default_factory=dict)
    corner_join: Literal["arc", "miter"] = "arc"


class OffsetResult(BaseModel):
    offset_loop: OffsetLoop
    perimeter: float
    area: float
    bounding_box: BoundingBox
    plate_size: str  # e.g. "446 × 286 mm"
    material_efficiency: float
    warnings: list[str] = Field(default_factory=list)
