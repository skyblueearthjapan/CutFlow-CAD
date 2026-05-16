"""簡易メモリメトリクス — Phase 5.

Prometheus / OpenTelemetry 等は社内 1 プロセス運用にはオーバーキル。
プロセス内で集計し ``GET /api/metrics`` で取れれば十分。

スレッドセーフ:
    asyncio から呼ばれる前提だが内部でも Lock を使い、将来 worker
    スレッドから触られても壊れないようにしておく。
"""

from __future__ import annotations

import time
from collections import deque
from threading import Lock

# ---------------------------------------------------------------------------
# Internal state (module singleton)
# ---------------------------------------------------------------------------

_LOCK = Lock()
_STARTED_AT = time.monotonic()
_REQUESTS: int = 0
_ERRORS: int = 0
# 直近 N 件のレスポンス時間 (ms) — 平均算出用
_DURATIONS_MS: deque[float] = deque(maxlen=256)
_COUNTERS: dict[str, int] = {}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def record_request(duration_ms: float, status_code: int) -> None:
    """1 リクエスト分の計測値を取り込む."""

    global _REQUESTS, _ERRORS
    with _LOCK:
        _REQUESTS += 1
        if int(status_code) >= 500:
            _ERRORS += 1
        try:
            _DURATIONS_MS.append(float(duration_ms))
        except (TypeError, ValueError):
            pass


def incr(name: str, by: int = 1) -> None:
    with _LOCK:
        _COUNTERS[name] = _COUNTERS.get(name, 0) + int(by)


def snapshot() -> dict:
    with _LOCK:
        avg_ms = (sum(_DURATIONS_MS) / len(_DURATIONS_MS)) if _DURATIONS_MS else 0.0
        return {
            "uptime_sec": time.monotonic() - _STARTED_AT,
            "request_count": _REQUESTS,
            "error_count": _ERRORS,
            "avg_response_ms": float(avg_ms),
            "counters": dict(_COUNTERS),
        }


def reset_for_tests() -> None:
    """テスト用 — グローバル状態をクリア."""

    global _REQUESTS, _ERRORS, _STARTED_AT
    with _LOCK:
        _REQUESTS = 0
        _ERRORS = 0
        _DURATIONS_MS.clear()
        _COUNTERS.clear()
        _STARTED_AT = time.monotonic()
