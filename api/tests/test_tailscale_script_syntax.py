"""H4 / H8 — deploy/tailscale-setup.sh stays syntactically valid bash.

The check uses ``bash -n`` (parse only — does not actually invoke the
script). Skipped silently when bash is not on PATH (e.g. minimal
Windows CI images without WSL).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "deploy" / "tailscale-setup.sh"


def test_tailscale_script_parses() -> None:
    if not _SCRIPT.exists():
        pytest.skip("deploy/tailscale-setup.sh not present in checkout")
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash not available on PATH")
    proc = subprocess.run(
        [bash, "-n", str(_SCRIPT)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert proc.returncode == 0, (
        f"bash -n failed: stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )


def test_tailscale_script_avoids_reset_flag() -> None:
    """H4 — ``--reset`` blows away an existing identity. The script must
    never call ``tailscale up --reset`` unconditionally.

    We strip comments / strings before the scan so a comment that
    mentions ``--reset`` for documentation doesn't trip the regex.
    """

    if not _SCRIPT.exists():
        pytest.skip("deploy/tailscale-setup.sh not present in checkout")
    raw = _SCRIPT.read_text(encoding="utf-8")
    code_lines: list[str] = []
    for line in raw.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        # Drop trailing inline comments (best-effort; bash doesn't have a
        # rigorous quoted-context tokeniser here but the script keeps it
        # simple).
        if " #" in line:
            line = line.split(" #", 1)[0]
        code_lines.append(line)
    code = "\n".join(code_lines)
    assert "--reset" not in code, (
        "deploy/tailscale-setup.sh must not pass --reset to 'tailscale up'"
    )
