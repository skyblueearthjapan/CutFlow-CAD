"""Phase 6 CRITICAL fix — viewBox / coordinate-space alignment.

ezdxf's ``SVGBackend.get_string`` emits the inner ``<path>`` coordinates
in a *normalized* space (``Settings.output_coordinate_space``, default
1,000,000) with Y already flipped. The outer SVG viewBox previously
echoed that normalized space, which collided with the frontend stripping
the outer ``<svg>`` and re-hosting the children under a DXF-mm viewBox —
the background paths ended up far outside the visible region.

The Phase 6 fix wraps the inner content in a
``<g transform="translate(min_x min_y) scale(s s)">`` group that maps
the normalized space back to DXF mm coordinates, and rewrites the outer
viewBox to ``"<min_x> <min_y> <w_mm> <h_mm>"`` so the standalone SVG
still renders correctly.

These tests pin that contract: after a render, every coordinate emitted
inside a ``<path d="...">`` must, **once the surrounding ``<g
transform>`` is applied**, fall inside the DXF mm bbox.
"""

from __future__ import annotations

import re
from pathlib import Path

import ezdxf
import pytest

from services.svg_render import _OUTPUT_COORD_SPACE, render_dxf_to_svg


def _write_line_dxf(path: Path, p1: tuple[float, float], p2: tuple[float, float]) -> None:
    doc = ezdxf.new()
    doc.modelspace().add_line(p1, p2)
    doc.saveas(str(path))


def _write_box_dxf(
    path: Path, x0: float, y0: float, x1: float, y1: float
) -> None:
    doc = ezdxf.new()
    msp = doc.modelspace()
    msp.add_line((x0, y0), (x1, y0))
    msp.add_line((x1, y0), (x1, y1))
    msp.add_line((x1, y1), (x0, y1))
    msp.add_line((x0, y1), (x0, y0))
    doc.saveas(str(path))


_VIEWBOX_RE = re.compile(r'viewBox="([^"]+)"')
_TRANSFORM_RE = re.compile(
    r'<g\s+transform="translate\(([-\d.]+)\s+([-\d.]+)\)\s+scale\(([-\d.eE]+)\s+([-\d.eE]+)\)"'
)


def _parse_viewbox(svg: str) -> tuple[float, float, float, float]:
    m = _VIEWBOX_RE.search(svg)
    assert m, f"no viewBox in svg: {svg[:200]}"
    parts = [float(v) for v in m.group(1).split()]
    assert len(parts) == 4, parts
    return parts[0], parts[1], parts[2], parts[3]


def _parse_wrapper_transform(svg: str) -> tuple[float, float, float, float]:
    m = _TRANSFORM_RE.search(svg)
    assert m, f"wrapper transform missing from rewritten svg: {svg[:400]}"
    return float(m.group(1)), float(m.group(2)), float(m.group(3)), float(m.group(4))


def _extract_path_xy_coords(svg: str) -> list[tuple[float, float]]:
    """Return every absolute (x, y) pair from path ``M``/``L`` commands.

    We only look at absolute uppercase commands followed by two numbers
    because that's what ezdxf emits for line moves. Relative (``l``)
    segments are ignored — they encode deltas, not coordinates, and the
    test only needs *some* absolute samples to confirm the mapping.
    """

    coords: list[tuple[float, float]] = []
    for d in re.findall(r'd="([^"]+)"', svg):
        # Capture each absolute ``M`` or ``L`` followed by two floats.
        for m in re.finditer(
            r"[ML]\s*(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)", d
        ):
            coords.append((float(m.group(1)), float(m.group(2))))
    return coords


def test_viewbox_matches_dxf_mm_bbox(tmp_path: Path) -> None:
    """A 100×50 mm box renders with a DXF-mm viewBox, not the normalized space."""

    p = tmp_path / "box.dxf"
    _write_box_dxf(p, 0.0, 0.0, 100.0, 50.0)
    result = render_dxf_to_svg(p, dark_theme=True)

    vb = _parse_viewbox(result["svg"])
    assert vb == pytest.approx((0.0, 0.0, 100.0, 50.0), abs=1e-6)
    assert result["width"] == pytest.approx(100.0, abs=1e-6)
    assert result["height"] == pytest.approx(50.0, abs=1e-6)


def test_inner_paths_inside_dxf_mm_bbox_after_wrapper(tmp_path: Path) -> None:
    """Once the wrapper transform is applied, every emitted absolute
    coordinate sits inside the DXF mm bbox (with a generous tolerance for
    the small floating-point drift the transform introduces)."""

    p = tmp_path / "line.dxf"
    _write_line_dxf(p, (0.0, 0.0), (100.0, 0.0))
    result = render_dxf_to_svg(p, dark_theme=True)
    svg = result["svg"]

    # Degenerate height (single horizontal line) collapses ezdxf's output
    # to a stub — re-run with a square box so we have actual paths to
    # inspect.
    if "<path" not in svg:
        _write_box_dxf(p, 10.0, 20.0, 110.0, 70.0)
        result = render_dxf_to_svg(p, dark_theme=True)
        svg = result["svg"]

    tx, ty, sx, sy = _parse_wrapper_transform(svg)
    bbox = result["bbox"]
    coords = _extract_path_xy_coords(svg)
    assert coords, "render produced no path coordinates to verify"

    # Map each inner SVG coordinate through the wrapper transform and
    # confirm it ends up inside the DXF mm bbox (with 1e-3 mm slack).
    for cx, cy in coords:
        dx = tx + sx * cx
        dy = ty + sy * cy
        assert bbox["min_x"] - 1e-3 <= dx <= bbox["max_x"] + 1e-3, (
            f"path x {cx} → {dx} outside bbox {bbox}"
        )
        assert bbox["min_y"] - 1e-3 <= dy <= bbox["max_y"] + 1e-3, (
            f"path y {cy} → {dy} outside bbox {bbox}"
        )


def test_viewbox_with_offset_min(tmp_path: Path) -> None:
    """A drawing whose min corner is not at the origin still gets the
    correct DXF-mm viewBox and a translate that lands on the min corner."""

    p = tmp_path / "offset.dxf"
    _write_box_dxf(p, 100.0, 200.0, 300.0, 350.0)
    result = render_dxf_to_svg(p, dark_theme=True)
    svg = result["svg"]

    vb = _parse_viewbox(svg)
    assert vb == pytest.approx((100.0, 200.0, 200.0, 150.0), abs=1e-6)

    tx, ty, sx, sy = _parse_wrapper_transform(svg)
    assert tx == pytest.approx(100.0, abs=1e-6)
    assert ty == pytest.approx(200.0, abs=1e-6)
    # Both scales equal w_mm / OUTPUT_COORD_SPACE (aspect-preserving).
    expected_scale = 200.0 / _OUTPUT_COORD_SPACE
    assert sx == pytest.approx(expected_scale, rel=1e-6)
    assert sy == pytest.approx(expected_scale, rel=1e-6)
