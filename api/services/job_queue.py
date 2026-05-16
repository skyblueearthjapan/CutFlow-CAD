"""非同期ジョブキュー — Phase 5.

軽量実装方針
------------
* ``asyncio.Queue`` で job_id を渡し、起動時に N (=2) 個のワーカーを
  ``asyncio.create_task`` で走らせる。
* ジョブ本体は ``async def job_func(progress_cb) -> dict`` で受ける。
  CPU 重い処理 (ネスティング BLF) は ``loop.run_in_executor`` でスレッ
  ドプール送りにする。
* 結果と進捗は **メモリ内** dict に保持し、最終結果のみ JSON で
  ``CUTFLOW_JOB_ROOT`` (デフォルト ``/var/cutflow/jobs`` か tmp) に永続化。
  プロセス再起動後でも ``GET /api/jobs/{id}`` が結果を返せる。
* キャンセル等の高度な制御は Phase 5 では非対応 (シングル PID 想定)。

このモジュールはランタイム singleton (``get_queue()``) を提供し、
``main.py`` の lifespan で start / stop される。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# 並行ワーカー数。CPU バウンドな BLF が走るので 2 並列に制限。
DEFAULT_WORKER_COUNT = 2
DEFAULT_QUEUE_MAXSIZE = 256


# ---------------------------------------------------------------------------
# Job state
# ---------------------------------------------------------------------------


@dataclass
class JobRecord:
    """単一ジョブの状態 (メモリ + JSON 永続化用)。"""

    job_id: str
    kind: str
    status: str  # pending / running / completed / failed
    progress: float = 0.0
    created_at: str = ""
    started_at: str | None = None
    completed_at: str | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    # 関連メタ (例: nest なら session_id / file_ids)
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Job func signature
# ---------------------------------------------------------------------------


# progress_cb(value 0..1) はワーカーが任意で呼ぶ
ProgressCallback = Callable[[float], None]
# シグネチャ: async def job(progress_cb: ProgressCallback) -> dict[str, Any]
JobFunc = Callable[[ProgressCallback], Awaitable[dict[str, Any]]]


# ---------------------------------------------------------------------------
# Queue
# ---------------------------------------------------------------------------


def _default_root() -> Path:
    raw = os.environ.get("CUTFLOW_JOB_ROOT")
    if raw:
        return Path(raw)
    # Linux でデフォルト /var/cutflow/jobs を使うが、Windows / CI では tmp.
    candidate = Path("/var/cutflow/jobs")
    try:
        candidate.mkdir(parents=True, exist_ok=True)
        # 書き込み権限が無ければフォールバック
        test = candidate / ".write_test"
        test.write_text("ok", encoding="utf-8")
        test.unlink(missing_ok=True)
        return candidate
    except OSError:
        return Path(tempfile.gettempdir()) / "cutflow-jobs"


class JobQueue:
    """軽量な asyncio ベースのジョブキュー."""

    def __init__(
        self,
        worker_count: int = DEFAULT_WORKER_COUNT,
        root: Path | None = None,
        maxsize: int = DEFAULT_QUEUE_MAXSIZE,
    ) -> None:
        self.worker_count = max(1, int(worker_count))
        self.root = Path(root) if root else _default_root()
        self.root.mkdir(parents=True, exist_ok=True)
        self._maxsize = maxsize
        # ``asyncio.Queue`` は最初に await されたときの event loop に紐づくため
        # singleton 再利用すると古い loop に拘束されてしまう。``start()`` で
        # 都度再生成することで TestClient lifespan の出入りに耐える。
        self._queue: asyncio.Queue[tuple[str, JobFunc]] | None = None
        # 起動前に submit された pending を一時保管する FIFO バッファ
        self._pending_buffer: list[tuple[str, JobFunc]] = []
        self._records: dict[str, JobRecord] = {}
        self._workers: list[asyncio.Task] = []
        self._running = False

    # ---- lifecycle ------------------------------------------------------

    async def start(self) -> None:
        if self._running:
            return
        # 新しい event loop に紐づく Queue を作り直す
        self._queue = asyncio.Queue(maxsize=self._maxsize)
        # 起動前 submit で溜まっていた pending を一気に投入
        for item in self._pending_buffer:
            self._queue.put_nowait(item)
        self._pending_buffer.clear()
        # M3: ディスク上に残った pending/running は再起動後に再実行できないので
        # ログで通知だけ行う (closure が失われているため自動復活は不可能)。
        try:
            if self.root.exists():
                stranded = []
                for p in self.root.glob("*.json"):
                    if p.name.startswith("."):
                        continue
                    try:
                        data = json.loads(p.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError):
                        continue
                    st = data.get("status")
                    if st in ("pending", "running"):
                        stranded.append((data.get("job_id"), st))
                if stranded:
                    log.warning(
                        "job queue start: %d stranded jobs cannot be revived (closures lost): %s",
                        len(stranded),
                        [s[0] for s in stranded[:10]],
                    )
        except OSError as exc:
            log.debug("job queue stranded scan failed: %s", exc)
        self._running = True
        for i in range(self.worker_count):
            self._workers.append(
                asyncio.create_task(self._worker_loop(i), name=f"job-worker-{i}")
            )
        log.info("job queue started (workers=%d, root=%s)", self.worker_count, self.root)

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        for t in self._workers:
            t.cancel()
        for t in self._workers:
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._workers.clear()
        # Queue 参照を捨てる — 次回 start で新規作成。未消化アイテムは
        # 失われるが、テスト/再起動シナリオではそれで十分。本番でも
        # シングルプロセス前提なので restart 時に消えるのは想定内。
        self._queue = None
        log.info("job queue stopped")

    # ---- public API -----------------------------------------------------

    def submit(self, kind: str, func: JobFunc, meta: dict[str, Any] | None = None) -> str:
        """ジョブをキューに積む — ``job_id`` を返す."""

        job_id = uuid.uuid4().hex
        rec = JobRecord(
            job_id=job_id,
            kind=str(kind),
            status="pending",
            created_at=_now_iso(),
            meta=dict(meta or {}),
        )
        self._records[job_id] = rec
        if self._queue is None:
            # キュー未起動 (テスト等) — pending バッファに積む
            self._pending_buffer.append((job_id, func))
        else:
            try:
                self._queue.put_nowait((job_id, func))
            except asyncio.QueueFull as exc:
                rec.status = "failed"
                rec.error = "job queue is full"
                rec.completed_at = _now_iso()
                self._persist(rec)
                raise RuntimeError("job queue full") from exc
        # 永続化 (pending 状態でもクラッシュ時に痕跡を残す)
        self._persist(rec)
        return job_id

    def get(self, job_id: str) -> JobRecord | None:
        rec = self._records.get(job_id)
        if rec is not None:
            return rec
        # メモリに無ければ JSON から復元
        path = self.root / f"{job_id}.json"
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return JobRecord(**data)
        except (OSError, json.JSONDecodeError, TypeError) as exc:
            log.warning("failed to restore job %s: %s", job_id, exc)
            return None

    def list_recent(self, limit: int = 100) -> list[JobRecord]:
        items = list(self._records.values())
        items.sort(key=lambda r: r.created_at, reverse=True)
        return items[:limit]

    def stats(self) -> dict[str, int]:
        running = sum(1 for r in self._records.values() if r.status == "running")
        completed = sum(1 for r in self._records.values() if r.status == "completed")
        failed = sum(1 for r in self._records.values() if r.status == "failed")
        return {
            "total": len(self._records),
            "running": running,
            "completed": completed,
            "failed": failed,
        }

    # ---- cleanup --------------------------------------------------------

    def purge_old(self, ttl_hours: float = 24 * 7) -> int:
        """C7: ``ttl_hours`` より古い job ファイル + メモリ内 record を削除.

        個人情報残留対策。``created_at`` が現在時刻より ``ttl_hours`` 前
        のジョブを root ディレクトリの JSON とメモリ両方からパージする。
        """

        cutoff = datetime.now(timezone.utc) - timedelta(hours=float(ttl_hours))
        removed = 0
        # メモリ内 record をスキャン
        for jid in list(self._records.keys()):
            rec = self._records.get(jid)
            if rec is None:
                continue
            try:
                ts = datetime.fromisoformat(rec.created_at) if rec.created_at else None
            except (TypeError, ValueError):
                ts = None
            if ts is not None and ts < cutoff:
                self._records.pop(jid, None)
                path = self.root / f"{jid}.json"
                try:
                    path.unlink(missing_ok=True)
                    removed += 1
                except OSError:
                    pass
        # ディスク上のオーファン JSON もスキャン (再起動後の残骸)
        if self.root.exists():
            for p in self.root.glob("*.json"):
                if p.name.startswith("."):
                    continue
                try:
                    st = p.stat()
                    mtime = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
                except OSError:
                    continue
                if mtime < cutoff and p.stem not in self._records:
                    try:
                        p.unlink(missing_ok=True)
                        removed += 1
                    except OSError:
                        pass
        if removed:
            log.info("purged %d old job records (ttl=%.1fh)", removed, ttl_hours)
        return removed

    def purge_for_session(self, session_id: str) -> int:
        """C7: 指定セッションに紐づく job_id を全て削除.

        セッション削除フックから呼ばれる。job 結果には部品 file_id や
        sheet 配置情報など個人情報・営業秘密が含まれるため、関連付け
        られていたジョブも消す。
        """

        removed = 0
        for jid in list(self._records.keys()):
            rec = self._records.get(jid)
            if rec is None:
                continue
            meta_sid = (rec.meta or {}).get("session_id")
            if meta_sid == session_id:
                self._records.pop(jid, None)
                path = self.root / f"{jid}.json"
                try:
                    path.unlink(missing_ok=True)
                    removed += 1
                except OSError:
                    pass
        if removed:
            log.info("purged %d jobs for session %s", removed, session_id)
        return removed

    # ---- worker internals -----------------------------------------------

    async def _worker_loop(self, idx: int) -> None:
        log.debug("worker %d ready", idx)
        while self._running:
            if self._queue is None:
                # 防御的: queue が無ければ少し待って再チェック
                await asyncio.sleep(0.05)
                continue
            try:
                job_id, func = await self._queue.get()
            except asyncio.CancelledError:
                break
            await self._run_job(job_id, func)
            try:
                self._queue.task_done()
            except (AttributeError, ValueError):
                pass

    async def _run_job(self, job_id: str, func: JobFunc) -> None:
        rec = self._records.get(job_id)
        if rec is None:
            log.warning("job %s missing from records, skipping", job_id)
            return
        rec.status = "running"
        rec.started_at = _now_iso()
        self._persist(rec)

        def _set_progress(p: float) -> None:
            try:
                rec.progress = max(0.0, min(1.0, float(p)))
            except (TypeError, ValueError):
                pass

        try:
            result = await func(_set_progress)
            rec.result = dict(result) if isinstance(result, dict) else {"value": result}
            rec.progress = 1.0
            rec.status = "completed"
        except asyncio.CancelledError:
            rec.status = "failed"
            rec.error = "cancelled"
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("job %s failed", job_id)
            rec.status = "failed"
            rec.error = f"{type(exc).__name__}: {exc}"
        finally:
            rec.completed_at = _now_iso()
            self._persist(rec)
            # 通知: 完了/失敗時に Discord 通知 (no-op if disabled)
            try:
                from services.discord_notify import notify_job_finished

                notify_job_finished(rec.kind, rec.job_id, rec.status, rec.error)
            except Exception as exc:  # noqa: BLE001
                log.debug("discord notify skipped: %s", exc)

    def _persist(self, rec: JobRecord) -> None:
        path = self.root / f"{rec.job_id}.json"
        try:
            payload = rec.to_dict()
            # atomic write
            fd, tmp = tempfile.mkstemp(prefix=".job.", suffix=".json", dir=str(self.root))
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(payload, fh, ensure_ascii=False, indent=2)
                os.replace(tmp, path)
            except Exception:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except OSError as exc:
            log.warning("failed to persist job %s: %s", rec.job_id, exc)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Singleton + test helpers
# ---------------------------------------------------------------------------


_QUEUE: JobQueue | None = None
_QUEUE_LOCK = asyncio.Lock() if False else None  # placeholder for clarity


def get_queue() -> JobQueue:
    """Process-wide singleton; constructed lazily without starting workers.

    ``main.py`` lifespan must call ``await get_queue().start()`` once the
    event loop is up.
    """

    global _QUEUE
    if _QUEUE is None:
        _QUEUE = JobQueue()
    return _QUEUE


def reset_queue_for_tests(root: Path | None = None, worker_count: int = 2) -> JobQueue:
    """Replace the singleton with a fresh queue pointing at ``root``.

    C8: 旧 singleton の ``_pending_buffer`` を明示的にクリアしてから捨てる。
    submit→reset→start の順序で動かれた場合の memory リーク (= closure が
    旧 JobQueue を参照したまま GC されない) を防ぐ。
    """

    global _QUEUE
    old = _QUEUE
    if old is not None:
        try:
            old._pending_buffer.clear()
            old._records.clear()
        except Exception:  # noqa: BLE001
            pass
    _QUEUE = JobQueue(worker_count=worker_count, root=root)
    return _QUEUE


# ---------------------------------------------------------------------------
# Helper for tests: run a job synchronously to completion (bypass queue)
# ---------------------------------------------------------------------------


async def run_inline(func: JobFunc) -> dict[str, Any]:
    """Run ``func`` immediately and return its result.

    Used by tests that want to validate job *content* without booting
    the worker task. Reuses the same progress-callback contract so the
    code under test is identical to production.
    """

    captured: dict[str, float] = {"p": 0.0}

    def _cb(p: float) -> None:
        captured["p"] = float(p)

    t0 = time.monotonic()
    result = await func(_cb)
    log.debug("inline job done in %.3fs (progress=%.2f)", time.monotonic() - t0, captured["p"])
    return result
