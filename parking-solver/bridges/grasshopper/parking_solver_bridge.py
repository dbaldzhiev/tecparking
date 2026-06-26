"""Grasshopper GHPython component — reads the native JSON project file and
rebuilds stall / boundary geometry inside Grasshopper.

USAGE — in GHPython component
------------------------------
Inputs (add these in the GH component editor):
  project_path  : str  — path to the .json file produced by the solver
  reload        : bool — connect a Button; set True to force re-read

Outputs:
  boundary      : Rhino.Geometry.Polyline
  stalls        : list[Rhino.Geometry.Polyline]
  stall_types   : list[str]
  stall_locked  : list[bool]
  aisles        : list[Rhino.Geometry.Polyline]
  total_stalls  : int
  metrics       : str  (formatted summary)

Paste this script into a GHPython component and connect the inputs/outputs.
Rhino 7 / 8, GHPython with IronPython 2 or CPython 3 engine.
"""

# ── standard Grasshopper component entry point ────────────────────────────────
import json
import os

try:
    import Rhino.Geometry as rg
    _IN_GH = True
except ImportError:
    _IN_GH = False   # running in test / standalone mode


def _coords_to_polyline(coords):
    """Convert a list of [x, y] coordinate pairs to a Rhino Polyline."""
    if not _IN_GH:
        return coords   # return raw coords when not in GH
    pts = [rg.Point3d(float(x), float(y), 0.0) for x, y in coords]
    pl = rg.Polyline(pts)
    return pl


def load_project(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def process(project_path: str):
    if not project_path or not os.path.exists(project_path):
        raise FileNotFoundError(f"Project file not found: {project_path}")

    data = load_project(project_path)

    # Boundary
    bnd_coords = data.get("boundary", {}).get("coordinates", [[]])[0]
    boundary = _coords_to_polyline(bnd_coords)

    # Stalls
    stalls_out = []
    stall_types = []
    stall_locked = []
    for s in data.get("stalls", []):
        coords = s["polygon"]["coordinates"][0]
        stalls_out.append(_coords_to_polyline(coords))
        stall_types.append(s.get("type", "standard"))
        stall_locked.append(s.get("locked", False))

    # Aisles
    aisles_out = []
    for a in data.get("aisles", []):
        geom = a["centerline"]
        if geom["type"] == "LineString":
            aisles_out.append(_coords_to_polyline(geom["coordinates"]))
        elif geom["type"] == "MultiLineString":
            for part in geom["coordinates"]:
                aisles_out.append(_coords_to_polyline(part))

    # Metrics
    m = data.get("metrics", {})
    total_stalls = m.get("total_stalls", 0)
    metrics_str = (
        f"Total: {total_stalls}  |  "
        f"{m.get('gross_area_per_stall', 0):.1f} m²/stall  |  "
        f"Site: {m.get('site_area', 0):.0f} m²"
    )

    return boundary, stalls_out, stall_types, stall_locked, aisles_out, total_stalls, metrics_str


# ── GHPython component body ──────────────────────────────────────────────────
# When pasted into a GH component the variables below map to the output params.
if _IN_GH:
    try:
        (boundary, stalls, stall_types, stall_locked,
         aisles, total_stalls, metrics) = process(project_path)  # noqa: F821
    except Exception as e:  # noqa: BLE001
        import sys
        print(f"ParkingSolverBridge error: {e}", file=sys.stderr)
