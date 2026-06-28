from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QDoubleSpinBox,
    QFileDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QToolBar,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QSizePolicy,
    QWidget,
)

from parking_solver.app.controller import Controller
from parking_solver.core.model import Layout
from parking_solver.io import export_ifc, export_pdf
from parking_solver.io.import_pdf import PDFTransform, calibrate, rasterise
from parking_solver.core.selection import curate_variants
from parking_solver.ui.canvas import ParkingCanvas
from parking_solver.ui.dashboard_panel import DashboardPanel
from parking_solver.ui.explore_panel import ExplorePanel, strategy_results_to_pareto
from parking_solver.ui.params_panel import ParamsPanel
from parking_solver.ui.pareto.pareto_panel import ParetoPanel



def _action(parent, label: str, tip: str, shortcut=None,
            checkable: bool = False, checked: bool = False) -> QAction:
    """Factory: create a QAction with tooltip and optional shortcut.

    *shortcut* may be a plain string (e.g. "G"), a QKeySequence.StandardKey
    enum value (e.g. QKeySequence.Open), or None.
    """
    a = QAction(label, parent)
    if shortcut is not None:
        a.setShortcut(shortcut)
        # Build a human-readable shortcut string for the tooltip
        if isinstance(shortcut, str):
            shortcut_str = shortcut
        else:
            shortcut_str = QKeySequence(shortcut).toString()
        a.setToolTip(f"{tip}  [{shortcut_str}]")
    else:
        a.setToolTip(tip)
    a.setStatusTip(tip)
    a.setCheckable(checkable)
    a.setChecked(checked)
    return a


def _toolbar_label(text: str) -> QLabel:
    """Small grey section label inside a toolbar."""
    lbl = QLabel(f"  {text}  ")
    lbl.setStyleSheet("color: #888; font-size: 10px;")
    return lbl


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Parking Layout Generator")
        self.resize(1440, 900)

        self._ctrl = Controller()
        self._pdf_transform: Optional[PDFTransform] = None
        self._pdf_bytes: Optional[bytes] = None

        # ── central canvas ────────────────────────────────────────────────────
        self._canvas = ParkingCanvas(self)
        self.setCentralWidget(self._canvas)
        self._canvas.boundary_drawn.connect(self._on_boundary_drawn)
        self._canvas.stall_count_changed.connect(self._on_stall_count)
        self._canvas.draw_status_changed.connect(self.statusBar().showMessage)
        self._canvas.entrance_placed.connect(self._on_entrance_placed)

        # ── right dock: parameters ────────────────────────────────────────────
        self._params_panel = ParamsPanel(self)
        self.addDockWidget(Qt.RightDockWidgetArea, self._params_panel)
        self._params_panel.params_changed.connect(self._on_params_changed)
        self._params_panel.profile_changed.connect(self._on_profile_changed)

        # ── left dock: exploration results ────────────────────────────────────
        self._explore_panel = ExplorePanel(self)
        self.addDockWidget(Qt.LeftDockWidgetArea, self._explore_panel)
        self._explore_panel.layout_selected.connect(self._on_explore_layout_selected)
        self._explore_panel.live_preview.connect(self._on_explore_live_preview)
        self._explore_panel.results_ready.connect(self._on_explore_results_ready)

        # ── bottom dock: Pareto explorer ──────────────────────────────────────
        self._pareto_panel = ParetoPanel(self)
        self.addDockWidget(Qt.BottomDockWidgetArea, self._pareto_panel)
        self._pareto_panel.setVisible(False)
        self._pareto_panel.candidate_selected.connect(self._on_pareto_candidate_selected)

        # ── right dock: curated variants dashboard ────────────────────────────
        self._dashboard_panel = DashboardPanel(self)
        self.addDockWidget(Qt.RightDockWidgetArea, self._dashboard_panel)
        self.tabifyDockWidget(self._params_panel, self._dashboard_panel)
        self._params_panel.raise_()
        self._dashboard_panel.layout_selected.connect(self._on_explore_layout_selected)

        # ── status bar ────────────────────────────────────────────────────────
        self._stall_label = QLabel("Stalls: —")
        self._stall_label.setStyleSheet("padding-right: 8px;")
        self.statusBar().addPermanentWidget(self._stall_label)
        self.statusBar().showMessage("Open a DXF, draw a polygon, or load a project to start.")

        self._build_actions()
        self._build_toolbars()
        self._build_menu()

    # ── action factory ────────────────────────────────────────────────────────

    def _build_actions(self) -> None:

        # ── Import ────────────────────────────────────────────────────────────
        self._act_open = _action(
            self, "Open DXF", "Import a DXF file — pick entities to use as the site boundary",
            shortcut=QKeySequence.Open)
        self._act_open.triggered.connect(self._open_dxf)

        self._act_open_pdf = _action(
            self, "PDF Underlay", "Rasterise a PDF page as a background reference image")
        self._act_open_pdf.triggered.connect(self._open_pdf)

        self._act_osm = _action(
            self, "OSM Map",
            "Toggle a black-and-white OpenStreetMap backdrop (default: Plovdiv)",
            checkable=True)
        self._act_osm.triggered.connect(self._toggle_osm)

        self._act_calibrate_pdf = _action(
            self, "Calibrate Scale…", "Two-point pixel→metre calibration for the PDF underlay")
        self._act_calibrate_pdf.triggered.connect(self._calibrate_pdf)

        self._act_open_dwg = _action(
            self, "Open DWG", "Import a DWG file (requires ODAFileConverter or dwg2dxf on PATH)")
        self._act_open_dwg.triggered.connect(self._open_dwg)

        self._act_save = _action(
            self, "Save", "Save current project to a JSON file",
            shortcut=QKeySequence.Save)
        self._act_save.triggered.connect(self._save_project)

        self._act_load = _action(
            self, "Load", "Load a previously saved project JSON file")
        self._act_load.triggered.connect(self._load_project)

        # ── Draw / Boundary ───────────────────────────────────────────────────
        self._act_draw = _action(
            self, "Draw Polygon", "Click vertices to draw a site boundary polygon — Esc to cancel",
            shortcut="D", checkable=True)
        self._act_draw.triggered.connect(self._toggle_draw)

        self._act_set_boundary = _action(
            self, "Set as Boundary",
            "Use selected DXF entities as the site boundary",
            shortcut="B")
        self._act_set_boundary.triggered.connect(self._set_boundary)

        self._act_add_entrance = _action(
            self, "Add Entrance",
            "Click inside the site to add a vehicle entrance — roads auto-connect to it",
            shortcut="N", checkable=True)
        self._act_add_entrance.triggered.connect(self._toggle_entrance_mode)

        self._act_clear_entrances = _action(
            self, "Clear Entrances",
            "Remove all entrances from the site")
        self._act_clear_entrances.triggered.connect(self._clear_entrances)

        # ── Solve ─────────────────────────────────────────────────────────────
        # The canvas auto-updates on any parameter change; Explore runs the full
        # analyzer-driven search and streams the results into the list + Dashboard.
        self._act_explore = _action(
            self, "Explore",
            "Analyze the site and try the applicable strategies — results stream "
            "into the list and Dashboard live; click any row to preview while it runs",
            shortcut="E")
        self._act_explore.triggered.connect(self._start_explore)

        # ── Selection / Lock ──────────────────────────────────────────────────
        self._act_lock = _action(
            self, "Lock", "Lock selected stalls — they survive re-solve as fixed elements",
            shortcut="L")
        self._act_lock.triggered.connect(self._lock_selected)

        self._act_unlock = _action(
            self, "Unlock", "Unlock selected stalls so the solver can replace them",
            shortcut="U")
        self._act_unlock.triggered.connect(self._unlock_selected)

        # ── Export ────────────────────────────────────────────────────────────
        self._act_export = _action(
            self, "Export DXF", "Export layout to a layered AutoCAD DXF file")
        self._act_export.triggered.connect(self._export_dxf)

        self._act_export_pdf = _action(
            self, "Export PDF", "Export a permit-style A3 documentation sheet (PDF)")
        self._act_export_pdf.triggered.connect(self._export_pdf)

        self._act_export_ifc = _action(
            self, "Export IFC", "Export stalls as IfcSpace entities (IFC4, Revit / ArchiCAD)")
        self._act_export_ifc.triggered.connect(self._export_ifc)

        self._act_export_3dm = _action(
            self, "Export .3dm", "Export to a Rhino .3dm file (requires rhino3dm + CMake)")
        self._act_export_3dm.triggered.connect(self._export_3dm)

        # ── View toggles ──────────────────────────────────────────────────────
        self._act_params = _action(
            self, "Parameters", "Show / hide the Parameters panel",
            checkable=True, checked=True)
        self._act_params.triggered.connect(
            lambda checked: self._params_panel.setVisible(checked))

        self._act_explore_panel = _action(
            self, "Explore Panel", "Show / hide the Exploration Results panel",
            checkable=True, checked=True)
        self._act_explore_panel.triggered.connect(
            lambda checked: self._explore_panel.setVisible(checked))
        self._explore_panel.visibilityChanged.connect(self._act_explore_panel.setChecked)

        self._act_pareto = _action(
            self, "Pareto", "Show / hide the Pareto Explorer panel",
            checkable=True, checked=False)
        self._act_pareto.triggered.connect(
            lambda checked: self._pareto_panel.setVisible(checked))

        self._act_dashboard = _action(
            self, "Dashboard", "Show / hide the curated-variants Dashboard",
            checkable=True, checked=True)
        self._act_dashboard.triggered.connect(
            lambda checked: self._dashboard_panel.setVisible(checked))
        self._dashboard_panel.visibilityChanged.connect(self._act_dashboard.setChecked)

    def _build_toolbars(self) -> None:
        """Two toolbars: Import/Draw/Solve on top; Select/Export/View below."""

        # ── Toolbar 1: workflow ───────────────────────────────────────────────
        tb1 = QToolBar("Workflow", self)
        tb1.setObjectName("toolbar_workflow")
        tb1.setMovable(False)
        self.addToolBar(Qt.TopToolBarArea, tb1)

        tb1.addWidget(_toolbar_label("IMPORT"))
        tb1.addAction(self._act_open)
        tb1.addAction(self._act_open_pdf)
        tb1.addAction(self._act_calibrate_pdf)
        tb1.addAction(self._act_open_dwg)
        tb1.addAction(self._act_osm)
        tb1.addSeparator()

        tb1.addWidget(_toolbar_label("SITE"))
        tb1.addAction(self._act_draw)
        tb1.addAction(self._act_set_boundary)
        tb1.addAction(self._act_add_entrance)
        tb1.addAction(self._act_clear_entrances)
        tb1.addSeparator()

        tb1.addWidget(_toolbar_label("SOLVE"))
        tb1.addAction(self._act_explore)
        tb1.addSeparator()

        # Push remaining items to the right
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        tb1.addWidget(spacer)

        tb1.addWidget(_toolbar_label("PROJECT"))
        tb1.addAction(self._act_save)
        tb1.addAction(self._act_load)

        # ── Toolbar 2: selection + export + view ──────────────────────────────
        tb2 = QToolBar("Selection & Export", self)
        tb2.setObjectName("toolbar_select_export")
        tb2.setMovable(False)
        self.addToolBar(Qt.TopToolBarArea, tb2)

        tb2.addWidget(_toolbar_label("SELECT"))
        tb2.addAction(self._act_lock)
        tb2.addAction(self._act_unlock)
        tb2.addSeparator()

        tb2.addWidget(_toolbar_label("EXPORT"))
        tb2.addAction(self._act_export)
        tb2.addAction(self._act_export_pdf)
        tb2.addAction(self._act_export_ifc)
        tb2.addAction(self._act_export_3dm)
        tb2.addSeparator()

        spacer2 = QWidget()
        spacer2.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        tb2.addWidget(spacer2)

        tb2.addWidget(_toolbar_label("VIEW"))
        tb2.addAction(self._act_params)
        tb2.addAction(self._act_explore_panel)
        tb2.addAction(self._act_dashboard)
        tb2.addAction(self._act_pareto)

    def _build_menu(self) -> None:
        # File ─────────────────────────────────────────────────────────────────
        file_menu = self.menuBar().addMenu("&File")
        file_menu.addAction(self._act_open)
        file_menu.addAction(self._act_open_pdf)
        file_menu.addAction(self._act_calibrate_pdf)
        file_menu.addAction(self._act_open_dwg)
        file_menu.addSeparator()
        file_menu.addAction(self._act_save)
        file_menu.addAction(self._act_load)
        file_menu.addSeparator()
        quit_act = QAction("&Quit", self)
        quit_act.setShortcut(QKeySequence.Quit)
        quit_act.triggered.connect(QApplication.instance().quit)
        file_menu.addAction(quit_act)

        # Edit ─────────────────────────────────────────────────────────────────
        edit_menu = self.menuBar().addMenu("&Edit")
        edit_menu.addAction(self._act_draw)
        edit_menu.addAction(self._act_set_boundary)
        edit_menu.addAction(self._act_add_entrance)
        edit_menu.addAction(self._act_clear_entrances)
        edit_menu.addSeparator()
        edit_menu.addAction(self._act_lock)
        edit_menu.addAction(self._act_unlock)

        # Layout ───────────────────────────────────────────────────────────────
        layout_menu = self.menuBar().addMenu("&Layout")
        layout_menu.addAction(self._act_explore)

        # Export ───────────────────────────────────────────────────────────────
        export_menu = self.menuBar().addMenu("&Export")
        export_menu.addAction(self._act_export)
        export_menu.addAction(self._act_export_pdf)
        export_menu.addAction(self._act_export_ifc)
        export_menu.addAction(self._act_export_3dm)

        # View ─────────────────────────────────────────────────────────────────
        view_menu = self.menuBar().addMenu("&View")
        view_menu.addAction(self._act_params)
        view_menu.addAction(self._act_explore_panel)
        view_menu.addAction(self._act_dashboard)
        view_menu.addAction(self._act_pareto)

    # ── DXF / boundary slots ──────────────────────────────────────────────────

    def _open_dxf(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Open DXF", "", "DXF Files (*.dxf);;All Files (*)"
        )
        if not path:
            return
        try:
            entities = self._ctrl.open_dxf(path)
            self._canvas.show_dxf_entities(entities)
            self.statusBar().showMessage(
                f"Loaded {len(entities)} entities from {Path(path).name}. "
                "Click to select, then 'Set as Boundary'."
            )
        except Exception as exc:
            QMessageBox.critical(self, "Error opening DXF", str(exc))

    def _toggle_draw(self, checked: bool) -> None:
        self._canvas.set_draw_mode(checked)
        if not checked and not self._ctrl.site:
            self.statusBar().showMessage("Drawing cancelled.")

    def _set_boundary(self) -> None:
        handles = self._canvas.selected_handles()
        if not handles:
            QMessageBox.information(
                self, "No selection",
                "Click DXF entities to select them, then press 'Set as Boundary'."
            )
            return
        try:
            self._ctrl.set_boundary_from_entities(handles)
            self._canvas.show_boundary(self._ctrl.site.boundary)
            self._notify_site_changed()
            self.statusBar().showMessage(
                f"Boundary set from {len(handles)} selected entities."
            )
            self._auto_generate()
        except Exception as exc:
            QMessageBox.critical(self, "Error setting boundary", str(exc))

    def _on_boundary_drawn(self, pts: list) -> None:
        self._act_draw.setChecked(False)
        self._ctrl.set_boundary_from_polygon(pts)
        self._canvas.show_boundary(self._ctrl.site.boundary)
        self._notify_site_changed()
        self.statusBar().showMessage(
            f"Boundary set — {len(pts)} vertices.  "
            "Adjust parameters — the canvas updates automatically; press Explore for options."
        )
        self._auto_generate()

    def _toggle_entrance_mode(self, checked: bool) -> None:
        if self._ctrl.site is None:
            self._act_add_entrance.setChecked(False)
            QMessageBox.information(
                self, "No site", "Set a boundary or draw a polygon first."
            )
            return
        self._canvas.set_entrance_mode(checked)

    def _on_entrance_placed(self, x: float, y: float) -> None:
        self._ctrl.add_entrance(x, y)
        self._canvas.show_entrances(self._ctrl.site.entrances)
        n = len(self._ctrl.site.entrances)
        self.statusBar().showMessage(f"Entrance added — {n} total.")
        self._auto_generate()

    def _clear_entrances(self) -> None:
        if self._ctrl.site is None:
            return
        self._ctrl.clear_entrances()
        self._canvas.show_entrances([])
        self.statusBar().showMessage("All entrances removed.")
        self._auto_generate()

    def _notify_site_changed(self) -> None:
        """Update UI elements that depend on the current site polygon."""
        poly = self._ctrl.site.boundary if self._ctrl.site else None
        self._params_panel.set_site_polygon(poly)
        if self._ctrl.site:
            self._canvas.show_entrances(self._ctrl.site.entrances)
            self._trigger_auto_explore()

    # ── params / generate ─────────────────────────────────────────────────────

    def _on_profile_changed(self, profile) -> None:
        self._ctrl.profile = profile
        if self._ctrl.site is not None:
            self._trigger_auto_explore()
            self._run_generate()

    def _on_params_changed(self, params) -> None:
        self._ctrl.update_params(params)
        self._ctrl.update_setback(self._params_panel.current_setback())
        if self._ctrl.site is not None:
            self._run_generate()

    def _auto_generate(self) -> None:
        if self._params_panel.auto_generate and self._ctrl.site is not None:
            self._run_generate()

    def _run_generate(self) -> None:
        try:
            layout = self._ctrl.generate()
            self._canvas.show_layout(layout)
            m = layout.metrics
            by = ", ".join(
                f"{v} {k}" for k, v in sorted(m.by_type.items(), key=lambda kv: -kv[1])
            )
            self.statusBar().showMessage(
                f"{m.total_stalls} stalls  "
                f"({by})  ·  "
                f"{m.gross_area_per_stall:.1f} m²/stall  ·  "
                f"{layout.params.angle:.0f}°  {layout.params.orientation:.0f}°"
            )
        except Exception as exc:
            QMessageBox.critical(self, "Generate failed", str(exc))

    # ── lock / unlock ─────────────────────────────────────────────────────────

    def _lock_selected(self) -> None:
        indices = self._canvas.selected_stall_indices()
        if not indices:
            self.statusBar().showMessage(
                "Select stalls first: drag a rubber-band selection over them."
            )
            return
        self._ctrl.set_stalls_locked(indices, True)
        for i in indices:
            self._canvas.set_stall_locked(i, True)
        self.statusBar().showMessage(
            f"Locked {len(indices)} stall(s) — shown with red border.  "
            "They survive re-solves; the canvas updates automatically."
        )

    def _unlock_selected(self) -> None:
        indices = self._canvas.selected_stall_indices()
        if not indices:
            self.statusBar().showMessage(
                "Select stalls first: drag a rubber-band selection over them."
            )
            return
        self._ctrl.set_stalls_locked(indices, False)
        for i in indices:
            self._canvas.set_stall_locked(i, False)
        self.statusBar().showMessage(f"Unlocked {len(indices)} stall(s).")

    # ── explore all ───────────────────────────────────────────────────────────

    def _trigger_auto_explore(self) -> None:
        """Start exploration in the ExplorePanel with current stall params."""
        p = self._params_panel.current_params()
        self._explore_panel.start_explore(
            self._ctrl.site, self._ctrl.profile,
            stall_width=p.stall_width,
            stall_length=p.stall_length,
        )

    def _start_explore(self) -> None:
        """Toolbar/menu action: show the explore panel, re-explore if no site."""
        if self._ctrl.site is None:
            QMessageBox.information(
                self, "No site",
                "Open a DXF and set a boundary, or draw a polygon first."
            )
            return
        self._explore_panel.setVisible(True)
        self._explore_panel.raise_()
        self._act_explore_panel.setChecked(True)

    def _on_explore_live_preview(self, layout: Layout) -> None:
        """Best-so-far layout streamed in while exploration is still running."""
        self._ctrl.layout = layout
        self._canvas.show_layout(layout)
        self.statusBar().showMessage(
            f"Exploring… best so far: {layout.metrics.total_stalls} stalls "
            f"({layout.params.layout_type.value.replace('_', ' ')})"
        )

    def _on_explore_layout_selected(self, layout: Layout) -> None:
        """User single-clicked or double-clicked a row in the ExplorePanel."""
        self._ctrl.layout = layout
        self._canvas.show_layout(layout)
        m = layout.metrics
        self.statusBar().showMessage(
            f"Explore layout — {m.total_stalls} stalls  ·  "
            f"{layout.params.layout_type.value.replace('_', ' ').title()}  ·  "
            f"{layout.params.angle:.0f}°  ·  {layout.params.orientation:.0f}°  ·  "
            f"{m.gross_area_per_stall:.1f} m²/stall"
        )

    def _on_explore_results_ready(self, results: list) -> None:
        """Feed all explore results into the Pareto scatter and the curated dashboard."""
        pareto_result = strategy_results_to_pareto(results)
        self._pareto_panel.load_explore_results(results, pareto_result)
        self._pareto_panel.setVisible(True)
        self._act_pareto.setChecked(True)

        # Curated variants → dashboard
        site = self._ctrl.site
        variants = curate_variants(results, k=5)
        self._dashboard_panel.set_variants(variants, site)
        self._dashboard_panel.setVisible(True)
        self._dashboard_panel.raise_()
        self._act_dashboard.setChecked(True)

    def _on_pareto_candidate_selected(self, layout: Layout) -> None:
        self._ctrl.layout = layout
        self._canvas.show_layout(layout)
        m = layout.metrics
        self.statusBar().showMessage(
            f"Loaded candidate — {m.total_stalls} stalls  ·  "
            f"{layout.params.angle:.0f}°  ·  {m.gross_area_per_stall:.1f} m²/stall"
        )

    # ── OSM underlay ──────────────────────────────────────────────────────────

    def _toggle_osm(self, checked: bool) -> None:
        if not checked:
            self._canvas.clear_osm_underlay()
            self.statusBar().showMessage("OSM map hidden.")
            return
        from parking_solver.io import osm
        QApplication.setOverrideCursor(Qt.WaitCursor)
        self.statusBar().showMessage("Fetching OpenStreetMap tiles (Plovdiv)…")
        try:
            tiles, w_px, h_px, m_per_px = osm.fetch_osm_area()
            self._canvas.show_osm_underlay(tiles, w_px, h_px, m_per_px)
            self.statusBar().showMessage("OSM map loaded (black & white).")
        except Exception as exc:  # noqa: BLE001
            self._act_osm.setChecked(False)
            QMessageBox.warning(
                self, "OSM unavailable",
                f"Could not fetch OpenStreetMap tiles (need internet):\n{exc}"
            )
        finally:
            QApplication.restoreOverrideCursor()

    # ── PDF underlay ──────────────────────────────────────────────────────────

    def _open_pdf(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Open PDF Underlay", "", "PDF Files (*.pdf);;All Files (*)"
        )
        if not path:
            return
        try:
            png_bytes, w_px, h_px = rasterise(path, dpi=150)
            self._pdf_bytes = png_bytes
            raw_tf = PDFTransform(origin_px=(0.0, 0.0), scale=1.0)
            self._pdf_transform = raw_tf
            self._canvas.show_pdf_underlay(png_bytes, w_px, h_px, raw_tf)
            self.statusBar().showMessage(
                f"PDF loaded ({w_px}×{h_px} px).  "
                "Use Export → Calibrate Scale… to set the real-world scale."
            )
        except Exception as exc:
            QMessageBox.critical(self, "Error opening PDF", str(exc))

    def _calibrate_pdf(self) -> None:
        if self._pdf_bytes is None:
            QMessageBox.information(self, "No PDF", "Open a PDF underlay first.")
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("Calibrate PDF Scale")
        form = QFormLayout(dlg)
        form.addRow(QLabel(
            "Enter two points in pixel space and the known real distance between them.\n"
            "Read pixel coordinates from the image (hover over it in an image viewer)."
        ))
        def _px_spin(value: float) -> QDoubleSpinBox:
            spin = QDoubleSpinBox()
            spin.setRange(0, 99999)
            spin.setValue(value)
            return spin

        p1x = _px_spin(0)
        p1y = _px_spin(0)
        p2x = _px_spin(100)
        p2y = _px_spin(0)
        dist = QDoubleSpinBox()
        dist.setRange(0.01, 9999)
        dist.setValue(10.0)
        dist.setSuffix(" m")
        form.addRow("Point 1 X (px):", p1x)
        form.addRow("Point 1 Y (px):", p1y)
        form.addRow("Point 2 X (px):", p2x)
        form.addRow("Point 2 Y (px):", p2y)
        form.addRow("Real distance:", dist)
        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        form.addRow(btns)
        if dlg.exec() != QDialog.Accepted:
            return
        try:
            tf = calibrate(
                (p1x.value(), p1y.value()),
                (p2x.value(), p2y.value()),
                dist.value(),
            )
            self._pdf_transform = tf
            self._canvas.show_pdf_underlay(self._pdf_bytes, 0, 0, tf)
            self.statusBar().showMessage(
                f"PDF calibrated — scale {tf.scale * 1000:.2f} mm/pixel"
            )
        except Exception as exc:
            QMessageBox.critical(self, "Calibration failed", str(exc))

    def _open_dwg(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Open DWG", "", "DWG Files (*.dwg);;All Files (*)"
        )
        if not path:
            return
        try:
            from parking_solver.io import import_dwg
            doc = import_dwg.load_doc(path)
            entities = import_dwg.list_entities(doc)
            self._canvas.show_dxf_entities(entities)
            self.statusBar().showMessage(
                f"Loaded {len(entities)} entities from {Path(path).name} via DWG converter.  "
                "Select entities, then 'Set as Boundary'."
            )
        except Exception as exc:
            QMessageBox.critical(self, "Error opening DWG", str(exc))

    # ── export ────────────────────────────────────────────────────────────────

    def _export_dxf(self) -> None:
        if self._ctrl.layout is None:
            QMessageBox.information(self, "No layout", "Generate a layout first.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export DXF", "parking_layout.dxf", "DXF Files (*.dxf)"
        )
        if not path:
            return
        try:
            self._ctrl.export_dxf(path)
            self.statusBar().showMessage(f"DXF exported → {Path(path).name}")
        except Exception as exc:
            QMessageBox.critical(self, "Export failed", str(exc))

    def _export_pdf(self) -> None:
        if self._ctrl.layout is None:
            QMessageBox.information(self, "No layout", "Generate a layout first.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export PDF Documentation", "parking_layout.pdf", "PDF Files (*.pdf)"
        )
        if not path:
            return
        try:
            export_pdf.export(self._ctrl.layout, self._ctrl.site, path)
            self.statusBar().showMessage(f"PDF exported → {Path(path).name}")
        except Exception as exc:
            QMessageBox.critical(self, "PDF export failed", str(exc))

    def _export_ifc(self) -> None:
        if self._ctrl.layout is None:
            QMessageBox.information(self, "No layout", "Generate a layout first.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export IFC", "parking_layout.ifc", "IFC Files (*.ifc)"
        )
        if not path:
            return
        try:
            export_ifc.export(self._ctrl.layout, self._ctrl.site, path)
            self.statusBar().showMessage(f"IFC exported → {Path(path).name}")
        except Exception as exc:
            QMessageBox.critical(self, "IFC export failed", str(exc))

    def _export_3dm(self) -> None:
        if self._ctrl.layout is None:
            QMessageBox.information(self, "No layout", "Generate a layout first.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Rhino .3dm", "parking_layout.3dm", "Rhino Files (*.3dm)"
        )
        if not path:
            return
        try:
            from parking_solver.io import export_3dm
            export_3dm.export(self._ctrl.layout, self._ctrl.site, path)
            self.statusBar().showMessage(f".3dm exported → {Path(path).name}")
        except ImportError as exc:
            QMessageBox.warning(
                self, "rhino3dm not installed",
                f"{exc}\n\nInstall CMake, then:  pip install rhino3dm"
            )
        except Exception as exc:
            QMessageBox.critical(self, ".3dm export failed", str(exc))

    # ── project IO ────────────────────────────────────────────────────────────

    def _save_project(self) -> None:
        if self._ctrl.site is None:
            QMessageBox.information(self, "Nothing to save", "Open or draw a boundary first.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Project", "project.json", "JSON Files (*.json)"
        )
        if not path:
            return
        try:
            self._ctrl.save_project(path)
            self.statusBar().showMessage(f"Project saved → {Path(path).name}")
        except Exception as exc:
            QMessageBox.critical(self, "Save failed", str(exc))

    def _load_project(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Load Project", "", "JSON Files (*.json)"
        )
        if not path:
            return
        try:
            self._ctrl.load_project(path)
            if self._ctrl.site:
                self._canvas.show_boundary(self._ctrl.site.boundary)
                self._notify_site_changed()
            if self._ctrl.layout:
                self._canvas.show_layout(self._ctrl.layout)
            self.statusBar().showMessage(f"Project loaded from {Path(path).name}")
        except Exception as exc:
            QMessageBox.critical(self, "Load failed", str(exc))

    # ── misc ──────────────────────────────────────────────────────────────────

    def _on_stall_count(self, count: int) -> None:
        self._stall_label.setText(f"Stalls: {count}")
