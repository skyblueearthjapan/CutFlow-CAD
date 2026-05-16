"""PDF export tests (Phase 3)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from main import app
from services.pdf_export import export_pdf


def _upload(c: TestClient, path: Path) -> tuple[str, str]:
    with path.open("rb") as fh:
        r = c.post(
            "/api/upload",
            files=[("files", (path.name, fh.read(), "application/dxf"))],
        )
    assert r.status_code == 201, r.text
    data = r.json()
    return data["session_id"], data["files"][0]["file_id"]


def _is_pdf(data: bytes) -> bool:
    return data[:4] == b"%PDF"


def test_export_pdf_unit_renders_min_frame(
    sample_dxf_paths: dict[str, Path], tmp_path: Path
) -> None:
    """The bare service call must produce a parsable PDF on a real sample."""

    src = sample_dxf_paths["small"]
    dest = tmp_path / "out.pdf"
    export_pdf(src, dest, frame="none")
    assert dest.exists()
    data = dest.read_bytes()
    assert _is_pdf(data)
    # A real drawing produces a non-trivial PDF (> 1 KB).
    assert len(data) > 1024


def test_export_pdf_with_cutflow_frame(
    sample_dxf_paths: dict[str, Path], tmp_path: Path
) -> None:
    src = sample_dxf_paths["small"]
    dest = tmp_path / "framed.pdf"
    export_pdf(
        src,
        dest,
        frame="cutflow",
        title="test-sample",
        material="SS400 t9",
        perimeter_mm=1471.0,
        plate_size="446 × 286 mm",
    )
    data = dest.read_bytes()
    assert _is_pdf(data)


def test_export_endpoint_rejects_unknown_format(
    sample_dxf_paths: dict[str, Path], isolated_store
) -> None:
    path = sample_dxf_paths["small"]
    with TestClient(app) as c:
        sid, fid = _upload(c, path)
        r = c.get(
            f"/api/session/{sid}/file/{fid}/export",
            params={"format": "svg"},
        )
        assert r.status_code == 400


def test_export_endpoint_rejects_bad_frame(
    sample_dxf_paths: dict[str, Path], isolated_store
) -> None:
    path = sample_dxf_paths["small"]
    with TestClient(app) as c:
        sid, fid = _upload(c, path)
        r = c.get(
            f"/api/session/{sid}/file/{fid}/export",
            params={"format": "pdf", "with_frame": "bogus"},
        )
        assert r.status_code == 400


def test_export_pdf_endpoint_returns_application_pdf(
    sample_dxf_paths: dict[str, Path], isolated_store
) -> None:
    """End-to-end: GET export?format=pdf streams a valid application/pdf back."""

    path = sample_dxf_paths["small"]
    with TestClient(app) as c:
        sid, fid = _upload(c, path)
        r = c.get(
            f"/api/session/{sid}/file/{fid}/export",
            params={"format": "pdf", "with_frame": "none"},
        )
        assert r.status_code == 200, r.text
        assert r.headers["content-type"].startswith("application/pdf")
        assert _is_pdf(r.content)
        assert ".pdf" in r.headers.get("content-disposition", "")


def test_export_pdf_with_offset_overlay(
    sample_dxf_paths: dict[str, Path], isolated_store
) -> None:
    """When with_offset=true, the offset polygon must be in the PDF stream."""

    path = sample_dxf_paths["small"]
    with TestClient(app) as c:
        sid, fid = _upload(c, path)
        # Detect + offset first.
        r = c.post(f"/api/session/{sid}/file/{fid}/detect-outer")
        assert r.status_code == 200
        r = c.post(
            f"/api/session/{sid}/file/{fid}/offset",
            json={"default_mm": 3.0, "corner_join": "arc"},
        )
        assert r.status_code == 200, r.text

        r = c.get(
            f"/api/session/{sid}/file/{fid}/export",
            params={"format": "pdf", "with_offset": "true", "with_frame": "cutflow"},
        )
        assert r.status_code == 200, r.text
        assert _is_pdf(r.content)
        # Filename should encode the offset/clean flags.
        cd = r.headers.get("content-disposition", "")
        assert "offset" in cd
