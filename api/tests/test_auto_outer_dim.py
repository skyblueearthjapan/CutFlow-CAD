"""Phase 4 — POST /dimensions/auto-outer endpoint tests.

Verifies the auto-outer-dimension endpoint that generates two linear
dimensions (top width / right height) from the confirmed outer-loop bbox.

These tests synthesise a tiny DXF in ``tmp_path`` so they run anywhere
without depending on the optional sample fixtures.
"""

from __future__ import annotations

from pathlib import Path

import ezdxf
from fastapi.testclient import TestClient

from main import app


def _make_square_dxf(dest: Path, w: float = 100.0, h: float = 50.0) -> None:
    """Write a closed-square DXF that the outer detector picks up as the loop."""

    doc = ezdxf.new("R2010")
    msp = doc.modelspace()
    msp.add_line((0.0, 0.0), (w, 0.0))
    msp.add_line((w, 0.0), (w, h))
    msp.add_line((w, h), (0.0, h))
    msp.add_line((0.0, h), (0.0, 0.0))
    # A small inner circle so the loop is judged as a real part outline
    # (the detector rejects bare 4-vertex rectangles as drawing frames).
    msp.add_circle((w / 2, h / 2), 5.0)
    doc.saveas(str(dest))


def _upload(c: TestClient, path: Path) -> tuple[str, str]:
    with path.open("rb") as fh:
        r = c.post(
            "/api/upload",
            files=[("files", (path.name, fh.read(), "application/dxf"))],
        )
    assert r.status_code == 201, r.text
    data = r.json()
    return data["session_id"], data["files"][0]["file_id"]


def test_auto_outer_dim_requires_outer(tmp_path: Path, isolated_store) -> None:
    """Without a confirmed outer, the endpoint must return 409."""

    src = tmp_path / "square.dxf"
    _make_square_dxf(src)
    with TestClient(app) as c:
        sid, fid = _upload(c, src)
        r = c.post(f"/api/session/{sid}/file/{fid}/dimensions/auto-outer")
        assert r.status_code == 409, r.text
        body = r.json()
        # FastAPI wraps string details under ``detail``.
        detail = body.get("detail")
        assert isinstance(detail, str)
        assert "外径" in detail


def test_auto_outer_dim_adds_two_dims(tmp_path: Path, isolated_store) -> None:
    """After detect-outer succeeds, the endpoint appends 2 dims with a plausible bbox."""

    w, h = 100.0, 50.0
    src = tmp_path / "square.dxf"
    _make_square_dxf(src, w=w, h=h)
    with TestClient(app) as c:
        sid, fid = _upload(c, src)
        d = c.post(f"/api/session/{sid}/file/{fid}/detect-outer")
        assert d.status_code == 200, d.text

        r = c.post(f"/api/session/{sid}/file/{fid}/dimensions/auto-outer")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["added"] == 2
        assert len(body["dimensions"]) == 2

        bbox = body["bbox"]
        # Square is [0,0] – [100,50] plus a circle of r=5 at centre → bbox
        # unchanged on x (0..100) and y (0..50).
        assert abs(bbox["min_x"]) < 1e-6
        assert abs(bbox["min_y"]) < 1e-6
        assert abs(bbox["max_x"] - w) < 1e-6
        assert abs(bbox["max_y"] - h) < 1e-6
        assert abs(body["width"] - w) < 1e-6
        assert abs(body["height"] - h) < 1e-6

        # Verify the two dims are linear and oriented top (horizontal) /
        # right (vertical).
        dims = body["dimensions"]
        top, right = dims[0], dims[1]
        assert top["type"] == "linear"
        assert right["type"] == "linear"
        # Top dim: horizontal — y values equal, above max_y.
        assert abs(top["p1"][1] - top["p2"][1]) < 1e-6
        assert top["p1"][1] > bbox["max_y"]
        # Right dim: vertical — x values equal, to the right of max_x.
        assert abs(right["p1"][0] - right["p2"][0]) < 1e-6
        assert right["p1"][0] > bbox["max_x"]

        # A subsequent call appends another 2 dims (no clobber).
        r2 = c.post(f"/api/session/{sid}/file/{fid}/dimensions/auto-outer")
        assert r2.status_code == 200, r2.text
        assert r2.json()["added"] == 2
        assert len(r2.json()["dimensions"]) == 4
