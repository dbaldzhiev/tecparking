from __future__ import annotations

from shapely import affinity
from shapely.geometry import LineString
from shapely.ops import unary_union

from parking_solver.core import scorer
from parking_solver.core.geometry.helpers import offset_inward, stall_parallelogram
from parking_solver.core.model import (
    DriveAisle,
    FixedElements,
    Layout,
    LayoutParams,
    Metrics,
    Site,
    Stall,
    StallType,
)
from parking_solver.core.regulations.engine import RegulationProfile, module_geometry


def generate(
    site: Site,
    profile: RegulationProfile,
    params: LayoutParams,
    fixed: FixedElements | None = None,
) -> Layout:
    """Generate a double-loaded parking layout via banding.

    Supports all stall angles (45/60/75/90°) via the rotate-to-axis-align trick.
    If *fixed* contains locked stalls, their footprints are subtracted from the
    work area so newly generated stalls flow around them.
    """
    # 1. Work area
    work = offset_inward(site.boundary, site.setbacks)
    if site.obstacles:
        work = work.difference(unary_union(site.obstacles))
    if fixed and fixed.stalls:
        obstacle = fixed.as_obstacle_union(clearance=profile.overhang_allowance)
        if obstacle is not None:
            work = work.difference(obstacle)
    if work.is_empty or work.area < 1e-6:
        return _empty_layout(site, params, profile)

    # 2. Rotate so aisle runs along +x
    centroid = work.centroid
    work_r = affinity.rotate(work, -params.orientation, origin=centroid, use_radians=False)

    # 3. Module geometry (angle-aware)
    aisle_override = params.aisle_width  # None → take from profile
    mod = module_geometry(
        profile,
        params.layout_type,
        params.angle,
        params.stall_width,
        params.aisle_dir,
        aisle_width_override=aisle_override,
    )

    # 4. Band across the axis-aligned bounding box
    stalls: list[Stall] = []
    aisle_lines: list[DriveAisle] = []
    xmin, ymin, xmax, ymax = work_r.bounds

    y = ymin
    while y + mod.width <= ymax:
        # Two rows: bottom at y, top at y + stall_depth + aisle_width
        for row_y in (y, y + mod.stall_depth + mod.aisle_width):
            x = xmin
            while x <= xmax:
                cell = stall_parallelogram(
                    x, row_y, params.stall_width, params.stall_length, params.angle
                )
                if cell.intersection(work_r).area >= 0.999 * cell.area:
                    stall_world = affinity.rotate(
                        cell, params.orientation, origin=centroid, use_radians=False
                    )
                    stalls.append(
                        Stall(polygon=stall_world, type=StallType.STANDARD, angle=params.angle)
                    )
                x += mod.pitch

        # Aisle centerline (horizontal mid-line of the aisle strip)
        aisle_cy = y + mod.stall_depth + mod.aisle_width / 2
        raw_cl = LineString([(xmin, aisle_cy), (xmax, aisle_cy)])
        cl_clipped = raw_cl.intersection(work_r)
        if not cl_clipped.is_empty:
            cl_world = affinity.rotate(
                cl_clipped, params.orientation, origin=centroid, use_radians=False
            )
            aisle_lines.append(
                DriveAisle(centerline=cl_world, width=mod.aisle_width, direction=params.aisle_dir)
            )

        y += mod.width

    # Re-include locked stalls unchanged
    if fixed and fixed.stalls:
        stalls = fixed.stalls + stalls

    metrics = scorer.score(stalls, site)
    return Layout(
        stalls=stalls,
        aisles=aisle_lines,
        entrances=site.entrances,
        metrics=metrics,
        params=params,
        profile_id=profile.id,
    )


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
