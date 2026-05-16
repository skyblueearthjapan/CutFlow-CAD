"""Discord 通知の単体テスト (ファイル出力経由)."""

from __future__ import annotations

import json
from pathlib import Path

from services import discord_notify


def test_disabled_by_default(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("CUTFLOW_DISCORD_NOTIFY", raising=False)
    inbox = tmp_path / "inbox.txt"
    monkeypatch.setenv("CUTFLOW_DISCORD_INBOX", str(inbox))
    assert discord_notify.notify_session_created("sid", 3) is False
    assert not inbox.exists()


def test_enabled_writes_appendable_line(tmp_path: Path, monkeypatch) -> None:
    inbox = tmp_path / "subdir" / "inbox.txt"
    monkeypatch.setenv("CUTFLOW_DISCORD_NOTIFY", "true")
    monkeypatch.setenv("CUTFLOW_DISCORD_INBOX", str(inbox))

    assert discord_notify.notify_session_created("sid1", 5) is True
    assert discord_notify.notify_job_finished("nest", "abc12345", "completed") is True
    assert discord_notify.notify_error("offset", "boom") is True

    lines = inbox.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 3
    parsed = [json.loads(l) for l in lines]
    assert parsed[0]["event"] == "session_created"
    assert parsed[0]["file_count"] == 5
    assert parsed[1]["event"] == "job_finished"
    assert parsed[1]["status"] == "completed"
    assert parsed[2]["event"] == "error"


def test_failure_to_write_does_not_raise(tmp_path: Path, monkeypatch) -> None:
    # readonly dir simulated via a path that cannot be created (file in the way)
    bad = tmp_path / "blocker"
    bad.write_text("not a dir")
    monkeypatch.setenv("CUTFLOW_DISCORD_NOTIFY", "true")
    monkeypatch.setenv("CUTFLOW_DISCORD_INBOX", str(bad / "child.txt"))
    # Should swallow OSError gracefully (not raise)
    result = discord_notify.notify_session_created("sid", 1)
    # On Windows behaviour varies; either False (failed) or True (succeeded
    # via overwrite). Just make sure it doesn't raise.
    assert isinstance(result, bool)
