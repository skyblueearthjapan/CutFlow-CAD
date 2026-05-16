"""ChamferSpec field validation (M1)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from models.schemas import ChamferSpec


# ---------------------------------------------------------------------------
# size_mm guard
# ---------------------------------------------------------------------------


def test_size_mm_zero_rejected() -> None:
    """size_mm > 0 is required — a 0 mm chamfer is meaningless."""

    with pytest.raises(ValidationError):
        ChamferSpec(corner_id="C1", size_mm=0.0, angle_deg=45.0, type="C")


def test_size_mm_above_20_rejected() -> None:
    """size_mm ≤ 20 mm — a 25 mm chamfer on a workshop plate is almost
    certainly a typo and clamping silently would mask the bug."""

    with pytest.raises(ValidationError):
        ChamferSpec(corner_id="C1", size_mm=25.0, angle_deg=45.0, type="C")


def test_size_mm_boundary_accepts_20() -> None:
    """The upper bound is inclusive (20 mm exactly is fine)."""

    spec = ChamferSpec(corner_id="C1", size_mm=20.0, angle_deg=45.0, type="C")
    assert spec.size_mm == 20.0


# ---------------------------------------------------------------------------
# angle_deg guard
# ---------------------------------------------------------------------------


def test_angle_zero_rejected() -> None:
    """angle_deg > 0 — a 0° turn is degenerate."""

    with pytest.raises(ValidationError):
        ChamferSpec(corner_id="C1", size_mm=2.0, angle_deg=0.0, type="C")


def test_angle_180_rejected() -> None:
    """angle_deg < 180 — a 180° turn is a straight line, not a chamfer."""

    with pytest.raises(ValidationError):
        ChamferSpec(corner_id="C1", size_mm=2.0, angle_deg=180.0, type="C")


# ---------------------------------------------------------------------------
# Bevel-specific minimum angle (model_validator)
# ---------------------------------------------------------------------------


def test_bevel_sub_5_deg_rejected() -> None:
    """Sub-5° bevels are below practical cutter resolution; reject upfront."""

    with pytest.raises(ValidationError):
        ChamferSpec(corner_id="E1", size_mm=2.0, angle_deg=4.5, type="bevel")


def test_bevel_5_deg_accepted() -> None:
    spec = ChamferSpec(corner_id="E1", size_mm=2.0, angle_deg=5.0, type="bevel")
    assert spec.angle_deg == 5.0


def test_c_face_4_deg_accepted() -> None:
    """The 5° floor only applies to bevel specs — C面 with <5° is allowed."""

    spec = ChamferSpec(corner_id="C1", size_mm=2.0, angle_deg=4.0, type="C")
    assert spec.angle_deg == 4.0
