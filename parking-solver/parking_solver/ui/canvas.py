from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QPointF, Qt, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QPolygonF,
    QTransform,
    QWheelEvent,
)
from PySide6.QtWidgets import (
    QGraphicsEllipseItem,
    QGraphicsItem,
    QGraphicsPathItem,
    QGraphicsPixmapItem,
    QGraphicsPolygonItem,
    QGraphicsScene,
    QGraphicsView,
)

from parking_solver.core.model import DriveAisle, Layout, Stall, StallType

_STALL_COLORS = {
    StallType.STANDARD: QColor(80, 160, 80, 200),
    StallType.COMPACT: QColor(80, 130, 200, 200),
    StallType.ACCESSIBLE: QColor(0, 200, 220, 200),
    StallType.ACCESSIBLE_VAN: QColor(0, 180, 220, 200),
    StallType.EV: QColor(60, 80, 230, 200),
    StallType.EV_ACCESSIBLE: QColor(60, 80, 200, 200),
    StallType.MOTORCYCLE: QColor(200, 140, 40, 200),
}


def _w2s(x: float, y: float) -> QPointF:
    """World (CAD Y-up) → scene (Qt Y-down)."""
    return QPointF(x, -y)


def _poly_to_qpoly(shapely_poly) -> QPolygonF:
    return QPolygonF([_w2s(x, y) for x, y in shapely_poly.exterior.coords])


# ── QGraphicsItem subclasses ──────────────────────────────────────────────────

class BoundaryItem(QGraphicsPathItem):
    def __init__(self, polygon, parent=None):
        super().__init__(parent)
        path = QPainterPath()
        coords = list(polygon.exterior.coords)
        if coords:
            path.moveTo(_w2s(*coords[0]))
            for c in coords[1:]:
                path.lineTo(_w2s(*c))
            path.closeSubpath()
        self.setPath(path)
        self.setPen(QPen(QColor(255, 255, 255), 0))
        self.setBrush(QBrush(QColor(60, 60, 60, 100)))
        self.setZValue(1)


class StallItem(QGraphicsPolygonItem):
    _PEN_NORMAL = QPen(QColor(200, 200, 200, 160), 0)
    _PEN_LOCKED = QPen(QColor(255, 60, 60), 0.08)
    _PEN_SELECTED = QPen(QColor(255, 220, 0), 0.08)

    def __init__(self, stall: Stall, index: int, parent=None):
        super().__init__(_poly_to_qpoly(stall.polygon), parent)
        self._index = index
        self._locked = stall.locked
        color = _STALL_COLORS.get(stall.type, QColor(80, 160, 80, 200))
        self.setBrush(QBrush(color))
        self._refresh_pen()
        self.setZValue(3)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)

    @property
    def stall_index(self) -> int:
        return self._index

    def set_locked(self, locked: bool) -> None:
        self._locked = locked
        self._refresh_pen()
        self.update()

    def _refresh_pen(self) -> None:
        if self._locked:
            self.setPen(self._PEN_LOCKED)
        else:
            self.setPen(self._PEN_NORMAL)

    def paint(self, painter, option, widget=None):
        super().paint(painter, option, widget)
        if self.isSelected():
            self.setPen(self._PEN_SELECTED)
            super().paint(painter, option, widget)
            self._refresh_pen()


class AisleItem(QGraphicsPathItem):
    def __init__(self, aisle: DriveAisle, parent=None):
        super().__init__(parent)
        path = QPainterPath()

        def _add_linestring(ls):
            coords = list(ls.coords)
            if coords:
                path.moveTo(_w2s(*coords[0]))
                for c in coords[1:]:
                    path.lineTo(_w2s(*c))

        cl = aisle.centerline
        if cl.geom_type == "LineString":
            _add_linestring(cl)
        elif cl.geom_type == "MultiLineString":
            for part in cl.geoms:
                _add_linestring(part)

        self.setPath(path)
        pen = QPen(QColor(200, 180, 80, 100), aisle.width)
        pen.setCapStyle(Qt.RoundCap)
        self.setPen(pen)
        self.setZValue(2)


class DXFEntityItem(QGraphicsPathItem):
    def __init__(self, geom, handle: str, parent=None):
        super().__init__(parent)
        self._handle = handle
        path = QPainterPath()
        self._geom_to_path(geom, path)
        self.setPath(path)
        self.setPen(QPen(QColor(160, 160, 160), 0))
        self.setZValue(0)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)

    @property
    def handle(self) -> str:
        return self._handle

    def _geom_to_path(self, geom, path: QPainterPath) -> None:
        from shapely.geometry import LineString, MultiLineString, Polygon

        if isinstance(geom, Polygon):
            coords = list(geom.exterior.coords)
            if coords:
                path.moveTo(_w2s(*coords[0]))
                for c in coords[1:]:
                    path.lineTo(_w2s(*c))
                path.closeSubpath()
        elif isinstance(geom, LineString):
            coords = list(geom.coords)
            if coords:
                path.moveTo(_w2s(*coords[0]))
                for c in coords[1:]:
                    path.lineTo(_w2s(*c))
        elif isinstance(geom, MultiLineString):
            for part in geom.geoms:
                coords = list(part.coords)
                if coords:
                    path.moveTo(_w2s(*coords[0]))
                    for c in coords[1:]:
                        path.lineTo(_w2s(*c))

    def paint(self, painter, option, widget=None):
        if self.isSelected():
            self.setPen(QPen(QColor(255, 200, 0), 0))
        else:
            self.setPen(QPen(QColor(160, 160, 160), 0))
        super().paint(painter, option, widget)


class _Crosshair(QGraphicsEllipseItem):
    def __init__(self, parent=None):
        super().__init__(-0.2, -0.2, 0.4, 0.4, parent)
        self.setPen(QPen(QColor(255, 200, 0), 0))
        self.setBrush(Qt.NoBrush)
        self.setZValue(20)


# ── main canvas ───────────────────────────────────────────────────────────────

class ParkingCanvas(QGraphicsView):
    boundary_drawn = Signal(list)      # emits list[tuple[float, float]] world coords
    stall_count_changed = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)

        self.setRenderHint(QPainter.Antialiasing)
        self.setDragMode(QGraphicsView.DragMode.RubberBandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setBackgroundBrush(QBrush(QColor(30, 30, 30)))

        self._draw_mode = False
        self._draw_points: list[tuple[float, float]] = []
        self._draw_preview: list[QGraphicsItem] = []
        self._crosshair: Optional[_Crosshair] = None

        self._boundary_item: Optional[QGraphicsItem] = None
        self._layout_items: list[QGraphicsItem] = []
        self._stall_items: list[StallItem] = []
        self._dxf_items: list[DXFEntityItem] = []
        self._underlay_item: Optional[QGraphicsPixmapItem] = None

        # Middle-mouse panning state
        self._panning = False
        self._pan_start: QPointF = QPointF()

    # ── public API ────────────────────────────────────────────────────────────

    def set_draw_mode(self, active: bool) -> None:
        self._draw_mode = active
        if active:
            self.setDragMode(QGraphicsView.DragMode.NoDrag)
            self.setCursor(Qt.CrossCursor)
        else:
            self.setDragMode(QGraphicsView.DragMode.RubberBandDrag)
            self.setCursor(Qt.ArrowCursor)
            self._clear_draw_preview()
            self._draw_points.clear()

    def show_pdf_underlay(
        self,
        png_bytes: bytes,
        width_px: int,
        height_px: int,
        transform,          # PDFTransform — supplies scale (metres/pixel)
    ) -> None:
        """Display a rasterised PDF page as a semi-transparent backdrop.

        *transform* is a ``PDFTransform`` from ``import_pdf.calibrate()``.
        The pixmap is placed so its origin (top-left) maps to world (0, 0),
        scaled to metres, then Y-flipped to match the Qt coordinate system.
        """
        if self._underlay_item is not None:
            self._scene.removeItem(self._underlay_item)
            self._underlay_item = None

        pixmap = QPixmap()
        pixmap.loadFromData(png_bytes, "PNG")

        item = QGraphicsPixmapItem(pixmap)
        item.setOpacity(0.45)
        item.setZValue(-1)

        # Scale: pixels → metres, then Y-flip (PDF Y-down, scene Y-down → same)
        s = transform.scale       # metres per pixel
        item.setTransform(QTransform().scale(s, s))

        # Position origin: world (0,0) → scene (0, 0); Y already matches (both down)
        item.setPos(0.0, 0.0)

        self._scene.addItem(item)
        self._underlay_item = item
        self._fit()

    def clear_pdf_underlay(self) -> None:
        if self._underlay_item is not None:
            self._scene.removeItem(self._underlay_item)
            self._underlay_item = None

    def show_dxf_entities(self, entities: list) -> None:
        for item in self._dxf_items:
            self._scene.removeItem(item)
        self._dxf_items.clear()
        for ent in entities:
            item = DXFEntityItem(ent.geometry, ent.handle)
            self._scene.addItem(item)
            self._dxf_items.append(item)
        self._fit()

    def show_boundary(self, polygon) -> None:
        if self._boundary_item is not None:
            self._scene.removeItem(self._boundary_item)
        item = BoundaryItem(polygon)
        self._scene.addItem(item)
        self._boundary_item = item
        self._fit()

    def show_layout(self, layout: Layout) -> None:
        for item in self._layout_items:
            self._scene.removeItem(item)
        self._layout_items.clear()
        self._stall_items.clear()

        for aisle in layout.aisles:
            item = AisleItem(aisle)
            self._scene.addItem(item)
            self._layout_items.append(item)

        for idx, stall in enumerate(layout.stalls):
            item = StallItem(stall, idx)
            self._scene.addItem(item)
            self._layout_items.append(item)
            self._stall_items.append(item)

        self.stall_count_changed.emit(layout.metrics.total_stalls)

    def clear_layout(self) -> None:
        for item in self._layout_items:
            self._scene.removeItem(item)
        self._layout_items.clear()
        self._stall_items.clear()

    def selected_stall_indices(self) -> list[int]:
        return [
            item.stall_index
            for item in self._scene.selectedItems()
            if isinstance(item, StallItem)
        ]

    def set_stall_locked(self, index: int, locked: bool) -> None:
        if 0 <= index < len(self._stall_items):
            self._stall_items[index].set_locked(locked)

    def selected_handles(self) -> list[str]:
        return [
            item.handle
            for item in self._scene.selectedItems()
            if isinstance(item, DXFEntityItem)
        ]

    # ── event overrides ───────────────────────────────────────────────────────

    def wheelEvent(self, event: QWheelEvent) -> None:
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        self.scale(factor, factor)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MiddleButton:
            self._panning = True
            self._pan_start = event.pos()
            self.setCursor(Qt.ClosedHandCursor)
            event.accept()
            return

        if self._draw_mode and event.button() == Qt.LeftButton:
            world = self._to_world(event.pos())
            self._draw_points.append(world)
            self._update_draw_preview()
            event.accept()
            return

        if self._draw_mode and event.button() == Qt.RightButton:
            self._finish_polygon()
            event.accept()
            return

        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._panning:
            delta = event.pos() - self._pan_start
            self._pan_start = event.pos()
            self.horizontalScrollBar().setValue(
                self.horizontalScrollBar().value() - delta.x()
            )
            self.verticalScrollBar().setValue(
                self.verticalScrollBar().value() - delta.y()
            )
            event.accept()
            return

        if self._draw_mode:
            world = self._to_world(event.pos())
            if self._crosshair is None:
                self._crosshair = _Crosshair()
                self._scene.addItem(self._crosshair)
            self._crosshair.setPos(_w2s(*world))

        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MiddleButton:
            self._panning = False
            self.setCursor(Qt.ArrowCursor if not self._draw_mode else Qt.CrossCursor)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        if self._draw_mode and event.button() == Qt.LeftButton:
            self._finish_polygon()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def keyPressEvent(self, event) -> None:
        if self._draw_mode and event.key() in (Qt.Key_Return, Qt.Key_Enter):
            self._finish_polygon()
        else:
            super().keyPressEvent(event)

    # ── internals ─────────────────────────────────────────────────────────────

    def _to_world(self, view_pos) -> tuple[float, float]:
        sp = self.mapToScene(view_pos)
        return sp.x(), -sp.y()

    def _update_draw_preview(self) -> None:
        self._clear_draw_preview()
        pts = self._draw_points
        pen = QPen(QColor(255, 200, 0), 0)
        for i in range(len(pts) - 1):
            p1 = _w2s(*pts[i])
            p2 = _w2s(*pts[i + 1])
            line = self._scene.addLine(p1.x(), p1.y(), p2.x(), p2.y(), pen)
            line.setZValue(20)
            self._draw_preview.append(line)

    def _clear_draw_preview(self) -> None:
        for item in self._draw_preview:
            self._scene.removeItem(item)
        self._draw_preview.clear()
        if self._crosshair is not None:
            self._scene.removeItem(self._crosshair)
            self._crosshair = None

    def _finish_polygon(self) -> None:
        pts = self._draw_points.copy()
        self._clear_draw_preview()
        self._draw_points.clear()
        self.set_draw_mode(False)
        if len(pts) >= 3:
            self.boundary_drawn.emit(pts)

    def _fit(self) -> None:
        r = self._scene.itemsBoundingRect()
        if not r.isEmpty():
            self.fitInView(r.adjusted(-5, -5, 5, 5), Qt.KeepAspectRatio)
