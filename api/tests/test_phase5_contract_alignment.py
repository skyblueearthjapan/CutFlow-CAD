"""Phase 5 contract alignment — FE/BE wire-shape snapshots.

両 Opus レビューで合算 CRITICAL 指摘が出ていた箇所をピン留めする。
壊れたリネームをここで早期に検出するためのガード。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from main import app


def _upload_one(c: TestClient, path: Path) -> tuple[str, str]:
    with path.open("rb") as fh:
        r = c.post(
            "/api/upload",
            files=[("files", (path.name, fh.read(), "application/dxf"))],
        )
    assert r.status_code == 201, r.text
    data = r.json()
    return data["session_id"], data["files"][0]["file_id"]


# ---------------------------------------------------------------------------
# C1 — NestRequest 受理スキーマ (sheet wrapper)
# ---------------------------------------------------------------------------


def test_nest_request_accepts_sheet_wrapper(
    sample_dxf_paths: dict[str, Path], isolated_store, isolated_queue
) -> None:
    """C1: BE が ``sheet: {width_mm, height_mm, quantity}`` の wrapper を受ける."""

    small = sample_dxf_paths["small"]
    with TestClient(app) as c:
        sid, fid = _upload_one(c, small)
        r = c.post(
            f"/api/session/{sid}/nest",
            json={
                "file_ids": [fid],
                "sheet": {"width_mm": 1000, "height_mm": 1000, "quantity": 1},
                "spacing_mm": 5.0,
                "algorithm": "bottom_left",
                "rotation": True,
            },
        )
        assert r.status_code == 202, r.text
        body = r.json()
        assert "job_id" in body
        assert body["status"] == "pending"


def test_nest_request_rejects_flat_legacy_keys(
    sample_dxf_paths: dict[str, Path], isolated_store, isolated_queue
) -> None:
    """C1: 旧 FE shape (``sheet_width`` / ``allow_rotate``) は拒否される."""

    small = sample_dxf_paths["small"]
    with TestClient(app) as c:
        sid, fid = _upload_one(c, small)
        r = c.post(
            f"/api/session/{sid}/nest",
            json={
                "file_ids": [fid],
                # 旧 FE flat keys — BE は sheet wrapper を要求するため 422
                "sheet_width": 1000,
                "sheet_height": 1000,
                "spacing_mm": 5.0,
                "algorithm": "bottom_left",
                "allow_rotate": True,
            },
        )
        assert r.status_code == 422, r.text


# ---------------------------------------------------------------------------
# C2 — JobStatus enum (pending / running / completed / failed)
# ---------------------------------------------------------------------------


def test_job_status_uses_pending_completed_failed(
    sample_dxf_paths: dict[str, Path], isolated_store, isolated_queue
) -> None:
    """C2: ジョブの status 値が BE 形式 (``pending`` → 最終 ``completed``)."""

    import time

    small = sample_dxf_paths["small"]
    with TestClient(app) as c:
        sid, fid = _upload_one(c, small)
        r = c.post(
            f"/api/session/{sid}/nest",
            json={
                "file_ids": [fid],
                "sheet": {"width_mm": 1000, "height_mm": 1000, "quantity": 1},
                "spacing_mm": 5.0,
                "algorithm": "bottom_left",
                "rotation": True,
            },
        )
        job_id = r.json()["job_id"]
        # poll until terminal
        deadline = time.monotonic() + 20.0
        status = None
        while time.monotonic() < deadline:
            jr = c.get(f"/api/jobs/{job_id}")
            assert jr.status_code == 200
            status = jr.json()["status"]
            # status must be one of the BE enum values
            assert status in {"pending", "running", "completed", "failed"}, status
            if status in {"completed", "failed"}:
                break
            time.sleep(0.1)
        assert status == "completed", status


# ---------------------------------------------------------------------------
# C3 — GET /jobs/{id}/result envelope
# ---------------------------------------------------------------------------


def test_job_result_envelope_keys(
    sample_dxf_paths: dict[str, Path], isolated_store, isolated_queue
) -> None:
    """C3: ``/result`` レスポンスが ``{sheets, unplaced, warnings, utilization}``."""

    import time

    small = sample_dxf_paths["small"]
    with TestClient(app) as c:
        sid, fid = _upload_one(c, small)
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
        job_id = r.json()["job_id"]
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline:
            if c.get(f"/api/jobs/{job_id}").json()["status"] in {"completed", "failed"}:
                break
            time.sleep(0.1)

        r = c.get(f"/api/jobs/{job_id}/result")
        assert r.status_code == 200, r.text
        body = r.json()
        # envelope keys
        assert set(body.keys()) >= {"sheets", "unplaced", "warnings", "utilization"}
        # unplaced は数値 (件数)
        assert isinstance(body["unplaced"], int)
        # utilization は数値 (0..1)
        assert isinstance(body["utilization"], (int, float))


# ---------------------------------------------------------------------------
# C4 — Sheet / Placement フィールド名
# ---------------------------------------------------------------------------


def test_sheet_placement_field_names(
    sample_dxf_paths: dict[str, Path], isolated_store, isolated_queue
) -> None:
    """C4: BE が ``sheet_index / width_mm / height_mm / efficiency`` を返す."""

    import time

    small = sample_dxf_paths["small"]
    with TestClient(app) as c:
        sid, fid = _upload_one(c, small)
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
        job_id = r.json()["job_id"]
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline:
            if c.get(f"/api/jobs/{job_id}").json()["status"] in {"completed", "failed"}:
                break
            time.sleep(0.1)
        body = c.get(f"/api/jobs/{job_id}/result").json()
        assert body["sheets"], "expected at least 1 sheet"
        s = body["sheets"][0]
        assert "sheet_index" in s
        assert "width_mm" in s
        assert "height_mm" in s
        assert "efficiency" in s
        assert "placements" in s
        # placements が空でないことを想定するのは不可能 (oversize の可能性)
        if s["placements"]:
            p = s["placements"][0]
            assert "file_id" in p
            assert "sheet_index" in p
            assert "x_mm" in p
            assert "y_mm" in p
            assert "width_mm" in p
            assert "height_mm" in p
            assert "rotation_deg" in p


# ---------------------------------------------------------------------------
# H1 — Template alias keys + 必須キー
# ---------------------------------------------------------------------------


def test_templates_response_includes_alias_keys() -> None:
    """H1: GET /templates が ``template_id`` と ``spacing_mm`` を含む."""

    with TestClient(app) as c:
        r = c.get("/api/templates")
    assert r.status_code == 200
    data = r.json()
    assert "templates" in data
    assert data["templates"], "expected at least one template"
    t = data["templates"][0]
    # alias keys
    assert "template_id" in t
    assert "spacing_mm" in t
    # 必須キー
    assert "name" in t
    assert "material" in t
    assert "thickness_mm" in t


# ---------------------------------------------------------------------------
# C5 — ApplyTemplateResponse 必須キー
# ---------------------------------------------------------------------------


def test_apply_template_response_includes_template_obj(
    sample_dxf_paths: dict[str, Path], isolated_store
) -> None:
    """C5: apply-template レスポンスに full Template が含まれる."""

    small = sample_dxf_paths["small"]
    with TestClient(app) as c:
        with small.open("rb") as fh:
            r = c.post(
                "/api/upload",
                files=[("files", (small.name, fh.read(), "application/dxf"))],
            )
        sid = r.json()["session_id"]
        r = c.post(f"/api/sessions/{sid}/apply-template/ss400-t9-3mm")
    assert r.status_code == 200, r.text
    body = r.json()
    # 必須キー
    assert body["template_id"] == "ss400-t9-3mm"
    assert "session_id" in body
    assert "applied_to" in body
    assert "skipped" in body
    assert "default_offset_mm" in body
    assert body["template"] is not None
    assert body["template"]["template_id"] == "ss400-t9-3mm"
    assert "material" in body["template"]
    assert "spacing_mm" in body["template"]


# ---------------------------------------------------------------------------
# H2 — /sessions/saved ラッパキー
# ---------------------------------------------------------------------------


def test_saved_sessions_uses_saved_wrapper(isolated_store) -> None:
    """H2: 空リストでも ``{saved: []}`` を返す (``sessions`` キーではない)."""

    with TestClient(app) as c:
        r = c.get("/api/sessions/saved")
    assert r.status_code == 200
    body = r.json()
    assert "saved" in body
    assert isinstance(body["saved"], list)
    assert "sessions" not in body


# ---------------------------------------------------------------------------
# H3 — /sessions/load レスポンス shape (Session-like)
# ---------------------------------------------------------------------------


def test_sessions_load_returns_session_shape(
    sample_dxf_paths: dict[str, Path], isolated_store, tmp_path, monkeypatch
) -> None:
    """H3: ``/sessions/load/{name}`` が ``{session_id, files, expires_at}`` を返す."""

    monkeypatch.setenv("CUTFLOW_SAVED_ROOT", str(tmp_path / "saved"))

    small = sample_dxf_paths["small"]
    with TestClient(app) as c:
        with small.open("rb") as fh:
            r = c.post(
                "/api/upload",
                files=[("files", (small.name, fh.read(), "application/dxf"))],
            )
        sid = r.json()["session_id"]
        # save
        r = c.post("/api/sessions/save", json={"name": "tcs-h3", "session_id": sid})
        assert r.status_code == 201, r.text
        # load
        r = c.post("/api/sessions/load/tcs-h3")
        assert r.status_code == 200, r.text
        body = r.json()
        assert "session_id" in body
        assert "files" in body
        assert isinstance(body["files"], list)
        # FE Session 型は ``expires_at: string`` を想定 — None は不可
        assert body.get("expires_at") is not None
