"""Frame cleanup tests (Phase 3)."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from main import app
from models.schemas import (
    BoundingBox,
    DeleteCandidates,
    EntityOut,
    FileEntities,
    Stats,
)
from services.frame_cleanup import detect_frame_entities


def _upload(c: TestClient, path: Path) -> tuple[str, str]:
    with path.open("rb") as fh:
        r = c.post(
            "/api/upload",
            files=[("files", (path.name, fh.read(), "application/dxf"))],
        )
    assert r.status_code == 201, r.text
    data = r.json()
    return data["session_id"], data["files"][0]["file_id"]


def _make_payload(
    candidate_frame_ids: list[str],
    frame_categories: dict[str, str],
    extras: list[EntityOut] | None = None,
) -> FileEntities:
    entities: list[EntityOut] = [
        EntityOut(id=eid, type="LWPOLYLINE", category=frame_categories.get(eid, "other"),
                  layer="0", color=256, geom={"vertices": [[0, 0], [100, 0]], "closed": False})
        for eid in frame_categories
    ]
    if extras:
        entities.extend(extras)
    return FileEntities(
        file_id="fid",
        name="test.dxf",
        bounding_box=BoundingBox(min_x=0, min_y=0, max_x=100, max_y=100),
        entities=entities,
        delete_candidates=DeleteCandidates(FRAME=candidate_frame_ids),
        stats=Stats(total=len(entities), by_category={"frame": len(candidate_frame_ids)}),
        units="mm",
    )


# ---------------------------------------------------------------------------
# Unit tests for the heuristic
# ---------------------------------------------------------------------------


def test_detect_frame_entities_uses_classifier_bucket() -> None:
    payload = _make_payload(
        candidate_frame_ids=["e00001"],
        frame_categories={"e00001": "frame", "e00002": "other"},
    )
    out = detect_frame_entities(payload)
    assert out == ["e00001"]


def test_detect_frame_entities_excludes_outer_loop() -> None:
    payload = _make_payload(
        candidate_frame_ids=["e00001"],
        frame_categories={"e00001": "frame"},
    )
    out = detect_frame_entities(payload, outer_loop=["e00001"])
    assert out == []


def test_detect_frame_entities_picks_up_frame_category_inserts() -> None:
    extras = [
        EntityOut(id="e00010", type="INSERT", category="frame", layer="0",
                  color=256, geom={"x": 0, "y": 0, "name": "FRAME"})
    ]
    payload = _make_payload(
        candidate_frame_ids=[],
        frame_categories={},
        extras=extras,
    )
    out = detect_frame_entities(payload)
    assert out == ["e00010"]


# ---------------------------------------------------------------------------
# Endpoint smoke test (requires a sample with a frame to find)
# ---------------------------------------------------------------------------


def test_cleanup_frame_endpoint_returns_ids(
    sample_dxf_paths: dict[str, Path], isolated_store
) -> None:
    path = sample_dxf_paths["large"]  # ベースフレーム likely has a frame
    with TestClient(app) as c:
        sid, fid = _upload(c, path)
        r = c.post(f"/api/session/{sid}/file/{fid}/cleanup-frame")
        assert r.status_code == 200, r.text
        body = r.json()
        assert "removed_count" in body
        assert "frame_entity_ids" in body
        assert body["removed_count"] == len(body["frame_entity_ids"])
        # IDs must round-trip into the file's delete reservation.
        if body["removed_count"]:
            r2 = c.get(f"/api/session/{sid}/file/{fid}")
            deleted = set(r2.json()["deleted_ids"])
            for eid in body["frame_entity_ids"]:
                assert eid in deleted
