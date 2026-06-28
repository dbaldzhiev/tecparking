from __future__ import annotations

from pathlib import Path
from typing import Optional

from shapely.geometry import Point

from parking_solver.core import generator
from parking_solver.core.ada_placement import place_special_stalls
from parking_solver.core.geometry.helpers import longest_edge_midpoint
from parking_solver.core.model import (
    Entrance,
    EntranceKind,
    FixedElements,
    Layout,
    LayoutParams,
    Site,
)
from parking_solver.core.regulations.engine import RegulationProfile, load_profile
from parking_solver.io import export_dxf, import_dxf, project_io


def _default_entrance(polygon) -> Entrance:
    """A single site entrance at the midpoint of the longest boundary edge."""
    mx, my = longest_edge_midpoint(polygon)
    return Entrance(point=Point(mx, my), kind=EntranceKind.SITE)

_DEFAULT_PROFILE = (
    Path(__file__).parent.parent / "core" / "regulations" / "profiles" / "generic_eu.yaml"
)


class Controller:
    """Owns project state and orchestrates core ↔ UI.  Synchronous in Phase 0-2."""

    def __init__(self) -> None:
        self.site: Optional[Site] = None
        self.layout: Optional[Layout] = None
        self.params = LayoutParams()
        self.profile: RegulationProfile = load_profile(_DEFAULT_PROFILE)
        self._dxf_entities: list[import_dxf.DXFEntity] = []

    # ── DXF import ────────────────────────────────────────────────────────────

    def open_dxf(self, path: str | Path) -> list[import_dxf.DXFEntity]:
        doc = import_dxf.load_doc(path)
        self._dxf_entities = import_dxf.list_entities(doc)
        return self._dxf_entities

    def set_boundary_from_entities(self, handles: list[str], tol: float = 0.01) -> None:
        selected = [e for e in self._dxf_entities if e.handle in handles]
        if not selected:
            raise ValueError("No matching entities for the given handles")
        poly = import_dxf.boundary_from_entities(selected, tol)
        self.site = Site(boundary=poly, entrances=[_default_entrance(poly)])
        self.layout = None

    def set_boundary_from_polygon(self, coords: list[tuple[float, float]]) -> None:
        from shapely.geometry import Polygon
        poly = Polygon(coords)
        self.site = Site(boundary=poly, entrances=[_default_entrance(poly)])
        self.layout = None

    # ── entrances ───────────────────────────────────────────────────────────────

    def add_entrance(self, x: float, y: float, kind: EntranceKind = EntranceKind.SITE) -> None:
        """Append a user-placed entrance at world coords (x, y)."""
        if self.site is None:
            return
        self.site.entrances.append(Entrance(point=Point(x, y), kind=kind))

    def clear_entrances(self) -> None:
        if self.site is not None:
            self.site.entrances = []

    # ── params / site config ──────────────────────────────────────────────────

    def update_params(self, params: LayoutParams) -> None:
        self.params = params

    def update_setback(self, setback: float) -> None:
        if self.site is not None:
            self.site.setbacks = setback

    # ── stall locking ─────────────────────────────────────────────────────────

    def set_stalls_locked(self, indices: list[int], locked: bool) -> None:
        if self.layout is None:
            return
        for i in indices:
            if 0 <= i < len(self.layout.stalls):
                self.layout.stalls[i].locked = locked

    def locked_stalls(self) -> FixedElements:
        if self.layout is None:
            return FixedElements()
        return FixedElements(stalls=[s for s in self.layout.stalls if s.locked])

    # ── solver ────────────────────────────────────────────────────────────────

    def generate(self) -> Optional[Layout]:
        if self.site is None:
            return None
        fixed = self.locked_stalls()
        raw = generator.generate(self.site, self.profile, self.params, fixed=fixed or None)
        self.layout = place_special_stalls(raw, self.site, self.profile)
        return self.layout

    # ── export ────────────────────────────────────────────────────────────────

    def export_dxf(self, path: str | Path) -> None:
        if self.layout is None:
            raise RuntimeError("No layout to export — generate first")
        boundary = self.site.boundary if self.site else None
        export_dxf.export(self.layout, boundary, path)

    # ── project IO ───────────────────────────────────────────────────────────

    def save_project(self, path: str | Path) -> None:
        if self.site is None:
            raise RuntimeError("Nothing to save — open or draw a boundary first")
        project_io.save(self.site, self.layout, path)

    def load_project(self, path: str | Path) -> None:
        self.site, self.layout = project_io.load(path)
        self._dxf_entities = []
