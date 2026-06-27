# Parking Layout Generator

A desktop app that takes a 2D site boundary and generates regulation-compliant parking layouts in real time. Stalls can be locked in place while the solver re-fills the remaining area. A multi-objective optimizer (NSGA-II) explores the design space and presents a Pareto front of candidates to browse and compare.

**Key features**

- Import site boundary from DXF / DWG, or draw it directly on the canvas
- Parametric control: stall angle (45 / 60 / 75 / 90°), orientation sweep, stall dimensions, aisle width
- Auto-placement of ADA-accessible and EV stalls from a swappable regulation profile (YAML)
- Select, lock, and unlock individual stalls — re-solve flows around what's fixed
- Multi-objective optimization with live Pareto scatter and advantages/disadvantages summary
- PDF underlay with 2-point scale calibration
- Export to DXF, IFC (Revit/ArchiCAD), PDF documentation sheet, and Rhino .3dm

---

## Requirements

- Python 3.11 or newer
- CMake (optional — only needed for the `.3dm` Rhino export via `rhino3dm`)

---

## Installation

```bash
cd parking-solver
python -m venv .venv

# Windows
.venv\Scripts\activate

# macOS / Linux
source .venv/bin/activate

pip install -e ".[dev]"
```

---

## Run

```bash
parking-solver
```

Or without the entry point:

```bash
python -m parking_solver
```

---

## Quick start

1. **Draw a boundary** — click `Draw Polygon` (D), place vertices, hover near the first point to snap-close.
2. **Generate** — press `G`. Adjust angle, orientation, and stall dimensions in the Parameters panel.
3. **Lock stalls** — rubber-band select stalls, press `L`. Press `G` again; locked stalls stay put.
4. **Optimize** — press `O`, set population / generations, and watch the Pareto front build live.
5. **Export** — DXF, PDF, or IFC from the Export menu.

---

## Run tests

```bash
pytest
```
