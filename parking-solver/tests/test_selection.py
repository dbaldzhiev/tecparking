"""Curated variant selection tests."""
from __future__ import annotations

from pathlib import Path

import pytest
from shapely.geometry import Point, Polygon, box

from parking_solver.core.generator import generate_all
from parking_solver.core.model import Entrance, EntranceKind, Site
from parking_solver.core.regulations.engine import load_profile
from parking_solver.core.selection import (
    CuratedVariant,
    composite_scores,
    curate_variants,
)

_PROFILE = (
    Path(__file__).parent.parent
    / "parking_solver" / "core" / "regulations" / "profiles" / "bulgarian.yaml"
)


@pytest.fixture(scope="module")
def profile():
    return load_profile(_PROFILE)


@pytest.fixture(scope="module")
def results(profile):
    site = Site(
        boundary=box(0, 0, 60, 40),
        entrances=[Entrance(point=Point(30, 0), kind=EntranceKind.SITE)],
    )
    return generate_all(site, profile, stall_width=2.5, stall_length=5.0)


def test_composite_scores_in_range(results):
    scored = composite_scores(results)
    assert len(scored) == len(results)
    for _, sc, comp in scored:
        assert 0.0 <= comp <= 1.0
        for v in sc.values():
            assert 0.0 <= v <= 1.0


def test_curate_returns_variants(results):
    variants = curate_variants(results, k=5)
    assert 1 <= len(variants) <= 5
    assert all(isinstance(v, CuratedVariant) for v in variants)


def test_curate_variants_are_distinct(results):
    variants = curate_variants(results, k=5)
    layout_ids = [id(v.result) for v in variants]
    assert len(layout_ids) == len(set(layout_ids)), "Curated variants must be distinct"


def test_curate_has_rationales_and_labels(results):
    for v in curate_variants(results, k=5):
        assert v.label
        assert len(v.rationale) > 5


def test_max_capacity_is_actually_max(results):
    variants = curate_variants(results, k=5)
    cap_variant = next((v for v in variants if v.label == "Max capacity"), None)
    if cap_variant is not None:
        best = max(r.stall_count for r in results)
        assert cap_variant.result.stall_count == best


def test_best_balance_present(results):
    variants = curate_variants(results, k=5)
    assert any(v.label == "Best balance" for v in variants)


def test_curate_empty_results():
    assert curate_variants([], k=5) == []
