"""Entrance handling + road-network connectivity tests."""
from __future__ import annotations

from pathlib import Path

import pytest
from shapely.geometry import Point, Polygon, box

from parking_solver.app.controller import Controller, _default_entrance
from parking_solver.core.generator import generate
from parking_solver.core.geometry.helpers import longest_edge_midpoint
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
from parking_solver.core.scorer import compute_entrance_connectivity
from shapely.geometry import LineString

_PROFILE = (
    Path(__file__).parent.parent
    / "parking_solver" / "core" / "regulations" / "profiles" / "generic_eu.yaml"
)


@pytest.fixture(scope="module")
def profile():
    return load_profile(_PROFILE)


# ── longest_edge_midpoint ─────────────────────────────────────────────────────

def test_longest_edge_midpoint_rectangle():
    # 60×40 rectangle: longest edges are the two 60m horizontal ones
    poly = box(0, 0, 60, 40)
    mx, my = longest_edge_midpoint(poly)
    assert mx == pytest.approx(30.0)
    assert my in (pytest.approx(0.0), pytest.approx(40.0))


# ── default entrance ──────────────────────────────────────────────────────────

def test_default_entrance_on_boundary():
    poly = box(0, 0, 60, 40)
    e = _default_entrance(poly)
    assert e.kind == EntranceKind.SITE
    # Entrance lies on the boundary
    assert poly.exterior.distance(e.point) < 1e-6


def test_controller_creates_default_entrance_from_polygon():
    ctrl = Controller()
    ctrl.set_boundary_from_polygon([(0, 0), (60, 0), (60, 40), (0, 40)])
    assert len(ctrl.site.entrances) == 1
    assert ctrl.site.entrances[0].kind == EntranceKind.SITE


def test_controller_add_and_clear_entrances():
    ctrl = Controller()
    ctrl.set_boundary_from_polygon([(0, 0), (60, 0), (60, 40), (0, 40)])
    ctrl.add_entrance(10, 10)
    assert len(ctrl.site.entrances) == 2
    ctrl.clear_entrances()
    assert len(ctrl.site.entrances) == 0


# ── compute_entrance_connectivity ─────────────────────────────────────────────

def test_connectivity_no_entrances_is_one():
    aisle = DriveAisle(LineString([(0, 0), (10, 0)]), 6.0, AisleDir.TWO_WAY)
    assert compute_entrance_connectivity([aisle], []) == 1.0


def test_connectivity_no_aisles_is_zero():
    e = Entrance(point=Point(0, 0), kind=EntranceKind.SITE)
    assert compute_entrance_connectivity([], [e]) == 0.0


def test_connectivity_entrance_on_aisle():
    aisle = DriveAisle(LineString([(0, 0), (10, 0)]), 6.0, AisleDir.TWO_WAY)
    e = Entrance(point=Point(5, 0), kind=EntranceKind.SITE)
    assert compute_entrance_connectivity([aisle], [e]) == 1.0


def test_connectivity_entrance_far_from_aisle():
    aisle = DriveAisle(LineString([(0, 0), (10, 0)]), 6.0, AisleDir.TWO_WAY)
    e = Entrance(point=Point(5, 50), kind=EntranceKind.SITE)
    assert compute_entrance_connectivity([aisle], [e]) == 0.0


# ── generator auto-connects entrances ─────────────────────────────────────────

def test_generate_connects_entrance_to_network(profile):
    """An entrance placed away from the rows still ends up linked to the roads."""
    boundary = box(0, 0, 50, 32)
    # Entrance at a corner, deliberately offset from the natural aisle grid
    entrance = Entrance(point=Point(0, 16), kind=EntranceKind.SITE)
    site = Site(boundary=boundary, entrances=[entrance], setbacks=0.0)
    params = LayoutParams(
        orientation=0.0, layout_type=LayoutType.STANDARD,
        angle=90.0, stall_width=2.5, stall_length=5.0, aisle_dir=AisleDir.TWO_WAY,
    )
    layout = generate(site, profile, params)
    conn = compute_entrance_connectivity(layout.aisles, site.entrances)
    assert conn == 1.0, "Entrance should be connected to the road network after generation"


def test_generate_no_entrance_still_works(profile):
    """A site without entrances generates normally (connector logic is a no-op)."""
    site = Site(boundary=box(0, 0, 50, 32), setbacks=0.0)
    params = LayoutParams(
        orientation=0.0, layout_type=LayoutType.STANDARD,
        angle=90.0, stall_width=2.5, stall_length=5.0, aisle_dir=AisleDir.TWO_WAY,
    )
    layout = generate(site, profile, params)
    assert layout.metrics.total_stalls == 60   # 80 cells − cross-aisle collector columns
