"""H6 — auto_distribute on a single closed LWPOLYLINE outer.

Synthesises a payload-like object whose outer loop is one LWPOLYLINE
(square 100x100). The auto-distribute walk must visit each chord
segment so the bridges land roughly along the real perimeter rather
than degenerating onto a single chord between first / last vertices.
"""

from __future__ import annotations

from dataclasses import dataclass

from services.bridges import attach_positions, auto_distribute


@dataclass
class _Ent:
    id: str
    type: str
    geom: dict


class _Payload:
    def __init__(self, entities):
        self.entities = entities


def _square_polyline_payload() -> tuple[_Payload, list[str]]:
    verts = [
        [0.0, 0.0],
        [100.0, 0.0],
        [100.0, 100.0],
        [0.0, 100.0],
    ]
    poly = _Ent(
        id="poly1",
        type="LWPOLYLINE",
        geom={"vertices": verts, "closed": True},
    )
    return _Payload([poly]), ["poly1"]


def test_auto_distribute_walks_polyline_segments() -> None:
    payload, loop = _square_polyline_payload()
    out = auto_distribute(payload, loop, count=4, width_mm=2.0)
    assert len(out) == 4
    # Composite labels (En#k) should be emitted for the per-segment slots.
    labels = {b["edge_id"] for b in out}
    assert all("#" in lbl for lbl in labels), labels
    # All ratios are in [0, 1] (the slot mapper clamps).
    for b in out:
        assert 0.0 <= b["position_ratio"] <= 1.0


def test_attach_positions_resolves_composite_labels() -> None:
    payload, loop = _square_polyline_payload()
    out = auto_distribute(payload, loop, count=4, width_mm=2.0)
    enriched = attach_positions(out, payload, loop)
    assert all(b.get("position") is not None for b in enriched)
    # Four bridges across the 400 mm perimeter → roughly cardinal-ish.
    xs = [b["position"][0] for b in enriched]
    ys = [b["position"][1] for b in enriched]
    # We expect at least two distinct X values AND two distinct Y values
    # (i.e. bridges are not all on one edge).
    assert len(set(round(x, 1) for x in xs)) >= 2
    assert len(set(round(y, 1) for y in ys)) >= 2
