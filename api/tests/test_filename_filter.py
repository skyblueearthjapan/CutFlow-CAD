"""Assembly-drawing filename filter (H3).

DESIGN.md §4 states CutFlow•CAD only handles single-part DXFs. Folder uploads
will typically include the parent assembly drawing (``...組立図.DXF`` or
``...-0T_...DXF``); the server must silently drop them so the user doesn't
need to pre-curate.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from main import app
from routers.session import is_assembly_drawing


@pytest.mark.parametrize(
    "name,expected",
    [
        ("25057-P1-0T_昇降軸駆動部組立図.DXF", True),
        ("foo_assembly.dxf", True),
        ("FOO-0T_bar.DXF", True),
        ("25057-P1-03_センタープレート.DXF", False),
        ("foo_bar.dxf", False),
    ],
)
def test_is_assembly_drawing(name: str, expected: bool) -> None:
    assert is_assembly_drawing(name) is expected


def test_upload_skips_assembly_drawings(
    sample_dxf_paths: dict[str, Path], isolated_store
) -> None:
    """Mix a part DXF with an assembly DXF, then confirm only the part wins."""

    part = sample_dxf_paths["small"]
    asm_bytes = part.read_bytes()  # bytes don't have to be a real assembly

    with TestClient(app) as c:
        r = c.post(
            "/api/upload",
            files=[
                ("files", (part.name, part.read_bytes(), "application/dxf")),
                ("files", ("foo_組立図.dxf", asm_bytes, "application/dxf")),
            ],
        )
    assert r.status_code == 201, r.text
    files = r.json()["files"]
    assert len(files) == 1
    assert files[0]["name"] == part.name


def test_upload_rejects_when_only_assembly(isolated_store) -> None:
    """When every input is an assembly the whole upload should 400."""

    with TestClient(app) as c:
        r = c.post(
            "/api/upload",
            files=[
                ("files", ("foo_組立図.dxf", b"x", "application/dxf")),
                ("files", ("bar_assembly.DXF", b"x", "application/dxf")),
            ],
        )
    assert r.status_code == 400
    assert "assembly" in r.json()["detail"].lower()
