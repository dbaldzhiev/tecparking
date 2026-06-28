from pathlib import Path

import pytest
from shapely.geometry import Polygon, box

from parking_solver.core.generator import generate, generate_all
from parking_solver.core.geometry.helpers import polygon_edge_directions, stall_parallelogram_mirrored
from parking_solver.core.model import AisleDir, LayoutParams, LayoutType, Site
from parking_solver.core.regulations.engine import load_profile

_PROFILE = Path(__file__).parent.parent / "parking_solver" / "core" / "regulations" / "profiles" / "generic_eu.yaml"


@pytest.fixture(scope="module")
def profile():
    return load_profile(_PROFILE)


@pytest.fixture
def default_params():
    return LayoutParams(orientation=0.0, stall_width=2.5, stall_length=5.0, aisle_dir=AisleDir.TWO_WAY)


def test_generator_rectangle_stall_count(profile, default_params):
    """Golden test: 50×32 m rectangle, no setback → 60 stalls.

    (80 stall cells fit, but cross-aisle collectors — spaced so no row runs longer
    than ~40 m — occupy some columns; reserved lanes clip the stalls they cover.)
    """
    site = Site(boundary=box(0, 0, 50, 32), setbacks=0.0)
    layout = generate(site, profile, default_params)
    assert layout.metrics.total_stalls == 60, (
        f"Expected 60 stalls, got {layout.metrics.total_stalls}"
    )


def test_generator_rectangle_no_overlaps(profile, default_params):
    site = Site(boundary=box(0, 0, 50, 32), setbacks=0.0)
    layout = generate(site, profile, default_params)
    polys = [s.polygon for s in layout.stalls]
    for i in range(len(polys)):
        for j in range(i + 1, len(polys)):
            overlap = polys[i].intersection(polys[j]).area
            assert overlap < 1e-6, f"Stalls {i} and {j} overlap by {overlap:.2e} m²"


def test_generator_rectangle_all_inside(profile, default_params):
    boundary = box(0, 0, 50, 32)
    site = Site(boundary=boundary, setbacks=0.0)
    layout = generate(site, profile, default_params)
    for i, stall in enumerate(layout.stalls):
        ratio = stall.polygon.intersection(boundary).area / stall.polygon.area
        assert ratio >= 0.999, f"Stall {i} only {ratio:.4f} inside boundary"


def test_generator_rectangle_stall_dimensions(profile, default_params):
    site = Site(boundary=box(0, 0, 50, 32), setbacks=0.0)
    layout = generate(site, profile, default_params)
    for stall in layout.stalls:
        assert stall.polygon.area == pytest.approx(2.5 * 5.0, abs=1e-4)


def test_generator_obstacle_reduces_count(profile, default_params):
    boundary = box(0, 0, 50, 32)
    obstacle = box(20, 10, 30, 22)
    site = Site(boundary=boundary, obstacles=[obstacle], setbacks=0.0)
    layout = generate(site, profile, default_params)
    assert layout.metrics.total_stalls < 80
    assert layout.metrics.total_stalls > 0


def test_generator_obstacle_no_stall_inside(profile, default_params):
    boundary = box(0, 0, 50, 32)
    obstacle = box(20, 10, 30, 22)
    site = Site(boundary=boundary, obstacles=[obstacle], setbacks=0.0)
    layout = generate(site, profile, default_params)
    for i, stall in enumerate(layout.stalls):
        overlap = stall.polygon.intersection(obstacle).area
        assert overlap < 1e-6, f"Stall {i} intersects obstacle by {overlap:.2e} m²"


def test_generator_empty_after_full_obstacle(profile, default_params):
    boundary = box(0, 0, 50, 32)
    obstacle = box(-1, -1, 51, 33)  # covers everything
    site = Site(boundary=boundary, obstacles=[obstacle], setbacks=0.0)
    layout = generate(site, profile, default_params)
    assert layout.metrics.total_stalls == 0


def test_generator_metrics_consistent(profile, default_params):
    site = Site(boundary=box(0, 0, 50, 32), setbacks=0.0)
    layout = generate(site, profile, default_params)
    assert layout.metrics.total_stalls == sum(layout.metrics.by_type.values())
    assert layout.metrics.site_area == pytest.approx(50 * 32, abs=1e-6)
    assert layout.metrics.gross_area_per_stall == pytest.approx(
        layout.metrics.site_area / layout.metrics.total_stalls, abs=1e-6
    )


# ── Phase 1: angled layouts ───────────────────────────────────────────────────

@pytest.mark.parametrize("angle", [45.0, 60.0, 75.0])
def test_generator_angled_produces_stalls(profile, angle):
    params = LayoutParams(
        orientation=0.0, angle=angle, stall_width=2.5, stall_length=5.0,
        aisle_dir=AisleDir.ONE_WAY,
    )
    site = Site(boundary=box(0, 0, 60, 40), setbacks=0.0)
    layout = generate(site, profile, params)
    assert layout.metrics.total_stalls > 0, f"No stalls at angle {angle}°"


@pytest.mark.parametrize("angle", [45.0, 60.0, 75.0])
def test_generator_angled_no_overlaps(profile, angle):
    params = LayoutParams(
        orientation=0.0, angle=angle, stall_width=2.5, stall_length=5.0,
        aisle_dir=AisleDir.ONE_WAY,
    )
    site = Site(boundary=box(0, 0, 60, 40), setbacks=0.0)
    layout = generate(site, profile, params)
    polys = [s.polygon for s in layout.stalls]
    for i in range(min(len(polys), 50)):   # sample check — full check for < 50 stalls
        for j in range(i + 1, min(len(polys), 50)):
            overlap = polys[i].intersection(polys[j]).area
            assert overlap < 1e-5, f"Stalls {i} and {j} overlap by {overlap:.2e} m² at {angle}°"


@pytest.mark.parametrize("angle", [45.0, 60.0, 75.0, 90.0])
def test_generator_angled_all_inside(profile, angle):
    aisle_dir = AisleDir.TWO_WAY if angle == 90.0 else AisleDir.ONE_WAY
    params = LayoutParams(
        orientation=0.0, angle=angle, stall_width=2.5, stall_length=5.0,
        aisle_dir=aisle_dir,
    )
    boundary = box(0, 0, 60, 40)
    site = Site(boundary=boundary, setbacks=0.0)
    layout = generate(site, profile, params)
    for i, stall in enumerate(layout.stalls):
        ratio = stall.polygon.intersection(boundary).area / stall.polygon.area
        assert ratio >= 0.999, f"Stall {i} only {ratio:.4f} inside boundary at {angle}°"


@pytest.mark.parametrize("angle", [45.0, 60.0, 75.0, 90.0])
def test_generator_angled_stall_area(profile, angle):
    """Parallelogram area = W × L regardless of angle."""
    aisle_dir = AisleDir.TWO_WAY if angle == 90.0 else AisleDir.ONE_WAY
    params = LayoutParams(
        orientation=0.0, angle=angle, stall_width=2.5, stall_length=5.0,
        aisle_dir=aisle_dir,
    )
    site = Site(boundary=box(0, 0, 60, 40), setbacks=0.0)
    layout = generate(site, profile, params)
    for stall in layout.stalls:
        assert stall.polygon.area == pytest.approx(2.5 * 5.0, abs=1e-4)


def test_generator_orientation_sweep(profile):
    """Non-zero orientation should still produce stalls without crashing."""
    for orientation in [0, 15, 30, 45, 90]:
        params = LayoutParams(
            orientation=float(orientation), angle=90.0,
            stall_width=2.5, stall_length=5.0, aisle_dir=AisleDir.TWO_WAY,
        )
        site = Site(boundary=box(0, 0, 50, 32), setbacks=0.0)
        layout = generate(site, profile, params)
        assert layout.metrics.total_stalls > 0, f"No stalls at orientation {orientation}°"


def test_generator_setback_reduces_count(profile):
    boundary = box(0, 0, 50, 32)
    site_no_setback = Site(boundary=boundary, setbacks=0.0)
    site_setback = Site(boundary=boundary, setbacks=3.0)
    params = LayoutParams(orientation=0.0, stall_width=2.5, stall_length=5.0, aisle_dir=AisleDir.TWO_WAY)
    layout_no = generate(site_no_setback, profile, params)
    layout_sb = generate(site_setback, profile, params)
    assert layout_sb.metrics.total_stalls < layout_no.metrics.total_stalls


def test_banded_has_cross_aisles(profile):
    """Banded layout must emit at least 2 cross-aisles connecting the row aisles."""
    site = Site(boundary=box(0, 0, 50, 32), setbacks=0.0)
    params = LayoutParams(orientation=0.0, stall_width=2.5, stall_length=5.0,
                          aisle_dir=AisleDir.TWO_WAY, layout_type=LayoutType.STANDARD)
    layout = generate(site, profile, params)
    # Row aisles are horizontal; end/cross aisles are vertical.
    # Total aisles = row aisles + 2 cross aisles (at xmin and xmax).
    assert len(layout.aisles) >= 3, "Expected row aisles + at least 2 cross aisles"


# ── Perimeter ring ────────────────────────────────────────────────────────────

def test_perimeter_ring_produces_stalls(profile):
    site = Site(boundary=box(0, 0, 50, 32), setbacks=0.0)
    params = LayoutParams(stall_width=2.5, stall_length=5.0, aisle_dir=AisleDir.TWO_WAY,
                          layout_type=LayoutType.PERIMETER_RING)
    layout = generate(site, profile, params)
    assert layout.metrics.total_stalls > 0


def test_perimeter_ring_all_inside(profile):
    boundary = box(0, 0, 50, 32)
    site = Site(boundary=boundary, setbacks=0.0)
    params = LayoutParams(stall_width=2.5, stall_length=5.0, aisle_dir=AisleDir.TWO_WAY,
                          layout_type=LayoutType.PERIMETER_RING)
    layout = generate(site, profile, params)
    for i, s in enumerate(layout.stalls):
        ratio = s.polygon.intersection(boundary).area / s.polygon.area
        assert ratio >= 0.999, f"Perimeter stall {i} only {ratio:.4f} inside boundary"


def test_perimeter_ring_no_overlaps(profile):
    site = Site(boundary=box(0, 0, 50, 32), setbacks=0.0)
    params = LayoutParams(stall_width=2.5, stall_length=5.0, aisle_dir=AisleDir.TWO_WAY,
                          layout_type=LayoutType.PERIMETER_RING)
    layout = generate(site, profile, params)
    polys = [s.polygon for s in layout.stalls]
    for i in range(len(polys)):
        for j in range(i + 1, len(polys)):
            assert polys[i].intersection(polys[j]).area < 1e-4, \
                f"Perimeter stalls {i} and {j} overlap"


def test_perimeter_ring_has_ring_road(profile):
    site = Site(boundary=box(0, 0, 50, 32), setbacks=0.0)
    params = LayoutParams(stall_width=2.5, stall_length=5.0, aisle_dir=AisleDir.TWO_WAY,
                          layout_type=LayoutType.PERIMETER_RING)
    layout = generate(site, profile, params)
    assert len(layout.aisles) == 1, "Perimeter ring should produce exactly one ring-road aisle"


# ── Ring + infill ─────────────────────────────────────────────────────────────

def test_ring_infill_more_stalls_than_ring_alone(profile):
    site = Site(boundary=box(0, 0, 60, 40), setbacks=0.0)
    ring_params = LayoutParams(stall_width=2.5, stall_length=5.0, aisle_dir=AisleDir.TWO_WAY,
                               layout_type=LayoutType.PERIMETER_RING)
    fill_params = LayoutParams(stall_width=2.5, stall_length=5.0, aisle_dir=AisleDir.TWO_WAY,
                               layout_type=LayoutType.RING_INFILL)
    ring_layout = generate(site, profile, ring_params)
    fill_layout = generate(site, profile, fill_params)
    assert fill_layout.metrics.total_stalls > ring_layout.metrics.total_stalls, \
        "Ring+infill should have more stalls than ring alone"


def test_ring_infill_all_inside(profile):
    boundary = box(0, 0, 60, 40)
    site = Site(boundary=boundary, setbacks=0.0)
    params = LayoutParams(stall_width=2.5, stall_length=5.0, aisle_dir=AisleDir.TWO_WAY,
                          layout_type=LayoutType.RING_INFILL)
    layout = generate(site, profile, params)
    for i, s in enumerate(layout.stalls):
        ratio = s.polygon.intersection(boundary).area / s.polygon.area
        assert ratio >= 0.999, f"Ring+infill stall {i} only {ratio:.4f} inside boundary"


def test_ring_infill_no_overlaps(profile):
    site = Site(boundary=box(0, 0, 60, 40), setbacks=0.0)
    params = LayoutParams(stall_width=2.5, stall_length=5.0, aisle_dir=AisleDir.TWO_WAY,
                          layout_type=LayoutType.RING_INFILL)
    layout = generate(site, profile, params)
    polys = [s.polygon for s in layout.stalls]
    for i in range(min(len(polys), 60)):
        for j in range(i + 1, min(len(polys), 60)):
            assert polys[i].intersection(polys[j]).area < 1e-4, \
                f"Ring+infill stalls {i} and {j} overlap"


# ── Edge direction helper ─────────────────────────────────────────────────────

def test_edge_directions_rectangle():
    poly = box(0, 0, 50, 32)
    dirs = polygon_edge_directions(poly)
    # Rectangle has edges at 0° and 90°; longer horizontal edges should come first
    assert 0 in [round(d) for d in dirs]
    assert 90 in [round(d) for d in dirs]
    assert round(dirs[0]) == 0, "Horizontal (longer) edge should be dominant"


def test_edge_directions_rotated_rectangle():
    poly = Polygon([(0, 0), (50, 0), (60, 20), (10, 20)])
    dirs = polygon_edge_directions(poly)
    assert len(dirs) > 0
    for d in dirs:
        assert 0.0 <= d < 180.0


# ── Fishbone (herringbone) ────────────────────────────────────────────────────

def test_fishbone_produces_stalls(profile):
    site = Site(boundary=box(0, 0, 60, 40), setbacks=0.0)
    params = LayoutParams(stall_width=2.5, stall_length=5.0, aisle_dir=AisleDir.TWO_WAY,
                          layout_type=LayoutType.FISHBONE)
    layout = generate(site, profile, params)
    assert layout.metrics.total_stalls > 0


def test_fishbone_no_overlaps(profile):
    site = Site(boundary=box(0, 0, 60, 40), setbacks=0.0)
    params = LayoutParams(stall_width=2.5, stall_length=5.0, aisle_dir=AisleDir.TWO_WAY,
                          layout_type=LayoutType.FISHBONE)
    layout = generate(site, profile, params)
    polys = [s.polygon for s in layout.stalls]
    for i in range(len(polys)):
        for j in range(i + 1, len(polys)):
            assert polys[i].intersection(polys[j]).area < 1e-4, \
                f"Fishbone stalls {i} and {j} overlap"


def test_fishbone_all_inside(profile):
    boundary = box(0, 0, 60, 40)
    site = Site(boundary=boundary, setbacks=0.0)
    params = LayoutParams(stall_width=2.5, stall_length=5.0, aisle_dir=AisleDir.TWO_WAY,
                          layout_type=LayoutType.FISHBONE)
    layout = generate(site, profile, params)
    for i, s in enumerate(layout.stalls):
        ratio = s.polygon.intersection(boundary).area / s.polygon.area
        assert ratio >= 0.999, f"Fishbone stall {i} only {ratio:.4f} inside boundary"


def test_fishbone_stall_area(profile):
    site = Site(boundary=box(0, 0, 60, 40), setbacks=0.0)
    params = LayoutParams(stall_width=2.5, stall_length=5.0, aisle_dir=AisleDir.ONE_WAY,
                          angle=60.0, layout_type=LayoutType.FISHBONE)
    layout = generate(site, profile, params)
    for s in layout.stalls:
        assert s.polygon.area == pytest.approx(2.5 * 5.0, abs=1e-4)


def test_fishbone_mirrored_stall_area():
    """stall_parallelogram_mirrored must have area = W × L at all angles."""
    for angle in [45.0, 60.0, 75.0, 90.0]:
        poly = stall_parallelogram_mirrored(0.0, 0.0, 2.5, 5.0, angle)
        assert poly.area == pytest.approx(2.5 * 5.0, abs=1e-4), \
            f"Mirrored stall area wrong at {angle}°"


# ── Multi-ring ────────────────────────────────────────────────────────────────

def test_multi_ring_produces_stalls(profile):
    site = Site(boundary=box(0, 0, 60, 50), setbacks=0.0)
    params = LayoutParams(stall_width=2.5, stall_length=5.0, aisle_dir=AisleDir.TWO_WAY,
                          layout_type=LayoutType.MULTI_RING)
    layout = generate(site, profile, params)
    assert layout.metrics.total_stalls > 0


def test_multi_ring_more_stalls_than_single_ring(profile):
    site = Site(boundary=box(0, 0, 80, 60), setbacks=0.0)
    ring_params = LayoutParams(stall_width=2.5, stall_length=5.0, aisle_dir=AisleDir.TWO_WAY,
                               layout_type=LayoutType.PERIMETER_RING)
    multi_params = LayoutParams(stall_width=2.5, stall_length=5.0, aisle_dir=AisleDir.TWO_WAY,
                                layout_type=LayoutType.MULTI_RING)
    ring_layout = generate(site, profile, ring_params)
    multi_layout = generate(site, profile, multi_params)
    assert multi_layout.metrics.total_stalls > ring_layout.metrics.total_stalls


def test_multi_ring_all_inside(profile):
    boundary = box(0, 0, 60, 50)
    site = Site(boundary=boundary, setbacks=0.0)
    params = LayoutParams(stall_width=2.5, stall_length=5.0, aisle_dir=AisleDir.TWO_WAY,
                          layout_type=LayoutType.MULTI_RING)
    layout = generate(site, profile, params)
    for i, s in enumerate(layout.stalls):
        ratio = s.polygon.intersection(boundary).area / s.polygon.area
        assert ratio >= 0.999, f"Multi-ring stall {i} only {ratio:.4f} inside boundary"


# ── Spine + branches ──────────────────────────────────────────────────────────

def test_spine_branches_produces_stalls(profile):
    site = Site(boundary=box(0, 0, 60, 40), setbacks=0.0)
    params = LayoutParams(stall_width=2.5, stall_length=5.0, aisle_dir=AisleDir.TWO_WAY,
                          layout_type=LayoutType.SPINE_BRANCHES)
    layout = generate(site, profile, params)
    assert layout.metrics.total_stalls > 0


def test_spine_branches_all_inside(profile):
    boundary = box(0, 0, 60, 40)
    site = Site(boundary=boundary, setbacks=0.0)
    params = LayoutParams(stall_width=2.5, stall_length=5.0, aisle_dir=AisleDir.TWO_WAY,
                          layout_type=LayoutType.SPINE_BRANCHES)
    layout = generate(site, profile, params)
    for i, s in enumerate(layout.stalls):
        ratio = s.polygon.intersection(boundary).area / s.polygon.area
        assert ratio >= 0.999, f"Spine stall {i} only {ratio:.4f} inside boundary"


def test_spine_branches_has_spine_aisle(profile):
    site = Site(boundary=box(0, 0, 60, 40), setbacks=0.0)
    params = LayoutParams(stall_width=2.5, stall_length=5.0, aisle_dir=AisleDir.TWO_WAY,
                          layout_type=LayoutType.SPINE_BRANCHES)
    layout = generate(site, profile, params)
    assert len(layout.aisles) >= 1, "Spine+branches must have at least the spine aisle"


def test_spine_branches_no_overlaps(profile):
    site = Site(boundary=box(0, 0, 60, 40), setbacks=0.0)
    params = LayoutParams(stall_width=2.5, stall_length=5.0, aisle_dir=AisleDir.TWO_WAY,
                          layout_type=LayoutType.SPINE_BRANCHES)
    layout = generate(site, profile, params)
    polys = [s.polygon for s in layout.stalls]
    for i in range(min(len(polys), 60)):
        for j in range(i + 1, min(len(polys), 60)):
            assert polys[i].intersection(polys[j]).area < 1e-4, \
                f"Spine stalls {i} and {j} overlap"


# ── generate_all ──────────────────────────────────────────────────────────────

def test_generate_all_returns_sorted(profile):
    site = Site(boundary=box(0, 0, 60, 40), setbacks=0.0)
    results = generate_all(site, profile, stall_width=2.5, stall_length=5.0)
    assert len(results) > 0
    counts = [r.stall_count for r in results]
    assert counts == sorted(counts, reverse=True), "generate_all must be sorted by stall count"


def test_generate_all_all_inside(profile):
    boundary = box(0, 0, 60, 40)
    site = Site(boundary=boundary, setbacks=0.0)
    results = generate_all(site, profile, stall_width=2.5, stall_length=5.0)
    for result in results:
        for i, s in enumerate(result.layout.stalls):
            ratio = s.polygon.intersection(boundary).area / s.polygon.area
            assert ratio >= 0.999, \
                f"{result.label}: stall {i} only {ratio:.4f} inside boundary"


def test_generate_all_analyzer_picks_strategies(profile):
    """The analyzer narrows the search: a simple convex rectangle uses the go-to
    perimeter+infill only; never the deprecated empty-centre patterns."""
    site = Site(boundary=box(0, 0, 80, 60), setbacks=0.0)
    results = generate_all(site, profile, stall_width=2.5, stall_length=5.0)
    assert results
    found = {r.layout_type for r in results}
    buildable = {
        LayoutType.RING_INFILL, LayoutType.SUBDIVIDED,
        LayoutType.STANDARD, LayoutType.FISHBONE,
    }
    assert found <= buildable, f"Unexpected strategy explored: {found - buildable}"
    # Convex compact rectangle → perimeter+infill is the go-to.
    assert LayoutType.RING_INFILL in found
    deprecated = {
        LayoutType.PERIMETER_RING, LayoutType.MULTI_RING,
        LayoutType.SPINE_BRANCHES, LayoutType.MIXED_ANGLE,
    }
    assert not (found & deprecated)


def test_analyze_polygon_decisions(profile):
    """Analyzer routes shapes: convex→perimeter+infill, concave→+subdivision,
    narrow→banded+herringbone (herringbone only for tight spaces)."""
    from parking_solver.core.generator import analyze_polygon, _compute_work
    from shapely.geometry import Polygon

    convex = _compute_work(Site(boundary=box(0, 0, 80, 60)), profile, None)
    p = analyze_polygon(convex, profile)
    assert p.perimeter_infill and not p.subdivision and not p.herringbone

    lshape = _compute_work(
        Site(boundary=Polygon([(0, 0), (80, 0), (80, 25), (45, 25), (45, 50), (0, 50)])),
        profile, None)
    p = analyze_polygon(lshape, profile)
    assert p.subdivision   # reflex vertex → subdivide

    narrow = _compute_work(Site(boundary=box(0, 0, 120, 15)), profile, None)
    p = analyze_polygon(narrow, profile)
    assert p.herringbone and p.banded and not p.subdivision


def test_generate_all_connectivity_is_strong(profile):
    """Every explored layout must be well-connected (low dead ends, drivable)."""
    from parking_solver.core.scorer import compute_circuit_validity, compute_dead_ends
    site = Site(boundary=box(0, 0, 60, 40), setbacks=0.0,
                entrances=[])
    results = generate_all(site, profile, stall_width=2.5, stall_length=5.0)
    for r in results:
        assert compute_dead_ends(r.layout.aisles) <= 0.20, \
            f"{r.layout_type.value}: too many dead ends"
