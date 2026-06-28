from __future__ import annotations

import math

import pyclipper
from shapely import affinity
from shapely.geometry import Polygon

_CLIPPER_SCALE = 1_000_000  # 1 µm resolution in metres


def stall_parallelogram(
    x: float,
    row_y: float,
    stall_width: float,
    stall_length: float,
    theta_deg: float,
) -> Polygon:
    """Parallelogram stall footprint in the rotated (axis-aligned) frame.

    Long axis goes up-and-left at angle theta_deg from the aisle (x-axis).
    theta_deg=90 → perpendicular (= a plain rectangle).
    """
    t = math.radians(theta_deg)
    dl = (-math.cos(t), math.sin(t))   # long axis direction
    dw = (math.sin(t), math.cos(t))    # width axis direction (rightward)

    c0 = (x, row_y)
    c1 = (x + stall_width * dw[0], row_y + stall_width * dw[1])
    c2 = (c1[0] + stall_length * dl[0], c1[1] + stall_length * dl[1])
    c3 = (x + stall_length * dl[0], row_y + stall_length * dl[1])
    return Polygon([c0, c1, c2, c3])


def stall_parallelogram_mirrored(
    x: float,
    row_y: float,
    stall_width: float,
    stall_length: float,
    theta_deg: float,
) -> Polygon:
    """Right-leaning stall for top rows in fishbone/herringbone layouts.

    This is the horizontal reflection of stall_parallelogram.  At any angle:
      * area = stall_width × stall_length  (proven analytically)
      * adjacent mirrored stalls at pitch W/sin(θ) share a boundary edge but
        do not overlap — same non-overlap guarantee as the normal stall
    theta_deg=90 → plain rectangle, identical to stall_parallelogram.
    """
    t = math.radians(theta_deg)
    s, c = math.sin(t), math.cos(t)
    W, L = stall_width, stall_length
    r = row_y

    r0 = (x + W * s, r)
    r1 = (x, r + W * c)
    r2 = (x + L * c, r + W * c + L * s)
    r3 = (x + W * s + L * c, r + L * s)
    return Polygon([r0, r1, r2, r3])


def offset_inward(polygon: Polygon, distance: float) -> Polygon:
    """Shrink polygon inward by distance using pyclipper (Clipper2)."""
    if distance <= 0:
        return polygon

    coords = list(polygon.exterior.coords[:-1])
    path = [[int(round(x * _CLIPPER_SCALE)), int(round(y * _CLIPPER_SCALE))] for x, y in coords]

    pco = pyclipper.PyclipperOffset()
    pco.AddPath(path, pyclipper.JT_MITER, pyclipper.ET_CLOSEDPOLYGON)
    results = pco.Execute(int(-distance * _CLIPPER_SCALE))

    if not results:
        return Polygon()

    def _unscale(p):
        return [(x / _CLIPPER_SCALE, y / _CLIPPER_SCALE) for x, y in p]

    outer = _unscale(results[0])
    holes = [_unscale(r) for r in results[1:]]
    return Polygon(outer, holes)


def rotate_geom(geom, angle_deg: float, origin):
    """Rotate geometry by angle_deg (CCW) around origin."""
    return affinity.rotate(geom, angle_deg, origin=origin, use_radians=False)


def longest_edge_midpoint(polygon: Polygon) -> tuple[float, float]:
    """Midpoint of the polygon's longest exterior edge.

    Used to position a default site entrance: the longest edge is almost always
    the street frontage where vehicles enter.  Returns the point in world coords.
    """
    coords = list(polygon.exterior.coords)
    best_len = -1.0
    best_mid = (polygon.centroid.x, polygon.centroid.y)
    for i in range(len(coords) - 1):
        (x0, y0), (x1, y1) = coords[i], coords[i + 1]
        length = math.hypot(x1 - x0, y1 - y0)
        if length > best_len:
            best_len = length
            best_mid = ((x0 + x1) / 2.0, (y0 + y1) / 2.0)
    return best_mid


def polygon_edge_directions(polygon: Polygon) -> list[float]:
    """Return unique edge orientations in [0°, 180°), longest total edge-length first.

    Each value is a candidate *orientation* for the banding generator: passing it
    as LayoutParams.orientation makes aisles run parallel to that polygon edge.
    """
    coords = list(polygon.exterior.coords)
    by_angle: dict[int, float] = {}

    for i in range(len(coords) - 1):
        dx = coords[i + 1][0] - coords[i][0]
        dy = coords[i + 1][1] - coords[i][1]
        length = math.hypot(dx, dy)
        if length < 1e-6:
            continue
        # Fold to [0°, 180°): parallel and anti-parallel edges share the same orientation
        ang = math.degrees(math.atan2(dy, dx)) % 180.0
        key = round(ang)
        by_angle[key] = by_angle.get(key, 0.0) + length

    return [float(a) for a, _ in sorted(by_angle.items(), key=lambda kv: -kv[1])]
