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
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QSpinBox,
    QToolBar,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
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


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Parking Layout Generator — Phase 4")
        self.resize(1400, 900)

        self._ctrl = Controller()
        self._opt_worker: Optional[OptimizeWorker] = None
        self._pdf_transform: Optional[PDFTransform] = None
        self._pdf_bytes: Optional[bytes] = None
        self._pdf_cal_points: list[tuple[float, float]] = []   # pixel coords for calibration

        # ── central canvas ────────────────────────────────────────────────────
        self._canvas = ParkingCanvas(self)
        self.setCentralWidget(self._canvas)
        self._canvas.boundary_drawn.connect(self._on_boundary_drawn)
        self._canvas.stall_count_changed.connect(self._on_stall_count)

        # ── params dock ───────────────────────────────────────────────────────
        self._params_panel = ParamsPanel(self)
        self.addDockWidget(Qt.RightDockWidgetArea, self._params_panel)
        self._params_panel.params_changed.connect(self._on_params_changed)

        # ── Pareto dock ───────────────────────────────────────────────────────
        self._pareto_panel = ParetoPanel(self)
        self.addDockWidget(Qt.BottomDockWidgetArea, self._pareto_panel)
        self._pareto_panel.setVisible(False)
        self._pareto_panel.candidate_selected.connect(self._on_pareto_candidate_selected)

        # ── status bar ────────────────────────────────────────────────────────
        self._stall_label = QLabel("Stalls: 0")
        self.statusBar().addPermanentWidget(self._stall_label)

        self._build_actions()
        self._build_toolbar()
        self._build_menu()

    # ── actions ───────────────────────────────────────────────────────────────

    def _build_actions(self) -> None:
        self._act_open = QAction("Open DXF", self)
        self._act_open.setShortcut(QKeySequence.Open)
        self._act_open.triggered.connect(self._open_dxf)

        self._act_draw = QAction("Draw Polygon", self)
        self._act_draw.setCheckable(True)
        self._act_draw.setShortcut("D")
        self._act_draw.triggered.connect(self._toggle_draw)

        self._act_set_boundary = QAction("Set as Boundary", self)
        self._act_set_boundary.setShortcut("B")
        self._act_set_boundary.triggered.connect(self._set_boundary)

        self._act_generate = QAction("Generate", self)
        self._act_generate.setShortcut("G")
        self._act_generate.triggered.connect(self._generate_manual)

        self._act_optimize = QAction("Optimize…", self)
        self._act_optimize.setShortcut("O")
        self._act_optimize.triggered.connect(self._start_optimize)

        self._act_stop_opt = QAction("Stop", self)
        self._act_stop_opt.setEnabled(False)
        self._act_stop_opt.triggered.connect(self._stop_optimize)

        self._act_lock = QAction("Lock Stalls", self)
        self._act_lock.setShortcut("L")
        self._act_lock.triggered.connect(self._lock_selected)

        self._act_unlock = QAction("Unlock Stalls", self)
        self._act_unlock.setShortcut("U")
        self._act_unlock.triggered.connect(self._unlock_selected)

        self._act_open_pdf = QAction("Open PDF Underlay…", self)
        self._act_open_pdf.triggered.connect(self._open_pdf)

        self._act_calibrate_pdf = QAction("Calibrate PDF Scale…", self)
        self._act_calibrate_pdf.triggered.connect(self._calibrate_pdf)

        self._act_open_dwg = QAction("Open DWG…", self)
        self._act_open_dwg.triggered.connect(self._open_dwg)

        self._act_export = QAction("Export DXF", self)
        self._act_export.triggered.connect(self._export_dxf)

        self._act_export_pdf = QAction("Export PDF…", self)
        self._act_export_pdf.triggered.connect(self._export_pdf)

        self._act_export_ifc = QAction("Export IFC…", self)
        self._act_export_ifc.triggered.connect(self._export_ifc)

        self._act_export_3dm = QAction("Export .3dm (Rhino)…", self)
        self._act_export_3dm.triggered.connect(self._export_3dm)

        self._act_save = QAction("Save Project", self)
        self._act_save.setShortcut(QKeySequence.Save)
        self._act_save.triggered.connect(self._save_project)

        self._act_load = QAction("Load Project", self)
        self._act_load.triggered.connect(self._load_project)

        self._act_params = QAction("Parameters", self)
        self._act_params.setCheckable(True)
        self._act_params.setChecked(True)
        self._act_params.triggered.connect(
            lambda checked: self._params_panel.setVisible(checked)
        )

        self._act_pareto = QAction("Pareto Explorer", self)
        self._act_pareto.setCheckable(True)
        self._act_pareto.setChecked(False)
        self._act_pareto.triggered.connect(
            lambda checked: self._pareto_panel.setVisible(checked)
        )

    def _build_toolbar(self) -> None:
        tb = QToolBar("Main", self)
        self.addToolBar(tb)
        tb.addAction(self._act_open)
        tb.addAction(self._act_open_pdf)
        tb.addAction(self._act_open_dwg)
        tb.addSeparator()
        tb.addAction(self._act_draw)
        tb.addAction(self._act_set_boundary)
        tb.addSeparator()
        tb.addAction(self._act_generate)
        tb.addAction(self._act_optimize)
        tb.addAction(self._act_stop_opt)
        tb.addSeparator()
        tb.addAction(self._act_lock)
        tb.addAction(self._act_unlock)
        tb.addSeparator()
        tb.addAction(self._act_export)
        tb.addAction(self._act_export_pdf)
        tb.addAction(self._act_export_ifc)
        tb.addAction(self._act_export_3dm)
        tb.addSeparator()
        tb.addAction(self._act_save)
        tb.addAction(self._act_load)
        tb.addSeparator()
        tb.addAction(self._act_params)
        tb.addAction(self._act_pareto)

    def _build_menu(self) -> None:
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

        edit_menu = self.menuBar().addMenu("&Edit")
        edit_menu.addAction(self._act_draw)
        edit_menu.addAction(self._act_set_boundary)
        edit_menu.addSeparator()
        edit_menu.addAction(self._act_lock)
        edit_menu.addAction(self._act_unlock)
        edit_menu.addSeparator()
        edit_menu.addAction(self._act_params)

        layout_menu = self.menuBar().addMenu("&Layout")
        layout_menu.addAction(self._act_generate)
        layout_menu.addSeparator()
        layout_menu.addAction(self._act_optimize)
        layout_menu.addAction(self._act_stop_opt)
        layout_menu.addSeparator()
        layout_menu.addAction(self._act_lock)
        layout_menu.addAction(self._act_unlock)
        layout_menu.addSeparator()
        layout_menu.addAction(self._act_export)
        layout_menu.addAction(self._act_export_pdf)
        layout_menu.addAction(self._act_export_ifc)
        layout_menu.addAction(self._act_export_3dm)
        layout_menu.addSeparator()
        layout_menu.addAction(self._act_pareto)

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
        if checked:
            self.statusBar().showMessage(
                "Click to place vertices. Double-click / Enter / right-click to close."
            )

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
        self.statusBar().showMessage(f"Boundary drawn ({len(pts)} vertices).")
        self._auto_generate()

    # ── params / generate slots ───────────────────────────────────────────────

    def _on_params_changed(self, params) -> None:
        self._ctrl.update_params(params)
        self._ctrl.update_setback(self._params_panel.current_setback())
        if self._ctrl.site is not None:
            self._run_generate()

    def _generate_manual(self) -> None:
        if self._ctrl.site is None:
            QMessageBox.information(
                self, "No site", "Open a DXF and set a boundary, or draw a polygon first."
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
            self.statusBar().showMessage(
                f"Generated {layout.metrics.total_stalls} stalls  |  "
                f"angle {layout.params.angle:.0f}°  |  "
                f"orientation {layout.params.orientation:.0f}°  |  "
                f"{layout.metrics.gross_area_per_stall:.1f} m²/stall"
            )
        except Exception as exc:
            QMessageBox.critical(self, "Generate failed", str(exc))

    # ── lock / unlock slots ───────────────────────────────────────────────────

    def _lock_selected(self) -> None:
        indices = self._canvas.selected_stall_indices()
        if not indices:
            self.statusBar().showMessage("Select stalls on the canvas first (rubber-band drag).")
            return
        self._ctrl.set_stalls_locked(indices, True)
        for i in indices:
            self._canvas.set_stall_locked(i, True)
        self.statusBar().showMessage(
            f"Locked {len(indices)} stall(s). Re-generate to constrain around them."
        )

    def _unlock_selected(self) -> None:
        indices = self._canvas.selected_stall_indices()
        if not indices:
            self.statusBar().showMessage("Select stalls on the canvas first (rubber-band drag).")
            return
        self._ctrl.set_stalls_locked(indices, False)
        for i in indices:
            self._canvas.set_stall_locked(i, False)
        self.statusBar().showMessage(f"Unlocked {len(indices)} stall(s).")

    # ── optimizer slots ───────────────────────────────────────────────────────

    def _start_optimize(self) -> None:
        if self._ctrl.site is None:
            QMessageBox.information(
                self, "No site", "Open a DXF and set a boundary, or draw a polygon first."
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
                f"Optimizing… gen {cur}/{tot}"
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
            f"Optimization complete — {len(result.candidates)} Pareto candidates."
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
            f"Loaded Pareto candidate — {m.total_stalls} stalls  |  "
            f"angle {layout.params.angle:.0f}°  |  "
            f"{m.gross_area_per_stall:.1f} m²/stall"
        )

    # ── optimize params dialog ────────────────────────────────────────────────

    def _ask_optimize_params(self) -> OptimizationParams | None:
        dlg = QDialog(self)
        dlg.setWindowTitle("Optimization settings")
        form = QFormLayout(dlg)

        pop_spin = QSpinBox()
        pop_spin.setRange(10, 200)
        pop_spin.setValue(40)
        pop_spin.setSingleStep(10)
        form.addRow("Population size:", pop_spin)

        gen_spin = QSpinBox()
        gen_spin.setRange(5, 500)
        gen_spin.setValue(30)
        gen_spin.setSingleStep(5)
        form.addRow("Generations:", gen_spin)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        form.addRow(btns)

        if dlg.exec() != QDialog.Accepted:
            return None

        return OptimizationParams(pop_size=pop_spin.value(), n_gen=gen_spin.value())

    # ── PDF underlay slots ────────────────────────────────────────────────────

    def _open_pdf(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Open PDF Underlay", "", "PDF Files (*.pdf);;All Files (*)"
        )
        if not path:
            return
        try:
            png_bytes, w_px, h_px = rasterise(path, dpi=150)
            self._pdf_bytes = png_bytes
            self._pdf_cal_points.clear()
            self.statusBar().showMessage(
                f"PDF loaded ({w_px}×{h_px} px). Calibrate: enter two known points "
                "and their distance via Layout → Calibrate PDF Scale…"
            )
            # Show at 1 px = 1 unit until calibrated
            raw_transform = PDFTransform(origin_px=(0.0, 0.0), scale=1.0)
            self._pdf_transform = raw_transform
            self._canvas.show_pdf_underlay(png_bytes, w_px, h_px, raw_transform)
        except Exception as exc:
            QMessageBox.critical(self, "Error opening PDF", str(exc))

    def _calibrate_pdf(self) -> None:
        if self._pdf_bytes is None:
            QMessageBox.information(self, "No PDF", "Open a PDF underlay first.")
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("Calibrate PDF Scale")
        form = QFormLayout(dlg)
        form.addRow(QLabel("Enter two calibration points (pixel x, y) and the real distance."))
        p1x = QDoubleSpinBox(); p1x.setRange(0, 99999); p1x.setValue(0)
        p1y = QDoubleSpinBox(); p1y.setRange(0, 99999); p1y.setValue(0)
        p2x = QDoubleSpinBox(); p2x.setRange(0, 99999); p2x.setValue(100)
        p2y = QDoubleSpinBox(); p2y.setRange(0, 99999); p2y.setValue(0)
        dist = QDoubleSpinBox(); dist.setRange(0.01, 9999); dist.setValue(10.0); dist.setSuffix(" m")
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
            # Re-display with calibrated scale
            import fitz
            doc = fitz.open()   # dummy — we already have png_bytes, just re-show
            # Re-rasterise not needed; just update the transform
            w_px = int(len(self._pdf_bytes) ** 0.5)  # rough; won't be used
            self._canvas.show_pdf_underlay(self._pdf_bytes, 0, 0, tf)
            self.statusBar().showMessage(
                f"PDF calibrated: {tf.scale*1000:.2f} mm/pixel"
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
                f"Loaded {len(entities)} entities from {Path(path).name} (via DWG converter). "
                "Select entities, then 'Set as Boundary'."
            )
        except Exception as exc:
            QMessageBox.critical(self, "Error opening DWG", str(exc))

    # ── additional export slots ───────────────────────────────────────────────

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
            export_pdf.export(
                self._ctrl.layout,
                self._ctrl.site,
                path,
                title="Parking Layout",
            )
            self.statusBar().showMessage(f"PDF exported to {Path(path).name}")
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
            self.statusBar().showMessage(f"IFC exported to {Path(path).name}")
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
            self.statusBar().showMessage(f".3dm exported to {Path(path).name}")
        except ImportError as exc:
            QMessageBox.warning(
                self, "rhino3dm not installed",
                f"{exc}\n\nInstall CMake then: pip install rhino3dm"
            )
        except Exception as exc:
            QMessageBox.critical(self, ".3dm export failed", str(exc))

    # ── misc slots ────────────────────────────────────────────────────────────

    def _on_stall_count(self, count: int) -> None:
        self._stall_label.setText(f"Stalls: {count}")

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
            self.statusBar().showMessage(f"Exported to {Path(path).name}")
        except Exception as exc:
            QMessageBox.critical(self, "Export failed", str(exc))

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
            self.statusBar().showMessage(f"Saved to {Path(path).name}")
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
            self.statusBar().showMessage(f"Loaded from {Path(path).name}")
        except Exception as exc:
            QMessageBox.critical(self, "Load failed", str(exc))
