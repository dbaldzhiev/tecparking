"""ParkingCanvas — QGraphicsView with polygon-draw mode, DXF picking, layout display.

Drawing mode UX
---------------
- Left-click: place a vertex (shown as a yellow dot).
- Hover near first vertex: snap ring appears; click closes the polygon.
- Drag any placed vertex to reposition it.
- Right-click or Escape: cancel drawing.
- Double-click or Enter: close polygon (same as snapping to first point).

Coordinate system
-----------------
World space uses CAD convention (Y-up, metres).
Scene space uses Qt convention (Y-down).
The single conversion is: scene_y = -world_y.
All items are placed in scene space; world coords are reconstructed at the boundary.
"""
from __future__ import annotations

import math
from typing import Optional

from PySide6.QtCore import QPointF, Qt, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QImage,
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
    QGraphicsLineItem,
    QGraphicsPathItem,
    QGraphicsPixmapItem,
    QGraphicsPolygonItem,
    QGraphicsScene,
    QGraphicsSimpleTextItem,
    QGraphicsView,
)

from parking_solver.core.model import (
    AisleDir,
    DriveAisle,
    Entrance,
    EntranceKind,
    Layout,
    Stall,
    StallType,
)

# ── palette ───────────────────────────────────────────────────────────────────
_STALL_COLORS = {
    StallType.STANDARD:       QColor(80,  160, 80,  200),
    StallType.COMPACT:        QColor(80,  130, 200, 200),
    StallType.ACCESSIBLE:     QColor(0,   200, 220, 200),
    StallType.ACCESSIBLE_VAN: QColor(0,   180, 220, 200),
    StallType.EV:             QColor(60,  80,  230, 200),
    StallType.EV_ACCESSIBLE:  QColor(60,  80,  200, 200),
    StallType.MOTORCYCLE:     QColor(200, 140, 40,  200),
}

_COL_VERTEX      = QColor(255, 200, 0)      # placed vertex dots
_COL_VERTEX_FIRST = QColor(80, 220, 80)     # first vertex (close-snap target)
_COL_SNAP_RING   = QColor(80, 220, 80, 180) # snap indicator ring
_COL_EDGE        = QColor(255, 200, 0)      # polygon edges being drawn
_COL_RUBBER      = QColor(255, 200, 0, 120) # rubber-band edge to cursor
_VERTEX_RADIUS_PX = 7    # screen-pixel radius of vertex handles
_SNAP_THRESHOLD_PX = 18  # screen-pixel snap radius for first vertex


def _w2s(x: float, y: float) -> QPointF:
    """World (Y-up) → scene (Y-down)."""
    return QPointF(x, -y)


def _poly_to_qpoly(shapely_poly) -> QPolygonF:
    return QPolygonF([_w2s(x, y) for x, y in shapely_poly.exterior.coords])


# ── Scene items ───────────────────────────────────────────────────────────────

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
    _PEN_NORMAL   = QPen(QColor(200, 200, 200, 160), 0)
    _PEN_LOCKED   = QPen(QColor(255, 60, 60), 0.08)
    _PEN_SELECTED = QPen(QColor(255, 220, 0), 0.08)

    def __init__(self, stall: Stall, index: int, parent=None):
        super().__init__(_poly_to_qpoly(stall.polygon), parent)
        self._index  = index
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
        self.setPen(self._PEN_LOCKED if self._locked else self._PEN_NORMAL)

    def paint(self, painter, option, widget=None):
        super().paint(painter, option, widget)
        if self.isSelected():
            self.setPen(self._PEN_SELECTED)
            super().paint(painter, option, widget)
            self._refresh_pen()


class AisleItem(QGraphicsPathItem):
    """A drive aisle rendered as a thick road band.

    One-way aisles are tinted differently and carry chevron arrows along the
    centerline; two-way aisles carry a dashed centre divider line.
    """

    _COL_TWO_WAY = QColor(200, 180, 80, 100)   # amber
    _COL_ONE_WAY = QColor(90, 180, 200, 110)   # cyan

    def __init__(self, aisle: DriveAisle, parent=None):
        super().__init__(parent)
        self._direction = aisle.direction
        self._flow = aisle.flow   # world-space unit travel vector, or None
        path = QPainterPath()

        # Collect scene-space polylines so paint() can draw arrows / dividers.
        self._segments: list[list[QPointF]] = []

        def _add_ls(ls):
            coords = list(ls.coords)
            if coords:
                pts = [_w2s(*c) for c in coords]
                self._segments.append(pts)
                path.moveTo(pts[0])
                for p in pts[1:]:
                    path.lineTo(p)

        cl = aisle.centerline
        if cl.geom_type == "LineString":
            _add_ls(cl)
        elif cl.geom_type == "MultiLineString":
            for part in cl.geoms:
                _add_ls(part)

        self.setPath(path)
        col = self._COL_ONE_WAY if aisle.direction == AisleDir.ONE_WAY else self._COL_TWO_WAY
        pen = QPen(col, aisle.width)
        pen.setCapStyle(Qt.RoundCap)
        self.setPen(pen)
        self.setZValue(2)

    def paint(self, painter, option, widget=None):
        super().paint(painter, option, widget)
        if self._direction == AisleDir.ONE_WAY:
            self._paint_arrows(painter)
        else:
            self._paint_divider(painter)

    def _paint_divider(self, painter) -> None:
        """Dashed white centre line for two-way aisles."""
        pen = QPen(QColor(235, 235, 235, 150), 0.12, Qt.DashLine)
        painter.setPen(pen)
        for pts in self._segments:
            for a, b in zip(pts, pts[1:]):
                painter.drawLine(a, b)

    def _paint_arrows(self, painter) -> None:
        """Chevron arrows along the centerline pointing in the travel direction.

        Uses the aisle's BFS-derived `flow` vector when available so arrows point
        the way a driver actually travels (away from the entrance); falls back to
        coordinate order otherwise.
        """
        pen = QPen(QColor(235, 245, 250, 220), 0.18)
        painter.setPen(pen)
        spacing = 6.0       # metres between chevrons
        size = 0.9          # chevron half-size in metres
        # World (Y-up) flow → scene (Y-down) flow
        flow_scene = (self._flow[0], -self._flow[1]) if self._flow else None
        for pts in self._segments:
            for a, b in zip(pts, pts[1:]):
                dx, dy = b.x() - a.x(), b.y() - a.y()
                seg_len = math.hypot(dx, dy)
                if seg_len < 1e-6:
                    continue
                # pu = position-stepping unit (a→b); du = chevron pointing unit
                pux, puy = dx / seg_len, dy / seg_len
                dux, duy = pux, puy
                if flow_scene is not None and (dux * flow_scene[0] + duy * flow_scene[1]) < 0:
                    dux, duy = -dux, -duy                  # point chevrons along flow
                nx, ny = -duy, dux                         # left normal of pointing dir
                n = max(int(seg_len // spacing), 1)
                for k in range(1, n + 1):
                    t = (k - 0.5) * (seg_len / n)
                    cx, cy = a.x() + pux * t, a.y() + puy * t
                    tip = QPointF(cx + dux * size, cy + duy * size)
                    left = QPointF(cx - dux * size + nx * size, cy - duy * size + ny * size)
                    right = QPointF(cx - dux * size - nx * size, cy - duy * size - ny * size)
                    painter.drawLine(left, tip)
                    painter.drawLine(right, tip)


class EntranceItem(QGraphicsPathItem):
    """A site/building entrance marker — a diamond at the entrance point.

    Fixed pixel size (ItemIgnoresTransformations) so it stays visible at any zoom.
    Green = site entrance, blue = building entrance.
    """

    _COL_SITE = QColor(80, 220, 90)
    _COL_BUILDING = QColor(90, 150, 240)

    def __init__(self, entrance: Entrance, parent=None):
        super().__init__(parent)
        r = 9
        path = QPainterPath()
        path.moveTo(0, -r)
        path.lineTo(r, 0)
        path.lineTo(0, r)
        path.lineTo(-r, 0)
        path.closeSubpath()
        self.setPath(path)
        col = self._COL_SITE if entrance.kind == EntranceKind.SITE else self._COL_BUILDING
        self.setBrush(QBrush(col))
        self.setPen(QPen(QColor(20, 20, 20), 1.5))
        self.setPos(_w2s(entrance.point.x, entrance.point.y))
        self.setZValue(10)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
        self.setToolTip(f"{entrance.kind.value.title()} entrance")


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
        self.setPen(
            QPen(QColor(255, 200, 0), 0) if self.isSelected()
            else QPen(QColor(160, 160, 160), 0)
        )
        super().paint(painter, option, widget)


class _DrawVertex(QGraphicsEllipseItem):
    """A draggable vertex handle shown during polygon drawing.

    Uses ItemIgnoresTransformations so it always appears _VERTEX_RADIUS_PX pixels
    wide regardless of zoom.  Its scene *position* (anchor) is still in scene
    coordinates so dragging works correctly.
    """

    def __init__(self, scene_pos: QPointF, index: int, is_first: bool,
                 canvas: "ParkingCanvas", parent=None):
        r = _VERTEX_RADIUS_PX
        super().__init__(-r, -r, 2 * r, 2 * r, parent)
        self._index  = index
        self._canvas = canvas
        self._color  = _COL_VERTEX_FIRST if is_first else _COL_VERTEX
        self.setBrush(QBrush(self._color))
        self.setPen(QPen(QColor(20, 20, 20), 1.5))
        self.setPos(scene_pos)
        self.setZValue(22)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
        self.setCursor(Qt.SizeAllCursor)

    def itemChange(self, change, value):
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionChange:
            # value = proposed new scene pos (QPointF)
            wx, wy = float(value.x()), float(-value.y())   # scene→world
            if 0 <= self._index < len(self._canvas._draw_points):
                self._canvas._draw_points[self._index] = (wx, wy)
                self._canvas._redraw_edges()
        return super().itemChange(change, value)

    def highlight_snap(self, active: bool) -> None:
        if active:
            pen = QPen(QColor(255, 255, 255), 2.5)
            self.setBrush(QBrush(QColor(120, 255, 120)))
        else:
            pen = QPen(QColor(20, 20, 20), 1.5)
            self.setBrush(QBrush(self._color))
        self.setPen(pen)
        self.update()


class _SnapRing(QGraphicsEllipseItem):
    """Pulsing ring that appears around the first vertex when close enough to snap."""

    def __init__(self, parent=None):
        r = _VERTEX_RADIUS_PX + 6
        super().__init__(-r, -r, 2 * r, 2 * r, parent)
        self.setPen(QPen(_COL_SNAP_RING, 2))
        self.setBrush(Qt.NoBrush)
        self.setZValue(21)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
        self.setVisible(False)


class _RubberEdge(QGraphicsLineItem):
    """The dashed line from the last placed vertex to the cursor."""

    def __init__(self, parent=None):
        super().__init__(parent)
        pen = QPen(_COL_RUBBER, 1.2, Qt.DashLine)
        self.setPen(pen)
        self.setZValue(20)


# ── Main canvas ───────────────────────────────────────────────────────────────

class ParkingCanvas(QGraphicsView):
    boundary_drawn       = Signal(list)   # list[tuple[float, float]] world coords
    stall_count_changed  = Signal(int)
    draw_status_changed  = Signal(str)    # hint text for the status bar
    entrance_placed      = Signal(float, float)  # world x, y

    def __init__(self, parent=None):
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)

        self.setRenderHint(QPainter.Antialiasing)
        self.setDragMode(QGraphicsView.DragMode.RubberBandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setBackgroundBrush(QBrush(QColor(30, 30, 30)))

        # ── entrance-placement mode ───────────────────────────────────────────
        self._entrance_mode = False

        # ── draw-mode state ───────────────────────────────────────────────────
        self._draw_mode   = False
        self._draw_points: list[tuple[float, float]] = []
        self._vertex_items: list[_DrawVertex] = []
        self._edge_items:   list[QGraphicsLineItem] = []
        self._edge_labels:  list[QGraphicsSimpleTextItem] = []   # live edge lengths (m)
        self._rubber_edge:  Optional[_RubberEdge] = None
        self._rubber_label: Optional[QGraphicsSimpleTextItem] = None
        self._snap_ring:    Optional[_SnapRing] = None
        self._snapping      = False          # cursor is near first vertex

        # ── scene content ─────────────────────────────────────────────────────
        self._boundary_item:  Optional[QGraphicsItem] = None
        self._layout_items:   list[QGraphicsItem] = []
        self._stall_items:    list[StallItem] = []
        self._dxf_items:      list[DXFEntityItem] = []
        self._entrance_items: list[EntranceItem] = []
        self._underlay_item:  Optional[QGraphicsPixmapItem] = None
        self._osm_item:       Optional[QGraphicsPixmapItem] = None
        self._osm_attrib:     Optional[QGraphicsSimpleTextItem] = None

        # ── pan state ─────────────────────────────────────────────────────────
        self._panning   = False
        self._pan_start = QPointF()

    # ── public API ────────────────────────────────────────────────────────────

    def set_entrance_mode(self, active: bool) -> None:
        """Toggle entrance-placement mode: left-click adds an entrance."""
        self._entrance_mode = active
        if active:
            self.setDragMode(QGraphicsView.DragMode.NoDrag)
            self.setCursor(Qt.PointingHandCursor)
            self.draw_status_changed.emit(
                "Click inside the site to add an entrance.  Press the button again to finish."
            )
        else:
            self.setDragMode(QGraphicsView.DragMode.RubberBandDrag)
            self.setCursor(Qt.ArrowCursor)

    def set_draw_mode(self, active: bool) -> None:
        self._draw_mode = active
        if active:
            self.setDragMode(QGraphicsView.DragMode.NoDrag)
            self.setCursor(Qt.CrossCursor)
            self._create_snap_ring()
            self.draw_status_changed.emit(
                "Click to place vertices.  "
                "Hover near first point to close.  "
                "Escape = cancel."
            )
        else:
            self.setDragMode(QGraphicsView.DragMode.RubberBandDrag)
            self.setCursor(Qt.ArrowCursor)
            self._clear_draw_state()

    def show_pdf_underlay(self, png_bytes: bytes, width_px: int, height_px: int,
                          transform) -> None:
        if self._underlay_item is not None:
            self._scene.removeItem(self._underlay_item)
        pixmap = QPixmap()
        pixmap.loadFromData(png_bytes, "PNG")
        item = QGraphicsPixmapItem(pixmap)
        item.setOpacity(0.45)
        item.setZValue(-1)
        s = transform.scale
        item.setTransform(QTransform().scale(s, s))
        item.setPos(0.0, 0.0)
        self._scene.addItem(item)
        self._underlay_item = item
        self._fit()

    def clear_pdf_underlay(self) -> None:
        if self._underlay_item is not None:
            self._scene.removeItem(self._underlay_item)
            self._underlay_item = None

    def show_osm_underlay(
        self, tiles: list, width_px: int, height_px: int, m_per_px: float,
    ) -> None:
        """Stitch OSM tiles into a black-and-white halftone backdrop.

        *tiles* is a list of (png_bytes, col, row).  The image is converted to a
        1-bit diffusion-dithered (halftone) picture, scaled to metres so a site
        drawn near the origin overlays it at real-world scale, and centred on the
        scene origin.
        """
        self.clear_osm_underlay()
        canvas_img = QImage(width_px, height_px, QImage.Format_RGB32)
        canvas_img.fill(QColor(255, 255, 255))
        painter = QPainter(canvas_img)
        for data, col, row in tiles:
            tile = QImage()
            tile.loadFromData(data, "PNG")
            if not tile.isNull():
                painter.drawImage(col * 256, row * 256, tile)
        painter.end()

        # Grayscale → 1-bit diffusion dither = black-and-white halftone.
        gray = canvas_img.convertToFormat(QImage.Format_Grayscale8)
        halftone = gray.convertToFormat(QImage.Format_Mono, Qt.DiffuseDither)
        display = halftone.convertToFormat(QImage.Format_RGB32)

        item = QGraphicsPixmapItem(QPixmap.fromImage(display))
        item.setOpacity(0.30)                    # faint backdrop, never obscures the design
        item.setZValue(-100)                     # firmly behind everything
        item.setOffset(-width_px / 2.0, -height_px / 2.0)
        item.setTransform(QTransform().scale(m_per_px, m_per_px))
        self._scene.addItem(item)
        self._osm_item = item

        # Required OSM attribution, anchored to the map's bottom-left corner.
        attrib = QGraphicsSimpleTextItem("© OpenStreetMap contributors")
        attrib.setBrush(QBrush(QColor(230, 230, 230)))
        attrib.setPen(QPen(QColor(20, 20, 20), 0.5))
        font = QFont()
        font.setPointSize(8)
        attrib.setFont(font)
        attrib.setZValue(50)
        attrib.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
        half_w = (width_px / 2.0) * m_per_px
        half_h = (height_px / 2.0) * m_per_px
        attrib.setPos(-half_w, half_h)
        self._scene.addItem(attrib)
        self._osm_attrib = attrib

        # Only frame the map when the canvas is otherwise empty; never yank the
        # view away from an existing design.
        if self._boundary_item is None and not self._layout_items and not self._dxf_items:
            self.centerOn(0.0, 0.0)

    def clear_osm_underlay(self) -> None:
        if self._osm_item is not None:
            self._scene.removeItem(self._osm_item)
            self._osm_item = None
        if self._osm_attrib is not None:
            self._scene.removeItem(self._osm_attrib)
            self._osm_attrib = None

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

        self.show_entrances(layout.entrances)
        self.stall_count_changed.emit(layout.metrics.total_stalls)

    def show_entrances(self, entrances: list) -> None:
        """Render entrance markers, replacing any previously shown."""
        for item in self._entrance_items:
            self._scene.removeItem(item)
        self._entrance_items.clear()
        for e in entrances:
            item = EntranceItem(e)
            self._scene.addItem(item)
            self._entrance_items.append(item)

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

    # ── events ────────────────────────────────────────────────────────────────

    def wheelEvent(self, event: QWheelEvent) -> None:
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        self.scale(factor, factor)
        # Reposition snap ring after zoom (its anchor is in scene space)
        if self._draw_mode and self._snap_ring and self._vertex_items:
            self._snap_ring.setPos(self._vertex_items[0].pos())

    def mousePressEvent(self, event) -> None:
        # ── middle-mouse pan ──────────────────────────────────────────────────
        if event.button() == Qt.MiddleButton:
            self._panning   = True
            self._pan_start = event.pos()
            self.setCursor(Qt.ClosedHandCursor)
            event.accept()
            return

        # ── entrance placement left click ─────────────────────────────────────
        if self._entrance_mode and event.button() == Qt.LeftButton:
            wx, wy = self._to_world(event.pos())
            self.entrance_placed.emit(wx, wy)
            event.accept()
            return

        # ── draw mode left click ──────────────────────────────────────────────
        if self._draw_mode and event.button() == Qt.LeftButton:
            if self._snapping and len(self._draw_points) >= 3:
                # Snap to first vertex → close polygon
                self._finish_polygon()
            else:
                # Check if a _DrawVertex is under the cursor so we don't
                # place a new point when the user clicks to drag
                items_under = [i for i in self.items(event.pos())
                               if isinstance(i, _DrawVertex)]
                if not items_under:
                    self._place_vertex(event.pos())
            event.accept()
            return

        # ── draw mode right click → cancel ────────────────────────────────────
        if self._draw_mode and event.button() == Qt.RightButton:
            self.set_draw_mode(False)
            self.draw_status_changed.emit("Drawing cancelled.")
            event.accept()
            return

        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        # ── pan ───────────────────────────────────────────────────────────────
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

        # ── draw mode ─────────────────────────────────────────────────────────
        if self._draw_mode:
            view_pos  = event.pos()
            scene_pos = self.mapToScene(view_pos)
            self._update_rubber_edge(scene_pos)
            self._update_snap(view_pos)
            n = len(self._draw_points)
            hint = (
                f"{n} point{'s' if n != 1 else ''} placed — "
                + ("click first point to close" if self._snapping and n >= 3
                   else "click to add vertex  |  Escape to cancel")
            )
            self.draw_status_changed.emit(hint)

        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MiddleButton:
            self._panning = False
            self.setCursor(Qt.CrossCursor if self._draw_mode else Qt.ArrowCursor)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        if self._draw_mode and event.button() == Qt.LeftButton and len(self._draw_points) >= 3:
            self._finish_polygon()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def keyPressEvent(self, event) -> None:
        if self._draw_mode:
            if event.key() in (Qt.Key_Return, Qt.Key_Enter):
                if len(self._draw_points) >= 3:
                    self._finish_polygon()
            elif event.key() == Qt.Key_Escape:
                self.set_draw_mode(False)
                self.draw_status_changed.emit("Drawing cancelled.")
            elif event.key() == Qt.Key_Backspace and self._draw_points:
                # Remove last placed vertex
                self._remove_last_vertex()
            return
        super().keyPressEvent(event)

    # ── drawing internals ─────────────────────────────────────────────────────

    def _place_vertex(self, view_pos) -> None:
        scene_pos = self.mapToScene(view_pos)
        wx, wy = scene_pos.x(), -scene_pos.y()
        idx = len(self._draw_points)
        self._draw_points.append((wx, wy))

        is_first = (idx == 0)
        v = _DrawVertex(scene_pos, idx, is_first, self)
        self._scene.addItem(v)
        self._vertex_items.append(v)

        if len(self._draw_points) >= 2:
            self._add_edge(idx - 1, idx)

        if is_first and self._snap_ring:
            self._snap_ring.setPos(scene_pos)
            self._snap_ring.setVisible(False)

    def _make_dim_label(self) -> QGraphicsSimpleTextItem:
        """A fixed-pixel-size text item used to annotate an edge length."""
        lbl = QGraphicsSimpleTextItem()
        lbl.setBrush(QBrush(QColor(255, 230, 120)))
        lbl.setPen(QPen(QColor(20, 20, 20), 0.5))
        font = QFont()
        font.setPointSize(9)
        font.setBold(True)
        lbl.setFont(font)
        lbl.setZValue(23)
        lbl.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
        self._scene.addItem(lbl)
        return lbl

    def _set_dim_label(self, lbl: QGraphicsSimpleTextItem,
                       w1: tuple[float, float], w2: tuple[float, float]) -> None:
        length = math.hypot(w2[0] - w1[0], w2[1] - w1[1])
        lbl.setText(f"{length:.1f} m")
        mid = _w2s((w1[0] + w2[0]) / 2.0, (w1[1] + w2[1]) / 2.0)
        lbl.setPos(mid.x(), mid.y())

    def _add_edge(self, i: int, j: int) -> None:
        p1 = _w2s(*self._draw_points[i])
        p2 = _w2s(*self._draw_points[j])
        line = QGraphicsLineItem(p1.x(), p1.y(), p2.x(), p2.y())
        line.setPen(QPen(_COL_EDGE, 0))
        line.setZValue(19)
        self._scene.addItem(line)
        self._edge_items.append(line)

        lbl = self._make_dim_label()
        self._set_dim_label(lbl, self._draw_points[i], self._draw_points[j])
        self._edge_labels.append(lbl)

    def _redraw_edges(self) -> None:
        """Called by _DrawVertex.itemChange when a vertex is dragged."""
        for item in self._edge_items:
            self._scene.removeItem(item)
        self._edge_items.clear()
        for lbl in self._edge_labels:
            self._scene.removeItem(lbl)
        self._edge_labels.clear()
        for i in range(1, len(self._draw_points)):
            self._add_edge(i - 1, i)
        # Also update snap ring position if first vertex moved
        if self._snap_ring and self._vertex_items:
            self._snap_ring.setPos(self._vertex_items[0].pos())

    def _remove_last_vertex(self) -> None:
        if not self._draw_points:
            return
        self._draw_points.pop()
        v = self._vertex_items.pop()
        self._scene.removeItem(v)
        # Remove the last edge + its dimension label too
        if self._edge_items:
            self._scene.removeItem(self._edge_items.pop())
        if self._edge_labels:
            self._scene.removeItem(self._edge_labels.pop())

    def _update_rubber_edge(self, scene_pos: QPointF) -> None:
        if not self._draw_points:
            return
        last = _w2s(*self._draw_points[-1])
        target = (
            self._vertex_items[0].pos()
            if (self._snapping and len(self._draw_points) >= 3)
            else scene_pos
        )
        if self._rubber_edge is None:
            self._rubber_edge = _RubberEdge()
            self._scene.addItem(self._rubber_edge)
        self._rubber_edge.setLine(last.x(), last.y(), target.x(), target.y())

        # Live length of the segment being drawn (world space is metres).
        if self._rubber_label is None:
            self._rubber_label = self._make_dim_label()
        w_last = self._draw_points[-1]
        w_target = (target.x(), -target.y())   # scene → world
        self._set_dim_label(self._rubber_label, w_last, w_target)

    def _update_snap(self, view_pos) -> None:
        """Compute screen-space distance to first vertex; set snap state."""
        if not self._vertex_items or len(self._draw_points) < 3:
            self._set_snapping(False)
            return

        first_scene = self._vertex_items[0].pos()
        first_view  = self.mapFromScene(first_scene)
        dist_px = math.hypot(view_pos.x() - first_view.x(),
                             view_pos.y() - first_view.y())
        self._set_snapping(dist_px <= _SNAP_THRESHOLD_PX)

    def _set_snapping(self, active: bool) -> None:
        if active == self._snapping:
            return
        self._snapping = active
        if self._vertex_items:
            self._vertex_items[0].highlight_snap(active)
        if self._snap_ring:
            self._snap_ring.setVisible(active)
        self.setCursor(Qt.PointingHandCursor if active else Qt.CrossCursor)

    def _create_snap_ring(self) -> None:
        if self._snap_ring is not None:
            return
        self._snap_ring = _SnapRing()
        self._scene.addItem(self._snap_ring)

    def _clear_draw_state(self) -> None:
        for v in self._vertex_items:
            self._scene.removeItem(v)
        self._vertex_items.clear()

        for e in self._edge_items:
            self._scene.removeItem(e)
        self._edge_items.clear()

        for lbl in self._edge_labels:
            self._scene.removeItem(lbl)
        self._edge_labels.clear()

        if self._rubber_edge is not None:
            self._scene.removeItem(self._rubber_edge)
            self._rubber_edge = None

        if self._rubber_label is not None:
            self._scene.removeItem(self._rubber_label)
            self._rubber_label = None

        if self._snap_ring is not None:
            self._scene.removeItem(self._snap_ring)
            self._snap_ring = None

        self._draw_points.clear()
        self._snapping = False

    def _finish_polygon(self) -> None:
        pts = list(self._draw_points)
        self._clear_draw_state()
        self._draw_mode = False
        self.setDragMode(QGraphicsView.DragMode.RubberBandDrag)
        self.setCursor(Qt.ArrowCursor)
        if len(pts) >= 3:
            self.boundary_drawn.emit(pts)

    def _to_world(self, view_pos) -> tuple[float, float]:
        sp = self.mapToScene(view_pos)
        return sp.x(), -sp.y()

    def _fit(self) -> None:
        r = self._scene.itemsBoundingRect()
        if not r.isEmpty():
            self.fitInView(r.adjusted(-5, -5, 5, 5), Qt.KeepAspectRatio)
