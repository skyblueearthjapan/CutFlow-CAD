"""Smoke tests for the health/root endpoints."""

from __future__ import annotations

from fastapi.testclient import TestClient

from main import app


def test_health() -> None:
    with TestClient(app) as c:
        r = c.get("/api/health")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ok"
    assert data["service"] == "cutflow-cad-api"
    assert "version" in data


def test_root() -> None:
    with TestClient(app) as c:
        r = c.get("/")
    assert r.status_code == 200
    assert r.json()["docs"] == "/docs"
