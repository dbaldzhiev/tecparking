"""Unified results dashboard.

Presents a small, curated set of optimized parking variants as visual cards —
each with a live mini-preview of the layout, the key telemetry, a plain-language
rationale, and a one-click load into the editable canvas.  This is the holistic
"pick your variant" surface that sits on top of the exhaustive explorer and the
multi-objective selection logic.

Architecture
------------
* DashboardPanel (QDockWidget) owns a scrollable column of _VariantCard widgets.
* set_variants(variants, site) rebuilds the cards from curate_variants() output.
* Clicking a card's "Load" button emits layout_selected(Layout) → MainWindow
  loads it onto the canvas where it stays fully editable.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QColor, QPainter, QPen, QPolygonF
from PySide6.QtCore import QPointF
from PySide6.QtWidgets import (
    QDockWidget,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from parking_solver.core.model import AisleDir, Layout, Site
from parking_solver.core.selection import CuratedVariant

# Strategy accent colours (match explore/pareto panels)
_STALL_FILL = QColor(80, 160, 80, 200)
_AISLE_TWO = QColor(200, 180, 80, 160)
_AISLE_ONE = QColor(90, 180, 200, 170)
_NET_TWO = QColor(255, 225, 120, 230)   # bright centerline overlay (two-way)
_NET_ONE = QColor(140, 225, 255, 230)   # bright centerline overlay (one-way)
_BOUNDARY = QColor(210, 210, 210)
_ENTRANCE = QColor(80, 220, 90)


class _MiniPreview(QWidget):
    """Tiny scaled rendering of a layout: boundary + aisles + stalls + entrances."""

    def __init__(self, layout: Layout, site: Optional[Site], parent=None):
        super().__init__(parent)
        self._layout = layout
        self._site = site
        self.setMinimumHeight(120)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def _bounds(self):
        if self._site is not None and not self._site.boundary.is_empty:
            return self._site.boundary.bounds
        xs, ys = [], []
        for s in self._layout.stalls:
            x0, y0, x1, y1 = s.polygon.bounds
            xs += [x0, x1]
            ys += [y0, y1]
        if not xs:
            return (0.0, 0.0, 1.0, 1.0)
        return (min(xs), min(ys), max(xs), max(ys))

    def paintEvent(self, _event):
        minx, miny, maxx, maxy = self._bounds()
        w_world = max(maxx - minx, 1e-6)
        h_world = max(maxy - miny, 1e-6)

        margin = 6
        avail_w = self.width() - 2 * margin
        avail_h = self.height() - 2 * margin
        scale = min(avail_w / w_world, avail_h / h_world)
        off_x = margin + (avail_w - w_world * scale) / 2
        off_y = margin + (avail_h - h_world * scale) / 2

        def tx(x, y):
            # world (Y-up) → widget (Y-down), scaled & centred
            sx = off_x + (x - minx) * scale
            sy = off_y + (maxy - y) * scale
            return QPointF(sx, sy)

        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), QColor(35, 35, 35))

        # boundary
        if self._site is not None and not self._site.boundary.is_empty:
            poly = QPolygonF([tx(x, y) for x, y in self._site.boundary.exterior.coords])
            p.setPen(QPen(_BOUNDARY, 1))
            p.setBrush(QBrush(QColor(60, 60, 60, 80)))
            p.drawPolygon(poly)

        # aisles
        for a in self._layout.aisles:
            col = _AISLE_ONE if a.direction == AisleDir.ONE_WAY else _AISLE_TWO
            p.setPen(QPen(col, max(1.0, a.width * scale * 0.6)))
            cl = a.centerline
            parts = list(cl.geoms) if cl.geom_type == "MultiLineString" else [cl]
            for part in parts:
                coords = list(part.coords)
                for i in range(len(coords) - 1):
                    p.drawLine(tx(*coords[i]), tx(*coords[i + 1]))

        # stalls
        p.setPen(QPen(QColor(40, 40, 40), 0))
        p.setBrush(QBrush(_STALL_FILL))
        for s in self._layout.stalls:
            poly = QPolygonF([tx(x, y) for x, y in s.polygon.exterior.coords])
            p.drawPolygon(poly)

        # road-network overlay — thin bright centerlines so the interconnected
        # network is clearly readable on top of the stalls.
        p.setBrush(Qt.NoBrush)
        for a in self._layout.aisles:
            bright = _NET_ONE if a.direction == AisleDir.ONE_WAY else _NET_TWO
            p.setPen(QPen(bright, 1.4))
            cl = a.centerline
            parts = list(cl.geoms) if cl.geom_type == "MultiLineString" else [cl]
            for part in parts:
                coords = list(part.coords)
                for i in range(len(coords) - 1):
                    p.drawLine(tx(*coords[i]), tx(*coords[i + 1]))

        # entrances
        p.setPen(QPen(QColor(20, 20, 20), 1))
        p.setBrush(QBrush(_ENTRANCE))
        for e in self._layout.entrances:
            c = tx(e.point.x, e.point.y)
            p.drawEllipse(c, 3.5, 3.5)

        p.end()


class _VariantCard(QFrame):
    """A single curated variant: preview, headline, telemetry, rationale, load btn."""

    load_requested = Signal(object)   # Layout

    def __init__(self, variant: CuratedVariant, site: Optional[Site], rank: int, parent=None):
        super().__init__(parent)
        self._variant = variant
        self.setFrameShape(QFrame.StyledPanel)
        self.setStyleSheet(
            "QFrame { background: #2a2a2a; border: 1px solid #444; border-radius: 6px; }"
            "QLabel { border: none; }"
        )

        r = variant.result
        m = r.layout.metrics

        vbox = QVBoxLayout(self)
        vbox.setContentsMargins(8, 8, 8, 8)
        vbox.setSpacing(6)

        # ── header ────────────────────────────────────────────────────────────
        header = QHBoxLayout()
        title = QLabel(f"{rank}. {variant.label}")
        title.setStyleSheet("font-weight: bold; font-size: 13px; color: #eee;")
        badge = QLabel(f"score {variant.composite * 100:.0f}")
        badge.setStyleSheet(
            "color: #1e1e1e; background: #8fd18f; border-radius: 6px; "
            "padding: 1px 6px; font-size: 11px; font-weight: bold;"
        )
        header.addWidget(title)
        header.addStretch()
        header.addWidget(badge)
        vbox.addLayout(header)

        # ── preview ───────────────────────────────────────────────────────────
        vbox.addWidget(_MiniPreview(r.layout, site))

        # ── telemetry grid ─────────────────────────────────────────────────────
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(2)
        cells = [
            ("Stalls", f"{r.stall_count}"),
            ("m²/stall", f"{m.gross_area_per_stall:.1f}"),
            ("Strategy", "Adaptive" if r.layout_type.value == "subdivided"
                         else r.layout_type.value.replace("_", " ").title()),
            ("Angle", "auto" if not r.angle else f"{r.angle:.0f}°"),
            ("Dead ends", f"{r.dead_ends * 100:.0f}%"),
            ("Isolated", f"{r.stall_isolation * 100:.0f}%"),
            ("Road reach", f"{r.road_coverage * 100:.0f}%"),
            ("Entrance", f"{r.entrance_connectivity * 100:.0f}%"),
            ("Circulation", f"{r.aisle_area_ratio * 100:.0f}%"),
            ("Drivable", f"{r.circuit_validity * 100:.0f}%"),
        ]
        for i, (k, v) in enumerate(cells):
            row, col = divmod(i, 2)
            kl = QLabel(f"{k}:")
            kl.setStyleSheet("color: #999; font-size: 11px;")
            vl = QLabel(v)
            vl.setStyleSheet("color: #ddd; font-size: 11px; font-weight: bold;")
            grid.addWidget(kl, row, col * 2)
            grid.addWidget(vl, row, col * 2 + 1)
        vbox.addLayout(grid)

        # ── rationale ───────────────────────────────────────────────────────────
        rationale = QLabel(variant.rationale)
        rationale.setWordWrap(True)
        rationale.setStyleSheet("color: #bbb; font-size: 11px; font-style: italic;")
        vbox.addWidget(rationale)

        # ── load button ─────────────────────────────────────────────────────────
        btn = QPushButton("Load into canvas (editable)")
        btn.setStyleSheet(
            "QPushButton { background: #3a5; color: #fff; border-radius: 4px; padding: 4px; }"
            "QPushButton:hover { background: #4b6; }"
        )
        btn.clicked.connect(lambda: self.load_requested.emit(r.layout))
        vbox.addWidget(btn)


class DashboardPanel(QDockWidget):
    """Curated-variants dashboard dock.

    Signals
    -------
    layout_selected(Layout) — emitted when the user loads a variant card.
    """

    layout_selected = Signal(object)   # Layout

    def __init__(self, parent=None):
        super().__init__("Dashboard", parent)
        self.setAllowedAreas(
            Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea | Qt.BottomDockWidgetArea
        )
        self.setMinimumWidth(300)

        self._status = QLabel("Run Explore to see optimized variants.")
        self._status.setAlignment(Qt.AlignCenter)
        self._status.setWordWrap(True)
        self._status.setStyleSheet("color: #aaa; padding: 8px;")

        self._cards_host = QWidget()
        self._cards_layout = QVBoxLayout(self._cards_host)
        self._cards_layout.setContentsMargins(6, 6, 6, 6)
        self._cards_layout.setSpacing(8)
        self._cards_layout.addStretch()

        inner = QWidget()
        vbox = QVBoxLayout(inner)
        vbox.setContentsMargins(0, 0, 0, 0)
        vbox.addWidget(self._status)
        vbox.addWidget(self._cards_host)
        vbox.addStretch()

        scroll = QScrollArea()
        scroll.setWidget(inner)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.setWidget(scroll)

    # ── public API ────────────────────────────────────────────────────────────

    def set_variants(self, variants: list[CuratedVariant], site: Optional[Site]) -> None:
        """Rebuild the card column from a curated variant list."""
        # clear old cards (keep the trailing stretch)
        while self._cards_layout.count() > 1:
            item = self._cards_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        if not variants:
            self._status.setText("No layouts found for this site.")
            return

        self._status.setText(
            f"{len(variants)} optimized variants — click to load (stays editable)."
        )
        for rank, v in enumerate(variants, start=1):
            card = _VariantCard(v, site, rank)
            card.load_requested.connect(self.layout_selected.emit)
            self._cards_layout.insertWidget(self._cards_layout.count() - 1, card)
