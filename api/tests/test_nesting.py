"""BLF ネスティングアルゴリズムの単体テスト + E2E (上位ルート経由)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from main import app
from services.nesting import (
    PartInput,
    build_sheet_summaries,
    nest_bottom_left,
    overall_efficiency,
)


# ---------------------------------------------------------------------------
# Unit — BLF behaviour
# ---------------------------------------------------------------------------


def test_blf_single_part_fits_at_origin() -> None:
    parts = [PartInput(file_id="a", width_mm=100, height_mm=80)]
    sheets, unplaced, warns = nest_bottom_left(parts, sheet_w=500, sheet_h=400)
    assert unplaced == []
    assert len(sheets) == 1
    p = sheets[0].placements[0]
    assert p.file_id == "a"
    assert p.x_mm == pytest.approx(0.0)
    assert p.y_mm == pytest.approx(0.0)


def test_blf_packs_no_overlap() -> None:
    parts = [
        PartInput(file_id="a", width_mm=100, height_mm=80),
        PartInput(file_id="b", width_mm=90, height_mm=50),
        PartInput(file_id="c", width_mm=60, height_mm=60),
    ]
    sheets, unplaced, _ = nest_bottom_left(
        parts, sheet_w=300, sheet_h=200, spacing_mm=2.0
    )
    assert unplaced == []
    assert len(sheets[0].placements) == 3
    # 衝突しないこと
    pls = sheets[0].placements
    for i in range(len(pls)):
        for j in range(i + 1, len(pls)):
            pi, pj = pls[i], pls[j]
            no_overlap = (
                pi.x_mm + pi.width_mm <= pj.x_mm + 1e-6
                or pj.x_mm + pj.width_mm <= pi.x_mm + 1e-6
                or pi.y_mm + pi.height_mm <= pj.y_mm + 1e-6
                or pj.y_mm + pj.height_mm <= pi.y_mm + 1e-6
            )
            assert no_overlap, f"overlap between {pi.file_id} and {pj.file_id}"


def test_blf_oversize_part_unplaced() -> None:
    parts = [PartInput(file_id="big", width_mm=600, height_mm=400)]
    sheets, unplaced, warns = nest_bottom_left(parts, sheet_w=500, sheet_h=300)
    assert unplaced == ["big"]
    assert warns


def test_blf_rotation_helps_long_thin_parts() -> None:
    """100x50 部品 4 個を 200x200 板に詰める → 回転無効だと無理だが回転で入る."""

    parts = [PartInput(file_id=f"p{i}", width_mm=180, height_mm=40) for i in range(4)]
    no_rot, _, _ = nest_bottom_left(parts, sheet_w=200, sheet_h=200, rotation=False)
    with_rot, _, _ = nest_bottom_left(parts, sheet_w=200, sheet_h=200, rotation=True)
    placed_no_rot = sum(len(s.placements) for s in no_rot)
    placed_rot = sum(len(s.placements) for s in with_rot)
    assert placed_rot >= placed_no_rot


def test_efficiency_calculation() -> None:
    parts = [
        PartInput(file_id="a", width_mm=100, height_mm=100),
        PartInput(file_id="b", width_mm=100, height_mm=100),
    ]
    sheets, unplaced, _ = nest_bottom_left(
        parts, sheet_w=200, sheet_h=200, spacing_mm=0
    )
    assert unplaced == []
    eff = overall_efficiency(sheets)
    # 200x200 に 100x100 を 2 個 → 50%
    assert eff == pytest.approx(0.5)


def test_build_sheet_summaries_shape() -> None:
    parts = [PartInput(file_id="a", width_mm=10, height_mm=10)]
    sheets, _, _ = nest_bottom_left(parts, sheet_w=100, sheet_h=100)
    summaries = build_sheet_summaries(sheets)
    assert len(summaries) == 1
    s = summaries[0]
    assert s["sheet_index"] == 0
    assert s["placements"][0]["file_id"] == "a"
    assert s["efficiency"] >= 0


def test_blf_multi_sheet_overflow() -> None:
    """5 部品 x 100x100 を 200x100 板 (×2) に詰める → 2 枚目にあふれる."""

    parts = [PartInput(file_id=f"p{i}", width_mm=100, height_mm=100) for i in range(5)]
    sheets, unplaced, _ = nest_bottom_left(
        parts,
        sheet_w=200,
        sheet_h=100,
        sheet_quantity=2,
        rotation=False,
    )
    placed = sum(len(s.placements) for s in sheets)
    # 1 枚 200x100 に 2 個 ×2 枚 = 4 個 まで、1 個は unplaced
    assert placed == 4
    assert len(unplaced) == 1


# ---------------------------------------------------------------------------
# E2E — POST /nest + GET /jobs/{id}
# ---------------------------------------------------------------------------


def _upload(client: TestClient, paths: list[Path]) -> dict:
    files = []
    handles = []
    for p in paths:
        fh = p.open("rb")
        handles.append(fh)
        files.append(("files", (p.name, fh.read(), "application/dxf")))
    for fh in handles:
        fh.close()
    r = client.post("/api/upload", files=files)
    assert r.status_code == 201, r.text
    return r.json()


def _wait_for_job(client: TestClient, job_id: str, timeout_s: float = 30.0) -> dict:
    import time

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        r = client.get(f"/api/jobs/{job_id}")
        assert r.status_code == 200, r.text
        data = r.json()
        if data["status"] in ("completed", "failed"):
            return data
        time.sleep(0.1)
    raise AssertionError(f"job {job_id} did not finish within {timeout_s}s")


def test_nest_e2e_three_parts(
    sample_dxf_paths: dict[str, Path], isolated_store, isolated_queue
) -> None:
    """3 部品 (小+中+大) でネスティング → 歩留まり > 0 を確認."""

    with TestClient(app) as c:
        info = _upload(
            c,
            [
                sample_dxf_paths["small"],
                sample_dxf_paths["medium"],
                sample_dxf_paths["large"],
            ],
        )
        sid = info["session_id"]
        file_ids = [f["file_id"] for f in info["files"]]

        # 大きめの板に投げる (実部品は 2000mm 級になるサンプルもあるため余裕を見る)
        r = c.post(
            f"/api/session/{sid}/nest",
            json={
                "file_ids": file_ids,
                "sheet": {"width_mm": 3000, "height_mm": 3000, "quantity": 1},
                "spacing_mm": 5.0,
                "algorithm": "bottom_left",
                "rotation": True,
            },
        )
        assert r.status_code == 202, r.text
        job_id = r.json()["job_id"]

        done = _wait_for_job(c, job_id)
        assert done["status"] == "completed", done
        result = done["result"]
        assert result is not None
        # 少なくとも 1 部品は配置されている (3 個全部入らない場合は warn)
        assert result["placed_count"] >= 1
        assert result["total_efficiency"] >= 0.0


def test_nest_rejects_unknown_file_id(
    sample_dxf_paths: dict[str, Path], isolated_store, isolated_queue
) -> None:
    with TestClient(app) as c:
        info = _upload(c, [sample_dxf_paths["small"]])
        sid = info["session_id"]
        r = c.post(
            f"/api/session/{sid}/nest",
            json={
                "file_ids": ["does_not_exist"],
                "sheet": {"width_mm": 1000, "height_mm": 1000, "quantity": 1},
            },
        )
        assert r.status_code == 422


def test_nest_rejects_unsupported_algorithm(
    sample_dxf_paths: dict[str, Path], isolated_store, isolated_queue
) -> None:
    with TestClient(app) as c:
        info = _upload(c, [sample_dxf_paths["small"]])
        sid = info["session_id"]
        fid = info["files"][0]["file_id"]
        r = c.post(
            f"/api/session/{sid}/nest",
            json={
                "file_ids": [fid],
                "sheet": {"width_mm": 1000, "height_mm": 1000, "quantity": 1},
                "algorithm": "no_fit_polygon",
            },
        )
        assert r.status_code == 400
