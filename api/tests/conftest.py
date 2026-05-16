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


# Three representative sample drawings selected by the spec. The path is
# overridable through ``CUTFLOW_SAMPLES_DIR`` so CI environments can ship
# their own fixtures without editing this file (H3). A repo-local default
# at ``api/tests/fixtures/samples/`` is checked first so contributors only
# need to drop the DXFs in once.
_LOCAL_DEFAULT = Path(__file__).parent / "fixtures" / "samples"
_LEGACY_DEFAULT = Path(
    r"C:/Users/imaizumi.LINEWORKS-NET/Desktop/コベルコブームRBシステム/P1_昇降軸/昇降軸"
)
SAMPLE_DIR = Path(
    os.environ.get(
        "CUTFLOW_SAMPLES_DIR",
        str(_LOCAL_DEFAULT if _LOCAL_DEFAULT.exists() else _LEGACY_DEFAULT),
    )
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


@pytest.fixture()
def isolated_queue(tmp_path, monkeypatch):
    """Reset the global job queue singleton with a per-test root dir.

    Required by any test that submits jobs via /api/session/{sid}/nest —
    otherwise records / asyncio.Queue from a previous TestClient lifespan
    leak between tests and the queue gets stuck waiting on a stale loop.
    """

    from services.job_queue import reset_queue_for_tests

    monkeypatch.setenv("CUTFLOW_JOB_ROOT", str(tmp_path / "jobs"))
    q = reset_queue_for_tests(root=tmp_path / "jobs", worker_count=2)
    yield q
