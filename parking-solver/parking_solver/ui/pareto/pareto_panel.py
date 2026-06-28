"""Pareto explorer dock widget.

Layout
------
Top    : axis selector (X / Y combo boxes) — choose which two objectives to plot
Middle : pyqtgraph ScatterPlotItem — one dot per candidate; click selects
Bottom : QTextEdit — advantages/disadvantages for the selected candidate

Signals
-------
candidate_selected(Layout)  — emitted when the user clicks a Pareto point
"""
from __future__ import annotations

from typing import Optional

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDockWidget,
    QFormLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from parking_solver.core.model import LayoutType
from parking_solver.core.optimizer import (
    OBJ_LABELS,
    Candidate,
    ParetoResult,
    candidate_advantages,
)

# Per-strategy colours for explore-mode scatter (RGBA)
_STRATEGY_BRUSH: dict[LayoutType, tuple[int, int, int]] = {
    LayoutType.STANDARD:       (80,  160,  80),
    LayoutType.FISHBONE:       (80,  200, 160),
    LayoutType.PERIMETER_RING: (80,  120, 200),
    LayoutType.RING_INFILL:    (160,  80, 200),
    LayoutType.MULTI_RING:     (200,  80,  80),
    LayoutType.SPINE_BRANCHES: (200, 160,  80),
    LayoutType.MIXED_ANGLE:    (220, 100, 180),
    LayoutType.SUBDIVIDED:     (120, 200, 120),
}

pg.setConfigOption("background", "#1e1e1e")
pg.setConfigOption("foreground", "#dddddd")


class ParetoPanel(QDockWidget):
    candidate_selected = Signal(object)   # Layout

    def __init__(self, parent=None):
        super().__init__("Pareto Explorer", parent)
        self.setAllowedAreas(Qt.BottomDockWidgetArea | Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea)

        self._result: Optional[ParetoResult] = None
        self._candidates: list[Candidate] = []
        self._selected_idx: Optional[int] = None
        # Per-candidate brushes set in explore mode (None → use uniform green)
        self._candidate_brushes: list | None = None

        # ── axis selectors ─────────────────────────────────────────────────────
        self._combo_x = QComboBox()
        self._combo_y = QComboBox()
        for combo in (self._combo_x, self._combo_y):
            combo.addItems(OBJ_LABELS)
        self._combo_x.setCurrentIndex(0)   # stall count
        self._combo_y.setCurrentIndex(1)   # area/stall

        axis_form = QFormLayout()
        axis_form.addRow("X axis:", self._combo_x)
        axis_form.addRow("Y axis:", self._combo_y)
        self._combo_x.currentIndexChanged.connect(self._redraw)
        self._combo_y.currentIndexChanged.connect(self._redraw)

        # ── pyqtgraph scatter ─────────────────────────────────────────────────
        self._plot_widget = pg.PlotWidget()
        self._plot_widget.setMinimumHeight(200)
        self._scatter = pg.ScatterPlotItem(
            size=10, pen=pg.mkPen(None), brush=pg.mkBrush(80, 160, 80, 200)
        )
        self._plot_widget.addItem(self._scatter)
        self._scatter.sigClicked.connect(self._on_point_clicked)

        # ── progress bar (visible during a run) ───────────────────────────────
        self._progress = QProgressBar()
        self._progress.setVisible(False)
        self._progress.setTextVisible(True)

        # ── gen label ─────────────────────────────────────────────────────────
        self._gen_label = QLabel("No optimization run yet.")
        self._gen_label.setAlignment(Qt.AlignCenter)

        # ── advantages text ───────────────────────────────────────────────────
        self._adv_text = QTextEdit()
        self._adv_text.setReadOnly(True)
        self._adv_text.setMaximumHeight(110)
        self._adv_text.setPlaceholderText("Click a Pareto point to see advantages/disadvantages vs. median.")

        # ── load button ───────────────────────────────────────────────────────
        self._load_btn = QPushButton("Load selected layout")
        self._load_btn.setEnabled(False)
        self._load_btn.clicked.connect(self._emit_selected)

        # ── assemble ──────────────────────────────────────────────────────────
        container = QWidget()
        vbox = QVBoxLayout(container)
        vbox.setContentsMargins(4, 4, 4, 4)
        vbox.setSpacing(4)

        axis_widget = QWidget()
        axis_widget.setLayout(axis_form)
        vbox.addWidget(axis_widget)
        vbox.addWidget(self._plot_widget)
        vbox.addWidget(self._progress)
        vbox.addWidget(self._gen_label)
        vbox.addWidget(QLabel("Selected candidate:"))
        vbox.addWidget(self._adv_text)
        vbox.addWidget(self._load_btn)

        self.setWidget(container)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)

    # ── public API ────────────────────────────────────────────────────────────

    def start_run(self, n_gen: int) -> None:
        self._candidates = []
        self._selected_idx = None
        self._result = None
        self._candidate_brushes = None
        self._scatter.setData([])
        self._adv_text.clear()
        self._load_btn.setEnabled(False)
        self._progress.setVisible(True)
        self._progress.setMaximum(n_gen)
        self._progress.setValue(0)
        self._gen_label.setText("Running…")

    def update_generation(self, gen: int, candidates: list[Candidate]) -> None:
        self._candidates = list(candidates)
        self._selected_idx = None
        self._progress.setValue(gen)
        self._gen_label.setText(f"Generation {gen}  —  {len(candidates)} candidates")
        self._redraw()

    def finish_run(self, result: ParetoResult) -> None:
        self._result = result
        self._candidates = list(result.candidates)
        self._candidate_brushes = None
        self._progress.setVisible(False)
        self._gen_label.setText(
            f"Done — {len(self._candidates)} non-dominated candidates"
        )
        self._redraw()

    def load_explore_results(self, results: list, pareto_result: ParetoResult) -> None:
        """Show all explore results coloured by strategy type.

        *results* is ``list[StrategyResult]`` (imported lazily to avoid circular deps).
        *pareto_result* is the corresponding ``ParetoResult`` built from the same list.
        """
        self._result = pareto_result
        self._candidates = list(pareto_result.candidates)
        self._selected_idx = None
        self._progress.setVisible(False)
        self._gen_label.setText(
            f"Explore — {len(self._candidates)} layouts  (colour = strategy)"
        )
        self._adv_text.clear()
        self._load_btn.setEnabled(False)

        # Build per-candidate brushes from strategy type
        brushes = []
        for r in results:
            rgb = _STRATEGY_BRUSH.get(r.layout_type, (150, 150, 150))
            brushes.append(pg.mkBrush(*rgb, 200))
        self._candidate_brushes = brushes

        self._redraw()

    # ── internals ─────────────────────────────────────────────────────────────

    def _redraw(self) -> None:
        if not self._candidates:
            self._scatter.setData([])
            return

        xi = self._combo_x.currentIndex()
        yi = self._combo_y.currentIndex()
        mat = np.vstack([c.objectives for c in self._candidates])

        xs = self._display_value(mat[:, xi], xi)
        ys = self._display_value(mat[:, yi], yi)

        if self._candidate_brushes:
            brushes = [
                pg.mkBrush(255, 200, 0, 240) if i == self._selected_idx
                else self._candidate_brushes[i]
                for i in range(len(self._candidates))
            ]
        else:
            brushes = [
                pg.mkBrush(255, 200, 0, 240) if i == self._selected_idx
                else pg.mkBrush(80, 160, 80, 200)
                for i in range(len(self._candidates))
            ]

        self._scatter.setData(
            x=xs, y=ys,
            brush=brushes,
            data=list(range(len(self._candidates))),
        )

        self._plot_widget.setLabel("bottom", OBJ_LABELS[xi])
        self._plot_widget.setLabel("left", OBJ_LABELS[yi])

    @staticmethod
    def _display_value(col: np.ndarray, obj_idx: int) -> np.ndarray:
        """Flip negated objectives back to their natural (displayed) direction."""
        if obj_idx in (0, 4):   # OBJ_COUNT and OBJ_ADA_MARGIN are negated
            return -col
        return col

    def _on_point_clicked(self, scatter, points) -> None:
        if not points:
            return
        idx = points[0].data()
        if idx is None or idx >= len(self._candidates):
            return
        self._selected_idx = idx
        self._redraw()
        self._load_btn.setEnabled(True)

        cand = self._candidates[idx]
        if self._result and len(self._result.candidates) > 1:
            text = candidate_advantages(cand, self._result)
        else:
            m = cand.layout.metrics
            text = (
                f"Stalls: {m.total_stalls}  |  "
                f"Area/stall: {m.gross_area_per_stall:.1f} m²  |  "
                f"Angle: {cand.params.angle:.0f}°  |  "
                f"Orientation: {cand.params.orientation:.0f}°"
            )
        self._adv_text.setPlainText(text)

    def _emit_selected(self) -> None:
        if self._selected_idx is not None and self._selected_idx < len(self._candidates):
            layout = self._candidates[self._selected_idx].layout
            self.candidate_selected.emit(layout)
