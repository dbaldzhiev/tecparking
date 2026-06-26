"""Phase 3 optimizer tests — headless, no Qt required."""
from __future__ import annotations

import pathlib

import numpy as np
import pytest
from shapely.geometry import box

from parking_solver.core.model import AisleDir, LayoutParams, LayoutType, Site, StallType
from parking_solver.core.optimizer import (
    N_OBJ,
    ANGLES,
    Candidate,
    OptimizationParams,
    ParetoResult,
    _params_from_x,
    candidate_advantages,
    run,
)
from parking_solver.core.regulations.engine import load_profile

_PROFILE_PATH = (
    pathlib.Path(__file__).parent.parent
    / "parking_solver" / "core" / "regulations" / "profiles" / "generic_eu.yaml"
)


@pytest.fixture
def profile():
    return load_profile(_PROFILE_PATH)


@pytest.fixture
def small_site():
    """30 m × 24 m — fast to solve."""
    return Site(boundary=box(0, 0, 30, 24))


@pytest.fixture
def small_opt():
    return OptimizationParams(pop_size=12, n_gen=4, seed=1)


# ── _params_from_x ────────────────────────────────────────────────────────────

def test_params_from_x_90_gives_two_way():
    p = _params_from_x(np.array([0.0, 3.0, 2.5]))   # angle_idx=3 → 90°
    assert p.angle == 90.0
    assert p.aisle_dir == AisleDir.TWO_WAY


def test_params_from_x_45_gives_one_way():
    p = _params_from_x(np.array([45.0, 0.0, 2.5]))  # angle_idx=0 → 45°
    assert p.angle == 45.0
    assert p.aisle_dir == AisleDir.ONE_WAY


def test_params_from_x_clamps_orientation():
    p = _params_from_x(np.array([200.0, 2.0, 2.5]))
    assert p.orientation <= 175.0


def test_params_from_x_all_angles():
    for idx, expected in enumerate(ANGLES):
        p = _params_from_x(np.array([0.0, float(idx), 2.5]))
        assert p.angle == expected


# ── run() integration ─────────────────────────────────────────────────────────

def test_run_returns_pareto_result(small_site, profile, small_opt):
    result = run(small_site, profile, small_opt)
    assert isinstance(result, ParetoResult)


def test_run_has_candidates(small_site, profile, small_opt):
    result = run(small_site, profile, small_opt)
    assert len(result.candidates) > 0


def test_run_objective_matrix_shape(small_site, profile, small_opt):
    result = run(small_site, profile, small_opt)
    mat = result.objective_matrix
    assert mat.ndim == 2
    assert mat.shape[1] == N_OBJ


def test_run_all_candidates_have_stalls(small_site, profile, small_opt):
    result = run(small_site, profile, small_opt)
    for cand in result.candidates:
        assert cand.layout.metrics.total_stalls > 0


def test_run_non_dominated_front(small_site, profile, small_opt):
    """No candidate should be strictly dominated by another on all objectives."""
    result = run(small_site, profile, small_opt)
    mat = result.objective_matrix
    n = mat.shape[0]
    for i in range(n):
        for j in range(n):
            if i == j:
                continue
            # i is NOT dominated by j on all objectives
            dominated = np.all(mat[j] <= mat[i]) and np.any(mat[j] < mat[i])
            assert not dominated, f"Candidate {i} is dominated by {j}"


def test_run_generation_callback_called(small_site, profile, small_opt):
    calls = []
    run(small_site, profile, small_opt, generation_callback=lambda g, c: calls.append(g))
    assert len(calls) == small_opt.n_gen


def test_run_generation_callback_receives_candidates(small_site, profile, small_opt):
    received = []
    run(small_site, profile, small_opt, generation_callback=lambda g, c: received.extend(c))
    assert all(isinstance(c, Candidate) for c in received)


# ── candidate_advantages ──────────────────────────────────────────────────────

def test_candidate_advantages_single_candidate(small_site, profile, small_opt):
    """With only one candidate, advantages() returns a graceful fallback message."""
    result = run(small_site, profile, small_opt)
    single = ParetoResult(candidates=[result.candidates[0]], n_gen=1)
    text = candidate_advantages(result.candidates[0], single)
    assert "Only one candidate" in text or len(text) > 10


def test_candidate_advantages_full_front(small_site, profile, small_opt):
    result = run(small_site, profile, small_opt)
    if len(result.candidates) < 2:
        pytest.skip("Too few candidates for comparison")
    text = candidate_advantages(result.candidates[0], result)
    assert len(text) > 20
    assert "\n" in text   # multi-line output
