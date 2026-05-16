"""材質・板厚・加工代テンプレートの読み込み — Phase 5.

固定 JSON (``api/data/templates.json``) を起動時にメモリにロードする。
カスタム JSON は ``CUTFLOW_TEMPLATES_FILE`` 環境変数で指定可能 (運用者
が追加プリセットを差し込みたい場合)。
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from threading import Lock

log = logging.getLogger(__name__)

_DEFAULT_PATH = Path(__file__).resolve().parents[1] / "data" / "templates.json"

_CACHE: list[dict] | None = None
_CACHE_LOCK = Lock()


def _path() -> Path:
    raw = os.environ.get("CUTFLOW_TEMPLATES_FILE")
    if raw:
        return Path(raw)
    return _DEFAULT_PATH


def _load_raw() -> list[dict]:
    p = _path()
    if not p.exists():
        log.warning("templates file not found: %s", p)
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.error("failed to load templates from %s: %s", p, exc)
        return []
    items = data.get("templates") if isinstance(data, dict) else None
    if not isinstance(items, list):
        log.warning("templates file has no 'templates' list: %s", p)
        return []
    # 軽い正規化 (id 重複は最後勝ち)
    by_id: dict[str, dict] = {}
    for t in items:
        if not isinstance(t, dict) or not t.get("id"):
            continue
        by_id[str(t["id"])] = t
    return list(by_id.values())


def list_templates() -> list[dict]:
    """Cached list of template dicts (JSON-shaped)."""

    global _CACHE
    with _CACHE_LOCK:
        if _CACHE is None:
            _CACHE = _load_raw()
        return list(_CACHE)


def find_template(template_id: str) -> dict | None:
    for t in list_templates():
        if t.get("id") == template_id:
            return t
    return None


def reload_templates() -> int:
    """ホットリロード — テスト/運用で JSON を編集した直後に呼ぶ用."""

    global _CACHE
    with _CACHE_LOCK:
        _CACHE = _load_raw()
        return len(_CACHE)
