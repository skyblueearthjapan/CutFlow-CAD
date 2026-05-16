"""Phase 4 — dimension service + endpoint tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from main import app
from models.schemas import Dimension
from services.dimensions import dimensions_dxf_extras


def _upload(c: TestClient, path: Path) -> tuple[str, str]:
    with path.open("rb") as fh:
        r = c.post(
            "/api/upload",
            files=[("files", (path.name, fh.read(), "application/dxf"))],
        )
    assert r.status_code == 201, r.text
    data = r.json()
    return data["session_id"], data["files"][0]["file_id"]


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------


def test_dimension_rejects_identical_points() -> None:
    with pytest.raises(ValidationError):
        Dimension(id="d1", type="linear", p1=[0.0, 0.0], p2=[0.0, 0.0])


def test_dimension_accepts_distinct_points() -> None:
    d = Dimension(id="d1", type="linear", p1=[0.0, 0.0], p2=[100.0, 0.0])
    assert d.id == "d1"
    assert d.style == "iso"


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


def test_dimensions_dxf_extras_carries_layer() -> None:
    out = dimensions_dxf_extras(
        [{"id": "d1", "type": "linear", "p1": [0, 0], "p2": [10, 0]}]
    )
    assert len(out) == 1
    assert out[0]["layer"] == "CUTFLOW_DIM"
    assert out[0]["type"] == "linear"


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


def test_dimensions_round_trip(
    sample_dxf_paths: dict[str, Path], isolated_store
) -> None:
    path = sample_dxf_paths["small"]
    with TestClient(app) as c:
        sid, fid = _upload(c, path)
        r = c.post(
            f"/api/session/{sid}/file/{fid}/dimensions",
            json={
                "dimensions": [
                    {"id": "d1", "type": "linear", "p1": [0.0, 0.0], "p2": [100.0, 0.0]},
                    {"id": "d2", "type": "diameter", "p1": [50.0, 50.0], "p2": [60.0, 50.0]},
                ]
            },
        )
        assert r.status_code == 200, r.text
        assert len(r.json()["dimensions"]) == 2

        r2 = c.get(f"/api/session/{sid}/file/{fid}/dimensions")
        assert r2.status_code == 200
        ids = sorted(d["id"] for d in r2.json()["dimensions"])
        assert ids == ["d1", "d2"]

        # Delete one.
        r3 = c.delete(f"/api/session/{sid}/file/{fid}/dimensions/d1")
        assert r3.status_code == 204

        r4 = c.get(f"/api/session/{sid}/file/{fid}/dimensions")
        assert [d["id"] for d in r4.json()["dimensions"]] == ["d2"]


def test_dimension_delete_unknown_id_returns_404(
    sample_dxf_paths: dict[str, Path], isolated_store
) -> None:
    path = sample_dxf_paths["small"]
    with TestClient(app) as c:
        sid, fid = _upload(c, path)
        r = c.delete(f"/api/session/{sid}/file/{fid}/dimensions/ghost")
        assert r.status_code == 404


def test_dimension_rejects_degenerate_via_endpoint(
    sample_dxf_paths: dict[str, Path], isolated_store
) -> None:
    path = sample_dxf_paths["small"]
    with TestClient(app) as c:
        sid, fid = _upload(c, path)
        r = c.post(
            f"/api/session/{sid}/file/{fid}/dimensions",
            json={
                "dimensions": [
                    {"id": "bad", "type": "linear", "p1": [1.0, 1.0], "p2": [1.0, 1.0]},
                ]
            },
        )
        assert r.status_code == 422
