"""Parking layout generator.

Six strategies (dispatched via LayoutParams.layout_type):

  STANDARD
      Double-loaded banded rows.  Aisles run parallel to `orientation`.
      Cross-aisles at both ends make the road network connected (ladder
      topology).  When orientation=0 on a non-axis-aligned polygon the
      dominant polygon-edge direction is auto-selected.

  FISHBONE
      Herringbone variant of STANDARD.  Bottom rows lean left, top rows
      lean right (stall_parallelogram_mirrored), creating a V-pattern at
      each aisle.  Same connectivity as STANDARD.

  PERIMETER_RING
      Stalls placed 90° to every polygon edge; a single ring road runs
      inside them.  Good for narrow or irregular sites.

  RING_INFILL
      Perimeter ring + banded interior aligned to the dominant edge.

  MULTI_RING
      Concentric perimeter rings filling inward.  Each ring road serves
      the outer stalls of the inner ring and the inner stalls of the outer
      ring (double-loaded module, bent around the polygon).

  SPINE_BRANCHES
      Wide central collector aisle running through the polygon midpoint.
      The polygon is split into two halves and each is filled with banded
      double-loaded rows whose aisles terminate at the collector — creating
      a true T-network that all connects to one through-road.

Road-network connectivity
-------------------------
Research (Eiselt & Laporte, branch-and-cut MIP) shows connectivity must
be a hard constraint.  Every strategy here guarantees it:
  * STANDARD / FISHBONE: ladder topology (row aisles + 2 cross-aisles)
  * PERIMETER_RING / MULTI_RING: ring roads are inherently connected
  * RING_INFILL: interior ends terminate at the ring road
  * SPINE_BRANCHES: branch aisles terminate at the spine collector
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import Callable

from shapely import affinity
from shapely.geometry import (
    GeometryCollection,
    LineString,
    MultiLineString,
    MultiPolygon,
    Point,
    Polygon,
    box as _box,
)
from shapely.ops import nearest_points, split as shapely_split, unary_union

from parking_solver.core import scorer
from parking_solver.core.scorer import (
    compute_aisle_area_ratio,
    compute_circuit_validity,
    compute_dead_ends,
    compute_entrance_connectivity,
    compute_road_coverage,
    compute_stall_isolation,
    directed_road_graph,
    entrance_source_nodes,
)
from parking_solver.core.geometry.helpers import (
    offset_inward,
    polygon_edge_directions,
    stall_parallelogram,
    stall_parallelogram_mirrored,
)
from parking_solver.core.model import (
    AisleDir,
    DriveAisle,
    FixedElements,
    Layout,
    LayoutParams,
    LayoutType,
    Metrics,
    Site,
    Stall,
    StallType,
)
from parking_solver.core.regulations.engine import RegulationProfile, module_geometry


# ── Result container for exhaustive exploration ───────────────────────────────

@dataclass
class StrategyResult:
    """One entry from generate_all() — a specific strategy/orientation/angle combo."""
    layout_type: LayoutType
    orientation: float
    angle: float
    stall_width: float
    layout: Layout
    road_coverage: float = 0.0        # fraction of site area served by road network (0–1)
    dead_ends: float = 0.0            # fraction of road segments off the main network (0–1)
    stall_isolation: float = 0.0      # fraction of stalls with no row neighbors (0–1)
    entrance_connectivity: float = 1.0 # fraction of entrances reachable by road (0–1)
    aisle_area_ratio: float = 0.0     # fraction of site consumed by drive aisles (0–1)
    circuit_validity: float = 1.0     # fraction of network drivable in-and-out (0–1)

    @property
    def stall_count(self) -> int:
        return self.layout.metrics.total_stalls

    @property
    def label(self) -> str:
        name = self.layout_type.value.replace("_", " ").title()
        return (
            f"{name} | {self.orientation:.0f}° | {self.angle:.0f}° | "
            f"{self.stall_count} stalls | "
            f"{self.layout.metrics.gross_area_per_stall:.1f} m²/stall"
        )


# ── Public API ────────────────────────────────────────────────────────────────

def generate(
    site: Site,
    profile: RegulationProfile,
    params: LayoutParams,
    fixed: FixedElements | None = None,
) -> Layout:
    """Generate a parking layout, dispatching to the selected strategy."""
    work = _compute_work(site, profile, fixed)
    if work is None or work.is_empty or work.area < 1e-6:
        return _empty_layout(site, params, profile)

    params = _resolve_orientation(params, work)

    if params.layout_type == LayoutType.PERIMETER_RING:
        stalls, aisles = _generate_perimeter_ring(work, profile, params)
    elif params.layout_type == LayoutType.RING_INFILL:
        stalls, aisles = _generate_ring_infill(work, profile, params)
    elif params.layout_type == LayoutType.MULTI_RING:
        stalls, aisles = _generate_multi_ring(work, profile, params)
    elif params.layout_type == LayoutType.SPINE_BRANCHES:
        stalls, aisles = _generate_spine_branches(work, profile, params)
    elif params.layout_type == LayoutType.FISHBONE:
        stalls, aisles = _generate_fishbone(work, profile, params)
    elif params.layout_type == LayoutType.MIXED_ANGLE:
        stalls, aisles = _generate_mixed_angle(work, profile, params)
    elif params.layout_type == LayoutType.SUBDIVIDED:
        stalls, aisles = _generate_subdivided(work, profile, params)
    else:
        stalls, aisles = _generate_banded(work, profile, params)

    # ── Compliance: everything must sit inside the buildable area ──────────────
    # Drop any generated stall not fully inside; clip every interior aisle so its
    # *full-width* band stays inside.  Stitch/rescue connectors are clipped too,
    # so no interior road ever pokes over the boundary.
    stalls, aisles = _enforce_inside(stalls, aisles, work)
    aisles = _stitch_network(aisles, profile, work)
    _assign_flow_directions(aisles, site.entrances)
    aisles = _ensure_drivable(aisles, site.entrances, profile, work)
    _, aisles = _enforce_inside(stalls, aisles, work)

    # The entrance driveway is the one legitimate boundary crossing (the gate), so
    # it's added last and clipped to the parcel — it reaches the entrance rather
    # than stopping half a road-width short.
    aisles = _connect_entrances(aisles, site.entrances, profile, work)

    # Roads win over stalls: secondary roads (stitch / rescue collectors, the
    # entrance driveway) are routed after the stalls are placed, so any stall a
    # road runs over is removed — you can't park where a lane is.
    stalls = _clip_stalls_to_roads(stalls, aisles)

    # Locked stalls are user-placed and survive as-is — they were subtracted from
    # the work area on purpose, so they legitimately sit outside it.
    if fixed and fixed.stalls:
        stalls = fixed.stalls + stalls

    metrics = scorer.score(stalls, site)
    return Layout(
        stalls=stalls,
        aisles=aisles,
        entrances=site.entrances,
        metrics=metrics,
        params=params,
        profile_id=profile.id,
    )


@dataclass
class PolygonPlan:
    """Which strategies the analyzer decided are worth exploring for a site."""
    perimeter_infill: bool = True
    subdivision: bool = False
    banded: bool = False
    herringbone: bool = False
    angles: list[float] = field(default_factory=lambda: [90.0])
    orientations: list[float] = field(default_factory=lambda: [0.0])
    reason: str = ""


def analyze_polygon(
    work: Polygon,
    profile: RegulationProfile,
    stall_length: float = 5.0,
) -> PolygonPlan:
    """Decide which strategies to explore for *work*, to keep the search small.

    Two top-level strategies exist — **perimeter + infill** (the go-to for any
    reasonably compact site) and **subdivision** (for complex/non-convex sites,
    which then fills each region with banded/herringbone micro-strategies).
    Banded and herringbone are only explored *directly* on narrow/small sites
    where a ring or a decomposition doesn't make sense; herringbone is reserved
    for those tight spaces specifically.
    """
    available = [a for a in (90.0, 60.0, 45.0) if str(int(a)) in profile.aisles] or [90.0]
    edge_dirs = polygon_edge_directions(work)
    orientations = _build_orientations(edge_dirs)

    area = work.area
    try:
        cc = list(work.minimum_rotated_rectangle.exterior.coords)
        s1 = math.hypot(cc[1][0] - cc[0][0], cc[1][1] - cc[0][1])
        s2 = math.hypot(cc[2][0] - cc[1][0], cc[2][1] - cc[1][1])
        short, long = (s1, s2) if s1 <= s2 else (s2, s1)
    except Exception:
        minx, miny, maxx, maxy = work.bounds
        short, long = sorted([maxx - minx, maxy - miny])

    hull_area = work.convex_hull.area
    convexity = area / hull_area if hull_area > 1e-9 else 1.0
    reflex = len(_reflex_points(work))
    aspect = long / short if short > 1e-9 else 1.0

    module = 2.0 * stall_length + 6.0          # double-loaded module depth (~16 m)
    narrow = short < module                     # only ~one module fits across
    small = area < 1000.0
    complex_shape = convexity < 0.82 or reflex >= 1

    plan = PolygonPlan(angles=available, orientations=orientations[:3])
    plan.perimeter_infill = short >= module and not small
    plan.subdivision = complex_shape and not narrow
    plan.herringbone = narrow or small          # tight spaces only
    plan.banded = narrow or small or aspect > 3.5 or not plan.perimeter_infill
    if not any((plan.perimeter_infill, plan.subdivision, plan.banded, plan.herringbone)):
        plan.banded = True

    picked = [n for n, on in (
        ("perimeter+infill", plan.perimeter_infill), ("subdivision", plan.subdivision),
        ("banded", plan.banded), ("herringbone", plan.herringbone)) if on]
    plan.reason = (f"short={short:.0f}m aspect={aspect:.1f} convexity={convexity:.2f} "
                   f"reflex={reflex} → {', '.join(picked)}")
    return plan


def generate_all(
    site: Site,
    profile: RegulationProfile,
    stall_width: float = 2.5,
    stall_length: float = 5.0,
    fixed: FixedElements | None = None,
    progress_callback: Callable[[int, int], None] | None = None,
    result_callback: Callable[[StrategyResult], None] | None = None,
) -> list[StrategyResult]:
    """Exhaustive exploration across all strategies, characteristic orientations,
    and stall angles.  Returns results sorted by stall count (descending).

    progress_callback(done, total) is called before each task if provided.
    result_callback(StrategyResult) is called as soon as each non-empty layout is
    generated — letting the UI stream results into the list live, rather than
    waiting for the whole sweep to finish.
    """
    work = _compute_work(site, profile, fixed)
    if work is None or work.is_empty:
        return []

    # A pre-pass analyzes the polygon and decides which strategies are worth
    # trying — most sites only need perimeter+infill, so the search stays small.
    plan = analyze_polygon(work, profile, stall_length)
    orients = plan.orientations or [0.0]
    angled = [a for a in plan.angles if a < 90]   # herringbone is angled-only

    tasks: list[tuple] = []
    seen: set[tuple] = set()

    def _add_task(lt: LayoutType, orientation: float, angle: float) -> None:
        key = (lt, round(orientation), round(angle))
        if key in seen:
            return
        seen.add(key)
        adir = AisleDir.ONE_WAY if 0 < angle < 90 else AisleDir.TWO_WAY
        tasks.append((lt, orientation, angle, stall_width, adir))

    if plan.perimeter_infill:
        for orientation in orients[:2]:
            for angle in plan.angles:
                _add_task(LayoutType.RING_INFILL, orientation, angle)
    if plan.subdivision:
        for orientation in orients[:2]:
            for angle in (*plan.angles, 0.0):   # 0.0 = auto best-per-region
                _add_task(LayoutType.SUBDIVIDED, orientation, angle)
    if plan.banded:
        for orientation in orients[:2]:
            for angle in plan.angles:
                _add_task(LayoutType.STANDARD, orientation, angle)
    if plan.herringbone:
        for orientation in orients[:2]:
            for angle in angled:
                _add_task(LayoutType.FISHBONE, orientation, angle)

    results: list[StrategyResult] = []
    for i, (lt, orientation, angle, sw, adir) in enumerate(tasks):
        if progress_callback:
            progress_callback(i, len(tasks))
        params = LayoutParams(
            orientation=orientation,
            layout_type=lt,
            angle=angle,
            stall_width=sw,
            stall_length=stall_length,
            aisle_dir=adir,
        )
        try:
            layout = generate(site, profile, params, fixed=fixed)
            if layout.metrics.total_stalls > 0:
                coverage = compute_road_coverage(layout.aisles, site, stall_length)
                dead_e = compute_dead_ends(layout.aisles)
                isolation = compute_stall_isolation(layout.stalls, sw)
                entr_conn = compute_entrance_connectivity(layout.aisles, site.entrances)
                aisle_ratio = compute_aisle_area_ratio(layout.aisles, site)
                circuit = compute_circuit_validity(layout.aisles, site.entrances)
                # Skip fundamentally undrivable variants (compliance can prevent a
                # rescue on hard angled/tapering cases) — they aren't usable layouts.
                if circuit < 0.5:
                    continue
                sr = StrategyResult(
                    layout_type=lt,
                    orientation=orientation,
                    angle=angle,
                    stall_width=sw,
                    layout=layout,
                    road_coverage=coverage,
                    dead_ends=dead_e,
                    stall_isolation=isolation,
                    entrance_connectivity=entr_conn,
                    aisle_area_ratio=aisle_ratio,
                    circuit_validity=circuit,
                )
                results.append(sr)
                if result_callback:
                    result_callback(sr)
        except Exception:
            pass

    if progress_callback:
        progress_callback(len(tasks), len(tasks))

    return sorted(results, key=lambda r: r.stall_count, reverse=True)


# ── Compliance: keep everything inside the buildable area ─────────────────────

def _clip_centerline_inside(geom, width: float, work: Polygon):
    """Clip an aisle centerline so its full-width band stays inside *work*.

    Erodes the buildable area by half the road width and intersects the centerline
    with it, so ``centerline.buffer(width/2)`` cannot cross the boundary.  Returns
    a LineString / MultiLineString, or None when nothing survives.
    """
    inner = offset_inward(work, width / 2.0)
    if inner is None or inner.is_empty:
        return None
    clipped = geom.intersection(inner)
    if clipped.is_empty:
        return None
    if isinstance(clipped, (LineString, MultiLineString)):
        return clipped
    if isinstance(clipped, GeometryCollection):
        lines = [g for g in clipped.geoms
                 if isinstance(g, LineString) and g.length > 1e-6]
        if not lines:
            return None
        return MultiLineString(lines) if len(lines) > 1 else lines[0]
    return None


def _enforce_inside(
    stalls: list[Stall],
    aisles: list[DriveAisle],
    work: Polygon,
) -> tuple[list[Stall], list[DriveAisle]]:
    """Drop out-of-bounds stalls and clip aisle bands to the buildable area."""
    work_buf = work.buffer(1e-6)
    kept_stalls = [s for s in stalls if work_buf.contains(s.polygon)]
    kept_aisles: list[DriveAisle] = []
    for a in aisles:
        cl = _clip_centerline_inside(a.centerline, a.width, work)
        if cl is not None and not cl.is_empty:
            kept_aisles.append(replace(a, centerline=cl))
    return kept_stalls, kept_aisles


def _clip_stalls_to_roads(
    stalls: list[Stall],
    aisles: list[DriveAisle],
    max_overlap_frac: float = 0.05,
) -> list[Stall]:
    """Remove stalls that a drive-aisle band runs over.

    Primary row aisles sit *between* stall rows (edge-adjacent, no overlap), but
    secondary roads — stitch/rescue collectors and the entrance driveway — are
    routed after stalls are placed and can cross them.  A stall a road covers by
    more than *max_overlap_frac* of its area is dropped (you can't park there).
    """
    if not stalls or not aisles:
        return stalls
    road = unary_union([a.centerline.buffer(a.width / 2.0) for a in aisles])
    kept: list[Stall] = []
    for s in stalls:
        if s.polygon.intersection(road).area <= max_overlap_frac * s.polygon.area:
            kept.append(s)
    return kept


# ── Road-network stitching ────────────────────────────────────────────────────

def _aisle_segments(aisles: list[DriveAisle]) -> list[LineString]:
    """Flatten aisle centerlines into individual LineStrings (handles MultiLineString)."""
    segs: list[LineString] = []
    for a in aisles:
        cl = a.centerline
        if isinstance(cl, MultiLineString):
            segs.extend(g for g in cl.geoms if not g.is_empty)
        elif not cl.is_empty:
            segs.append(cl)
    return segs


def _stitch_network(
    aisles: list[DriveAisle],
    profile: RegulationProfile,
    work: Polygon | None = None,
    tolerance: float = 0.6,
) -> list[DriveAisle]:
    """Connect disconnected road components into a single network.

    Subdivided regions and layouts on slanted edges can leave the road network in
    several pieces.  This finds the connected components (union-find over segment
    endpoints) and links every non-main component to the main one with a short
    connector aisle, so the whole layout becomes drivable end-to-end.  Connectors
    are clipped to stay inside *work*.  No-op when already a single component.
    """
    segs = _aisle_segments(aisles)
    n = len(segs)
    if n < 2:
        return aisles

    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    endpoints = [
        (Point(list(s.coords)[0]), Point(list(s.coords)[-1])) for s in segs
    ]
    for i in range(n):
        for j in range(i + 1, n):
            if (segs[j].distance(endpoints[i][0]) <= tolerance
                    or segs[j].distance(endpoints[i][1]) <= tolerance
                    or segs[i].distance(endpoints[j][0]) <= tolerance
                    or segs[i].distance(endpoints[j][1]) <= tolerance):
                union(i, j)

    comps: dict[int, list[LineString]] = {}
    for i in range(n):
        comps.setdefault(find(i), []).append(segs[i])
    if len(comps) < 2:
        return aisles

    comp_geoms = {root: unary_union(geoms) for root, geoms in comps.items()}
    main_root = max(comp_geoms, key=lambda r: comp_geoms[r].length)
    main_geom = comp_geoms[main_root]
    width = profile.fire_lane.min_width if profile.fire_lane else 3.5

    connectors: list[DriveAisle] = []
    for root, geom in comp_geoms.items():
        if root == main_root:
            continue
        p_main, p_other = nearest_points(main_geom, geom)
        connector = LineString([(p_other.x, p_other.y), (p_main.x, p_main.y)])
        if connector.length < 1e-6:
            continue
        if work is not None:
            connector = _clip_centerline_inside(connector, width, work)
            if connector is None or connector.is_empty:
                continue
        connectors.append(DriveAisle(
            centerline=connector, width=width, direction=AisleDir.TWO_WAY,
        ))
        main_geom = unary_union([main_geom, geom, connector])

    return list(aisles) + connectors


# ── One-way flow direction (BFS from entrance) ────────────────────────────────

def _assign_flow_directions(
    aisles: list[DriveAisle],
    entrances: list,
    snap: float = 0.6,
) -> None:
    """Set `flow` (a unit travel vector) on each one-way aisle by BFS from entrances.

    The road network is treated as a graph (nodes = quantised segment endpoints,
    edges = aisle segments).  A breadth-first sweep from the entrance-nearest nodes
    gives each node its hop-distance from an entrance.  One-way aisles are then
    oriented so traffic flows *away from* the entrance (into the lot), giving
    arrows that consistently lead a driver in from the street.  Two-way aisles and
    aisles unreachable from any entrance keep ``flow = None``.
    """
    one_ways = [a for a in aisles if a.direction == AisleDir.ONE_WAY]
    for a in aisles:
        a.flow = None
    if not one_ways or not entrances:
        return

    from collections import defaultdict, deque

    def key(pt) -> tuple[int, int]:
        return (round(pt[0] / snap), round(pt[1] / snap))

    all_segs = _aisle_segments(aisles)

    # Global node set: every segment endpoint (quantised).
    nodes: dict[tuple[int, int], tuple[float, float]] = {}
    for seg in all_segs:
        coords = list(seg.coords)
        if len(coords) < 2:
            continue
        nodes[key(coords[0])] = coords[0]
        nodes[key(coords[-1])] = coords[-1]

    if not nodes:
        return

    # Adjacency that captures T-junctions: project *every* node onto *every*
    # segment it lies on (within snap), order them along the segment, and link
    # consecutive ones.  This connects a row aisle that ends in the middle of a
    # cross-aisle — which a naive endpoint-only graph would leave disconnected.
    adj: dict[tuple[int, int], set[tuple[int, int]]] = defaultdict(set)
    for seg in all_segs:
        on_seg: list[tuple[float, tuple[int, int]]] = []
        for k, (px, py) in nodes.items():
            if seg.distance(Point(px, py)) <= snap:
                on_seg.append((seg.project(Point(px, py)), k))
        on_seg.sort()
        for (_, k1), (_, k2) in zip(on_seg, on_seg[1:]):
            if k1 != k2:
                adj[k1].add(k2)
                adj[k2].add(k1)

    # Multi-source BFS from the node nearest each entrance.
    dist: dict[tuple[int, int], int] = {}
    dq: deque = deque()
    for e in entrances:
        nearest = min(
            nodes,
            key=lambda k: math.hypot(nodes[k][0] - e.point.x, nodes[k][1] - e.point.y),
        )
        if nearest not in dist:
            dist[nearest] = 0
            dq.append(nearest)
    while dq:
        u = dq.popleft()
        for v in adj[u]:
            if v not in dist:
                dist[v] = dist[u] + 1
                dq.append(v)

    inf = float("inf")

    # Group parallel one-way aisles and make adjacent rows alternate direction
    # (serpentine).  With two-way end-collectors this is the textbook one-way
    # circulation: a driver can snake row→collector→next row→collector and always
    # reach an exit.  All-same-direction rows would strand half the lot.
    def _grp(coords) -> int:
        ang = math.degrees(math.atan2(coords[-1][1] - coords[0][1],
                                      coords[-1][0] - coords[0][0])) % 180.0
        return round(ang / 5.0)

    groups: dict[int, list] = defaultdict(list)
    for a in one_ways:
        segs = _aisle_segments([a])
        if not segs:
            continue
        coords = list(segs[0].coords)
        if len(coords) >= 2:
            groups[_grp(coords)].append((a, coords))

    for members in groups.values():
        c0 = members[0][1]
        gx, gy = c0[-1][0] - c0[0][0], c0[-1][1] - c0[0][1]
        gl = math.hypot(gx, gy)
        if gl < 1e-9:
            continue
        g = (gx / gl, gy / gl)
        normal = (-g[1], g[0])

        def _offset(coords) -> float:
            mx = (coords[0][0] + coords[-1][0]) / 2.0
            my = (coords[0][1] + coords[-1][1]) / 2.0
            return mx * normal[0] + my * normal[1]

        members.sort(key=lambda m: _offset(m[1]))

        # Assign a row LEVEL by perpendicular offset: aisle fragments at the same
        # offset (one row split by a concave notch) belong to the SAME row and must
        # share a direction.  Alternating by raw list index would give such
        # fragments opposite flow and trap drivers — so we alternate by level.
        levels: list[float] = []
        member_level: list[int] = []
        for (_a, coords) in members:
            off = _offset(coords)
            if levels and abs(off - levels[-1]) < 1.0:
                member_level.append(len(levels) - 1)
            else:
                levels.append(off)
                member_level.append(len(levels) - 1)

        # Base orientation: row 0 flows away from the entrance (nicer arrows in).
        coords0 = members[0][1]
        da = dist.get(key(coords0[0]), inf)
        db = dist.get(key(coords0[-1]), inf)
        if da <= db:
            away = (coords0[-1][0] - coords0[0][0], coords0[-1][1] - coords0[0][1])
        else:
            away = (coords0[0][0] - coords0[-1][0], coords0[0][1] - coords0[-1][1])
        base_sign = 1.0 if (away[0] * g[0] + away[1] * g[1]) >= 0 else -1.0

        for (a, _coords), lvl in zip(members, member_level):
            s = base_sign * (1.0 if lvl % 2 == 0 else -1.0)
            a.flow = (g[0] * s, g[1] * s)


# ── Drivability guarantee ─────────────────────────────────────────────────────

def _ensure_drivable(
    aisles: list[DriveAisle],
    entrances: list,
    profile: RegulationProfile,
    work: Polygon | None = None,
    snap: float = 0.6,
) -> list[DriveAisle]:
    """Add minimal two-way rescue connectors so every aisle is reachable in AND out.

    One-way layouts on non-rectangular boundaries can leave rows a driver can
    enter but not exit (the bbox end-collectors don't reach a tapering row).  This
    does a directed reachability check from the entrance and links each trapped
    node to the nearest already-drivable node with a short two-way connector —
    guaranteeing the lot is always circulable.  No-op when nothing is trapped.
    """
    if not aisles or not entrances:
        return aisles

    nodes, fwd, bwd = directed_road_graph(aisles, snap)
    if not nodes:
        return aisles

    from collections import deque

    def bfs(graph: dict, srcs: set) -> set:
        seen = set(srcs)
        dq = deque(srcs)
        while dq:
            u = dq.popleft()
            for v in graph[u]:
                if v not in seen:
                    seen.add(v)
                    dq.append(v)
        return seen

    sources = entrance_source_nodes(nodes, entrances)
    valid = bfs(fwd, sources) & bfs(bwd, sources)
    trapped = [k for k in nodes if k not in valid]
    if not trapped:
        return aisles

    width = profile.fire_lane.min_width if profile.fire_lane else 3.5
    valid_pts: dict = {k: nodes[k] for k in valid} or {s: nodes[s] for s in sources}
    inner = offset_inward(work, width / 2.0) if work is not None else None

    result = list(aisles)
    remaining = set(trapped)
    # Greedy: connect the trapped node nearest to the drivable set with a straight
    # two-way connector that stays *inside* the buildable area, then treat it as
    # drivable.  Only inside-routable pairs are considered, so the rescue never
    # adds a road that crosses the boundary.
    while remaining and valid_pts:
        best = None
        for tk in remaining:
            tx, ty = nodes[tk]
            for vp in valid_pts.values():
                d = math.hypot(tx - vp[0], ty - vp[1])
                if best is not None and d >= best[0]:
                    continue
                if inner is not None and d > snap:
                    if not inner.covers(LineString([(tx, ty), vp])):
                        continue   # connector would cross outside — skip
                best = (d, tk, (tx, ty), vp)
        if best is None:
            break   # no compliant connector available for the rest
        d, tk, tp, vp = best
        if d > snap:
            result.append(DriveAisle(
                centerline=LineString([tp, vp]), width=width, direction=AisleDir.TWO_WAY,
            ))
        valid_pts[tk] = nodes[tk]
        remaining.discard(tk)

    return result


# ── Entrance connectivity ─────────────────────────────────────────────────────

def _connect_entrances(
    aisles: list[DriveAisle],
    entrances: list,
    profile: RegulationProfile,
    work: Polygon | None = None,
    tolerance: float = 2.0,
) -> list[DriveAisle]:
    """Ensure every entrance touches the road network.

    For each entrance further than *tolerance* from any aisle centerline, append
    a short two-way connector aisle from the entrance to the nearest point on the
    network, clipped to stay inside *work*.  No-op when there are no entrances or
    no aisles.
    """
    if not entrances or not aisles:
        return aisles

    network = unary_union([a.centerline for a in aisles])
    width = profile.fire_lane.min_width if profile.fire_lane else 3.5

    result = list(aisles)
    for e in entrances:
        if network.distance(e.point) <= tolerance:
            continue
        p_on_net, _ = nearest_points(network, e.point)
        connector = LineString([(e.point.x, e.point.y), (p_on_net.x, p_on_net.y)])
        if connector.length < 1e-6:
            continue
        if work is not None:
            # Clip to the parcel itself (not eroded): the driveway may reach the
            # boundary entrance, but must not stray outside the parcel.
            clipped = connector.intersection(work.buffer(1e-6))
            if clipped.is_empty:
                continue
            if isinstance(clipped, MultiLineString):
                clipped = max(clipped.geoms, key=lambda g: g.length)
            if not isinstance(clipped, LineString):
                continue
            connector = clipped
        result.append(DriveAisle(
            centerline=connector, width=width, direction=AisleDir.TWO_WAY,
        ))
        # Extend the network so the next entrance can connect through this connector
        network = unary_union([network, connector])

    return result


# ── Orientation helpers ───────────────────────────────────────────────────────

def _build_orientations(edge_dirs: list[float]) -> list[float]:
    """Build the orientation list for generate_all().

    Oxford ESGI91 tile-and-trim: the best packing angle is not always exactly
    aligned with a polygon edge — small offsets (±5°) around the dominant
    direction can find a few extra stalls.  We refine around the two most
    important edge directions; remaining edges are included without offsets.
    """
    if not edge_dirs:
        return [0.0]

    seen: set[int] = set()
    result: list[float] = []

    def _add(o: float) -> None:
        key = round(o % 180.0)
        if key not in seen:
            seen.add(key)
            result.append(o % 180.0)

    # Refine around the top-2 dominant edge directions with ±5° offsets
    for base in edge_dirs[:2]:
        for offset in (-5.0, 0.0, 5.0):
            _add(base + offset)

    # Include additional edge directions (up to 4 total) without offsets
    for d in edge_dirs[2:4]:
        _add(d)

    return result


# ── Work area ─────────────────────────────────────────────────────────────────

def _compute_work(
    site: Site,
    profile: RegulationProfile,
    fixed: FixedElements | None,
) -> Polygon | None:
    work = offset_inward(site.boundary, site.setbacks)
    if site.obstacles:
        work = work.difference(unary_union(site.obstacles))
    if fixed and fixed.stalls:
        obs = fixed.as_obstacle_union(clearance=profile.overhang_allowance)
        if obs is not None:
            work = work.difference(obs)
    if work.is_empty:
        return None
    if isinstance(work, (MultiPolygon, GeometryCollection)):
        polys = [g for g in work.geoms if isinstance(g, Polygon) and g.area > 1e-6]
        return max(polys, key=lambda g: g.area) if polys else None
    return work


def _resolve_orientation(params: LayoutParams, work: Polygon) -> LayoutParams:
    """Snap orientation=0° to the dominant edge when the polygon is non-axis-aligned.

    PERIMETER_RING and MULTI_RING are rotationally symmetric — no snap needed.
    """
    if params.layout_type in (LayoutType.PERIMETER_RING, LayoutType.MULTI_RING):
        return params
    if params.orientation != 0.0:
        return params

    dirs = polygon_edge_directions(work)
    if not dirs:
        return params
    dominant = dirs[0]
    if abs(dominant) < 5.0 or abs(dominant - 180.0) < 5.0:
        return params
    return replace(params, orientation=dominant)


# ── Cross-aisle (collector) lane reservation ──────────────────────────────────

def _collector_width(profile: RegulationProfile, params: LayoutParams, mod) -> float:
    """Width of a cross-aisle collector — a two-way drive lane (~6 m)."""
    try:
        m = module_geometry(
            profile, LayoutType.STANDARD, 90.0, params.stall_width, AisleDir.TWO_WAY,
        )
        return m.aisle_width
    except Exception:
        return profile.fire_lane.min_width if profile.fire_lane else mod.aisle_width


# Max length of an uninterrupted stall row before a cross-aisle collector is
# inserted — keeps roads from running the whole length of a wide site.
_MAX_ROW_RUN = 40.0


def _max_dead_end(profile: RegulationProfile) -> float:
    fl = getattr(profile, "fire_lane", None)
    return getattr(fl, "max_dead_end", None) or 50.0


def _row_run(profile: RegulationProfile) -> float:
    return min(_MAX_ROW_RUN, _max_dead_end(profile))


def _cross_positions(
    xmin: float, xmax: float, cross_w: float, max_run: float,
) -> list[float]:
    """Pick x-positions (rotated frame) for cross-aisle collector lanes.

    Collectors are placed inset from both ends, with extra evenly-spaced ones so
    no continuous row run exceeds *max_run* — this stops a layout from producing
    one very long uninterrupted road across a wide site.  Returns [] when the
    span is too narrow for a proper lane.
    """
    span = xmax - xmin
    if span < cross_w * 2.0:
        return []
    lo = xmin + cross_w / 2.0
    hi = xmax - cross_w / 2.0
    if hi - lo < 1.0:
        return [(xmin + xmax) / 2.0]
    run = max(max_run, cross_w + 1.0)
    n_gaps = max(1, math.ceil((hi - lo) / run))
    step = (hi - lo) / n_gaps
    return [lo + i * step for i in range(n_gaps + 1)]


# ── Inner banded row engine ───────────────────────────────────────────────────

def _banded_rotated(
    work_r: Polygon,
    profile: RegulationProfile,
    params: LayoutParams,
    centroid,
    add_end_aisles: bool = True,
    aisle_zone_r: Polygon | None = None,
) -> tuple[list[Stall], list[DriveAisle]]:
    """Place double-loaded banded rows in the pre-rotated frame.

    *centroid* is the rotation origin used to convert stalls / aisles back to
    world coordinates.  Keeping centroid fixed across sub-polygon calls
    (e.g. from SPINE_BRANCHES or RING_INFILL) guarantees all geometry aligns
    to the same reference frame.

    *aisle_zone_r* (optional, default = work_r): polygon used for clipping
    aisle centerlines.  Pass a slightly larger polygon to extend cross-aisles
    beyond the stall-placement zone — used by RING_INFILL so interior
    cross-aisles reach the ring road centerline.
    """
    mod = module_geometry(
        profile, LayoutType.STANDARD,
        params.angle, params.stall_width, params.aisle_dir,
        aisle_width_override=params.aisle_width,
    )
    if aisle_zone_r is None:
        aisle_zone_r = work_r

    stalls: list[Stall] = []
    aisles: list[DriveAisle] = []
    xmin, ymin, xmax, ymax = work_r.bounds
    aisle_ys: list[float] = []

    # Cross-aisle collector lanes: choose their x-positions up front (a lane inset
    # from each end turns every row aisle into a through-corridor → connected
    # ladder).  Stalls the lanes run over are removed afterwards by
    # _clip_stalls_to_roads, so the lanes are proper full-width drive aisles.
    cross_w = _collector_width(profile, params, mod)
    cross_xs = (_cross_positions(xmin, xmax, cross_w, _row_run(profile))
                if add_end_aisles else [])

    def _place_row(row_y: float) -> None:
        x = xmin
        while x <= xmax:
            cell = stall_parallelogram(x, row_y, params.stall_width, params.stall_length, params.angle)
            if work_r.buffer(1e-6).contains(cell):
                stalls.append(Stall(
                    polygon=affinity.rotate(cell, params.orientation, origin=centroid),
                    type=StallType.STANDARD, angle=params.angle,
                ))
            x += mod.pitch

    def _add_aisle(cy: float) -> None:
        cl = LineString([(xmin, cy), (xmax, cy)]).intersection(aisle_zone_r)
        if cl.is_empty:
            return
        aisles.append(DriveAisle(
            centerline=affinity.rotate(cl, params.orientation, origin=centroid),
            width=mod.aisle_width, direction=params.aisle_dir,
        ))

    y = ymin
    while y + mod.width <= ymax:
        _place_row(y)
        _place_row(y + mod.stall_depth + mod.aisle_width)
        cy = y + mod.stall_depth + mod.aisle_width / 2
        aisle_ys.append(cy)
        _add_aisle(cy)
        y += mod.width

    remaining = ymax - y
    if remaining >= mod.stall_depth + mod.aisle_width / 2:
        _place_row(y)
        cy = y + mod.stall_depth + mod.aisle_width / 2
        aisle_ys.append(cy)
        _add_aisle(cy)

    # Cross-aisle collectors: two-way lanes spanning the row-aisle centerlines, so
    # they connect every row aisle without reaching past the outer rows.
    if cross_xs and len(aisle_ys) >= 2:
        for xc in cross_xs:
            cl = LineString([(xc, aisle_ys[0]), (xc, aisle_ys[-1])]).intersection(aisle_zone_r)
            if cl.is_empty:
                continue
            aisles.append(DriveAisle(
                centerline=affinity.rotate(cl, params.orientation, origin=centroid),
                width=cross_w, direction=AisleDir.TWO_WAY,
            ))

    return stalls, aisles


def _generate_banded(
    work: Polygon,
    profile: RegulationProfile,
    params: LayoutParams,
    add_end_aisles: bool = True,
) -> tuple[list[Stall], list[DriveAisle]]:
    centroid = work.centroid
    work_r = affinity.rotate(work, -params.orientation, origin=centroid, use_radians=False)
    return _banded_rotated(work_r, profile, params, centroid, add_end_aisles)


# ── Strategy 1: Fishbone / herringbone ────────────────────────────────────────

def _fishbone_rotated(
    work_r: Polygon,
    profile: RegulationProfile,
    params: LayoutParams,
    centroid,
    add_end_aisles: bool = True,
) -> tuple[list[Stall], list[DriveAisle]]:
    """Herringbone banding in the pre-rotated frame.

    Bottom rows use stall_parallelogram (lean left / normal);
    top rows use stall_parallelogram_mirrored (lean right).
    The resulting V-pattern at each aisle is the herringbone signature.
    """
    mod = module_geometry(
        profile, LayoutType.STANDARD,
        params.angle, params.stall_width, params.aisle_dir,
        aisle_width_override=params.aisle_width,
    )

    stalls: list[Stall] = []
    aisles: list[DriveAisle] = []
    xmin, ymin, xmax, ymax = work_r.bounds
    aisle_ys: list[float] = []

    # Cross-aisle collector lanes (see _banded_rotated).
    cross_w = _collector_width(profile, params, mod)
    cross_xs = (_cross_positions(xmin, xmax, cross_w, _row_run(profile))
                if add_end_aisles else [])

    def _place_row(row_y: float, mirrored: bool) -> None:
        fn = stall_parallelogram_mirrored if mirrored else stall_parallelogram
        x = xmin
        while x <= xmax:
            cell = fn(x, row_y, params.stall_width, params.stall_length, params.angle)
            if work_r.buffer(1e-6).contains(cell):
                stalls.append(Stall(
                    polygon=affinity.rotate(cell, params.orientation, origin=centroid),
                    type=StallType.STANDARD, angle=params.angle,
                ))
            x += mod.pitch

    def _add_aisle(cy: float) -> None:
        cl = LineString([(xmin, cy), (xmax, cy)]).intersection(work_r)
        if cl.is_empty:
            return
        aisles.append(DriveAisle(
            centerline=affinity.rotate(cl, params.orientation, origin=centroid),
            width=mod.aisle_width, direction=params.aisle_dir,
        ))

    y = ymin
    while y + mod.width <= ymax:
        _place_row(y, mirrored=False)                                      # bottom ↖
        _place_row(y + mod.stall_depth + mod.aisle_width, mirrored=True)  # top    ↗
        cy = y + mod.stall_depth + mod.aisle_width / 2
        aisle_ys.append(cy)
        _add_aisle(cy)
        y += mod.width

    remaining = ymax - y
    if remaining >= mod.stall_depth + mod.aisle_width / 2:
        _place_row(y, mirrored=False)
        cy = y + mod.stall_depth + mod.aisle_width / 2
        aisle_ys.append(cy)
        _add_aisle(cy)

    # Cross-aisle collectors spanning the row-aisle centerlines (see _banded_rotated).
    if cross_xs and len(aisle_ys) >= 2:
        for xc in cross_xs:
            cl = LineString([(xc, aisle_ys[0]), (xc, aisle_ys[-1])]).intersection(work_r)
            if cl.is_empty:
                continue
            aisles.append(DriveAisle(
                centerline=affinity.rotate(cl, params.orientation, origin=centroid),
                width=cross_w, direction=AisleDir.TWO_WAY,
            ))

    return stalls, aisles


def _generate_fishbone(
    work: Polygon,
    profile: RegulationProfile,
    params: LayoutParams,
    add_end_aisles: bool = True,
) -> tuple[list[Stall], list[DriveAisle]]:
    centroid = work.centroid
    work_r = affinity.rotate(work, -params.orientation, origin=centroid, use_radians=False)
    return _fishbone_rotated(work_r, profile, params, centroid, add_end_aisles)


# ── Strategy 2: Perimeter ring ────────────────────────────────────────────────

def _generate_perimeter_ring(
    work: Polygon,
    profile: RegulationProfile,
    params: LayoutParams,
) -> tuple[list[Stall], list[DriveAisle]]:
    """90° stalls along every edge; one ring road runs inside them."""
    stall_len = params.stall_length
    stall_w = params.stall_width
    aisle_w = _ring_aisle_width(profile, params)

    stalls = _place_perimeter_stalls(work, stall_len, stall_w)

    aisles: list[DriveAisle] = []
    ring_poly = offset_inward(work, stall_len + aisle_w / 2)
    if ring_poly and not ring_poly.is_empty:
        ring_cl = LineString(ring_poly.exterior.coords)
        aisles.append(DriveAisle(centerline=ring_cl, width=aisle_w, direction=params.aisle_dir))

    return stalls, aisles


# ── Strategy 3: Multi-ring (concentric) ──────────────────────────────────────

def _generate_multi_ring(
    work: Polygon,
    profile: RegulationProfile,
    params: LayoutParams,
) -> tuple[list[Stall], list[DriveAisle]]:
    """Concentric perimeter rings filling the polygon inward.

    Each ring iteration places inward-facing stalls and a closed ring road.
    Two adjacent rings share a ring road (double-loaded module, bent).

    Connectivity: two perpendicular spur corridors (aligned to the dominant
    polygon edge direction) cut through all stall rings and connect every
    ring road to its neighbours.  Stalls in the spur zone are omitted to
    keep the corridor physically passable.  The spur DriveAisles extend from
    the outer boundary to the centre, crossing all ring roads.
    """
    stall_len = params.stall_length
    stall_w = params.stall_width
    aisle_w = _ring_aisle_width(profile, params)
    ring_step = stall_len + aisle_w

    cx, cy = work.centroid.x, work.centroid.y
    far = max(work.bounds[2] - work.bounds[0], work.bounds[3] - work.bounds[1]) * 1.5

    # Two spur corridors: dominant polygon edge direction + perpendicular
    edge_dirs = polygon_edge_directions(work)
    base_alpha = math.radians(edge_dirs[0]) if edge_dirs else 0.0

    spur_data: list[tuple[float, Polygon]] = []
    for alpha in [base_alpha, base_alpha + math.pi / 2]:
        dx, dy = math.cos(alpha), math.sin(alpha)
        px, py = -dy * aisle_w / 2, dx * aisle_w / 2
        rect = Polygon([
            (cx + px - far * dx, cy + py - far * dy),
            (cx - px - far * dx, cy - py - far * dy),
            (cx - px + far * dx, cy - py + far * dy),
            (cx + px + far * dx, cy + py + far * dy),
        ])
        zone = work.intersection(rect)
        if not zone.is_empty:
            spur_data.append((alpha, zone))

    spur_zones = [z for _, z in spur_data]

    all_stalls: list[Stall] = []
    all_aisles: list[DriveAisle] = []
    current = work

    while True:
        for stall in _place_perimeter_stalls(current, stall_len, stall_w):
            if not any(stall.polygon.intersection(sz).area > 1e-4 for sz in spur_zones):
                all_stalls.append(stall)

        ring_cl_poly = offset_inward(current, stall_len + aisle_w / 2)
        if ring_cl_poly and not ring_cl_poly.is_empty:
            all_aisles.append(DriveAisle(
                centerline=LineString(ring_cl_poly.exterior.coords),
                width=aisle_w, direction=params.aisle_dir,
            ))

        next_poly = offset_inward(current, ring_step)
        if next_poly is None or next_poly.is_empty or next_poly.area < stall_w * stall_len * 2:
            break
        current = next_poly

    # Spur DriveAisles: straight two-way corridors connecting every ring road
    for alpha, _zone in spur_data:
        dx, dy = math.cos(alpha), math.sin(alpha)
        raw_cl = LineString([
            (cx - far * dx, cy - far * dy),
            (cx + far * dx, cy + far * dy),
        ])
        spur_cl = raw_cl.intersection(work)
        if not spur_cl.is_empty:
            all_aisles.append(DriveAisle(
                centerline=spur_cl, width=aisle_w, direction=AisleDir.TWO_WAY,
            ))

    return all_stalls, all_aisles


# ── Strategy 4: Ring + interior banded fill ───────────────────────────────────

def _generate_ring_infill(
    work: Polygon,
    profile: RegulationProfile,
    params: LayoutParams,
) -> tuple[list[Stall], list[DriveAisle]]:
    """Perimeter ring + banded interior aligned to the dominant polygon edge.

    Connectivity fix: the interior cross-aisles use an *expanded* aisle zone
    (offset to the ring road centreline = stall_len + aisle_w/2 from the
    boundary) so they extend through the ring-stall zone and meet the ring
    road, making every interior row aisle reachable from the ring road.
    """
    stall_len = params.stall_length
    aisle_w = _ring_aisle_width(profile, params)

    ring_stalls, ring_aisles = _generate_perimeter_ring(work, profile, params)

    # Stall placement zone: inner side of ring road
    interior = offset_inward(work, stall_len + aisle_w)
    if interior is None or interior.is_empty or interior.area < 1e-6:
        return ring_stalls, ring_aisles

    # Aisle routing zone: extends to the ring road centreline so cross-aisles
    # physically reach the ring road and the network is connected
    aisle_zone = offset_inward(work, stall_len + aisle_w / 2)
    if aisle_zone is None or aisle_zone.is_empty:
        aisle_zone = interior

    edge_dirs = polygon_edge_directions(work)
    # Honour the requested interior orientation (explore varies it); fall back to
    # the dominant polygon edge when unset.
    fill_orientation = params.orientation or (edge_dirs[0] if edge_dirs else 0.0)

    interior_params = LayoutParams(
        orientation=fill_orientation,
        layout_type=LayoutType.STANDARD,
        angle=params.angle,
        stall_width=params.stall_width,
        stall_length=params.stall_length,
        aisle_dir=params.aisle_dir,
        aisle_width=params.aisle_width,
    )

    if isinstance(interior, (MultiPolygon, GeometryCollection)):
        components = [g for g in interior.geoms if isinstance(g, Polygon) and g.area > 1e-6]
    else:
        components = [interior]

    centroid = work.centroid  # consistent rotation origin across all sub-polygons
    int_stalls: list[Stall] = []
    int_aisles: list[DriveAisle] = []
    for component in components:
        comp_r = affinity.rotate(component, -fill_orientation, origin=centroid, use_radians=False)
        az_r = affinity.rotate(aisle_zone, -fill_orientation, origin=centroid, use_radians=False)
        # No interior end-aisles: every interior row-aisle already extends out to
        # the ring road (via the expanded aisle zone), so the ring *is* the
        # collector.  Dropping the duplicate end-aisles removes redundant tarmac
        # and lets the network stitcher keep one clean connected loop.
        s, a = _banded_rotated(
            comp_r, profile, interior_params, centroid,
            add_end_aisles=False, aisle_zone_r=az_r,
        )
        int_stalls.extend(s)
        int_aisles.extend(a)

    return ring_stalls + int_stalls, ring_aisles + int_aisles


# ── Strategy 5: Spine + branch aisles ────────────────────────────────────────

def _generate_spine_branches(
    work: Polygon,
    profile: RegulationProfile,
    params: LayoutParams,
) -> tuple[list[Stall], list[DriveAisle]]:
    """Wide central collector aisle + perpendicular double-loaded branch aisles.

    In the rotated frame (aisles run along x):
      * A horizontal spine of width spine_w runs through the polygon mid-height.
      * The polygon is clipped into upper and lower halves at the spine edges.
      * Each half is filled with banded rows using _banded_rotated() so stalls
        from both halves share the same rotation origin (no drift).
    The spine connects all branch aisles — the network is fully connected.
    """
    centroid = work.centroid
    work_r = affinity.rotate(work, -params.orientation, origin=centroid, use_radians=False)
    xmin, ymin, xmax, ymax = work_r.bounds

    aisle_w = _ring_aisle_width(profile, params)
    spine_w = max(6.0, aisle_w * 1.5)
    y_mid = (ymin + ymax) / 2
    spine_bot = y_mid - spine_w / 2
    spine_top = y_mid + spine_w / 2

    # Spine aisle (in rotated frame → world)
    spine_cl_r = LineString([(xmin, y_mid), (xmax, y_mid)]).intersection(work_r)
    aisles: list[DriveAisle] = []
    if not spine_cl_r.is_empty:
        aisles.append(DriveAisle(
            centerline=affinity.rotate(spine_cl_r, params.orientation, origin=centroid),
            width=spine_w, direction=AisleDir.TWO_WAY,
        ))

    stalls: list[Stall] = []
    for y_lo, y_hi in [(ymin, spine_bot), (spine_top, ymax)]:
        if y_hi - y_lo < 2.0:
            continue
        clip = _box(xmin - 1, y_lo, xmax + 1, y_hi)
        sub_r = work_r.intersection(clip)
        if sub_r.is_empty:
            continue

        # Handle geometry collections from intersection
        sub_polys: list[Polygon]
        if isinstance(sub_r, (MultiPolygon, GeometryCollection)):
            sub_polys = [g for g in sub_r.geoms if isinstance(g, Polygon) and g.area > 1.0]
        elif isinstance(sub_r, Polygon) and sub_r.area > 1.0:
            sub_polys = [sub_r]
        else:
            continue

        for sub in sub_polys:
            s, a = _banded_rotated(sub, profile, params, centroid, add_end_aisles=True)
            stalls.extend(s)
            aisles.extend(a)

    return stalls, aisles


# ── Strategy 6: Mixed-angle banding ──────────────────────────────────────────

def _generate_mixed_angle(
    work: Polygon,
    profile: RegulationProfile,
    params: LayoutParams,
) -> tuple[list[Stall], list[DriveAisle]]:
    """Splits the work area into two horizontal bands at different stall angles.

    Tries all pairs from the profile's available angles at three split ratios
    (1/3, 1/2, 2/3 of the bounding-box height) and returns the combination
    that yields the most stalls.  Useful for elongated sites where mixing
    e.g. 60° + 90° beats a pure single-angle layout.
    """
    centroid = work.centroid
    work_r = affinity.rotate(work, -params.orientation, origin=centroid, use_radians=False)
    xmin, ymin, xmax, ymax = work_r.bounds
    height = ymax - ymin

    available_angles = sorted(
        float(k) for k in profile.aisles if k.isdigit()
    )
    if len(available_angles) < 2:
        return _banded_rotated(work_r, profile, params, centroid)

    best_stalls: list[Stall] = []
    best_aisles: list[DriveAisle] = []
    best_count = -1

    for split_frac in (1 / 3, 1 / 2, 2 / 3):
        y_split = ymin + height * split_frac
        bot_clip = _box(xmin - 1, ymin - 1, xmax + 1, y_split)
        top_clip = _box(xmin - 1, y_split, xmax + 1, ymax + 1)
        bottom_r = work_r.intersection(bot_clip)
        top_r = work_r.intersection(top_clip)
        if bottom_r.is_empty or top_r.is_empty:
            continue

        for angle1 in available_angles:
            for angle2 in available_angles:
                if angle1 == angle2:
                    continue
                adir1 = AisleDir.ONE_WAY if angle1 < 90 else AisleDir.TWO_WAY
                adir2 = AisleDir.ONE_WAY if angle2 < 90 else AisleDir.TWO_WAY
                p1 = replace(params, angle=angle1, aisle_dir=adir1)
                p2 = replace(params, angle=angle2, aisle_dir=adir2)

                trial_stalls: list[Stall] = []
                trial_aisles: list[DriveAisle] = []
                for sub_r, p in ((bottom_r, p1), (top_r, p2)):
                    if isinstance(sub_r, Polygon) and sub_r.area > 1.0:
                        s, a = _banded_rotated(sub_r, profile, p, centroid,
                                               add_end_aisles=True)
                        trial_stalls.extend(s)
                        trial_aisles.extend(a)
                    elif isinstance(sub_r, (MultiPolygon, GeometryCollection)):
                        for g in sub_r.geoms:
                            if isinstance(g, Polygon) and g.area > 1.0:
                                s, a = _banded_rotated(g, profile, p, centroid,
                                                       add_end_aisles=True)
                                trial_stalls.extend(s)
                                trial_aisles.extend(a)

                if len(trial_stalls) > best_count:
                    best_count = len(trial_stalls)
                    best_stalls = trial_stalls
                    best_aisles = trial_aisles

    return best_stalls, best_aisles


# ── Strategy 7: Polygon subdivision ──────────────────────────────────────────

_MIN_REGION_AREA = 60.0   # m² — below this a sub-region isn't worth its own fill


def _reflex_points(poly: Polygon) -> list[tuple[float, float]]:
    """Vertices of *poly* where the interior angle exceeds 180° (concave corners).

    These are the natural places to cut a non-convex polygon into simpler pieces.
    """
    coords = list(poly.exterior.coords)[:-1]
    n = len(coords)
    if n < 4:
        return []
    # Signed area → winding direction
    area2 = sum(
        coords[i][0] * coords[(i + 1) % n][1] - coords[(i + 1) % n][0] * coords[i][1]
        for i in range(n)
    )
    ccw = area2 > 0
    reflex: list[tuple[float, float]] = []
    for i in range(n):
        ax, ay = coords[(i - 1) % n]
        bx, by = coords[i]
        cx, cy = coords[(i + 1) % n]
        cross = (bx - ax) * (cy - by) - (by - ay) * (cx - bx)
        is_reflex = (cross < 0) if ccw else (cross > 0)
        if is_reflex:
            reflex.append((bx, by))
    return reflex


def _rectangularity(poly: Polygon) -> float:
    """How close a polygon is to its minimum bounding rectangle (1.0 = perfect)."""
    mrr = poly.minimum_rotated_rectangle
    if mrr.area < 1e-9:
        return 0.0
    return poly.area / mrr.area


def _cut_line(px: float, py: float, angle_deg: float, work: Polygon) -> LineString:
    """A line through (px, py) at *angle_deg*, long enough to span *work*."""
    minx, miny, maxx, maxy = work.bounds
    diag = 2.0 * math.hypot(maxx - minx, maxy - miny) + 10.0
    t = math.radians(angle_deg)
    dx, dy = math.cos(t), math.sin(t)
    return LineString([
        (px - dx * diag, py - dy * diag),
        (px + dx * diag, py + dy * diag),
    ])


def _safe_split(work: Polygon, line: LineString) -> list[Polygon]:
    """Split *work* by *line*; return the resulting polygon pieces above the area floor."""
    try:
        result = shapely_split(work, line)
    except Exception:
        return [work]
    polys = [
        g for g in getattr(result, "geoms", [result])
        if isinstance(g, Polygon) and g.area > _MIN_REGION_AREA
    ]
    return polys if len(polys) >= 2 else [work]


def _decompose(
    work: Polygon,
    dominant_angle: float,
    max_depth: int = 2,
    _depth: int = 0,
) -> list[Polygon]:
    """Recursively cut a non-convex polygon into more rectangular sub-regions.

    At each reflex vertex we try a cut parallel and perpendicular to the dominant
    edge direction, and keep the cut that makes the pieces most rectangular
    (area-weighted).  Recurses up to *max_depth* levels → at most 2^depth regions.
    Convex polygons (no reflex vertices) are returned unchanged.
    """
    if _depth >= max_depth:
        return [work]
    reflex = _reflex_points(work)
    if not reflex:
        return [work]

    best_score = _rectangularity(work) * work.area
    best_pieces: list[Polygon] | None = None
    for (rx, ry) in reflex:
        for direction in (dominant_angle, dominant_angle + 90.0):
            pieces = _safe_split(work, _cut_line(rx, ry, direction, work))
            if len(pieces) < 2:
                continue
            score = sum(_rectangularity(p) * p.area for p in pieces)
            if score > best_score:
                best_score = score
                best_pieces = pieces

    if best_pieces is None:
        return [work]

    result: list[Polygon] = []
    for p in best_pieces:
        result.extend(_decompose(p, dominant_angle, max_depth, _depth + 1))
    return result


def _angle_program(params: LayoutParams, profile: RegulationProfile) -> list[float]:
    """Resolve the per-region angle choices from an explore task's params.

    params.angle > 0  → a single fixed program angle (e.g. 90° max-capacity).
    params.angle == 0 → "auto": try every available module angle and keep the
                        densest per region.
    """
    available = [a for a in (90.0, 60.0, 45.0) if str(int(a)) in profile.aisles]
    if not available:
        available = [90.0]
    if params.angle and params.angle > 0:
        wanted = float(params.angle)
        return [wanted] if wanted in available else [min(available)]
    return available


def _best_layout_for_region(
    region: Polygon,
    profile: RegulationProfile,
    params: LayoutParams,
    angle_choices: list[float],
    axis: float,
) -> tuple[list[Stall], list[DriveAisle]]:
    """Fill one region with the densest double-loaded module (banded or herringbone).

    Tries the requested aisle *axis* plus the region's own dominant edge direction,
    across the allowed *angle_choices*, with both banded and herringbone modules —
    keeping whichever places the most stalls.
    """
    edge_dirs = polygon_edge_directions(region)
    orients: list[float] = [axis]
    for d in edge_dirs[:2]:
        if all(round(d) != round(o % 180.0) for o in orients):
            orients.append(d)

    best_s: list[Stall] = []
    best_a: list[DriveAisle] = []
    best_n = -1
    for orient in orients:
        for angle in angle_choices:
            adir = AisleDir.ONE_WAY if angle < 90 else AisleDir.TWO_WAY
            p = replace(
                params, orientation=orient, angle=angle, aisle_dir=adir,
                layout_type=LayoutType.STANDARD,
            )
            for fn in (_generate_banded, _generate_fishbone):
                try:
                    s, a = fn(region, profile, p)
                except Exception:
                    continue
                if len(s) > best_n:
                    best_n, best_s, best_a = len(s), s, a
    return best_s, best_a


def _generate_subdivided(
    work: Polygon,
    profile: RegulationProfile,
    params: LayoutParams,
) -> tuple[list[Stall], list[DriveAisle]]:
    """Decompose the work area into regions and fill each with the best module.

    This is the tool's primary, buildable strategy.  Non-convex sites (L-shapes,
    notched lots) are split at reflex vertices into near-rectangular regions; each
    region is filled with the densest perpendicular/angled double-loaded module,
    aligned to ``params.orientation``.  ``params.angle`` selects the angle program
    (a fixed angle, or 0 = best-per-region).  Regions tile the polygon, so their
    two-way cross-aisles meet along shared edges; the merged network is then
    stitched and entrance-connected by ``generate()``.
    """
    axis = params.orientation
    angle_choices = _angle_program(params, profile)
    regions = _decompose(work, axis)

    all_stalls: list[Stall] = []
    all_aisles: list[DriveAisle] = []
    for region in regions:
        s, a = _best_layout_for_region(region, profile, params, angle_choices, axis)
        all_stalls.extend(s)
        all_aisles.extend(a)
    return all_stalls, all_aisles


# ── Perimeter stall placement helpers ────────────────────────────────────────

def _place_perimeter_stalls(
    work: Polygon,
    stall_len: float,
    stall_w: float,
) -> list[Stall]:
    """Place 90° stalls inward along every polygon edge.

    Starts at t = stall_w/2 from each vertex (corner clearance for the ring
    road).  Incremental overlap check avoids corner conflicts between edges.
    """
    placed: list[Stall] = []
    coords = list(work.exterior.coords)

    for i in range(len(coords) - 1):
        p1 = coords[i]
        p2 = coords[i + 1]
        dx = p2[0] - p1[0]
        dy = p2[1] - p1[1]
        edge_len = math.hypot(dx, dy)
        if edge_len < stall_w * 2 + 1e-6:
            continue

        ex, ey = dx / edge_len, dy / edge_len
        nx, ny = -ey, ex  # inward normal (CCW exterior)

        mid_x = (p1[0] + p2[0]) / 2
        mid_y = (p1[1] + p2[1]) / 2
        if not work.contains(Point(mid_x + nx * 0.1, mid_y + ny * 0.1)):
            nx, ny = -nx, -ny

        t = stall_w / 2
        end_t = edge_len - stall_w / 2
        while t + stall_w <= end_t + 1e-9:
            ax = p1[0] + t * ex
            ay = p1[1] + t * ey
            bx = p1[0] + (t + stall_w) * ex
            by = p1[1] + (t + stall_w) * ey
            cx = bx + stall_len * nx
            cy = by + stall_len * ny
            dx2 = ax + stall_len * nx
            dy2 = ay + stall_len * ny
            poly = Polygon([(ax, ay), (bx, by), (cx, cy), (dx2, dy2)])

            if (work.intersection(poly).area >= 0.999 * poly.area
                    and not _overlaps_any(poly, placed)):
                placed.append(Stall(polygon=poly, type=StallType.STANDARD, angle=90.0))

            t += stall_w

    return _filter_perimeter_access(placed)


def _access_clearance_rect(poly: Polygon, clear: float) -> Polygon | None:
    """Thin rectangle just in front of a perimeter stall (its driving mouth)."""
    coords = list(poly.exterior.coords)
    if len(coords) < 4:
        return None
    o1, o2, i2, i1 = coords[0], coords[1], coords[2], coords[3]
    ox, oy = (o1[0] + o2[0]) / 2, (o1[1] + o2[1]) / 2     # outer mid (boundary side)
    ix, iy = (i1[0] + i2[0]) / 2, (i1[1] + i2[1]) / 2     # inner mid (interior side)
    nx, ny = ix - ox, iy - oy
    L = math.hypot(nx, ny)
    if L < 1e-9:
        return None
    nx, ny = nx / L, ny / L
    return Polygon([
        i1, i2,
        (i2[0] + nx * clear, i2[1] + ny * clear),
        (i1[0] + nx * clear, i1[1] + ny * clear),
    ])


def _filter_perimeter_access(
    stalls: list[Stall], clear: float = 2.0, frac: float = 0.2,
) -> list[Stall]:
    """Drop perimeter stalls whose access is blocked by another stall.

    At sharp/acute corners the stalls from one edge can swing round in front of
    the stalls on the adjacent edge — those rear stalls are unreachable.  We test
    each stall's driving mouth (a thin strip just inside it); if another stall
    covers more than *frac* of it, the stall is inaccessible and removed.
    """
    if len(stalls) < 2:
        return stalls
    rects = [_access_clearance_rect(s.polygon, clear) for s in stalls]
    kept: list[Stall] = []
    for k, s in enumerate(stalls):
        rect = rects[k]
        if rect is None or rect.area < 1e-9:
            kept.append(s)
            continue
        blocked = any(
            j != k and stalls[j].polygon.intersection(rect).area > frac * rect.area
            for j in range(len(stalls))
        )
        if not blocked:
            kept.append(s)
    return kept


def _overlaps_any(poly: Polygon, stalls: list[Stall]) -> bool:
    for s in stalls:
        if poly.intersects(s.polygon) and poly.intersection(s.polygon).area > 1e-4:
            return True
    return False


def _ring_aisle_width(profile: RegulationProfile, params: LayoutParams) -> float:
    if params.aisle_width is not None:
        return params.aisle_width
    spec = profile.aisles.get("90")
    if spec is None:
        raise ValueError(f"No 90° aisle spec in profile {profile.id!r}")
    if params.aisle_dir == AisleDir.TWO_WAY and spec.two_way is not None:
        return spec.two_way
    if spec.one_way is not None:
        return spec.one_way
    return spec.two_way or 6.0


def _empty_layout(site: Site, params: LayoutParams, profile: RegulationProfile) -> Layout:
    return Layout(
        stalls=[],
        aisles=[],
        entrances=site.entrances,
        metrics=Metrics(
            total_stalls=0,
            by_type={},
            gross_area_per_stall=0.0,
            site_area=site.boundary.area,
        ),
        params=params,
        profile_id=profile.id,
    )
