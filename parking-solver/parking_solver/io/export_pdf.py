"""PDF documentation export — permit-style sheet with layout drawing, legend,
north arrow, and stall schedule.

Uses reportlab.  One A3 landscape page (420 × 297 mm).
"""
from __future__ import annotations

import math
from pathlib import Path
from typing import Optional

from parking_solver.core.model import Layout, Site, StallType

try:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A3, landscape
    from reportlab.lib.units import mm
    from reportlab.pdfgen import canvas as rl_canvas
    from reportlab.platypus import Table, TableStyle
    _REPORTLAB = True
except ImportError:
    _REPORTLAB = False

# Stall type → RGB tuple (0-1 scale) for the drawing
_TYPE_COLOR = {
    StallType.STANDARD:      (0.31, 0.63, 0.31),
    StallType.COMPACT:       (0.31, 0.51, 0.78),
    StallType.ACCESSIBLE:    (0.00, 0.78, 0.86),
    StallType.ACCESSIBLE_VAN:(0.00, 0.71, 0.86),
    StallType.EV:            (0.24, 0.31, 0.90),
    StallType.EV_ACCESSIBLE: (0.24, 0.31, 0.78),
    StallType.MOTORCYCLE:    (0.78, 0.55, 0.16),
}
_TYPE_LABEL = {
    StallType.STANDARD:       "Standard",
    StallType.COMPACT:        "Compact",
    StallType.ACCESSIBLE:     "Accessible",
    StallType.ACCESSIBLE_VAN: "Accessible Van",
    StallType.EV:             "EV",
    StallType.EV_ACCESSIBLE:  "EV Accessible",
    StallType.MOTORCYCLE:     "Motorcycle",
}


def export(
    layout: Layout,
    site: Optional[Site],
    path: str | Path,
    title: str = "Parking Layout",
    north_angle: float = 0.0,
) -> None:
    """Write a single-page A3 landscape PDF documentation sheet.

    Parameters
    ----------
    north_angle : degrees clockwise from screen-up to true North (for north arrow)
    """
    if not _REPORTLAB:
        raise ImportError("reportlab is required for PDF export.  pip install reportlab")

    path = Path(path)
    page_w, page_h = landscape(A3)

    # Drawing area: left 65% of page
    draw_margin = 15 * mm
    draw_w = page_w * 0.65 - draw_margin * 2
    draw_h = page_h - draw_margin * 2
    draw_origin_x = draw_margin
    draw_origin_y = draw_margin

    c = rl_canvas.Canvas(str(path), pagesize=landscape(A3))

    # ── compute world-to-page scale ───────────────────────────────────────────
    boundary = site.boundary if site else None
    all_polys = [s.polygon for s in layout.stalls]
    if boundary:
        all_polys.append(boundary)

    if all_polys:
        xs = [x for p in all_polys for x, _ in p.exterior.coords]
        ys = [y for p in all_polys for _, y in p.exterior.coords]
        wx0, wy0, wx1, wy1 = min(xs), min(ys), max(xs), max(ys)
    else:
        wx0, wy0, wx1, wy1 = 0, 0, 1, 1

    world_w = max(wx1 - wx0, 1e-6)
    world_h = max(wy1 - wy0, 1e-6)
    scale = min(draw_w / world_w, draw_h / world_h)

    def _w2p(x: float, y: float):
        px = draw_origin_x + (x - wx0) * scale
        py = draw_origin_y + (y - wy0) * scale
        return px, py

    # ── background ────────────────────────────────────────────────────────────
    c.setFillColorRGB(0.12, 0.12, 0.12)
    c.rect(0, 0, page_w, page_h, fill=1, stroke=0)

    # ── boundary ──────────────────────────────────────────────────────────────
    if boundary:
        pts = [_w2p(x, y) for x, y in boundary.exterior.coords]
        p = c.beginPath()
        p.moveTo(*pts[0])
        for pt in pts[1:]:
            p.lineTo(*pt)
        p.close()
        c.setFillColorRGB(0.20, 0.20, 0.20)
        c.setStrokeColorRGB(1, 1, 1)
        c.setLineWidth(0.5)
        c.drawPath(p, fill=1, stroke=1)

    # ── stalls ────────────────────────────────────────────────────────────────
    for stall in layout.stalls:
        r, g, b = _TYPE_COLOR.get(stall.type, (0.31, 0.63, 0.31))
        pts = [_w2p(x, y) for x, y in stall.polygon.exterior.coords]
        p = c.beginPath()
        p.moveTo(*pts[0])
        for pt in pts[1:]:
            p.lineTo(*pt)
        p.close()
        c.setFillColorRGB(r, g, b)
        c.setStrokeColorRGB(0.8, 0.8, 0.8)
        c.setLineWidth(0.2)
        c.drawPath(p, fill=1, stroke=1)

    # ── north arrow ───────────────────────────────────────────────────────────
    _draw_north_arrow(c, draw_origin_x + 30 * mm, draw_origin_y + draw_h - 20 * mm, north_angle)

    # ── right panel ───────────────────────────────────────────────────────────
    panel_x = page_w * 0.65
    panel_w = page_w - panel_x - 10 * mm

    # Title
    c.setFillColorRGB(1, 1, 1)
    c.setFont("Helvetica-Bold", 14)
    c.drawString(panel_x, page_h - 20 * mm, title)

    # Metrics
    m = layout.metrics
    c.setFont("Helvetica", 9)
    y_txt = page_h - 32 * mm
    for line in [
        f"Total stalls:   {m.total_stalls}",
        f"m²/stall:       {m.gross_area_per_stall:.1f}",
        f"Site area:      {m.site_area:.0f} m²",
        f"Angle:          {layout.params.angle:.0f}°",
        f"Orientation:    {layout.params.orientation:.0f}°",
    ]:
        c.drawString(panel_x, y_txt, line)
        y_txt -= 5 * mm

    # Stall schedule table
    y_txt -= 6 * mm
    c.setFont("Helvetica-Bold", 9)
    c.drawString(panel_x, y_txt, "Stall schedule")
    y_txt -= 4 * mm

    rows = [["Type", "Count", "%"]]
    for stype, count in sorted(m.by_type.items(), key=lambda kv: -kv[1]):
    # find the enum by value string
        try:
            st = StallType(stype)
            label = _TYPE_LABEL.get(st, stype)
        except ValueError:
            label = stype
        pct = count / m.total_stalls * 100 if m.total_stalls else 0
        rows.append([label, str(count), f"{pct:.0f}%"])

    col_widths = [panel_w * 0.55, panel_w * 0.22, panel_w * 0.23]
    tbl = Table(rows, colWidths=col_widths, rowHeights=5 * mm)
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#444444")),
        ("TEXTCOLOR", (0, 0), (-1, -1), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1),
         [colors.HexColor("#2a2a2a"), colors.HexColor("#333333")]),
        ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#666666")),
    ]))
    tbl_w, tbl_h = tbl.wrap(panel_w, 200 * mm)
    tbl.drawOn(c, panel_x, y_txt - tbl_h)

    # Legend colour swatches
    y_leg = y_txt - tbl_h - 10 * mm
    c.setFont("Helvetica-Bold", 9)
    c.drawString(panel_x, y_leg, "Legend")
    y_leg -= 5 * mm
    c.setFont("Helvetica", 8)
    for st, (r, g, b) in _TYPE_COLOR.items():
        c.setFillColorRGB(r, g, b)
        c.rect(panel_x, y_leg, 8 * mm, 4 * mm, fill=1, stroke=0)
        c.setFillColorRGB(1, 1, 1)
        c.drawString(panel_x + 10 * mm, y_leg + 0.5 * mm, _TYPE_LABEL[st])
        y_leg -= 5.5 * mm

    # Footer
    c.setFont("Helvetica", 7)
    c.setFillColorRGB(0.5, 0.5, 0.5)
    c.drawString(draw_margin, 6 * mm,
                 "⚠ Illustrative — verify all dimensions and stall counts against local regulations before submission.")

    c.save()


def _draw_north_arrow(c, cx: float, cy: float, angle_deg: float) -> None:
    r = 10 * mm
    theta = math.radians(angle_deg)
    tip_x = cx + r * math.sin(theta)
    tip_y = cy + r * math.cos(theta)
    tail_x = cx - r * 0.5 * math.sin(theta)
    tail_y = cy - r * 0.5 * math.cos(theta)
    c.setStrokeColorRGB(1, 1, 1)
    c.setFillColorRGB(1, 1, 1)
    c.setLineWidth(1)
    c.line(tail_x, tail_y, tip_x, tip_y)
    c.setFont("Helvetica-Bold", 8)
    label_x = tip_x + 3 * mm * math.sin(theta)
    label_y = tip_y + 3 * mm * math.cos(theta) - 2 * mm
    c.drawCentredString(label_x, label_y, "N")
