"""セッション archive (tar.gz) の単体テスト + E2E."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from main import app
from services.session_archive import (
    ArchiveError,
    delete_saved,
    list_saved,
    load_session,
    sanitize_name,
    save_session,
)


def test_sanitize_name_strips_unsafe() -> None:
    assert sanitize_name("hello/world") == "hello_world"
    assert sanitize_name("コベルコ-2026") == "コベルコ-2026"


def test_sanitize_name_rejects_empty() -> None:
    with pytest.raises(ArchiveError):
        sanitize_name("   ")


def test_save_and_load_roundtrip(tmp_path: Path, monkeypatch) -> None:
    sessions_root = tmp_path / "sessions"
    saved_root = tmp_path / "saved"
    monkeypatch.setenv("CUTFLOW_SAVED_ROOT", str(saved_root))

    # 偽セッションディレクトリを準備
    sid = "fakesid"
    sdir = sessions_root / sid
    (sdir / "originals").mkdir(parents=True)
    (sdir / "originals" / "x.dxf").write_text("dummy")
    (sdir / "meta.json").write_text(
        '{"session_id":"fakesid","created_at":"2026-01-01T00:00:00+00:00",'
        '"expires_at":"2026-01-02T00:00:00+00:00","files":[{"file_id":"f1",'
        '"name":"x.dxf","size":5,"path":"' + str(sdir / "originals" / "x.dxf").replace("\\", "\\\\") + '","status":"ready"}]}'
    )

    info = save_session(sdir, "テスト保存")
    assert info.size_bytes > 0
    assert info.file_count == 1

    items = list_saved()
    assert any(it.name == "テスト保存" for it in items)

    new_sid, fc = load_session("テスト保存", sessions_root=sessions_root, ttl_hours=24)
    assert new_sid != sid
    assert fc == 1
    # 復元されたファイルが存在
    assert (sessions_root / new_sid / "originals" / "x.dxf").exists()
    # meta.json が新セッションIDに書き換わっている
    import json

    new_meta = json.loads((sessions_root / new_sid / "meta.json").read_text(encoding="utf-8"))
    assert new_meta["session_id"] == new_sid

    assert delete_saved("テスト保存") is True


def test_load_unknown_archive_raises(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CUTFLOW_SAVED_ROOT", str(tmp_path / "saved"))
    with pytest.raises(ArchiveError):
        load_session("does_not_exist", sessions_root=tmp_path / "sessions", ttl_hours=24)


def test_save_session_dir_not_found(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CUTFLOW_SAVED_ROOT", str(tmp_path / "saved"))
    with pytest.raises(ArchiveError):
        save_session(tmp_path / "nope", "name")


# --------------------------------------------------------------------------
# E2E — POST /api/sessions/save → GET /sessions/saved → POST /load/{name}
# --------------------------------------------------------------------------


def test_session_save_load_e2e(
    sample_dxf_paths: dict[str, Path], isolated_store, tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("CUTFLOW_SAVED_ROOT", str(tmp_path / "saved-e2e"))

    with TestClient(app) as c:
        # upload to get a session
        small = sample_dxf_paths["small"]
        with small.open("rb") as fh:
            r = c.post(
                "/api/upload",
                files=[("files", (small.name, fh.read(), "application/dxf"))],
            )
        assert r.status_code == 201
        sid = r.json()["session_id"]

        # save
        r = c.post("/api/sessions/save", json={"name": "myproject", "session_id": sid})
        assert r.status_code == 201, r.text
        assert r.json()["name"] == "myproject"

        # list
        r = c.get("/api/sessions/saved")
        assert r.status_code == 200
        names = [it["name"] for it in r.json()["saved"]]
        assert "myproject" in names

        # load
        r = c.post("/api/sessions/load/myproject")
        assert r.status_code == 200, r.text
        new_sid = r.json()["session_id"]
        assert new_sid != sid

        # 復元したセッションが読める
        r = c.get(f"/api/session/{new_sid}")
        assert r.status_code == 200

        # delete
        r = c.delete("/api/sessions/saved/myproject")
        assert r.status_code == 204


def test_load_nonexistent_returns_404(isolated_store, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CUTFLOW_SAVED_ROOT", str(tmp_path / "saved-404"))
    with TestClient(app) as c:
        r = c.post("/api/sessions/load/nonexistent_xyz")
        assert r.status_code == 404
