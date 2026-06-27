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
from dataclasses import dataclass, replace
from typing import Callable

from shapely import affinity
from shapely.geometry import (
    GeometryCollection,
    LineString,
    MultiPolygon,
    Point,
    Polygon,
    box as _box,
)
from shapely.ops import unary_union

from parking_solver.core import scorer
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
    else:
        stalls, aisles = _generate_banded(work, profile, params)

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


def generate_all(
    site: Site,
    profile: RegulationProfile,
    stall_width: float = 2.5,
    stall_length: float = 5.0,
    fixed: FixedElements | None = None,
    progress_callback: Callable[[int, int], None] | None = None,
) -> list[StrategyResult]:
    """Exhaustive exploration across all strategies, characteristic orientations,
    and stall angles.  Returns results sorted by stall count (descending).

    progress_callback(done, total) is called after each task if provided.
    """
    work = _compute_work(site, profile, fixed)
    if work is None or work.is_empty:
        return []

    edge_dirs = polygon_edge_directions(work)
    orientations: list[float] = edge_dirs[:4] if edge_dirs else [0.0]

    # Ring strategies are rotationally symmetric — only one orientation needed
    RING_STRATEGIES = {LayoutType.PERIMETER_RING, LayoutType.MULTI_RING}
    ANGLES_ALL = [45.0, 60.0, 75.0, 90.0]

    tasks: list[tuple] = []
    seen: set[tuple] = set()
    for lt in LayoutType:
        orients = [0.0] if lt in RING_STRATEGIES else orientations
        for orientation in orients:
            for angle in ANGLES_ALL:
                key = (lt, round(orientation), round(angle))
                if key in seen:
                    continue
                seen.add(key)
                adir = AisleDir.ONE_WAY if angle < 90 else AisleDir.TWO_WAY
                tasks.append((lt, orientation, angle, stall_width, adir))

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
                results.append(StrategyResult(
                    layout_type=lt,
                    orientation=orientation,
                    angle=angle,
                    stall_width=sw,
                    layout=layout,
                ))
        except Exception:
            pass

    if progress_callback:
        progress_callback(len(tasks), len(tasks))

    return sorted(results, key=lambda r: r.stall_count, reverse=True)


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


# ── Inner banded row engine ───────────────────────────────────────────────────

def _banded_rotated(
    work_r: Polygon,
    profile: RegulationProfile,
    params: LayoutParams,
    centroid,
    add_end_aisles: bool = True,
) -> tuple[list[Stall], list[DriveAisle]]:
    """Place double-loaded banded rows in the pre-rotated frame.

    *centroid* is the rotation origin used to convert stalls / aisles back to
    world coordinates.  Keeping centroid fixed across sub-polygon calls
    (e.g. from SPINE_BRANCHES or RING_INFILL) guarantees all geometry aligns
    to the same reference frame.
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

    def _place_row(row_y: float) -> None:
        x = xmin
        while x <= xmax:
            cell = stall_parallelogram(x, row_y, params.stall_width, params.stall_length, params.angle)
            if cell.intersection(work_r).area >= 0.999 * cell.area:
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

    if add_end_aisles and aisle_ys:
        y_lo = aisle_ys[0] - mod.aisle_width / 2
        y_hi = aisle_ys[-1] + mod.aisle_width / 2
        for x_pos in (xmin, xmax):
            cl = LineString([(x_pos, y_lo), (x_pos, y_hi)]).intersection(work_r)
            if cl.is_empty:
                continue
            aisles.append(DriveAisle(
                centerline=affinity.rotate(cl, params.orientation, origin=centroid),
                width=mod.aisle_width, direction=params.aisle_dir,
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

    def _place_row(row_y: float, mirrored: bool) -> None:
        fn = stall_parallelogram_mirrored if mirrored else stall_parallelogram
        x = xmin
        while x <= xmax:
            cell = fn(x, row_y, params.stall_width, params.stall_length, params.angle)
            if cell.intersection(work_r).area >= 0.999 * cell.area:
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

    if add_end_aisles and aisle_ys:
        y_lo = aisle_ys[0] - mod.aisle_width / 2
        y_hi = aisle_ys[-1] + mod.aisle_width / 2
        for x_pos in (xmin, xmax):
            cl = LineString([(x_pos, y_lo), (x_pos, y_hi)]).intersection(work_r)
            if cl.is_empty:
                continue
            aisles.append(DriveAisle(
                centerline=affinity.rotate(cl, params.orientation, origin=centroid),
                width=mod.aisle_width, direction=params.aisle_dir,
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

    Each iteration places one ring of inward-facing stalls and one ring road.
    Two adjacent iterations share a ring road — the outer ring's inner face
    and the next ring's outer face both open onto it (double-loaded module
    wrapped into a closed ring).
    """
    stall_len = params.stall_length
    stall_w = params.stall_width
    aisle_w = _ring_aisle_width(profile, params)
    ring_step = stall_len + aisle_w

    all_stalls: list[Stall] = []
    all_aisles: list[DriveAisle] = []
    current = work

    while True:
        ring_stalls = _place_perimeter_stalls(current, stall_len, stall_w)
        if not ring_stalls:
            break
        all_stalls.extend(ring_stalls)

        ring_cl_poly = offset_inward(current, stall_len + aisle_w / 2)
        if ring_cl_poly and not ring_cl_poly.is_empty:
            ring_cl = LineString(ring_cl_poly.exterior.coords)
            all_aisles.append(
                DriveAisle(centerline=ring_cl, width=aisle_w, direction=params.aisle_dir)
            )

        next_poly = offset_inward(current, ring_step)
        if next_poly is None or next_poly.is_empty or next_poly.area < stall_w * stall_len * 2:
            break
        current = next_poly

    return all_stalls, all_aisles


# ── Strategy 4: Ring + interior banded fill ───────────────────────────────────

def _generate_ring_infill(
    work: Polygon,
    profile: RegulationProfile,
    params: LayoutParams,
) -> tuple[list[Stall], list[DriveAisle]]:
    """Perimeter ring + banded interior aligned to the dominant polygon edge."""
    stall_len = params.stall_length
    aisle_w = _ring_aisle_width(profile, params)

    ring_stalls, ring_aisles = _generate_perimeter_ring(work, profile, params)

    interior = offset_inward(work, stall_len + aisle_w)
    if interior is None or interior.is_empty or interior.area < 1e-6:
        return ring_stalls, ring_aisles

    edge_dirs = polygon_edge_directions(work)
    fill_orientation = edge_dirs[0] if edge_dirs else params.orientation

    interior_params = LayoutParams(
        orientation=fill_orientation,
        layout_type=LayoutType.STANDARD,
        angle=params.angle,
        stall_width=params.stall_width,
        stall_length=params.stall_length,
        aisle_dir=params.aisle_dir,
        aisle_width=params.aisle_width,
    )

    components: list[Polygon]
    if isinstance(interior, (MultiPolygon, GeometryCollection)):
        components = [g for g in interior.geoms if isinstance(g, Polygon) and g.area > 1e-6]
    else:
        components = [interior]

    int_stalls: list[Stall] = []
    int_aisles: list[DriveAisle] = []
    for component in components:
        s, a = _generate_banded(component, profile, interior_params, add_end_aisles=True)
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

    return placed


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
