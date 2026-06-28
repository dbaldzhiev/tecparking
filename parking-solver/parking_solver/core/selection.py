"""Curated variant selection — turn a big explore result set into a few good picks.

The exhaustive explorer / NSGA-II produce dozens of layouts.  A planner doesn't
want to scan them all; they want a handful of clearly-differentiated options with
plain-language trade-offs.  This module normalises every layout across the
objective set, computes a weighted composite score, and selects a small set of
"champion" variants (max capacity, best circulation, most uniform, most
efficient, best overall balance), each tagged with a one-line rationale.

Pure / Qt-free so it can be unit-tested headless and reused by the dashboard.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from parking_solver.core.generator import StrategyResult
from parking_solver.core.model import LayoutType

# Composite weighting — capacity dominates, but a connected, fully-drivable
# network matters a lot too (connectivity + drivable together ≈ a third).
_WEIGHTS = {
    "capacity":     0.30,
    "efficiency":   0.12,
    "connectivity": 0.15,
    "drivable":     0.15,
    "uniformity":   0.08,
    "coverage":     0.10,
    "access":       0.10,
}


@dataclass
class CuratedVariant:
    """One recommended layout plus why it was picked."""
    result: StrategyResult
    label: str                                  # short headline, e.g. "Max capacity"
    rationale: str                              # one-line human explanation
    scores: dict[str, float] = field(default_factory=dict)   # normalised 0–1, higher=better
    composite: float = 0.0                      # weighted overall 0–1

    @property
    def layout(self):
        return self.result.layout


# ── metric extraction (higher = better) ──────────────────────────────────────

def _raw_metrics(r: StrategyResult) -> dict[str, float]:
    """Extract the comparison metrics, all oriented so higher = better."""
    return {
        "capacity":     float(r.stall_count),
        "efficiency":   -float(r.layout.metrics.gross_area_per_stall),  # less area = better
        "connectivity": 1.0 - float(r.dead_ends),
        "drivable":     float(r.circuit_validity),
        "uniformity":   1.0 - float(r.stall_isolation),
        "coverage":     float(r.road_coverage),
        "access":       float(r.entrance_connectivity),
    }


def _normalise(results: list[StrategyResult]) -> list[dict[str, float]]:
    """Min-max normalise each metric to [0, 1] across all results (higher=better)."""
    raws = [_raw_metrics(r) for r in results]
    keys = list(_WEIGHTS.keys())
    norm: list[dict[str, float]] = [dict() for _ in raws]
    for k in keys:
        vals = [m[k] for m in raws]
        lo, hi = min(vals), max(vals)
        span = hi - lo
        for i, m in enumerate(raws):
            norm[i][k] = 1.0 if span < 1e-9 else (m[k] - lo) / span
    return norm


def _composite(scores: dict[str, float]) -> float:
    return sum(scores[k] * w for k, w in _WEIGHTS.items())


# ── public API ────────────────────────────────────────────────────────────────

def composite_scores(results: list[StrategyResult]) -> list[tuple[StrategyResult, dict[str, float], float]]:
    """Return (result, normalised-scores, composite) for every result."""
    norm = _normalise(results)
    return [(r, norm[i], _composite(norm[i])) for i, r in enumerate(results)]


# Per-strategy display name + one-line character (advantages for the user).
_STRATEGY_INFO = [
    (LayoutType.RING_INFILL, "Perimeter + infill",
     "ring road around the edge plus interior rows — very well connected"),
    (LayoutType.SUBDIVIDED, "Adaptive",
     "site split into regions, each filled with its best module — great for "
     "irregular shapes"),
    (LayoutType.STANDARD, "Banded rows",
     "simple double-loaded rows with cross-aisles — easy to build and stripe"),
    (LayoutType.FISHBONE, "Herringbone",
     "angled one-way rows — fewer spaces but the easiest to enter and leave"),
]


def curate_variants(results: list[StrategyResult], k: int = 6) -> list[CuratedVariant]:
    """Pick up to *k* clearly-differentiated, buildable layouts with rationales.

    Every explored layout is fully connected (stitched road network, drivable
    in-and-out).  The meaningful choice is between *strategy characters*, so we
    surface the best layout of each strategy type plus the best overall:

      1. Best balance        — highest weighted composite score
      2. Perimeter + infill  — ring road + interior fill (usually the strongest)
      3. Adaptive            — region decomposition
      4. Banded rows         — plain double-loaded rows
      5. Herringbone         — angled one-way rows
      6. (fallback)          — next-best by composite

    Duplicates are removed so the dashboard always shows distinct options.
    """
    if not results:
        return []

    scored = composite_scores(results)
    by_idx = {id(r): (sc, comp) for r, sc, comp in scored}

    def _mk(r: StrategyResult, label: str, rationale: str) -> CuratedVariant:
        sc, comp = by_idx[id(r)]
        return CuratedVariant(result=r, label=label, rationale=rationale,
                              scores=sc, composite=comp)

    picks: list[CuratedVariant] = []
    chosen: set[int] = set()

    def _add(r: StrategyResult | None, label: str, rationale: str) -> None:
        if r is None or id(r) in chosen:
            return
        chosen.add(id(r))
        picks.append(_mk(r, label, rationale))

    def _rationale(r: StrategyResult, note: str) -> str:
        return (f"{r.stall_count} stalls · "
                f"{r.layout.metrics.gross_area_per_stall:.1f} m²/stall · "
                f"{r.dead_ends * 100:.0f}% dead ends — {note}.")

    # 1. Best overall composite.
    best_balance = max(scored, key=lambda t: t[2])[0]
    _add(best_balance, "Best balance",
         _rationale(best_balance, "top all-round score"))

    # 2–5. Best layout of each strategy character.
    for lt, label, note in _STRATEGY_INFO:
        grp = [(r, comp) for r, _sc, comp in scored if r.layout_type == lt]
        if grp:
            best = max(grp, key=lambda t: t[1])[0]
            _add(best, label, _rationale(best, note))

    # Fill remaining slots with the next-best distinct layouts by composite.
    for r, _sc, _comp in sorted(scored, key=lambda t: -t[2]):
        if len(picks) >= k:
            break
        _add(r, "Strong alternative", _rationale(r, "alternative"))

    return picks[:k]
