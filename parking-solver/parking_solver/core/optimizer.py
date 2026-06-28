"""Multi-objective data types for the explorer / Pareto view.

The NSGA-II optimisation *engine* was removed (2026): the explorer already does
an exhaustive search over the (now small) analyzer-selected strategy set, so a
separate optimiser added nothing.  This module keeps only the lightweight data
structures the Pareto scatter and explore panel still use to describe and
compare candidate layouts.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from parking_solver.core.model import Layout, LayoutParams

# Objective indices — "higher is better" objectives are stored negated so every
# axis is minimised, matching how the Pareto scatter flips them for display.
OBJ_COUNT = 0           # -stall_count        (maximise)
OBJ_AREA_PER_STALL = 1  # m²/stall            (minimise)
OBJ_CONNECTIVITY = 2    # dead-end fraction   (minimise: 0=all roads connected)
OBJ_UNIFORMITY = 3      # isolation fraction  (minimise: 0=all stalls in full rows)
OBJ_ADA_MARGIN = 4      # -ada_margin         (maximise)
OBJ_WALK_DIST = 5       # mean walk dist m    (minimise)
N_OBJ = 6

OBJ_LABELS = [
    "Stall count (↑)",
    "m²/stall (↓)",
    "Dead ends (↓)",
    "Isolated stalls (↓)",
    "ADA margin (↑)",
    "Walk dist m (↓)",
]


@dataclass
class Candidate:
    params: LayoutParams
    layout: Layout
    objectives: np.ndarray   # shape (N_OBJ,) — raw minimization values


@dataclass
class ParetoResult:
    """A set of candidate layouts plotted on the Pareto scatter."""
    candidates: list[Candidate] = field(default_factory=list)
    n_gen: int = 0

    @property
    def objective_matrix(self) -> np.ndarray:
        """Shape (n_candidates, N_OBJ)."""
        if not self.candidates:
            return np.empty((0, N_OBJ))
        return np.vstack([c.objectives for c in self.candidates])


def candidate_advantages(candidate: Candidate, pareto: ParetoResult) -> str:
    """Human-readable comparison of *candidate* vs. the set median."""
    mat = pareto.objective_matrix
    if mat.shape[0] < 2:
        return "Only one candidate — no comparison available."

    median = np.median(mat, axis=0)
    f = candidate.objectives

    count = int(-f[OBJ_COUNT])
    med_count = int(-median[OBJ_COUNT])
    area = f[OBJ_AREA_PER_STALL]
    med_area = median[OBJ_AREA_PER_STALL]
    conn = f[OBJ_CONNECTIVITY]
    med_conn = median[OBJ_CONNECTIVITY]
    unif = f[OBJ_UNIFORMITY]
    med_unif = median[OBJ_UNIFORMITY]
    ada_m = -f[OBJ_ADA_MARGIN]
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

    dconn = conn - med_conn
    lines.append(
        f"{'✔' if conn < 0.05 else '⚠'} Road dead ends: {conn * 100:.0f}%"
        f"  ({'better' if dconn <= 0 else 'worse'} than median)"
    )

    dunif = unif - med_unif
    lines.append(
        f"{'✔' if unif < 0.10 else '⚠'} Isolated stalls: {unif * 100:.0f}%"
        f"  ({'better' if dunif <= 0 else 'worse'} than median)"
    )

    if ada_m >= 0:
        lines.append(f"✔ ADA compliant  (margin {ada_m * 100:+.0f}% above minimum)")
    else:
        lines.append(f"✖ ADA deficient  ({ada_m * 100:.0f}% below minimum)")

    if walk > 0:
        dw = walk - med_walk
        sign = "+" if dw >= 0 else ""
        lines.append(f"{'↓' if dw <= 0 else '↑'} {sign}{dw:.0f} m avg walk vs median ({walk:.0f} m)")

    return "\n".join(lines)
