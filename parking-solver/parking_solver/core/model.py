from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Literal

from shapely.geometry import LineString, Point, Polygon


class StallType(Enum):
    STANDARD = "standard"
    COMPACT = "compact"
    ACCESSIBLE = "accessible"
    ACCESSIBLE_VAN = "accessible_van"
    EV = "ev"
    EV_ACCESSIBLE = "ev_accessible"
    MOTORCYCLE = "motorcycle"


class LayoutType(Enum):
    STANDARD = "standard"
    FISHBONE = "fishbone"  # Phase 1+


class AisleDir(Enum):
    ONE_WAY = "one_way"
    TWO_WAY = "two_way"


class EntranceKind(Enum):
    SITE = "site"
    BUILDING = "building"


@dataclass
class Entrance:
    point: Point
    kind: EntranceKind


@dataclass
class Site:
    boundary: Polygon
    obstacles: list[Polygon] = field(default_factory=list)
    entrances: list[Entrance] = field(default_factory=list)
    setbacks: float = 0.0


@dataclass
class Stall:
    polygon: Polygon
    type: StallType = StallType.STANDARD
    angle: float = 90.0
    locked: bool = False
    source: Literal["generated", "manual"] = "generated"


@dataclass
class DriveAisle:
    centerline: LineString
    width: float
    direction: AisleDir


@dataclass
class LayoutParams:
    orientation: float = 0.0
    layout_type: LayoutType = LayoutType.STANDARD
    angle: float = 90.0
    stall_width: float = 2.5
    stall_length: float = 5.0
    aisle_dir: AisleDir = AisleDir.TWO_WAY
    aisle_width: float | None = None  # None -> take from profile


@dataclass
class Metrics:
    total_stalls: int
    by_type: dict[str, int]
    gross_area_per_stall: float
    site_area: float


@dataclass
class FixedElements:
    """Locked stalls that must survive a re-solve."""
    stalls: list[Stall] = field(default_factory=list)

    def as_obstacle_union(self, clearance: float = 0.0):
        """Return union of locked stall footprints, optionally buffered."""
        from shapely.ops import unary_union
        geoms = [s.polygon.buffer(clearance) if clearance else s.polygon for s in self.stalls]
        return unary_union(geoms) if geoms else None


@dataclass
class Layout:
    stalls: list[Stall]
    aisles: list[DriveAisle]
    entrances: list[Entrance]
    metrics: Metrics
    params: LayoutParams
    profile_id: str
