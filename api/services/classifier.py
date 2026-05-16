"""Heuristic classifier for DXF entities → CutFlow CAD categories.

The sample drawings ship with layer names that are pure digits (``"0"``,
``"1"``…), so we cannot rely on layer semantics. Detection here uses entity
type + block-content signatures + geometric features (size, location).

Design philosophy
-----------------

* False positives on the **delete** side are far more harmful than false
  negatives. A vanishing outer-shape line cannot be recovered from the SVG;
  a stray dimension is just clutter. So whenever we are uncertain we fall
  back to ``other`` (= not a delete candidate).
* No machine learning, no fuzzy thresholds. Every rule is auditable and can
  be tweaked once we see how it behaves on real worksite drawings.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from typing import Any, Iterable

from ezdxf.document import Drawing
from ezdxf.entities import DXFEntity

from services.outer_detector import detect_frame_polyline

log = logging.getLogger(__name__)

# Block-name patterns that strongly suggest a tap (screw thread) symbol.
# Anchored at the start so a JIS dim-glyph name like ``JISB0001-...`` doesn't
# accidentally match "0001" inside the name. ``M[0-9]+`` is anchored on a
# word boundary so we still catch ``BLOCK_M8`` and friends.
_TAP_NAME_RE = re.compile(
    r"^(TAP\b|M[0-9]+\b|ねじ|タップ|JISB0205|JISB0001|0205\b|0001\b)",
    re.IGNORECASE,
)
# Block-name patterns for JIS surface-finish / datum / dimension callouts.
# These are render markers ("どこに何mm" 系)、not the outer profile.
_DIM_GLYPH_BLOCK_RE = re.compile(r"^(AGM_)?JISB?\d{4}", re.IGNORECASE)

# Numeric-only text used for balloon callouts (1〜3 ascii digits).
_BALLOON_TEXT_RE = re.compile(r"^[\s\(]*(\d{1,3})[\s\)]*$")


def classify_entities(
    entities: Iterable[tuple[str, DXFEntity, dict[str, Any]]],
    doc: Drawing,
) -> tuple[dict[str, str], dict[str, list[str]]]:
    """Return ``(category_by_id, delete_candidates)``.

    ``entities`` is an iterable of ``(entity_id, dxf_entity, geom_dict)`` as
    produced by :func:`services.dxf_parser.parse_file`.

    ``delete_candidates`` is keyed by the UI checkbox name
    (``DIMENSION`` / ``BALLOON`` / ``TAP`` / ``FRAME``).
    """

    items = list(entities)
    categories: dict[str, str] = {}
    candidates: dict[str, list[str]] = {
        "DIMENSION": [],
        "BALLOON": [],
        "TAP": [],
        "FRAME": [],
    }

    # Precompute per-block fingerprints once (cheap and reused for every INSERT).
    # NOTE: ``block_names()`` lower-cases its result, but ``INSERT.dxf.name`` keeps
    # the original case. We iterate ``doc.blocks`` directly so the dict key matches
    # what the lookup will use.
    block_sigs = {block.name: _block_signature(doc, block.name) for block in doc.blocks}

    # Detect "the" frame polyline (largest closed LWPOLYLINE, if any).
    frame_id = detect_frame_polyline(items)
    if frame_id is not None:
        categories[frame_id] = "frame"
        candidates["FRAME"].append(frame_id)

    for eid, ent, _geom in items:
        if eid in categories:
            continue
        t = ent.dxftype()

        if t in ("DIMENSION", "LEADER"):
            categories[eid] = "dim"
            candidates["DIMENSION"].append(eid)
            continue

        if t == "INSERT":
            cat = _classify_insert(ent, block_sigs)
            categories[eid] = cat
            if cat == "tap":
                candidates["TAP"].append(eid)
            elif cat == "balloon":
                candidates["BALLOON"].append(eid)
            elif cat == "dim":
                # Surface-finish / datum glyphs — drop with dimensions.
                candidates["DIMENSION"].append(eid)
            elif cat == "frame":
                # Title-block style INSERT (BLOCK006 in the samples).
                candidates["FRAME"].append(eid)
            continue

        if t == "HATCH":
            # Section hatching is a drafting overlay, not the profile.
            categories[eid] = "dim"
            candidates["DIMENSION"].append(eid)
            continue

        if t in ("TEXT", "MTEXT"):
            # Stand-alone TEXT/MTEXT can be table titles, part numbers in the
            # title-block, layer notes etc. We intentionally do NOT enqueue
            # them into the DIMENSION delete bucket — a one-click "寸法線削除"
            # must not also wipe the title-block text. The user can still
            # delete them individually if needed.
            categories[eid] = "other"
            continue

        # Geometry primitives stay as ``other`` here; the outer-detection pass
        # (Phase 2) will promote them to ``outer`` / ``hole``.
        categories.setdefault(eid, "other")

    # Deduplicate (an entity should only appear in one bucket).
    for key in candidates:
        seen: set[str] = set()
        deduped: list[str] = []
        for x in candidates[key]:
            if x in seen:
                continue
            seen.add(x)
            deduped.append(x)
        candidates[key] = deduped

    return categories, candidates


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _block_signature(doc: Drawing, name: str) -> dict[str, Any]:
    """Compute a tiny fingerprint we use to classify INSERTs.

    Walks the block once and records counts + radii + any text strings.
    """

    block = doc.blocks.get(name)
    sig: dict[str, Any] = {
        "name": name,
        "counts": Counter(),
        "circle_radii": [],
        "texts": [],
        "has_polyline": False,
    }
    if block is None:
        return sig

    for be in block:
        bt = be.dxftype()
        sig["counts"][bt] += 1
        if bt == "CIRCLE":
            try:
                sig["circle_radii"].append(float(be.dxf.radius))
            except Exception:  # noqa: BLE001
                pass
        elif bt in ("TEXT", "MTEXT"):
            try:
                txt = be.plain_text() if bt == "MTEXT" else str(be.dxf.text)
            except Exception:  # noqa: BLE001
                txt = str(getattr(be.dxf, "text", ""))
            if txt:
                sig["texts"].append(txt)
        elif bt in ("LWPOLYLINE", "POLYLINE"):
            sig["has_polyline"] = True
    return sig


def _classify_insert(ent: DXFEntity, block_sigs: dict[str, dict[str, Any]]) -> str:
    """Decide outer / hole / dim / balloon / tap / other for a single INSERT."""

    name = str(ent.dxf.name)
    sig = block_sigs.get(name, {"counts": Counter(), "circle_radii": [], "texts": []})
    counts: Counter = sig["counts"]
    radii: list[float] = sorted(sig["circle_radii"])

    # 1) Name-based TAP wins over JIS dim glyphs: an explicit ``M\d+`` /
    #    "TAP" / "ねじ" / "タップ" token anywhere in the block name almost
    #    always indicates a screw thread mark, even if the JIS prefix is
    #    also present (e.g. ``JISB0001_M8`` style naming we sometimes see).
    if re.search(r"\b(M[0-9]+|TAP|ねじ|タップ)\b", name, re.IGNORECASE):
        return "tap"

    # 2) Obvious JIS dimension glyphs (surface finish, datum, etc.).
    if _DIM_GLYPH_BLOCK_RE.match(name):
        return "dim"

    # 3) Other name-based TAP signals (JIS thread standards by code).
    if _TAP_NAME_RE.search(name):
        return "tap"

    # 4) Double-circle (inner + outer) signature → tap mark.
    #    Outer circle ≈ pitch dia, inner ≈ minor dia. Cross-hair LINES are optional.
    if (
        len(radii) >= 2
        and counts.get("CIRCLE", 0) >= 2
        and counts.get("LINE", 0) >= 0
        and 0.3 < (radii[0] / radii[-1]) < 0.95
        and radii[-1] < 30.0  # taps are small (< ~Ø60mm on these drawings)
    ):
        return "tap"

    # 5) Balloon: single circle + numeric text, OR explicit numeric texts only.
    if counts.get("CIRCLE", 0) == 1 and counts.get("MTEXT", 0) + counts.get("TEXT", 0) >= 1:
        if any(_BALLOON_TEXT_RE.match(t.strip()) for t in sig["texts"]):
            return "balloon"

    # 6) Heuristic balloon: a 6-line "hexagon" or 7-line frame + one MTEXT.
    if counts.get("MTEXT", 0) + counts.get("TEXT", 0) == 1 and counts.get("LINE", 0) in (3, 6, 7, 8):
        if any(_BALLOON_TEXT_RE.match(t.strip()) for t in sig["texts"]):
            return "balloon"

    # 7) Title block: ANY block holding many MTEXT items AND a closed polyline
    #    looks like a drawing frame (BLOCK006 in the samples). Mark as frame.
    if counts.get("MTEXT", 0) >= 10 and sig["has_polyline"]:
        return "frame"

    return "other"
