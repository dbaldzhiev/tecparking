"""UI smoke tests — construct real widgets to catch startup/runtime errors.

These guard against the class of bug where the app imports fine but crashes the
moment a widget is *constructed* (e.g. connecting to a signal that doesn't exist).
Runs headless via the Qt 'offscreen' platform plugin.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path

import pytest
from shapely.geometry import Point, box

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from parking_solver.core.generator import generate, generate_all
from parking_solver.core.model import (
    AisleDir,
    Entrance,
    EntranceKind,
    LayoutParams,
    Site,
)
from parking_solver.core.regulations.engine import load_profile
from parking_solver.core.selection import curate_variants

_PROFILE = (
    Path(__file__).parent.parent
    / "parking_solver" / "core" / "regulations" / "profiles" / "bulgarian.yaml"
)


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def profile():
    return load_profile(_PROFILE)


@pytest.fixture(scope="module")
def site():
    return Site(
        boundary=box(0, 0, 60, 40),
        entrances=[Entrance(point=Point(30, 0), kind=EntranceKind.SITE)],
    )


def test_mainwindow_constructs(app):
    """The whole window must build without raising (regression: bad signal name)."""
    from parking_solver.ui.main_window import MainWindow
    w = MainWindow()
    assert w is not None
    w.close()


def test_canvas_renders_layout(app, site, profile):
    """Canvas must accept a real layout (exercises AisleItem flow + EntranceItem)."""
    from parking_solver.ui.canvas import ParkingCanvas
    params = LayoutParams(orientation=0.0, angle=45.0, stall_width=2.5,
                          stall_length=5.0, aisle_dir=AisleDir.ONE_WAY)
    layout = generate(site, profile, params)
    canvas = ParkingCanvas()
    canvas.show_boundary(site.boundary)
    canvas.show_entrances(site.entrances)
    canvas.show_layout(layout)


def test_dashboard_set_variants(app, site, profile):
    """Dashboard must build cards (with mini-previews) from curated variants."""
    from parking_solver.ui.dashboard_panel import DashboardPanel
    results = generate_all(site, profile)
    variants = curate_variants(results, k=5)
    panel = DashboardPanel()
    panel.set_variants(variants, site)
    panel.set_variants([], site)   # empty case must not raise


def test_explore_panel_constructs(app):
    from parking_solver.ui.explore_panel import ExplorePanel
    panel = ExplorePanel()
    assert panel is not None


def test_explore_panel_streams_results(app, site, profile):
    """Results must fill the table live and emit a best-so-far preview."""
    from parking_solver.ui.explore_panel import ExplorePanel
    panel = ExplorePanel()
    panel._last_site = site
    panel._last_profile = profile
    panel._streaming = True

    results = generate_all(site, profile)
    assert results, "need results to test streaming"

    previews = []
    panel.live_preview.connect(lambda lay: previews.append(lay))

    for r in results[:6]:
        panel._on_result(r)
    assert panel._table.rowCount() == 6           # list fills live
    assert len(previews) >= 1                      # best-so-far previewed during run

    panel._on_done(results)
    assert panel._table.rowCount() == len(results)  # final sorted population
    assert panel._streaming is False


def test_explore_panel_click_stops_autofollow(app, site, profile):
    from parking_solver.ui.explore_panel import ExplorePanel
    panel = ExplorePanel()
    panel._streaming = True
    results = generate_all(site, profile)
    for r in results[:3]:
        panel._on_result(r)
    selected = []
    panel.layout_selected.connect(lambda lay: selected.append(lay))
    panel._on_row_clicked(1)
    assert panel._user_interacted is True
    assert len(selected) == 1   # user click previews that row
