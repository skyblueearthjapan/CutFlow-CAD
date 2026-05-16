"""Phase 6 — ezdxf SVGBackend rendering tests.

Covers:
* The service helper renders a tiny synthetic DXF to a real SVG string.
* The bounding box returned by the helper matches the DXF extents.
* ``exclude_entity_ids`` removes the corresponding draw call.
* The ``/render-svg`` endpoint returns JSON with the same fields and
  reflects the per-session delete reservation.
"""

from __future__ import annotations

import re
from pathlib import Path

import ezdxf
import pytest
from fastapi.testclient import TestClient

from main import app
from services.svg_render import entity_id_to_handle_map, render_dxf_to_svg


def _write_sample_dxf(path: Path) -> None:
    """Synthesize a small DXF with three lines + a circle for tests."""

    doc = ezdxf.new()
    msp = doc.modelspace()
    msp.add_line((0, 0), (100, 0))
    msp.add_line((0, 50), (100, 50))
    msp.add_line((0, 0), (0, 50))
    msp.add_circle((50, 25), 10)
    doc.saveas(str(path))


def test_render_dxf_to_svg_basic(tmp_path: Path) -> None:
    p = tmp_path / "sample.dxf"
    _write_sample_dxf(p)

    result = render_dxf_to_svg(p, dark_theme=True)
    svg = result["svg"]

    assert isinstance(svg, str)
    assert svg.startswith("<?xml") or svg.lstrip().startswith("<?xml")
    assert "<svg" in svg
    assert "</svg>" in svg

    bbox = result["bbox"]
    # Sample geometry spans (0,0)-(100,50) plus a circle of r=10 centred at
    # (50,25) — the circle stays within the rectangle so the rectangle wins.
    assert bbox["min_x"] == pytest.approx(0.0, abs=1e-6)
    assert bbox["min_y"] == pytest.approx(0.0, abs=1e-6)
    assert bbox["max_x"] == pytest.approx(100.0, abs=1e-6)
    assert bbox["max_y"] == pytest.approx(50.0, abs=1e-6)
    assert result["width"] == pytest.approx(100.0, abs=1e-6)
    assert result["height"] == pytest.approx(50.0, abs=1e-6)


def test_render_dxf_to_svg_excludes_entity_id(tmp_path: Path) -> None:
    p = tmp_path / "sample.dxf"
    _write_sample_dxf(p)

    full = render_dxf_to_svg(p, dark_theme=True)
    # Count drawn path/line/polyline elements as a stable proxy for
    # "things rendered". The exact tag varies with line styling.
    full_count = len(re.findall(r"<(?:path|line|polyline|circle)", full["svg"]))

    # Drop the first line (e00000) and re-render.
    partial = render_dxf_to_svg(
        p, dark_theme=True, exclude_entity_ids={"e00000"}
    )
    partial_count = len(re.findall(r"<(?:path|line|polyline|circle)", partial["svg"]))
    assert partial_count < full_count, (
        f"exclude_entity_ids did not reduce rendered elements "
        f"(full={full_count}, partial={partial_count})"
    )


def test_entity_id_to_handle_map_round_trip(tmp_path: Path) -> None:
    """The eid → handle mapping is deterministic and complete."""

    p = tmp_path / "sample.dxf"
    _write_sample_dxf(p)

    mapping = entity_id_to_handle_map(p)
    assert len(mapping) == 4  # 3 lines + 1 circle
    assert set(mapping.keys()) == {f"e{i:05d}" for i in range(4)}
    # Each handle is a non-empty hex-ish string.
    for h in mapping.values():
        assert h
        int(h, 16)  # raises if not valid hex


def test_render_dxf_to_svg_dark_vs_light(tmp_path: Path) -> None:
    """Dark theme switches the colour policy; the SVG bytes should differ."""

    p = tmp_path / "sample.dxf"
    _write_sample_dxf(p)
    dark = render_dxf_to_svg(p, dark_theme=True)["svg"]
    light = render_dxf_to_svg(p, dark_theme=False)["svg"]
    assert dark != light


# ---------------------------------------------------------------------------
# HTTP endpoint
# ---------------------------------------------------------------------------


def _upload(c: TestClient, path: Path) -> tuple[str, str]:
    with path.open("rb") as fh:
        r = c.post(
            "/api/upload",
            files=[("files", (path.name, fh.read(), "application/dxf"))],
        )
    assert r.status_code == 201, r.text
    data = r.json()
    return data["session_id"], data["files"][0]["file_id"]


def test_render_svg_endpoint(tmp_path: Path, isolated_store) -> None:
    p = tmp_path / "sample.dxf"
    _write_sample_dxf(p)

    with TestClient(app) as c:
        sid, fid = _upload(c, p)
        r = c.get(f"/api/session/{sid}/file/{fid}/render-svg")
        assert r.status_code == 200, r.text
        body = r.json()

    assert "svg" in body and "bbox" in body
    assert "<svg" in body["svg"]
    bbox = body["bbox"]
    assert bbox["max_x"] > bbox["min_x"]
    assert bbox["max_y"] > bbox["min_y"]
    assert body["width"] > 0
    assert body["height"] > 0


def test_render_svg_endpoint_respects_deletions(
    tmp_path: Path, isolated_store
) -> None:
    """A reserved deletion shrinks the rendered element count."""

    p = tmp_path / "sample.dxf"
    _write_sample_dxf(p)

    with TestClient(app) as c:
        sid, fid = _upload(c, p)

        r0 = c.get(f"/api/session/{sid}/file/{fid}/render-svg")
        assert r0.status_code == 200
        before = len(re.findall(r"<(?:path|line|polyline|circle)", r0.json()["svg"]))

        # Reserve the first entity for deletion.
        rd = c.post(
            f"/api/session/{sid}/file/{fid}/delete",
            json={"entity_ids": ["e00000"]},
        )
        assert rd.status_code == 200, rd.text

        r1 = c.get(f"/api/session/{sid}/file/{fid}/render-svg")
        after = len(re.findall(r"<(?:path|line|polyline|circle)", r1.json()["svg"]))
        assert after < before

        # apply_deletions=false brings the entity back.
        r2 = c.get(
            f"/api/session/{sid}/file/{fid}/render-svg",
            params={"apply_deletions": "false"},
        )
        restored = len(re.findall(r"<(?:path|line|polyline|circle)", r2.json()["svg"]))
        assert restored == before


def test_render_svg_endpoint_missing_session() -> None:
    with TestClient(app) as c:
        r = c.get("/api/session/does-not-exist/file/x/render-svg")
        assert r.status_code in (404, 410)
