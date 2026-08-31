"""Measurement, mass properties and materials.

The measurement tools are where the by-reference array problem described in
``core.comutil`` bites hardest: ``GetCOG``, ``GetInertia`` and
``GetBoundingBox`` are all declared to take an array CATIA writes into, and a
client that passes a plain Python list gets zeros back and reports them as
fact. Everything here goes through ``comutil.out_doubles``, which uses a
``VT_BYREF`` VARIANT and cross-checks suspicious all-zero answers against a
script evaluated inside CATIA.
"""

from __future__ import annotations

import logging
import os
from typing import Annotated, Any

from pydantic import Field

from catia_mcp.core import comutil, errors, refs, result
from catia_mcp.tools.base import registrar

logger = logging.getLogger("catia_mcp.tools.measure")

# CatMeasurableName, as reported by Measurable.GeometryName.
GEOMETRY_NAMES = {
    1: "point",
    2: "line",
    3: "plane",
    4: "circle",
    5: "cylinder",
    6: "sphere",
    7: "cone",
    8: "torus",
    9: "surface",
    10: "curve",
    11: "solid",
    12: "volume",
}


def _measurable_for(session: Any, token: str) -> tuple[Any, Any]:
    resolved = refs.resolve(session, token)
    reference = resolved.reference
    if reference is None:
        raise errors.BadReferenceError("Could not build a measurable reference for %r." % token)
    return resolved, session.measurable(reference)


def _describe(session: Any, measurable: Any) -> dict[str, Any]:
    app = session.app
    out: dict[str, Any] = {}
    code = comutil.safe(measurable, "GeometryName")
    if code is not None:
        try:
            out["geometry"] = GEOMETRY_NAMES.get(int(code), "type_%d" % int(code))
        except Exception:
            pass
    for label, member, digits in (
        ("length_mm", "Length", 4),
        ("area_mm2", "Area", 4),
        ("volume_mm3", "Volume", 4),
        ("radius_mm", "Radius", 4),
        ("diameter_mm", "Diameter", 4),
        ("angle_deg", "Angle", 4),
    ):
        value = comutil.safe(measurable, member)
        if value is not None:
            try:
                out[label] = round(float(value), digits)
            except Exception:
                pass
    try:
        out["centroid_mm"] = result.round_xyz(
            comutil.out_doubles(measurable, "GetCOG", 3, app=app)
        )
    except Exception:
        pass
    return out


def register(mcp: Any, session: Any) -> None:
    tool = registrar(mcp, session)

    @tool(
        "catia_measure",
        "Measure a single element: its type, and whichever of length, area, volume, "
        "radius and centre of gravity apply. Works on faces, edges, vertices, sketches, "
        "surfaces, bodies and whole components.",
        readonly=True,
        group="measure",
    )
    def catia_measure(
        element: Annotated[
            str,
            Field(
                description=(
                    "Reference token: 'face#2', 'edge@10,0,0', 'body', 'name:Pad.1', and so on."
                )
            ),
        ],
    ) -> dict:
        resolved, measurable = _measurable_for(session, element)
        payload = _describe(session, measurable)
        payload["element"] = resolved.label
        if not payload:
            raise errors.OperationFailedError(
                "CATIA returned no measurements for %s." % resolved.label,
                remediation="The element may be hidden or not a measurable geometry type.",
            )
        return result.ok(payload)

    @tool(
        "catia_measure_distance",
        "Measure the shortest distance between two elements, and report the two points "
        "where that minimum occurs. A distance of zero means the elements touch or "
        "interfere - which makes this a quick clearance check between two components in "
        "an assembly.",
        readonly=True,
        group="measure",
    )
    def catia_measure_distance(
        first: Annotated[str, Field(description="First element reference token.")],
        second: Annotated[str, Field(description="Second element reference token.")],
    ) -> dict:
        first_resolved, measurable = _measurable_for(session, first)
        second_resolved = refs.resolve(session, second)
        if second_resolved.reference is None:
            raise errors.BadReferenceError("Could not build a reference for %r." % second)

        try:
            distance = float(measurable.GetMinimumDistance(second_resolved.reference))
        except Exception as exc:
            raise errors.OperationFailedError(
                "CATIA could not measure between %s and %s: %s"
                % (first_resolved.label, second_resolved.label, errors.com_message(exc))
            ) from exc

        payload: dict[str, Any] = {
            "first": first_resolved.label,
            "second": second_resolved.label,
            "minimum_distance_mm": round(distance, 4),
            "touching_or_interfering": distance <= 1e-6,
        }
        try:
            points = comutil.out_doubles(
                measurable,
                "GetMinimumDistancePoints",
                6,
                app=session.app,
                args=(second_resolved.reference,),
            )
            payload["closest_points"] = {
                "on_first": result.round_xyz(points[0:3]),
                "on_second": result.round_xyz(points[3:6]),
            }
        except Exception:
            pass
        return result.ok(payload)

    @tool(
        "catia_measure_angle",
        "Measure the angle between two elements - two planar faces, two lines, or a line "
        "and a plane.",
        readonly=True,
        group="measure",
    )
    def catia_measure_angle(
        first: Annotated[str, Field(description="First element reference token.")],
        second: Annotated[str, Field(description="Second element reference token.")],
    ) -> dict:
        first_resolved, measurable = _measurable_for(session, first)
        second_resolved = refs.resolve(session, second)
        try:
            angle = float(measurable.GetAngleBetween(second_resolved.reference))
        except Exception as exc:
            raise errors.OperationFailedError(
                "CATIA could not measure the angle: %s" % errors.com_message(exc),
                remediation="Angles need directional geometry - planar faces, lines or axes.",
            ) from exc
        return result.ok(
            {
                "first": first_resolved.label,
                "second": second_resolved.label,
                "angle_deg": round(angle, 4),
            }
        )

    @tool(
        "catia_bounding_box",
        "Report the axis-aligned bounding box of an element or of the whole solid, with "
        "the minimum and maximum corners and the overall dimensions. Useful for sizing "
        "stock material or checking a part fits an envelope.",
        readonly=True,
        group="measure",
    )
    def catia_bounding_box(
        element: Annotated[
            str, Field(description="Reference token. Defaults to the part's main body.")
        ] = "body",
    ) -> dict:
        resolved, measurable = _measurable_for(session, element)
        values = comutil.out_doubles(measurable, "GetBoundingBox", 6, app=session.app)
        low = values[0:3]
        high = values[3:6]
        return result.ok(
            {
                "element": resolved.label,
                "min_mm": result.round_xyz(low),
                "max_mm": result.round_xyz(high),
                "dimensions_mm": {
                    "x": round(high[0] - low[0], 4),
                    "y": round(high[1] - low[1], 4),
                    "z": round(high[2] - low[2], 4),
                },
            }
        )

    @tool(
        "catia_mass_properties",
        "Report volume, surface area, mass, centre of gravity and the inertia matrix. "
        "When the document has a material applied, CATIA's own Analyze interface supplies "
        "the mass; otherwise pass a density and it is computed from the measured volume.",
        readonly=True,
        group="measure",
    )
    def catia_mass_properties(
        element: Annotated[
            str,
            Field(
                description=(
                    "Reference token to measure. Defaults to the whole document, which is "
                    "what you want for a part or an assembly."
                )
            ),
        ] = "",
        density: Annotated[
            float | None,
            Field(
                description=(
                    "Density in kg/m3, used only when no material is applied. Steel is "
                    "about 7850, aluminium about 2700."
                )
            ),
        ] = None,
    ) -> dict:
        doc = session.active_document()
        payload: dict[str, Any] = {"document": comutil.name_of(doc)}
        warnings: list[str] = []
        source = None

        if not element:
            analyze = comutil.safe(comutil.safe(doc, "Product"), "Analyze")
            if analyze is not None:
                source = "Product.Analyze"
                for label, member, factor in (
                    ("mass_kg", "Mass", 1.0),
                    ("volume_mm3", "Volume", 1.0),
                    ("wet_area_mm2", "WetArea", 1.0),
                ):
                    value = comutil.safe(analyze, member)
                    if value is not None:
                        try:
                            payload[label] = round(float(value) * factor, 6)
                        except Exception:
                            pass
                try:
                    payload["center_of_gravity_mm"] = result.round_xyz(
                        comutil.out_doubles(analyze, "GetGravityCenter", 3, app=session.app)
                    )
                except Exception:
                    warnings.append("Could not read the centre of gravity from Analyze.")
                try:
                    inertia = comutil.out_doubles(analyze, "GetInertia", 9, app=session.app)
                    payload["inertia_matrix_kg_mm2"] = [
                        [round(v, 6) for v in inertia[0:3]],
                        [round(v, 6) for v in inertia[3:6]],
                        [round(v, 6) for v in inertia[6:9]],
                    ]
                except Exception:
                    warnings.append("Could not read the inertia matrix from Analyze.")

        if source is None:
            token = element or "body"
            resolved, measurable = _measurable_for(session, token)
            source = "SPAWorkbench measurement of %s" % resolved.label
            payload["element"] = resolved.label
            payload.update(_describe(session, measurable))
            try:
                inertia = comutil.out_doubles(measurable, "GetInertia", 9, app=session.app)
                payload["inertia_matrix"] = [
                    [round(v, 6) for v in inertia[0:3]],
                    [round(v, 6) for v in inertia[3:6]],
                    [round(v, 6) for v in inertia[6:9]],
                ]
            except Exception:
                pass

        volume = payload.get("volume_mm3")
        if density and volume:
            mass = float(density) * float(volume) * 1e-9
            payload["mass_kg"] = round(mass, 6)
            payload["density_kg_m3"] = float(density)
            payload["mass_source"] = "computed from the measured volume and the given density"
        elif "mass_kg" in payload:
            payload["mass_source"] = "CATIA, using the material applied to the document"
        elif volume:
            warnings.append(
                "No material is applied and no density was given, so mass is unknown. "
                "Apply a material with catia_apply_material or pass density."
            )

        payload["source"] = source
        if volume:
            payload["volume_cm3"] = round(float(volume) / 1000.0, 4)
        return result.ok(payload, warnings=warnings)

    @tool(
        "catia_point_coordinates",
        "Read the 3D coordinates of a point, vertex or the centre of a circle.",
        readonly=True,
        group="measure",
    )
    def catia_point_coordinates(
        element: Annotated[
            str, Field(description="Reference token, e.g. 'vertex#4' or 'name:Point.1'.")
        ],
    ) -> dict:
        resolved, measurable = _measurable_for(session, element)
        for method in ("GetPoint", "GetCenter", "GetCOG"):
            try:
                values = comutil.out_doubles(measurable, method, 3, app=session.app)
                return result.ok(
                    {
                        "element": resolved.label,
                        "method": method,
                        "position_mm": result.round_xyz(values),
                    }
                )
            except Exception:
                continue
        raise errors.OperationFailedError(
            "CATIA reported no position for %s." % resolved.label,
            remediation="Only points, vertices and circle centres have a single position.",
        )

    @tool(
        "catia_face_plane",
        "For a planar face, report its plane: a point on it, its two in-plane directions "
        "and the outward normal. This is what you need to decide which face is 'the top "
        "one' before sketching on it.",
        readonly=True,
        group="measure",
    )
    def catia_face_plane(
        face: Annotated[str, Field(description="Reference token of the face.")],
    ) -> dict:
        resolved, measurable = _measurable_for(session, face)
        values = comutil.out_doubles(measurable, "GetPlane", 9, app=session.app)
        origin = values[0:3]
        first = values[3:6]
        second = values[6:9]
        normal = refs.cross(first, second)
        return result.ok(
            {
                "face": resolved.label,
                "origin_mm": result.round_xyz(origin),
                "direction_1": result.round_xyz(first),
                "direction_2": result.round_xyz(second),
                "normal": result.round_xyz(normal),
                "area_mm2": comutil.safe(measurable, "Area"),
            },
            hint="Sketch on it with catia_create_sketch(support='%s')." % face,
        )

    # ── materials ────────────────────────────────────────────────────────────

    @tool(
        "catia_list_materials",
        "List the materials available in a CATIA material catalogue. With no path, the "
        "standard catalogue shipped with the detected CATIA installation is used.",
        readonly=True,
        group="measure",
    )
    def catia_list_materials(
        catalog_path: Annotated[
            str, Field(description="Path to a .CATMaterial catalogue. Empty uses the default.")
        ] = "",
        family: Annotated[
            str, Field(description="Limit to one material family, e.g. 'Metal'.")
        ] = "",
    ) -> dict:
        path = catalog_path or _default_catalog(session)
        if not path or not os.path.exists(path):
            raise errors.ElementNotFoundError(
                "No material catalogue found%s." % (" at %s" % path if path else ""),
                remediation=(
                    "Pass catalog_path explicitly. The standard file is normally at "
                    "<CATIA install>\\startup\\materials\\Catalog.CATMaterial."
                ),
            )
        document = session.documents.Open(path)
        families = []
        try:
            for fam in comutil.com_iter(comutil.safe(document, "Families")):
                fam_name = comutil.name_of(fam)
                if family and fam_name.lower() != family.lower():
                    continue
                families.append(
                    {
                        "family": fam_name,
                        "materials": [
                            comutil.name_of(m)
                            for m in comutil.com_iter(comutil.safe(fam, "Materials"))
                        ],
                    }
                )
        finally:
            comutil.safe_call(document, "Close")
        return result.ok(
            {"catalog": path, "family_count": len(families), "families": families}
        )

    @tool(
        "catia_apply_material",
        "Apply a material from a CATIA catalogue to the active part or a named body. Once "
        "a material is applied, catia_mass_properties reports a real mass instead of "
        "needing a density.",
        group="measure",
    )
    def catia_apply_material(
        material: Annotated[str, Field(description="Material name, e.g. 'Steel' or 'Aluminium'.")],
        catalog_path: Annotated[
            str, Field(description="Path to the .CATMaterial catalogue. Empty uses the default.")
        ] = "",
        family: Annotated[
            str, Field(description="Family to look in. Empty searches every family.")
        ] = "",
        body: Annotated[
            str, Field(description="Body name to apply to. Empty applies to the whole part.")
        ] = "",
    ) -> dict:
        path = catalog_path or _default_catalog(session)
        if not path or not os.path.exists(path):
            raise errors.ElementNotFoundError(
                "No material catalogue found%s." % (" at %s" % path if path else "")
            )
        part = session.active_part()
        catalog = session.documents.Open(path)
        try:
            found = None
            for fam in comutil.com_iter(comutil.safe(catalog, "Families")):
                if family and comutil.name_of(fam).lower() != family.lower():
                    continue
                candidate = comutil.safe_call(comutil.safe(fam, "Materials"), "Item", material)
                if candidate is not None:
                    found = candidate
                    break
            if found is None:
                raise errors.ElementNotFoundError(
                    "No material called %r in %s. Use catia_list_materials to see what is "
                    "available." % (material, path)
                )

            target = (
                comutil.safe_call(comutil.safe(part, "Bodies"), "Item", body) if body else part
            )
            if target is None:
                raise errors.ElementNotFoundError("No body called %r." % body)

            manager = comutil.safe_call(part, "GetItem", "CATMatManagerVBExt")
            if manager is None:
                raise errors.UnsupportedCapabilityError(
                    "The material manager (CATMatManagerVBExt) is not available in this "
                    "CATIA session, so materials cannot be applied through automation."
                )
            _, _ = comutil.try_variants(
                [
                    ("ApplyMaterialOnPart", lambda: manager.ApplyMaterialOnPart(found, 1)),
                    ("ApplyMaterialOnBody", lambda: manager.ApplyMaterialOnBody(target, found, 1)),
                    ("ApplyMaterial", lambda: manager.ApplyMaterial(target, found, 1)),
                ],
                what="apply the material",
            )
        finally:
            comutil.safe_call(catalog, "Close")

        session.update_part(part)
        return result.ok(
            {"material": material, "applied_to": body or comutil.name_of(part), "catalog": path},
            message="Applied %s." % material,
            hint="catia_mass_properties will now report the real mass.",
        )


def _default_catalog(session: Any) -> str:
    install = session.version.install_path
    candidates = []
    if install:
        candidates.append(os.path.join(install, "startup", "materials", "Catalog.CATMaterial"))
        candidates.append(
            os.path.join(os.path.dirname(install), "startup", "materials", "Catalog.CATMaterial")
        )
    for candidate in candidates:
        if os.path.exists(candidate):
            return candidate
    return candidates[0] if candidates else ""
