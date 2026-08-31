"""Dress-up features: fillets, chamfers, drafts, shells, thickness, threads.

These are the tools that need real topological selection, which is where a
naive CATIA client usually gives up and fillets "the last feature" instead of
the edge that was asked for. Every tool here resolves proper references through
``core.refs``, so 'edge#4' and 'edge@20,0,10' mean what they say.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from pydantic import Field

from catia_mcp.core import comutil, constants, errors, refs, result
from catia_mcp.tools.base import registrar, set_parameter_value
from catia_mcp.tools.part_design import finish
from catia_mcp.tools.sketch import ensure_closed

logger = logging.getLogger("catia_mcp.tools.dressup")


def _resolve_all(session: Any, part: Any, tokens: list[str], what: str) -> list[Any]:
    if not tokens:
        raise errors.InvalidArgumentError("At least one %s reference is required." % what)
    return [refs.resolve(session, token, part=part) for token in tokens]


def register(mcp: Any, session: Any) -> None:
    tool = registrar(mcp, session)

    # ── fillets ──────────────────────────────────────────────────────────────

    @tool(
        "catia_fillet",
        "Round one or more edges with a constant radius. Edges are named with reference "
        "tokens - 'edge#4' by index, or 'edge@20,0,10' to pick the edge nearest a point, "
        "which survives later model changes. Tangent-continuous edges are followed by "
        "default so a single token usually rounds a whole chain.",
        group="dressup",
    )
    def catia_fillet(
        edges: Annotated[
            list[str],
            Field(
                description=(
                    "Reference tokens of the edges to round, e.g. ['edge#3','edge@0,0,20']. "
                    "A face token rounds every edge of that face."
                )
            ),
        ],
        radius: Annotated[float, Field(description="Fillet radius in mm.", gt=0)] = 2.0,
        propagate_tangency: Annotated[
            bool,
            Field(description="Continue the fillet across tangent-continuous edges."),
        ] = True,
        name: Annotated[str, Field(description="Name for the resulting feature.")] = "",
    ) -> dict:
        ensure_closed(session)
        part = session.active_part()
        factory = session.shape_factory()
        targets = _resolve_all(session, part, edges, "edge")
        propagation = constants.const(
            "catTangencyFilletEdgePropagation"
            if propagate_tangency
            else "catMinimalFilletEdgePropagation"
        )

        _, fillet = comutil.try_variants(
            [
                (
                    "AddNewSolidEdgeFilletWithConstantRadius",
                    lambda: factory.AddNewSolidEdgeFilletWithConstantRadius(
                        targets[0].reference, propagation, float(radius)
                    ),
                ),
                (
                    "AddNewEdgeFilletWithConstantRadius",
                    lambda: factory.AddNewEdgeFilletWithConstantRadius(
                        targets[0].reference, propagation, float(radius)
                    ),
                ),
            ],
            what="create an edge fillet",
        )

        warnings: list[str] = []
        for extra in targets[1:]:
            try:
                fillet.AddObjectToFillet(extra.reference)
            except Exception as exc:
                warnings.append(
                    "Could not add %s to the fillet (%s)."
                    % (extra.label, errors.com_message(exc))
                )

        payload = finish(session, part, fillet, "fillet", name)
        payload.update({"radius_mm": radius, "edges": [t.label for t in targets]})
        return result.ok(
            payload,
            message="Filleted %d edge selection(s) at R%.3f mm." % (len(targets), radius),
            warnings=warnings,
            hint=(
                "If the update failed, the radius is probably too large for the adjacent "
                "faces. catia_list_edges reports each edge's length as a rough ceiling."
            ),
        )

    @tool(
        "catia_variable_fillet",
        "Round an edge with a radius that varies along it, between a start and an end "
        "radius.",
        group="dressup",
    )
    def catia_variable_fillet(
        edge: Annotated[str, Field(description="Reference token of the edge to round.")],
        start_radius: Annotated[float, Field(description="Radius at the start, mm.", gt=0)] = 2.0,
        end_radius: Annotated[float, Field(description="Radius at the end, mm.", gt=0)] = 6.0,
        propagate_tangency: Annotated[bool, Field(description="Follow tangent edges.")] = True,
        name: Annotated[str, Field(description="Name for the resulting feature.")] = "",
    ) -> dict:
        ensure_closed(session)
        part = session.active_part()
        factory = session.shape_factory()
        target = refs.resolve(session, edge, part=part)
        propagation = constants.const(
            "catTangencyFilletEdgePropagation"
            if propagate_tangency
            else "catMinimalFilletEdgePropagation"
        )
        _, fillet = comutil.try_variants(
            [
                (
                    "AddNewSolidEdgeFilletWithVariableRadius",
                    lambda: factory.AddNewSolidEdgeFilletWithVariableRadius(
                        target.reference, propagation, float(start_radius)
                    ),
                ),
                (
                    "AddNewEdgeFilletWithVariableRadius",
                    lambda: factory.AddNewEdgeFilletWithVariableRadius(
                        target.reference, propagation, float(start_radius)
                    ),
                ),
            ],
            what="create a variable-radius fillet",
        )
        warnings: list[str] = []
        if not set_parameter_value(fillet, "EndRadius", end_radius):
            warnings.append(
                "Could not set the end radius directly; the fillet is constant at %.3f mm."
                % start_radius
            )
        payload = finish(session, part, fillet, "variable_fillet", name)
        payload.update({"start_radius_mm": start_radius, "end_radius_mm": end_radius})
        return result.ok(payload, message="Created a variable fillet.", warnings=warnings)

    @tool(
        "catia_face_fillet",
        "Round the junction between two faces that need not share an edge - CATIA's Face-"
        "Face Fillet. Use it where an edge fillet cannot reach, such as across a step.",
        group="dressup",
    )
    def catia_face_fillet(
        first_face: Annotated[str, Field(description="Reference token of the first face.")],
        second_face: Annotated[str, Field(description="Reference token of the second face.")],
        radius: Annotated[float, Field(description="Fillet radius in mm.", gt=0)] = 5.0,
        name: Annotated[str, Field(description="Name for the resulting feature.")] = "",
    ) -> dict:
        ensure_closed(session)
        part = session.active_part()
        factory = session.shape_factory()
        first = refs.resolve(session, first_face, part=part)
        second = refs.resolve(session, second_face, part=part)
        _, fillet = comutil.try_variants(
            [
                (
                    "AddNewFaceFilletWithConstantRadius",
                    lambda: factory.AddNewFaceFilletWithConstantRadius(
                        first.reference, second.reference, float(radius)
                    ),
                ),
                (
                    "AddNewFaceFillet",
                    lambda: factory.AddNewFaceFillet(
                        first.reference, second.reference, float(radius)
                    ),
                ),
            ],
            what="create a face-face fillet",
        )
        payload = finish(session, part, fillet, "face_fillet", name)
        payload.update({"radius_mm": radius, "faces": [first.label, second.label]})
        return result.ok(payload, message="Created a face-face fillet.")

    @tool(
        "catia_tritangent_fillet",
        "Replace a face with a fillet tangent to three faces - CATIA's Tritangent Fillet. "
        "The removed face is the one that disappears into the round.",
        group="dressup",
    )
    def catia_tritangent_fillet(
        first_face: Annotated[str, Field(description="First supporting face token.")],
        second_face: Annotated[str, Field(description="Second supporting face token.")],
        face_to_remove: Annotated[str, Field(description="Face token to be removed.")],
        name: Annotated[str, Field(description="Name for the resulting feature.")] = "",
    ) -> dict:
        ensure_closed(session)
        part = session.active_part()
        factory = session.shape_factory()
        first = refs.resolve(session, first_face, part=part)
        second = refs.resolve(session, second_face, part=part)
        removed = refs.resolve(session, face_to_remove, part=part)
        _, fillet = comutil.try_variants(
            [
                (
                    "AddNewTritangentFillet",
                    lambda: factory.AddNewTritangentFillet(
                        first.reference, second.reference, removed.reference
                    ),
                ),
            ],
            what="create a tritangent fillet",
        )
        payload = finish(session, part, fillet, "tritangent_fillet", name)
        return result.ok(payload, message="Created a tritangent fillet.")

    # ── chamfer ──────────────────────────────────────────────────────────────

    @tool(
        "catia_chamfer",
        "Bevel one or more edges. Choose length_angle mode (a length and an angle, the "
        "usual case) or two_lengths mode (a setback on each face).",
        group="dressup",
    )
    def catia_chamfer(
        edges: Annotated[
            list[str], Field(description="Reference tokens of the edges to chamfer.")
        ],
        length: Annotated[float, Field(description="Chamfer length in mm.", gt=0)] = 1.0,
        angle: Annotated[
            float, Field(description="Chamfer angle in degrees (length_angle mode).", gt=0, lt=180)
        ] = 45.0,
        second_length: Annotated[
            float | None, Field(description="Second length in mm (two_lengths mode).")
        ] = None,
        mode: Annotated[str, Field(description="length_angle | two_lengths.")] = "length_angle",
        propagate_tangency: Annotated[bool, Field(description="Follow tangent edges.")] = True,
        reverse: Annotated[
            bool, Field(description="Swap which face the length is measured on.")
        ] = False,
        name: Annotated[str, Field(description="Name for the resulting feature.")] = "",
    ) -> dict:
        ensure_closed(session)
        part = session.active_part()
        factory = session.shape_factory()
        targets = _resolve_all(session, part, edges, "edge")

        propagation = constants.const(
            "catTangencyChamfer" if propagate_tangency else "catMinimalChamfer"
        )
        key = mode.strip().lower()
        if key not in ("length_angle", "two_lengths"):
            raise errors.InvalidArgumentError(
                "mode must be 'length_angle' or 'two_lengths', got %r." % mode
            )
        chamfer_mode = constants.const(
            "catLengthAngleChamfer" if key == "length_angle" else "catTwoLengthChamfer"
        )
        orientation = constants.const(
            "catReverseChamfer" if reverse else "catNoReverseChamfer"
        )
        value2 = float(angle) if key == "length_angle" else float(second_length or length)

        _, chamfer = comutil.try_variants(
            [
                (
                    "AddNewChamfer(6)",
                    lambda: factory.AddNewChamfer(
                        targets[0].reference,
                        propagation,
                        chamfer_mode,
                        orientation,
                        float(length),
                        value2,
                    ),
                ),
                (
                    "AddNewChamfer(5)",
                    lambda: factory.AddNewChamfer(
                        targets[0].reference,
                        propagation,
                        chamfer_mode,
                        float(length),
                        value2,
                    ),
                ),
            ],
            what="create a chamfer",
        )

        warnings: list[str] = []
        for extra in targets[1:]:
            try:
                chamfer.AddElementToChamfer(extra.reference)
            except Exception as exc:
                warnings.append(
                    "Could not add %s to the chamfer (%s)."
                    % (extra.label, errors.com_message(exc))
                )
        # Set the driving values by name too: the enumeration ordering of
        # CatChamferMode has moved between releases, so the positional
        # arguments alone are not enough to be sure which is length and
        # which is angle.
        set_parameter_value(chamfer, "Length1", length)
        if key == "length_angle":
            set_parameter_value(chamfer, "Angle", angle)
        elif second_length is not None:
            set_parameter_value(chamfer, "Length2", second_length)

        payload = finish(session, part, chamfer, "chamfer", name)
        payload.update(
            {"length_mm": length, "angle_deg": angle if key == "length_angle" else None,
             "mode": key, "edges": [t.label for t in targets]}
        )
        return result.ok(
            payload,
            message="Chamfered %d edge selection(s)." % len(targets),
            warnings=warnings,
        )

    # ── shell, thickness, draft ──────────────────────────────────────────────

    @tool(
        "catia_shell",
        "Hollow the solid out to a thin wall, removing the named faces to leave the "
        "interior open. Give at least one face token, or the result is a fully closed "
        "hollow body.",
        group="dressup",
    )
    def catia_shell(
        faces_to_remove: Annotated[
            list[str],
            Field(description="Reference tokens of the faces to open, e.g. ['face@0,0,40']."),
        ],
        thickness: Annotated[
            float, Field(description="Wall thickness inwards, in mm.", gt=0)
        ] = 2.0,
        outward_thickness: Annotated[
            float, Field(description="Additional wall thickness outwards, in mm.", ge=0)
        ] = 0.0,
        name: Annotated[str, Field(description="Name for the resulting feature.")] = "",
    ) -> dict:
        ensure_closed(session)
        part = session.active_part()
        factory = session.shape_factory()
        targets = _resolve_all(session, part, faces_to_remove, "face")

        _, shell = comutil.try_variants(
            [
                (
                    "AddNewShell",
                    lambda: factory.AddNewShell(
                        targets[0].reference, float(thickness), float(outward_thickness)
                    ),
                ),
            ],
            what="create a shell",
        )
        warnings: list[str] = []
        for extra in targets[1:]:
            try:
                shell.AddFaceToRemove(extra.reference)
            except Exception as exc:
                warnings.append(
                    "Could not open %s (%s)." % (extra.label, errors.com_message(exc))
                )

        payload = finish(session, part, shell, "shell", name)
        payload.update(
            {"thickness_mm": thickness, "faces_removed": [t.label for t in targets]}
        )
        return result.ok(
            payload,
            message="Shelled to %.3f mm, opening %d face(s)." % (thickness, len(targets)),
            warnings=warnings,
        )

    @tool(
        "catia_thickness",
        "Add or remove material on specific faces by a set thickness, without shelling the "
        "whole solid. A negative value thins the wall.",
        group="dressup",
    )
    def catia_thickness(
        faces: Annotated[list[str], Field(description="Reference tokens of the faces.")],
        thickness: Annotated[
            float, Field(description="Thickness to add in mm; negative removes material.")
        ] = 1.0,
        name: Annotated[str, Field(description="Name for the resulting feature.")] = "",
    ) -> dict:
        ensure_closed(session)
        part = session.active_part()
        factory = session.shape_factory()
        targets = _resolve_all(session, part, faces, "face")
        _, feature = comutil.try_variants(
            [
                (
                    "AddNewThickness",
                    lambda: factory.AddNewThickness(targets[0].reference, float(thickness)),
                ),
            ],
            what="create a thickness feature",
        )
        warnings: list[str] = []
        for extra in targets[1:]:
            try:
                feature.AddFaceToThickness(extra.reference)
            except Exception as exc:
                warnings.append("Could not add %s (%s)." % (extra.label, exc))
        payload = finish(session, part, feature, "thickness", name)
        payload["thickness_mm"] = thickness
        return result.ok(payload, message="Applied %.3f mm thickness." % thickness,
                         warnings=warnings)

    @tool(
        "catia_draft",
        "Apply a draft angle to faces so a moulded or cast part can be released from the "
        "tool. Needs the faces to draft, a neutral element the draft pivots about (usually "
        "a planar face or plane), and a pulling direction.",
        group="dressup",
    )
    def catia_draft(
        faces: Annotated[list[str], Field(description="Reference tokens of the faces to draft.")],
        neutral_element: Annotated[
            str,
            Field(
                description=(
                    "Reference token of the neutral face or plane the draft pivots about, "
                    "e.g. 'face@0,0,0' or 'xy'."
                )
            ),
        ],
        angle: Annotated[float, Field(description="Draft angle in degrees.", gt=0, lt=90)] = 3.0,
        pulling_direction: Annotated[
            str,
            Field(
                description=(
                    "Reference token for the pull direction; usually the same plane as the "
                    "neutral element. Defaults to the neutral element."
                )
            ),
        ] = "",
        name: Annotated[str, Field(description="Name for the resulting feature.")] = "",
    ) -> dict:
        ensure_closed(session)
        part = session.active_part()
        factory = session.shape_factory()
        targets = _resolve_all(session, part, faces, "face")
        neutral = refs.resolve(session, neutral_element, part=part)
        pulling = (
            refs.resolve(session, pulling_direction, part=part) if pulling_direction else neutral
        )

        multiselect = constants.const("catNoneDraftMultiselectionMode")
        draft_mode = constants.const("catStandardDraftMode")

        _, draft = comutil.try_variants(
            [
                (
                    "AddNewDraft(7)",
                    lambda: factory.AddNewDraft(
                        targets[0].reference,
                        neutral.reference,
                        multiselect,
                        pulling.reference,
                        draft_mode,
                        float(angle),
                        constants.const("catNoneDraftMultiselectionMode"),
                    ),
                ),
                (
                    "AddNewDraft(6)",
                    lambda: factory.AddNewDraft(
                        targets[0].reference,
                        neutral.reference,
                        multiselect,
                        pulling.reference,
                        draft_mode,
                        float(angle),
                    ),
                ),
                (
                    "AddNewDraftAngle",
                    lambda: factory.AddNewDraftAngle(
                        targets[0].reference,
                        neutral.reference,
                        pulling.reference,
                        float(angle),
                    ),
                ),
            ],
            what="create a draft",
        )

        warnings: list[str] = []
        for extra in targets[1:]:
            try:
                draft.AddFaceToDraft(extra.reference)
            except Exception as exc:
                warnings.append("Could not add %s to the draft (%s)." % (extra.label, exc))
        set_parameter_value(draft, "DraftAngle", angle)

        payload = finish(session, part, draft, "draft", name)
        payload.update(
            {
                "angle_deg": angle,
                "faces": [t.label for t in targets],
                "neutral_element": neutral.label,
            }
        )
        return result.ok(payload, message="Applied a %.2f degree draft." % angle,
                         warnings=warnings)

    @tool(
        "catia_thread",
        "Add a thread or tap to a cylindrical face. The lateral face is the cylinder to "
        "thread and the limit face is the flat face the thread starts from.",
        group="dressup",
    )
    def catia_thread(
        lateral_face: Annotated[
            str, Field(description="Reference token of the cylindrical face to thread.")
        ],
        limit_face: Annotated[
            str, Field(description="Reference token of the face the thread starts from.")
        ],
        diameter: Annotated[
            float | None, Field(description="Thread diameter in mm; omit to keep CATIA's default.")
        ] = None,
        pitch: Annotated[float | None, Field(description="Thread pitch in mm.")] = None,
        depth: Annotated[float | None, Field(description="Threaded depth in mm.")] = None,
        right_handed: Annotated[bool, Field(description="Right-hand thread.")] = True,
        name: Annotated[str, Field(description="Name for the resulting feature.")] = "",
    ) -> dict:
        ensure_closed(session)
        part = session.active_part()
        factory = session.shape_factory()
        lateral = refs.resolve(session, lateral_face, part=part)
        limit = refs.resolve(session, limit_face, part=part)

        _, thread = comutil.try_variants(
            [
                (
                    "AddNewThread",
                    lambda: factory.AddNewThread(lateral.reference, limit.reference),
                ),
                (
                    "AddNewThreadWithParameters",
                    lambda: factory.AddNewThreadWithParameters(
                        lateral.reference, limit.reference
                    ),
                ),
            ],
            what="create a thread",
        )

        warnings: list[str] = []
        for member, value in (
            ("ThreadDiameter", diameter),
            ("Pitch", pitch),
            ("ThreadDepth", depth),
        ):
            if value is not None and not set_parameter_value(thread, member, value):
                warnings.append("Could not set %s." % member)
        if not right_handed:
            try:
                thread.RightThreadedOrNot = False
            except Exception:
                warnings.append("Could not set a left-hand thread on this release.")

        payload = finish(session, part, thread, "thread", name)
        return result.ok(payload, message="Created a thread.", warnings=warnings)

    @tool(
        "catia_remove_face",
        "Delete faces from the solid and let CATIA heal the result by extending the "
        "neighbouring faces - the Remove Face feature. Useful for simplifying a model "
        "before analysis or export.",
        destructive=True,
        group="dressup",
    )
    def catia_remove_face(
        faces_to_remove: Annotated[
            list[str], Field(description="Reference tokens of the faces to delete.")
        ],
        faces_to_keep: Annotated[
            list[str] | None,
            Field(description="Optional reference tokens of faces that must survive intact."),
        ] = None,
        name: Annotated[str, Field(description="Name for the resulting feature.")] = "",
    ) -> dict:
        ensure_closed(session)
        part = session.active_part()
        factory = session.shape_factory()
        removed = _resolve_all(session, part, faces_to_remove, "face")
        kept = [refs.resolve(session, token, part=part) for token in faces_to_keep or []]

        _, feature = comutil.try_variants(
            [
                (
                    "AddNewRemoveFace(2)",
                    lambda: factory.AddNewRemoveFace(
                        removed[0].reference, kept[0].reference if kept else None
                    ),
                ),
                (
                    "AddNewRemoveFace(1)",
                    lambda: factory.AddNewRemoveFace(removed[0].reference),
                ),
            ],
            what="remove faces",
        )
        warnings: list[str] = []
        for extra in removed[1:]:
            try:
                feature.AddFaceToRemove(extra.reference)
            except Exception as exc:
                warnings.append("Could not also remove %s (%s)." % (extra.label, exc))

        payload = finish(session, part, feature, "remove_face", name)
        payload["faces_removed"] = [t.label for t in removed]
        return result.ok(payload, message="Removed %d face(s)." % len(removed),
                         warnings=warnings)
