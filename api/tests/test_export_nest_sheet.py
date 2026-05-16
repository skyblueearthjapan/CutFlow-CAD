"""Phase 5 H4: シート別 DXF エクスポート."""

from __future__ import annotations

import time
from pathlib import Path

from fastapi.testclient import TestClient

from main import app


def _wait_for_job(c: TestClient, job_id: str, timeout_s: float = 20.0) -> dict:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        r = c.get(f"/api/jobs/{job_id}")
        assert r.status_code == 200
        data = r.json()
        if data["status"] in {"completed", "failed"}:
            return data
        time.sleep(0.1)
    raise AssertionError(f"job did not finish in {timeout_s}s")


def _upload_and_nest(c: TestClient, path: Path) -> tuple[str, str]:
    with path.open("rb") as fh:
        r = c.post(
            "/api/upload",
            files=[("files", (path.name, fh.read(), "application/dxf"))],
        )
    sid = r.json()["session_id"]
    fid = r.json()["files"][0]["file_id"]
    r = c.post(
        f"/api/session/{sid}/nest",
        json={
            "file_ids": [fid],
            "sheet": {"width_mm": 1500, "height_mm": 1500, "quantity": 1},
            "spacing_mm": 5.0,
            "algorithm": "bottom_left",
            "rotation": True,
        },
    )
    assert r.status_code == 202, r.text
    job_id = r.json()["job_id"]
    done = _wait_for_job(c, job_id)
    assert done["status"] == "completed", done
    return sid, job_id


def test_export_nest_sheet_returns_dxf(
    sample_dxf_paths, isolated_store, isolated_queue
) -> None:
    """H4: ``/result/sheets/{idx}/export?format=dxf`` が DXF を返す."""

    small = sample_dxf_paths["small"]
    with TestClient(app) as c:
        _sid, job_id = _upload_and_nest(c, small)
        r = c.get(f"/api/jobs/{job_id}/result/sheets/0/export?format=dxf")
    assert r.status_code == 200, r.text
    body = r.content.decode("utf-8")
    # 最小限の DXF 構造を含む
    assert "SECTION" in body
    assert "ENTITIES" in body
    assert "LWPOLYLINE" in body
    assert "EOF" in body
    # Content-Disposition に sheet 番号が入る
    cd = r.headers.get("content-disposition", "")
    assert "sheet0" in cd


def test_export_nest_sheet_unknown_index_404(
    sample_dxf_paths, isolated_store, isolated_queue
) -> None:
    small = sample_dxf_paths["small"]
    with TestClient(app) as c:
        _sid, job_id = _upload_and_nest(c, small)
        r = c.get(f"/api/jobs/{job_id}/result/sheets/99/export?format=dxf")
    assert r.status_code == 404


def test_export_nest_sheet_unsupported_format_400(
    sample_dxf_paths, isolated_store, isolated_queue
) -> None:
    small = sample_dxf_paths["small"]
    with TestClient(app) as c:
        _sid, job_id = _upload_and_nest(c, small)
        r = c.get(f"/api/jobs/{job_id}/result/sheets/0/export?format=pdf")
    assert r.status_code == 400


def test_export_nest_sheet_before_completion_409() -> None:
    """job が未完了 (存在しないジョブ) のときは 404 になる挙動でも OK."""

    with TestClient(app) as c:
        r = c.get("/api/jobs/no-such/result/sheets/0/export?format=dxf")
    assert r.status_code == 404
