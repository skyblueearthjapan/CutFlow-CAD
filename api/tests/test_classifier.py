"""Classifier edge-case tests.

These guard against regressions of the most damaging classifier mistakes:

* **C1**: standalone TEXT / MTEXT must never land in the DIMENSION delete
  bucket. If it did, "寸法線をまとめて削除" would also wipe the title-block
  text and the user would lose context.
"""

from __future__ import annotations

import ezdxf

from services.classifier import classify_entities


def _build_doc_with_standalone_text() -> tuple[
    object, list[tuple[str, object, dict]]
]:
    """Build an in-memory DXF with one standalone TEXT, one MTEXT and a few
    geometry primitives. Returns ``(doc, items)`` shaped for
    ``classify_entities``."""

    doc = ezdxf.new(setup=True)
    msp = doc.modelspace()
    msp.add_text("Title", dxfattribs={"insert": (10, 10)})
    msp.add_mtext("Notes\nwith multiple lines", dxfattribs={"insert": (20, 20)})
    msp.add_line((0, 0), (100, 0))
    msp.add_circle((50, 50), 5)

    items: list[tuple[str, object, dict]] = []
    for idx, e in enumerate(msp):
        eid = f"e{idx:05d}"
        items.append((eid, e, {}))
    return doc, items


def test_standalone_text_not_in_dimension_bucket() -> None:
    """C1 regression: TEXT/MTEXT must not be enqueued for DIMENSION delete."""

    doc, items = _build_doc_with_standalone_text()
    categories, candidates = classify_entities(items, doc)

    text_eids = {eid for eid, e, _ in items if e.dxftype() in ("TEXT", "MTEXT")}
    assert text_eids, "fixture should produce TEXT + MTEXT entities"
    overlap = text_eids & set(candidates["DIMENSION"])
    assert overlap == set(), (
        f"standalone TEXT/MTEXT leaked into DIMENSION bucket: {overlap}"
    )
    # They should still be classified as ``other`` (not deleted by default).
    for eid in text_eids:
        assert categories[eid] == "other"


def test_standalone_text_not_in_any_delete_bucket() -> None:
    """Stronger guard: standalone TEXT/MTEXT must not appear in ANY bucket."""

    doc, items = _build_doc_with_standalone_text()
    _categories, candidates = classify_entities(items, doc)

    text_eids = {eid for eid, e, _ in items if e.dxftype() in ("TEXT", "MTEXT")}
    for bucket in ("DIMENSION", "BALLOON", "TAP", "FRAME"):
        assert text_eids.isdisjoint(set(candidates[bucket])), (
            f"TEXT/MTEXT leaked into {bucket} bucket"
        )


def test_real_sample_text_safe(sample_dxf_paths) -> None:
    """Live sample sanity check: parse the medium drawing and confirm that
    no standalone TEXT/MTEXT entity lands in DIMENSION."""

    from services.dxf_parser import parse_file

    payload = parse_file(sample_dxf_paths["medium"], file_id="fid", name="m")
    text_ids = {e.id for e in payload.entities if e.type in ("TEXT", "MTEXT")}
    leaked = text_ids & set(payload.delete_candidates.DIMENSION)
    assert leaked == set(), (
        f"sample drawing leaks {len(leaked)} TEXT/MTEXT into DIM bucket"
    )
