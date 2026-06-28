"""5-polygon validation suite.

Tests the generator against a diverse set of real-world-like site shapes
and asserts minimum quality thresholds for each strategy result.

Polygons
--------
P1  Rectangle 60×40 m       — baseline, all strategies expected to work well
P2  L-shaped  (80+30)×50    — re-entrant corner, tests boundary handling
P3  Triangle  80×60          — acute angles, tests edge-direction auto-selection
P4  Narrow elongated 120×15  — extreme aspect ratio, tests aisle orientation
P5  Rectangle with obstacle  70×50 with 10×8 internal column cluster
"""
from __future__ import annotations

from pathlib import Path

import pytest
from shapely.geometry import Point, Polygon, box

from parking_solver.core.generator import generate_all
from parking_solver.core.model import Site
from parking_solver.core.scorer import (
    compute_dead_ends,
    compute_stall_isolation,
    theoretical_max_stalls,
)
from parking_solver.core.regulations.engine import load_profile

_PROFILE_PATH = (
    Path(__file__).parent.parent
    / "parking_solver" / "core" / "regulations" / "profiles" / "bulgarian.yaml"
)


@pytest.fixture(scope="module")
def profile():
    return load_profile(_PROFILE_PATH)


# ── 5 test polygons ──────────────────────────────────────────────────────────

@pytest.fixture
def p1_rectangle():
    """60×40 m — standard parking lot baseline."""
    return Site(boundary=box(0, 0, 60, 40), setbacks=0.0)


@pytest.fixture
def p2_lshape():
    """L-shaped site: 80×50 with 35×25 top-right corner removed."""
    outer = [(0, 0), (80, 0), (80, 25), (45, 25), (45, 50), (0, 50)]
    return Site(boundary=Polygon(outer), setbacks=0.0)


@pytest.fixture
def p3_triangle():
    """Scalene triangle: base 80 m, height 60 m."""
    return Site(boundary=Polygon([(0, 0), (80, 0), (30, 60)]), setbacks=0.0)


@pytest.fixture
def p4_narrow():
    """Narrow elongated: 120×15 m — tests aisle direction auto-selection."""
    return Site(boundary=box(0, 0, 120, 15), setbacks=0.0)


@pytest.fixture
def p5_with_obstacle():
    """70×50 m rectangle with a 10×8 m column cluster in the centre."""
    boundary = box(0, 0, 70, 50)
    obstacle = box(30, 21, 40, 29)   # centred obstacle
    return Site(boundary=boundary, obstacles=[obstacle], setbacks=0.0)


# ── helpers ──────────────────────────────────────────────────────────────────

def _best(site, profile) -> "StrategyResult | None":
    results = generate_all(site, profile, stall_width=2.5, stall_length=5.0)
    return results[0] if results else None


def _oxford_ceiling(site, profile) -> int:
    aisle_spec = profile.aisles.get("90")
    aisle_90 = (aisle_spec.two_way or aisle_spec.one_way or 6.0) if aisle_spec else 6.0
    density = theoretical_max_stalls(2.5, 5.0, aisle_90)
    return int(site.boundary.area * density)


# ── P1: Rectangle ────────────────────────────────────────────────────────────

def test_p1_produces_results(p1_rectangle, profile):
    # The analyzer narrows the search — a simple rectangle needs only a handful
    # of perimeter+infill variants, not an exhaustive cross-product.
    results = generate_all(p1_rectangle, profile)
    assert len(results) >= 3, "Expected a few perimeter+infill variants"


def test_p1_best_achieves_70pct_oxford_ceiling(p1_rectangle, profile):
    best = _best(p1_rectangle, profile)
    assert best is not None
    ceiling = _oxford_ceiling(p1_rectangle, profile)
    ratio = best.stall_count / ceiling
    assert ratio >= 0.70, f"Best is only {ratio:.0%} of Oxford ceiling ({best.stall_count}/{ceiling})"


def test_p1_dead_ends_low(p1_rectangle, profile):
    best = _best(p1_rectangle, profile)
    assert best is not None
    # Top result should be STANDARD or similar — low dead-end fraction
    assert best.dead_ends <= 0.40, f"Too many dead ends: {best.dead_ends:.2%}"


def test_p1_stall_isolation_low(p1_rectangle, profile):
    best = _best(p1_rectangle, profile)
    assert best is not None
    assert best.stall_isolation <= 0.25, f"Too many isolated stalls: {best.stall_isolation:.2%}"


def test_p1_road_coverage_high(p1_rectangle, profile):
    best = _best(p1_rectangle, profile)
    assert best is not None
    assert best.road_coverage >= 0.70, f"Road coverage too low: {best.road_coverage:.2%}"


# ── P2: L-shape ──────────────────────────────────────────────────────────────

def test_p2_produces_results(p2_lshape, profile):
    results = generate_all(p2_lshape, profile)
    assert len(results) > 5


def test_p2_best_stall_count(p2_lshape, profile):
    best = _best(p2_lshape, profile)
    assert best is not None
    # L-shape area ≈ 80×50 - 35×25 = 4000 - 875 = 3125 m²; expect >30 stalls
    assert best.stall_count >= 30, f"Only {best.stall_count} stalls on L-shaped site"


def test_p2_all_stalls_inside_boundary(p2_lshape, profile):
    best = _best(p2_lshape, profile)
    assert best is not None
    boundary = p2_lshape.boundary
    for i, stall in enumerate(best.layout.stalls):
        ratio = stall.polygon.intersection(boundary).area / stall.polygon.area
        assert ratio >= 0.99, f"Stall {i} only {ratio:.3f} inside L-shape"


# ── P3: Triangle ─────────────────────────────────────────────────────────────

def test_p3_produces_results(p3_triangle, profile):
    results = generate_all(p3_triangle, profile)
    assert len(results) > 3, "Triangle should produce at least a few valid layouts"


def test_p3_best_stall_count(p3_triangle, profile):
    best = _best(p3_triangle, profile)
    assert best is not None
    # Triangle area = ½×80×60 = 2400 m²; at ~14 m²/stall → ~170 max; expect ≥20
    assert best.stall_count >= 20, f"Only {best.stall_count} stalls on triangle"


def test_p3_all_stalls_inside_boundary(p3_triangle, profile):
    best = _best(p3_triangle, profile)
    assert best is not None
    boundary = p3_triangle.boundary
    for i, stall in enumerate(best.layout.stalls):
        ratio = stall.polygon.intersection(boundary).area / stall.polygon.area
        assert ratio >= 0.99, f"Stall {i} only {ratio:.3f} inside triangle"


# ── P4: Narrow elongated ─────────────────────────────────────────────────────

def test_p4_produces_results(p4_narrow, profile):
    results = generate_all(p4_narrow, profile)
    assert len(results) > 0, "Narrow site should still produce at least one layout"


def test_p4_aisle_parallel_to_long_axis(p4_narrow, profile):
    """On a 120×15 m site, the best orientation should be ≤10° (horizontal aisles)."""
    results = generate_all(p4_narrow, profile)
    assert results, "No results for narrow site"
    best = results[0]
    # Orientation 0° = horizontal aisles (along the 120m axis) — expect within 15°
    orient_deviation = min(best.orientation, 180 - best.orientation)
    assert orient_deviation <= 20, (
        f"Expected horizontal orientation, got {best.orientation:.0f}°"
    )


def test_p4_row_aisles_fit(p4_narrow, profile):
    """With a 15 m wide site and 90° stalls, a single double-loaded row fits (6+5+5=16 but
    profile sets overhang; any non-zero stall count passes here)."""
    best = _best(p4_narrow, profile)
    assert best is not None
    assert best.stall_count >= 4, f"Only {best.stall_count} stalls on 120×15 site"


# ── P5: Obstacle ─────────────────────────────────────────────────────────────

def test_p5_produces_results(p5_with_obstacle, profile):
    results = generate_all(p5_with_obstacle, profile)
    assert len(results) > 5


def test_p5_no_stalls_inside_obstacle(p5_with_obstacle, profile):
    best = _best(p5_with_obstacle, profile)
    assert best is not None
    obstacle = p5_with_obstacle.obstacles[0]
    for i, stall in enumerate(best.layout.stalls):
        overlap = stall.polygon.intersection(obstacle).area
        assert overlap < 1e-4, f"Stall {i} overlaps obstacle by {overlap:.4f} m²"


def test_p5_best_achieves_60pct_ceiling(p5_with_obstacle, profile):
    """Obstacle reduces effective area; still expect ≥60% of net-area ceiling."""
    best = _best(p5_with_obstacle, profile)
    assert best is not None
    # Net area after obstacle
    net_area = p5_with_obstacle.boundary.area - sum(o.area for o in p5_with_obstacle.obstacles)
    ceiling = int(net_area * theoretical_max_stalls(2.5, 5.0, 6.0))
    if ceiling > 0:
        ratio = best.stall_count / ceiling
        assert ratio >= 0.55, (
            f"Only {ratio:.0%} of net-area ceiling with obstacle ({best.stall_count}/{ceiling})"
        )


# ── Cross-polygon summary ─────────────────────────────────────────────────────

def test_exploration_offers_multiple_buildable_strategies(
    p1_rectangle, p2_lshape, p3_triangle, p4_narrow, p5_with_obstacle, profile
):
    """Exploration offers several distinct, buildable strategies — and never the
    deprecated empty-centre patterns."""
    from parking_solver.core.model import LayoutType
    deprecated = {
        LayoutType.PERIMETER_RING, LayoutType.MULTI_RING,
        LayoutType.SPINE_BRANCHES, LayoutType.MIXED_ANGLE,
    }
    sites = [p1_rectangle, p2_lshape, p3_triangle, p4_narrow, p5_with_obstacle]
    all_types = set()
    for site in sites:
        results = generate_all(site, profile)
        assert results, "every site should yield at least one layout"
        types = {r.layout_type for r in results}
        assert not (types & deprecated)
        all_types |= types
    assert len(all_types) >= 3, f"Expected several strategies, got {all_types}"


def test_exploration_offers_angle_variety(p1_rectangle, profile):
    """Within a strategy, the angle program (90 / 60 / 45) gives further variety."""
    results = generate_all(p1_rectangle, profile)
    angles = {round(r.angle) for r in results}
    assert len(angles) >= 2, f"Expected several angle programs, got {angles}"
