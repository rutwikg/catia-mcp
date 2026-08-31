"""Generative Shape Design: wireframe and surface geometry.

CATIA's ``HybridShapeFactory`` has grown a large family of near-identical
constructors, and which ones exist depends on the release and the licence.
Related constructors are therefore folded into one tool with a ``mode``
argument, and each mode goes through the adaptive-invocation helper so a
missing overload falls through to the next plausible signature instead of
failing outright.
"""

from __future__ import annotations

import logging
import math
from typing import Annotated, Any

from pydantic import Field

from catia_mcp.core import comutil, errors, refs, result, vectors
from catia_mcp.tools.base import registrar, rename, set_parameter_value
from catia_mcp.tools.sketch import ensure_closed

logger = logging.getLogger("catia_mcp.tools.gsd")

DEFAULT_GEOSET = "Geometrical Set.1"


# ── geometrical-set plumbing ─────────────────────────────────────────────────

def _geoset(session: Any, part: Any, name: str = ""):
    """Return the geometrical set new geometry should land in, creating one if needed."""
    holders = comutil.safe(part, "HybridBodies")
    if holders is None:
        raise errors.UnsupportedCapabilityError(
            "This part exposes no geometrical sets, so surface geometry cannot be stored."
        )
    wanted = name or session.state.last_geoset_name
    if wanted:
        found = comutil.safe_call(holders, "Item", wanted)
        if found is not None:
            session.state.last_geoset_name = comutil.name_of(found)
            return found
        if name:
            raise errors.ElementNotFoundError(
                "No geometrical set called %r. Create one with catia_gsd_create_geoset." % name
            )
    if comutil.com_count(holders) > 0:
        found = holders.Item(1)
        session.state.last_geoset_name = comutil.name_of(found)
        return found
    created = holders.Add()
    session.state.last_geoset_name = comutil.name_of(created)
    return created


def _place(
    session: Any,
    part: Any,
    shape: Any,
    geoset: str,
    name: str,
    kind: str,
) -> dict[str, Any]:
    """Append a hybrid shape to its geometrical set, name it and update it."""
    holder = _geoset(session, part, geoset)
    try:
        holder.AppendHybridShape(shape)
    except Exception as exc:
        logger.debug("AppendHybridShape failed (already parented?): %s", exc)
    try:
        part.InWorkObject = shape
    except Exception:
        pass

    final = rename(shape, name)
    try:
        part.UpdateObject(shape)
    except Exception as exc:
        from catia_mcp.tools.part_design import discard_failed

        removed = discard_failed(session, shape, final or kind)
        raise errors.OperationFailedError(
            "CATIA created %s but could not compute it: %s"
            % (final or kind, errors.com_message(exc)),
            remediation=(
                (
                    "The element has been removed, so the model is back in its previous "
                    "state. "
                    if removed
                    else "The element is still in the tree but in error; delete it with "
                    "catia_delete_element before continuing. "
                )
                + "Check that its inputs still exist, are compatible, and are not "
                "colinear or coincident, then recreate it with corrected references."
            ),
            details={"element": final or kind, "kind": kind, "rolled_back": removed},
        ) from exc

    session.state.note_feature(final)
    session.refresh_view()
    return {
        "created": final,
        "kind": kind,
        "geometrical_set": comutil.name_of(holder),
        "reference_token": "name:%s" % final if final else None,
    }


def _direction(session: Any, part: Any, hsf: Any, spec: Any) -> Any:
    """Build a HybridShapeDirection from a vector [x,y,z] or a reference token."""
    if spec is None:
        raise errors.InvalidArgumentError("A direction is required.")
    if isinstance(spec, (list, tuple)):
        values = [float(v) for v in spec][:3]
        if len(values) != 3:
            raise errors.InvalidArgumentError("A direction vector needs three numbers.")
        _, direction = comutil.try_variants(
            [
                (
                    "AddNewDirectionByCoord",
                    lambda: hsf.AddNewDirectionByCoord(values[0], values[1], values[2]),
                ),
            ],
            what="build a direction from coordinates",
        )
        return direction
    target = refs.resolve(session, str(spec), part=part)
    _, direction = comutil.try_variants(
        [
            ("AddNewDirection", lambda: hsf.AddNewDirection(target.reference)),
            ("identity", lambda: target.reference),
        ],
        what="build a direction from %s" % target.label,
    )
    return direction


def _ref(session: Any, part: Any, token: str) -> Any:
    return refs.resolve(session, token, part=part).reference


def register(mcp: Any, session: Any) -> None:
    tool = registrar(mcp, session)

    # ── geometrical sets ─────────────────────────────────────────────────────

    @tool(
        "catia_gsd_create_geoset",
        "Create a geometrical set to hold surface and wireframe geometry, and make it the "
        "target for subsequent GSD calls. Keeping construction geometry in named sets is "
        "what stops a surface model becoming unnavigable.",
        group="gsd",
    )
    def catia_gsd_create_geoset(
        name: Annotated[str, Field(description="Name for the geometrical set.")] = "",
        parent: Annotated[
            str, Field(description="Parent geometrical set name, for nesting. Optional.")
        ] = "",
    ) -> dict:
        ensure_closed(session)
        part = session.active_part()
        if parent:
            holder = comutil.safe_call(comutil.safe(part, "HybridBodies"), "Item", parent)
            if holder is None:
                raise errors.ElementNotFoundError("No geometrical set called %r." % parent)
            created = holder.HybridBodies.Add()
        else:
            created = part.HybridBodies.Add()
        final = rename(created, name)
        session.state.last_geoset_name = final
        try:
            part.InWorkObject = created
        except Exception:
            pass
        return result.ok(
            {"created": final, "reference_token": "name:%s" % final},
            message="Created geometrical set %r." % final,
        )

    @tool(
        "catia_gsd_set_active_geoset",
        "Choose which geometrical set new GSD geometry goes into.",
        idempotent=True,
        group="gsd",
    )
    def catia_gsd_set_active_geoset(
        name: Annotated[str, Field(description="Geometrical set name.")],
    ) -> dict:
        part = session.active_part()
        holder = _geoset(session, part, name)
        try:
            part.InWorkObject = holder
        except Exception:
            pass
        return result.ok(
            {"active_geometrical_set": comutil.name_of(holder)},
            message="GSD geometry will go into %r." % comutil.name_of(holder),
        )

    @tool(
        "catia_gsd_list_elements",
        "List the contents of the geometrical sets in the active part - points, curves, "
        "surfaces and nested sets - with reference tokens.",
        readonly=True,
        group="gsd",
    )
    def catia_gsd_list_elements(
        geoset: Annotated[
            str, Field(description="Limit to one geometrical set. Empty lists them all.")
        ] = "",
    ) -> dict:
        part = session.active_part()
        holders = comutil.safe(part, "HybridBodies")
        sets = []
        for holder in comutil.com_iter(holders):
            holder_name = comutil.name_of(holder)
            if geoset and holder_name != geoset:
                continue
            elements = [
                {"name": comutil.name_of(e), "reference_token": "name:%s" % comutil.name_of(e)}
                for e in comutil.com_iter(comutil.safe(holder, "HybridShapes"))
            ]
            sketches = [
                comutil.name_of(s)
                for s in comutil.com_iter(comutil.safe(holder, "HybridSketches"))
            ]
            sets.append(
                {
                    "name": holder_name,
                    "element_count": len(elements),
                    "elements": elements,
                    "sketches": sketches,
                    "nested_sets": [
                        comutil.name_of(h)
                        for h in comutil.com_iter(comutil.safe(holder, "HybridBodies"))
                    ],
                }
            )
        return result.ok({"count": len(sets), "geometrical_sets": sets})

    # ── points ───────────────────────────────────────────────────────────────

    @tool(
        "catia_gsd_point",
        "Create a point. Modes: 'coordinates' (x,y,z); 'on_curve' at a ratio or distance "
        "along a curve; 'between' two points at a ratio; 'center' of a circle or sphere; "
        "'on_plane' at plane coordinates.",
        group="gsd",
    )
    def catia_gsd_point(
        mode: Annotated[
            str, Field(description="coordinates | on_curve | between | center | on_plane.")
        ] = "coordinates",
        x: Annotated[float, Field(description="X coordinate, mm (coordinates/on_plane).")] = 0.0,
        y: Annotated[float, Field(description="Y coordinate, mm.")] = 0.0,
        z: Annotated[float, Field(description="Z coordinate, mm.")] = 0.0,
        reference: Annotated[
            str,
            Field(description="Curve, circle or plane reference token, depending on mode."),
        ] = "",
        second_reference: Annotated[
            str, Field(description="Second point reference token, for mode='between'.")
        ] = "",
        ratio: Annotated[
            float, Field(description="Position along the curve or between the points, 0 to 1.")
        ] = 0.5,
        distance: Annotated[
            float | None,
            Field(description="Distance along the curve in mm; overrides ratio when given."),
        ] = None,
        geoset: Annotated[str, Field(description="Target geometrical set.")] = "",
        name: Annotated[str, Field(description="Name for the point.")] = "",
    ) -> dict:
        ensure_closed(session)
        part = session.active_part()
        hsf = session.hybrid_factory()
        key = mode.strip().lower()

        if key == "coordinates":
            builders = [
                ("AddNewPointCoord",
                 lambda: hsf.AddNewPointCoord(float(x), float(y), float(z))),
            ]
        elif key == "on_curve":
            curve = _ref(session, part, _need(reference, "reference", "a curve"))
            if distance is not None:
                builders = [
                    (
                        "AddNewPointOnCurveFromDistance",
                        lambda: hsf.AddNewPointOnCurveFromDistance(
                            curve, None, float(distance), False
                        ),
                    ),
                ]
            else:
                builders = [
                    (
                        "AddNewPointOnCurveFromPercent",
                        lambda: hsf.AddNewPointOnCurveFromPercent(
                            curve, None, float(ratio), False
                        ),
                    ),
                ]
        elif key == "between":
            first = _ref(session, part, _need(reference, "reference", "the first point"))
            second = _ref(
                session, part, _need(second_reference, "second_reference", "the second point")
            )
            builders = [
                (
                    "AddNewPointBetween",
                    lambda: hsf.AddNewPointBetween(first, second, float(ratio), 1),
                ),
            ]
        elif key == "center":
            target = _ref(session, part, _need(reference, "reference", "a circle or sphere"))
            builders = [("AddNewPointCenter", lambda: hsf.AddNewPointCenter(target))]
        elif key == "on_plane":
            plane = _ref(session, part, _need(reference, "reference", "a plane"))
            builders = [
                ("AddNewPointOnPlane(3)",
                 lambda: hsf.AddNewPointOnPlane(plane, float(x), float(y))),
                ("AddNewPointOnPlane(4)",
                 lambda: hsf.AddNewPointOnPlane(plane, None, float(x), float(y))),
            ]
        else:
            raise errors.InvalidArgumentError(
                "mode must be coordinates, on_curve, between, center or on_plane."
            )

        _, point = comutil.try_variants(builders, what="create a point (%s)" % key)
        payload = _place(session, part, point, geoset, name, "point")
        payload["mode"] = key
        if key == "coordinates":
            payload["position"] = {"x": x, "y": y, "z": z}
        return result.ok(payload, message="Created point %r." % payload["created"])

    # ── curves ───────────────────────────────────────────────────────────────

    @tool(
        "catia_gsd_line",
        "Create a line. Modes: 'two_points'; 'point_direction' with a length; 'normal' to "
        "a surface at a point; 'tangent' to a curve at a point.",
        group="gsd",
    )
    def catia_gsd_line(
        mode: Annotated[
            str, Field(description="two_points | point_direction | normal | tangent.")
        ] = "two_points",
        start: Annotated[str, Field(description="Reference token of the start point.")] = "",
        end: Annotated[
            str, Field(description="Reference token of the end point (two_points).")
        ] = "",
        direction: Annotated[
            list[float] | None,
            Field(description="Direction vector [x,y,z] for point_direction mode."),
        ] = None,
        support: Annotated[
            str, Field(description="Surface or curve reference token for normal/tangent.")
        ] = "",
        length: Annotated[float, Field(description="Line length in mm.")] = 50.0,
        start_offset: Annotated[
            float, Field(description="Offset of the line start from the point, mm.")
        ] = 0.0,
        geoset: Annotated[str, Field(description="Target geometrical set.")] = "",
        name: Annotated[str, Field(description="Name for the line.")] = "",
    ) -> dict:
        ensure_closed(session)
        part = session.active_part()
        hsf = session.hybrid_factory()
        key = mode.strip().lower()

        if key == "two_points":
            first = _ref(session, part, _need(start, "start", "the start point"))
            second = _ref(session, part, _need(end, "end", "the end point"))
            builders = [("AddNewLinePtPt", lambda: hsf.AddNewLinePtPt(first, second))]
        elif key == "point_direction":
            origin = _ref(session, part, _need(start, "start", "the origin point"))
            vector = _direction(session, part, hsf, direction or [0.0, 0.0, 1.0])
            builders = [
                (
                    "AddNewLinePtDir",
                    lambda: hsf.AddNewLinePtDir(
                        origin, vector, float(start_offset), float(start_offset + length), False
                    ),
                ),
            ]
        elif key == "normal":
            origin = _ref(session, part, _need(start, "start", "the point"))
            surface = _ref(session, part, _need(support, "support", "the surface"))
            builders = [
                (
                    "AddNewLineNormal",
                    lambda: hsf.AddNewLineNormal(
                        origin, surface, float(start_offset), float(start_offset + length), False
                    ),
                ),
            ]
        elif key == "tangent":
            origin = _ref(session, part, _need(start, "start", "the point"))
            curve = _ref(session, part, _need(support, "support", "the curve"))
            builders = [
                (
                    "AddNewLineTangency",
                    lambda: hsf.AddNewLineTangency(
                        curve, origin, None, False, 0.0, float(length), False
                    ),
                ),
            ]
        else:
            raise errors.InvalidArgumentError(
                "mode must be two_points, point_direction, normal or tangent."
            )

        _, line = comutil.try_variants(builders, what="create a line (%s)" % key)
        payload = _place(session, part, line, geoset, name, "line")
        payload["mode"] = key
        return result.ok(payload, message="Created line %r." % payload["created"])

    @tool(
        "catia_gsd_circle",
        "Create a circle or arc. Modes: 'center_radius' needs a centre point, a support "
        "plane and a radius; 'center_point' passes through a point; 'three_points'.",
        group="gsd",
    )
    def catia_gsd_circle(
        mode: Annotated[
            str, Field(description="center_radius | center_point | three_points.")
        ] = "center_radius",
        center: Annotated[str, Field(description="Centre point reference token.")] = "",
        support: Annotated[
            str, Field(description="Support plane or surface reference token.")
        ] = "xy",
        radius: Annotated[float, Field(description="Radius in mm.", gt=0)] = 25.0,
        through_point: Annotated[
            str, Field(description="Point the circle passes through (center_point mode).")
        ] = "",
        points: Annotated[
            list[str] | None,
            Field(description="Three point reference tokens for three_points mode."),
        ] = None,
        geoset: Annotated[str, Field(description="Target geometrical set.")] = "",
        name: Annotated[str, Field(description="Name for the circle.")] = "",
    ) -> dict:
        ensure_closed(session)
        part = session.active_part()
        hsf = session.hybrid_factory()
        key = mode.strip().lower()
        plane = _ref(session, part, support) if support else None

        if key == "center_radius":
            centre = _ref(session, part, _need(center, "center", "the centre point"))
            builders = [
                (
                    "AddNewCircleCtrRad",
                    lambda: hsf.AddNewCircleCtrRad(centre, plane, False, float(radius)),
                ),
            ]
        elif key == "center_point":
            centre = _ref(session, part, _need(center, "center", "the centre point"))
            edge = _ref(session, part, _need(through_point, "through_point", "a point"))
            builders = [
                ("AddNewCircleCtrPt", lambda: hsf.AddNewCircleCtrPt(centre, edge, plane, False)),
            ]
        elif key == "three_points":
            tokens = list(points or [])
            if len(tokens) != 3:
                raise errors.InvalidArgumentError("three_points mode needs exactly three points.")
            p1, p2, p3 = (_ref(session, part, t) for t in tokens)
            builders = [
                ("AddNewCircle3Points", lambda: hsf.AddNewCircle3Points(p1, p2, p3)),
            ]
        else:
            raise errors.InvalidArgumentError(
                "mode must be center_radius, center_point or three_points."
            )

        _, circle = comutil.try_variants(builders, what="create a circle (%s)" % key)
        payload = _place(session, part, circle, geoset, name, "circle")
        payload["mode"] = key
        return result.ok(payload, message="Created circle %r." % payload["created"])

    @tool(
        "catia_gsd_spline",
        "Create a 3D spline through a list of existing points, given as reference tokens.",
        group="gsd",
    )
    def catia_gsd_spline(
        points: Annotated[
            list[str],
            Field(description="Reference tokens of the points to pass through, in order."),
        ],
        closed: Annotated[bool, Field(description="Close the spline into a loop.")] = False,
        geoset: Annotated[str, Field(description="Target geometrical set.")] = "",
        name: Annotated[str, Field(description="Name for the spline.")] = "",
    ) -> dict:
        ensure_closed(session)
        if len(points) < 2:
            raise errors.InvalidArgumentError("A spline needs at least two points.")
        part = session.active_part()
        hsf = session.hybrid_factory()
        spline = hsf.AddNewSpline()
        warnings: list[str] = []
        for token in points:
            reference = _ref(session, part, token)
            try:
                spline.AddPoint(reference)
            except Exception as exc:
                warnings.append("Could not add %s (%s)." % (token, errors.com_message(exc)))
        if closed:
            try:
                spline.SetClosing(1)
            except Exception:
                warnings.append("Could not close the spline on this release.")
        payload = _place(session, part, spline, geoset, name, "spline")
        payload["point_count"] = len(points)
        return result.ok(payload, message="Created spline %r." % payload["created"],
                         warnings=warnings)

    @tool(
        "catia_gsd_polyline",
        "Create a polyline through a list of existing points, given as reference tokens.",
        group="gsd",
    )
    def catia_gsd_polyline(
        points: Annotated[list[str], Field(description="Reference tokens of the points.")],
        closed: Annotated[bool, Field(description="Close the polyline.")] = False,
        geoset: Annotated[str, Field(description="Target geometrical set.")] = "",
        name: Annotated[str, Field(description="Name for the polyline.")] = "",
    ) -> dict:
        ensure_closed(session)
        if len(points) < 2:
            raise errors.InvalidArgumentError("A polyline needs at least two points.")
        part = session.active_part()
        hsf = session.hybrid_factory()
        polyline = hsf.AddNewPolyline()
        warnings: list[str] = []
        for index, token in enumerate(points, start=1):
            reference = _ref(session, part, token)
            try:
                polyline.InsertElement(reference, index)
            except Exception:
                try:
                    polyline.AddPoint(reference)
                except Exception as exc:
                    warnings.append("Could not add %s (%s)." % (token, exc))
        if closed:
            try:
                polyline.Closure = True
            except Exception:
                warnings.append("Could not close the polyline.")
        payload = _place(session, part, polyline, geoset, name, "polyline")
        return result.ok(payload, message="Created polyline %r." % payload["created"],
                         warnings=warnings)

    @tool(
        "catia_gsd_helix",
        "Create a helix around an axis - the curve to sweep for a spring or a thread.",
        group="gsd",
    )
    def catia_gsd_helix(
        axis: Annotated[str, Field(description="Reference token of the axis line.")],
        start_point: Annotated[str, Field(description="Reference token of the start point.")],
        pitch: Annotated[float, Field(description="Pitch (rise per turn) in mm.", gt=0)] = 10.0,
        height: Annotated[float, Field(description="Total height in mm.", gt=0)] = 50.0,
        clockwise: Annotated[bool, Field(description="Clockwise revolution.")] = True,
        taper_angle: Annotated[
            float, Field(description="Taper angle in degrees, for a conical helix.", ge=0, lt=90)
        ] = 0.0,
        geoset: Annotated[str, Field(description="Target geometrical set.")] = "",
        name: Annotated[str, Field(description="Name for the helix.")] = "",
    ) -> dict:
        ensure_closed(session)
        part = session.active_part()
        hsf = session.hybrid_factory()
        axis_ref = _ref(session, part, axis)
        start_ref = _ref(session, part, start_point)
        _, helix = comutil.try_variants(
            [
                ("AddNewHelix", lambda: hsf.AddNewHelix(axis_ref, start_ref)),
            ],
            what="create a helix",
        )
        warnings: list[str] = []
        for member, value in (("Pitch", pitch), ("Height", height), ("TaperAngle", taper_angle)):
            if not set_parameter_value(helix, member, value):
                warnings.append("Could not set %s." % member)
        try:
            helix.ClockwiseRevolution = bool(clockwise)
        except Exception:
            warnings.append("Could not set the revolution direction.")
        payload = _place(session, part, helix, geoset, name, "helix")
        payload.update({"pitch_mm": pitch, "height_mm": height,
                        "turns": round(height / pitch, 3) if pitch else None})
        return result.ok(payload, message="Created helix %r." % payload["created"],
                         warnings=warnings)

    # ── planes ───────────────────────────────────────────────────────────────

    @tool(
        "catia_gsd_plane",
        "Create a construction plane. Modes: 'offset' from an existing plane; "
        "'three_points'; 'normal_to_curve' at a point; 'equation' from ax+by+cz=d; "
        "'angle' rotated about an axis. Construction planes are what you sketch on when "
        "no face is in the right place.",
        group="gsd",
    )
    def catia_gsd_plane(
        mode: Annotated[
            str,
            Field(description="offset | three_points | normal_to_curve | equation | angle."),
        ] = "offset",
        reference: Annotated[
            str, Field(description="Base plane, curve or axis reference token.")
        ] = "xy",
        offset: Annotated[float, Field(description="Offset distance in mm (offset mode).")] = 25.0,
        reverse: Annotated[bool, Field(description="Offset the other way.")] = False,
        points: Annotated[
            list[str] | None, Field(description="Three point tokens for three_points mode.")
        ] = None,
        point: Annotated[
            str, Field(description="Point token for normal_to_curve mode.")
        ] = "",
        equation: Annotated[
            list[float] | None,
            Field(description="[a, b, c, d] for equation mode, with lengths in mm."),
        ] = None,
        angle: Annotated[float, Field(description="Rotation angle in degrees (angle mode).")] = 45.0,
        geoset: Annotated[str, Field(description="Target geometrical set.")] = "",
        name: Annotated[str, Field(description="Name for the plane.")] = "",
    ) -> dict:
        ensure_closed(session)
        part = session.active_part()
        hsf = session.hybrid_factory()
        key = mode.strip().lower()

        if key == "offset":
            base = _ref(session, part, reference or "xy")
            builders = [
                (
                    "AddNewPlaneOffset",
                    lambda: hsf.AddNewPlaneOffset(base, float(offset), bool(reverse)),
                ),
            ]
        elif key == "three_points":
            tokens = list(points or [])
            if len(tokens) != 3:
                raise errors.InvalidArgumentError("three_points mode needs exactly three points.")
            p1, p2, p3 = (_ref(session, part, t) for t in tokens)
            builders = [("AddNewPlane3Points", lambda: hsf.AddNewPlane3Points(p1, p2, p3))]
        elif key == "normal_to_curve":
            curve = _ref(session, part, _need(reference, "reference", "a curve"))
            anchor = _ref(session, part, _need(point, "point", "a point on the curve"))
            builders = [("AddNewPlaneNormal", lambda: hsf.AddNewPlaneNormal(curve, anchor))]
        elif key == "equation":
            values = [float(v) for v in (equation or [])]
            if len(values) != 4:
                raise errors.InvalidArgumentError("equation mode needs [a, b, c, d].")
            builders = [
                (
                    "AddNewPlaneEquation",
                    lambda: hsf.AddNewPlaneEquation(*values),
                ),
            ]
        elif key == "angle":
            axis = _ref(session, part, _need(reference, "reference", "a rotation axis"))
            base = _ref(session, part, "xy")
            builders = [
                (
                    "AddNewPlaneAngle",
                    lambda: hsf.AddNewPlaneAngle(axis, base, float(angle), False),
                ),
            ]
        else:
            raise errors.InvalidArgumentError(
                "mode must be offset, three_points, normal_to_curve, equation or angle."
            )

        _, plane = comutil.try_variants(builders, what="create a plane (%s)" % key)
        payload = _place(session, part, plane, geoset, name, "plane")
        payload["mode"] = key
        return result.ok(
            payload,
            message="Created plane %r." % payload["created"],
            hint="Sketch on it with catia_create_sketch(support='name:%s')." % payload["created"],
        )

    # ── surfaces ─────────────────────────────────────────────────────────────

    @tool(
        "catia_gsd_extrude",
        "Extrude a profile (a sketch or curve) along a direction to make a surface.",
        group="gsd",
    )
    def catia_gsd_extrude(
        profile: Annotated[str, Field(description="Reference token of the profile.")],
        direction: Annotated[
            list[float] | None, Field(description="Direction vector [x,y,z].")
        ] = None,
        length: Annotated[float, Field(description="Extrusion length in mm.")] = 50.0,
        length_reverse: Annotated[
            float, Field(description="Length in the opposite direction, mm.", ge=0)
        ] = 0.0,
        geoset: Annotated[str, Field(description="Target geometrical set.")] = "",
        name: Annotated[str, Field(description="Name for the surface.")] = "",
    ) -> dict:
        ensure_closed(session)
        part = session.active_part()
        hsf = session.hybrid_factory()
        profile_ref = _ref(session, part, profile)
        vector = _direction(session, part, hsf, direction or [0.0, 0.0, 1.0])
        _, surface = comutil.try_variants(
            [
                (
                    "AddNewExtrude",
                    lambda: hsf.AddNewExtrude(
                        profile_ref, float(length), float(length_reverse), vector
                    ),
                ),
            ],
            what="extrude a profile",
        )
        payload = _place(session, part, surface, geoset, name, "extruded_surface")
        payload["length_mm"] = length
        return result.ok(payload, message="Created extruded surface %r." % payload["created"])

    @tool(
        "catia_gsd_revolve",
        "Revolve a profile around an axis to make a surface of revolution.",
        group="gsd",
    )
    def catia_gsd_revolve(
        profile: Annotated[str, Field(description="Reference token of the profile.")],
        axis: Annotated[str, Field(description="Reference token of the revolution axis.")],
        angle: Annotated[float, Field(description="First angle in degrees.")] = 360.0,
        second_angle: Annotated[float, Field(description="Second angle in degrees.")] = 0.0,
        geoset: Annotated[str, Field(description="Target geometrical set.")] = "",
        name: Annotated[str, Field(description="Name for the surface.")] = "",
    ) -> dict:
        ensure_closed(session)
        part = session.active_part()
        hsf = session.hybrid_factory()
        profile_ref = _ref(session, part, profile)
        axis_ref = _ref(session, part, axis)
        _, surface = comutil.try_variants(
            [
                (
                    "AddNewRevol",
                    lambda: hsf.AddNewRevol(
                        profile_ref, float(angle), float(second_angle), axis_ref
                    ),
                ),
            ],
            what="revolve a profile",
        )
        payload = _place(session, part, surface, geoset, name, "revolved_surface")
        payload["angle_deg"] = angle
        return result.ok(payload, message="Created surface of revolution %r." % payload["created"])

    @tool(
        "catia_gsd_sweep",
        "Sweep a profile along a guide curve to make a surface.",
        group="gsd",
    )
    def catia_gsd_sweep(
        profile: Annotated[str, Field(description="Reference token of the profile to sweep.")],
        guide: Annotated[str, Field(description="Reference token of the guide curve.")],
        geoset: Annotated[str, Field(description="Target geometrical set.")] = "",
        name: Annotated[str, Field(description="Name for the surface.")] = "",
    ) -> dict:
        ensure_closed(session)
        part = session.active_part()
        hsf = session.hybrid_factory()
        profile_ref = _ref(session, part, profile)
        guide_ref = _ref(session, part, guide)
        _, surface = comutil.try_variants(
            [
                (
                    "AddNewSweepExplicit",
                    lambda: hsf.AddNewSweepExplicit(profile_ref, guide_ref),
                ),
                (
                    "AddNewSweepSegment",
                    lambda: hsf.AddNewSweepSegment(profile_ref, guide_ref),
                ),
            ],
            what="sweep a profile",
        )
        payload = _place(session, part, surface, geoset, name, "swept_surface")
        return result.ok(payload, message="Created swept surface %r." % payload["created"])

    @tool(
        "catia_gsd_multi_section",
        "Loft a surface through a series of section curves - CATIA's Multi-Sections "
        "Surface. Optional guide curves control the shape between sections.",
        group="gsd",
    )
    def catia_gsd_multi_section(
        sections: Annotated[
            list[str], Field(description="Reference tokens of the section curves, in order.")
        ],
        guides: Annotated[
            list[str] | None, Field(description="Optional guide curve reference tokens.")
        ] = None,
        geoset: Annotated[str, Field(description="Target geometrical set.")] = "",
        name: Annotated[str, Field(description="Name for the surface.")] = "",
    ) -> dict:
        ensure_closed(session)
        if len(sections) < 2:
            raise errors.InvalidArgumentError("A loft needs at least two sections.")
        part = session.active_part()
        hsf = session.hybrid_factory()
        loft = hsf.AddNewLoft()
        warnings: list[str] = []
        for token in sections:
            reference = _ref(session, part, token)
            try:
                loft.AddSectionToLoft(reference, 1, None)
            except Exception:
                try:
                    loft.AddSectionToLoft(reference)
                except Exception as exc:
                    warnings.append("Could not add section %s (%s)." % (token, exc))
        for token in guides or []:
            reference = _ref(session, part, token)
            try:
                loft.AddGuide(reference)
            except Exception as exc:
                warnings.append("Could not add guide %s (%s)." % (token, exc))
        payload = _place(session, part, loft, geoset, name, "multi_section_surface")
        payload["sections"] = len(sections)
        return result.ok(payload, message="Created a multi-sections surface.", warnings=warnings)

    @tool(
        "catia_gsd_fill",
        "Fill a closed boundary of curves or edges with a surface.",
        group="gsd",
    )
    def catia_gsd_fill(
        boundaries: Annotated[
            list[str], Field(description="Reference tokens forming a closed boundary.")
        ],
        geoset: Annotated[str, Field(description="Target geometrical set.")] = "",
        name: Annotated[str, Field(description="Name for the surface.")] = "",
    ) -> dict:
        ensure_closed(session)
        part = session.active_part()
        hsf = session.hybrid_factory()
        fill = hsf.AddNewFill()
        warnings: list[str] = []
        for token in boundaries:
            reference = _ref(session, part, token)
            try:
                fill.AddBound(reference)
            except Exception as exc:
                warnings.append("Could not add boundary %s (%s)." % (token, exc))
        payload = _place(session, part, fill, geoset, name, "fill_surface")
        return result.ok(payload, message="Created a fill surface.", warnings=warnings)

    @tool(
        "catia_gsd_offset_surface",
        "Create a surface offset from an existing one by a set distance.",
        group="gsd",
    )
    def catia_gsd_offset_surface(
        surface: Annotated[str, Field(description="Reference token of the surface.")],
        offset: Annotated[float, Field(description="Offset distance in mm.")] = 5.0,
        reverse: Annotated[bool, Field(description="Offset the other way.")] = False,
        geoset: Annotated[str, Field(description="Target geometrical set.")] = "",
        name: Annotated[str, Field(description="Name for the surface.")] = "",
    ) -> dict:
        ensure_closed(session)
        part = session.active_part()
        hsf = session.hybrid_factory()
        reference = _ref(session, part, surface)
        orientation = 0 if reverse else 1
        _, result_surface = comutil.try_variants(
            [
                (
                    "AddNewOffset(6)",
                    lambda: hsf.AddNewOffset(
                        reference, orientation, float(offset), False, False, 0.0
                    ),
                ),
                (
                    "AddNewOffset(3)",
                    lambda: hsf.AddNewOffset(reference, float(offset), orientation),
                ),
            ],
            what="offset a surface",
        )
        set_parameter_value(result_surface, "Offset", offset)
        payload = _place(session, part, result_surface, geoset, name, "offset_surface")
        payload["offset_mm"] = offset
        return result.ok(payload, message="Created an offset surface.")

    @tool(
        "catia_gsd_blend",
        "Blend between two curves or edges to create a transition surface.",
        group="gsd",
    )
    def catia_gsd_blend(
        first_curve: Annotated[str, Field(description="First curve reference token.")],
        second_curve: Annotated[str, Field(description="Second curve reference token.")],
        first_support: Annotated[str, Field(description="Optional first support surface.")] = "",
        second_support: Annotated[str, Field(description="Optional second support surface.")] = "",
        geoset: Annotated[str, Field(description="Target geometrical set.")] = "",
        name: Annotated[str, Field(description="Name for the surface.")] = "",
    ) -> dict:
        ensure_closed(session)
        part = session.active_part()
        hsf = session.hybrid_factory()
        c1 = _ref(session, part, first_curve)
        c2 = _ref(session, part, second_curve)
        s1 = _ref(session, part, first_support) if first_support else None
        s2 = _ref(session, part, second_support) if second_support else None
        _, blend = comutil.try_variants(
            [
                ("AddNewBlend(4)", lambda: hsf.AddNewBlend(c1, s1, c2, s2)),
                ("AddNewBlend(2)", lambda: hsf.AddNewBlend(c1, c2)),
            ],
            what="create a blend surface",
        )
        payload = _place(session, part, blend, geoset, name, "blend_surface")
        return result.ok(payload, message="Created a blend surface.")

    # ── operations on existing geometry ──────────────────────────────────────

    @tool(
        "catia_gsd_join",
        "Join several surfaces or curves into a single element. This is what you do before "
        "thickening or closing a set of surfaces into a solid.",
        group="gsd",
    )
    def catia_gsd_join(
        elements: Annotated[
            list[str], Field(description="Reference tokens of the elements to join.")
        ],
        tolerance: Annotated[
            float, Field(description="Merging distance in mm.", gt=0)
        ] = 0.001,
        geoset: Annotated[str, Field(description="Target geometrical set.")] = "",
        name: Annotated[str, Field(description="Name for the result.")] = "",
    ) -> dict:
        ensure_closed(session)
        if len(elements) < 2:
            raise errors.InvalidArgumentError("A join needs at least two elements.")
        part = session.active_part()
        hsf = session.hybrid_factory()
        first = _ref(session, part, elements[0])
        second = _ref(session, part, elements[1])
        join = hsf.AddNewJoin(first, second)
        warnings: list[str] = []
        for token in elements[2:]:
            try:
                join.AddElement(_ref(session, part, token))
            except Exception as exc:
                warnings.append("Could not add %s (%s)." % (token, exc))
        try:
            join.SetMergingDistance(float(tolerance))
        except Exception:
            pass
        payload = _place(session, part, join, geoset, name, "join")
        payload["element_count"] = len(elements)
        return result.ok(payload, message="Joined %d elements." % len(elements),
                         warnings=warnings)

    @tool(
        "catia_gsd_split",
        "Cut a surface or curve with another element, keeping one side.",
        group="gsd",
    )
    def catia_gsd_split(
        element: Annotated[str, Field(description="Reference token of the element to cut.")],
        cutting_element: Annotated[str, Field(description="Reference token of the cutter.")],
        keep_positive_side: Annotated[
            bool, Field(description="Keep the positive side of the cut.")
        ] = True,
        geoset: Annotated[str, Field(description="Target geometrical set.")] = "",
        name: Annotated[str, Field(description="Name for the result.")] = "",
    ) -> dict:
        ensure_closed(session)
        part = session.active_part()
        hsf = session.hybrid_factory()
        target = _ref(session, part, element)
        cutter = _ref(session, part, cutting_element)
        side = 1 if keep_positive_side else 0
        _, split = comutil.try_variants(
            [
                ("AddNewHybridSplit", lambda: hsf.AddNewHybridSplit(target, side)),
                ("AddNewSplit", lambda: hsf.AddNewSplit(target, side)),
            ],
            what="split an element",
        )
        warnings: list[str] = []
        for member in ("Cutting", "CuttingElem"):
            try:
                setattr(split, member, cutter)
                break
            except Exception:
                continue
        else:
            try:
                split.AddCuttingElem(cutter)
            except Exception as exc:
                warnings.append("Could not attach the cutting element (%s)." % exc)

        payload = _place(session, part, split, geoset, name, "split")
        return result.ok(payload, message="Split %s." % element, warnings=warnings)

    @tool(
        "catia_gsd_trim",
        "Trim two surfaces or curves against each other, keeping one side of each.",
        group="gsd",
    )
    def catia_gsd_trim(
        first_element: Annotated[str, Field(description="First element reference token.")],
        second_element: Annotated[str, Field(description="Second element reference token.")],
        keep_first_positive: Annotated[
            bool, Field(description="Keep the positive side of the first element.")
        ] = True,
        keep_second_positive: Annotated[
            bool, Field(description="Keep the positive side of the second element.")
        ] = True,
        geoset: Annotated[str, Field(description="Target geometrical set.")] = "",
        name: Annotated[str, Field(description="Name for the result.")] = "",
    ) -> dict:
        ensure_closed(session)
        part = session.active_part()
        hsf = session.hybrid_factory()
        first = _ref(session, part, first_element)
        second = _ref(session, part, second_element)
        _, trim = comutil.try_variants(
            [
                (
                    "AddNewHybridTrim",
                    lambda: hsf.AddNewHybridTrim(
                        first, 1 if keep_first_positive else 0,
                        second, 1 if keep_second_positive else 0,
                    ),
                ),
                (
                    "AddNewTrim",
                    lambda: hsf.AddNewTrim(
                        first, 1 if keep_first_positive else 0,
                        second, 1 if keep_second_positive else 0,
                    ),
                ),
            ],
            what="trim two elements",
        )
        payload = _place(session, part, trim, geoset, name, "trim")
        return result.ok(payload, message="Trimmed two elements.")

    @tool(
        "catia_gsd_intersect",
        "Create the intersection of two elements - a curve where two surfaces meet, or a "
        "point where a curve meets a surface.",
        group="gsd",
    )
    def catia_gsd_intersect(
        first_element: Annotated[str, Field(description="First element reference token.")],
        second_element: Annotated[str, Field(description="Second element reference token.")],
        geoset: Annotated[str, Field(description="Target geometrical set.")] = "",
        name: Annotated[str, Field(description="Name for the result.")] = "",
    ) -> dict:
        ensure_closed(session)
        part = session.active_part()
        hsf = session.hybrid_factory()
        first = _ref(session, part, first_element)
        second = _ref(session, part, second_element)
        _, intersection = comutil.try_variants(
            [("AddNewIntersection", lambda: hsf.AddNewIntersection(first, second))],
            what="intersect two elements",
        )
        payload = _place(session, part, intersection, geoset, name, "intersection")
        return result.ok(payload, message="Created an intersection.")

    @tool(
        "catia_gsd_project",
        "Project a curve or point onto a surface or plane.",
        group="gsd",
    )
    def catia_gsd_project(
        element: Annotated[str, Field(description="Element to project.")],
        support: Annotated[str, Field(description="Surface or plane to project onto.")],
        normal: Annotated[
            bool, Field(description="Project normal to the support rather than along a direction.")
        ] = True,
        direction: Annotated[
            list[float] | None, Field(description="Projection direction [x,y,z] when normal=false.")
        ] = None,
        geoset: Annotated[str, Field(description="Target geometrical set.")] = "",
        name: Annotated[str, Field(description="Name for the result.")] = "",
    ) -> dict:
        ensure_closed(session)
        part = session.active_part()
        hsf = session.hybrid_factory()
        target = _ref(session, part, element)
        onto = _ref(session, part, support)
        _, projection = comutil.try_variants(
            [("AddNewProject", lambda: hsf.AddNewProject(target, onto))],
            what="project an element",
        )
        warnings: list[str] = []
        if not normal:
            try:
                projection.Normal = False
                projection.Direction = _direction(
                    session, part, hsf, direction or [0.0, 0.0, 1.0]
                )
            except Exception as exc:
                warnings.append("Could not set a projection direction (%s)." % exc)
        payload = _place(session, part, projection, geoset, name, "projection")
        return result.ok(payload, message="Created a projection.", warnings=warnings)

    @tool(
        "catia_gsd_extract",
        "Extract a face, edge or set of connected elements from existing geometry into an "
        "independent surface or curve. This is the clean way to reuse solid topology in "
        "surface work.",
        group="gsd",
    )
    def catia_gsd_extract(
        element: Annotated[
            str, Field(description="Reference token of the face or edge to extract.")
        ],
        propagate: Annotated[
            bool, Field(description="Propagate across tangent-continuous neighbours.")
        ] = False,
        geoset: Annotated[str, Field(description="Target geometrical set.")] = "",
        name: Annotated[str, Field(description="Name for the result.")] = "",
    ) -> dict:
        ensure_closed(session)
        part = session.active_part()
        hsf = session.hybrid_factory()
        target = _ref(session, part, element)
        _, extract = comutil.try_variants(
            [("AddNewExtract", lambda: hsf.AddNewExtract(target))],
            what="extract geometry",
        )
        warnings: list[str] = []
        try:
            extract.PropagationType = 1 if propagate else 0
        except Exception:
            warnings.append("Could not set the propagation type.")
        payload = _place(session, part, extract, geoset, name, "extract")
        return result.ok(payload, message="Extracted %s." % element, warnings=warnings)

    @tool(
        "catia_gsd_healing",
        "Heal small gaps between surfaces so they can be joined or closed into a solid.",
        group="gsd",
    )
    def catia_gsd_healing(
        elements: Annotated[list[str], Field(description="Reference tokens to heal.")],
        merging_distance: Annotated[
            float, Field(description="Largest gap to close, mm.", gt=0)
        ] = 0.1,
        geoset: Annotated[str, Field(description="Target geometrical set.")] = "",
        name: Annotated[str, Field(description="Name for the result.")] = "",
    ) -> dict:
        ensure_closed(session)
        part = session.active_part()
        hsf = session.hybrid_factory()
        healing = hsf.AddNewHealing()
        warnings: list[str] = []
        for token in elements:
            try:
                healing.AddElementToHeal(_ref(session, part, token))
            except Exception as exc:
                warnings.append("Could not add %s (%s)." % (token, exc))
        set_parameter_value(healing, "MergingDistance", merging_distance)
        payload = _place(session, part, healing, geoset, name, "healing")
        return result.ok(payload, message="Created a healing feature.", warnings=warnings)

    @tool(
        "catia_gsd_transform",
        "Copy geometry with a transformation: translate, rotate, symmetry, scale or "
        "affinity. Unlike catia_transform_body this creates new surface geometry and "
        "leaves the original in place.",
        group="gsd",
    )
    def catia_gsd_transform(
        element: Annotated[str, Field(description="Reference token of the element to copy.")],
        operation: Annotated[
            str, Field(description="translate | rotate | symmetry | scale.")
        ] = "translate",
        reference: Annotated[
            str,
            Field(description="Axis, plane or point reference token, depending on operation."),
        ] = "",
        vector: Annotated[
            list[float] | None, Field(description="Translation vector [x,y,z] in mm.")
        ] = None,
        value: Annotated[
            float, Field(description="Angle in degrees for rotate, ratio for scale.")
        ] = 90.0,
        geoset: Annotated[str, Field(description="Target geometrical set.")] = "",
        name: Annotated[str, Field(description="Name for the result.")] = "",
    ) -> dict:
        ensure_closed(session)
        part = session.active_part()
        hsf = session.hybrid_factory()
        target = _ref(session, part, element)
        key = operation.strip().lower()

        if key == "translate":
            values = [float(v) for v in (vector or [10.0, 0.0, 0.0])][:3]
            magnitude = math.sqrt(sum(v * v for v in values))
            if magnitude < 1e-9:
                raise errors.InvalidArgumentError("The translation vector cannot be zero.")
            direction = _direction(session, part, hsf, values)
            builders = [
                ("AddNewTranslate",
                 lambda: hsf.AddNewTranslate(target, direction, magnitude)),
            ]
        elif key == "rotate":
            axis = _ref(session, part, _need(reference, "reference", "a rotation axis"))
            builders = [
                ("AddNewRotate(3)",
                 lambda: hsf.AddNewRotate(target, axis, float(value))),
                ("AddNewRotate(4)",
                 lambda: hsf.AddNewRotate(target, axis, float(value), False)),
            ]
        elif key == "symmetry":
            mirror = _ref(session, part, _need(reference, "reference", "a plane, line or point"))
            builders = [("AddNewSymmetry", lambda: hsf.AddNewSymmetry(target, mirror))]
        elif key == "scale":
            centre = _ref(session, part, _need(reference, "reference", "a point or plane"))
            builders = [("AddNewScaling", lambda: hsf.AddNewScaling(target, centre, float(value)))]
        else:
            raise errors.InvalidArgumentError(
                "operation must be translate, rotate, symmetry or scale."
            )

        _, transformed = comutil.try_variants(builders, what="apply a GSD %s" % key)
        payload = _place(session, part, transformed, geoset, name, key)
        payload["operation"] = key
        return result.ok(payload, message="Created a %s copy." % key)

    # ── surface to solid ─────────────────────────────────────────────────────

    @tool(
        "catia_gsd_thick_surface",
        "Thicken a surface into a solid of a given wall thickness.",
        group="gsd",
    )
    def catia_gsd_thick_surface(
        surface: Annotated[str, Field(description="Reference token of the surface.")],
        thickness: Annotated[float, Field(description="Thickness in mm.", gt=0)] = 2.0,
        second_thickness: Annotated[
            float, Field(description="Thickness on the other side, mm.", ge=0)
        ] = 0.0,
        reverse: Annotated[bool, Field(description="Thicken the other way.")] = False,
        name: Annotated[str, Field(description="Name for the resulting feature.")] = "",
    ) -> dict:
        ensure_closed(session)
        part = session.active_part()
        factory = session.shape_factory()
        reference = _ref(session, part, surface)
        orientation = 0 if reverse else 1
        _, feature = comutil.try_variants(
            [
                (
                    "AddNewThickSurface(4)",
                    lambda: factory.AddNewThickSurface(
                        reference, orientation, float(thickness), float(second_thickness)
                    ),
                ),
                (
                    "AddNewThickSurface(3)",
                    lambda: factory.AddNewThickSurface(
                        reference, float(thickness), float(second_thickness)
                    ),
                ),
            ],
            what="thicken a surface",
        )
        from catia_mcp.tools.part_design import finish

        payload = finish(session, part, feature, "thick_surface", name)
        payload["thickness_mm"] = thickness
        return result.ok(payload, message="Thickened %s to %.3f mm." % (surface, thickness))

    @tool(
        "catia_gsd_close_surface",
        "Close a watertight surface into a solid. The surface must bound a closed volume, "
        "so join and heal the pieces first if needed.",
        group="gsd",
    )
    def catia_gsd_close_surface(
        surface: Annotated[str, Field(description="Reference token of the closed surface.")],
        name: Annotated[str, Field(description="Name for the resulting feature.")] = "",
    ) -> dict:
        ensure_closed(session)
        part = session.active_part()
        factory = session.shape_factory()
        reference = _ref(session, part, surface)
        _, feature = comutil.try_variants(
            [("AddNewCloseSurface", lambda: factory.AddNewCloseSurface(reference))],
            what="close a surface into a solid",
        )
        from catia_mcp.tools.part_design import finish

        payload = finish(session, part, feature, "close_surface", name)
        return result.ok(
            payload,
            message="Closed %s into a solid." % surface,
            hint=(
                "If the update fails, the surface is not watertight. Join the pieces with "
                "catia_gsd_join and close the gaps with catia_gsd_healing first."
            ),
        )

    @tool(
        "catia_gsd_axis_system",
        "Create an axis system at a point with explicit X and Y directions - useful as a "
        "local reference frame for positioning and measurement.",
        group="gsd",
    )
    def catia_gsd_axis_system(
        origin: Annotated[
            list[float], Field(description="Origin [x,y,z] in part coordinates, mm.")
        ] = [0.0, 0.0, 0.0],
        x_direction: Annotated[
            list[float], Field(description="X axis direction [x,y,z].")
        ] = [1.0, 0.0, 0.0],
        y_direction: Annotated[
            list[float], Field(description="Y axis direction [x,y,z].")
        ] = [0.0, 1.0, 0.0],
        name: Annotated[str, Field(description="Name for the axis system.")] = "",
        set_current: Annotated[
            bool, Field(description="Make it the part's current axis system.")
        ] = False,
    ) -> dict:
        ensure_closed(session)
        # Validate before creating: CATIA stores whatever directions it is given
        # and only rejects them at update time, as a modal dialog.
        problem = vectors.check_direction_pair(
            x_direction,
            y_direction,
            first_label="x_direction",
            second_label="y_direction",
        )
        if problem:
            raise errors.InvalidArgumentError(
                problem,
                remediation=(
                    "Give two directions that are not parallel - the defaults [1,0,0] and "
                    "[0,1,0] are a safe starting point. The Z axis is derived from them."
                ),
            )
        part = session.active_part()
        systems = comutil.safe(part, "AxisSystems")
        if systems is None:
            raise errors.UnsupportedCapabilityError(
                "This part exposes no AxisSystems collection."
            )
        axis_system = systems.Add()
        warnings: list[str] = []
        try:
            axis_system.PutOrigin(comutil.in_doubles(origin[:3]))
            axis_system.PutXAxis(comutil.in_doubles(x_direction[:3]))
            axis_system.PutYAxis(comutil.in_doubles(y_direction[:3]))
        except Exception as exc:
            warnings.append("Could not set the axis directions (%s)." % errors.com_message(exc))
        final = rename(axis_system, name)
        if set_current:
            try:
                axis_system.IsCurrent = True
            except Exception:
                warnings.append("Could not make it the current axis system.")
        session.update_part(part)
        return result.ok(
            {"created": final, "reference_token": "name:%s" % final, "origin": origin},
            message="Created axis system %r." % final,
            warnings=warnings,
        )


def _need(value: str, argument: str, what: str) -> str:
    if not value:
        raise errors.InvalidArgumentError(
            "This mode needs %s: pass %s=<reference token>." % (what, argument)
        )
    return value
