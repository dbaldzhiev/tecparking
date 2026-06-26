"""pyRevit / Dynamo script — places parking stall families from a ParkingSolver
JSON project file.

USAGE
-----
1. In Revit, open your project with a Parking Stall family loaded.
2. Run this script via pyRevit (add to a pyRevit extension) or paste into a
   Dynamo CPython3 script node.
3. Set PROJECT_JSON_PATH to your exported .json file.
4. Set FAMILY_NAME / TYPE_MAP to match your loaded family and type names.

The script places the stall centroid as a family instance and sets a rotation
so the stall long axis aligns with the generated angle.

Coordinate note: ParkingSolver exports in metres (world XY, Y-up).
Revit internal units are feet; conversion applied automatically.
"""
from __future__ import annotations

import json
import math
import os

# ── user configuration ───────────────────────────────────────────────────────
PROJECT_JSON_PATH = r"C:\path\to\your\project.json"  # <-- edit this

FAMILY_NAME = "Parking Space"   # Revit family name (must be loaded)

# Map solver stall type → Revit family type name
TYPE_MAP = {
    "standard":       "9'-0\" x 18'-0\"",
    "compact":        "8'-0\" x 16'-0\"",
    "accessible":     "Accessible 11'-0\" x 18'-0\"",
    "accessible_van": "Van Accessible 13'-0\" x 18'-0\"",
    "ev":             "EV 9'-0\" x 18'-0\"",
    "ev_accessible":  "EV Accessible",
    "motorcycle":     "Motorcycle",
}

METRES_TO_FEET = 3.28084
# ─────────────────────────────────────────────────────────────────────────────


def _metres_to_internal(m: float) -> float:
    return m * METRES_TO_FEET


def run_in_revit():
    """Entry point when running inside Revit via pyRevit or Dynamo."""
    try:
        import clr
        clr.AddReference("RevitAPI")
        clr.AddReference("RevitAPIUI")
        from Autodesk.Revit.DB import (
            FilteredElementCollector,
            FamilySymbol,
            Transaction,
            XYZ,
        )
        from Autodesk.Revit.UI import UIApplication
    except ImportError:
        print("RevitAPI not found — this script must run inside Revit/pyRevit/Dynamo.")
        return

    # pyRevit / Dynamo both expose __revit__
    app = __revit__  # noqa: F821
    doc = app.ActiveUIDocument.Document

    if not os.path.exists(PROJECT_JSON_PATH):
        print(f"Project file not found: {PROJECT_JSON_PATH}")
        return

    with open(PROJECT_JSON_PATH, "r", encoding="utf-8") as fh:
        data = json.load(fh)

    # Find family symbols
    collector = FilteredElementCollector(doc).OfClass(FamilySymbol)
    symbols: dict[str, FamilySymbol] = {}
    for sym in collector:
        if sym.FamilyName == FAMILY_NAME:
            symbols[sym.get_Parameter(
                clr.GetClrType(Autodesk.Revit.DB.BuiltInParameter)  # noqa
            ).AsString() if False else sym.Name] = sym

    placed = 0
    skipped = 0

    with Transaction(doc, "Place Parking Stalls from ParkingSolver") as t:
        t.Start()
        for stall in data.get("stalls", []):
            stype = stall.get("type", "standard")
            type_name = TYPE_MAP.get(stype, TYPE_MAP["standard"])
            sym = symbols.get(type_name)
            if sym is None:
                skipped += 1
                continue

            if not sym.IsActive:
                sym.Activate()
                doc.Regenerate()

            coords = stall["polygon"]["coordinates"][0]
            xs = [c[0] for c in coords]
            ys = [c[1] for c in coords]
            cx = sum(xs) / len(xs)
            cy = sum(ys) / len(ys)

            loc = XYZ(
                _metres_to_internal(cx),
                _metres_to_internal(cy),
                0.0,
            )
            angle_rad = math.radians(stall.get("angle", 90.0))

            inst = doc.Create.NewFamilyInstance(
                loc, sym,
                Autodesk.Revit.DB.Structure.StructuralType.NonStructural  # noqa
            )
            # Rotate so stall faces the correct direction
            axis = Autodesk.Revit.DB.Line.CreateBound(  # noqa
                loc, XYZ(loc.X, loc.Y, 1.0)
            )
            Autodesk.Revit.DB.ElementTransformUtils.RotateElement(  # noqa
                doc, inst.Id, axis, angle_rad
            )
            placed += 1

        t.Commit()

    print(f"Placed {placed} stalls, skipped {skipped} (unknown type or missing family).")


if __name__ == "__main__":
    # When run as a plain Python script (e.g. from CLI for testing the JSON reader)
    if not os.path.exists(PROJECT_JSON_PATH):
        print(f"Set PROJECT_JSON_PATH to a valid .json file.  Currently: {PROJECT_JSON_PATH}")
    else:
        with open(PROJECT_JSON_PATH) as fh:
            d = json.load(fh)
        total = d.get("metrics", {}).get("total_stalls", 0)
        print(f"Project loaded: {total} stalls.  Run inside Revit to place families.")
else:
    # Called by pyRevit / Dynamo
    run_in_revit()
