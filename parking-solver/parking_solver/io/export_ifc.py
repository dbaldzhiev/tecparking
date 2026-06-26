"""IFC export — writes parking stalls as IfcSpace entities with a custom
property set that carries stall type and locked state.

Uses IFC4 schema via ifcopenshell.  The output is readable by Revit, ArchiCAD,
and any IFC-capable BIM tool.

Note: IfcParkingSpace was introduced in IFC4x3; we use IfcSpace + Pset here
for maximum compatibility.
"""
from __future__ import annotations

import uuid
from pathlib import Path
from typing import Optional

from parking_solver.core.model import Layout, Site, StallType

try:
    import ifcopenshell
    _IFC = True
except ImportError:
    _IFC = False

_IFC_STALL_TYPE = {
    StallType.STANDARD:       "REGULAR",
    StallType.COMPACT:        "COMPACT",
    StallType.ACCESSIBLE:     "HANDICAP",
    StallType.ACCESSIBLE_VAN: "HANDICAP_VAN",
    StallType.EV:             "ELECTRIC_VEHICLE",
    StallType.EV_ACCESSIBLE:  "EV_ACCESSIBLE",
    StallType.MOTORCYCLE:     "MOTORCYCLE",
}


def _guid() -> str:
    """Generate a compressed IFC GUID (22 chars, base64-like)."""
    chars = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz_$"
    raw = uuid.uuid4().bytes
    result = []
    for i in range(0, 16, 3):
        chunk = raw[i:i + 3] + b"\x00"
        n = int.from_bytes(chunk[:3], "big")
        for _ in range(4):
            result.append(chars[n & 0x3F])
            n >>= 6
    return "".join(result[:22])


def export(layout: Layout, site: Optional[Site], path: str | Path) -> None:
    """Write *layout* to an IFC4 file at *path*."""
    if not _IFC:
        raise ImportError("ifcopenshell is required for IFC export.  pip install ifcopenshell")

    path = Path(path)
    f = ifcopenshell.file(schema="IFC4")

    # ── header (ifcopenshell 0.8 API) ─────────────────────────────────────────
    f.header.file_name.name = str(path)
    f.header.file_name.originating_system = "ParkingSolver"
    f.header.file_description.description = "Parking Layout Export"

    # ── minimal project hierarchy ──────────────────────────────────────────────
    person = f.create_entity("IfcPerson")
    org = f.create_entity("IfcOrganization", Name="ParkingSolver")
    pao = f.create_entity("IfcPersonAndOrganization", ThePerson=person, TheOrganization=org)
    app = f.create_entity("IfcApplication",
                          ApplicationDeveloper=org,
                          Version="0.1",
                          ApplicationFullName="ParkingSolver",
                          ApplicationIdentifier="ParkingSolver")
    owner = f.create_entity("IfcOwnerHistory",
                            OwningUser=pao,
                            OwningApplication=app,
                            ChangeAction="NOTDEFINED",
                            CreationDate=0)

    ctx = f.create_entity("IfcGeometricRepresentationContext",
                          ContextType="Model",
                          CoordinateSpaceDimension=2,
                          Precision=1e-5,
                          WorldCoordinateSystem=f.create_entity(
                              "IfcAxis2Placement2D",
                              Location=f.create_entity("IfcCartesianPoint",
                                                       Coordinates=(0.0, 0.0))
                          ))

    units = f.create_entity("IfcUnitAssignment",
                             Units=[f.create_entity("IfcSIUnit",
                                                    UnitType="LENGTHUNIT",
                                                    Name="METRE")])

    project = f.create_entity("IfcProject",
                               GlobalId=_guid(),
                               OwnerHistory=owner,
                               Name="ParkingProject",
                               UnitsInContext=units,
                               RepresentationContexts=[ctx])

    site_ifc = f.create_entity("IfcSite",
                                GlobalId=_guid(),
                                OwnerHistory=owner,
                                Name="Site")

    f.create_entity("IfcRelAggregates",
                    GlobalId=_guid(),
                    OwnerHistory=owner,
                    RelatingObject=project,
                    RelatedObjects=[site_ifc])

    # ── helper: polyline from coords ──────────────────────────────────────────

    def _polyline(coords):
        pts = [f.create_entity("IfcCartesianPoint",
                               Coordinates=(float(x), float(y)))
               for x, y in coords]
        return f.create_entity("IfcPolyline", Points=pts)

    def _footprint_shape(coords):
        poly = _polyline(coords)
        geom_set = f.create_entity("IfcGeometricCurveSet", Elements=[poly])
        shape_rep = f.create_entity("IfcShapeRepresentation",
                                    ContextOfItems=ctx,
                                    RepresentationIdentifier="FootPrint",
                                    RepresentationType="GeometricCurveSet",
                                    Items=[geom_set])
        return f.create_entity("IfcProductDefinitionShape",
                               Representations=[shape_rep])

    # ── site boundary ─────────────────────────────────────────────────────────
    if site:
        bnd_coords = list(site.boundary.exterior.coords)[:-1]
        bnd_space = f.create_entity("IfcSpace",
                                    GlobalId=_guid(),
                                    OwnerHistory=owner,
                                    Name="SiteBoundary",
                                    Representation=_footprint_shape(bnd_coords))
        f.create_entity("IfcRelAggregates",
                        GlobalId=_guid(),
                        OwnerHistory=owner,
                        RelatingObject=site_ifc,
                        RelatedObjects=[bnd_space])

    # ── stall spaces ──────────────────────────────────────────────────────────
    stall_entities = []
    for idx, stall in enumerate(layout.stalls):
        coords = list(stall.polygon.exterior.coords)[:-1]
        space = f.create_entity("IfcSpace",
                                GlobalId=_guid(),
                                OwnerHistory=owner,
                                Name=f"Stall_{idx + 1:04d}",
                                Representation=_footprint_shape(coords))

        # Custom property set: stall type + locked flag
        ptype_prop = f.create_entity("IfcPropertySingleValue",
                                     Name="StallType",
                                     NominalValue=f.create_entity(
                                         "IfcLabel",
                                         wrappedValue=_IFC_STALL_TYPE.get(stall.type, "REGULAR")
                                     ))
        locked_prop = f.create_entity("IfcPropertySingleValue",
                                      Name="Locked",
                                      NominalValue=f.create_entity(
                                          "IfcBoolean",
                                          wrappedValue=stall.locked
                                      ))
        angle_prop = f.create_entity("IfcPropertySingleValue",
                                     Name="StallAngleDeg",
                                     NominalValue=f.create_entity(
                                         "IfcReal",
                                         wrappedValue=float(stall.angle)
                                     ))
        pset = f.create_entity("IfcPropertySet",
                               GlobalId=_guid(),
                               OwnerHistory=owner,
                               Name="Pset_ParkingStall",
                               HasProperties=[ptype_prop, locked_prop, angle_prop])
        f.create_entity("IfcRelDefinesByProperties",
                        GlobalId=_guid(),
                        OwnerHistory=owner,
                        RelatedObjects=[space],
                        RelatingPropertyDefinition=pset)

        stall_entities.append(space)

    if stall_entities:
        f.create_entity("IfcRelContainedInSpatialStructure",
                        GlobalId=_guid(),
                        OwnerHistory=owner,
                        RelatingStructure=site_ifc,
                        RelatedElements=stall_entities)

    f.write(str(path))
