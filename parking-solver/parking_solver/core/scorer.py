from __future__ import annotations

import math

import numpy as np
from shapely.geometry import LineString, MultiLineString, Point
from shapely.ops import unary_union

from parking_solver.core.model import AisleDir, DriveAisle, Metrics, Site, Stall


def _aisle_linestrings(aisle: DriveAisle) -> list[LineString]:
    """Return all constituent LineStrings for a DriveAisle centerline.

    Centerlines can be MultiLineString when the aisle is clipped by an obstacle.
    """
    cl = aisle.centerline
    if isinstance(cl, MultiLineString):
        return list(cl.geoms)
    return [cl]


def score(stalls: list[Stall], site: Site) -> Metrics:
    """Minimal scorer: count stalls, compute gross area per stall."""
    total = len(stalls)
    by_type: dict[str, int] = {}
    for stall in stalls:
        key = stall.type.value
        by_type[key] = by_type.get(key, 0) + 1

    site_area = site.boundary.area
    gross_area_per_stall = site_area / max(total, 1)

    return Metrics(
        total_stalls=total,
        by_type=by_type,
        gross_area_per_stall=gross_area_per_stall,
        site_area=site_area,
    )


def compute_road_coverage(
    aisles: list[DriveAisle],
    site: Site,
    stall_length: float,
) -> float:
    """Fraction of site area reachable from the road network (0–1).

    Oxford ESGI91 (Section 4): every point in the domain should be within
    stall_length + aisle_width/2 of a road centreline for a stall to be
    accessible.  This metric captures how well the road network serves the
    whole site.  A value of 1.0 means every square metre of the site is
    adjacent to a road and could theoretically hold a stall or aisle space.
    """
    if not aisles or site.boundary.is_empty:
        return 0.0
    buffers = [a.centerline.buffer(a.width / 2 + stall_length) for a in aisles]
    served = unary_union(buffers).intersection(site.boundary)
    return served.area / site.boundary.area


def compute_dead_ends(aisles: list[DriveAisle], tolerance: float = 0.5) -> float:
    """Fraction of road segments outside the largest connected component (0=fully connected).

    Uses union-find over all centerlines: two segments are connected when they
    touch or cross within *tolerance* (so a cross-aisle that crosses a row aisle
    in its middle counts as connected, not just endpoint-to-endpoint joins).
    Returns the fraction of segments *not* in the main network component.

    0.0 = all roads form one connected network (ideal).
    1.0 = every road segment is isolated (worst).

    Ring strategies (PERIMETER_RING, MULTI_RING) emit no DriveAisle objects and
    return 0.0 (neutral — the ring is implicitly connected).
    STANDARD on irregular polygons may score poorly when cross-aisles are absent.
    """
    # Flatten all DriveAisles into individual LineStrings (obstacle clipping can
    # produce MultiLineStrings; treat each sub-segment as an independent road piece).
    segs: list[LineString] = []
    for a in aisles:
        segs.extend(_aisle_linestrings(a))

    n = len(segs)
    if n < 2:
        return 0.0

    # Union-Find over individual road segments
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        pa, pb = find(a), find(b)
        if pa != pb:
            parent[pa] = pb

    for i in range(n):
        for j in range(i + 1, n):
            # Segment-to-segment distance catches both endpoint joins and
            # mid-span crossings (a collector crossing the rows).
            if segs[i].distance(segs[j]) <= tolerance:
                union(i, j)

    # Fraction of segments NOT in the largest connected component
    from collections import Counter
    sizes = Counter(find(i) for i in range(n))
    largest = max(sizes.values())
    return (n - largest) / n


def compute_stall_isolation(
    stalls: list[Stall],
    stall_width: float,
) -> float:
    """Fraction of stalls that are isolated (no row neighbor within 2.5×stall_width).

    Stalls in a proper row always have at least one neighbor at approximately
    stall_width distance.  An isolated stall is a lone partial-row remnant
    placed at a polygon edge where the row couldn't fit more stalls.
    These "stub" stalls are counted and returned as a fraction of total stalls.
    """
    if len(stalls) < 2:
        return 0.0

    centroids = np.array([
        [s.polygon.centroid.x, s.polygon.centroid.y] for s in stalls
    ])
    search_r = stall_width * 2.5

    isolated = 0
    for i, c in enumerate(centroids):
        dists = np.linalg.norm(centroids - c, axis=1)
        # Count neighbors excluding self
        neighbors = int(np.sum((dists > 1e-3) & (dists <= search_r)))
        if neighbors < 2:
            isolated += 1

    return isolated / len(stalls)


def compute_aisle_area_ratio(
    aisles: list[DriveAisle],
    site: Site,
) -> float:
    """Fraction of the site consumed by drive aisles (0–1, lower = less overhead).

    Drive aisles are pure circulation — every m² spent on them is a m² not
    available for stalls.  Buffering each centerline by half its width and
    clipping to the site gives the real tarmac footprint.  A low ratio means the
    layout spends most of its land on parking rather than driving lanes.
    """
    if not aisles or site.boundary.is_empty:
        return 0.0
    footprints = [a.centerline.buffer(a.width / 2.0) for a in aisles]
    covered = unary_union(footprints).intersection(site.boundary)
    return covered.area / site.boundary.area


def compute_walk_distance(
    stalls: list[Stall],
    entrances: list,
) -> tuple[float, float]:
    """Mean and max distance from each stall to its *nearest* entrance (metres).

    Pedestrians walk to whichever entrance is closest, so distance is measured to
    the nearest one (not just the first).  Returns (0, 0) when there are no stalls
    or no entrances.
    """
    if not stalls or not entrances:
        return 0.0, 0.0
    pts = [e.point for e in entrances]
    total = 0.0
    worst = 0.0
    for s in stalls:
        c = s.polygon.centroid
        d = min(math.hypot(c.x - p.x, c.y - p.y) for p in pts)
        total += d
        worst = max(worst, d)
    return total / len(stalls), worst


def directed_road_graph(aisles: list[DriveAisle], snap: float = 0.6):
    """Build a directed graph of the road network.

    Nodes are quantised segment endpoints; edges follow drive direction (two-way
    aisles bidirectional, one-way aisles along their `flow` vector).  Every node
    is projected onto every segment it lies on (T-junctions), so a row aisle that
    ends in the middle of a cross-aisle is properly connected.

    Returns ``(nodes, fwd, bwd)`` where *nodes* maps key→(x, y) and *fwd*/*bwd*
    are key→set(key) forward/reverse adjacency.
    """
    from collections import defaultdict

    def key(pt) -> tuple[int, int]:
        return (round(pt[0] / snap), round(pt[1] / snap))

    seg_info: list[tuple[LineString, AisleDir, tuple | None]] = []
    for a in aisles:
        for s in _aisle_linestrings(a):
            if not s.is_empty:
                seg_info.append((s, a.direction, a.flow))

    nodes: dict[tuple[int, int], tuple[float, float]] = {}
    for s, _, _ in seg_info:
        coords = list(s.coords)
        if len(coords) < 2:
            continue
        nodes[key(coords[0])] = coords[0]
        nodes[key(coords[-1])] = coords[-1]

    fwd: dict[tuple[int, int], set] = defaultdict(set)
    for s, direction, flow in seg_info:
        on_seg = []
        for k, (px, py) in nodes.items():
            if s.distance(Point(px, py)) <= snap:
                on_seg.append((s.project(Point(px, py)), k))
        on_seg.sort()
        seq = [k for _, k in on_seg]
        if len(seq) < 2:
            continue
        if direction == AisleDir.ONE_WAY and flow is not None:
            coords = list(s.coords)
            seg_vec = (coords[-1][0] - coords[0][0], coords[-1][1] - coords[0][1])
            if seg_vec[0] * flow[0] + seg_vec[1] * flow[1] < 0:
                seq = seq[::-1]
            for u, v in zip(seq, seq[1:]):
                fwd[u].add(v)
        else:
            for u, v in zip(seq, seq[1:]):
                fwd[u].add(v)
                fwd[v].add(u)

    bwd: dict[tuple[int, int], set] = defaultdict(set)
    for u, vs in fwd.items():
        for v in vs:
            bwd[v].add(u)

    return nodes, fwd, bwd


def _bfs(graph: dict, srcs: set) -> set:
    from collections import deque
    seen = set(srcs)
    dq = deque(srcs)
    while dq:
        u = dq.popleft()
        for v in graph[u]:
            if v not in seen:
                seen.add(v)
                dq.append(v)
    return seen


def entrance_source_nodes(nodes: dict, entrances: list) -> set:
    """The graph node nearest each entrance (the points a driver can enter from)."""
    sources = set()
    for e in entrances:
        if not nodes:
            break
        nearest = min(
            nodes,
            key=lambda k: math.hypot(nodes[k][0] - e.point.x, nodes[k][1] - e.point.y),
        )
        sources.add(nearest)
    return sources


def compute_circuit_validity(
    aisles: list[DriveAisle],
    entrances: list,
    snap: float = 0.6,
) -> float:
    """Fraction of the road network a driver can both reach *and* exit from (0–1).

    A node is valid only if it is reachable from an entrance (drive *in*) AND can
    reach an entrance (drive *out*).  Nodes that trap the driver — no legal
    one-way path back to an exit — are invalid.  1.0 = the lot circulates fully.
    Returns 1.0 when there are no aisles or entrances (nothing to validate).
    """
    if not aisles or not entrances:
        return 1.0
    nodes, fwd, bwd = directed_road_graph(aisles, snap)
    if not nodes:
        return 1.0
    sources = entrance_source_nodes(nodes, entrances)
    valid = _bfs(fwd, sources) & _bfs(bwd, sources)
    return len(valid) / len(nodes)


def compute_entrance_connectivity(
    aisles: list[DriveAisle],
    entrances: list,
    tolerance: float = 2.0,
) -> float:
    """Fraction of entrances that connect to the road network (0–1).

    Every entrance must be reachable by car: its point should lie within
    *tolerance* metres of some aisle centerline.  A layout whose road network
    doesn't touch the entrance is physically unusable — drivers cannot get in.

    Returns 1.0 when there are no entrances (vacuously connected) and 0.0 when
    there are entrances but no roads.
    """
    if not entrances:
        return 1.0
    if not aisles:
        return 0.0
    network = unary_union([a.centerline for a in aisles])
    connected = sum(
        1 for e in entrances if network.distance(e.point) <= tolerance
    )
    return connected / len(entrances)


def theoretical_max_stalls(
    stall_width: float,
    stall_length: float,
    aisle_width_90: float,
) -> float:
    """Oxford ESGI91 density ceiling for 90° double-row packing (stalls / m²).

    ρ(90°) = 2·h·w / ((lane + 2·h) × w)
           = 2·h / (lane + 2·h)           [simplifies]

    where h = stall_length, w = stall_width, lane = two-way aisle width.

    Multiply by site area to get the theoretical upper bound on stall count.
    The formula is derived in Section 3.3 of the ESGI91 report and proven
    to be the global optimum for tiling an infinite plane.
    """
    module_width = aisle_width_90 + 2.0 * stall_length
    return 2.0 / (module_width * stall_width)
