from pathlib import Path

import pytest

from parking_solver.core.model import AisleDir, LayoutType
from parking_solver.core.regulations.engine import load_profile, module_geometry

_PROFILE = Path(__file__).parent.parent / "parking_solver" / "core" / "regulations" / "profiles" / "generic_eu.yaml"


def test_load_profile():
    profile = load_profile(_PROFILE)
    assert profile.id == "generic_eu"
    assert profile.units == "metres"
    assert profile.stalls["standard"].width == pytest.approx(2.50)
    assert profile.stalls["standard"].length == pytest.approx(5.00)
    assert profile.stalls["compact"].max_ratio == pytest.approx(0.30)
    assert profile.fire_lane.min_width == pytest.approx(3.50)
    assert profile.overhang_allowance == pytest.approx(0.60)


def test_module_geometry_90_two_way():
    profile = load_profile(_PROFILE)
    mod = module_geometry(profile, LayoutType.STANDARD, 90.0, 2.5, AisleDir.TWO_WAY)
    assert mod.aisle_width == pytest.approx(6.0)
    assert mod.width == pytest.approx(16.0)   # 6 + 2×5
    assert mod.pitch == pytest.approx(2.5)
    assert mod.n_rows == 2
    assert mod.stall_depth == pytest.approx(5.0)


def test_module_geometry_missing_angle_raises():
    profile = load_profile(_PROFILE)
    with pytest.raises(ValueError, match="No aisle spec"):
        module_geometry(profile, LayoutType.STANDARD, 30.0, 2.5, AisleDir.TWO_WAY)


def test_module_geometry_missing_direction_raises():
    profile = load_profile(_PROFILE)
    # 75° only has one_way in the profile
    with pytest.raises(ValueError, match="No two_way aisle width"):
        module_geometry(profile, LayoutType.STANDARD, 75.0, 2.5, AisleDir.TWO_WAY)
