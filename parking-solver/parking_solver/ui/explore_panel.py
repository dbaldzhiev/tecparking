"""Exploration results panel.

Auto-started when a site polygon is set.  Shows every strategy × orientation
× angle combination ranked by stall count.  Clicking a row loads that layout
into the canvas; results are also forwarded to the Pareto panel as a
multi-objective scatter plot.

Architecture
------------
* ExplorePanel is a QDockWidget placed in the left dock area.
* It owns an ExploreWorker (background thread) and replaces it on each new
  exploration run.
* The Pareto panel receives results via convert_to_pareto_result() so the
  scatter plot shows stall-count vs. m²/stall coloured by strategy type.
"""
from __future__ import annotations

from typing import Optional

import numpy as np
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QComboBox,
    QDockWidget,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from parking_solver.app.workers import ExploreWorker
from parking_solver.core.generator import StrategyResult
from parking_solver.core.model import LayoutType, Site
from parking_solver.core.optimizer import (
    N_OBJ,
    Candidate,
    OBJ_AREA_PER_STALL,
    OBJ_CONNECTIVITY,
    OBJ_COUNT,
    OBJ_UNIFORMITY,
    ParetoResult,
)
from parking_solver.core.regulations.engine import RegulationProfile
from parking_solver.core.scorer import theoretical_max_stalls

# ── Per-strategy display colours (RGB) ───────────────────────────────────────

STRATEGY_COLOR: dict[LayoutType, tuple[int, int, int]] = {
    LayoutType.STANDARD:       (80,  160,  80),   # green
    LayoutType.FISHBONE:       (80,  200, 160),   # teal
    LayoutType.PERIMETER_RING: (80,  120, 200),   # blue
    LayoutType.RING_INFILL:    (160,  80, 200),   # purple
    LayoutType.MULTI_RING:     (200,  80,  80),   # red
    LayoutType.SPINE_BRANCHES: (200, 160,  80),   # amber
    LayoutType.MIXED_ANGLE:    (220, 100, 180),   # pink
    LayoutType.SUBDIVIDED:     (120, 200, 120),   # light green
}

STRATEGY_LABEL: dict[LayoutType, str] = {
    LayoutType.STANDARD:       "Banded rows",
    LayoutType.FISHBONE:       "Herringbone",
    LayoutType.PERIMETER_RING: "Perimeter ring",
    LayoutType.RING_INFILL:    "Perimeter + infill",
    LayoutType.MULTI_RING:     "Multi-ring",
    LayoutType.SPINE_BRANCHES: "Spine + branches",
    LayoutType.MIXED_ANGLE:    "Mixed angle",
    LayoutType.SUBDIVIDED:     "Adaptive",
}


def _angle_text(angle: float) -> str:
    """Display an explore angle program: 0 means 'auto / best per region'."""
    return "auto" if not angle else f"{angle:.0f}"

_TABLE_COLS = ["#", "Strategy", "Stalls", "m²/stall", "Coverage"]

# Sort options: label → key (sorted ascending, so negate "higher-is-better").
_SORT_KEYS = {
    "Stalls": lambda r: -r.stall_count,
    "Density (m²/stall)": lambda r: r.layout.metrics.gross_area_per_stall,
    "Coverage": lambda r: -r.road_coverage,
}


def strategy_results_to_pareto(results: list[StrategyResult]) -> ParetoResult:
    """Convert generate_all() output to a ParetoResult for the Pareto panel."""
    candidates = []
    for r in results:
        f = np.zeros(N_OBJ)
        f[OBJ_COUNT] = -float(r.stall_count)
        f[OBJ_AREA_PER_STALL] = float(r.layout.metrics.gross_area_per_stall)
        f[OBJ_CONNECTIVITY] = float(r.dead_ends)
        f[OBJ_UNIFORMITY] = float(r.stall_isolation)
        candidates.append(Candidate(params=r.layout.params, layout=r.layout, objectives=f))
    return ParetoResult(candidates=candidates, n_gen=0)


class ExplorePanel(QDockWidget):
    """Docked exploration results panel.

    Signals
    -------
    layout_selected(Layout)      — emitted when the user clicks a result row
    live_preview(Layout)         — best-so-far layout while a run is streaming
    results_ready(list)          — list[StrategyResult] when the run finishes
    """

    layout_selected = Signal(object)   # Layout
    live_preview = Signal(object)      # Layout (best so far, during streaming)
    results_ready = Signal(object)     # list[StrategyResult]

    def __init__(self, parent=None) -> None:
        super().__init__("Explore", parent)
        self.setAllowedAreas(
            Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea | Qt.BottomDockWidgetArea
        )
        self.setMinimumWidth(280)

        self._results: list[StrategyResult] = []
        self._worker: Optional[ExploreWorker] = None

        # Streaming state
        self._streaming: bool = False
        self._user_interacted: bool = False   # user clicked a row → stop auto-follow
        self._best_count: int = -1
        self._total_tasks: int = 0

        # ── progress ─────────────────────────────────────────────────────────
        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setFormat("Generating… %p%")
        self._progress.setVisible(False)

        self._status = QLabel("No site loaded.")
        self._status.setAlignment(Qt.AlignCenter)
        self._status.setWordWrap(True)

        # ── sort control ──────────────────────────────────────────────────────
        self._sort_combo = QComboBox()
        self._sort_combo.addItems(list(_SORT_KEYS))
        self._sort_combo.currentIndexChanged.connect(self._on_sort_changed)
        sort_row = QHBoxLayout()
        sort_row.addWidget(QLabel("Sort by:"))
        sort_row.addWidget(self._sort_combo, 1)

        # ── results table ─────────────────────────────────────────────────────
        self._table = QTableWidget(0, len(_TABLE_COLS))
        self._table.setHorizontalHeaderLabels(_TABLE_COLS)
        self._table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setAlternatingRowColors(True)
        self._table.setMinimumHeight(160)
        self._table.currentCellChanged.connect(
            lambda cur_row, _cur_col, _prev_row, _prev_col: self._on_row_changed(cur_row)
        )
        self._table.cellClicked.connect(self._on_row_clicked)
        self._table.doubleClicked.connect(self._on_row_double_clicked)

        # ── telemetry box ─────────────────────────────────────────────────────
        tele_box = QGroupBox("Selected")
        tele_layout = QFormLayout(tele_box)

        self._tele_strategy = QLabel("—")
        self._tele_orient = QLabel("—")
        self._tele_angle = QLabel("—")
        self._tele_stalls = QLabel("—")
        self._tele_area = QLabel("—")
        self._tele_stall_w = QLabel("—")
        self._tele_efficiency = QLabel("—")
        self._tele_road_cov = QLabel("—")
        self._tele_dead_ends = QLabel("—")
        self._tele_isolation = QLabel("—")
        self._tele_entr_conn = QLabel("—")
        self._tele_circulation = QLabel("—")
        self._tele_drivable = QLabel("—")

        tele_layout.addRow("Strategy:", self._tele_strategy)
        tele_layout.addRow("Orientation:", self._tele_orient)
        tele_layout.addRow("Angle:", self._tele_angle)
        tele_layout.addRow("Stalls:", self._tele_stalls)
        tele_layout.addRow("m²/stall:", self._tele_area)
        tele_layout.addRow("Stall width:", self._tele_stall_w)
        tele_layout.addRow("Coverage:", self._tele_efficiency)
        tele_layout.addRow("Road reach:", self._tele_road_cov)
        tele_layout.addRow("Dead ends:", self._tele_dead_ends)
        tele_layout.addRow("Isolated stalls:", self._tele_isolation)
        tele_layout.addRow("Entrance link:", self._tele_entr_conn)
        tele_layout.addRow("Circulation:", self._tele_circulation)
        tele_layout.addRow("Drivable:", self._tele_drivable)

        # ── load button ───────────────────────────────────────────────────────
        self._load_btn = QPushButton("Load into canvas")
        self._load_btn.setEnabled(False)
        self._load_btn.clicked.connect(self._emit_selected)

        # ── re-explore button ─────────────────────────────────────────────────
        self._rerun_btn = QPushButton("Re-explore")
        self._rerun_btn.setEnabled(False)
        self._rerun_btn.setToolTip(
            "Re-run exploration with current stall width / length from the Parameters panel"
        )
        self._rerun_btn.clicked.connect(self._rerun)

        btn_row = QHBoxLayout()
        btn_row.addWidget(self._load_btn)
        btn_row.addWidget(self._rerun_btn)

        # ── assemble ──────────────────────────────────────────────────────────
        inner = QWidget()
        vbox = QVBoxLayout(inner)
        vbox.setContentsMargins(4, 4, 4, 4)
        vbox.setSpacing(4)
        vbox.addWidget(self._progress)
        vbox.addWidget(self._status)
        vbox.addLayout(sort_row)
        vbox.addWidget(self._table)
        vbox.addWidget(tele_box)
        vbox.addLayout(btn_row)

        scroll = QScrollArea()
        scroll.setWidget(inner)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.setWidget(scroll)

        # For re-run
        self._last_site: Optional[Site] = None
        self._last_profile: Optional[RegulationProfile] = None
        self._last_stall_w: float = 2.5
        self._last_stall_l: float = 5.0

    # ── public API ────────────────────────────────────────────────────────────

    def start_explore(
        self,
        site: Site,
        profile: RegulationProfile,
        stall_width: float = 2.5,
        stall_length: float = 5.0,
    ) -> None:
        """Cancel any running exploration and start a new one."""
        self._last_site = site
        self._last_profile = profile
        self._last_stall_w = stall_width
        self._last_stall_l = stall_length

        if self._worker and self._worker.isRunning():
            self._worker.terminate()
            self._worker.wait(2000)

        self._results = []
        self._streaming = True
        self._user_interacted = False
        self._best_count = -1
        self._total_tasks = 0
        self._table.setRowCount(0)
        self._load_btn.setEnabled(False)
        self._progress.setRange(0, 0)   # indeterminate until first progress tick
        self._progress.setValue(0)
        self._progress.setFormat("Starting…")
        self._progress.setVisible(True)
        self._status.setText("Exploring strategies — results appear live; click any row to preview.")
        self._clear_telemetry()

        self._worker = ExploreWorker(
            site=site,
            profile=profile,
            stall_width=stall_width,
            stall_length=stall_length,
            parent=self,
        )
        self._worker.progress.connect(self._on_progress)
        self._worker.result_ready.connect(self._on_result)
        self._worker.finished_ok.connect(self._on_done)
        self._worker.failed.connect(self._on_failed)
        self._worker.start()

    # ── internals ─────────────────────────────────────────────────────────────

    def _rerun(self) -> None:
        if self._last_site and self._last_profile:
            self.start_explore(
                self._last_site, self._last_profile,
                self._last_stall_w, self._last_stall_l,
            )

    def _on_progress(self, done: int, total: int) -> None:
        self._total_tasks = total
        if total:
            self._progress.setRange(0, total)
            self._progress.setValue(done)
            best = f" · best {self._best_count}" if self._best_count > 0 else ""
            self._progress.setFormat(f"Exploring {done}/{total}{best}")

    def _on_result(self, r: StrategyResult) -> None:
        """A single layout streamed in from the worker — show it live."""
        row = len(self._results)
        self._results.append(r)
        self._append_row(row, r)

        # Auto-follow the best layout so the canvas shows progress, until the
        # user takes over by clicking a row.
        if not self._user_interacted and r.stall_count > self._best_count:
            self._best_count = r.stall_count
            self._table.blockSignals(True)
            self._table.selectRow(row)
            self._table.blockSignals(False)
            self._update_telemetry(r)
            self._load_btn.setEnabled(True)
            self.live_preview.emit(r.layout)

    def _on_done(self, results: list) -> None:
        self._streaming = False
        self._progress.setVisible(False)
        self._rerun_btn.setEnabled(True)

        # Preserve the user's selection across the final re-sort.
        selected = None
        cur = self._table.currentRow()
        if self._user_interacted and 0 <= cur < len(self._results):
            selected = self._results[cur]

        self._results = self._sorted(results)

        # Oxford ESGI91 density ceiling: theoretical maximum stalls for this site
        ceiling_str = ""
        if results and self._last_profile and self._last_site:
            aisle_spec = self._last_profile.aisles.get("90")
            if aisle_spec:
                aisle_90 = aisle_spec.two_way or aisle_spec.one_way or 6.0
                density = theoretical_max_stalls(
                    self._last_stall_w, self._last_stall_l, aisle_90
                )
                ceiling = int(self._last_site.boundary.area * density)
                best = max((r.stall_count for r in results), default=0)
                pct = best / ceiling * 100 if ceiling else 0
                ceiling_str = f"  ·  Oxford ceiling: {ceiling} ({pct:.0f}% achieved)"

        self._status.setText(
            f"{len(results)} layouts — sorted by {self._sort_combo.currentText().lower()}; "
            f"click a row to preview, double-click to load.{ceiling_str}"
        )
        self._populate(self._results)
        self.results_ready.emit(self._results)

        # Restore selection (by identity) or default to the top row.
        target = self._results.index(selected) if selected in self._results else 0
        if self._results:
            self._table.selectRow(target)

    def _sorted(self, results: list[StrategyResult]) -> list[StrategyResult]:
        key = _SORT_KEYS.get(self._sort_combo.currentText())
        return sorted(results, key=key) if key else list(results)

    def _on_sort_changed(self) -> None:
        """Re-sort the existing results when the user changes the sort metric."""
        if self._streaming or not self._results:
            return
        cur = self._table.currentRow()
        selected = self._results[cur] if 0 <= cur < len(self._results) else None
        self._results = self._sorted(self._results)
        self._populate(self._results)
        if selected is not None and selected in self._results:
            self._table.selectRow(self._results.index(selected))
        elif self._results:
            self._table.selectRow(0)

    def _on_failed(self, msg: str) -> None:
        self._streaming = False
        self._progress.setVisible(False)
        self._status.setText(f"Exploration failed: {msg}")
        self._rerun_btn.setEnabled(True)

    def _append_row(self, row: int, r: StrategyResult) -> None:
        """Insert a single result row (used both live and in the final sort)."""
        if self._table.rowCount() <= row:
            self._table.setRowCount(row + 1)
        lt = r.layout_type
        bg = QColor(*STRATEGY_COLOR.get(lt, (150, 150, 150)), 60)
        vals = [
            str(row + 1),
            STRATEGY_LABEL.get(lt, lt.value),
            str(r.stall_count),
            f"{r.layout.metrics.gross_area_per_stall:.1f}",
            f"{r.road_coverage * 100:.0f}%",
        ]
        for col, v in enumerate(vals):
            item = QTableWidgetItem(v)
            item.setTextAlignment(Qt.AlignCenter)
            item.setBackground(bg)
            self._table.setItem(row, col, item)

    def _populate(self, results: list[StrategyResult]) -> None:
        self._table.setRowCount(len(results))
        for row, r in enumerate(results):
            self._append_row(row, r)

    def _on_row_changed(self, row: int) -> None:
        # Telemetry/display only — works during a live run too (keyboard nav).
        if 0 <= row < len(self._results):
            self._update_telemetry(self._results[row])
            self._load_btn.setEnabled(True)

    def _on_row_clicked(self, row: int, _col: int = 0) -> None:
        """A real user click — take over from auto-follow and preview on canvas."""
        self._user_interacted = True
        if 0 <= row < len(self._results):
            self._update_telemetry(self._results[row])
            self._load_btn.setEnabled(True)
            self.layout_selected.emit(self._results[row].layout)

    def _on_row_double_clicked(self) -> None:
        self._emit_selected()

    def _emit_selected(self) -> None:
        row = self._table.currentRow()
        if 0 <= row < len(self._results):
            self.layout_selected.emit(self._results[row].layout)

    def _update_telemetry(self, r: StrategyResult) -> None:
        lt = r.layout_type
        m = r.layout.metrics
        self._tele_strategy.setText(STRATEGY_LABEL.get(lt, lt.value))
        self._tele_orient.setText(f"{r.orientation:.0f}°")
        self._tele_angle.setText(_angle_text(r.angle) + ("" if not r.angle else "°"))
        self._tele_stalls.setText(str(r.stall_count))
        self._tele_area.setText(f"{m.gross_area_per_stall:.2f} m²")
        self._tele_stall_w.setText(f"{r.stall_width:.2f} m")
        if m.site_area > 0 and r.stall_count > 0:
            stall_floor = r.stall_count * r.stall_width * r.layout.params.stall_length
            coverage = stall_floor / m.site_area * 100.0
            self._tele_efficiency.setText(f"{coverage:.1f}%")
        else:
            self._tele_efficiency.setText("—")
        # Road reach: Oxford ESGI91 road accessibility metric
        self._tele_road_cov.setText(f"{r.road_coverage * 100:.0f}%")
        self._tele_dead_ends.setText(f"{r.dead_ends * 100:.0f}%")
        self._tele_isolation.setText(f"{r.stall_isolation * 100:.0f}%")
        self._tele_entr_conn.setText(f"{r.entrance_connectivity * 100:.0f}%")
        self._tele_circulation.setText(f"{r.aisle_area_ratio * 100:.0f}%")
        self._tele_drivable.setText(f"{r.circuit_validity * 100:.0f}%")

    def _clear_telemetry(self) -> None:
        for lbl in (
            self._tele_strategy, self._tele_orient, self._tele_angle,
            self._tele_stalls, self._tele_area, self._tele_stall_w,
            self._tele_efficiency, self._tele_road_cov,
            self._tele_dead_ends, self._tele_isolation,
            self._tele_entr_conn, self._tele_circulation,
            self._tele_drivable,
        ):
            lbl.setText("—")
