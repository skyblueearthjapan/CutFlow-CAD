"""Phase 4 — annotations endpoint and combined export integration."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from main import app


def _upload(c: TestClient, path: Path) -> tuple[str, str]:
    with path.open("rb") as fh:
        r = c.post(
            "/api/upload",
            files=[("files", (path.name, fh.read(), "application/dxf"))],
        )
    assert r.status_code == 201
    data = r.json()
    return data["session_id"], data["files"][0]["file_id"]


def test_annotations_unified_payload(
    sample_dxf_paths: dict[str, Path], isolated_store
) -> None:
    path = sample_dxf_paths["small"]
    with TestClient(app) as c:
        sid, fid = _upload(c, path)
        # Seed dimensions + notes.
        c.post(
            f"/api/session/{sid}/file/{fid}/dimensions",
            json={"dimensions": [
                {"id": "d1", "type": "linear", "p1": [0, 0], "p2": [10, 0]},
            ]},
        )
        c.post(
            f"/api/session/{sid}/file/{fid}/notes",
            json={"notes": [{"id": "n1", "position": [5, 5], "text": "test"}]},
        )

        r = c.get(f"/api/session/{sid}/file/{fid}/annotations")
        assert r.status_code == 200
        body = r.json()
        assert len(body["dimensions"]) == 1
        assert len(body["notes"]) == 1
        assert body["bridges"] == []
        assert body["added_holes"] == []
        assert body["edits"] == []


def test_export_dxf_with_phase4_overlays(
    sample_dxf_paths: dict[str, Path], isolated_store
) -> None:
    """DXF export with all Phase-4 query flags returns a valid file."""

    path = sample_dxf_paths["small"]
    with TestClient(app) as c:
        sid, fid = _upload(c, path)
        # Seed dimension + hole + note.
        c.post(
            f"/api/session/{sid}/file/{fid}/dimensions",
            json={"dimensions": [
                {"id": "d1", "type": "linear", "p1": [0, 0], "p2": [50, 0]},
            ]},
        )
        c.post(
            f"/api/session/{sid}/file/{fid}/holes",
            json={"holes": [{"id": "h1", "position": [5, 5], "diameter": 9.0}]},
        )
        c.post(
            f"/api/session/{sid}/file/{fid}/notes",
            json={"notes": [{"id": "n1", "position": [10, 10], "text": "Ra 3.2"}]},
        )

        r = c.get(
            f"/api/session/{sid}/file/{fid}/export"
            "?format=dxf&with_dimensions=true&with_added_holes=true&with_notes=true"
        )
        assert r.status_code == 200, r.text
        assert r.headers["content-type"].startswith("application/dxf") or \
               "dxf" in r.headers["content-type"].lower()
        # Sanity check: response carries DXF magic / ASCII header marker.
        assert len(r.content) > 100
        head = r.content[:512].decode("ascii", errors="replace")
        assert "SECTION" in head or "0\nSECTION" in head


def test_export_pdf_with_phase4_overlays_smoke(
    sample_dxf_paths: dict[str, Path], isolated_store
) -> None:
    path = sample_dxf_paths["small"]
    with TestClient(app) as c:
        sid, fid = _upload(c, path)
        c.post(
            f"/api/session/{sid}/file/{fid}/notes",
            json={"notes": [{"id": "n1", "position": [10, 10], "text": "test"}]},
        )
        r = c.get(
            f"/api/session/{sid}/file/{fid}/export?format=pdf&with_notes=true"
        )
        assert r.status_code == 200, r.text
        # PDF magic bytes.
        assert r.content[:4] == b"%PDF"
