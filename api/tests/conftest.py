"""Pytest fixtures shared by the API tests."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Allow running pytest from the repo root or from api/.
_API_DIR = Path(__file__).resolve().parents[1]
if str(_API_DIR) not in sys.path:
    sys.path.insert(0, str(_API_DIR))


# Three representative sample drawings selected by the spec.
SAMPLE_DIR = Path(
    r"C:/Users/imaizumi.LINEWORKS-NET/Desktop/コベルコブームRBシステム/P1_昇降軸/昇降軸"
)
SAMPLE_FILES = {
    "small": "25057-P1-06_カラー.DXF",
    "medium": "25057-P1-03_センタープレート.DXF",
    "large": "25057-P1-01②_ベースフレーム.DXF",
}


@pytest.fixture(scope="session")
def sample_dxf_paths() -> dict[str, Path]:
    """Map sample size → absolute path on disk; skip if unavailable."""

    paths = {k: SAMPLE_DIR / v for k, v in SAMPLE_FILES.items()}
    missing = [str(p) for p in paths.values() if not p.exists()]
    if missing:
        pytest.skip(f"sample DXFs not available: {missing}")
    return paths


@pytest.fixture()
def isolated_store(tmp_path, monkeypatch):
    """Point the singleton session store at a per-test temp dir."""

    from storage.session_store import reset_store_for_tests

    monkeypatch.setenv("CUTFLOW_SESSION_ROOT", str(tmp_path))
    store = reset_store_for_tests(tmp_path)
    yield store
