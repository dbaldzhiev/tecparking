"""Polygon subdivision + road-network stitching tests."""
from __future__ import annotations

from pathlib import Path

import pytest
from shapely.geometry import LineString, Point, Polygon, box

from parking_solver.core.generator import (
    _decompose,
    _reflex_points,
    _stitch_network,
    generate,
    generate_all,
)
from parking_solver.core.model import (
    AisleDir,
    DriveAisle,
    Entrance,
    EntranceKind,
    LayoutParams,
    LayoutType,
    Site,
)
from parking_solver.core.regulations.engine import load_profile
from parking_solver.core.scorer import compute_dead_ends

_PROFILE = (
    Path(__file__).parent.parent
    / "parking_solver" / "core" / "regulations" / "profiles" / "bulgarian.yaml"
)

_LSHAPE = Polygon([(0, 0), (80, 0), (80, 25), (45, 25), (45, 50), (0, 50)])
_TRIANGLE = Polygon([(0, 0), (80, 0), (30, 60)])
_ENTRANCE = [Entrance(point=Point(40, 0), kind=EntranceKind.SITE)]


@pytest.fixture(scope="module")
def profile():
    return load_profile(_PROFILE)


# ── reflex detection + decomposition ──────────────────────────────────────────

def test_reflex_points_lshape():
    reflex = _reflex_points(_LSHAPE)
    assert len(reflex) == 1
    assert reflex[0] == pytest.approx((45.0, 25.0))


def test_reflex_points_convex_none():
    assert _reflex_points(box(0, 0, 60, 40)) == []


def test_decompose_lshape_splits():
    regions = _decompose(_LSHAPE, 0.0)
    assert len(regions) >= 2
    # Pieces should roughly tile the original area
    total = sum(r.area for r in regions)
    assert total == pytest.approx(_LSHAPE.area, rel=0.02)


def test_decompose_convex_single():
    regions = _decompose(box(0, 0, 60, 40), 0.0)
    assert len(regions) == 1


# ── network stitching ─────────────────────────────────────────────────────────

def test_stitch_connects_two_components(profile):
    # Two parallel, disconnected aisles → stitching adds a connector
    a1 = DriveAisle(LineString([(0, 0), (20, 0)]), 6.0, AisleDir.TWO_WAY)
    a2 = DriveAisle(LineString([(0, 30), (20, 30)]), 6.0, AisleDir.TWO_WAY)
    before = compute_dead_ends([a1, a2])
    stitched = _stitch_network([a1, a2], profile)
    after = compute_dead_ends(stitched)
    assert len(stitched) > 2
    assert after < before


def test_stitch_noop_single_component(profile):
    # A connected ladder needs no extra connectors
    a1 = DriveAisle(LineString([(0, 0), (20, 0)]), 6.0, AisleDir.TWO_WAY)
    a2 = DriveAisle(LineString([(0, 0), (0, 20)]), 6.0, AisleDir.TWO_WAY)
    stitched = _stitch_network([a1, a2], profile)
    assert len(stitched) == 2


# ── subdivided strategy end-to-end ────────────────────────────────────────────

def test_subdivided_lshape_connected(profile):
    site = Site(boundary=_LSHAPE, entrances=_ENTRANCE)
    p = LayoutParams(layout_type=LayoutType.SUBDIVIDED, angle=90.0,
                     stall_width=2.5, stall_length=5.0, aisle_dir=AisleDir.TWO_WAY)
    layout = generate(site, profile, p)
    assert layout.metrics.total_stalls > 0
    assert compute_dead_ends(layout.aisles) < 0.10, "Subdivided regions must be road-connected"


def test_subdivided_no_overlaps(profile):
    site = Site(boundary=_LSHAPE, entrances=_ENTRANCE)
    p = LayoutParams(layout_type=LayoutType.SUBDIVIDED, angle=90.0,
                     stall_width=2.5, stall_length=5.0, aisle_dir=AisleDir.TWO_WAY)
    layout = generate(site, profile, p)
    polys = [s.polygon for s in layout.stalls]
    # Sample-based overlap check (full O(n²) is slow for many stalls)
    for i in range(0, len(polys), 7):
        for j in range(i + 1, min(i + 15, len(polys))):
            overlap = polys[i].intersection(polys[j]).area
            assert overlap < 1e-3, f"Stalls {i},{j} overlap {overlap:.3f}"


def test_subdivided_all_inside_boundary(profile):
    site = Site(boundary=_LSHAPE, entrances=_ENTRANCE)
    p = LayoutParams(layout_type=LayoutType.SUBDIVIDED, angle=90.0,
                     stall_width=2.5, stall_length=5.0, aisle_dir=AisleDir.TWO_WAY)
    layout = generate(site, profile, p)
    for i, s in enumerate(layout.stalls):
        ratio = s.polygon.intersection(_LSHAPE).area / s.polygon.area
        assert ratio >= 0.99, f"Stall {i} only {ratio:.3f} inside boundary"


def test_stitching_fixes_triangle_standard(profile):
    """Triangle STANDARD used to leave rows disconnected; stitching must fix it."""
    site = Site(boundary=_TRIANGLE, entrances=_ENTRANCE)
    p = LayoutParams(layout_type=LayoutType.STANDARD, angle=90.0,
                     stall_width=2.5, stall_length=5.0, aisle_dir=AisleDir.TWO_WAY)
    layout = generate(site, profile, p)
    assert compute_dead_ends(layout.aisles) < 0.10


def test_subdivided_appears_in_explore(profile):
    site = Site(boundary=_LSHAPE, entrances=_ENTRANCE)
    results = generate_all(site, profile)
    types = {r.layout_type for r in results}
    assert LayoutType.SUBDIVIDED in types
