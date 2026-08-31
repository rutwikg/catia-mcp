"""Part Design: turning sketches into solid material.

Every tool here closes any sketch that is still open for edition first, then
creates the feature, then asks CATIA to update just that feature so a failure
is reported against the right thing rather than surfacing three calls later.
"""

from __future__ import annotations

import logging
import os
from typing import Annotated, Any

from pydantic import Field

from catia_mcp.core import comutil, constants, errors, refs, result
from catia_mcp.tools.base import registrar, rename, set_parameter_value
from catia_mcp.tools.sketch import ensure_closed

logger = logging.getLogger("catia_mcp.tools.part_design")

LIMIT_MODES = {
    "dimension": "catOffsetLimit",
    "up_to_next": "catUpToNextLimit",
    "up_to_last": "catUpToLastLimit",
    "up_to_plane": "catUpToPlaneLimit",
    "up_to_surface": "catUpToSurfaceLimit",
}


def discard_failed(session: Any, feature: Any, name: str) -> bool:
    """Remove a feature whose update failed, restoring the previous good state.

    A feature left in error keeps failing every subsequent ``Update()``, and
    CATIA raises each of those as a modal dialog that blocks all further
    automation - so one bad call otherwise wedges the whole session. Set
    CATIA_MCP_KEEP_FAILED_FEATURES=1 to keep them for debugging instead.
    """
    if os.environ.get("CATIA_MCP_KEEP_FAILED_FEATURES", "").strip() not in ("", "0", "false"):
        return False
    try:
        selection = session.selection()
        selection.Clear()
        selection.Add(feature)
        selection.Delete()
        selection.Clear()
        return True
    except Exception as exc:
        logger.info("Could not remove the failed feature %r: %s", name, exc)
        return False


def finish(session: Any, part: Any, feature: Any, kind: str, name: str = "") -> dict[str, Any]:
    """Name, update and summarise a freshly created feature."""
    final_name = rename(feature, name)
    try:
        part.UpdateObject(feature)
    except Exception as exc:
        removed = discard_failed(session, feature, final_name or kind)
        raise errors.OperationFailedError(
            "CATIA created %s but could not compute it: %s"
            % (final_name or kind, errors.com_message(exc)),
            remediation=(
                (
                    "The feature has been removed, so the model is back in its previous "
                    "state and safe to keep working in. "
                    if removed
                    else "The feature is still in the tree but in error; remove it with "
                    "catia_delete_element before continuing. "
                )
                + "Common causes: an open or self-intersecting profile, a value larger "
                "than the surrounding geometry allows, or a limit surface the extrusion "
                "never reaches."
            ),
            details={"feature": final_name, "kind": kind, "rolled_back": removed},
        ) from exc
    session.state.note_feature(final_name)
    session.refresh_view()
    return {
        "created": final_name,
        "kind": kind,
        "reference_token": "name:%s" % final_name if final_name else None,
    }


def _resolve_sketch(session: Any, name: str) -> Any:
    """Find the profile sketch for a solid feature."""
    closed = ensure_closed(session)
    wanted = name or closed or session.state.last_sketch_name
    if not wanted:
        raise errors.ElementNotFoundError(
            "No profile sketch available.",
            remediation=(
                "Create one with catia_create_sketch and draw a closed profile, or pass "
                "sketch='<name>' explicitly."
            ),
        )
    part = session.active_part()
    for body in comutil.com_iter(comutil.safe(part, "Bodies")):
        found = comutil.safe_call(comutil.safe(body, "Sketches"), "Item", wanted)
        if found is not None:
            return found
    for holder in comutil.com_iter(comutil.safe(part, "HybridBodies")):
        found = comutil.safe_call(comutil.safe(holder, "HybridSketches"), "Item", wanted)
        if found is not None:
            return found
    return refs.find_named(session, wanted)


def _apply_limit(
    session: Any,
    feature: Any,
    member: str,
    mode: str,
    value: float | None,
    limiting: str,
    part: Any,
) -> list[str]:
    """Configure FirstLimit / SecondLimit on a prism-like feature."""
    warnings: list[str] = []
    limit = comutil.safe(feature, member)
    if limit is None:
        return ["%s is not exposed on this feature." % member]

    key = LIMIT_MODES.get(mode)
    if key is None:
        raise errors.InvalidArgumentError(
            "Unknown limit mode %r. Valid: %s" % (mode, ", ".join(sorted(LIMIT_MODES)))
        )
    try:
        limit.LimitMode = constants.const(key)
    except Exception as exc:
        warnings.append("Could not set %s mode to %s (%s)." % (member, mode, exc))

    if mode == "dimension":
        if value is None:
            warnings.append("%s is a dimension limit but no value was given." % member)
        elif not set_parameter_value(limit, "Dimension", value):
            warnings.append("Could not apply the %s dimension." % member)
    elif mode in ("up_to_plane", "up_to_surface"):
        if not limiting:
            raise errors.InvalidArgumentError(
                "Limit mode %r needs limiting_element to name the plane or surface to stop at."
                % mode
            )
        target = refs.resolve(session, limiting, part=part)
        try:
            limit.LimitingElement = target.reference
        except Exception as exc:
            raise errors.OperationFailedError(
                "Could not use %s as the limit: %s" % (target.label, errors.com_message(exc))
            ) from exc
    return warnings


def register(mcp: Any, session: Any) -> None:
    tool = registrar(mcp, session)

    # ── extrusions ───────────────────────────────────────────────────────────

    @tool(
        "catia_pad",
        "Extrude a closed sketch profile into solid material. Supports a fixed length, "
        "symmetric extrusion about the sketch plane, a second limit in the opposite "
        "direction, up-to-next / up-to-last, stopping at a named plane or surface, and "
        "thin-walled pads.",
        group="part_design",
    )
    def catia_pad(
        length: Annotated[
            float, Field(description="Extrusion length in mm (ignored for up_to_* modes).")
        ] = 10.0,
        sketch: Annotated[
            str, Field(description="Profile sketch name. Defaults to the most recent sketch.")
        ] = "",
        limit_mode: Annotated[
            str,
            Field(
                description=(
                    "How the extrusion ends: dimension | up_to_next | up_to_last | "
                    "up_to_plane | up_to_surface."
                )
            ),
        ] = "dimension",
        limiting_element: Annotated[
            str,
            Field(
                description=(
                    "Reference token of the plane or surface to stop at, for up_to_plane "
                    "and up_to_surface."
                )
            ),
        ] = "",
        second_length: Annotated[
            float | None,
            Field(description="Length of a second limit on the other side of the sketch, mm."),
        ] = None,
        symmetric: Annotated[
            bool, Field(description="Extrude the same distance either side of the sketch plane.")
        ] = False,
        reverse: Annotated[
            bool, Field(description="Extrude towards the other side of the sketch plane.")
        ] = False,
        thickness: Annotated[
            float | None,
            Field(description="Wall thickness in mm to make this a thin pad instead of solid."),
        ] = None,
        name: Annotated[str, Field(description="Name for the resulting feature.")] = "",
    ) -> dict:
        part = session.active_part()
        factory = session.shape_factory()
        profile = _resolve_sketch(session, sketch)

        _, pad = comutil.try_variants(
            [
                ("AddNewPad", lambda: factory.AddNewPad(profile, float(length))),
                (
                    "AddNewPadFromRef",
                    lambda: factory.AddNewPadFromRef(
                        part.CreateReferenceFromObject(profile), float(length)
                    ),
                ),
            ],
            what="create a pad",
        )

        warnings = _apply_limit(
            session, pad, "FirstLimit", limit_mode, length, limiting_element, part
        )
        if second_length is not None:
            warnings += _apply_limit(
                session, pad, "SecondLimit", "dimension", second_length, "", part
            )
        if symmetric:
            try:
                pad.IsSymmetric = True
            except Exception:
                warnings.append("Could not make the pad symmetric.")
        if reverse:
            try:
                pad.DirectionOrientation = constants.const("catInverseOrientation")
            except Exception:
                warnings.append("Could not reverse the extrusion direction.")
        if thickness is not None:
            try:
                pad.IsThin = True
                set_parameter_value(pad, "ThinThickness1", float(thickness))
            except Exception:
                warnings.append("Could not make the pad thin-walled on this release.")

        payload = finish(session, part, pad, "pad", name)
        payload.update(
            {"length_mm": length, "limit_mode": limit_mode, "symmetric": symmetric}
        )
        return result.ok(payload, message="Created pad %r." % payload["created"],
                         warnings=warnings)

    @tool(
        "catia_pocket",
        "Cut material by extruding a closed sketch profile into the solid. Same limit "
        "options as catia_pad, including through-all via up_to_last.",
        group="part_design",
    )
    def catia_pocket(
        depth: Annotated[float, Field(description="Cut depth in mm.")] = 10.0,
        sketch: Annotated[str, Field(description="Profile sketch name.")] = "",
        limit_mode: Annotated[
            str,
            Field(
                description=(
                    "dimension | up_to_next | up_to_last (through all) | up_to_plane | "
                    "up_to_surface."
                )
            ),
        ] = "dimension",
        limiting_element: Annotated[
            str, Field(description="Reference token of the plane or surface to stop at.")
        ] = "",
        symmetric: Annotated[bool, Field(description="Cut both sides of the sketch plane.")] = False,
        reverse: Annotated[bool, Field(description="Cut towards the other side.")] = False,
        name: Annotated[str, Field(description="Name for the resulting feature.")] = "",
    ) -> dict:
        part = session.active_part()
        factory = session.shape_factory()
        profile = _resolve_sketch(session, sketch)

        _, pocket = comutil.try_variants(
            [
                ("AddNewPocket", lambda: factory.AddNewPocket(profile, float(depth))),
                (
                    "AddNewPocketFromRef",
                    lambda: factory.AddNewPocketFromRef(
                        part.CreateReferenceFromObject(profile), float(depth)
                    ),
                ),
            ],
            what="create a pocket",
        )

        warnings = _apply_limit(
            session, pocket, "FirstLimit", limit_mode, depth, limiting_element, part
        )
        if symmetric:
            try:
                pocket.IsSymmetric = True
            except Exception:
                warnings.append("Could not make the pocket symmetric.")
        if reverse:
            try:
                pocket.DirectionOrientation = constants.const("catInverseOrientation")
            except Exception:
                warnings.append("Could not reverse the cut direction.")

        payload = finish(session, part, pocket, "pocket", name)
        payload.update({"depth_mm": depth, "limit_mode": limit_mode})
        return result.ok(payload, message="Created pocket %r." % payload["created"],
                         warnings=warnings)

    @tool(
        "catia_shaft",
        "Revolve a sketch profile around an axis to make a solid of revolution. The axis "
        "is the sketch's centre line if it has one (see the axis flag on "
        "catia_sketch_line), otherwise pass an explicit axis reference.",
        group="part_design",
    )
    def catia_shaft(
        angle: Annotated[
            float, Field(description="Revolution angle in degrees.", gt=0, le=360)
        ] = 360.0,
        second_angle: Annotated[
            float, Field(description="Angle revolved the other way, degrees.", ge=0, le=360)
        ] = 0.0,
        sketch: Annotated[str, Field(description="Profile sketch name.")] = "",
        axis: Annotated[
            str,
            Field(
                description=(
                    "Optional reference token for the revolution axis, e.g. a construction "
                    "line or 'name:Line.1'. Omit to use the sketch's centre line."
                )
            ),
        ] = "",
        name: Annotated[str, Field(description="Name for the resulting feature.")] = "",
    ) -> dict:
        part = session.active_part()
        factory = session.shape_factory()
        profile = _resolve_sketch(session, sketch)

        _, shaft = comutil.try_variants(
            [
                ("AddNewShaft", lambda: factory.AddNewShaft(profile)),
                (
                    "AddNewShaftFromRef",
                    lambda: factory.AddNewShaftFromRef(part.CreateReferenceFromObject(profile)),
                ),
            ],
            what="create a shaft",
        )

        warnings: list[str] = []
        if axis:
            target = refs.resolve(session, axis, part=part)
            try:
                shaft.RevoluteAxis = target.reference
            except Exception as exc:
                warnings.append("Could not apply the axis %s (%s)." % (target.label, exc))
        if not set_parameter_value(shaft, "FirstAngle", angle):
            warnings.append("Could not set the revolution angle.")
        if second_angle and not set_parameter_value(shaft, "SecondAngle", second_angle):
            warnings.append("Could not set the second revolution angle.")

        payload = finish(session, part, shaft, "shaft", name)
        payload["angle_deg"] = angle
        return result.ok(
            payload,
            message="Created shaft %r." % payload["created"],
            warnings=warnings,
            hint=(
                "If CATIA reports no axis, add a construction line to the sketch with "
                "catia_sketch_line(..., construction=true, axis=true)."
            ),
        )

    @tool(
        "catia_groove",
        "Cut material by revolving a sketch profile around an axis - the subtractive "
        "counterpart of catia_shaft.",
        group="part_design",
    )
    def catia_groove(
        angle: Annotated[
            float, Field(description="Revolution angle in degrees.", gt=0, le=360)
        ] = 360.0,
        sketch: Annotated[str, Field(description="Profile sketch name.")] = "",
        axis: Annotated[str, Field(description="Optional revolution axis reference token.")] = "",
        name: Annotated[str, Field(description="Name for the resulting feature.")] = "",
    ) -> dict:
        part = session.active_part()
        factory = session.shape_factory()
        profile = _resolve_sketch(session, sketch)

        _, groove = comutil.try_variants(
            [
                ("AddNewGroove", lambda: factory.AddNewGroove(profile)),
                (
                    "AddNewGrooveFromRef",
                    lambda: factory.AddNewGrooveFromRef(part.CreateReferenceFromObject(profile)),
                ),
            ],
            what="create a groove",
        )

        warnings: list[str] = []
        if axis:
            target = refs.resolve(session, axis, part=part)
            try:
                groove.RevoluteAxis = target.reference
            except Exception as exc:
                warnings.append("Could not apply the axis %s (%s)." % (target.label, exc))
        if not set_parameter_value(groove, "FirstAngle", angle):
            warnings.append("Could not set the revolution angle.")

        payload = finish(session, part, groove, "groove", name)
        payload["angle_deg"] = angle
        return result.ok(payload, message="Created groove %r." % payload["created"],
                         warnings=warnings)

    @tool(
        "catia_rib",
        "Sweep a closed profile along a guide curve to add material - CATIA's Rib feature. "
        "The profile and the centre curve must be separate sketches or curves.",
        group="part_design",
    )
    def catia_rib(
        profile: Annotated[str, Field(description="Reference token of the profile sketch.")],
        center_curve: Annotated[
            str, Field(description="Reference token of the guide curve or sketch.")
        ],
        name: Annotated[str, Field(description="Name for the resulting feature.")] = "",
    ) -> dict:
        return _swept(session, "rib", profile, center_curve, name)

    @tool(
        "catia_slot",
        "Sweep a closed profile along a guide curve to remove material - CATIA's Slot "
        "feature, the subtractive counterpart of catia_rib.",
        group="part_design",
    )
    def catia_slot(
        profile: Annotated[str, Field(description="Reference token of the profile sketch.")],
        center_curve: Annotated[str, Field(description="Reference token of the guide curve.")],
        name: Annotated[str, Field(description="Name for the resulting feature.")] = "",
    ) -> dict:
        return _swept(session, "slot", profile, center_curve, name)

    def _swept(sess: Any, kind: str, profile: str, curve: str, name: str) -> dict:
        ensure_closed(sess)
        part = sess.active_part()
        factory = sess.shape_factory()
        profile_ref = refs.resolve(sess, profile, part=part)
        curve_ref = refs.resolve(sess, curve, part=part)
        method = "AddNewRib" if kind == "rib" else "AddNewSlot"

        _, feature = comutil.try_variants(
            [
                (
                    method + "FromRef",
                    lambda: getattr(factory, method + "FromRef")(
                        profile_ref.reference, curve_ref.reference
                    ),
                ),
                (
                    method,
                    lambda: getattr(factory, method)(profile_ref.obj, curve_ref.obj),
                ),
            ],
            what="create a %s" % kind,
        )
        payload = finish(sess, part, feature, kind, name)
        payload.update({"profile": profile_ref.label, "center_curve": curve_ref.label})
        return result.ok(payload, message="Created %s %r." % (kind, payload["created"]))

    @tool(
        "catia_stiffener",
        "Create a stiffener (rib/gusset) from an open sketch profile, thickened normal to "
        "the sketch plane and extended until it meets the existing solid.",
        group="part_design",
    )
    def catia_stiffener(
        sketch: Annotated[str, Field(description="Open profile sketch name.")] = "",
        thickness: Annotated[float, Field(description="Stiffener thickness in mm.", gt=0)] = 5.0,
        name: Annotated[str, Field(description="Name for the resulting feature.")] = "",
    ) -> dict:
        part = session.active_part()
        factory = session.shape_factory()
        profile = _resolve_sketch(session, sketch)

        _, stiffener = comutil.try_variants(
            [
                ("AddNewStiffener", lambda: factory.AddNewStiffener(profile)),
                (
                    "AddNewStiffenerFromRef",
                    lambda: factory.AddNewStiffenerFromRef(
                        part.CreateReferenceFromObject(profile)
                    ),
                ),
            ],
            what="create a stiffener",
        )
        warnings: list[str] = []
        if not set_parameter_value(stiffener, "Thickness1", thickness):
            warnings.append("Could not set the stiffener thickness.")
        payload = finish(session, part, stiffener, "stiffener", name)
        payload["thickness_mm"] = thickness
        return result.ok(payload, message="Created stiffener %r." % payload["created"],
                         warnings=warnings)

    # ── holes ────────────────────────────────────────────────────────────────

    @tool(
        "catia_hole",
        "Create a hole in a face. Position it either by a 3D point (the usual case - give "
        "the point and the face token) or from a sketch containing hole centres. Supports "
        "simple, tapered, counterbored, countersunk and counterdrilled holes, blind or "
        "through, with optional threading.",
        group="part_design",
    )
    def catia_hole(
        face: Annotated[
            str,
            Field(
                description=(
                    "Reference token of the face to drill into, e.g. 'face#1' or "
                    "'face@0,0,20'. Use catia_list_faces to find it."
                )
            ),
        ],
        x: Annotated[float, Field(description="Hole centre X in part coordinates, mm.")] = 0.0,
        y: Annotated[float, Field(description="Hole centre Y in part coordinates, mm.")] = 0.0,
        z: Annotated[float, Field(description="Hole centre Z in part coordinates, mm.")] = 0.0,
        diameter: Annotated[float, Field(description="Hole diameter in mm.", gt=0)] = 6.0,
        depth: Annotated[float, Field(description="Hole depth in mm.", gt=0)] = 10.0,
        through: Annotated[
            bool, Field(description="Make the hole go all the way through the material.")
        ] = False,
        hole_type: Annotated[
            str,
            Field(
                description=(
                    "simple | tapered | counterbored | countersunk | counterdrilled."
                )
            ),
        ] = "simple",
        head_diameter: Annotated[
            float | None,
            Field(description="Head diameter for counterbored/countersunk holes, mm."),
        ] = None,
        head_depth: Annotated[
            float | None, Field(description="Head depth for counterbored holes, mm.")
        ] = None,
        head_angle: Annotated[
            float | None, Field(description="Head angle for countersunk holes, degrees.")
        ] = None,
        threaded: Annotated[bool, Field(description="Add a thread to the hole.")] = False,
        name: Annotated[str, Field(description="Name for the resulting feature.")] = "",
    ) -> dict:
        ensure_closed(session)
        part = session.active_part()
        factory = session.shape_factory()
        support = refs.resolve(session, face, part=part)

        _, hole = comutil.try_variants(
            [
                (
                    "AddNewHoleFromPoint",
                    lambda: factory.AddNewHoleFromPoint(
                        float(x), float(y), float(z), support.reference, float(depth)
                    ),
                ),
                (
                    "AddNewHoleFromRefPoint",
                    lambda: factory.AddNewHoleFromRefPoint(
                        None, support.reference, float(depth)
                    ),
                ),
            ],
            what="create a hole",
        )

        warnings: list[str] = []
        if not set_parameter_value(hole, "Diameter", diameter):
            warnings.append("Could not set the hole diameter.")

        type_key = {
            "simple": "catSimpleHole",
            "tapered": "catTaperedHole",
            "counterbored": "catCounterboredHole",
            "countersunk": "catCountersunkHole",
            "counterdrilled": "catCounterdrilledHole",
        }.get(hole_type.lower())
        if type_key is None:
            raise errors.InvalidArgumentError(
                "Unknown hole_type %r. Valid: simple, tapered, counterbored, countersunk, "
                "counterdrilled." % hole_type
            )
        try:
            hole.Type = constants.const(type_key)
        except Exception:
            warnings.append("Could not set the hole type to %s." % hole_type)

        if through:
            limit = comutil.safe(hole, "BottomLimit")
            if limit is not None:
                try:
                    limit.LimitMode = constants.const("catUpToLastLimit")
                except Exception:
                    warnings.append("Could not make the hole a through hole.")
        for member, value in (
            ("HeadDiameter", head_diameter),
            ("HeadDepth", head_depth),
            ("HeadAngle", head_angle),
        ):
            if value is not None and not set_parameter_value(hole, member, value):
                warnings.append("Could not set %s." % member)
        if threaded:
            try:
                hole.ThreadingMode = 1
            except Exception:
                warnings.append("Could not enable threading on this release.")

        payload = finish(session, part, hole, "hole", name)
        payload.update(
            {
                "diameter_mm": diameter,
                "depth_mm": depth,
                "through": through,
                "hole_type": hole_type,
                "face": support.label,
                "position": {"x": x, "y": y, "z": z},
            }
        )
        return result.ok(
            payload,
            message="Created a %s hole of diameter %.2f mm." % (hole_type, diameter),
            warnings=warnings,
            hint=(
                "The point must lie on the named face. If CATIA rejects it, read the face "
                "centroid from catia_list_faces and offset from there."
            ),
        )

    @tool(
        "catia_hole_from_sketch",
        "Create holes at every point of a sketch, drilled into the named face. Convenient "
        "for bolt patterns laid out in a sketch.",
        group="part_design",
    )
    def catia_hole_from_sketch(
        sketch: Annotated[str, Field(description="Sketch containing the hole centre points.")],
        diameter: Annotated[float, Field(description="Hole diameter in mm.", gt=0)] = 6.0,
        depth: Annotated[float, Field(description="Hole depth in mm.", gt=0)] = 10.0,
        through: Annotated[bool, Field(description="Drill all the way through.")] = False,
        name: Annotated[str, Field(description="Name for the resulting feature.")] = "",
    ) -> dict:
        part = session.active_part()
        factory = session.shape_factory()
        profile = _resolve_sketch(session, sketch)

        _, hole = comutil.try_variants(
            [
                ("AddNewHoleFromSketch", lambda: factory.AddNewHoleFromSketch(profile)),
                (
                    "AddNewHoleFromSketchRef",
                    lambda: factory.AddNewHoleFromSketch(
                        part.CreateReferenceFromObject(profile)
                    ),
                ),
            ],
            what="create holes from a sketch",
        )
        warnings: list[str] = []
        if not set_parameter_value(hole, "Diameter", diameter):
            warnings.append("Could not set the hole diameter.")
        if through:
            limit = comutil.safe(hole, "BottomLimit")
            if limit is not None:
                try:
                    limit.LimitMode = constants.const("catUpToLastLimit")
                except Exception:
                    warnings.append("Could not make the holes through holes.")
        else:
            limit = comutil.safe(hole, "BottomLimit")
            if limit is not None:
                set_parameter_value(limit, "Dimension", depth)

        payload = finish(session, part, hole, "hole", name)
        payload.update({"diameter_mm": diameter, "sketch": comutil.name_of(profile)})
        return result.ok(payload, message="Created holes from %s." % comutil.name_of(profile),
                         warnings=warnings)

    @tool(
        "catia_solid_combine",
        "Create a solid from two intersecting sketch profiles extruded normal to their "
        "planes - CATIA's Combine feature. Useful for shapes that are hard to express as "
        "one profile.",
        group="part_design",
    )
    def catia_solid_combine(
        first_sketch: Annotated[str, Field(description="First profile sketch name.")],
        second_sketch: Annotated[str, Field(description="Second profile sketch name.")],
        name: Annotated[str, Field(description="Name for the resulting feature.")] = "",
    ) -> dict:
        ensure_closed(session)
        part = session.active_part()
        factory = session.shape_factory()
        first = _resolve_sketch(session, first_sketch)
        second = _resolve_sketch(session, second_sketch)
        _, combine = comutil.try_variants(
            [
                ("AddNewSolidCombine", lambda: factory.AddNewSolidCombine(first, second)),
                (
                    "AddNewSolidCombineFromRef",
                    lambda: factory.AddNewSolidCombine(
                        part.CreateReferenceFromObject(first),
                        part.CreateReferenceFromObject(second),
                    ),
                ),
            ],
            what="combine two profiles",
        )
        payload = finish(session, part, combine, "combine", name)
        return result.ok(payload, message="Created combine %r." % payload["created"])
