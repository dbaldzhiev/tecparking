from pathlib import Path

import pytest
from shapely.geometry import box

from parking_solver.core.generator import generate
from parking_solver.core.model import AisleDir, LayoutParams, Site
from parking_solver.core.regulations.engine import load_profile

_PROFILE = Path(__file__).parent.parent / "parking_solver" / "core" / "regulations" / "profiles" / "generic_eu.yaml"


@pytest.fixture(scope="module")
def profile():
    return load_profile(_PROFILE)


@pytest.fixture
def default_params():
    return LayoutParams(orientation=0.0, stall_width=2.5, stall_length=5.0, aisle_dir=AisleDir.TWO_WAY)


def test_generator_rectangle_stall_count(profile, default_params):
    """Golden test: 50×32 m rectangle with no setback → exactly 80 stalls."""
    site = Site(boundary=box(0, 0, 50, 32), setbacks=0.0)
    layout = generate(site, profile, default_params)
    assert layout.metrics.total_stalls == 80, (
        f"Expected 80 stalls, got {layout.metrics.total_stalls}"
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
