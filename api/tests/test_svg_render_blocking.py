"""Phase 6 HIGH-1 — ``/render-svg`` must not block the event loop.

ezdxf's render is CPU-bound (tens to hundreds of milliseconds on a busy
DXF). Calling it directly from an ``async def`` endpoint pins the asyncio
loop while it runs, starving every other request on the worker. The fix
in ``routers/files.py`` dispatches the call via
``fastapi.concurrency.run_in_threadpool`` so the event loop can keep
serving while ezdxf chews on the geometry.

This test pins the contract structurally by patching
``run_in_threadpool`` and asserting the endpoint calls it. The structural
check is cheap and resilient — a future refactor that drops the offload
fails this test the moment it lands.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import ezdxf
from fastapi.testclient import TestClient

from main import app


def _write_sample_dxf(path: Path) -> None:
    doc = ezdxf.new()
    msp = doc.modelspace()
    msp.add_line((0, 0), (100, 0))
    msp.add_line((100, 0), (100, 50))
    msp.add_line((100, 50), (0, 50))
    msp.add_line((0, 50), (0, 0))
    doc.saveas(str(path))


def _upload(c: TestClient, path: Path) -> tuple[str, str]:
    with path.open("rb") as fh:
        r = c.post(
            "/api/upload",
            files=[("files", (path.name, fh.read(), "application/dxf"))],
        )
    assert r.status_code == 201, r.text
    data = r.json()
    return data["session_id"], data["files"][0]["file_id"]


def test_render_svg_dispatches_via_threadpool(tmp_path: Path, isolated_store) -> None:
    """The endpoint must hand off to ``run_in_threadpool`` so it cannot
    monopolize the asyncio loop on a slow render."""

    p = tmp_path / "sample.dxf"
    _write_sample_dxf(p)

    with TestClient(app) as c:
        sid, fid = _upload(c, p)

        # Patch the symbol the router imported (not the original module
        # path) so the wrapper sees our spy.
        with patch(
            "routers.files.run_in_threadpool",
            wraps=__import__(
                "fastapi.concurrency", fromlist=["run_in_threadpool"]
            ).run_in_threadpool,
        ) as spy:
            r = c.get(f"/api/session/{sid}/file/{fid}/render-svg")
            assert r.status_code == 200, r.text
            spy.assert_called()  # at least one offload happened
