"""DIMENSION text extraction (H2).

AutoCAD stores ``<>`` (literally) in ``DIMENSION.dxf.text`` to indicate "use
the measured value". The parser must resolve that to a number via
``actual_measurement`` / ``get_measurement()`` rather than handing the literal
``<>`` over to the SVG renderer.
"""

from __future__ import annotations

import ezdxf

from services.dxf_parser import _geom_for, parse_file


def _make_dim_doc() -> ezdxf.document.Drawing:
    doc = ezdxf.new(setup=True)
    msp = doc.modelspace()
    # Horizontal linear dim from (0,0) to (100,0), text below at y=-10.
    dim = msp.add_linear_dim(
        base=(0, -10),
        p1=(0, 0),
        p2=(100, 0),
        dimstyle="EZDXF",
    )
    dim.render()
    return doc


def test_dimension_text_resolves_measured_value() -> None:
    """When DXF text is empty/``<>``, the parser surfaces the measurement."""

    doc = _make_dim_doc()
    msp = doc.modelspace()
    dims = [e for e in msp if e.dxftype() == "DIMENSION"]
    assert dims, "fixture should yield a DIMENSION"

    geom = _geom_for(dims[0], doc)
    text = geom.get("text", "")
    # Either we got a numeric string (e.g. "100.0") or the safe fallback.
    assert text and text != "<>", f"unexpected text: {text!r}"
    if text != "(寸法)":
        # Should be a float-ish render of the measured value (≈ 100).
        try:
            val = float(text)
        except ValueError as exc:
            raise AssertionError(f"text {text!r} not numeric") from exc
        assert 95.0 <= val <= 105.0, f"measurement out of range: {val}"


def test_sample_dimension_text_not_literal_angle_brackets(sample_dxf_paths) -> None:
    """No DIMENSION on the real samples should still carry literal ``<>``."""

    payload = parse_file(sample_dxf_paths["medium"], file_id="fid", name="m")
    for e in payload.entities:
        if e.type != "DIMENSION":
            continue
        assert e.geom.get("text") != "<>", (
            f"DIMENSION {e.id} retained literal '<>' as its text"
        )
