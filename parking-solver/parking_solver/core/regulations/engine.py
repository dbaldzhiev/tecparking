from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import BaseModel

from parking_solver.core.model import AisleDir, LayoutType


class StallSpec(BaseModel):
    width: float
    length: float
    max_ratio: float | None = None
    access_aisle: float | None = None


class AisleWidths(BaseModel):
    two_way: float | None = None
    one_way: float | None = None


class FireLane(BaseModel):
    min_width: float
    max_dead_end: float


class AccessibleCount(BaseModel):
    table: list[list[int]]   # [[total, required], ...]
    van_fraction: float = 0.125


class EVSpec(BaseModel):
    required_ratio: float = 0.10
    ev_ready_ratio: float = 0.20


class RegulationProfile(BaseModel):
    id: str
    units: str
    stalls: dict[str, StallSpec]
    aisles: dict[str, AisleWidths]
    fire_lane: FireLane
    overhang_allowance: float = 0.0
    accessible_count: AccessibleCount | None = None
    ev: EVSpec | None = None


@dataclass
class ModuleGeometry:
    width: float        # gross band height = aisle_width + 2 * stall_depth
    pitch: float        # along-aisle x-spacing per stall = stall_width / sin(θ)
    aisle_width: float
    stall_depth: float  # y-projection per stall = L·sin(θ) + W·cos(θ)
    n_rows: int         # 2 = double-loaded


def load_profile(path: Path | str) -> RegulationProfile:
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return RegulationProfile.model_validate(data)


def module_geometry(
    profile: RegulationProfile,
    layout_type: LayoutType,
    angle: float,
    stall_width: float,
    aisle_dir: AisleDir,
    aisle_width_override: float | None = None,
) -> ModuleGeometry:
    """Compute gross module geometry for any supported stall angle."""
    angle_key = str(int(angle))
    aisle_spec = profile.aisles.get(angle_key)
    if aisle_spec is None:
        raise ValueError(f"No aisle spec for angle {angle}° in profile {profile.id!r}")

    if aisle_width_override is not None:
        aisle_w = aisle_width_override
    elif aisle_dir == AisleDir.TWO_WAY:
        if aisle_spec.two_way is None:
            raise ValueError(f"No two_way aisle width for {angle}° in profile {profile.id!r}")
        aisle_w = aisle_spec.two_way
    else:
        if aisle_spec.one_way is None:
            raise ValueError(f"No one_way aisle width for {angle}° in profile {profile.id!r}")
        aisle_w = aisle_spec.one_way

    stall_len = profile.stalls["standard"].length  # L
    t = math.radians(angle)

    # Projected stall depth (y-extent in rotated frame):  L·sin(θ) + W·cos(θ)
    depth_proj = stall_len * math.sin(t) + stall_width * math.cos(t)

    # Along-aisle pitch between adjacent stall dividers: W / sin(θ)
    pitch = stall_width / math.sin(t)

    return ModuleGeometry(
        width=aisle_w + 2 * depth_proj,
        pitch=pitch,
        aisle_width=aisle_w,
        stall_depth=depth_proj,
        n_rows=2,
    )


def required_accessible(total_stalls: int, profile: RegulationProfile) -> tuple[int, int]:
    """Return (n_accessible, n_van_accessible) required for total_stalls.

    Uses the step-function table in the profile.  Returns (0, 0) if the
    profile has no accessible_count section.
    """
    spec = profile.accessible_count
    if spec is None or total_stalls == 0:
        return 0, 0

    required = 0
    for threshold, count in spec.table:
        if total_stalls >= threshold:
            required = count
        else:
            break

    n_van = max(1, round(required * spec.van_fraction)) if required else 0
    n_acc = required - n_van
    return n_acc, n_van


def required_ev(total_stalls: int, profile: RegulationProfile) -> int:
    """Return number of EV stalls required."""
    if profile.ev is None or total_stalls == 0:
        return 0
    return max(1, round(total_stalls * profile.ev.required_ratio))
