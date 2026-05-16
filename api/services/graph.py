"""Topology helpers for outer-loop detection.

Phase 2 needs to turn a heterogeneous bag of DXF primitives (LINE / ARC /
LWPOLYLINE / POLYLINE / CIRCLE) into a planar graph whose faces we can
enumerate. The pipeline is:

1. ``endpoints_of(geom)`` returns the (start, end) pair for each edge entity.
   A CIRCLE is a self-loop (start == end) and is treated specially.
2. ``build_graph(items, tol)`` snaps endpoints with a K-D tree, assigns
   node IDs and returns an undirected multigraph keyed on entity-ids.
3. ``find_closed_loops(graph)`` enumerates the (small) set of simple cycles
   that bound a face of the planar embedding.

The whole module is intentionally tolerant: malformed entities are skipped
rather than raised, because real-world DXFs from a workshop are noisy.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable

import networkx as nx
from scipy.spatial import cKDTree
from shapely.geometry import LineString, Point
from shapely.ops import polygonize_full


# Endpoint-merge tolerance. The spec asks for 0.01 mm; some sample DXFs
# carry geometry drawn against a 0.1 mm snap grid, so a slightly looser
# 0.05 mm gives meaningful improvement on the noisier samples without
# fusing legitimately distinct vertices.
DEFAULT_TOL_MM = 0.05


# ---------------------------------------------------------------------------
# Endpoint extraction
# ---------------------------------------------------------------------------


def _arc_endpoints(geom: dict[str, Any]) -> tuple[tuple[float, float], tuple[float, float]] | None:
    cx = geom.get("cx")
    cy = geom.get("cy")
    r = geom.get("r")
    sa = geom.get("start_angle")
    ea = geom.get("end_angle")
    if None in (cx, cy, r, sa, ea):
        return None
    sa_r = math.radians(float(sa))
    ea_r = math.radians(float(ea))
    p0 = (float(cx) + float(r) * math.cos(sa_r), float(cy) + float(r) * math.sin(sa_r))
    p1 = (float(cx) + float(r) * math.cos(ea_r), float(cy) + float(r) * math.sin(ea_r))
    return p0, p1


def _poly_endpoints(geom: dict[str, Any]) -> tuple[tuple[float, float], tuple[float, float]] | None:
    verts = geom.get("vertices") or []
    if len(verts) < 2:
        return None
    a = (float(verts[0][0]), float(verts[0][1]))
    b = (float(verts[-1][0]), float(verts[-1][1]))
    return a, b


def endpoints_of(dxftype: str, geom: dict[str, Any]) -> tuple[tuple[float, float], tuple[float, float]] | None:
    """Return ``(start, end)`` for an entity, or ``None`` if not edge-like."""

    if dxftype == "LINE":
        x1 = geom.get("x1")
        y1 = geom.get("y1")
        x2 = geom.get("x2")
        y2 = geom.get("y2")
        if None in (x1, y1, x2, y2):
            return None
        return (float(x1), float(y1)), (float(x2), float(y2))

    if dxftype == "ARC":
        return _arc_endpoints(geom)

    if dxftype in ("LWPOLYLINE", "POLYLINE"):
        # A closed polyline has identical start/end after snapping; report it
        # the same as an open polyline with matching endpoints — the cycle
        # enumeration will pick it up as a self-loop or simple two-node cycle.
        return _poly_endpoints(geom)

    if dxftype == "CIRCLE":
        cx = geom.get("cx")
        cy = geom.get("cy")
        r = geom.get("r")
        if None in (cx, cy, r):
            return None
        # Self-loop; both "endpoints" coincide on the right-most point.
        p = (float(cx) + float(r), float(cy))
        return p, p

    return None


# ---------------------------------------------------------------------------
# Graph construction
# ---------------------------------------------------------------------------


@dataclass
class EdgeInfo:
    """Per-edge metadata stored alongside the networkx edge."""

    entity_id: str
    dxftype: str
    geom: dict[str, Any]
    node_a: int
    node_b: int
    length: float = 0.0
    # Polyline sample for area / point-in-polygon / hole-counting. Always
    # in geometric order from ``node_a`` to ``node_b`` (CIRCLE samples a
    # full CCW loop).
    samples: list[tuple[float, float]] = field(default_factory=list)


@dataclass
class TopoGraph:
    """Snapped graph view of edge-like DXF entities."""

    nodes: list[tuple[float, float]]  # coordinate per node id
    edges: dict[str, EdgeInfo]  # by entity_id
    graph: nx.MultiGraph  # undirected; edges keyed on entity_id

    def coord(self, node: int) -> tuple[float, float]:
        return self.nodes[node]


def _sample_arc(
    cx: float, cy: float, r: float, sa: float, ea: float, deg_step: float = 2.0
) -> list[tuple[float, float]]:
    # Sweep CCW from sa → ea (DXF convention). Handle wrap-around.
    if ea < sa:
        ea += 360.0
    sweep = ea - sa
    n = max(2, int(math.ceil(sweep / max(0.5, deg_step))) + 1)
    pts: list[tuple[float, float]] = []
    for i in range(n):
        t = sa + sweep * i / (n - 1)
        rad = math.radians(t)
        pts.append((cx + r * math.cos(rad), cy + r * math.sin(rad)))
    return pts


def _sample_polyline(verts: list[list[float]], closed: bool, deg_step: float = 2.0) -> list[tuple[float, float]]:
    """Walk a LWPOLYLINE/POLYLINE turning each bulged segment into points.

    ``verts[i]`` may be ``[x, y]`` or ``[x, y, bulge]``. bulge = tan(angle/4)
    on the arc that connects vertex i → i+1 (signed in DXF space).
    """

    pts: list[tuple[float, float]] = []
    n = len(verts)
    if n == 0:
        return pts
    pts.append((float(verts[0][0]), float(verts[0][1])))

    last = n if closed else n - 1
    for i in range(last):
        a = verts[i]
        b = verts[(i + 1) % n]
        x1, y1 = float(a[0]), float(a[1])
        x2, y2 = float(b[0]), float(b[1])
        bulge = float(a[2]) if len(a) >= 3 else 0.0

        if abs(bulge) < 1e-9:
            pts.append((x2, y2))
            continue

        # Convert bulge to arc parameters. theta = 4*atan(bulge).
        theta = 4.0 * math.atan(bulge)
        chord = math.hypot(x2 - x1, y2 - y1)
        if chord < 1e-9:
            pts.append((x2, y2))
            continue
        radius = abs(chord / (2.0 * math.sin(theta / 2.0)))
        # Sagitta direction: perpendicular to chord, flipped by bulge sign.
        mx, my = (x1 + x2) / 2.0, (y1 + y2) / 2.0
        # Unit normal (rotate chord 90° CCW).
        dx, dy = (x2 - x1) / chord, (y2 - y1) / chord
        nx_, ny_ = -dy, dx
        # Distance from chord midpoint to centre.
        h = math.sqrt(max(0.0, radius * radius - (chord / 2.0) ** 2))
        sign = 1.0 if bulge > 0 else -1.0
        cx = mx + sign * h * nx_
        cy = my + sign * h * ny_

        sa = math.atan2(y1 - cy, x1 - cx)
        ea = math.atan2(y2 - cy, x2 - cx)
        # Force the sweep direction matching the bulge sign.
        if bulge > 0:  # CCW
            if ea < sa:
                ea += 2.0 * math.pi
        else:  # CW
            if ea > sa:
                ea -= 2.0 * math.pi

        sweep = abs(ea - sa)
        steps = max(2, int(math.ceil(math.degrees(sweep) / max(0.5, deg_step))) + 1)
        # Emit interior samples only (k=1..steps-2) then append the exact
        # endpoint once — the previous version appended ``(x2,y2)`` AND a
        # near-identical interpolated point (H2).
        for k in range(1, steps - 1):
            t = sa + (ea - sa) * (k / (steps - 1))
            pts.append((cx + radius * math.cos(t), cy + radius * math.sin(t)))
        pts.append((x2, y2))

    return pts


def _samples_for(dxftype: str, geom: dict[str, Any]) -> list[tuple[float, float]]:
    if dxftype == "LINE":
        x1 = geom.get("x1"); y1 = geom.get("y1")
        x2 = geom.get("x2"); y2 = geom.get("y2")
        if None in (x1, y1, x2, y2):
            return []
        return [(float(x1), float(y1)), (float(x2), float(y2))]

    if dxftype == "ARC":
        cx = geom.get("cx"); cy = geom.get("cy"); r = geom.get("r")
        sa = geom.get("start_angle"); ea = geom.get("end_angle")
        if None in (cx, cy, r, sa, ea):
            return []
        return _sample_arc(float(cx), float(cy), float(r), float(sa), float(ea))

    if dxftype in ("LWPOLYLINE", "POLYLINE"):
        verts = geom.get("vertices") or []
        return _sample_polyline(verts, bool(geom.get("closed")))

    if dxftype == "CIRCLE":
        cx = geom.get("cx"); cy = geom.get("cy"); r = geom.get("r")
        if None in (cx, cy, r):
            return []
        # Full CCW loop, closed back on itself.
        return _sample_arc(float(cx), float(cy), float(r), 0.0, 360.0)

    return []


def _length_of(samples: list[tuple[float, float]]) -> float:
    if len(samples) < 2:
        return 0.0
    s = 0.0
    for i in range(len(samples) - 1):
        s += math.hypot(samples[i + 1][0] - samples[i][0], samples[i + 1][1] - samples[i][1])
    return s


def build_graph(
    items: Iterable[tuple[str, str, dict[str, Any]]],
    tol: float = DEFAULT_TOL_MM,
) -> TopoGraph:
    """Build the topology graph.

    Each item is ``(entity_id, dxftype, geom)``. Returns a ``TopoGraph`` with
    one network edge per entity_id; CIRCLEs land as self-loops.
    """

    # First pass: extract endpoints & samples.
    raw: list[tuple[str, str, dict[str, Any], tuple[float, float], tuple[float, float]]] = []
    samples_by_id: dict[str, list[tuple[float, float]]] = {}
    for eid, dxftype, geom in items:
        ep = endpoints_of(dxftype, geom)
        if ep is None:
            continue
        raw.append((eid, dxftype, geom, ep[0], ep[1]))
        samples_by_id[eid] = _samples_for(dxftype, geom)

    if not raw:
        return TopoGraph(nodes=[], edges={}, graph=nx.MultiGraph())

    # Collect endpoint coordinates for the K-D tree.
    coords: list[tuple[float, float]] = []
    for _eid, _t, _g, a, b in raw:
        coords.append(a)
        coords.append(b)

    # Union-find for transitive endpoint merging (M4). The previous
    # implementation only merged neighbours of the *first* unseen point,
    # which broke when three endpoints A, B, C lay on a line and only
    # the (A,B) / (B,C) pairs were within tol — A would not collapse to
    # C even though B bridges them.
    tree = cKDTree(coords)
    n_coords = len(coords)
    parent = list(range(n_coords))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    pairs = tree.query_pairs(r=tol)
    for i, j in pairs:
        union(i, j)

    # Compress: each root → a fresh node id with the root's coordinate.
    root_to_node: dict[int, int] = {}
    nodes: list[tuple[float, float]] = []
    canonical: list[int] = [-1] * n_coords
    for i in range(n_coords):
        r = find(i)
        if r not in root_to_node:
            root_to_node[r] = len(nodes)
            nodes.append(coords[r])
        canonical[i] = root_to_node[r]

    # Build the network graph keyed on entity_id.
    g = nx.MultiGraph()
    edges: dict[str, EdgeInfo] = {}
    for k, (eid, dxftype, geom, _a, _b) in enumerate(raw):
        node_a = canonical[2 * k]
        node_b = canonical[2 * k + 1]
        samples = samples_by_id.get(eid, [])
        info = EdgeInfo(
            entity_id=eid,
            dxftype=dxftype,
            geom=geom,
            node_a=node_a,
            node_b=node_b,
            length=_length_of(samples),
            samples=samples,
        )
        edges[eid] = info
        g.add_node(node_a)
        g.add_node(node_b)
        # Use entity_id as the edge key so we can look it up unambiguously.
        g.add_edge(node_a, node_b, key=eid, info=info)

    return TopoGraph(nodes=nodes, edges=edges, graph=g)


# ---------------------------------------------------------------------------
# Loop enumeration
# ---------------------------------------------------------------------------


def find_closed_loops(
    topo: TopoGraph, max_cycles: int = 60
) -> list[list[str]]:
    """Enumerate plausible outer-loop candidates as ordered entity_id chains.

    Two passes (H4, H5):

    1. Shapely ``polygonize_full`` over each edge's sample polyline. This
       correctly handles multi-edges (LINE+ARC across the same node pair
       — e.g. a D-shape) and recovers face boundaries that
       ``nx.cycle_basis`` silently collapses. Each polygon's boundary
       segments are matched back to entity_ids by point proximity.
    2. Legacy ``cycle_basis`` walk as a fallback so existing two-LINE
       rectangles keep landing in the candidate pool even when shapely
       fails (e.g. zero-length samples, degenerate geometry).
    """

    g = topo.graph
    if g.number_of_edges() == 0:
        return []

    results: list[list[str]] = []
    seen_signatures: set[frozenset[str]] = set()

    # CIRCLE self-loops are immediate single-entity cycles.
    for u, v, key in g.edges(keys=True):
        if u == v:
            sig = frozenset({key})
            if sig in seen_signatures:
                continue
            seen_signatures.add(sig)
            results.append([key])

    # ----- Pass 1: Shapely polygonize_full ------------------------------
    # Snap each LineString endpoint to its shared graph-node coordinate
    # so polygonize can recognise the topology — without this an ARC
    # ending at ``(-10.0, 1.2e-15)`` and a LINE starting at
    # ``(-10.0, 0.0)`` fail to glue (DXF samples carry sub-tolerance
    # noise that polygonize_full's exact-coord match cannot tolerate).
    line_strings: list[tuple[str, LineString]] = []
    for eid, info in topo.edges.items():
        if info.node_a == info.node_b and info.dxftype == "CIRCLE":
            continue  # already emitted as a self-loop above
        pts = list(info.samples)
        if len(pts) < 2:
            continue
        node_a_xy = topo.nodes[info.node_a]
        node_b_xy = topo.nodes[info.node_b]
        # Determine which sample end maps to which graph node so we can
        # snap correctly even for reversed sample orientations.
        d_a_first = math.hypot(pts[0][0] - node_a_xy[0], pts[0][1] - node_a_xy[1])
        d_a_last = math.hypot(pts[-1][0] - node_a_xy[0], pts[-1][1] - node_a_xy[1])
        if d_a_first <= d_a_last:
            pts[0] = (float(node_a_xy[0]), float(node_a_xy[1]))
            pts[-1] = (float(node_b_xy[0]), float(node_b_xy[1]))
        else:
            pts[0] = (float(node_b_xy[0]), float(node_b_xy[1]))
            pts[-1] = (float(node_a_xy[0]), float(node_a_xy[1]))
        try:
            line_strings.append((eid, LineString(pts)))
        except Exception:  # noqa: BLE001 - degenerate samples
            continue

    if line_strings:
        try:
            poly_collection, _dangles, _cuts, _invalid = polygonize_full(
                [ls for _, ls in line_strings]
            )
            polys = list(getattr(poly_collection, "geoms", []) or [])
        except Exception:  # noqa: BLE001 - polygonize is robust but be safe
            polys = []

        # Sort largest first so the candidate cap doesn't drop the big
        # face for a tiny noise loop.
        polys.sort(key=lambda p: getattr(p, "area", 0.0), reverse=True)

        for poly in polys:
            ordered = _polygon_to_entity_chain(poly, topo)
            if not ordered:
                continue
            # A single-entity result means the polygon is dominated by one
            # closed polyline — Strategy B already handles that case better
            # (without spurious "wrap" effects), so we let it pass and we
            # skip here.
            if len(ordered) < 2:
                continue
            # Sanity: the chain must form a connected 2-edge-connected
            # cycle in the topology graph. ``polygonize`` happily glues
            # near-collinear LineStrings into a face even when they share
            # no graph node — that yields a chain like ``[e00002, e00001]``
            # for the ベースフレーム sample which is not a real cycle.
            if not _chain_is_node_cycle(ordered, topo):
                continue
            sig = frozenset(ordered)
            if sig in seen_signatures:
                continue
            seen_signatures.add(sig)
            results.append(ordered)
            if len(results) >= max_cycles:
                return results

    # ----- Pass 2: legacy cycle_basis fallback --------------------------
    for comp in nx.connected_components(g):
        sub = g.subgraph(comp)
        simple = nx.Graph()
        edge_choice: dict[tuple[int, int], str] = {}
        for u, v, key in sub.edges(keys=True):
            a, b = (u, v) if u <= v else (v, u)
            if simple.has_edge(a, b):
                cur = edge_choice[(a, b)]
                if topo.edges[key].length > topo.edges[cur].length:
                    edge_choice[(a, b)] = key
                continue
            simple.add_edge(a, b)
            edge_choice[(a, b)] = key

        try:
            basis = nx.cycle_basis(simple)
        except Exception:  # noqa: BLE001
            basis = []

        for cycle_nodes in basis:
            if len(cycle_nodes) < 2:
                continue
            ordered_edges = _nodes_to_edge_chain(cycle_nodes, edge_choice)
            if not ordered_edges:
                continue
            sig = frozenset(ordered_edges)
            if sig in seen_signatures:
                continue
            seen_signatures.add(sig)
            results.append(ordered_edges)
            if len(results) >= max_cycles:
                return results

    return results


def _chain_is_node_cycle(chain: list[str], topo: "TopoGraph") -> bool:
    """True when consecutive entities share an endpoint node and the
    last entity wraps back to the first. Self-loop entities (CIRCLE) and
    closed polylines satisfy this with a single entity."""

    if not chain:
        return False
    if len(chain) == 1:
        info = topo.edges.get(chain[0])
        return bool(info and info.node_a == info.node_b)

    prev = topo.edges.get(chain[0])
    if prev is None:
        return False
    # Decide the orientation of the first edge by checking which of its
    # endpoints is shared with the second edge.
    second = topo.edges.get(chain[1])
    if second is None:
        return False
    shared = {second.node_a, second.node_b}
    if prev.node_b in shared:
        start_node = prev.node_a
        tail = prev.node_b
    elif prev.node_a in shared:
        start_node = prev.node_b
        tail = prev.node_a
    else:
        return False

    for k in range(1, len(chain)):
        info = topo.edges.get(chain[k])
        if info is None:
            return False
        if info.node_a == tail:
            tail = info.node_b
        elif info.node_b == tail:
            tail = info.node_a
        else:
            return False
    return tail == start_node


def _polygon_to_entity_chain(poly, topo: "TopoGraph") -> list[str]:
    """Walk a shapely Polygon's exterior and map each segment to its
    contributing entity-id by snapping segment midpoints to the closest
    edge sample.

    Returns the chain in traversal order, or ``[]`` if any segment cannot
    be confidently attributed.
    """

    try:
        coords = list(poly.exterior.coords)
    except Exception:  # noqa: BLE001
        return []
    if len(coords) < 4:  # closed ring needs >= 3 unique + 1 closing dup
        return []

    # Precompute the candidate-edge LineStrings keyed on entity_id.
    candidate_lines: dict[str, LineString] = {}
    for eid, info in topo.edges.items():
        if len(info.samples) < 2:
            continue
        try:
            candidate_lines[eid] = LineString(info.samples)
        except Exception:  # noqa: BLE001
            continue
    if not candidate_lines:
        return []

    chain: list[str] = []
    seen: set[str] = set()
    tol = max(0.5, DEFAULT_TOL_MM * 20)  # generous tol; we just need attribution

    for i in range(len(coords) - 1):
        x1, y1 = coords[i]
        x2, y2 = coords[i + 1]
        mid = Point((x1 + x2) / 2.0, (y1 + y2) / 2.0)
        best_eid: str | None = None
        best_dist = float("inf")
        for eid, ls in candidate_lines.items():
            d = ls.distance(mid)
            if d < best_dist:
                best_dist = d
                best_eid = eid
        if best_eid is None or best_dist > tol:
            return []
        if chain and chain[-1] == best_eid:
            continue  # same entity covers multiple consecutive segments
        if best_eid in seen and best_eid != chain[0]:
            # would create a non-simple visit pattern
            continue
        chain.append(best_eid)
        seen.add(best_eid)

    if not chain:
        return []
    return chain


def _nodes_to_edge_chain(
    cycle_nodes: list[int], edge_choice: dict[tuple[int, int], str]
) -> list[str]:
    chain: list[str] = []
    n = len(cycle_nodes)
    for i in range(n):
        a, b = cycle_nodes[i], cycle_nodes[(i + 1) % n]
        key = (a, b) if a <= b else (b, a)
        eid = edge_choice.get(key)
        if eid is None:
            return []
        chain.append(eid)
    return chain


# ---------------------------------------------------------------------------
# Polygon helpers
# ---------------------------------------------------------------------------


def polygon_from_loop(topo: TopoGraph, loop: list[str]) -> list[tuple[float, float]]:
    """Stitch the entity-sample polylines into one ordered closed polygon.

    Walks adjacent edges, flipping each as needed so they share endpoints.
    Returns the closed point list (last point == first point dropped).
    """

    if not loop:
        return []
    if len(loop) == 1:
        # CIRCLE self-loop or a single closed polyline.
        info = topo.edges.get(loop[0])
        if info is None:
            return []
        pts = list(info.samples)
        if len(pts) >= 2 and pts[0] == pts[-1]:
            return pts[:-1]
        return pts

    pts: list[tuple[float, float]] = []
    # Decide direction of edge 0 by checking shared endpoint with edge 1.
    e0 = topo.edges.get(loop[0])
    e1 = topo.edges.get(loop[1])
    if e0 is None or e1 is None:
        return []
    shared_with_next = {e1.node_a, e1.node_b}
    if e0.node_b in shared_with_next:
        cur_pts = list(e0.samples)
        last_node = e0.node_b
    else:
        cur_pts = list(reversed(e0.samples))
        last_node = e0.node_a
    pts.extend(cur_pts)

    for k in range(1, len(loop)):
        info = topo.edges.get(loop[k])
        if info is None:
            continue
        if info.node_a == last_node:
            seg = list(info.samples)
            last_node = info.node_b
        elif info.node_b == last_node:
            seg = list(reversed(info.samples))
            last_node = info.node_a
        else:
            # Discontinuity — try the nearer endpoint by coordinate.
            tail = pts[-1]
            d_a = math.hypot(info.samples[0][0] - tail[0], info.samples[0][1] - tail[1])
            d_b = math.hypot(info.samples[-1][0] - tail[0], info.samples[-1][1] - tail[1])
            if d_a <= d_b:
                seg = list(info.samples)
                last_node = info.node_b
            else:
                seg = list(reversed(info.samples))
                last_node = info.node_a
        # Avoid duplicating the shared vertex.
        if seg and pts and _close(seg[0], pts[-1]):
            seg = seg[1:]
        pts.extend(seg)

    # Drop the closing duplicate, if any.
    if len(pts) >= 2 and _close(pts[0], pts[-1]):
        pts.pop()
    return pts


def _close(a: tuple[float, float], b: tuple[float, float], tol: float = 1e-4) -> bool:
    return abs(a[0] - b[0]) < tol and abs(a[1] - b[1]) < tol


def polygon_area(pts: list[tuple[float, float]]) -> float:
    """Unsigned shoelace area for a closed polygon."""

    n = len(pts)
    if n < 3:
        return 0.0
    s = 0.0
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return abs(s) / 2.0


def polygon_perimeter(pts: list[tuple[float, float]]) -> float:
    n = len(pts)
    if n < 2:
        return 0.0
    s = 0.0
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        s += math.hypot(x2 - x1, y2 - y1)
    return s


def polygon_bbox(pts: list[tuple[float, float]]) -> tuple[float, float, float, float]:
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def point_in_polygon(pt: tuple[float, float], poly: list[tuple[float, float]]) -> bool:
    """Even-odd test, no shapely required."""

    x, y = pt
    n = len(poly)
    if n < 3:
        return False
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi + 1e-30) + xi):
            inside = not inside
        j = i
    return inside
