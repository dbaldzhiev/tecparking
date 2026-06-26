from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDockWidget,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QScrollArea,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from parking_solver.core.model import AisleDir, LayoutParams, LayoutType


class ParamsPanel(QDockWidget):
    """Dock panel exposing all LayoutParams as live controls.

    Emits `params_changed(LayoutParams)` after a short debounce so the
    canvas can auto-regenerate without hammering the solver on every tick.
    """

    params_changed = Signal(object)   # LayoutParams

    _ANGLES = [45, 60, 75, 90]

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

        self._auto_cb = QCheckBox("Auto-generate on change")
        self._auto_cb.setChecked(True)

        # ── stall geometry ────────────────────────────────────────────────────
        stall_box = QGroupBox("Stall")
        stall_form = QFormLayout(stall_box)

        self._angle_combo = QComboBox()
        for a in self._ANGLES:
            self._angle_combo.addItem(f"{a}°", a)
        self._angle_combo.setCurrentIndex(3)  # default 90°
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

        self._orient_slider = QSlider(Qt.Horizontal)
        self._orient_slider.setRange(0, 175)
        self._orient_slider.setSingleStep(5)
        self._orient_slider.setPageStep(15)
        self._orient_slider.setValue(0)
        self._orient_label = QLabel("0°")
        self._orient_label.setAlignment(Qt.AlignRight)
        layout_form.addRow("Orientation:", self._orient_slider)
        layout_form.addRow("", self._orient_label)

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
        vbox.addWidget(self._auto_cb)
        vbox.addWidget(stall_box)
        vbox.addWidget(aisle_box)
        vbox.addWidget(layout_box)
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

        self._on_angle_changed()  # enforce initial aisle-dir state

    # ── public ────────────────────────────────────────────────────────────────

    @property
    def auto_generate(self) -> bool:
        return self._auto_cb.isChecked()

    def current_params(self) -> LayoutParams:
        angle = self._angle_combo.currentData()
        aisle_dir = self._dir_combo.currentData()
        aisle_w_val = self._aisle_w_spin.value()
        return LayoutParams(
            orientation=float(self._orient_slider.value()),
            layout_type=LayoutType.STANDARD,
            angle=float(angle),
            stall_width=self._width_spin.value(),
            stall_length=self._length_spin.value(),
            aisle_dir=aisle_dir,
            aisle_width=aisle_w_val if aisle_w_val > 0 else None,
        )

    def current_setback(self) -> float:
        return self._setback_spin.value()

    # ── slots ─────────────────────────────────────────────────────────────────

    def _on_angle_changed(self) -> None:
        angle = self._angle_combo.currentData()
        is_90 = angle == 90
        # Only 90° supports two-way aisles in the profile
        self._dir_combo.setEnabled(is_90)
        if not is_90:
            self._dir_combo.setCurrentIndex(1)  # force one-way
        self._schedule()

    def _on_orientation_changed(self, value: int) -> None:
        self._orient_label.setText(f"{value}°")
        self._schedule()

    def _schedule(self, *_) -> None:
        if self._auto_cb.isChecked():
            self._debounce.start()

    def _emit_params(self) -> None:
        self.params_changed.emit(self.current_params())
