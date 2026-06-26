from __future__ import annotations

from pathlib import Path

import ezdxf

from parking_solver.core.model import Layout, StallType

_LAYERS: dict[str, int] = {
    "BOUNDARY": 7,                          # white
    "AISLE": 6,                             # magenta
    "OBSTACLE": 1,                          # red
    StallType.STANDARD.value: 2,            # yellow
    StallType.COMPACT.value: 3,             # green
    StallType.ACCESSIBLE.value: 4,          # cyan
    StallType.ACCESSIBLE_VAN.value: 4,
    StallType.EV.value: 5,                  # blue
    StallType.EV_ACCESSIBLE.value: 5,
    StallType.MOTORCYCLE.value: 30,
}


def export(layout: Layout, boundary_polygon, output_path: Path | str) -> None:
    """Write a layered DXF with boundary, stalls, and aisle centerlines."""
    doc = ezdxf.new("R2010")
    msp = doc.modelspace()

    for name, color in _LAYERS.items():
        if name not in doc.layers:
            doc.layers.add(name, color=color)

    if boundary_polygon is not None:
        _add_closed_lwpoly(msp, boundary_polygon.exterior.coords, "BOUNDARY")

    for stall in layout.stalls:
        _add_closed_lwpoly(msp, stall.polygon.exterior.coords, stall.type.value)

    for aisle in layout.aisles:
        cl = aisle.centerline
        if cl.is_empty:
            continue
        if cl.geom_type == "LineString":
            msp.add_lwpolyline(list(cl.coords), dxfattribs={"layer": "AISLE"})
        elif cl.geom_type == "MultiLineString":
            for part in cl.geoms:
                msp.add_lwpolyline(list(part.coords), dxfattribs={"layer": "AISLE"})

    doc.saveas(str(output_path))


def _add_closed_lwpoly(msp, coords, layer: str) -> None:
    pts = [(float(x), float(y)) for x, y in coords]
    if len(pts) >= 2 and pts[0] == pts[-1]:
        pts = pts[:-1]
    msp.add_lwpolyline(pts, close=True, dxfattribs={"layer": layer})
