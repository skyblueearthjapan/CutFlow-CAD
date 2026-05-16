"""End-to-end: upload → detect-outer → offset → export with_offset.

Asserts the offset LWPOLYLINE is round-tripped into the exported DXF on
the dedicated ``CUTFLOW_OFFSET`` layer.
"""

from __future__ import annotations

from pathlib import Path

from ezdxf import recover
from fastapi.testclient import TestClient

from main import app


def _upload(c: TestClient, path: Path) -> tuple[str, str]:
    with path.open("rb") as fh:
        r = c.post(
            "/api/upload",
            files=[("files", (path.name, fh.read(), "application/dxf"))],
        )
    assert r.status_code == 201, r.text
    data = r.json()
    return data["session_id"], data["files"][0]["file_id"]


def test_detect_outer_persists_loop(
    sample_dxf_paths: dict[str, Path], isolated_store
) -> None:
    """detect-outer must persist outer.json AND surface it on subsequent GETs."""

    path = sample_dxf_paths["small"]
    with TestClient(app) as c:
        sid, fid = _upload(c, path)

        # 1) Detect.
        r = c.post(f"/api/session/{sid}/file/{fid}/detect-outer")
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["status"] in {"success", "low_confidence"}
        assert data["outer_loop"], "expected a non-empty outer loop on the sample"

        # 2) GET entities → confirmed loop entities now carry category "outer".
        r2 = c.get(f"/api/session/{sid}/file/{fid}")
        assert r2.status_code == 200
        loop_ids = set(data["outer_loop"])
        outer_cats = {e["id"] for e in r2.json()["entities"] if e["category"] == "outer"}
        assert loop_ids.issubset(outer_cats), (
            f"persisted outer-loop not reflected as 'outer' category: "
            f"missing {loop_ids - outer_cats}"
        )


def test_outer_manual_open_chain_returns_422(
    sample_dxf_paths: dict[str, Path], isolated_store
) -> None:
    path = sample_dxf_paths["small"]
    with TestClient(app) as c:
        sid, fid = _upload(c, path)
        r = c.get(f"/api/session/{sid}/file/{fid}")
        # Pick two arbitrary LINEs that are very unlikely to be adjacent.
        line_ids = [e["id"] for e in r.json()["entities"] if e["type"] == "LINE"][:3]
        if len(line_ids) < 3:
            return
        rr = c.post(
            f"/api/session/{sid}/file/{fid}/outer-manual",
            json={"entity_ids": line_ids},
        )
        assert rr.status_code == 422, rr.text


def test_offset_requires_confirmed_outer(
    sample_dxf_paths: dict[str, Path], isolated_store
) -> None:
    path = sample_dxf_paths["small"]
    with TestClient(app) as c:
        sid, fid = _upload(c, path)
        r = c.post(
            f"/api/session/{sid}/file/{fid}/offset",
            json={"default_mm": 3.0},
        )
        # No outer yet → 422.
        assert r.status_code == 422


def test_full_offset_pipeline_and_export(
    sample_dxf_paths: dict[str, Path], isolated_store
) -> None:
    """Detect → offset → export with_offset=true → verify cyan LWPOLYLINE present."""

    path = sample_dxf_paths["small"]
    with TestClient(app) as c:
        sid, fid = _upload(c, path)

        # 1) Detect outer.
        r = c.post(f"/api/session/{sid}/file/{fid}/detect-outer")
        assert r.status_code == 200, r.text
        det = r.json()
        original_perim = det["loop_summary"]["perimeter"]

        # 2) Compute the offset (3mm round join).
        r = c.post(
            f"/api/session/{sid}/file/{fid}/offset",
            json={"default_mm": 3.0, "corner_join": "arc"},
        )
        assert r.status_code == 200, r.text
        off = r.json()
        # The offset perimeter must be larger than the original.
        assert off["perimeter"] > original_perim
        assert "plate_size" in off and "mm" in off["plate_size"]
        assert 0.0 <= off["material_efficiency"] <= 1.0
        verts = off["offset_loop"]["vertices"]
        assert len(verts) >= 3

        # 3) Default export (without offset) → fresh DXF must not have the
        #    CUTFLOW_OFFSET layer populated.
        r = c.get(f"/api/session/{sid}/file/{fid}/export")
        assert r.status_code == 200
        assert "_clean.dxf" in r.headers.get("content-disposition", "")
        plain_bytes = r.content

        # 4) Export with offset.
        r = c.get(f"/api/session/{sid}/file/{fid}/export", params={"with_offset": "true"})
        assert r.status_code == 200, r.text
        assert "_clean_offset.dxf" in r.headers.get("content-disposition", "")
        merged_bytes = r.content

    # 5) Validate the merged DXF parses and carries the offset polyline.
    out_tmp = Path("test_export_with_offset.dxf")
    plain_tmp = Path("test_export_plain.dxf")
    try:
        out_tmp.write_bytes(merged_bytes)
        plain_tmp.write_bytes(plain_bytes)

        merged_doc, _ = recover.readfile(str(out_tmp))
        plain_doc, _ = recover.readfile(str(plain_tmp))

        merged_polys = [
            e for e in merged_doc.modelspace()
            if e.dxftype() == "LWPOLYLINE" and str(e.dxf.layer) == "CUTFLOW_OFFSET"
        ]
        plain_polys = [
            e for e in plain_doc.modelspace()
            if e.dxftype() == "LWPOLYLINE" and str(e.dxf.layer) == "CUTFLOW_OFFSET"
        ]

        assert len(merged_polys) == 1, "with_offset export must add exactly one offset polyline"
        assert len(plain_polys) == 0, "plain export must not include the offset polyline"
    finally:
        out_tmp.unlink(missing_ok=True)
        plain_tmp.unlink(missing_ok=True)
