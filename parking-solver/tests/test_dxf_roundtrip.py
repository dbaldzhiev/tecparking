import tempfile
from pathlib import Path

import pytest
from shapely.geometry import box

from parking_solver.core.generator import generate
from parking_solver.core.model import AisleDir, LayoutParams, Site
from parking_solver.core.regulations.engine import load_profile
from parking_solver.io.export_dxf import export
from parking_solver.io.import_dxf import load_doc

_PROFILE = Path(__file__).parent.parent / "parking_solver" / "core" / "regulations" / "profiles" / "generic_eu.yaml"

_STALL_LAYERS = {
    "standard", "compact", "accessible", "accessible_van",
    "ev", "ev_accessible", "motorcycle",
}


@pytest.fixture(scope="module")
def profile():
    return load_profile(_PROFILE)


@pytest.fixture(scope="module")
def layout_and_boundary(profile):
    boundary = box(0, 0, 50, 32)
    site = Site(boundary=boundary, setbacks=0.0)
    params = LayoutParams(orientation=0.0, stall_width=2.5, stall_length=5.0, aisle_dir=AisleDir.TWO_WAY)
    layout = generate(site, profile, params)
    return layout, boundary


def test_dxf_stall_count(layout_and_boundary):
    layout, boundary = layout_and_boundary
    with tempfile.NamedTemporaryFile(suffix=".dxf", delete=False) as f:
        out_path = Path(f.name)
    try:
        export(layout, boundary, out_path)
        doc = load_doc(out_path)
        msp = doc.modelspace()
        stall_count = sum(
            1 for e in msp
            if e.dxftype() == "LWPOLYLINE" and e.dxf.layer in _STALL_LAYERS
        )
        assert stall_count == layout.metrics.total_stalls
    finally:
        out_path.unlink(missing_ok=True)


def test_dxf_layers_present(layout_and_boundary):
    layout, boundary = layout_and_boundary
    with tempfile.NamedTemporaryFile(suffix=".dxf", delete=False) as f:
        out_path = Path(f.name)
    try:
        export(layout, boundary, out_path)
        doc = load_doc(out_path)
        layer_names = {layer.dxf.name for layer in doc.layers}
        assert "BOUNDARY" in layer_names
        assert "AISLE" in layer_names
        assert "standard" in layer_names
    finally:
        out_path.unlink(missing_ok=True)


def test_dxf_boundary_entity(layout_and_boundary):
    layout, boundary = layout_and_boundary
    with tempfile.NamedTemporaryFile(suffix=".dxf", delete=False) as f:
        out_path = Path(f.name)
    try:
        export(layout, boundary, out_path)
        doc = load_doc(out_path)
        msp = doc.modelspace()
        boundary_entities = [
            e for e in msp
            if e.dxftype() == "LWPOLYLINE" and e.dxf.layer == "BOUNDARY"
        ]
        assert len(boundary_entities) == 1
    finally:
        out_path.unlink(missing_ok=True)
