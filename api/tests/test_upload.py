"""Upload → session-create flow."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from main import app


def test_upload_creates_session(sample_dxf_paths: dict[str, Path], isolated_store) -> None:
    path = sample_dxf_paths["small"]
    with TestClient(app) as c, path.open("rb") as fh:
        r = c.post(
            "/api/upload",
            files=[("files", (path.name, fh.read(), "application/dxf"))],
        )
    assert r.status_code == 201, r.text
    data = r.json()
    assert "session_id" in data
    assert len(data["files"]) == 1
    assert data["files"][0]["name"] == path.name
    assert data["files"][0]["status"] == "ready"
    assert data["files"][0]["size"] > 0
    assert "expires_at" in data


def test_upload_rejects_non_dxf(isolated_store) -> None:
    with TestClient(app) as c:
        r = c.post(
            "/api/upload",
            files=[("files", ("notes.txt", b"hello", "text/plain"))],
        )
    assert r.status_code == 400


def test_upload_rejects_invalid_dxf(isolated_store) -> None:
    with TestClient(app) as c:
        r = c.post(
            "/api/upload",
            files=[("files", ("garbage.dxf", b"this is not a dxf at all", "application/dxf"))],
        )
    assert r.status_code == 400


def test_get_and_delete_session(sample_dxf_paths: dict[str, Path], isolated_store) -> None:
    path = sample_dxf_paths["small"]
    with TestClient(app) as c:
        with path.open("rb") as fh:
            r = c.post(
                "/api/upload",
                files=[("files", (path.name, fh.read(), "application/dxf"))],
            )
        sid = r.json()["session_id"]

        info = c.get(f"/api/session/{sid}")
        assert info.status_code == 200
        assert info.json()["session_id"] == sid

        rm = c.delete(f"/api/session/{sid}")
        assert rm.status_code == 204

        gone = c.get(f"/api/session/{sid}")
        assert gone.status_code == 404
