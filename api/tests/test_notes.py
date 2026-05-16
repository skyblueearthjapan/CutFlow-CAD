"""Phase 4 — note (注記) service + endpoint tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from main import app
from models.schemas import Note
from services.notes import notes_dxf_extras


def _upload(c: TestClient, path: Path) -> tuple[str, str]:
    with path.open("rb") as fh:
        r = c.post(
            "/api/upload",
            files=[("files", (path.name, fh.read(), "application/dxf"))],
        )
    assert r.status_code == 201
    data = r.json()
    return data["session_id"], data["files"][0]["file_id"]


def test_note_rejects_empty_text() -> None:
    with pytest.raises(ValidationError):
        Note(id="n1", position=[0, 0], text="")


def test_notes_dxf_extras_routes_preset_to_layer() -> None:
    out = notes_dxf_extras([
        {"id": "n1", "position": [0, 0], "text": "Ra 3.2", "preset": "roughness"},
        {"id": "n2", "position": [0, 0], "text": "Weld", "preset": "welding"},
        {"id": "n3", "position": [0, 0], "text": "Free", "preset": "general"},
    ])
    layers = [o["layer"] for o in out]
    assert layers == ["CUTFLOW_NOTE_RA", "CUTFLOW_NOTE_WELD", "CUTFLOW_NOTE"]


def test_notes_round_trip(
    sample_dxf_paths: dict[str, Path], isolated_store
) -> None:
    path = sample_dxf_paths["small"]
    with TestClient(app) as c:
        sid, fid = _upload(c, path)
        r = c.post(
            f"/api/session/{sid}/file/{fid}/notes",
            json={
                "notes": [
                    {"id": "n1", "position": [10.0, 20.0],
                     "text": "面粗さ Ra 3.2", "preset": "roughness"},
                ]
            },
        )
        assert r.status_code == 200, r.text
        r2 = c.get(f"/api/session/{sid}/file/{fid}/notes")
        assert r2.status_code == 200
        assert r2.json()["notes"][0]["text"] == "面粗さ Ra 3.2"
