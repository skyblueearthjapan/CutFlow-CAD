"""Phase 5 C7: ジョブメタの個人情報残留対策."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from main import app
from services.job_queue import JobQueue, JobRecord, reset_queue_for_tests


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stale(hours: float = 200.0) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def test_purge_old_removes_records_past_ttl(tmp_path: Path) -> None:
    """``purge_old`` で TTL を過ぎた job をメモリとディスクの両方から削除."""

    q = JobQueue(worker_count=1, root=tmp_path)
    # 古い rec を直接書き込む
    old_rec = JobRecord(job_id="old1", kind="nest", status="completed", created_at=_stale(300))
    q._records["old1"] = old_rec
    q._persist(old_rec)
    # 新しい rec
    new_rec = JobRecord(job_id="new1", kind="nest", status="completed", created_at=_now())
    q._records["new1"] = new_rec
    q._persist(new_rec)

    removed = q.purge_old(ttl_hours=168.0)
    assert removed >= 1
    assert "old1" not in q._records
    assert "new1" in q._records
    assert not (tmp_path / "old1.json").exists()
    assert (tmp_path / "new1.json").exists()


def test_purge_for_session_removes_related_jobs(tmp_path: Path) -> None:
    """``purge_for_session`` でセッション ID に紐づく job を全削除."""

    q = JobQueue(worker_count=1, root=tmp_path)
    r1 = JobRecord(
        job_id="j1",
        kind="nest",
        status="completed",
        created_at=_now(),
        meta={"session_id": "S1"},
    )
    r2 = JobRecord(
        job_id="j2",
        kind="nest",
        status="completed",
        created_at=_now(),
        meta={"session_id": "S2"},
    )
    q._records["j1"] = r1
    q._records["j2"] = r2
    q._persist(r1)
    q._persist(r2)

    removed = q.purge_for_session("S1")
    assert removed == 1
    assert "j1" not in q._records
    assert "j2" in q._records


def test_delete_session_hook_purges_jobs(
    sample_dxf_paths, isolated_store, isolated_queue
) -> None:
    """DELETE /api/session/{sid} が関連ジョブも消す."""

    small = sample_dxf_paths["small"]
    with TestClient(app) as c:
        with small.open("rb") as fh:
            r = c.post(
                "/api/upload",
                files=[("files", (small.name, fh.read(), "application/dxf"))],
            )
        sid = r.json()["session_id"]
        fid = r.json()["files"][0]["file_id"]
        # 1 件ジョブを投入
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
        # まずジョブが見える
        assert c.get(f"/api/jobs/{job_id}").status_code == 200
        # session 削除 → ジョブも消える
        r = c.delete(f"/api/session/{sid}")
        assert r.status_code == 204
        # 数秒 worker 完了を待つ前にチェックすると残っている可能性があるが、
        # purge_for_session はキューから records を消すので 404 が返るはず
        r = c.get(f"/api/jobs/{job_id}")
        assert r.status_code == 404


def test_reset_queue_clears_old_pending(tmp_path: Path) -> None:
    """C8: ``reset_queue_for_tests`` が旧 singleton の pending_buffer をクリア."""

    q1 = reset_queue_for_tests(root=tmp_path / "a")
    # ジョブを 1 件 pending buffer に積む (queue 未起動)
    async def _noop(_cb):
        return {"x": 1}
    q1.submit("nest", _noop, meta={"session_id": "S1"})
    assert q1._pending_buffer or "S1" in {(r.meta or {}).get("session_id") for r in q1._records.values()}

    q2 = reset_queue_for_tests(root=tmp_path / "b")
    # 旧キューの buffer はクリアされている
    assert q1._pending_buffer == []
    # 新キューは独立
    assert q2 is not q1
