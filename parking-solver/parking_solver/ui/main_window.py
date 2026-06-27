from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QIcon, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QDoubleSpinBox,
    QFileDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QSpinBox,
    QToolBar,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QSizePolicy,
    QWidget,
)

from parking_solver.app.controller import Controller
from parking_solver.app.workers import OptimizeWorker
from parking_solver.core.model import Layout
from parking_solver.core.optimizer import OptimizationParams
from parking_solver.io import export_ifc, export_pdf
from parking_solver.io.import_pdf import PDFTransform, calibrate, rasterise
from parking_solver.ui.canvas import ParkingCanvas
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
        self._opt_worker: Optional[OptimizeWorker] = None
        self._pdf_transform: Optional[PDFTransform] = None
        self._pdf_bytes: Optional[bytes] = None

        # ── central canvas ────────────────────────────────────────────────────
        self._canvas = ParkingCanvas(self)
        self.setCentralWidget(self._canvas)
        self._canvas.boundary_drawn.connect(self._on_boundary_drawn)
        self._canvas.stall_count_changed.connect(self._on_stall_count)
        self._canvas.draw_status_changed.connect(self.statusBar().showMessage)

        # ── right dock: parameters ────────────────────────────────────────────
        self._params_panel = ParamsPanel(self)
        self.addDockWidget(Qt.RightDockWidgetArea, self._params_panel)
        self._params_panel.params_changed.connect(self._on_params_changed)

        # ── bottom dock: Pareto explorer ──────────────────────────────────────
        self._pareto_panel = ParetoPanel(self)
        self.addDockWidget(Qt.BottomDockWidgetArea, self._pareto_panel)
        self._pareto_panel.setVisible(False)
        self._pareto_panel.candidate_selected.connect(self._on_pareto_candidate_selected)

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

        # ── Solve ─────────────────────────────────────────────────────────────
        self._act_generate = _action(
            self, "Generate", "Generate a parking layout using current parameters",
            shortcut="G")
        self._act_generate.triggered.connect(self._generate_manual)

        self._act_optimize = _action(
            self, "Optimize…",
            "Run NSGA-II multi-objective optimization and show the Pareto front",
            shortcut="O")
        self._act_optimize.triggered.connect(self._start_optimize)

        self._act_stop_opt = _action(
            self, "Stop", "Stop the running optimization")
        self._act_stop_opt.setEnabled(False)
        self._act_stop_opt.triggered.connect(self._stop_optimize)

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

        self._act_pareto = _action(
            self, "Pareto", "Show / hide the Pareto Explorer panel",
            checkable=True, checked=False)
        self._act_pareto.triggered.connect(
            lambda checked: self._pareto_panel.setVisible(checked))

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
        tb1.addSeparator()

        tb1.addWidget(_toolbar_label("SITE"))
        tb1.addAction(self._act_draw)
        tb1.addAction(self._act_set_boundary)
        tb1.addSeparator()

        tb1.addWidget(_toolbar_label("SOLVE"))
        tb1.addAction(self._act_generate)
        tb1.addAction(self._act_optimize)
        tb1.addAction(self._act_stop_opt)
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
        edit_menu.addSeparator()
        edit_menu.addAction(self._act_lock)
        edit_menu.addAction(self._act_unlock)

        # Layout ───────────────────────────────────────────────────────────────
        layout_menu = self.menuBar().addMenu("&Layout")
        layout_menu.addAction(self._act_generate)
        layout_menu.addSeparator()
        layout_menu.addAction(self._act_optimize)
        layout_menu.addAction(self._act_stop_opt)

        # Export ───────────────────────────────────────────────────────────────
        export_menu = self.menuBar().addMenu("&Export")
        export_menu.addAction(self._act_export)
        export_menu.addAction(self._act_export_pdf)
        export_menu.addAction(self._act_export_ifc)
        export_menu.addAction(self._act_export_3dm)

        # View ─────────────────────────────────────────────────────────────────
        view_menu = self.menuBar().addMenu("&View")
        view_menu.addAction(self._act_params)
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
        self.statusBar().showMessage(
            f"Boundary set — {len(pts)} vertices.  "
            "Adjust parameters and press Generate, or enable Auto-generate."
        )
        self._auto_generate()

    # ── params / generate ─────────────────────────────────────────────────────

    def _on_params_changed(self, params) -> None:
        self._ctrl.update_params(params)
        self._ctrl.update_setback(self._params_panel.current_setback())
        if self._ctrl.site is not None:
            self._run_generate()

    def _generate_manual(self) -> None:
        if self._ctrl.site is None:
            QMessageBox.information(
                self, "No site",
                "Open a DXF and set a boundary, or draw a polygon first."
            )
            return
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
            "Press Generate to re-solve around them."
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

    # ── optimizer ─────────────────────────────────────────────────────────────

    def _start_optimize(self) -> None:
        if self._ctrl.site is None:
            QMessageBox.information(
                self, "No site",
                "Open a DXF and set a boundary, or draw a polygon first."
            )
            return
        if self._opt_worker and self._opt_worker.isRunning():
            return

        opt_params = self._ask_optimize_params()
        if opt_params is None:
            return

        fixed = self._ctrl.locked_stalls()
        self._opt_worker = OptimizeWorker(
            site=self._ctrl.site,
            profile=self._ctrl.profile,
            opt_params=opt_params,
            fixed=fixed if fixed.stalls else None,
            parent=self,
        )
        self._opt_worker.generation_ready.connect(self._on_generation_ready)
        self._opt_worker.finished_ok.connect(self._on_optimize_done)
        self._opt_worker.failed.connect(self._on_optimize_failed)
        self._opt_worker.progress.connect(
            lambda cur, tot: self.statusBar().showMessage(
                f"Optimizing… generation {cur}/{tot}"
            )
        )

        self._pareto_panel.setVisible(True)
        self._act_pareto.setChecked(True)
        self._pareto_panel.start_run(opt_params.n_gen)
        self._act_optimize.setEnabled(False)
        self._act_stop_opt.setEnabled(True)
        self._opt_worker.start()

    def _stop_optimize(self) -> None:
        if self._opt_worker and self._opt_worker.isRunning():
            self._opt_worker.terminate()
            self._opt_worker.wait(2000)
        self._act_optimize.setEnabled(True)
        self._act_stop_opt.setEnabled(False)
        self.statusBar().showMessage("Optimization stopped.")

    def _on_generation_ready(self, gen: int, candidates: list) -> None:
        self._pareto_panel.update_generation(gen, candidates)

    def _on_optimize_done(self, result) -> None:
        self._pareto_panel.finish_run(result)
        self._act_optimize.setEnabled(True)
        self._act_stop_opt.setEnabled(False)
        self.statusBar().showMessage(
            f"Optimization complete — {len(result.candidates)} Pareto candidates.  "
            "Click a point in the Pareto Explorer to load a layout."
        )

    def _on_optimize_failed(self, msg: str) -> None:
        self._act_optimize.setEnabled(True)
        self._act_stop_opt.setEnabled(False)
        QMessageBox.critical(self, "Optimization failed", msg)

    def _on_pareto_candidate_selected(self, layout: Layout) -> None:
        self._ctrl.layout = layout
        self._canvas.show_layout(layout)
        m = layout.metrics
        self.statusBar().showMessage(
            f"Loaded Pareto candidate — {m.total_stalls} stalls  ·  "
            f"{layout.params.angle:.0f}°  ·  {m.gross_area_per_stall:.1f} m²/stall"
        )

    def _ask_optimize_params(self) -> OptimizationParams | None:
        dlg = QDialog(self)
        dlg.setWindowTitle("Optimization settings")
        form = QFormLayout(dlg)

        pop_spin = QSpinBox()
        pop_spin.setRange(10, 200)
        pop_spin.setValue(40)
        pop_spin.setSingleStep(10)
        pop_spin.setToolTip("Larger population → broader search, slower per generation")
        form.addRow("Population size:", pop_spin)

        gen_spin = QSpinBox()
        gen_spin.setRange(5, 500)
        gen_spin.setValue(30)
        gen_spin.setSingleStep(5)
        gen_spin.setToolTip("More generations → refined front, more time")
        form.addRow("Generations:", gen_spin)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        form.addRow(btns)

        if dlg.exec() != QDialog.Accepted:
            return None
        return OptimizationParams(pop_size=pop_spin.value(), n_gen=gen_spin.value())

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
        p1x = QDoubleSpinBox(); p1x.setRange(0, 99999); p1x.setValue(0)
        p1y = QDoubleSpinBox(); p1y.setRange(0, 99999); p1y.setValue(0)
        p2x = QDoubleSpinBox(); p2x.setRange(0, 99999); p2x.setValue(100)
        p2y = QDoubleSpinBox(); p2y.setRange(0, 99999); p2y.setValue(0)
        dist = QDoubleSpinBox()
        dist.setRange(0.01, 9999); dist.setValue(10.0); dist.setSuffix(" m")
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
            if self._ctrl.layout:
                self._canvas.show_layout(self._ctrl.layout)
            self.statusBar().showMessage(f"Project loaded from {Path(path).name}")
        except Exception as exc:
            QMessageBox.critical(self, "Load failed", str(exc))

    # ── misc ──────────────────────────────────────────────────────────────────

    def _on_stall_count(self, count: int) -> None:
        self._stall_label.setText(f"Stalls: {count}")
