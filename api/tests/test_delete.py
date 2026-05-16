"""End-to-end: upload → parse → delete reservation → export."""

from __future__ import annotations

from pathlib import Path

import ezdxf
from ezdxf import recover
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


def test_delete_and_export(sample_dxf_paths: dict[str, Path], isolated_store) -> None:
    path = sample_dxf_paths["medium"]

    with TestClient(app) as c:
        sid, fid = _upload(c, path)

        # 1) Fetch entity payload.
        r = c.get(f"/api/session/{sid}/file/{fid}")
        assert r.status_code == 200, r.text
        payload = r.json()
        cands = payload["delete_candidates"]
        all_targets = (
            cands["DIMENSION"] + cands["BALLOON"] + cands["TAP"] + cands["FRAME"]
        )
        assert all_targets, "expected at least one delete candidate"

        # Pick a small subset so the test is deterministic & quick.
        to_delete = all_targets[:5]

        # 2) Reserve deletion.
        r = c.post(
            f"/api/session/{sid}/file/{fid}/delete",
            json={"entity_ids": to_delete},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["deleted_count"] == len(to_delete)
        assert body["remaining"] == payload["stats"]["total"] - len(to_delete)

        # 3) Round-trip via re-fetch: deleted_ids field reflects the reservation.
        r2 = c.get(f"/api/session/{sid}/file/{fid}")
        assert set(r2.json()["deleted_ids"]) == set(to_delete)

        # 4) Export the cleaned DXF.
        r = c.get(f"/api/session/{sid}/file/{fid}/export")
        assert r.status_code == 200, r.text
        assert r.headers["content-type"].startswith("application/")
        assert "_clean.dxf" in r.headers.get("content-disposition", "")

        out_bytes = r.content
        assert len(out_bytes) > 0

    # 5) Validate the exported file still parses and has fewer modelspace entities.
    out_tmp = Path("test_export_clean.dxf")
    try:
        out_tmp.write_bytes(out_bytes)
        cleaned, _ = recover.readfile(str(out_tmp))
        orig, _ = recover.readfile(str(path))
        orig_count = sum(1 for _ in orig.modelspace())
        clean_count = sum(1 for _ in cleaned.modelspace())
        assert clean_count == orig_count - len(to_delete)
    finally:
        out_tmp.unlink(missing_ok=True)


def test_delete_idempotent(sample_dxf_paths: dict[str, Path], isolated_store) -> None:
    """Sending the same IDs twice should not double-count."""

    path = sample_dxf_paths["small"]
    with TestClient(app) as c:
        sid, fid = _upload(c, path)
        r = c.get(f"/api/session/{sid}/file/{fid}")
        ids = [e["id"] for e in r.json()["entities"][:2]]

        r1 = c.post(f"/api/session/{sid}/file/{fid}/delete", json={"entity_ids": ids})
        r2 = c.post(f"/api/session/{sid}/file/{fid}/delete", json={"entity_ids": ids})
        assert r1.json()["deleted_count"] == 2
        assert r2.json()["deleted_count"] == 2  # still 2, not 4
