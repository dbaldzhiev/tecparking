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
