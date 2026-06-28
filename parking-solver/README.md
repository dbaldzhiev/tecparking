# Parking Solver

A desktop tool (PySide6 + Shapely) that generates, scores, and optimizes parking
lot layouts for an arbitrary site polygon — multiple strategies, multi-objective
ranking, and a curated dashboard of editable variants.

## Run

```powershell
.\redeploy.ps1            # reinstall (editable), smoke-check, then launch
.\redeploy.ps1 -Test      # run the fast test suite first
python -m parking_solver  # if already installed
```

## Workflow

1. **Get a site** — *Draw Polygon* (D) on the canvas, or *Open DXF/DWG* and
   *Set as Boundary* (B). A default vehicle **entrance** is placed on the longest
   edge; add more with *Add Entrance* (N). Roads always auto-connect to entrances.
2. **Explore** — runs automatically when the site is set (or press *Explore*, E).
   Several buildable strategies are tried across orientations and stall angles;
   every layout is stitched into one **connected, drivable road network** linked
   to the entrances. Results **stream into the list live** in a background thread;
   the canvas shows the best-so-far and you can click any row to preview it
   mid-run.
3. **Choose a variant** — the **Dashboard** (right dock) shows a few curated,
   buildable picks — *Best balance*, *Perimeter + infill*, *Adaptive*, *Banded
   rows*, *Herringbone* — each with a mini-preview (with the road network drawn
   on top), telemetry, and a one-line rationale. The **Pareto Explorer** (bottom)
   plots all candidates on any two objectives.
4. **Inspect & edit** — click a row or card to load it onto the canvas. Select
   stalls and *Lock* (L) the ones to keep, then re-solve around them.
5. **Export** — DXF, PDF documentation sheet, IFC, or Rhino `.3dm`. Save/Load the
   whole project as JSON.

### Generate vs Explore vs Optimize

| Action | What it does | When |
|--------|--------------|------|
| **Generate** (G) | One layout from the exact Parameters-panel settings | Manual, precise control |
| **Explore** (E)  | Decomposition variants (orientation × angle program) → live list + Dashboard | Discovery (the main path) |
| **Optimize** (O) | NSGA-II continuous search (also varies stall width) → Pareto front | Advanced fine-tuning |

### Strategies

Exploration generates only **buildable, well-connected** layouts:

- **Perimeter + infill** — a ring road around the edge with interior banded fill;
  the ring keeps everything interconnected (strong on irregular/blocky sites).
- **Adaptive** — the site is split into near-rectangular regions, each filled with
  its best module (banded or herringbone); best for irregular shapes.
- **Banded rows** — simple double-loaded rows with cross-aisles; ideal for clean
  rectangles.
- **Herringbone** — angled one-way rows; fewer spaces but easiest to park.

Banded rows and herringbone also act as the *modules* placed inside Adaptive's
regions. Empty-centre / poorly-connected patterns (pure perimeter ring,
multi-ring, spine-and-branches, mixed-angle) are **not** explored. Every layout
passes through road-network stitching + entrance-connection + one-way flow
assignment, so dead ends stay near zero and the lot is always drivable in and out.

## Metrics

Each layout is scored on: **stall count**, **m²/stall**, road **dead-ends**
(connectivity), **isolated stalls** (uniformity), **road coverage**, **entrance
link**, **circulation overhead**, and **drivable %** (one-way circuit validity).
One-way aisles are auto-oriented (serpentine) so the lot is always drivable in
*and* out.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest -q                                  # all
.\.venv\Scripts\python.exe -m pytest --ignore=tests/test_polygon_validation.py -q  # fast
```
