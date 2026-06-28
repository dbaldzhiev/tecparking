"""OpenStreetMap tile fetching for the canvas backdrop.

Respects the OSMF tile usage policy (https://operations.osmfoundation.org/policies/tiles/):
  * sends a genuine, identifying User-Agent (never a faked/library default one);
  * caches every tile on disk so a tile is downloaded at most once, ever — repeat
    toggles do zero network traffic;
  * only fetches a small area on explicit user request, with a short delay
    between *actual* downloads to stay well under the rate limits;
  * the caller shows the required "© OpenStreetMap contributors" attribution.

Qt-free, per the io/ layering rule.  ATTRIBUTION is exported for the UI to draw.
"""
from __future__ import annotations

import math
import time
from pathlib import Path
from urllib.request import Request, urlopen

# Plovdiv, Bulgaria — the default starting position.
PLOVDIV_LAT = 42.1354
PLOVDIV_LON = 24.7453

ATTRIBUTION = "© OpenStreetMap contributors"

_TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
# A specific, identifying User-Agent is required by the policy.
_USER_AGENT = "tecparking-solver/1.0 (desktop parking-layout tool; non-commercial)"
_TILE_PX = 256
_CACHE_DIR = Path.home() / ".parking_solver" / "osm_cache"
_MIN_DOWNLOAD_INTERVAL = 0.15   # seconds between *network* requests (cache hits are free)

_last_download = 0.0


def meters_per_pixel(lat: float, zoom: int) -> float:
    """Ground resolution of a web-mercator tile pixel at *lat* / *zoom*."""
    return 156543.03392 * math.cos(math.radians(lat)) / (2 ** zoom)


def _deg2tile(lat: float, lon: float, zoom: int) -> tuple[float, float]:
    lat_r = math.radians(lat)
    n = 2.0 ** zoom
    x = (lon + 180.0) / 360.0 * n
    y = (1.0 - math.asinh(math.tan(lat_r)) / math.pi) / 2.0 * n
    return x, y


def _tile_bytes(z: int, x: int, y: int, timeout: float) -> bytes:
    """Return one tile's PNG bytes, from the on-disk cache when available."""
    global _last_download
    cache = _CACHE_DIR / f"{z}_{x}_{y}.png"
    if cache.exists():
        return cache.read_bytes()

    # Throttle real downloads only.
    wait = _MIN_DOWNLOAD_INTERVAL - (time.monotonic() - _last_download)
    if wait > 0:
        time.sleep(wait)

    url = _TILE_URL.format(z=z, x=x, y=y)
    req = Request(url, headers={"User-Agent": _USER_AGENT})
    with urlopen(req, timeout=timeout) as resp:
        data = resp.read()
    _last_download = time.monotonic()

    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache.write_bytes(data)
    return data


def fetch_osm_area(
    lat: float = PLOVDIV_LAT,
    lon: float = PLOVDIV_LON,
    zoom: int = 15,
    grid: int = 4,
    timeout: float = 10.0,
) -> tuple[list[tuple[bytes, int, int]], int, int, float]:
    """Fetch a *grid*×*grid* block of OSM tiles centred on (lat, lon).

    Tiles come from the on-disk cache when present, so this hits the network only
    for tiles never seen before.  Returns ``(tiles, width_px, height_px, m_per_px)``
    where *tiles* is a list of ``(png_bytes, col, row)``.  Raises on network
    failure (the caller shows a message).
    """
    fx, fy = _deg2tile(lat, lon, zoom)
    cx, cy = int(fx), int(fy)
    half = grid // 2

    tiles: list[tuple[bytes, int, int]] = []
    for row, ty in enumerate(range(cy - half, cy - half + grid)):
        for col, tx in enumerate(range(cx - half, cx - half + grid)):
            tiles.append((_tile_bytes(zoom, tx, ty, timeout), col, row))

    size_px = grid * _TILE_PX
    return tiles, size_px, size_px, meters_per_pixel(lat, zoom)
