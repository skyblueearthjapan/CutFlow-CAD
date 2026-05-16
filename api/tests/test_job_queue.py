"""Job queue 単体テスト."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from services.job_queue import JobQueue, reset_queue_for_tests, run_inline


@pytest.mark.asyncio
async def test_submit_and_complete(tmp_path: Path) -> None:
    q = JobQueue(worker_count=1, root=tmp_path)
    await q.start()
    try:
        async def _job(progress_cb) -> dict:
            progress_cb(0.5)
            await asyncio.sleep(0.01)
            progress_cb(1.0)
            return {"value": 42}

        job_id = q.submit("test", _job, meta={"foo": "bar"})
        # Poll until done
        for _ in range(200):
            rec = q.get(job_id)
            assert rec is not None
            if rec.status in ("completed", "failed"):
                break
            await asyncio.sleep(0.05)
        rec = q.get(job_id)
        assert rec is not None
        assert rec.status == "completed", rec
        assert rec.result == {"value": 42}
        assert rec.progress == pytest.approx(1.0)
    finally:
        await q.stop()


@pytest.mark.asyncio
async def test_failed_job_records_error(tmp_path: Path) -> None:
    q = JobQueue(worker_count=1, root=tmp_path)
    await q.start()
    try:
        async def _bad(progress_cb) -> dict:
            raise ValueError("boom")

        job_id = q.submit("test", _bad)
        for _ in range(200):
            rec = q.get(job_id)
            if rec and rec.status in ("completed", "failed"):
                break
            await asyncio.sleep(0.05)
        rec = q.get(job_id)
        assert rec is not None
        assert rec.status == "failed"
        assert "boom" in (rec.error or "")
    finally:
        await q.stop()


@pytest.mark.asyncio
async def test_get_restores_from_disk(tmp_path: Path) -> None:
    q1 = JobQueue(worker_count=1, root=tmp_path)
    await q1.start()
    try:
        async def _job(progress_cb) -> dict:
            return {"v": 1}

        jid = q1.submit("test", _job)
        for _ in range(100):
            rec = q1.get(jid)
            if rec and rec.status == "completed":
                break
            await asyncio.sleep(0.05)
    finally:
        await q1.stop()

    # 新しいキュー (再起動シミュレーション)
    q2 = JobQueue(worker_count=1, root=tmp_path)
    # メモリ未ロード時の disk 復元
    rec = q2.get(jid)
    assert rec is not None
    assert rec.status == "completed"


@pytest.mark.asyncio
async def test_run_inline_runs_job_func() -> None:
    async def _job(progress_cb) -> dict:
        progress_cb(0.3)
        return {"x": "y"}

    result = await run_inline(_job)
    assert result == {"x": "y"}


def test_reset_queue_returns_fresh_singleton(tmp_path: Path) -> None:
    q1 = reset_queue_for_tests(root=tmp_path / "a")
    q2 = reset_queue_for_tests(root=tmp_path / "b")
    assert q1 is not q2
    assert q2.root == tmp_path / "b"


def test_stats_summary(tmp_path: Path) -> None:
    q = JobQueue(worker_count=1, root=tmp_path)
    stats = q.stats()
    assert stats == {"total": 0, "running": 0, "completed": 0, "failed": 0}
