from __future__ import annotations

import math

from parking_solver.core.model import Layout, Site, Stall, StallType
from parking_solver.core.regulations.engine import (
    RegulationProfile,
    required_accessible,
    required_ev,
)


def place_special_stalls(layout: Layout, site: Site, profile: RegulationProfile) -> Layout:
    """Post-process a generated layout to assign ADA/EV stall types.

    Stalls are converted (not added) — the total count is unchanged.
    Conversion is done closest-to-entrance first; falls back to centroid if no entrances.
    """
    if not layout.stalls:
        return layout

    # Reference point for proximity sorting
    if site.entrances:
        ref = site.entrances[0].point
        ref_xy = (ref.x, ref.y)
    else:
        c = site.boundary.centroid
        ref_xy = (c.x, c.y)

    # Sort stall indices by distance to reference point
    def _dist(stall: Stall) -> float:
        cx, cy = stall.polygon.centroid.coords[0]
        return math.hypot(cx - ref_xy[0], cy - ref_xy[1])

    # Only convert STANDARD stalls that are not locked
    available = [
        i for i, s in enumerate(layout.stalls)
        if s.type == StallType.STANDARD and not s.locked
    ]
    available.sort(key=lambda i: _dist(layout.stalls[i]))

    n_acc, n_van = required_accessible(len(layout.stalls), profile)
    n_ev = required_ev(len(layout.stalls), profile)

    stalls = list(layout.stalls)   # shallow copy list; Stall objects are mutated in place
    ptr = 0

    for _ in range(n_van):
        if ptr >= len(available):
            break
        stalls[available[ptr]].type = StallType.ACCESSIBLE_VAN
        ptr += 1

    for _ in range(n_acc):
        if ptr >= len(available):
            break
        stalls[available[ptr]].type = StallType.ACCESSIBLE
        ptr += 1

    for _ in range(n_ev):
        if ptr >= len(available):
            break
        stalls[available[ptr]].type = StallType.EV
        ptr += 1

    # Rebuild metrics to reflect updated types
    from parking_solver.core.scorer import score
    metrics = score(stalls, site)

    return Layout(
        stalls=stalls,
        aisles=layout.aisles,
        entrances=layout.entrances,
        metrics=metrics,
        params=layout.params,
        profile_id=layout.profile_id,
    )
