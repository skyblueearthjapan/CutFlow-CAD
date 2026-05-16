"""Phase 4 — bridge service + endpoint tests."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from main import app
from models.schemas import Bridge, EntityOut
from services.bridges import auto_distribute, bridges_dxf_extras


def _upload(c: TestClient, path: Path) -> tuple[str, str]:
    with path.open("rb") as fh:
        r = c.post(
            "/api/upload",
            files=[("files", (path.name, fh.read(), "application/dxf"))],
        )
    assert r.status_code == 201
    data = r.json()
    return data["session_id"], data["files"][0]["file_id"]


def _square_payload(w: float = 100.0, h: float = 50.0) -> tuple[SimpleNamespace, list[str]]:
    ents = [
        EntityOut(id="e1", type="LINE", geom={"x1": 0.0, "y1": 0.0, "x2": w, "y2": 0.0}),
        EntityOut(id="e2", type="LINE", geom={"x1": w, "y1": 0.0, "x2": w, "y2": h}),
        EntityOut(id="e3", type="LINE", geom={"x1": w, "y1": h, "x2": 0.0, "y2": h}),
        EntityOut(id="e4", type="LINE", geom={"x1": 0.0, "y1": h, "x2": 0.0, "y2": 0.0}),
    ]
    payload = SimpleNamespace(entities=ents)
    return payload, ["e1", "e2", "e3", "e4"]


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def test_bridge_width_lower_bound() -> None:
    with pytest.raises(ValidationError):
        Bridge(id="b1", edge_id="E1", position_ratio=0.5, width_mm=0.1)


def test_bridge_width_upper_bound() -> None:
    with pytest.raises(ValidationError):
        Bridge(id="b1", edge_id="E1", position_ratio=0.5, width_mm=20.0)


def test_bridge_position_ratio_clamped() -> None:
    with pytest.raises(ValidationError):
        Bridge(id="b1", edge_id="E1", position_ratio=1.5, width_mm=2.0)


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


def test_auto_distribute_returns_n_bridges_on_square() -> None:
    payload, loop = _square_payload()
    out = auto_distribute(payload, loop, count=4, width_mm=2.0)
    assert len(out) == 4
    # Each bridge should reference an edge from the loop.
    edge_ids = {b["edge_id"] for b in out}
    assert edge_ids.issubset({"E1", "E2", "E3", "E4"})


def test_auto_distribute_count_zero_returns_empty() -> None:
    payload, loop = _square_payload()
    assert auto_distribute(payload, loop, count=0, width_mm=2.0) == []


def test_bridges_dxf_extras_creates_perpendicular_line() -> None:
    payload, loop = _square_payload(w=100.0, h=50.0)
    # Place a bridge mid-E1 (bottom edge, y=0); the perpendicular tab
    # should land vertically symmetric around (50, 0).
    out = bridges_dxf_extras(
        [{"id": "b1", "edge_id": "E1", "position_ratio": 0.5, "width_mm": 4.0}],
        payload,
        loop,
    )
    assert len(out) == 1
    s = out[0]["start"]
    e = out[0]["end"]
    # X midpoint of both endpoints == 50; spread vertically by 4mm.
    assert abs((s[0] + e[0]) / 2 - 50.0) < 1e-6
    assert abs(abs(e[1] - s[1]) - 4.0) < 1e-6


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


def test_bridges_requires_confirmed_outer(
    sample_dxf_paths: dict[str, Path], isolated_store
) -> None:
    path = sample_dxf_paths["small"]
    with TestClient(app) as c:
        sid, fid = _upload(c, path)
        r = c.post(
            f"/api/session/{sid}/file/{fid}/bridges",
            json={"bridges": []},
        )
        assert r.status_code == 422


def test_bridges_auto_after_detect_outer(
    sample_dxf_paths: dict[str, Path], isolated_store
) -> None:
    path = sample_dxf_paths["medium"]
    with TestClient(app) as c:
        sid, fid = _upload(c, path)
        d = c.post(f"/api/session/{sid}/file/{fid}/detect-outer")
        assert d.status_code == 200

        r = c.post(
            f"/api/session/{sid}/file/{fid}/bridges/auto",
            json={"count": 4, "width_mm": 2.0},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        # Auto distribution may yield <count on sparse loops; we just
        # check the contract (list, no exception, valid count).
        assert isinstance(body["bridges"], list)
        assert len(body["bridges"]) <= 4
        for b in body["bridges"]:
            assert b["width_mm"] == 2.0


def test_bridges_rejects_unknown_edge_id(
    sample_dxf_paths: dict[str, Path], isolated_store
) -> None:
    path = sample_dxf_paths["small"]
    with TestClient(app) as c:
        sid, fid = _upload(c, path)
        c.post(f"/api/session/{sid}/file/{fid}/detect-outer")
        r = c.post(
            f"/api/session/{sid}/file/{fid}/bridges",
            json={"bridges": [{"id": "b1", "edge_id": "E999",
                                "position_ratio": 0.5, "width_mm": 2.0}]},
        )
        assert r.status_code == 422
