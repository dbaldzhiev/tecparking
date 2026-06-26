from __future__ import annotations

import math
from pathlib import Path
from typing import NamedTuple

import ezdxf
from ezdxf import recover
from shapely.geometry import LineString, Polygon
from shapely.ops import linemerge


class DXFEntity(NamedTuple):
    handle: str
    type: str
    geometry: object  # Shapely LineString or Polygon


def load_doc(path: Path | str):
    """Load a DXF document, recovering if needed."""
    try:
        return ezdxf.readfile(str(path))
    except Exception:
        doc, _ = recover.readfile(str(path))
        return doc


def list_entities(doc) -> list[DXFEntity]:
    """Return renderable entities (LINE, LWPOLYLINE, POLYLINE, ARC, CIRCLE, SPLINE) from modelspace."""
    msp = doc.modelspace()
    result = []
    for e in msp:
        geom = _entity_to_geom(e)
        if geom is not None:
            result.append(DXFEntity(handle=e.dxf.handle, type=e.dxftype(), geometry=geom))
    return result


def boundary_from_entities(entities: list[DXFEntity], tol: float = 0.01) -> Polygon:
    """Chain selected entities into a closed Polygon within tol."""
    lines: list[LineString] = []
    for ent in entities:
        g = ent.geometry
        if isinstance(g, Polygon):
            return g  # fast path: already a closed loop
        if isinstance(g, LineString):
            lines.append(g)

    if not lines:
        raise ValueError("No line geometry in selected entities to chain into a boundary")

    merged = linemerge(lines)

    # merged may be a single LineString or a MultiLineString
    candidates = merged.geoms if merged.geom_type == "MultiLineString" else [merged]

    for part in candidates:
        coords = list(part.coords)
        if _dist(coords[0], coords[-1]) <= tol:
            return Polygon(coords)

    # Fallback: close the longest segment
    longest = max(candidates, key=lambda seg: seg.length)
    coords = list(longest.coords)
    coords.append(coords[0])
    return Polygon(coords)


# ── internals ─────────────────────────────────────────────────────────────────

def _dist(a, b) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _entity_to_geom(entity):
    t = entity.dxftype()
    try:
        if t == "LWPOLYLINE":
            pts = list(entity.get_points("xy"))
            if not pts:
                return None
            if entity.is_closed or _dist(pts[0], pts[-1]) < 1e-9:
                return Polygon(pts)
            return LineString(pts)

        if t == "POLYLINE":
            pts = [(v.dxf.location.x, v.dxf.location.y) for v in entity.vertices]
            if not pts:
                return None
            if entity.is_closed:
                return Polygon(pts)
            return LineString(pts)

        if t == "LINE":
            s = entity.dxf.start
            e = entity.dxf.end
            return LineString([(s.x, s.y), (e.x, e.y)])

        if t in ("ARC", "CIRCLE", "SPLINE", "ELLIPSE"):
            pts = [(p.x, p.y) for p in entity.flattening(0.01)]
            if len(pts) < 2:
                return None
            if _dist(pts[0], pts[-1]) < 0.01:
                return Polygon(pts)
            return LineString(pts)

    except Exception:
        pass
    return None
