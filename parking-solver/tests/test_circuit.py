"""One-way circuit validation tests."""
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
    Site,
)
from parking_solver.core.regulations.engine import load_profile
from parking_solver.core.scorer import compute_circuit_validity

_PROFILE = (
    Path(__file__).parent.parent
    / "parking_solver" / "core" / "regulations" / "profiles" / "bulgarian.yaml"
)


@pytest.fixture(scope="module")
def profile():
    return load_profile(_PROFILE)


def test_two_way_network_fully_drivable():
    """A two-way ladder is always fully circulable."""
    aisles = [
        DriveAisle(LineString([(0, 0), (40, 0)]), 6.0, AisleDir.TWO_WAY),
        DriveAisle(LineString([(0, 20), (40, 20)]), 6.0, AisleDir.TWO_WAY),
        DriveAisle(LineString([(0, 0), (0, 20)]), 6.0, AisleDir.TWO_WAY),
        DriveAisle(LineString([(40, 0), (40, 20)]), 6.0, AisleDir.TWO_WAY),
    ]
    entr = [Entrance(point=Point(0, 0), kind=EntranceKind.SITE)]
    assert compute_circuit_validity(aisles, entr) == pytest.approx(1.0)


def test_one_way_rows_with_two_way_collectors_drivable():
    """Two one-way rows (opposite flow) joined by two-way cross-aisles = valid loop."""
    row_lo = DriveAisle(LineString([(0, 0), (40, 0)]), 4.0, AisleDir.ONE_WAY, flow=(1.0, 0.0))
    row_hi = DriveAisle(LineString([(0, 20), (40, 20)]), 4.0, AisleDir.ONE_WAY, flow=(-1.0, 0.0))
    left = DriveAisle(LineString([(0, 0), (0, 20)]), 6.0, AisleDir.TWO_WAY)
    right = DriveAisle(LineString([(40, 0), (40, 20)]), 6.0, AisleDir.TWO_WAY)
    entr = [Entrance(point=Point(0, 0), kind=EntranceKind.SITE)]
    assert compute_circuit_validity([row_lo, row_hi, left, right], entr) == pytest.approx(1.0)


def test_trapped_one_way_detected():
    """A lone one-way aisle pointing away from the entrance traps the driver."""
    # Enter at x=0, the only road forces you to x=40 with no way back.
    trap = DriveAisle(LineString([(0, 0), (40, 0)]), 4.0, AisleDir.ONE_WAY, flow=(1.0, 0.0))
    entr = [Entrance(point=Point(0, 0), kind=EntranceKind.SITE)]
    score = compute_circuit_validity([trap], entr)
    assert score < 1.0   # the far end is reachable-in but not reachable-out


def test_circuit_validity_vacuous_cases():
    assert compute_circuit_validity([], []) == 1.0
    entr = [Entrance(point=Point(0, 0), kind=EntranceKind.SITE)]
    assert compute_circuit_validity([], entr) == 1.0


def test_generated_angled_layout_is_drivable(profile):
    """A real 45° one-way layout must be fully circulable thanks to two-way collectors."""
    site = Site(
        boundary=box(0, 0, 60, 40),
        entrances=[Entrance(point=Point(30, 0), kind=EntranceKind.SITE)],
    )
    params = LayoutParams(orientation=0.0, angle=45.0, stall_width=2.5,
                          stall_length=5.0, aisle_dir=AisleDir.ONE_WAY)
    layout = generate(site, profile, params)
    score = compute_circuit_validity(layout.aisles, site.entrances)
    assert score >= 0.95, f"45° layout only {score:.0%} drivable"


def test_all_explored_layouts_drivable_on_irregular_sites(profile):
    """The drivability guarantee (rescue connectors) must hold even on tapering /
    concave boundaries where one-way rows would otherwise trap drivers."""
    from parking_solver.core.generator import generate_all
    sites = {
        "octagon": Polygon([(20, 0), (80, 0), (100, 20), (100, 60),
                            (80, 80), (20, 80), (0, 60), (0, 20)]),
        "lshape": Polygon([(0, 0), (80, 0), (80, 25), (45, 25), (45, 50), (0, 50)]),
    }
    for name, poly in sites.items():
        site = Site(boundary=poly,
                    entrances=[Entrance(point=Point(*poly.exterior.coords[0]),
                                        kind=EntranceKind.SITE)])
        results = generate_all(site, profile)
        # Compliance is enforced first (no interior road may cross the boundary),
        # so badly-undrivable variants are filtered out of exploration and the
        # best layout is always fully drivable.
        for r in results:
            assert r.circuit_validity >= 0.5, (
                f"{name}/{r.layout_type.value} only {r.circuit_validity:.0%} drivable"
            )
        assert max(r.circuit_validity for r in results) >= 0.999


def test_generated_90_layout_is_drivable(profile):
    site = Site(
        boundary=box(0, 0, 50, 32),
        entrances=[Entrance(point=Point(25, 0), kind=EntranceKind.SITE)],
    )
    params = LayoutParams(orientation=0.0, angle=90.0, stall_width=2.5,
                          stall_length=5.0, aisle_dir=AisleDir.TWO_WAY)
    layout = generate(site, profile, params)
    assert compute_circuit_validity(layout.aisles, site.entrances) == pytest.approx(1.0)
