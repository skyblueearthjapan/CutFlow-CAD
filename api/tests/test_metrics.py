"""メトリクスサービス + /api/metrics endpoint テスト."""

from __future__ import annotations

from fastapi.testclient import TestClient

from main import app
from services import metrics as metrics_mod


def test_record_and_snapshot_basic() -> None:
    metrics_mod.reset_for_tests()
    metrics_mod.record_request(12.5, 200)
    metrics_mod.record_request(99.0, 500)
    metrics_mod.incr("foo")
    metrics_mod.incr("foo", 2)

    snap = metrics_mod.snapshot()
    assert snap["request_count"] == 2
    assert snap["error_count"] == 1
    assert snap["avg_response_ms"] > 0
    assert snap["counters"]["foo"] == 3


def test_metrics_endpoint_returns_snapshot(isolated_store) -> None:
    metrics_mod.reset_for_tests()
    with TestClient(app) as c:
        # 何かしらリクエストを 1 件発生させる
        c.get("/api/health")
        r = c.get("/api/metrics")
    assert r.status_code == 200
    data = r.json()
    assert "uptime_sec" in data
    assert "request_count" in data
    assert data["request_count"] >= 1
    assert "jobs_total" in data


def test_reset_clears_state() -> None:
    metrics_mod.record_request(1.0, 200)
    metrics_mod.reset_for_tests()
    snap = metrics_mod.snapshot()
    assert snap["request_count"] == 0
    assert snap["error_count"] == 0
    assert snap["avg_response_ms"] == 0.0
