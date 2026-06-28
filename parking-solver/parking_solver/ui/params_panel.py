from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDockWidget,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSlider,
    QVBoxLayout,
    QWidget,
)
from shapely.geometry import Polygon

from parking_solver.core.geometry.helpers import polygon_edge_directions
from parking_solver.core.model import AisleDir, LayoutParams, LayoutType
from parking_solver.core.regulations.engine import RegulationProfile, load_profile

_PROFILES_DIR = (
    Path(__file__).parent.parent / "core" / "regulations" / "profiles"
)
_KNOWN_PROFILES: list[tuple[str, str]] = [
    ("Generic EU", "generic_eu.yaml"),
    ("Bulgarian (BG)", "bulgarian.yaml"),
]


class ParamsPanel(QDockWidget):
    """Dock panel exposing all LayoutParams as live controls.

    Emits `params_changed(LayoutParams)` after a short debounce so the
    canvas can auto-regenerate without hammering the solver on every tick.
    Emits `profile_changed(RegulationProfile)` when the user switches profiles.
    """

    params_changed = Signal(object)    # LayoutParams
    profile_changed = Signal(object)   # RegulationProfile

    _ANGLES = [30, 45, 60, 75, 90]

    def __init__(self, parent=None) -> None:
        super().__init__("Parameters", parent)
        self.setFeatures(
            QDockWidget.DockWidgetFeature.DockWidgetMovable
            | QDockWidget.DockWidgetFeature.DockWidgetFloatable
        )
        self.setMinimumWidth(220)

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(150)
        self._debounce.timeout.connect(self._emit_params)

        # ── stall geometry ────────────────────────────────────────────────────
        stall_box = QGroupBox("Stall")
        stall_form = QFormLayout(stall_box)

        self._angle_combo = QComboBox()
        for a in self._ANGLES:
            self._angle_combo.addItem(f"{a}°", a)
        self._angle_combo.setCurrentIndex(4)  # default 90°
        stall_form.addRow("Angle:", self._angle_combo)

        self._width_spin = QDoubleSpinBox()
        self._width_spin.setRange(2.00, 3.50)
        self._width_spin.setSingleStep(0.05)
        self._width_spin.setDecimals(2)
        self._width_spin.setSuffix(" m")
        self._width_spin.setValue(2.50)
        stall_form.addRow("Width:", self._width_spin)

        self._length_spin = QDoubleSpinBox()
        self._length_spin.setRange(4.00, 6.50)
        self._length_spin.setSingleStep(0.10)
        self._length_spin.setDecimals(2)
        self._length_spin.setSuffix(" m")
        self._length_spin.setValue(5.00)
        stall_form.addRow("Length:", self._length_spin)

        # ── aisle ─────────────────────────────────────────────────────────────
        aisle_box = QGroupBox("Aisle")
        aisle_form = QFormLayout(aisle_box)

        self._dir_combo = QComboBox()
        self._dir_combo.addItem("Two-way", AisleDir.TWO_WAY)
        self._dir_combo.addItem("One-way", AisleDir.ONE_WAY)
        aisle_form.addRow("Direction:", self._dir_combo)

        self._aisle_w_spin = QDoubleSpinBox()
        self._aisle_w_spin.setRange(0.0, 12.0)
        self._aisle_w_spin.setSingleStep(0.25)
        self._aisle_w_spin.setDecimals(2)
        self._aisle_w_spin.setSuffix(" m")
        self._aisle_w_spin.setSpecialValueText("Profile default")
        self._aisle_w_spin.setValue(0.0)
        aisle_form.addRow("Width:", self._aisle_w_spin)

        # ── layout ────────────────────────────────────────────────────────────
        layout_box = QGroupBox("Layout")
        layout_form = QFormLayout(layout_box)

        # Only buildable, well-connected strategies are offered.
        self._strategy_combo = QComboBox()
        self._strategy_combo.addItem("Adaptive (recommended)", LayoutType.SUBDIVIDED)
        self._strategy_combo.addItem("Perimeter + infill", LayoutType.RING_INFILL)
        self._strategy_combo.addItem("Banded rows", LayoutType.STANDARD)
        self._strategy_combo.addItem("Herringbone", LayoutType.FISHBONE)
        layout_form.addRow("Strategy:", self._strategy_combo)

        self._orient_slider = QSlider(Qt.Horizontal)
        self._orient_slider.setRange(0, 175)
        self._orient_slider.setSingleStep(1)
        self._orient_slider.setPageStep(15)
        self._orient_slider.setValue(0)
        self._orient_label = QLabel("0°")
        self._orient_label.setAlignment(Qt.AlignRight)
        layout_form.addRow("Orientation:", self._orient_slider)
        layout_form.addRow("", self._orient_label)

        # Snap-to-edge buttons (populated when a site polygon is loaded)
        self._edge_btn_row = QWidget()
        self._edge_btn_layout = QHBoxLayout(self._edge_btn_row)
        self._edge_btn_layout.setContentsMargins(0, 0, 0, 0)
        self._edge_btn_layout.setSpacing(4)
        self._edge_btn_row.setVisible(False)
        layout_form.addRow("Snap to edge:", self._edge_btn_row)

        # ── regulation profile ────────────────────────────────────────────────
        reg_box = QGroupBox("Regulation")
        reg_form = QFormLayout(reg_box)

        self._profile_combo = QComboBox()
        for label, _ in _KNOWN_PROFILES:
            self._profile_combo.addItem(label)
        reg_form.addRow("Profile:", self._profile_combo)

        # ── site ─────────────────────────────────────────────────────────────
        site_box = QGroupBox("Site")
        site_form = QFormLayout(site_box)

        self._setback_spin = QDoubleSpinBox()
        self._setback_spin.setRange(0.0, 15.0)
        self._setback_spin.setSingleStep(0.25)
        self._setback_spin.setDecimals(2)
        self._setback_spin.setSuffix(" m")
        self._setback_spin.setValue(0.0)
        site_form.addRow("Setback:", self._setback_spin)

        # ── assemble ──────────────────────────────────────────────────────────
        inner = QWidget()
        vbox = QVBoxLayout(inner)
        vbox.addWidget(stall_box)
        vbox.addWidget(aisle_box)
        vbox.addWidget(layout_box)
        vbox.addWidget(reg_box)
        vbox.addWidget(site_box)
        vbox.addStretch()

        scroll = QScrollArea()
        scroll.setWidget(inner)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.setWidget(scroll)

        # ── connections ───────────────────────────────────────────────────────
        self._angle_combo.currentIndexChanged.connect(self._on_angle_changed)
        self._width_spin.valueChanged.connect(self._schedule)
        self._length_spin.valueChanged.connect(self._schedule)
        self._dir_combo.currentIndexChanged.connect(self._schedule)
        self._aisle_w_spin.valueChanged.connect(self._schedule)
        self._orient_slider.valueChanged.connect(self._on_orientation_changed)
        self._setback_spin.valueChanged.connect(self._schedule)
        self._strategy_combo.currentIndexChanged.connect(self._on_strategy_changed)
        self._profile_combo.currentIndexChanged.connect(self._on_profile_changed)

        self._on_angle_changed()    # enforce initial aisle-dir state
        self._on_strategy_changed() # enforce initial angle-control state

    # ── public ────────────────────────────────────────────────────────────────

    @property
    def auto_generate(self) -> bool:
        return True   # the canvas always updates on parameter change

    def current_params(self) -> LayoutParams:
        angle = self._angle_combo.currentData()
        aisle_dir = self._dir_combo.currentData()
        aisle_w_val = self._aisle_w_spin.value()
        strategy = self._strategy_combo.currentData()
        return LayoutParams(
            orientation=float(self._orient_slider.value()),
            layout_type=strategy,
            angle=float(angle),
            stall_width=self._width_spin.value(),
            stall_length=self._length_spin.value(),
            aisle_dir=aisle_dir,
            aisle_width=aisle_w_val if aisle_w_val > 0 else None,
        )

    def current_setback(self) -> float:
        return self._setback_spin.value()

    def current_profile(self) -> RegulationProfile:
        idx = self._profile_combo.currentIndex()
        _, filename = _KNOWN_PROFILES[idx]
        return load_profile(_PROFILES_DIR / filename)

    def set_site_polygon(self, polygon: Polygon | None) -> None:
        """Update snap-to-edge buttons to reflect the current site polygon."""
        # Clear old buttons
        while self._edge_btn_layout.count():
            item = self._edge_btn_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        if polygon is None:
            self._edge_btn_row.setVisible(False)
            return

        dirs = polygon_edge_directions(polygon)
        if not dirs:
            self._edge_btn_row.setVisible(False)
            return

        for ang in dirs[:4]:  # at most 4 buttons
            deg = int(round(ang))
            btn = QPushButton(f"{deg}°")
            btn.setFixedHeight(22)
            btn.setToolTip(f"Set orientation to {deg}° (parallel to that polygon edge)")
            btn.clicked.connect(lambda _checked=False, d=deg: self._snap_orientation(d))
            self._edge_btn_layout.addWidget(btn)

        self._edge_btn_row.setVisible(True)

    # ── slots ─────────────────────────────────────────────────────────────────

    def _on_angle_changed(self) -> None:
        angle = self._angle_combo.currentData()
        is_90 = angle == 90
        self._dir_combo.setEnabled(is_90)
        if not is_90:
            self._dir_combo.setCurrentIndex(1)  # force one-way
        self._schedule()

    def _on_strategy_changed(self) -> None:
        # All offered strategies use the angle as the module angle (Adaptive treats
        # it as the per-region program). Direction only applies to 90° two-way.
        self._angle_combo.setEnabled(True)
        self._dir_combo.setEnabled(self._angle_combo.currentData() == 90)
        self._schedule()

    def _on_profile_changed(self) -> None:
        profile = self.current_profile()
        self.profile_changed.emit(profile)
        self._schedule()

    def _snap_orientation(self, deg: int) -> None:
        self._orient_slider.setValue(min(deg, 175))

    def _on_orientation_changed(self, value: int) -> None:
        self._orient_label.setText(f"{value}°")
        self._schedule()

    def _schedule(self, *_) -> None:
        self._debounce.start()

    def _emit_params(self) -> None:
        self.params_changed.emit(self.current_params())
