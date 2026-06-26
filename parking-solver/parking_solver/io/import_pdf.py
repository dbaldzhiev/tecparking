"""PDF underlay import — rasterise a page and compute a pixel→metre transform.

Two-point calibration workflow
-------------------------------
1. Call ``rasterise(path, page=0, dpi=150)`` → returns ``(image_bytes, page_w_px, page_h_px)``.
2. Display the image in the canvas as a backdrop.
3. The user clicks two points in *pixel* space (p1_px, p2_px) and enters the
   real-world distance between them in metres → ``calibrate(p1_px, p2_px, real_metres)``.
4. ``calibrate`` returns a ``PDFTransform`` that converts any pixel coordinate to
   metres in world space (origin at p1_px).

This module is UI-agnostic; the canvas / dialog layer owns Qt interaction.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path


@dataclass
class PDFTransform:
    """Pixel-space → world-space (metres) affine transform.

    Derived from a two-point calibration:
      origin_px  : pixel coordinates of the first calibration point
      scale      : metres per pixel
    """
    origin_px: tuple[float, float]
    scale: float          # metres / pixel

    def to_world(self, x_px: float, y_px: float) -> tuple[float, float]:
        """Convert a pixel coordinate to world metres (Y-up, origin at cal point 1)."""
        dx = x_px - self.origin_px[0]
        dy = -(y_px - self.origin_px[1])   # flip Y: PDF Y-down → world Y-up
        return dx * self.scale, dy * self.scale


def rasterise(
    path: str | Path,
    page: int = 0,
    dpi: int = 150,
) -> tuple[bytes, int, int]:
    """Render a PDF page to a PNG byte-string.

    Returns (png_bytes, width_px, height_px).
    Caller owns the bytes — pass to ``QPixmap.loadFromData()`` for display.
    """
    try:
        import fitz  # PyMuPDF
    except ImportError as exc:
        raise ImportError("PyMuPDF (fitz) is required for PDF import.  pip install PyMuPDF") from exc

    doc = fitz.open(str(path))
    if page >= len(doc):
        raise ValueError(f"PDF has {len(doc)} pages; requested page {page}")

    pg = doc.load_page(page)
    mat = fitz.Matrix(dpi / 72, dpi / 72)
    pix = pg.get_pixmap(matrix=mat, alpha=False)
    return pix.tobytes("png"), pix.width, pix.height


def calibrate(
    p1_px: tuple[float, float],
    p2_px: tuple[float, float],
    real_metres: float,
) -> PDFTransform:
    """Derive a pixel→metre transform from two clicked calibration points.

    ``real_metres`` is the known distance between p1 and p2 in the real world.
    """
    if real_metres <= 0:
        raise ValueError("real_metres must be positive")
    dx = p2_px[0] - p1_px[0]
    dy = p2_px[1] - p1_px[1]
    dist_px = math.hypot(dx, dy)
    if dist_px < 1:
        raise ValueError("Calibration points are too close together")
    scale = real_metres / dist_px
    return PDFTransform(origin_px=p1_px, scale=scale)
