"""POST /delete must silently ignore unknown entity_ids (M8).

A stale client (e.g. one that fetched the file before a previous delete) may
submit IDs the server has never seen. We drop those instead of 4xx-ing — the
``deleted_count`` returned reflects the merged on-disk set of *known* ids.
"""

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
    assert r.status_code == 201, r.text
    data = r.json()
    return data["session_id"], data["files"][0]["file_id"]


def test_unknown_entity_ids_are_ignored(
    sample_dxf_paths: dict[str, Path], isolated_store
) -> None:
    path = sample_dxf_paths["small"]
    with TestClient(app) as c:
        sid, fid = _upload(c, path)

        r = c.get(f"/api/session/{sid}/file/{fid}")
        real = [e["id"] for e in r.json()["entities"][:2]]

        # Send 2 valid + 3 bogus IDs.
        bogus = ["does-not-exist", "e99999", "ghost"]
        payload = {"entity_ids": real + bogus}
        r2 = c.post(f"/api/session/{sid}/file/{fid}/delete", json=payload)
        assert r2.status_code == 200, r2.text

        body = r2.json()
        # Only the 2 real ids should have been merged.
        assert body["deleted_count"] == len(real)

        # Confirm the persisted reservation contains only the real IDs.
        r3 = c.get(f"/api/session/{sid}/file/{fid}")
        deleted_ids = set(r3.json()["deleted_ids"])
        assert deleted_ids == set(real)
        for b in bogus:
            assert b not in deleted_ids


def test_only_bogus_ids_yields_zero_delete(
    sample_dxf_paths: dict[str, Path], isolated_store
) -> None:
    path = sample_dxf_paths["small"]
    with TestClient(app) as c:
        sid, fid = _upload(c, path)
        r = c.post(
            f"/api/session/{sid}/file/{fid}/delete",
            json={"entity_ids": ["nope", "still-nope"]},
        )
    assert r.status_code == 200
    assert r.json()["deleted_count"] == 0
