"""Phase 4 IO tests — PDF calibration, IFC export, PDF doc export, DWG wrapper."""
from __future__ import annotations

import math
import pathlib
import tempfile

import pytest
from shapely.geometry import box

from parking_solver.core.model import AisleDir, LayoutParams, LayoutType, Site
from parking_solver.core import generator
from parking_solver.core.ada_placement import place_special_stalls
from parking_solver.core.regulations.engine import load_profile
from parking_solver.io.import_pdf import PDFTransform, calibrate

_PROFILE_PATH = (
    pathlib.Path(__file__).parent.parent
    / "parking_solver" / "core" / "regulations" / "profiles" / "generic_eu.yaml"
)


@pytest.fixture
def profile():
    return load_profile(_PROFILE_PATH)


@pytest.fixture
def site():
    return Site(boundary=box(0, 0, 50, 32))


@pytest.fixture
def layout(site, profile):
    params = LayoutParams(angle=90.0, aisle_dir=AisleDir.TWO_WAY)
    raw = generator.generate(site, profile, params)
    return place_special_stalls(raw, site, profile)


# ── PDF calibration ───────────────────────────────────────────────────────────

def test_calibrate_returns_transform():
    tf = calibrate((0, 0), (100, 0), 10.0)
    assert isinstance(tf, PDFTransform)
    assert abs(tf.scale - 0.1) < 1e-9


def test_calibrate_scale_correct():
    tf = calibrate((0, 0), (200, 0), 50.0)
    assert abs(tf.scale - 0.25) < 1e-9


def test_calibrate_to_world_horizontal():
    tf = calibrate((0, 0), (100, 0), 10.0)
    x, y = tf.to_world(100, 0)
    assert abs(x - 10.0) < 1e-9
    assert abs(y) < 1e-9


def test_calibrate_to_world_y_flipped():
    tf = calibrate((0, 0), (0, 100), 10.0)
    x, y = tf.to_world(0, 100)
    assert abs(x) < 1e-9
    assert abs(y + 10.0) < 1e-6   # Y is flipped: pixel-down → world-up negative


def test_calibrate_diagonal():
    # 3-4-5 triangle in pixels → real world distance 5 m
    tf = calibrate((0, 0), (3, 4), 5.0)
    assert abs(tf.scale - 1.0) < 1e-9


def test_calibrate_zero_distance_raises():
    with pytest.raises(ValueError, match="positive"):
        calibrate((0, 0), (0, 0), 0.0)


def test_calibrate_points_same_raises():
    with pytest.raises(ValueError, match="close"):
        calibrate((50, 50), (50, 50), 10.0)


# ── PDF export ────────────────────────────────────────────────────────────────

def test_export_pdf_creates_file(layout, site):
    from parking_solver.io import export_pdf
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
        export_pdf.export(layout, site, tf.name)
        size = pathlib.Path(tf.name).stat().st_size
    assert size > 1000   # non-trivial PDF


def test_export_pdf_no_site(layout):
    from parking_solver.io import export_pdf
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
        export_pdf.export(layout, None, tf.name)
        size = pathlib.Path(tf.name).stat().st_size
    assert size > 1000


# ── IFC export ────────────────────────────────────────────────────────────────

def test_export_ifc_creates_file(layout, site):
    from parking_solver.io import export_ifc
    with tempfile.NamedTemporaryFile(suffix=".ifc", delete=False) as tf:
        export_ifc.export(layout, site, tf.name)
        content = pathlib.Path(tf.name).read_text(encoding="utf-8")
    assert "IFC4" in content
    assert "IFCSPACE(" in content
    assert "Pset_ParkingStall" in content


def test_export_ifc_stall_count(layout, site):
    from parking_solver.io import export_ifc
    with tempfile.NamedTemporaryFile(suffix=".ifc", delete=False) as tf:
        export_ifc.export(layout, site, tf.name)
        content = pathlib.Path(tf.name).read_text(encoding="utf-8")
    # Each stall + 1 site boundary = total_stalls + 1 IfcSpace lines
    count = content.count("IFCSPACE(")
    assert count == layout.metrics.total_stalls + 1


def test_export_ifc_no_site(layout):
    from parking_solver.io import export_ifc
    with tempfile.NamedTemporaryFile(suffix=".ifc", delete=False) as tf:
        export_ifc.export(layout, None, tf.name)
        assert pathlib.Path(tf.name).stat().st_size > 100


# ── DWG wrapper (no converter → ImportError) ─────────────────────────────────

def test_import_dwg_no_converter_raises(tmp_path):
    from parking_solver.io import import_dwg
    fake_dwg = tmp_path / "site.dwg"
    fake_dwg.write_bytes(b"\x00" * 100)

    # ODAFileConverter and dwg2dxf are not on PATH in the test environment
    import shutil
    if shutil.which("ODAFileConverter") or shutil.which("dwg2dxf"):
        pytest.skip("A DWG converter is installed — skipping 'no converter' test")

    with pytest.raises(ImportError, match="converter"):
        import_dwg.load_doc(fake_dwg)


def test_import_dwg_missing_file_raises():
    from parking_solver.io import import_dwg
    with pytest.raises(FileNotFoundError):
        import_dwg.load_doc("/nonexistent/file.dwg")
