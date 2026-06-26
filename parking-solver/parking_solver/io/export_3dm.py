"""Rhino .3dm export — writes stalls and boundary as curves on typed layers.

Requires ``rhino3dm`` (needs CMake to build; install separately):
    pip install rhino3dm   # only works if cmake is available

Stall polygons → closed PolylineCurves on per-type layers.
Boundary polygon → closed PolylineCurve on layer "BOUNDARY".
Aisles → open PolylineCurves on layer "AISLE".

All geometry is 2D (z = 0) in the world XY plane.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from parking_solver.core.model import Layout, Site, StallType

_LAYER_COLORS = {
    "BOUNDARY":      (255, 255, 255),
    "AISLE":         (200, 180, 80),
    StallType.STANDARD:       (80,  160, 80),
    StallType.COMPACT:        (80,  130, 200),
    StallType.ACCESSIBLE:     (0,   200, 220),
    StallType.ACCESSIBLE_VAN: (0,   180, 220),
    StallType.EV:             (60,  80,  230),
    StallType.EV_ACCESSIBLE:  (60,  80,  200),
    StallType.MOTORCYCLE:     (200, 140, 40),
}
_STALL_LAYER = {
    StallType.STANDARD:       "STALL_STANDARD",
    StallType.COMPACT:        "STALL_COMPACT",
    StallType.ACCESSIBLE:     "STALL_ACCESSIBLE",
    StallType.ACCESSIBLE_VAN: "STALL_ACCESSIBLE_VAN",
    StallType.EV:             "STALL_EV",
    StallType.EV_ACCESSIBLE:  "STALL_EV_ACCESSIBLE",
    StallType.MOTORCYCLE:     "STALL_MOTORCYCLE",
}


def export(layout: Layout, site: Optional[Site], path: str | Path) -> None:
    """Write *layout* to a Rhino .3dm file at *path*."""
    try:
        import rhino3dm
    except ImportError as exc:
        raise ImportError(
            "rhino3dm is required for .3dm export.  "
            "Install CMake then: pip install rhino3dm"
        ) from exc

    path = Path(path)
    model = rhino3dm.File3dm()
    model.Settings.ModelUnitSystem = rhino3dm.UnitSystem.Meters

    layer_indices: dict[str, int] = {}

    def _get_layer(name: str, rgb: tuple) -> int:
        if name not in layer_indices:
            layer = rhino3dm.Layer()
            layer.Name = name
            layer.Color = (rgb[0], rgb[1], rgb[2], 255)
            idx = model.Layers.Add(layer)
            layer_indices[name] = idx
        return layer_indices[name]

    def _poly_curve(coords) -> rhino3dm.PolylineCurve:
        pts = [rhino3dm.Point3d(float(x), float(y), 0.0) for x, y in coords]
        return rhino3dm.PolylineCurve(pts)

    # Boundary
    if site:
        coords = list(site.boundary.exterior.coords)
        crv = _poly_curve(coords)
        attr = rhino3dm.ObjectAttributes()
        attr.LayerIndex = _get_layer("BOUNDARY", (255, 255, 255))
        model.Objects.AddCurve(crv, attr)

    # Aisles
    for aisle in layout.aisles:
        cl = aisle.centerline
        if cl.geom_type == "LineString":
            geoms = [cl]
        else:
            geoms = list(cl.geoms)
        for geom in geoms:
            crv = _poly_curve(list(geom.coords))
            attr = rhino3dm.ObjectAttributes()
            attr.LayerIndex = _get_layer("AISLE", (200, 180, 80))
            model.Objects.AddCurve(crv, attr)

    # Stalls
    for stall in layout.stalls:
        layer_name = _STALL_LAYER.get(stall.type, "STALL_STANDARD")
        rgb = _LAYER_COLORS.get(stall.type, (80, 160, 80))
        coords = list(stall.polygon.exterior.coords)
        crv = _poly_curve(coords)
        attr = rhino3dm.ObjectAttributes()
        attr.LayerIndex = _get_layer(layer_name, rgb)
        model.Objects.AddCurve(crv, attr)

    model.Write(str(path), 7)   # Rhino 7 format
