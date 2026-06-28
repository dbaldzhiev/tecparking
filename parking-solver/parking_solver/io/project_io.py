from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from shapely.geometry import mapping, shape

from parking_solver.core.model import (
    AisleDir,
    DriveAisle,
    Entrance,
    EntranceKind,
    Layout,
    LayoutParams,
    LayoutType,
    Metrics,
    Site,
    Stall,
    StallType,
)


class _GeoEncoder(json.JSONEncoder):
    """Handle numpy scalars/arrays that shapely.mapping may produce."""

    def default(self, obj):
        if isinstance(obj, np.integer):
            return int(obj)
        if isinstance(obj, np.floating):
            return float(obj)
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        return super().default(obj)


# ── serialisers ──────────────────────────────────────────────────────────────

def _ser_entrance(e: Entrance) -> dict:
    return {"point": mapping(e.point), "kind": e.kind.value}


def _ser_stall(s: Stall) -> dict:
    return {
        "polygon": mapping(s.polygon),
        "type": s.type.value,
        "angle": s.angle,
        "locked": s.locked,
        "source": s.source,
    }


def _ser_aisle(a: DriveAisle) -> dict:
    return {
        "centerline": mapping(a.centerline),
        "width": a.width,
        "direction": a.direction.value,
        "flow": list(a.flow) if a.flow is not None else None,
    }


def _ser_metrics(m: Metrics) -> dict:
    return {
        "total_stalls": m.total_stalls,
        "by_type": m.by_type,
        "gross_area_per_stall": m.gross_area_per_stall,
        "site_area": m.site_area,
    }


def _ser_params(p: LayoutParams) -> dict:
    return {
        "orientation": p.orientation,
        "layout_type": p.layout_type.value,
        "angle": p.angle,
        "stall_width": p.stall_width,
        "stall_length": p.stall_length,
        "aisle_dir": p.aisle_dir.value,
        "aisle_width": p.aisle_width,
    }


def _ser_site(s: Site) -> dict:
    return {
        "boundary": mapping(s.boundary),
        "obstacles": [mapping(o) for o in s.obstacles],
        "entrances": [_ser_entrance(e) for e in s.entrances],
        "setbacks": s.setbacks,
    }


def _ser_layout(layout: Layout) -> dict:
    return {
        "stalls": [_ser_stall(s) for s in layout.stalls],
        "aisles": [_ser_aisle(a) for a in layout.aisles],
        "entrances": [_ser_entrance(e) for e in layout.entrances],
        "metrics": _ser_metrics(layout.metrics),
        "params": _ser_params(layout.params),
        "profile_id": layout.profile_id,
    }


# ── deserialisers ─────────────────────────────────────────────────────────────

def _des_entrance(d: dict) -> Entrance:
    return Entrance(point=shape(d["point"]), kind=EntranceKind(d["kind"]))


def _des_stall(d: dict) -> Stall:
    return Stall(
        polygon=shape(d["polygon"]),
        type=StallType(d["type"]),
        angle=d["angle"],
        locked=d["locked"],
        source=d["source"],
    )


def _des_aisle(d: dict) -> DriveAisle:
    flow = d.get("flow")
    return DriveAisle(
        centerline=shape(d["centerline"]),
        width=d["width"],
        direction=AisleDir(d["direction"]),
        flow=tuple(flow) if flow is not None else None,
    )


def _des_metrics(d: dict) -> Metrics:
    return Metrics(
        total_stalls=d["total_stalls"],
        by_type=d["by_type"],
        gross_area_per_stall=d["gross_area_per_stall"],
        site_area=d["site_area"],
    )


def _des_params(d: dict) -> LayoutParams:
    return LayoutParams(
        orientation=d["orientation"],
        layout_type=LayoutType(d["layout_type"]),
        angle=d["angle"],
        stall_width=d["stall_width"],
        stall_length=d["stall_length"],
        aisle_dir=AisleDir(d["aisle_dir"]),
        aisle_width=d.get("aisle_width"),
    )


def _des_site(d: dict) -> Site:
    return Site(
        boundary=shape(d["boundary"]),
        obstacles=[shape(o) for o in d["obstacles"]],
        entrances=[_des_entrance(e) for e in d["entrances"]],
        setbacks=d["setbacks"],
    )


def _des_layout(d: dict) -> Layout:
    return Layout(
        stalls=[_des_stall(s) for s in d["stalls"]],
        aisles=[_des_aisle(a) for a in d["aisles"]],
        entrances=[_des_entrance(e) for e in d["entrances"]],
        metrics=_des_metrics(d["metrics"]),
        params=_des_params(d["params"]),
        profile_id=d["profile_id"],
    )


# ── public API ────────────────────────────────────────────────────────────────

def save(site: Site, layout: Layout | None, path: Path | str) -> None:
    data = {
        "version": 1,
        "site": _ser_site(site),
        "layout": _ser_layout(layout) if layout is not None else None,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, cls=_GeoEncoder)


def load(path: Path | str) -> tuple[Site, Layout | None]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    site = _des_site(data["site"])
    layout = _des_layout(data["layout"]) if data.get("layout") is not None else None
    return site, layout
