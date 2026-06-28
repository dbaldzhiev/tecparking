"""Containment compliance — every stall and every road (full width) inside the site."""
from __future__ import annotations

from pathlib import Path

import pytest
from shapely.geometry import Point, Polygon, box

from parking_solver.core.generator import generate, generate_all
from parking_solver.core.model import (
    AisleDir,
    Entrance,
    EntranceKind,
    LayoutParams,
    LayoutType,
    Site,
)
from parking_solver.core.regulations.engine import load_profile

_PROFILE = (
    Path(__file__).parent.parent
    / "parking_solver" / "core" / "regulations" / "profiles" / "bulgarian.yaml"
)

_SHAPES = {
    "rect": box(0, 0, 60, 40),
    "lshape": Polygon([(0, 0), (80, 0), (80, 25), (45, 25), (45, 50), (0, 50)]),
    "octagon": Polygon([(20, 0), (80, 0), (100, 20), (100, 60),
                        (80, 80), (20, 80), (0, 60), (0, 20)]),
    "narrow": box(0, 0, 120, 16),
}


@pytest.fixture(scope="module")
def profile():
    return load_profile(_PROFILE)


def _site(poly: Polygon) -> Site:
    from parking_solver.core.geometry.helpers import longest_edge_midpoint
    mx, my = longest_edge_midpoint(poly)   # how the app places the default entrance
    return Site(
        boundary=poly,
        entrances=[Entrance(point=Point(mx, my), kind=EntranceKind.SITE)],
    )


def _assert_inside(layout, boundary, label):
    b = boundary.buffer(1e-4)   # tiny tolerance for float noise
    for i, s in enumerate(layout.stalls):
        assert b.contains(s.polygon), f"{label}: stall {i} pokes outside the boundary"
    for j, a in enumerate(layout.aisles):
        band = a.centerline.buffer(a.width / 2.0)
        # Entrance driveways legitimately reach the boundary (the gate); exempt
        # any road whose band touches an entrance point.  Every other road must
        # stay fully inside.
        if any(band.distance(e.point) < 0.5 for e in layout.entrances):
            continue
        outside = band.difference(b).area
        assert outside < 1e-2, \
            f"{label}: road {j} has {outside:.3f} m² outside the boundary (not at a gate)"


def _assert_no_road_over_stalls(layout, label):
    from shapely.ops import unary_union
    if not layout.aisles or not layout.stalls:
        return
    road = unary_union([a.centerline.buffer(a.width / 2.0) for a in layout.aisles])
    for i, s in enumerate(layout.stalls):
        overlap = s.polygon.intersection(road).area
        assert overlap <= 0.05 * s.polygon.area, \
            f"{label}: stall {i} is {overlap / s.polygon.area:.0%} under a road"


@pytest.mark.parametrize("name", list(_SHAPES))
def test_explored_layouts_are_contained(name, profile):
    poly = _SHAPES[name]
    results = generate_all(_site(poly), profile)
    assert results
    for r in results:
        _assert_inside(r.layout, poly, f"{name}/{r.layout_type.value}")
        _assert_no_road_over_stalls(r.layout, f"{name}/{r.layout_type.value}")


@pytest.mark.parametrize("strategy", [
    LayoutType.STANDARD, LayoutType.FISHBONE,
    LayoutType.RING_INFILL, LayoutType.SUBDIVIDED,
])
def test_each_strategy_contained_with_setback(strategy, profile):
    """With a setback the buildable area shrinks — nothing may cross the boundary."""
    poly = box(0, 0, 70, 50)
    site = Site(boundary=poly, setbacks=3.0,
                entrances=[Entrance(point=Point(35, 0), kind=EntranceKind.SITE)])
    params = LayoutParams(orientation=0.0, layout_type=strategy, angle=90.0,
                          stall_width=2.5, stall_length=5.0, aisle_dir=AisleDir.TWO_WAY)
    layout = generate(site, profile, params)
    assert layout.metrics.total_stalls > 0
    _assert_inside(layout, poly, strategy.value)
