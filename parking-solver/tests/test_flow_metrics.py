"""Traffic-flow direction (P4) + physical metrics (circulation, walk distance)."""
from __future__ import annotations

from pathlib import Path

import pytest
from shapely.geometry import LineString, Point, Polygon, box

from parking_solver.core.generator import _assign_flow_directions, generate
from parking_solver.core.model import (
    AisleDir,
    DriveAisle,
    Entrance,
    EntranceKind,
    LayoutParams,
    LayoutType,
    Site,
    Stall,
    StallType,
)
from parking_solver.core.regulations.engine import load_profile
from parking_solver.core.scorer import (
    compute_aisle_area_ratio,
    compute_walk_distance,
)

_PROFILE = (
    Path(__file__).parent.parent
    / "parking_solver" / "core" / "regulations" / "profiles" / "bulgarian.yaml"
)


@pytest.fixture(scope="module")
def profile():
    return load_profile(_PROFILE)


# ── flow direction ────────────────────────────────────────────────────────────

def test_flow_points_away_from_entrance():
    """A one-way aisle running from x=0 to x=20 with an entrance at x=0
    should get a flow vector pointing in +x (into the lot)."""
    aisle = DriveAisle(LineString([(0, 0), (20, 0)]), 4.0, AisleDir.ONE_WAY)
    entr = [Entrance(point=Point(0, 0), kind=EntranceKind.SITE)]
    _assign_flow_directions([aisle], entr)
    assert aisle.flow is not None
    assert aisle.flow[0] > 0.9   # pointing +x, away from the entrance at x=0


def test_flow_reverses_with_entrance_position():
    """Same aisle, entrance at the far end → flow points the other way."""
    aisle = DriveAisle(LineString([(0, 0), (20, 0)]), 4.0, AisleDir.ONE_WAY)
    entr = [Entrance(point=Point(20, 0), kind=EntranceKind.SITE)]
    _assign_flow_directions([aisle], entr)
    assert aisle.flow is not None
    assert aisle.flow[0] < -0.9


def test_two_way_aisle_has_no_flow():
    aisle = DriveAisle(LineString([(0, 0), (20, 0)]), 6.0, AisleDir.TWO_WAY)
    entr = [Entrance(point=Point(0, 0), kind=EntranceKind.SITE)]
    _assign_flow_directions([aisle], entr)
    assert aisle.flow is None


def test_no_entrance_leaves_flow_none():
    aisle = DriveAisle(LineString([(0, 0), (20, 0)]), 4.0, AisleDir.ONE_WAY)
    _assign_flow_directions([aisle], [])
    assert aisle.flow is None


def test_generate_assigns_flow_to_one_way(profile):
    """A 45° layout (one-way aisles) should come back with flow vectors set."""
    site = Site(
        boundary=box(0, 0, 50, 40),
        entrances=[Entrance(point=Point(25, 0), kind=EntranceKind.SITE)],
    )
    params = LayoutParams(
        orientation=0.0, layout_type=LayoutType.STANDARD,
        angle=45.0, stall_width=2.5, stall_length=5.0, aisle_dir=AisleDir.ONE_WAY,
    )
    layout = generate(site, profile, params)
    one_ways = [a for a in layout.aisles if a.direction == AisleDir.ONE_WAY]
    if one_ways:
        assert any(a.flow is not None for a in one_ways), "one-way aisles should get flow"


# ── circulation overhead ──────────────────────────────────────────────────────

def test_aisle_area_ratio_range(profile):
    site = Site(boundary=box(0, 0, 50, 32),
                entrances=[Entrance(point=Point(25, 0), kind=EntranceKind.SITE)])
    params = LayoutParams(orientation=0.0, angle=90.0, stall_width=2.5,
                          stall_length=5.0, aisle_dir=AisleDir.TWO_WAY)
    layout = generate(site, profile, params)
    ratio = compute_aisle_area_ratio(layout.aisles, site)
    assert 0.0 < ratio < 1.0


def test_aisle_area_ratio_zero_without_aisles():
    site = Site(boundary=box(0, 0, 50, 32))
    assert compute_aisle_area_ratio([], site) == 0.0


# ── walk distance (nearest entrance) ──────────────────────────────────────────

def test_walk_distance_uses_nearest_entrance():
    stall = Stall(polygon=box(48, 0, 50, 5), type=StallType.STANDARD)
    near = Entrance(point=Point(49, 2.5), kind=EntranceKind.SITE)
    far = Entrance(point=Point(0, 0), kind=EntranceKind.SITE)
    mean, mx = compute_walk_distance([stall], [near, far])
    # Should measure to the near entrance, not the first/far one
    assert mean < 5.0
    assert mx < 5.0


def test_walk_distance_empty():
    assert compute_walk_distance([], []) == (0.0, 0.0)
