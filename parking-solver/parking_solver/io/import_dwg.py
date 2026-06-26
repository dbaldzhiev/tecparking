"""DWG import — thin wrapper that converts DWG → DXF via an external converter,
then delegates to ``import_dxf``.

Supported converters (tried in order)
--------------------------------------
1. ODA File Converter (``ODAFileConverter``) — free, reliable, closed-source.
   Download: https://www.opendesign.com/guestfiles/oda_file_converter
2. ``dwg2dxf`` from libredwg — fully FOSS, maturity varies by DWG version.

If neither is found on PATH an ``ImportError`` is raised with install instructions.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from parking_solver.io import import_dxf


def _find_converter() -> tuple[str, str] | None:
    """Return (name, executable_path) or None if nothing is on PATH."""
    for name, exe in [
        ("ODAFileConverter", "ODAFileConverter"),
        ("libredwg dwg2dxf", "dwg2dxf"),
    ]:
        found = shutil.which(exe)
        if found:
            return name, found
    return None


def _convert_oda(dwg_path: Path, out_dir: Path, exe: str) -> Path:
    """Run ODA File Converter (CLI mode)."""
    subprocess.run(
        [exe, str(dwg_path.parent), str(out_dir), "ACAD2018", "DXF", "0", "1",
         dwg_path.name],
        check=True,
        capture_output=True,
    )
    candidates = list(out_dir.glob("*.dxf"))
    if not candidates:
        raise RuntimeError("ODAFileConverter produced no DXF output")
    return candidates[0]


def _convert_libredwg(dwg_path: Path, out_dir: Path, exe: str) -> Path:
    out_path = out_dir / (dwg_path.stem + ".dxf")
    subprocess.run(
        [exe, str(dwg_path), str(out_path)],
        check=True,
        capture_output=True,
    )
    if not out_path.exists():
        raise RuntimeError("dwg2dxf produced no output")
    return out_path


def load_doc(path: str | Path):
    """Convert *path* (a .dwg file) to DXF and return an ezdxf document.

    Requires ODAFileConverter or libredwg's dwg2dxf on PATH.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)

    found = _find_converter()
    if found is None:
        raise ImportError(
            "No DWG converter found on PATH.\n"
            "  Option 1: Install ODA File Converter from "
            "https://www.opendesign.com/guestfiles/oda_file_converter\n"
            "  Option 2: Install libredwg and ensure 'dwg2dxf' is on PATH.\n"
            "  Alternatively, convert the file to DXF manually and use 'Open DXF'."
        )

    name, exe = found
    with tempfile.TemporaryDirectory() as tmp:
        out_dir = Path(tmp)
        if "ODA" in name:
            dxf_path = _convert_oda(path, out_dir, exe)
        else:
            dxf_path = _convert_libredwg(path, out_dir, exe)
        return import_dxf.load_doc(dxf_path)


def list_entities(doc) -> list[import_dxf.DXFEntity]:
    """Delegate to import_dxf after conversion."""
    return import_dxf.list_entities(doc)
