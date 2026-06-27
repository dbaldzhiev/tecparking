"""Multi-objective parking optimizer — pymoo NSGA-II backend.

Decision variables
------------------
x[0]  orientation   Real  [0, 175]     degrees – axis the aisles run along
x[1]  angle_idx     Int   [0, 3]       index into ANGLES (45/60/75/90°)
x[2]  stall_width   Real  [2.20, 3.00] metres
x[3]  strategy_idx  Int   [0, 2]       index into STRATEGIES (BANDED/RING/RING_INFILL)

Objectives (all *minimized* by pymoo)
--------------------------------------
0  -total_stalls           (maximise count)
1  gross_area_per_stall    (minimise area/stall)
2  -ada_margin             (maximise ADA excess ratio vs required)
3  mean_walk_distance      (minimise mean distance to entrance; 0 if no entrances)

Live-streaming architecture
----------------------------
pymoo's Callback ABC is used instead of save_history so generation_callback
fires *during* the run, not in a post-hoc replay loop.  A problem-level cache
(x_key → (LayoutParams, Layout, F)) eliminates re-evaluation when reconstructing
Candidate objects from pymoo's population.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable

import numpy as np
from pymoo.algorithms.moo.nsga2 import NSGA2
from pymoo.core.callback import Callback as _PymooCallback
from pymoo.core.problem import ElementwiseProblem
from pymoo.operators.crossover.sbx import SBX
from pymoo.operators.mutation.pm import PM
from pymoo.operators.sampling.rnd import FloatRandomSampling
from pymoo.optimize import minimize
from pymoo.termination import get_termination

from parking_solver.core import generator, scorer
from parking_solver.core.ada_placement import place_special_stalls
from parking_solver.core.model import (
    AisleDir,
    Layout,
    LayoutParams,
    LayoutType,
    Site,
    StallType,
)
from parking_solver.core.regulations.engine import (
    RegulationProfile,
    required_accessible,
    required_ev,
)

ANGLES = [45.0, 60.0, 75.0, 90.0]
STRATEGIES = [
    LayoutType.STANDARD,
    LayoutType.FISHBONE,
    LayoutType.PERIMETER_RING,
    LayoutType.RING_INFILL,
    LayoutType.MULTI_RING,
    LayoutType.SPINE_BRANCHES,
]

# Objective indices
OBJ_COUNT = 0
OBJ_AREA_PER_STALL = 1
OBJ_ADA_MARGIN = 2
OBJ_WALK_DIST = 3
N_OBJ = 4

OBJ_LABELS = [
    "Stall count (↑)",
    "m²/stall (↓)",
    "ADA margin (↑)",
    "Walk dist m (↓)",
]


@dataclass
class OptimizationParams:
    pop_size: int = 40
    n_gen: int = 30
    seed: int = 42


@dataclass
class Candidate:
    params: LayoutParams
    layout: Layout
    objectives: np.ndarray   # shape (N_OBJ,) — raw minimization values


@dataclass
class ParetoResult:
    """Non-dominated front returned after the optimizer finishes."""
    candidates: list[Candidate] = field(default_factory=list)
    n_gen: int = 0

    @property
    def objective_matrix(self) -> np.ndarray:
        """Shape (n_candidates, N_OBJ)."""
        if not self.candidates:
            return np.empty((0, N_OBJ))
        return np.vstack([c.objectives for c in self.candidates])


# ── Decision variable decoding ─────────────────────────────────────────────────

def _params_from_x(x: np.ndarray) -> LayoutParams:
    orientation = float(np.clip(x[0], 0, 175))
    angle = ANGLES[int(np.clip(round(x[1]), 0, 3))]
    stall_width = float(np.clip(x[2], 2.20, 3.00))
    # 4th variable (strategy) is optional for backward compatibility with tests
    if len(x) > 3:
        strategy = STRATEGIES[int(np.clip(round(x[3]), 0, len(STRATEGIES) - 1))]
    else:
        strategy = LayoutType.STANDARD
    aisle_dir = AisleDir.ONE_WAY if angle < 90 else AisleDir.TWO_WAY
    return LayoutParams(
        orientation=orientation,
        layout_type=strategy,
        angle=angle,
        stall_width=stall_width,
        aisle_dir=aisle_dir,
    )


# ── Objective helpers ──────────────────────────────────────────────────────────

def _mean_walk_distance(layout: Layout, site: Site) -> float:
    if not layout.stalls or not site.entrances:
        return 0.0
    ref = site.entrances[0].point
    total = sum(
        math.hypot(s.polygon.centroid.x - ref.x, s.polygon.centroid.y - ref.y)
        for s in layout.stalls
    )
    return total / len(layout.stalls)


def _ada_margin(layout: Layout, profile: RegulationProfile) -> float:
    """Fractional excess above the ADA requirement (0 = just met, negative = deficient)."""
    total = len(layout.stalls)
    n_acc, n_van = required_accessible(total, profile)
    required = n_acc + n_van
    if required == 0:
        return 1.0
    provided = sum(
        1 for s in layout.stalls
        if s.type in (StallType.ACCESSIBLE, StallType.ACCESSIBLE_VAN)
    )
    return (provided - required) / max(required, 1)


# ── pymoo problem ──────────────────────────────────────────────────────────────

class _ParkingProblem(ElementwiseProblem):
    """4-variable parking problem.  Results are cached to avoid re-evaluation."""

    def __init__(self, site: Site, profile: RegulationProfile, fixed=None):
        super().__init__(
            n_var=4,
            n_obj=N_OBJ,
            n_ieq_constr=0,
            xl=np.array([0.0,   0.0, 2.20, 0.0]),
            xu=np.array([175.0, 3.0, 3.00, float(len(STRATEGIES) - 1)]),  # auto-scales
        )
        self._site = site
        self._profile = profile
        self._fixed = fixed
        # Cache: rounded-x tuple → (LayoutParams, Layout, np.ndarray F)
        self._cache: dict[tuple, tuple[LayoutParams, Layout, np.ndarray]] = {}

    def _evaluate(self, x, out, *args, **kwargs):
        key = tuple(np.round(x, 8))
        if key in self._cache:
            _, _, f = self._cache[key]
            out["F"] = f
            return

        params = _params_from_x(x)
        try:
            layout = generator.generate(self._site, self._profile, params, fixed=self._fixed)
            layout = place_special_stalls(layout, self._site, self._profile)
        except Exception:
            out["F"] = np.full(N_OBJ, 1e9)
            return

        total = layout.metrics.total_stalls
        if total == 0:
            out["F"] = np.full(N_OBJ, 1e9)
            return

        f = np.array([
            -float(total),
            float(layout.metrics.gross_area_per_stall),
            -float(_ada_margin(layout, self._profile)),
            float(_mean_walk_distance(layout, self._site)),
        ])
        self._cache[key] = (params, layout, f)
        out["F"] = f


# ── Live-streaming callback ────────────────────────────────────────────────────

class _LiveStreamCallback(_PymooCallback):
    """Fires the user callback after each generation using cached layouts.

    Always instantiated (pymoo calls self.callback(self) unconditionally);
    user_cb=None simply makes notify() a no-op.
    """

    def __init__(self, problem: _ParkingProblem, user_cb: Callable | None = None):
        super().__init__()
        self._problem = problem
        self._user_cb = user_cb

    def notify(self, algorithm):
        if self._user_cb is None or algorithm.opt is None:
            return
        candidates = _pop_to_candidates(self._problem, algorithm.opt)
        if candidates:
            self._user_cb(algorithm.n_gen, candidates)


# ── Candidate reconstruction ───────────────────────────────────────────────────

def _pop_to_candidates(problem: _ParkingProblem, pop) -> list[Candidate]:
    """Reconstruct Candidate objects from pymoo population using the problem cache.

    Falls back to re-evaluation only if the individual is not in cache (rare).
    """
    candidates = []
    for ind in pop:
        x = ind.X
        key = tuple(np.round(x, 8))
        entry = problem._cache.get(key)
        if entry is not None:
            params, layout, _ = entry
        else:
            # Fallback: re-evaluate (should be uncommon after first-gen startup)
            params = _params_from_x(x)
            try:
                layout = generator.generate(problem._site, problem._profile, params,
                                            fixed=problem._fixed)
                layout = place_special_stalls(layout, problem._site, problem._profile)
            except Exception:
                continue
        f = ind.F
        if f is None or np.any(f >= 1e8):
            continue
        candidates.append(Candidate(params=params, layout=layout, objectives=f))
    return candidates


# ── Public API ─────────────────────────────────────────────────────────────────

def run(
    site: Site,
    profile: RegulationProfile,
    opt_params: OptimizationParams,
    fixed=None,
    generation_callback: Callable[[int, list[Candidate]], None] | None = None,
) -> ParetoResult:
    """Run NSGA-II and return the final non-dominated front.

    *generation_callback* is called live after each generation with
    ``(gen_number, list[Candidate])``.
    """
    problem = _ParkingProblem(site, profile, fixed)

    algorithm = NSGA2(
        pop_size=opt_params.pop_size,
        sampling=FloatRandomSampling(),
        crossover=SBX(prob=0.9, eta=15),
        mutation=PM(eta=20),
        eliminate_duplicates=True,
    )

    termination = get_termination("n_gen", opt_params.n_gen)

    # Always pass a callback object — pymoo calls self.callback(self) unconditionally
    cb = _LiveStreamCallback(problem, generation_callback)

    result = minimize(
        problem,
        algorithm,
        termination,
        seed=opt_params.seed,
        save_history=False,
        verbose=False,
        callback=cb,
    )

    all_candidates: list[Candidate] = []
    if result.opt is not None:
        all_candidates = _pop_to_candidates(problem, result.opt)

    return ParetoResult(candidates=all_candidates, n_gen=opt_params.n_gen)


def candidate_advantages(candidate: Candidate, pareto: ParetoResult) -> str:
    """Human-readable comparison of *candidate* vs. the Pareto-median."""
    mat = pareto.objective_matrix
    if mat.shape[0] < 2:
        return "Only one candidate in the front — no comparison available."

    median = np.median(mat, axis=0)
    f = candidate.objectives

    count = int(-f[OBJ_COUNT])
    med_count = int(-median[OBJ_COUNT])
    area = f[OBJ_AREA_PER_STALL]
    med_area = median[OBJ_AREA_PER_STALL]
    ada_m = -f[OBJ_ADA_MARGIN]
    med_ada = -median[OBJ_ADA_MARGIN]
    walk = f[OBJ_WALK_DIST]
    med_walk = median[OBJ_WALK_DIST]

    strategy_name = candidate.params.layout_type.value.replace("_", " ").title()

    lines = [f"Strategy: {strategy_name}  |  Angle: {candidate.params.angle:.0f}°  |  "
             f"Orientation: {candidate.params.orientation:.0f}°"]

    dc = count - med_count
    sign = "+" if dc >= 0 else ""
    lines.append(f"{'↑' if dc >= 0 else '↓'} {sign}{dc} stalls vs median ({count} total)")

    da = area - med_area
    sign = "+" if da >= 0 else ""
    lines.append(f"{'↑' if da <= 0 else '↓'} {sign}{da:.1f} m²/stall vs median ({area:.1f} m²/stall)")

    if ada_m >= 0:
        lines.append(f"✔ ADA compliant  (margin {ada_m * 100:+.0f}% above minimum)")
    else:
        lines.append(f"✖ ADA deficient  ({ada_m * 100:.0f}% below minimum)")

    if walk > 0:
        dw = walk - med_walk
        sign = "+" if dw >= 0 else ""
        lines.append(f"{'↓' if dw <= 0 else '↑'} {sign}{dw:.0f} m avg walk vs median ({walk:.0f} m)")

    return "\n".join(lines)
