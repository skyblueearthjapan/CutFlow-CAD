"""テンプレート endpoint テスト."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from main import app
from services.templates import find_template, list_templates, reload_templates


def test_list_templates_loaded_from_default() -> None:
    items = list_templates()
    assert len(items) >= 5
    # 必須キー
    for t in items:
        assert "id" in t
        assert "name" in t
        assert "material" in t
        assert "thickness_mm" in t
        assert "default_offset_mm" in t


def test_find_template_known_id() -> None:
    t = find_template("ss400-t9-3mm")
    assert t is not None
    assert t["material"] == "SS400"


def test_find_template_unknown() -> None:
    assert find_template("does-not-exist") is None


def test_get_templates_endpoint() -> None:
    with TestClient(app) as c:
        r = c.get("/api/templates")
    assert r.status_code == 200
    data = r.json()
    assert "templates" in data
    assert len(data["templates"]) >= 5


def test_apply_template_to_session(sample_dxf_paths: dict[str, Path], isolated_store) -> None:
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
        assert body["template_id"] == "ss400-t9-3mm"
        assert body["default_offset_mm"] == 3.0
        assert len(body["applied_to"]) == 1


def test_apply_template_unknown_returns_404(sample_dxf_paths: dict[str, Path], isolated_store) -> None:
    small = sample_dxf_paths["small"]
    with TestClient(app) as c:
        with small.open("rb") as fh:
            r = c.post(
                "/api/upload",
                files=[("files", (small.name, fh.read(), "application/dxf"))],
            )
        sid = r.json()["session_id"]
        r = c.post(f"/api/sessions/{sid}/apply-template/does-not-exist")
        assert r.status_code == 404


def test_reload_templates_returns_count() -> None:
    n = reload_templates()
    assert n >= 5
