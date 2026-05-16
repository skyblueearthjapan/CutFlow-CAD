"""High-fidelity DXF→SVG rendering via ezdxf's Drawing add-on (Phase 6).

The Phase 1–5 path converts each entity into a small JSON dict that the
frontend then renders as SVG primitives. That gives us full control over
delete-mode toggles and overlay editing but loses CAD-software parity for
dimensions, hatches and block expansion.

This module is the **background layer** counterpart: ezdxf's
``addons.drawing`` pipeline produces a complete SVG string for the entire
modelspace, which the frontend draws beneath its own editable overlay.

The two paths share the same parsed ``Drawing`` via ``load_document``'s
implicit ezdxf cache (each call re-reads from disk but the parsed payload
itself is reused through ``parse_file``'s LRU). Deletions are honoured by
mapping the ``e{idx:05d}`` entity IDs back to DXF handles and passing a
``filter_func`` to ``Frontend.draw_layout``.

ezdxf 1.4 API (verified live):
    >>> backend = SVGBackend()
    >>> Frontend(RenderContext(doc), backend, config=cfg).draw_layout(msp)
    >>> svg = backend.get_string(Page(0, 0, Units.mm))

CRITICAL viewBox alignment (Phase 6 fix):
    ezdxf's SVGBackend emits an SVG whose inner viewBox is the *normalized*
    coordinate space (default ``output_coordinate_space=1_000_000``) with
    Y already flipped. The outer ``width="<w>mm"`` / ``height="<h>mm"``
    attributes are correct in millimetres but the inner ``<path d=...>``
    coordinates are in the 0..1_000_000 normalized space.

    Our frontend strips the outer ``<svg>`` wrapper and re-hosts the
    inner content under the canvas viewBox (in DXF mm coords). That mismatch
    made the background invisible (paths far outside the mm viewBox).

    Fix: post-process the ezdxf SVG to wrap the inner ``<defs>`` + ``<g>``
    content in a ``<g transform="translate(min_x min_y) scale(s s)">``
    that maps the normalized coordinate space back to DXF mm space.
    ``s = w_mm / output_coordinate_space``. No Y-flip is needed: ezdxf
    already inverts Y so normalized y=0 → DXF y=max_y, which is exactly
    what the unflipped background layer wants in its DXF-mm viewBox.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from ezdxf import bbox as ezdxf_bbox
from ezdxf.addons.drawing import Frontend, RenderContext
from ezdxf.addons.drawing.config import (
    BackgroundPolicy,
    ColorPolicy,
    Configuration,
    LineweightPolicy,
)
from ezdxf.addons.drawing.layout import Page, Settings, Units
from ezdxf.addons.drawing.svg import SVGBackend

from .dxf_parser import load_document

log = logging.getLogger(__name__)

# SVG sanitization: tags / attribute patterns we strip before returning the
# string to the client. The backend never emits these, but the response is
# injected into the DOM via ``v-html`` so we belt-and-suspender against a
# malicious DXF that somehow ferries script content through ezdxf (M1).
_SCRIPT_RE = re.compile(r"<script[^>]*>.*?</script>", re.DOTALL | re.IGNORECASE)
_FOREIGN_OBJECT_RE = re.compile(
    r"<foreignObject[^>]*>.*?</foreignObject>", re.DOTALL | re.IGNORECASE
)
_ON_ATTR_DOUBLE = re.compile(r'\s+on\w+\s*=\s*"[^"]*"', re.IGNORECASE)
_ON_ATTR_SINGLE = re.compile(r"\s+on\w+\s*=\s*'[^']*'", re.IGNORECASE)
_ON_ATTR_BARE = re.compile(r"\s+on\w+\s*=\s*[^\s>]+", re.IGNORECASE)


def sanitize_svg(svg: str) -> str:
    """Strip ``<script>`` / ``<foreignObject>`` / ``on*=`` from an SVG string.

    Defensive cleanup before we inject the markup into the DOM via
    ``v-html``. ezdxf never emits these constructs, but the input DXF is
    arbitrary so we cannot rely on the backend alone to keep us safe.
    """

    if not svg:
        return svg
    s = _SCRIPT_RE.sub("", svg)
    s = _FOREIGN_OBJECT_RE.sub("", s)
    s = _ON_ATTR_DOUBLE.sub("", s)
    s = _ON_ATTR_SINGLE.sub("", s)
    s = _ON_ATTR_BARE.sub("", s)
    return s


def entity_id_to_handle_map(path: str | Path) -> dict[str, str]:
    """Return the ``e{idx:05d} → handle`` mapping for the modelspace.

    ``dxf_parser.parse_file`` assigns IDs by ``enumerate(msp)`` order, so
    re-enumerating produces the same mapping. Kept as a free function so
    routers can resolve a small set of IDs without paying for a render.
    """

    doc = load_document(path)
    msp = doc.modelspace()
    out: dict[str, str] = {}
    for idx, e in enumerate(msp):
        out[f"e{idx:05d}"] = str(e.dxf.handle)
    return out


# Default OCS used by ezdxf's SVGBackend when ``Settings()`` is not overridden.
# We pin to the default and pass an explicit Settings() so a future ezdxf
# release that flips the default still produces the value we transform by.
_OUTPUT_COORD_SPACE = float(Settings().output_coordinate_space)

# Pattern matching the inner SVG content we need to wrap with a DXF-mm
# transform. ezdxf emits, in order: an optional ``<defs>``, a top-level
# ``<rect>`` (the canvas background), and a top-level ``<g>`` containing
# the drawn paths. We capture the ``<svg ...>`` opening tag so we can
# re-emit it unchanged, then wrap *everything between the tags* in our
# transform group.
_SVG_ROOT_RE = re.compile(
    r"(<svg\b[^>]*viewBox=\"([^\"]+)\"[^>]*>)(.*?)(</svg>)",
    re.DOTALL | re.IGNORECASE,
)


def _rewrite_inner_to_dxf_mm(
    svg: str,
    min_x: float,
    min_y: float,
    w_mm: float,
    h_mm: float,
) -> str:
    """Rewrite the ezdxf SVG so inner paths render in DXF mm coordinates.

    Wraps the inner content in
    ``<g transform="translate(min_x, min_y) scale(s, s)">`` and rewrites
    the outer ``viewBox`` to ``"<min_x> <min_y> <w_mm> <h_mm>"`` so the
    standalone SVG keeps rendering correctly *and* the frontend (which
    strips the outer tag and re-hosts the children under the canvas
    viewBox) gets paths in DXF mm space.

    The X-scale and Y-scale are equal because ezdxf preserves the aspect
    ratio (inner viewBox is ``0 0 OCS OCS*h/w``).
    """

    if w_mm <= 0 or h_mm <= 0:
        return svg

    s = w_mm / _OUTPUT_COORD_SPACE
    new_viewbox = f"{min_x} {min_y} {w_mm} {h_mm}"

    def _wrap(match: re.Match[str]) -> str:
        open_tag, _old_vb, inner, close_tag = match.group(1, 2, 3, 4)
        # Rewrite the outer viewBox on the open tag — keep every other
        # attribute (width/height in mm, xmlns, etc.) untouched.
        new_open = re.sub(
            r'viewBox="[^"]+"',
            f'viewBox="{new_viewbox}"',
            open_tag,
            count=1,
        )
        wrapped = (
            f'<g transform="translate({min_x} {min_y}) scale({s} {s})">'
            f"{inner}"
            f"</g>"
        )
        return f"{new_open}{wrapped}{close_tag}"

    new_svg, n = _SVG_ROOT_RE.subn(_wrap, svg, count=1)
    if n == 0:
        # Defensive: if the regex didn't match we fall back to the
        # original SVG rather than break the response. The viewBox
        # mismatch is preferable to a 500.
        log.warning("svg viewBox rewrite skipped (root regex did not match)")
        return svg
    return new_svg


def render_dxf_to_svg(
    path: str | Path,
    *,
    dark_theme: bool = True,
    exclude_handles: set[str] | None = None,
    exclude_entity_ids: set[str] | None = None,
    edits: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Render ``path`` to an SVG string + bounding-box metadata.

    Args:
        path: filesystem path to the DXF file (re-read each call so
            deletion edits in the source are visible).
        dark_theme: when ``True``, render with the
            ``MONOCHROME_DARK_BG`` colour policy and an *off* background
            so the SVG can be layered atop a dark canvas. ``False`` keeps
            the DXF's native colour palette and a paperspace background.
        exclude_handles: DXF handles (the hex strings ezdxf uses
            internally) to filter from rendering. Use this when you
            already know the handles. Normalized to uppercase to match
            ezdxf's handle casing convention (M3).
        exclude_entity_ids: ``e{idx:05d}`` entity IDs in the Phase 1–5
            convention. Resolved to DXF handles by re-enumerating the
            modelspace via ``entity_id_to_handle_map``, then merged with
            ``exclude_handles``.
        edits: optional list of vertex edits to apply to the live ezdxf
            modelspace *before* rendering — used so the operator sees
            their line-edit translations on the background layer (HIGH-2).
            Each entry is ``{"entity_id", "vertex_index", "new_position"}``.
            Skipped silently when empty / None.

    Returns:
        ``{"svg": <xml string>, "bbox": {min_x, min_y, max_x, max_y},
           "width": float, "height": float}``.
        ``width`` / ``height`` are the SVG viewport extents in millimetres
        (the DXF native unit for this project), 0 when the drawing is
        empty.
    """

    doc = load_document(path)
    msp = doc.modelspace()

    # Resolve entity-ID exclusions to DXF handles via the shared helper so
    # the eid → handle mapping logic lives in exactly one place (M2). All
    # handles are normalized to uppercase so the filter matches ezdxf's
    # internal casing regardless of how the caller supplied them (M3).
    handles: set[str] = {h.upper() for h in (exclude_handles or set())}
    if exclude_entity_ids:
        eid_lookup = {f"e{idx:05d}": str(e.dxf.handle) for idx, e in enumerate(msp)}
        for eid in exclude_entity_ids:
            h = eid_lookup.get(eid)
            if h:
                handles.add(h.upper())

    # Apply Phase 4 vertex edits in-place on the live document so the
    # background SVG reflects the operator's translations. ``load_document``
    # returns a fresh ``Drawing`` per call (no caching), so this mutation
    # never leaks to other callers (HIGH-2).
    if edits:
        try:
            from .edits import apply_edits_to_msp

            apply_edits_to_msp(msp, edits)
        except Exception as exc:  # noqa: BLE001 — bad edit must not abort render
            log.warning("apply_edits_to_msp failed during render: %s", exc)

    backend = SVGBackend()
    cfg = Configuration(
        background_policy=BackgroundPolicy.OFF if dark_theme else BackgroundPolicy.PAPERSPACE,
        color_policy=ColorPolicy.MONOCHROME_DARK_BG if dark_theme else ColorPolicy.COLOR,
        lineweight_policy=LineweightPolicy.RELATIVE,
    )
    ctx = RenderContext(doc)

    filter_func = None
    if handles:
        # filter_func returns False to skip an entity. Using a captured set
        # avoids per-entity tuple/list construction inside the hot loop.
        def filter_func(e):  # noqa: E306 - tight inline closure
            h = getattr(e.dxf, "handle", None)
            return (str(h).upper() if h is not None else None) not in handles

    try:
        Frontend(ctx, backend, config=cfg).draw_layout(
            msp, finalize=True, filter_func=filter_func
        )
    except Exception as exc:  # noqa: BLE001 - ezdxf drawing can fail on broken DXFs
        # FontNotFoundError is common in slim containers when fonts haven't
        # been installed yet. Retry once with text policy that converts
        # MTEXT to placeholder rectangles, so the user still sees geometry.
        from ezdxf.fonts.font_manager import FontNotFoundError
        if isinstance(exc, FontNotFoundError):
            log.warning("ezdxf has no fonts — retrying render with MTEXT placeholders")
            try:
                from ezdxf.addons.drawing.properties import MTextPolicy
                backend = SVGBackend()
                cfg2 = Configuration(
                    background_policy=BackgroundPolicy.OFF if dark_theme else BackgroundPolicy.PAPERSPACE,
                    color_policy=ColorPolicy.MONOCHROME_DARK_BG if dark_theme else ColorPolicy.COLOR,
                    lineweight_policy=LineweightPolicy.RELATIVE,
                    mtext_policy=MTextPolicy.RECT,
                )
                Frontend(ctx, backend, config=cfg2).draw_layout(
                    msp, finalize=True, filter_func=filter_func
                )
            except Exception as exc2:  # noqa: BLE001
                log.exception("ezdxf SVG render failed even with MTEXT placeholders for %s", path)
                raise RuntimeError(f"svg render failed: {exc2}") from exc2
        else:
            log.exception("ezdxf SVG render failed for %s", path)
            raise RuntimeError(f"svg render failed: {exc}") from exc

    # ``Page(0, 0)`` triggers auto-detection from the content bounding box,
    # which is exactly what we want for an interactive canvas overlay.
    page = Page(0, 0, Units.mm)
    svg_str = backend.get_string(page)

    bbox_dict, width_mm, height_mm = _compute_bbox_and_size(msp, handles)

    # Rewrite the inner content so paths render in DXF mm coordinates
    # rather than ezdxf's 0..1_000_000 normalized space (CRITICAL fix).
    svg_str = _rewrite_inner_to_dxf_mm(
        svg_str,
        bbox_dict["min_x"],
        bbox_dict["min_y"],
        width_mm,
        height_mm,
    )

    # Defensive sanitization against arbitrary DXF content (M1).
    svg_str = sanitize_svg(svg_str)

    return {
        "svg": svg_str,
        "bbox": bbox_dict,
        "width": width_mm,
        "height": height_mm,
    }


def _compute_bbox_and_size(
    msp: Any, exclude_handles: set[str]
) -> tuple[dict[str, float], float, float]:
    """Compute the bbox + millimetre dimensions of the rendered region."""

    try:
        if exclude_handles:
            entities = [
                e
                for e in msp
                if str(getattr(e.dxf, "handle", "")).upper() not in exclude_handles
            ]
            ext = ezdxf_bbox.extents(entities) if entities else None
        else:
            ext = ezdxf_bbox.extents(msp)
        if ext is not None and ext.has_data:
            mn, mx = ext.extmin, ext.extmax
            min_x, min_y = float(mn.x), float(mn.y)
            max_x, max_y = float(mx.x), float(mx.y)
            return (
                {"min_x": min_x, "min_y": min_y, "max_x": max_x, "max_y": max_y},
                max(max_x - min_x, 0.0),
                max(max_y - min_y, 0.0),
            )
    except Exception as exc:  # noqa: BLE001 - bbox can throw on degenerate geometry
        log.debug("bbox extents failed during svg render: %s", exc)
    return ({"min_x": 0.0, "min_y": 0.0, "max_x": 0.0, "max_y": 0.0}, 0.0, 0.0)
