import pathlib
import tempfile

import pytest
from shapely.geometry import LineString, Point, Polygon

from parking_solver.core.model import (
    AisleDir,
    DriveAisle,
    Entrance,
    EntranceKind,
    Layout,
    LayoutParams,
    Metrics,
    Site,
    Stall,
    StallType,
)
from parking_solver.io.project_io import load, save

_STALL_POLY = Polygon([(0, 0), (2.5, 0), (2.5, 5.0), (0, 5.0)])
_BOUNDARY = Polygon([(0, 0), (50, 0), (50, 32), (0, 32)])


def _make_objects():
    site = Site(
        boundary=_BOUNDARY,
        obstacles=[],
        entrances=[Entrance(point=Point(25, 0), kind=EntranceKind.SITE)],
        setbacks=0.0,
    )
    stall = Stall(polygon=_STALL_POLY, type=StallType.STANDARD, angle=90.0, locked=False, source="generated")
    aisle = DriveAisle(centerline=LineString([(0, 8), (50, 8)]), width=6.0, direction=AisleDir.TWO_WAY)
    metrics = Metrics(total_stalls=1, by_type={"standard": 1}, gross_area_per_stall=1600.0, site_area=1600.0)
    layout = Layout(
        stalls=[stall],
        aisles=[aisle],
        entrances=site.entrances,
        metrics=metrics,
        params=LayoutParams(),
        profile_id="generic_eu",
    )
    return site, layout


def test_roundtrip_with_layout():
    site, layout = _make_objects()
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        path = pathlib.Path(f.name)
    try:
        save(site, layout, path)
        site2, layout2 = load(path)

        assert site2.boundary.area == pytest.approx(site.boundary.area, abs=1e-6)
        assert site2.setbacks == site.setbacks
        assert len(site2.entrances) == 1
        assert site2.entrances[0].kind == EntranceKind.SITE

        assert layout2 is not None
        assert len(layout2.stalls) == 1
        assert layout2.stalls[0].type == StallType.STANDARD
        assert layout2.stalls[0].polygon.area == pytest.approx(_STALL_POLY.area, abs=1e-6)
        assert layout2.stalls[0].locked is False

        assert len(layout2.aisles) == 1
        assert layout2.aisles[0].width == pytest.approx(6.0, abs=1e-6)
        assert layout2.aisles[0].direction == AisleDir.TWO_WAY

        assert layout2.profile_id == "generic_eu"
        assert layout2.metrics.total_stalls == 1
    finally:
        path.unlink(missing_ok=True)


def test_roundtrip_no_layout():
    site, _ = _make_objects()
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        path = pathlib.Path(f.name)
    try:
        save(site, None, path)
        site2, layout2 = load(path)
        assert layout2 is None
        assert site2.boundary.area == pytest.approx(site.boundary.area, abs=1e-6)
    finally:
        path.unlink(missing_ok=True)


def test_roundtrip_with_obstacle():
    from shapely.geometry import box
    obstacle = box(10, 10, 20, 20)
    site = Site(boundary=_BOUNDARY, obstacles=[obstacle], setbacks=2.0)
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        path = pathlib.Path(f.name)
    try:
        save(site, None, path)
        site2, _ = load(path)
        assert len(site2.obstacles) == 1
        assert site2.obstacles[0].area == pytest.approx(obstacle.area, abs=1e-6)
        assert site2.setbacks == pytest.approx(2.0)
    finally:
        path.unlink(missing_ok=True)
