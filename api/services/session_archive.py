"""セッションの ``tar.gz`` 圧縮 / 展開 — Phase 5 履歴管理用.

セッションディレクトリ (`originals/`, `state/`, `deleted.json` 等) をま
るごと圧縮し、``CUTFLOW_SAVED_ROOT`` (デフォルト ``/var/cutflow/saved-sessions``
または tmp フォールバック) に名前付きで保存する。読み込み時は新しい
``session_id`` で展開し、新たに ``meta.json`` を書き換えて TTL を延長する。
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import tarfile
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger(__name__)

MAX_ARCHIVE_BYTES = 100 * 1024 * 1024  # 100 MB
SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_\-　-鿿゠-ヿ぀-ゟ.]")
_DEFAULT_SAVED_ROOT = Path("/var/cutflow/saved-sessions")


def _default_root() -> Path:
    raw = os.environ.get("CUTFLOW_SAVED_ROOT")
    if raw:
        return Path(raw)
    try:
        _DEFAULT_SAVED_ROOT.mkdir(parents=True, exist_ok=True)
        test = _DEFAULT_SAVED_ROOT / ".write_test"
        test.write_text("ok", encoding="utf-8")
        test.unlink(missing_ok=True)
        return _DEFAULT_SAVED_ROOT
    except OSError:
        return Path(tempfile.gettempdir()) / "cutflow-saved-sessions"


class ArchiveError(Exception):
    pass


@dataclass
class ArchiveInfo:
    name: str
    path: Path
    size_bytes: int
    saved_at: datetime
    file_count: int = 0


def sanitize_name(name: str) -> str:
    """ファイル名として安全な文字に正規化する (日本語は許容)."""

    cleaned = SAFE_NAME_RE.sub("_", name.strip())
    cleaned = cleaned.strip("._-")
    if not cleaned:
        raise ArchiveError("空のセッション名は保存できません")
    if len(cleaned) > 128:
        cleaned = cleaned[:128]
    return cleaned


def saved_root(custom: Path | None = None) -> Path:
    root = Path(custom) if custom else _default_root()
    root.mkdir(parents=True, exist_ok=True)
    return root


def archive_path_for(name: str, root: Path | None = None) -> Path:
    return saved_root(root) / f"{sanitize_name(name)}.tar.gz"


def save_session(
    session_dir: Path,
    name: str,
    root: Path | None = None,
) -> ArchiveInfo:
    """セッションディレクトリを ``{name}.tar.gz`` として保存する.

    既存名は上書き。サイズ制限を超えそうな入力は中断 (``ArchiveError``).
    """

    if not session_dir.exists():
        raise ArchiveError(f"session dir not found: {session_dir}")
    safe = sanitize_name(name)
    target = saved_root(root) / f"{safe}.tar.gz"

    # 圧縮前に粗い合計サイズチェック (展開時の安全策)
    total = 0
    file_count = 0
    for p in session_dir.rglob("*"):
        if p.is_file():
            try:
                total += p.stat().st_size
                if p.suffix.lower() == ".dxf":
                    file_count += 1
            except OSError:
                continue
    if total > MAX_ARCHIVE_BYTES * 4:  # 圧縮率を雑に 4x と仮定
        raise ArchiveError(
            f"session too large to archive ({total // (1024 * 1024)} MB raw, max ~100 MB compressed)"
        )

    # tmp に書いてから rename することで部分書き込みを避ける
    fd, tmp_path = tempfile.mkstemp(prefix=".save.", suffix=".tar.gz", dir=str(target.parent))
    os.close(fd)
    try:
        with tarfile.open(tmp_path, "w:gz") as tar:
            tar.add(str(session_dir), arcname="session", recursive=True)
        size = Path(tmp_path).stat().st_size
        if size > MAX_ARCHIVE_BYTES:
            raise ArchiveError(
                f"compressed archive exceeds {MAX_ARCHIVE_BYTES // (1024 * 1024)} MB"
            )
        os.replace(tmp_path, target)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    saved_at = datetime.now(timezone.utc)
    return ArchiveInfo(
        name=safe,
        path=target,
        size_bytes=target.stat().st_size,
        saved_at=saved_at,
        file_count=file_count,
    )


def list_saved(root: Path | None = None) -> list[ArchiveInfo]:
    out: list[ArchiveInfo] = []
    base = saved_root(root)
    for p in sorted(base.glob("*.tar.gz")):
        try:
            st = p.stat()
            file_count = _count_dxfs_in_archive(p)
            out.append(
                ArchiveInfo(
                    name=p.stem.replace(".tar", ""),
                    path=p,
                    size_bytes=st.st_size,
                    saved_at=datetime.fromtimestamp(st.st_mtime, tz=timezone.utc),
                    file_count=file_count,
                )
            )
        except OSError as exc:
            log.warning("cannot stat saved archive %s: %s", p, exc)
    return out


def _count_dxfs_in_archive(path: Path) -> int:
    """archive 内の .dxf 数 (UI 用)。失敗時は 0."""

    try:
        with tarfile.open(path, "r:gz") as tar:
            return sum(1 for m in tar.getmembers() if m.isfile() and m.name.lower().endswith(".dxf"))
    except (tarfile.TarError, OSError) as exc:
        log.debug("dxf count failed for %s: %s", path, exc)
        return 0


def load_session(
    name: str,
    sessions_root: Path,
    ttl_hours: int,
    saved_root_override: Path | None = None,
) -> tuple[str, int]:
    """tar.gz を展開して新しい ``session_id`` で復元する.

    Returns ``(new_session_id, file_count)``.
    """

    safe = sanitize_name(name)
    archive = saved_root(saved_root_override) / f"{safe}.tar.gz"
    if not archive.exists():
        raise ArchiveError(f"saved session not found: {safe}")

    new_sid = uuid.uuid4().hex
    target_dir = sessions_root / new_sid
    target_dir.mkdir(parents=True, exist_ok=True)

    # tmp 展開後 → target に rename したいが、tarfile は in-place 展開のみ
    # なので直接展開し、失敗時はディレクトリごとロールバック。
    # H5: zip-bomb / symlink / FIFO / device 対策
    #   - 通常ファイル + ディレクトリのみ許可 (symlink / device / fifo を拒否)
    #   - 解凍後の合計サイズが 4x MAX_ARCHIVE_BYTES を超えたら abort
    #   - Python 3.12+ の ``filter='data'`` が使えれば併用
    try:
        max_total = MAX_ARCHIVE_BYTES * 4
        with tarfile.open(archive, "r:gz") as tar:
            safe_members = []
            total_uncompressed = 0
            for m in tar.getmembers():
                if m.name.startswith("/") or ".." in Path(m.name).parts:
                    log.warning("rejecting suspicious tar member: %s", m.name)
                    continue
                # H5: 通常ファイル/ディレクトリのみ許可
                if not (m.isfile() or m.isdir()):
                    log.warning(
                        "rejecting non-regular tar member: %s (type=%s)", m.name, m.type
                    )
                    continue
                if m.isfile():
                    total_uncompressed += int(m.size or 0)
                    if total_uncompressed > max_total:
                        raise ArchiveError(
                            f"archive uncompressed size exceeds {max_total // (1024 * 1024)} MB"
                        )
                safe_members.append(m)
            # Python 3.12+ filter='data' で hardlink/symlink/device を拒否
            try:
                tar.extractall(str(target_dir), members=safe_members, filter="data")
            except TypeError:
                # 古い Python (3.11 以下) — filter 引数なし
                tar.extractall(str(target_dir), members=safe_members)
    except Exception:
        shutil.rmtree(target_dir, ignore_errors=True)
        raise

    # archive 内は ``session/...`` で入っている想定 — 直下に持ち上げる
    inner = target_dir / "session"
    if inner.exists():
        for child in inner.iterdir():
            shutil.move(str(child), str(target_dir / child.name))
        shutil.rmtree(inner, ignore_errors=True)

    # meta.json を新セッションID & TTL で書き直す
    meta_path = target_dir / "meta.json"
    file_count = 0
    if meta_path.exists():
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
            data["session_id"] = new_sid
            now = datetime.now(timezone.utc)
            data["created_at"] = now.isoformat()
            data["expires_at"] = (now + timedelta(hours=ttl_hours)).isoformat()
            # files[].path を新セッションのパスへ書き換え
            for f in data.get("files") or []:
                old_path = Path(f.get("path") or "")
                f["path"] = str(target_dir / "originals" / old_path.name)
            file_count = len(data.get("files") or [])
            meta_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("failed to rewrite meta.json after load: %s", exc)
    return new_sid, file_count


def delete_saved(name: str, root: Path | None = None) -> bool:
    target = saved_root(root) / f"{sanitize_name(name)}.tar.gz"
    if not target.exists():
        return False
    try:
        target.unlink()
        return True
    except OSError as exc:
        log.warning("cannot delete saved session %s: %s", name, exc)
        return False
