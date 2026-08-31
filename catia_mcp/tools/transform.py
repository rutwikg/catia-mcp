"""Patterns, mirrors, transformations and boolean body operations."""

from __future__ import annotations

import logging
from typing import Annotated, Any

from pydantic import Field

from catia_mcp.core import comutil, errors, refs, result
from catia_mcp.tools.base import registrar, rename, set_parameter_value
from catia_mcp.tools.part_design import finish
from catia_mcp.tools.sketch import ensure_closed

logger = logging.getLogger("catia_mcp.tools.transform")

BOOLEAN_OPS = {
    "add": ("AddNewAdd", "union"),
    "remove": ("AddNewRemove", "subtraction"),
    "intersect": ("AddNewIntersect", "intersection"),
    "union_trim": ("AddNewUnionTrim", "union trim"),
}


def _item_to_pattern(session: Any, part: Any, token: str) -> Any:
    """Resolve what a pattern should replicate; defaults to the last feature."""
    if token:
        return refs.resolve(session, token, part=part).obj
    name = session.state.last_feature_name
    if not name:
        raise errors.InvalidArgumentError(
            "Nothing to pattern. Pass feature='name:Pocket.1', or create a feature first.",
        )
    return refs.find_named(session, name)


def _apply_repartition(pattern: Any, member: str, count: int, spacing: float) -> list[str]:
    warnings: list[str] = []
    repartition = comutil.safe(pattern, member)
    if repartition is None:
        return ["%s is not exposed on this pattern." % member]
    if not set_parameter_value(repartition, "InstancesCount", count):
        warnings.append("Could not set the instance count on %s." % member)
    if not set_parameter_value(repartition, "Spacing", spacing):
        warnings.append("Could not set the spacing on %s." % member)
    return warnings


def register(mcp: Any, session: Any) -> None:
    tool = registrar(mcp, session)

    # ── patterns ─────────────────────────────────────────────────────────────

    @tool(
        "catia_rect_pattern",
        "Repeat a feature on a rectangular grid. Directions are given as reference tokens "
        "of an edge or line - for example 'edge#1' - because CATIA needs a real direction "
        "element, not an axis name. Leave a direction empty to let CATIA choose its "
        "default for that axis.",
        group="transform",
    )
    def catia_rect_pattern(
        instances_1: Annotated[
            int, Field(description="Number of instances along the first direction.", ge=1)
        ] = 2,
        spacing_1: Annotated[
            float, Field(description="Spacing along the first direction, mm.", gt=0)
        ] = 20.0,
        instances_2: Annotated[
            int, Field(description="Number of instances along the second direction.", ge=1)
        ] = 1,
        spacing_2: Annotated[
            float, Field(description="Spacing along the second direction, mm.", gt=0)
        ] = 20.0,
        direction_1: Annotated[
            str, Field(description="Reference token of an edge or line for the first direction.")
        ] = "",
        direction_2: Annotated[
            str, Field(description="Reference token for the second direction.")
        ] = "",
        feature: Annotated[
            str,
            Field(description="Reference token of the feature to repeat. Defaults to the last one."),
        ] = "",
        reverse_1: Annotated[bool, Field(description="Reverse the first direction.")] = False,
        reverse_2: Annotated[bool, Field(description="Reverse the second direction.")] = False,
        name: Annotated[str, Field(description="Name for the resulting feature.")] = "",
    ) -> dict:
        ensure_closed(session)
        if direction_1 and direction_2 and direction_1.strip() == direction_2.strip():
            raise errors.InvalidArgumentError(
                "direction_1 and direction_2 are the same reference (%r), so they are "
                "colinear and CATIA cannot build a grid from them." % direction_1,
                remediation=(
                    "Pass two non-parallel references - catia_list_edges will show edges "
                    "along different axes - or leave direction_2 empty for a single-row "
                    "pattern with instances_2=1."
                ),
            )
        part = session.active_part()
        factory = session.shape_factory()
        item = _item_to_pattern(session, part, feature)
        dir1 = refs.resolve(session, direction_1, part=part).reference if direction_1 else None
        dir2 = refs.resolve(session, direction_2, part=part).reference if direction_2 else None

        _, pattern = comutil.try_variants(
            [
                (
                    "AddNewRectPattern(13)",
                    lambda: factory.AddNewRectPattern(
                        item,
                        int(instances_1),
                        int(instances_2),
                        float(spacing_1),
                        float(spacing_2),
                        1,
                        1,
                        dir1,
                        dir2,
                        not reverse_1,
                        not reverse_2,
                        0.0,
                        0.0,
                    ),
                ),
                (
                    "AddNewRectPattern(11)",
                    lambda: factory.AddNewRectPattern(
                        item,
                        int(instances_1),
                        int(instances_2),
                        float(spacing_1),
                        float(spacing_2),
                        1,
                        1,
                        dir1,
                        dir2,
                        not reverse_1,
                        not reverse_2,
                    ),
                ),
            ],
            what="create a rectangular pattern",
        )

        warnings = _apply_repartition(
            pattern, "FirstDirectionRepartition", int(instances_1), float(spacing_1)
        )
        if instances_2 > 1:
            warnings += _apply_repartition(
                pattern, "SecondDirectionRepartition", int(instances_2), float(spacing_2)
            )

        payload = finish(session, part, pattern, "rect_pattern", name)
        payload.update(
            {
                "instances": [int(instances_1), int(instances_2)],
                "spacing_mm": [spacing_1, spacing_2],
                "total_instances": int(instances_1) * int(instances_2),
            }
        )
        return result.ok(
            payload,
            message="Patterned %d x %d." % (instances_1, instances_2),
            warnings=warnings,
            hint=(
                "If the instances went the wrong way, pass explicit direction tokens - "
                "catia_list_edges will show you an edge parallel to the axis you want."
            ),
        )

    @tool(
        "catia_circ_pattern",
        "Repeat a feature around an axis. The rotation axis is a reference token for a "
        "cylindrical face, an edge, a line or an axis-system axis.",
        group="transform",
    )
    def catia_circ_pattern(
        instances: Annotated[
            int, Field(description="Number of instances around the axis.", ge=1)
        ] = 4,
        angular_spacing: Annotated[
            float, Field(description="Angle between instances, degrees.", gt=0)
        ] = 90.0,
        axis: Annotated[
            str,
            Field(
                description=(
                    "Reference token of the rotation axis: a cylindrical face, an edge, "
                    "or a line."
                )
            ),
        ] = "",
        radial_instances: Annotated[
            int, Field(description="Rings of instances at increasing radius.", ge=1)
        ] = 1,
        radial_spacing: Annotated[
            float, Field(description="Radial spacing between rings, mm.", gt=0)
        ] = 20.0,
        feature: Annotated[
            str, Field(description="Reference token of the feature to repeat.")
        ] = "",
        reverse: Annotated[bool, Field(description="Rotate the other way.")] = False,
        name: Annotated[str, Field(description="Name for the resulting feature.")] = "",
    ) -> dict:
        ensure_closed(session)
        part = session.active_part()
        factory = session.shape_factory()
        item = _item_to_pattern(session, part, feature)
        axis_ref = refs.resolve(session, axis, part=part).reference if axis else None

        _, pattern = comutil.try_variants(
            [
                (
                    "AddNewCircPattern(13)",
                    lambda: factory.AddNewCircPattern(
                        item,
                        int(instances),
                        int(radial_instances),
                        float(angular_spacing),
                        float(radial_spacing),
                        1,
                        1,
                        axis_ref,
                        None,
                        not reverse,
                        True,
                        0.0,
                        0.0,
                    ),
                ),
                (
                    "AddNewCircPattern(11)",
                    lambda: factory.AddNewCircPattern(
                        item,
                        int(instances),
                        int(radial_instances),
                        float(angular_spacing),
                        float(radial_spacing),
                        1,
                        1,
                        axis_ref,
                        None,
                        not reverse,
                        True,
                    ),
                ),
            ],
            what="create a circular pattern",
        )

        warnings = _apply_repartition(
            pattern, "AngularRepartition", int(instances), float(angular_spacing)
        )
        if radial_instances > 1:
            warnings += _apply_repartition(
                pattern, "RadialRepartition", int(radial_instances), float(radial_spacing)
            )

        payload = finish(session, part, pattern, "circ_pattern", name)
        payload.update(
            {
                "instances": int(instances),
                "angular_spacing_deg": angular_spacing,
                "axis": axis or "CATIA default",
            }
        )
        return result.ok(
            payload,
            message="Patterned %d instances around the axis." % instances,
            warnings=warnings,
        )

    @tool(
        "catia_user_pattern",
        "Repeat a feature at every point of a sketch - CATIA's User Pattern. Use it for "
        "irregular hole layouts that no rectangular or circular pattern can describe.",
        group="transform",
    )
    def catia_user_pattern(
        positions_sketch: Annotated[
            str, Field(description="Sketch whose points give the instance positions.")
        ],
        feature: Annotated[
            str, Field(description="Reference token of the feature to repeat.")
        ] = "",
        name: Annotated[str, Field(description="Name for the resulting feature.")] = "",
    ) -> dict:
        ensure_closed(session)
        part = session.active_part()
        factory = session.shape_factory()
        item = _item_to_pattern(session, part, feature)
        anchor = refs.resolve(session, positions_sketch, part=part)

        _, pattern = comutil.try_variants(
            [
                ("AddNewUserPattern(2)", lambda: factory.AddNewUserPattern(item, anchor.obj)),
                ("AddNewUserPattern(1)", lambda: factory.AddNewUserPattern(item)),
            ],
            what="create a user pattern",
        )
        warnings: list[str] = []
        try:
            pattern.AnchorPoint = anchor.reference
        except Exception:
            warnings.append("Could not bind the anchor sketch explicitly.")

        payload = finish(session, part, pattern, "user_pattern", name)
        payload["positions_sketch"] = anchor.label
        return result.ok(payload, message="Created a user pattern.", warnings=warnings)

    # ── mirror and transformations ───────────────────────────────────────────

    @tool(
        "catia_mirror",
        "Mirror the current body about a plane or planar face.",
        group="transform",
    )
    def catia_mirror(
        plane: Annotated[
            str, Field(description="Reference token of the mirror plane, e.g. 'yz' or 'face#2'.")
        ] = "yz",
        name: Annotated[str, Field(description="Name for the resulting feature.")] = "",
    ) -> dict:
        ensure_closed(session)
        part = session.active_part()
        factory = session.shape_factory()
        target = refs.resolve(session, plane, part=part)
        _, mirror = comutil.try_variants(
            [
                ("AddNewMirror", lambda: factory.AddNewMirror(target.reference)),
                ("AddNewSymmetry", lambda: factory.AddNewSymmetry(target.reference)),
            ],
            what="mirror the body",
        )
        payload = finish(session, part, mirror, "mirror", name)
        payload["plane"] = target.label
        return result.ok(payload, message="Mirrored about %s." % target.label)

    @tool(
        "catia_transform_body",
        "Apply a transformation feature to the current body: translate along a direction, "
        "rotate about an axis, mirror about a plane, or scale about a reference. These are "
        "history features - they move the body from that point in the tree onwards.",
        group="transform",
    )
    def catia_transform_body(
        operation: Annotated[
            str, Field(description="translate | rotate | symmetry | scale.")
        ],
        reference: Annotated[
            str,
            Field(
                description=(
                    "Reference token for the operation: a direction (edge/line) for "
                    "translate, an axis for rotate, a plane for symmetry, a point or plane "
                    "for scale."
                )
            ),
        ],
        value: Annotated[
            float,
            Field(
                description=(
                    "Distance in mm for translate, angle in degrees for rotate, ratio for "
                    "scale. Ignored for symmetry."
                )
            ),
        ] = 10.0,
        name: Annotated[str, Field(description="Name for the resulting feature.")] = "",
    ) -> dict:
        ensure_closed(session)
        part = session.active_part()
        factory = session.shape_factory()
        target = refs.resolve(session, reference, part=part)
        key = operation.strip().lower()

        builders = {
            "translate": [
                ("AddNewTranslate(2)",
                 lambda: factory.AddNewTranslate(target.reference, float(value))),
                ("AddNewTranslate(0)", lambda: factory.AddNewTranslate()),
            ],
            "rotate": [
                ("AddNewRotate(2)",
                 lambda: factory.AddNewRotate(target.reference, float(value))),
                ("AddNewRotate(0)", lambda: factory.AddNewRotate()),
            ],
            "symmetry": [
                ("AddNewSymmetry", lambda: factory.AddNewSymmetry(target.reference)),
            ],
            "scale": [
                ("AddNewScaling(2)",
                 lambda: factory.AddNewScaling(target.reference, float(value))),
            ],
        }
        if key not in builders:
            raise errors.InvalidArgumentError(
                "operation must be translate, rotate, symmetry or scale; got %r." % operation
            )

        _, feature = comutil.try_variants(builders[key], what="apply a %s" % key)
        warnings: list[str] = []
        if key == "translate":
            if not set_parameter_value(feature, "Distance", value):
                warnings.append("Could not set the translation distance.")
        elif key == "rotate":
            if not set_parameter_value(feature, "Angle", value):
                warnings.append("Could not set the rotation angle.")
        elif key == "scale":
            if not set_parameter_value(feature, "Ratio", value):
                warnings.append("Could not set the scale ratio.")

        payload = finish(session, part, feature, key, name)
        payload.update({"operation": key, "reference": target.label, "value": value})
        return result.ok(payload, message="Applied %s." % key, warnings=warnings)

    # ── bodies and booleans ──────────────────────────────────────────────────

    @tool(
        "catia_new_body",
        "Add a new empty body to the part and make it the in-work object, so subsequent "
        "features go into it. This is how you build shapes to combine with boolean "
        "operations.",
        group="transform",
    )
    def catia_new_body(
        name: Annotated[str, Field(description="Name for the new body.")] = "",
        set_in_work: Annotated[
            bool, Field(description="Make the new body the target for new features.")
        ] = True,
    ) -> dict:
        ensure_closed(session)
        part = session.active_part()
        body = part.Bodies.Add()
        final = rename(body, name)
        if set_in_work:
            part.InWorkObject = body
        return result.ok(
            {
                "created": final,
                "reference_token": "body:%s" % final,
                "in_work": set_in_work,
                "body_count": comutil.com_count(part.Bodies),
            },
            message="Created body %r." % final,
        )

    @tool(
        "catia_boolean",
        "Combine two bodies: add (union), remove (subtract), intersect, or union-trim. The "
        "tool body is consumed into the target body, which is the usual CATIA behaviour.",
        group="transform",
    )
    def catia_boolean(
        operation: Annotated[
            str, Field(description="add | remove | intersect | union_trim.")
        ],
        tool_body: Annotated[
            str, Field(description="Name of the body to combine in, e.g. 'Body.2'.")
        ],
        target_body: Annotated[
            str,
            Field(
                description=(
                    "Body to combine into. Defaults to the part's main body."
                )
            ),
        ] = "",
        name: Annotated[str, Field(description="Name for the resulting feature.")] = "",
    ) -> dict:
        ensure_closed(session)
        key = operation.strip().lower()
        if key not in BOOLEAN_OPS:
            raise errors.InvalidArgumentError(
                "operation must be one of %s; got %r." % (", ".join(BOOLEAN_OPS), operation)
            )
        method, label = BOOLEAN_OPS[key]
        part = session.active_part()
        factory = session.shape_factory()
        bodies = comutil.safe(part, "Bodies")

        tool_obj = comutil.safe_call(bodies, "Item", tool_body)
        if tool_obj is None:
            raise errors.ElementNotFoundError(
                "No body called %r. Available: %s"
                % (tool_body, ", ".join(comutil.name_of(b) for b in comutil.com_iter(bodies)))
            )
        target_obj = (
            comutil.safe_call(bodies, "Item", target_body)
            if target_body
            else comutil.safe(part, "MainBody")
        )
        if target_obj is None:
            raise errors.ElementNotFoundError("No body called %r." % target_body)

        # The boolean is created inside the target body, so it must be in work.
        part.InWorkObject = target_obj
        _, feature = comutil.try_variants(
            [(method, lambda: getattr(factory, method)(tool_obj))],
            what="perform a boolean %s" % label,
        )

        payload = finish(session, part, feature, "boolean_%s" % key, name)
        payload.update(
            {
                "operation": key,
                "tool_body": comutil.name_of(tool_obj),
                "target_body": comutil.name_of(target_obj),
            }
        )
        return result.ok(
            payload,
            message="Applied boolean %s of %s into %s."
            % (label, comutil.name_of(tool_obj), comutil.name_of(target_obj)),
        )

    @tool(
        "catia_split_body",
        "Cut the current body with a surface or plane, keeping the material on one side.",
        group="transform",
    )
    def catia_split_body(
        cutting_element: Annotated[
            str, Field(description="Reference token of the plane or surface to cut with.")
        ],
        keep_positive_side: Annotated[
            bool, Field(description="Keep the material on the positive side of the cutter.")
        ] = True,
        name: Annotated[str, Field(description="Name for the resulting feature.")] = "",
    ) -> dict:
        ensure_closed(session)
        part = session.active_part()
        factory = session.shape_factory()
        target = refs.resolve(session, cutting_element, part=part)
        side = 1 if keep_positive_side else 0
        _, split = comutil.try_variants(
            [
                ("AddNewSplit(2)", lambda: factory.AddNewSplit(target.reference, side)),
                ("AddNewSplit(1)", lambda: factory.AddNewSplit(target.reference)),
            ],
            what="split the body",
        )
        payload = finish(session, part, split, "split", name)
        payload.update({"cutting_element": target.label, "kept_side": "positive" if side else "negative"})
        return result.ok(payload, message="Split the body with %s." % target.label)
