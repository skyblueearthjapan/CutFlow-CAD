"""PDF export honours the ``material`` query param (H4)."""

from __future__ import annotations

from pathlib import Path

import pypdf
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


def _pdf_text(path: Path) -> str:
    """Return the concatenated extracted text from every page."""

    reader = pypdf.PdfReader(str(path))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def test_export_pdf_unit_embeds_material_text(
    sample_dxf_paths: dict[str, Path], tmp_path: Path
) -> None:
    """Calling :func:`export_pdf` with ``material='SS400 t9'`` puts that
    string into the rendered PDF header band.

    We extract text with pypdf because ReportLab compresses the content
    stream by default — searching the raw bytes for the ASCII label
    would only pass when compression was off.
    """

    src = sample_dxf_paths["small"]
    dest = tmp_path / "out-with-mat.pdf"
    export_pdf(
        src,
        dest,
        frame="cutflow",
        material="SS400 t9",
        title="testing-material",
    )
    assert _is_pdf(dest.read_bytes())
    text = _pdf_text(dest)
    assert "SS400 t9" in text


def test_export_pdf_unit_omits_material_label_when_absent(
    sample_dxf_paths: dict[str, Path], tmp_path: Path
) -> None:
    """No material label appears when the caller omits it — the header
    band just shows the filename + date."""

    src = sample_dxf_paths["small"]
    dest = tmp_path / "out-no-mat.pdf"
    export_pdf(src, dest, frame="cutflow", title="no-material")
    text = _pdf_text(dest)
    # The "材質:" prefix only ever appears when material is provided.
    assert "材質" not in text


def test_export_endpoint_forwards_material_query_param(
    sample_dxf_paths: dict[str, Path], isolated_store, tmp_path: Path
) -> None:
    """End-to-end: GET export?format=pdf&material=... lands in the PDF."""

    path = sample_dxf_paths["small"]
    with TestClient(app) as c:
        sid, fid = _upload(c, path)
        r = c.get(
            f"/api/session/{sid}/file/{fid}/export",
            params={
                "format": "pdf",
                "with_frame": "cutflow",
                "material": "AL5052 t6",
            },
        )
        assert r.status_code == 200, r.text
        assert _is_pdf(r.content)

        out = tmp_path / "endpoint.pdf"
        out.write_bytes(r.content)
        assert "AL5052 t6" in _pdf_text(out)


def test_export_endpoint_ignores_empty_material(
    sample_dxf_paths: dict[str, Path], isolated_store, tmp_path: Path
) -> None:
    """Empty / whitespace material does not pollute the header band — the
    router normalises it to None before calling export_pdf."""

    path = sample_dxf_paths["small"]
    with TestClient(app) as c:
        sid, fid = _upload(c, path)
        r = c.get(
            f"/api/session/{sid}/file/{fid}/export",
            params={
                "format": "pdf",
                "with_frame": "cutflow",
                "material": "   ",
            },
        )
        assert r.status_code == 200, r.text
        assert _is_pdf(r.content)
        out = tmp_path / "blank.pdf"
        out.write_bytes(r.content)
        assert "材質" not in _pdf_text(out)
