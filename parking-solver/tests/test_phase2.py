"""Phase 2 tests: ADA/EV placement and stall locking (constrained re-solve)."""
from __future__ import annotations

import pytest
from shapely.geometry import box

from parking_solver.core import generator
from parking_solver.core.ada_placement import place_special_stalls
from parking_solver.core.model import (
    AisleDir,
    FixedElements,
    LayoutParams,
    LayoutType,
    Site,
    StallType,
)
from parking_solver.core.regulations.engine import required_accessible, required_ev
from parking_solver.core.regulations.engine import load_profile

import pathlib

_PROFILE_PATH = (
    pathlib.Path(__file__).parent.parent
    / "parking_solver" / "core" / "regulations" / "profiles" / "generic_eu.yaml"
)


@pytest.fixture
def profile():
    return load_profile(_PROFILE_PATH)


@pytest.fixture
def large_site():
    """50 m × 32 m — reproduces the golden test with ~80 stalls."""
    return Site(boundary=box(0, 0, 50, 32))


@pytest.fixture
def params_90():
    return LayoutParams(
        angle=90.0,
        aisle_dir=AisleDir.TWO_WAY,
        layout_type=LayoutType.STANDARD,
    )


# ── required_accessible / required_ev unit tests ──────────────────────────────

def test_required_accessible_zero_stalls(profile):
    n_acc, n_van = required_accessible(0, profile)
    assert n_acc == 0 and n_van == 0


def test_required_accessible_25_stalls(profile):
    n_acc, n_van = required_accessible(25, profile)
    assert n_acc + n_van == 1
    assert n_van >= 1


def test_required_accessible_50_stalls(profile):
    n_acc, n_van = required_accessible(50, profile)
    assert n_acc + n_van == 2


def test_required_ev_zero(profile):
    assert required_ev(0, profile) == 0


def test_required_ev_100(profile):
    n = required_ev(100, profile)
    assert n == 10


# ── ADA placement integration tests ──────────────────────────────────────────

def test_place_special_stalls_preserves_count(large_site, profile, params_90):
    raw = generator.generate(large_site, profile, params_90)
    total_before = len(raw.stalls)
    result = place_special_stalls(raw, large_site, profile)
    assert len(result.stalls) == total_before


def test_place_special_stalls_creates_accessible(large_site, profile, params_90):
    raw = generator.generate(large_site, profile, params_90)
    result = place_special_stalls(raw, large_site, profile)
    types = [s.type for s in result.stalls]
    assert StallType.ACCESSIBLE_VAN in types or StallType.ACCESSIBLE in types


def test_place_special_stalls_creates_ev(large_site, profile, params_90):
    raw = generator.generate(large_site, profile, params_90)
    result = place_special_stalls(raw, large_site, profile)
    types = [s.type for s in result.stalls]
    assert StallType.EV in types


def test_place_special_stalls_metrics_consistent(large_site, profile, params_90):
    raw = generator.generate(large_site, profile, params_90)
    result = place_special_stalls(raw, large_site, profile)
    counted = sum(result.metrics.by_type.values())
    assert counted == result.metrics.total_stalls


def test_place_special_stalls_skips_locked(large_site, profile, params_90):
    raw = generator.generate(large_site, profile, params_90)
    # Lock the first two stalls as STANDARD
    raw.stalls[0].locked = True
    raw.stalls[1].locked = True
    result = place_special_stalls(raw, large_site, profile)
    # Locked stalls must remain STANDARD
    assert result.stalls[0].type == StallType.STANDARD
    assert result.stalls[1].type == StallType.STANDARD


# ── Stall locking / FixedElements constrained re-solve ───────────────────────

def test_fixed_elements_obstacle_subtracted(large_site, profile, params_90):
    """Locked stalls' footprints are not overwritten by the solver."""
    first_layout = generator.generate(large_site, profile, params_90)
    assert first_layout.stalls

    # Lock the first stall
    locked_stall = first_layout.stalls[0]
    locked_stall.locked = True
    fixed = FixedElements(stalls=[locked_stall])

    second_layout = generator.generate(large_site, profile, params_90, fixed=fixed)

    # The locked stall should appear exactly once in the result
    locked_polys = [s.polygon for s in second_layout.stalls if s.locked]
    assert len(locked_polys) == 1

    # No newly generated stall should intersect the locked one significantly
    locked_poly = locked_polys[0]
    for s in second_layout.stalls:
        if not s.locked:
            overlap = s.polygon.intersection(locked_poly).area
            assert overlap < 0.01 * s.polygon.area, (
                f"Generated stall overlaps locked stall by {overlap:.3f} m²"
            )


def test_fixed_stalls_prepended_in_result(large_site, profile, params_90):
    """Locked stalls come first in the output list."""
    first = generator.generate(large_site, profile, params_90)
    first.stalls[0].locked = True
    first.stalls[1].locked = True
    fixed = FixedElements(stalls=[first.stalls[0], first.stalls[1]])

    second = generator.generate(large_site, profile, params_90, fixed=fixed)
    assert second.stalls[0].locked
    assert second.stalls[1].locked


def test_generate_with_no_fixed_is_unchanged(large_site, profile, params_90):
    """Passing fixed=None behaves identically to no fixed argument."""
    layout_a = generator.generate(large_site, profile, params_90)
    layout_b = generator.generate(large_site, profile, params_90, fixed=None)
    assert len(layout_a.stalls) == len(layout_b.stalls)
