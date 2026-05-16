"""PDF export (Phase 3) — A4 landscape, 1 part per page, optional 材料取り枠.

The output is intentionally minimal: oryginal drawing entities (minus the
ones the user reserved for deletion, plus the offset polygon and chamfer
notes if requested), no production-grade title block. ReportLab handles
the PDF generation; we re-use ezdxf only to read the source geometry so
this module never duplicates the parser.

Frame modes
-----------

* ``none``    — pure drawing area, no rectangle.
* ``cutflow`` — material-takeoff frame (top header + bottom summary band).
* ``auto``    — alias for ``cutflow`` (Phase 3 default).

Coordinate handling
-------------------

PDF native units are points (1 pt = 1/72 in). DXF units are mm. The
drawing is scaled to fit the available content area uniformly and
centred. Both PDF and DXF use Y-up, so no axis flipping is needed.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from ezdxf import recover
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.units import mm as MM_TO_PT
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas as pdf_canvas

log = logging.getLogger(__name__)

# Page geometry (A4 landscape).
_PAGE_W, _PAGE_H = landscape(A4)  # 842 × 595 pt
_MARGIN_PT = 20.0  # outer page margin (pt)


# ---------------------------------------------------------------------------
# Japanese font registration (best-effort)
# ---------------------------------------------------------------------------


_DEFAULT_FONT = "Helvetica"  # PDF built-in — guaranteed available
_JP_FONT_NAME = "CutflowJP"
_JP_FONT_CACHED: str | None = None


def _resolve_jp_font() -> str:
    """Register a Japanese-capable font if available; fall back gracefully.

    Resolution order (M4):

    1. System Japanese TrueType fonts (MS Gothic, Yu Gothic, Noto CJK,
       Hiragino) — best fidelity when present.
    2. ReportLab's built-in CID font ``HeiseiKakuGo-W5`` (gothic) /
       ``HeiseiMin-W3`` (mincho). These ship with ReportLab and require
       no external font files, so they cover headless Linux / Docker
       containers that don't have CJK system fonts installed.
    3. Helvetica — ASCII only. Callers that hand Japanese strings to
       Helvetica get mojibake; the frame renderer therefore strips
       non-ASCII to ``?`` whenever the resolved font is Helvetica
       (see :func:`_safe_text`).
    """

    global _JP_FONT_CACHED
    if _JP_FONT_CACHED is not None:
        return _JP_FONT_CACHED

    # 1) System TTFs.
    ttf_candidates = [
        ("CutflowJP", r"C:/Windows/Fonts/msgothic.ttc"),
        ("CutflowJP", r"C:/Windows/Fonts/YuGothR.ttc"),
        ("CutflowJP", "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"),
        ("CutflowJP", "/Library/Fonts/Hiragino Sans GB.ttc"),
    ]
    for name, path in ttf_candidates:
        if Path(path).exists():
            try:
                pdfmetrics.registerFont(TTFont(name, path))
                _JP_FONT_CACHED = name
                return name
            except Exception as exc:  # noqa: BLE001 - font formats vary
                log.debug("font registration failed for %s: %s", path, exc)
                continue

    # 2) ReportLab CID fonts (no external file required).
    for cid_name in ("HeiseiKakuGo-W5", "HeiseiMin-W3"):
        try:
            pdfmetrics.registerFont(UnicodeCIDFont(cid_name))
            _JP_FONT_CACHED = cid_name
            return cid_name
        except Exception as exc:  # noqa: BLE001 - older ReportLab builds vary
            log.debug("CID font registration failed for %s: %s", cid_name, exc)
            continue

    # 3) Helvetica fallback (ASCII only — callers must sanitise).
    _JP_FONT_CACHED = _DEFAULT_FONT
    return _DEFAULT_FONT


def _safe_text(text: str) -> str:
    """Replace non-ASCII characters with ``?`` when the resolved font cannot
    render them (Helvetica fallback path). For TTF / CID Japanese fonts the
    text is returned unchanged.
    """

    if _JP_FONT_CACHED is None or _JP_FONT_CACHED != _DEFAULT_FONT:
        return text
    try:
        text.encode("ascii")
        return text
    except UnicodeEncodeError:
        return "".join(ch if ord(ch) < 128 else "?" for ch in text)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def export_pdf(
    source: Path | str,
    dest: Path | str,
    *,
    deleted_ids: set[str] | None = None,
    extra_polylines: Iterable[dict] | None = None,
    chamfer_annotations: Iterable[dict] | None = None,
    title: str | None = None,
    material: str | None = None,
    perimeter_mm: float | None = None,
    plate_size: str | None = None,
    frame: str = "auto",
) -> Path:
    """Render the source DXF to ``dest`` as an A4-landscape PDF.

    Parameters mirror the DXF-writer flags so the routers can pass through
    flags in one place. ``frame`` ∈ {``auto``, ``none``, ``cutflow``}; both
    ``auto`` and ``cutflow`` produce the material-takeoff frame.
    """

    deleted_ids = set(deleted_ids or [])
    extra_polylines = list(extra_polylines or [])
    chamfer_annotations = list(chamfer_annotations or [])

    doc, _auditor = recover.readfile(str(source))
    msp = doc.modelspace()

    # Collect renderable primitives + bounding box.
    primitives: list[dict[str, Any]] = []
    for idx, e in enumerate(msp):
        eid = f"e{idx:05d}"
        if eid in deleted_ids:
            continue
        item = _entity_to_primitive(e)
        if item is not None:
            primitives.append(item)

    # Append offset polylines as primitives (cyan, dashed in the PDF).
    for poly in extra_polylines:
        verts = poly.get("vertices") or []
        if len(verts) < 3:
            continue
        layer = str(poly.get("layer") or "CUTFLOW_OFFSET")
        primitives.append(
            {
                "kind": "polyline",
                "pts": [(float(v[0]), float(v[1])) for v in verts],
                "closed": bool(poly.get("closed", True)),
                "layer": layer,
                "dashed": True,
            }
        )

    # Chamfer annotations — rendered as labelled dots so the operator can
    # cross-reference them with the DXF notes.
    for ann in chamfer_annotations:
        anchor = ann.get("anchor")
        text = str(ann.get("text") or "")
        if not anchor or len(anchor) < 2 or not text:
            continue
        primitives.append(
            {
                "kind": "label",
                "x": float(anchor[0]),
                "y": float(anchor[1]),
                "text": text,
                "color": "chamfer",
            }
        )

    if not primitives:
        # Degenerate input — fall back to a 0..100 box so we still emit
        # a valid PDF the operator can use as a placeholder.
        primitives.append(
            {"kind": "polyline", "pts": [(0.0, 0.0), (100.0, 0.0),
                                          (100.0, 100.0), (0.0, 100.0)],
             "closed": True, "layer": "GUIDE", "dashed": True}
        )

    bbox = _primitives_bbox(primitives)
    use_frame = frame in ("auto", "cutflow")

    # H5: create the destination directory *before* opening the canvas —
    # ReportLab opens the file for write inside `Canvas(...)`, so a missing
    # parent dir raises FileNotFoundError mid-render. Mkdir must precede.
    Path(dest).parent.mkdir(parents=True, exist_ok=True)

    c = pdf_canvas.Canvas(str(dest), pagesize=landscape(A4))
    _draw_page(
        c,
        primitives,
        bbox,
        with_frame=use_frame,
        title=title or Path(str(source)).stem,
        material=material,
        perimeter_mm=perimeter_mm,
        plate_size=plate_size,
    )
    c.showPage()
    c.save()

    log.info(
        "rendered PDF %s (%d primitives, frame=%s)", dest, len(primitives), frame
    )
    return Path(dest)


# ---------------------------------------------------------------------------
# Page rendering
# ---------------------------------------------------------------------------


def _draw_page(
    c: pdf_canvas.Canvas,
    primitives: list[dict[str, Any]],
    bbox: tuple[float, float, float, float],
    *,
    with_frame: bool,
    title: str,
    material: str | None,
    perimeter_mm: float | None,
    plate_size: str | None,
) -> None:
    font = _resolve_jp_font()

    header_h = 36.0 if with_frame else 0.0
    footer_h = 26.0 if with_frame else 0.0

    drawing_left = _MARGIN_PT
    drawing_right = _PAGE_W - _MARGIN_PT
    drawing_top = _PAGE_H - _MARGIN_PT - header_h
    drawing_bottom = _MARGIN_PT + footer_h

    if with_frame:
        _draw_frame(
            c,
            font,
            title=title,
            material=material,
            perimeter_mm=perimeter_mm,
            plate_size=plate_size,
            header_h=header_h,
            footer_h=footer_h,
        )

    # Fit the drawing into the available content rectangle.
    xmin, ymin, xmax, ymax = bbox
    w = max(1e-6, xmax - xmin)
    h = max(1e-6, ymax - ymin)
    avail_w = drawing_right - drawing_left
    avail_h = drawing_top - drawing_bottom
    # Convert mm → pt (DXF coords are mm by convention here).
    scale_w = avail_w / (w * MM_TO_PT)
    scale_h = avail_h / (h * MM_TO_PT)
    scale = min(scale_w, scale_h) * 0.96  # leave a small visual gutter
    # Center inside the drawing rectangle.
    drawn_w = w * MM_TO_PT * scale
    drawn_h = h * MM_TO_PT * scale
    offset_x = drawing_left + (avail_w - drawn_w) / 2.0
    offset_y = drawing_bottom + (avail_h - drawn_h) / 2.0

    def tx(x_mm: float) -> float:
        return offset_x + (x_mm - xmin) * MM_TO_PT * scale

    def ty(y_mm: float) -> float:
        return offset_y + (y_mm - ymin) * MM_TO_PT * scale

    c.setLineCap(1)
    c.setLineJoin(1)

    for prim in primitives:
        kind = prim.get("kind")
        if kind == "line":
            _stroke_color(c, prim)
            c.line(tx(prim["x1"]), ty(prim["y1"]), tx(prim["x2"]), ty(prim["y2"]))
        elif kind == "circle":
            _stroke_color(c, prim)
            c.circle(tx(prim["cx"]), ty(prim["cy"]), prim["r"] * MM_TO_PT * scale,
                     stroke=1, fill=0)
        elif kind == "arc":
            _stroke_color(c, prim)
            _draw_arc(c, prim, tx, ty, scale)
        elif kind == "polyline":
            _stroke_color(c, prim)
            pts = prim.get("pts") or []
            if prim.get("dashed"):
                c.setDash(4, 3)
            path = c.beginPath()
            first = True
            for px, py in pts:
                if first:
                    path.moveTo(tx(px), ty(py))
                    first = False
                else:
                    path.lineTo(tx(px), ty(py))
            if prim.get("closed") and pts:
                path.close()
            c.drawPath(path, stroke=1, fill=0)
            if prim.get("dashed"):
                c.setDash()  # reset
        elif kind == "label":
            color = prim.get("color")
            if color == "chamfer":
                c.setFillColorRGB(0.66, 0.55, 0.98)
                c.setStrokeColorRGB(0.66, 0.55, 0.98)
            else:
                c.setFillColorRGB(0.1, 0.1, 0.1)
                c.setStrokeColorRGB(0.1, 0.1, 0.1)
            cx = tx(prim["x"])
            cy = ty(prim["y"])
            c.circle(cx, cy, 2.0, stroke=0, fill=1)
            c.setFont(font, 8)
            c.drawString(cx + 4, cy + 4, prim["text"])
            c.setFillColorRGB(0, 0, 0)
            c.setStrokeColorRGB(0, 0, 0)


def _stroke_color(c: pdf_canvas.Canvas, prim: dict[str, Any]) -> None:
    layer = str(prim.get("layer") or "")
    if layer == "CUTFLOW_OFFSET":
        c.setStrokeColorRGB(0.30, 0.81, 0.88)  # cyan
        c.setLineWidth(0.6)
    elif layer == "CUTFLOW_CHAMFER":
        c.setStrokeColorRGB(0.66, 0.55, 0.98)  # purple
        c.setLineWidth(0.6)
    elif layer == "GUIDE":
        c.setStrokeColorRGB(0.55, 0.55, 0.55)
        c.setLineWidth(0.4)
    else:
        c.setStrokeColorRGB(0.05, 0.05, 0.05)
        c.setLineWidth(0.5)


def _draw_arc(
    c: pdf_canvas.Canvas,
    prim: dict[str, Any],
    tx,
    ty,
    scale: float,
) -> None:
    """Approximate the arc with a polyline (PDF arc primitives don't
    accept start/end angles directly without bounding-box trickery).
    """

    cx = float(prim["cx"]); cy = float(prim["cy"]); r = float(prim["r"])
    sa = float(prim["sa"]); ea = float(prim["ea"])
    if ea < sa:
        ea += 360.0
    sweep = ea - sa
    steps = max(8, int(math.ceil(sweep / 2.0)) + 1)
    path = c.beginPath()
    for i in range(steps):
        t = math.radians(sa + sweep * (i / (steps - 1)))
        x = cx + r * math.cos(t)
        y = cy + r * math.sin(t)
        if i == 0:
            path.moveTo(tx(x), ty(y))
        else:
            path.lineTo(tx(x), ty(y))
    c.drawPath(path, stroke=1, fill=0)


# ---------------------------------------------------------------------------
# Frame (material-takeoff style)
# ---------------------------------------------------------------------------


def _draw_frame(
    c: pdf_canvas.Canvas,
    font: str,
    *,
    title: str,
    material: str | None,
    perimeter_mm: float | None,
    plate_size: str | None,
    header_h: float,
    footer_h: float,
) -> None:
    """Render the material-取り specific frame: header strip + footer strip."""

    # Outer page rectangle.
    c.setStrokeColorRGB(0.20, 0.20, 0.20)
    c.setLineWidth(0.8)
    c.rect(
        _MARGIN_PT,
        _MARGIN_PT,
        _PAGE_W - 2 * _MARGIN_PT,
        _PAGE_H - 2 * _MARGIN_PT,
        stroke=1,
        fill=0,
    )

    # Header band.
    c.rect(
        _MARGIN_PT,
        _PAGE_H - _MARGIN_PT - header_h,
        _PAGE_W - 2 * _MARGIN_PT,
        header_h,
        stroke=1,
        fill=0,
    )
    today = datetime.now().strftime("%Y-%m-%d")

    c.setFillColorRGB(0.05, 0.05, 0.05)
    c.setFont(font, 11)
    c.drawString(
        _MARGIN_PT + 10,
        _PAGE_H - _MARGIN_PT - 16,
        _safe_text(f"ファイル名: {title}"),
    )
    c.setFont(font, 9)
    parts: list[str] = []
    if material:
        parts.append(f"材質: {material}")
    parts.append(f"加工日: {today}")
    c.drawString(
        _MARGIN_PT + 10,
        _PAGE_H - _MARGIN_PT - 30,
        _safe_text("  ".join(parts)),
    )

    # Footer band.
    c.rect(
        _MARGIN_PT,
        _MARGIN_PT,
        _PAGE_W - 2 * _MARGIN_PT,
        footer_h,
        stroke=1,
        fill=0,
    )
    summary_bits: list[str] = []
    if perimeter_mm is not None:
        summary_bits.append(f"外周長: {perimeter_mm:,.0f}mm")
    if plate_size:
        summary_bits.append(f"板取り: {plate_size}")
    summary_bits.append("CutFlow•CAD")
    c.setFont(font, 9)
    c.drawString(
        _MARGIN_PT + 10,
        _MARGIN_PT + 9,
        _safe_text("    ".join(summary_bits)),
    )


# ---------------------------------------------------------------------------
# Entity → primitive conversion
# ---------------------------------------------------------------------------


def _entity_to_primitive(ent) -> dict[str, Any] | None:
    """Project an ezdxf entity onto the minimal primitive set the PDF needs.

    Returns ``None`` for entities we don't render (TEXT/MTEXT/INSERT etc.)
    — the operator already sees them on screen; printing them adds noise.
    """

    t = ent.dxftype()
    layer = str(getattr(ent.dxf, "layer", ""))

    try:
        if t == "LINE":
            return {
                "kind": "line",
                "x1": float(ent.dxf.start.x),
                "y1": float(ent.dxf.start.y),
                "x2": float(ent.dxf.end.x),
                "y2": float(ent.dxf.end.y),
                "layer": layer,
            }
        if t == "CIRCLE":
            return {
                "kind": "circle",
                "cx": float(ent.dxf.center.x),
                "cy": float(ent.dxf.center.y),
                "r": float(ent.dxf.radius),
                "layer": layer,
            }
        if t == "ARC":
            return {
                "kind": "arc",
                "cx": float(ent.dxf.center.x),
                "cy": float(ent.dxf.center.y),
                "r": float(ent.dxf.radius),
                "sa": float(ent.dxf.start_angle),
                "ea": float(ent.dxf.end_angle),
                "layer": layer,
            }
        if t == "LWPOLYLINE":
            pts = [(float(p[0]), float(p[1])) for p in ent.get_points("xy")]
            return {
                "kind": "polyline",
                "pts": pts,
                "closed": bool(ent.closed),
                "layer": layer,
            }
        if t == "POLYLINE":
            pts = [(float(v.dxf.location.x), float(v.dxf.location.y)) for v in ent.vertices]
            return {
                "kind": "polyline",
                "pts": pts,
                "closed": bool(ent.is_closed),
                "layer": layer,
            }
    except Exception as exc:  # noqa: BLE001 — bad entity should not abort render
        log.debug("PDF primitive conversion skipped %s: %s", t, exc)
    return None


def _primitives_bbox(
    primitives: list[dict[str, Any]],
) -> tuple[float, float, float, float]:
    xs: list[float] = []
    ys: list[float] = []
    for p in primitives:
        kind = p.get("kind")
        if kind == "line":
            xs.extend([p["x1"], p["x2"]]); ys.extend([p["y1"], p["y2"]])
        elif kind == "circle":
            xs.extend([p["cx"] - p["r"], p["cx"] + p["r"]])
            ys.extend([p["cy"] - p["r"], p["cy"] + p["r"]])
        elif kind == "arc":
            xs.extend([p["cx"] - p["r"], p["cx"] + p["r"]])
            ys.extend([p["cy"] - p["r"], p["cy"] + p["r"]])
        elif kind == "polyline":
            for x, y in p.get("pts") or []:
                xs.append(x); ys.append(y)
        elif kind == "label":
            xs.append(p["x"]); ys.append(p["y"])
    if not xs:
        return 0.0, 0.0, 1.0, 1.0
    return min(xs), min(ys), max(xs), max(ys)
