"""Phase 6 M1 — defensive SVG sanitization.

The Phase 6 background SVG is injected into the DOM via Vue's ``v-html``.
ezdxf never emits ``<script>`` / ``<foreignObject>`` / ``on*=`` event
handlers, but the input DXF is operator-supplied so we can't trust the
backend output blindly. ``services.svg_render.sanitize_svg`` strips those
constructs before we ship the payload — this test exercises it
directly with synthetic inputs so the cleanup is verified independently
of ezdxf's exact output.
"""

from __future__ import annotations

from services.svg_render import sanitize_svg


def test_sanitize_removes_script_block() -> None:
    raw = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10">'
        "<script>alert('boom')</script>"
        '<rect x="0" y="0" width="10" height="10" />'
        "</svg>"
    )
    out = sanitize_svg(raw)
    assert "<script" not in out
    assert "alert(" not in out
    assert "<rect" in out  # benign content survives


def test_sanitize_removes_event_handlers_quoted_and_bare() -> None:
    raw = (
        '<svg viewBox="0 0 10 10">'
        '<rect x="0" y="0" width="10" height="10" onclick="boom()" />'
        "<g onload='evil()'>"
        "<path d=\"M 0 0\" onmouseover=run() />"
        "</g></svg>"
    )
    out = sanitize_svg(raw)
    assert "onclick" not in out.lower()
    assert "onload" not in out.lower()
    assert "onmouseover" not in out.lower()
    # The benign geometry attributes are preserved.
    assert 'd="M 0 0"' in out
    assert 'width="10"' in out


def test_sanitize_removes_foreign_object() -> None:
    raw = (
        '<svg viewBox="0 0 10 10">'
        '<foreignObject x="0" y="0" width="10" height="10">'
        "<iframe src=\"javascript:alert(1)\"></iframe>"
        "</foreignObject>"
        '<rect x="0" y="0" width="10" height="10" />'
        "</svg>"
    )
    out = sanitize_svg(raw)
    assert "foreignObject" not in out
    assert "iframe" not in out  # purged together with the parent element
    assert "<rect" in out


def test_sanitize_is_noop_on_safe_svg() -> None:
    safe = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 50">'
        '<g><path d="M 0 0 L 100 50" stroke="white" /></g></svg>'
    )
    assert sanitize_svg(safe) == safe


def test_sanitize_empty_input() -> None:
    assert sanitize_svg("") == ""
