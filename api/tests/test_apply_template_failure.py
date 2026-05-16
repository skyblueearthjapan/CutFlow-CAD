"""Phase 5 H7: apply_template 全件失敗で 207 を返すこと."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from main import app


def test_apply_template_all_skipped_returns_207(
    sample_dxf_paths: dict[str, Path], isolated_store, monkeypatch
) -> None:
    """全ファイルの ``write_template_for_file`` が失敗したら 207 (Multi-Status)."""

    small = sample_dxf_paths["small"]
    with TestClient(app) as c:
        with small.open("rb") as fh:
            r = c.post(
                "/api/upload",
                files=[("files", (small.name, fh.read(), "application/dxf"))],
            )
        sid = r.json()["session_id"]

        # store.write_template_for_file を全件 raise させる
        from storage import get_store

        store = get_store()

        def _boom(_sid, _fid, _tpl):
            raise OSError("disk full")

        monkeypatch.setattr(store, "write_template_for_file", _boom)

        r = c.post(f"/api/sessions/{sid}/apply-template/ss400-t9-3mm")

    # H7: 全件失敗 → 207
    assert r.status_code == 207, r.text
    body = r.json()
    assert body["applied_to"] == []
    assert body["skipped"], "skipped list must be populated"


def test_apply_template_partial_success_returns_200(
    sample_dxf_paths: dict[str, Path], isolated_store
) -> None:
    """通常 (全件成功) なら 200。"""

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
    assert body["applied_to"]
    assert body["skipped"] == []
