from __future__ import annotations

from parking_solver.core.model import Metrics, Site, Stall


def score(stalls: list[Stall], site: Site) -> Metrics:
    """Minimal scorer: count stalls, compute gross area per stall."""
    total = len(stalls)
    by_type: dict[str, int] = {}
    for stall in stalls:
        key = stall.type.value
        by_type[key] = by_type.get(key, 0) + 1

    site_area = site.boundary.area
    gross_area_per_stall = site_area / max(total, 1)

    return Metrics(
        total_stalls=total,
        by_type=by_type,
        gross_area_per_stall=gross_area_per_stall,
        site_area=site_area,
    )
