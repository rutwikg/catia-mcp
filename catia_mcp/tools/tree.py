"""Inspecting and selecting what is in the document.

These are the tools a model should reach for before any modelling call: they
turn an opaque CATIA document into names, indices and reference tokens it can
feed back into the geometry tools.
"""

from __future__ import annotations

import fnmatch
import logging
from typing import Annotated, Any

from pydantic import Field

from catia_mcp.core import comutil, errors, refs, result
from catia_mcp.tools.base import registrar

logger = logging.getLogger("catia_mcp.tools.tree")

SHOW = 0
NO_SHOW = 1


def _types_for(session: Any, objects: list[Any]) -> dict[int, str]:
    """Ask CATIA for the type name of several objects in one selection pass.

    ``Selection.Item(n).Type`` is the only route to a CATIA type name from
    automation. Doing it one object at a time costs three round trips each, so
    they are batched - tracking which additions actually succeeded, because a
    failed ``Add`` would otherwise shift every later index.
    """
    if not objects:
        return {}
    try:
        selection = session.selection()
        selection.Clear()
    except Exception:
        return {}

    positions: list[int] = []
    for index, obj in enumerate(objects):
        try:
            selection.Add(obj)
            positions.append(index)
        except Exception:
            continue

    types: dict[int, str] = {}
    for slot, original_index in enumerate(positions, start=1):
        try:
            types[original_index] = str(selection.Item(slot).Type or "")
        except Exception:
            continue
    try:
        selection.Clear()
    except Exception:
        pass
    return types


def _child_nodes(part: Any, node: Any, kind: str) -> list[tuple[str, Any]]:
    """Return (relationship, object) pairs beneath a tree node."""
    out: list[tuple[str, Any]] = []
    if kind == "part":
        for label, member in (
            ("body", "Bodies"),
            ("geometrical_set", "HybridBodies"),
            ("axis_system", "AxisSystems"),
            ("ordered_geometrical_set", "OrderedGeometricalSets"),
        ):
            for child in comutil.com_iter(comutil.safe(node, member)):
                out.append((label, child))
    elif kind == "body":
        for label, member in (("sketch", "Sketches"), ("feature", "Shapes")):
            for child in comutil.com_iter(comutil.safe(node, member)):
                out.append((label, child))
    elif kind in ("geometrical_set", "ordered_geometrical_set"):
        for label, member in (
            ("geometry", "HybridShapes"),
            ("sketch", "HybridSketches"),
            ("geometrical_set", "HybridBodies"),
        ):
            for child in comutil.com_iter(comutil.safe(node, member)):
                out.append((label, child))
    return out


def register(mcp: Any, session: Any) -> None:
    tool = registrar(mcp, session)

    # ── structure ────────────────────────────────────────────────────────────

    @tool(
        "catia_describe_tree",
        "Walk the specification tree of the active document and return it as nested JSON: "
        "bodies, geometrical sets, sketches, features and - for assemblies - the component "
        "hierarchy. Every node carries a reference token you can pass straight to the "
        "modelling tools. Start here when you do not know what is in the document.",
        readonly=True,
        group="tree",
    )
    def catia_describe_tree(
        max_depth: Annotated[
            int, Field(description="How deep to recurse.", ge=1, le=12)
        ] = 5,
        include_types: Annotated[
            bool,
            Field(
                description=(
                    "Include each element's CATIA type name. Accurate but costs an extra "
                    "selection pass per level; turn off for very large models."
                )
            ),
        ] = True,
        max_children: Annotated[
            int, Field(description="Cap on children reported per node.", ge=1, le=2000)
        ] = 200,
    ) -> dict:
        doc = session.active_document()
        kind = session.document_kind(doc)

        if kind == "product":
            root = doc.Product
            tree = _describe_product(session, root, max_depth, 0, include_types, max_children)
            return result.ok({"document_kind": kind, "tree": tree})

        if kind != "part":
            raise errors.WrongDocumentTypeError(
                "catia_describe_tree understands Part and Product documents; the active "
                "document is a %s document." % kind
            )

        part = doc.Part
        tree = _describe_node(
            session, part, part, "part", max_depth, 0, include_types, max_children
        )
        return result.ok(
            {"document_kind": kind, "tree": tree},
            hint=(
                "Use the reference_token values with the modelling tools. For faces and "
                "edges, which are not in the tree, call catia_list_faces / catia_list_edges."
            ),
        )

    def _describe_node(
        sess: Any,
        part: Any,
        node: Any,
        kind: str,
        max_depth: int,
        depth: int,
        include_types: bool,
        max_children: int,
    ) -> dict[str, Any]:
        name = comutil.name_of(node)
        payload: dict[str, Any] = {
            "name": name,
            "node_kind": kind,
            "reference_token": "name:%s" % name if name else None,
        }
        if depth >= max_depth:
            payload["truncated"] = True
            return payload

        children = _child_nodes(part, node, kind)[:max_children]
        if not children:
            return payload

        types = _types_for(sess, [obj for _, obj in children]) if include_types else {}
        rendered: list[dict[str, Any]] = []
        for index, (label, obj) in enumerate(children):
            child = _describe_node(
                sess, part, obj, label, max_depth, depth + 1, include_types, max_children
            )
            if index in types:
                child["catia_type"] = types[index]
            rendered.append(child)
        payload["children"] = rendered
        return payload

    def _describe_product(
        sess: Any,
        product: Any,
        max_depth: int,
        depth: int,
        include_types: bool,
        max_children: int,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": comutil.name_of(product),
            "node_kind": "component",
            "part_number": str(comutil.safe(product, "PartNumber", "") or ""),
            "reference_token": "name:%s" % comutil.name_of(product),
        }
        if depth >= max_depth:
            payload["truncated"] = True
            return payload
        children = list(comutil.com_iter(comutil.safe(product, "Products")))[:max_children]
        if children:
            payload["children"] = [
                _describe_product(sess, c, max_depth, depth + 1, include_types, max_children)
                for c in children
            ]
        return payload

    @tool(
        "catia_list_bodies",
        "List the solid bodies in the active part, flagging which one is the main body and "
        "which is currently in work (the target of new features).",
        readonly=True,
        group="tree",
    )
    def catia_list_bodies() -> dict:
        part = session.active_part()
        main = comutil.name_of(comutil.safe(part, "MainBody"))
        in_work = comutil.name_of(comutil.safe(part, "InWorkObject"))
        bodies = []
        for body in comutil.com_iter(comutil.safe(part, "Bodies")):
            name = comutil.name_of(body)
            bodies.append(
                {
                    "name": name,
                    "reference_token": "body:%s" % name,
                    "is_main_body": name == main,
                    "is_in_work": name == in_work,
                    "features": comutil.com_count(comutil.safe(body, "Shapes")),
                    "sketches": comutil.com_count(comutil.safe(body, "Sketches")),
                }
            )
        return result.ok({"count": len(bodies), "in_work_object": in_work, "bodies": bodies})

    @tool(
        "catia_set_active_body",
        "Choose which body (or geometrical set) new features are added to, by setting "
        "CATIA's in-work object. Everything created afterwards lands there.",
        idempotent=True,
        group="tree",
    )
    def catia_set_active_body(
        name: Annotated[
            str, Field(description="Body or geometrical set name, e.g. 'PartBody' or 'Body.2'.")
        ],
    ) -> dict:
        part = session.active_part()
        target = None
        for member in ("Bodies", "HybridBodies", "OrderedGeometricalSets"):
            collection = comutil.safe(part, member)
            candidate = comutil.safe_call(collection, "Item", name) if collection else None
            if candidate is not None:
                target = candidate
                break
        if target is None:
            target = refs.find_named(session, name)
        part.InWorkObject = target
        return result.ok(
            {"in_work_object": comutil.name_of(target)},
            message="New features will now go into %r." % comutil.name_of(target),
        )

    @tool(
        "catia_list_features",
        "List the features of a body in creation order, optionally reporting which ones "
        "CATIA currently considers to be in error - this is how you find the feature "
        "blocking an update. Note that on releases with no update-status property, "
        "checking errors works by asking CATIA to recompute each feature, which is "
        "harmless but not free on a large part.",
        readonly=True,
        group="tree",
    )
    def catia_list_features(
        body: Annotated[
            str, Field(description="Body name. Defaults to the main body.")
        ] = "",
        include_errors: Annotated[
            bool,
            Field(
                description=(
                    "Check each feature's update status. Costs one extra call per feature, "
                    "and may trigger a recompute of features CATIA has not evaluated yet."
                )
            ),
        ] = True,
    ) -> dict:
        part = session.active_part()
        if body:
            target = comutil.safe_call(comutil.safe(part, "Bodies"), "Item", body)
            if target is None:
                raise errors.ElementNotFoundError("No body called %r." % body)
        else:
            target = comutil.safe(part, "MainBody")
            if target is None:
                raise errors.ElementNotFoundError("This part has no main body.")

        shapes = list(comutil.com_iter(comutil.safe(target, "Shapes")))
        types = _types_for(session, shapes)
        features = []
        errored = 0
        for index, shape in enumerate(shapes):
            name = comutil.name_of(shape)
            entry: dict[str, Any] = {
                "order": index + 1,
                "name": name,
                "reference_token": "name:%s" % name,
                "catia_type": types.get(index, ""),
            }
            if include_errors:
                status = _update_status(part, shape)
                entry["update_status"] = status
                if status == "error":
                    errored += 1
            features.append(entry)

        sketches = [
            {"name": comutil.name_of(s), "reference_token": "sketch:%s" % comutil.name_of(s)}
            for s in comutil.com_iter(comutil.safe(target, "Sketches"))
        ]
        payload = {
            "body": comutil.name_of(target),
            "feature_count": len(features),
            "features": features,
            "sketches": sketches,
        }
        hint = ""
        if errored:
            hint = (
                "%d feature(s) are in error. Fix or delete them before adding more geometry - "
                "CATIA will refuse to update the part otherwise." % errored
            )
        return result.ok(payload, hint=hint)

    # ── topology ─────────────────────────────────────────────────────────────

    def _topology_tool(kind: str, plural: str, noun: str, extra_hint: str):
        @tool(
            "catia_list_%s" % plural,
            "List the %ss of the visible solid geometry in the active document, with the "
            "index token (%s#n), the centre of gravity to aim proximity tokens at%s. "
            "Indices change whenever the model changes; centroids do not, so prefer "
            "'%s@x,y,z' tokens for anything you will reference more than once."
            % (noun, kind, extra_hint, kind),
            readonly=True,
            group="tree",
        )
        def lister(
            limit: Annotated[
                int, Field(description="Maximum number to return.", ge=1, le=1000)
            ] = 200,
            include_metrics: Annotated[
                bool,
                Field(
                    description=(
                        "Measure each element (centroid, area or length). Accurate and "
                        "usually what you want, but one measurement call each - turn it "
                        "off on very large models."
                    )
                ),
            ] = True,
        ) -> dict:
            entries = refs.topology_entries(
                session, kind, with_metrics=include_metrics, limit=limit
            )
            cleaned = [
                {k: v for k, v in entry.items() if not k.startswith("_")} for entry in entries
            ]
            return result.ok(
                {"kind": kind, "count": len(cleaned), plural: cleaned},
                hint=(
                    "Nothing found usually means the solid is hidden - Selection.Search "
                    "skips hidden geometry. Use catia_show_element first."
                )
                if not cleaned
                else "",
            )

        lister.__name__ = "catia_list_%s" % plural
        return lister

    _topology_tool(
        "face", "faces", "face", ", the area, and the outward normal for planar faces"
    )
    _topology_tool("edge", "edges", "edge", " and the length")
    _topology_tool("vertex", "vertices", "vertex", " and the exact position")

    @tool(
        "catia_resolve_reference",
        "Check what a reference token actually points at before you use it in a modelling "
        "call. Returns the resolved element's name, type and - for topology - its "
        "measured position, so you can confirm you are about to fillet the right edge.",
        readonly=True,
        group="tree",
    )
    def catia_resolve_reference(
        token: Annotated[
            str,
            Field(
                description=(
                    "Reference token, e.g. 'xy', 'name:Pad.1', 'face#3', 'edge@10,0,5'. "
                    "See catia_reference_help."
                )
            ),
        ],
    ) -> dict:
        resolved = refs.resolve(session, token)
        types = _types_for(session, [resolved.obj])
        payload: dict[str, Any] = {
            "token": token,
            "resolved_to": resolved.label,
            "kind": resolved.kind,
            "name": comutil.name_of(resolved.obj),
            "catia_type": types.get(0, ""),
        }
        if resolved.kind in ("face", "edge", "vertex") and resolved.reference is not None:
            try:
                measurable = session.measurable(resolved.reference)
                payload["measurements"] = _measure_summary(session, measurable, resolved.kind)
            except errors.CatiaError:
                pass
        return result.ok(payload)

    @tool(
        "catia_find_elements",
        "Search the active document for elements whose name matches a pattern "
        "(shell-style wildcards, e.g. 'Pad.*' or '*Hole*'). Returns reference tokens for "
        "every match.",
        readonly=True,
        group="tree",
    )
    def catia_find_elements(
        pattern: Annotated[
            str, Field(description="Name pattern with * and ? wildcards, e.g. 'Sketch.*'.")
        ],
        limit: Annotated[int, Field(description="Maximum matches.", ge=1, le=500)] = 100,
    ) -> dict:
        doc = session.active_document()
        part = comutil.safe(doc, "Part")
        if part is None:
            raise errors.WrongDocumentTypeError("catia_find_elements needs a Part document.")

        matches: list[dict[str, Any]] = []
        seen: set[str] = set()

        def visit(node: Any, kind: str, depth: int) -> None:
            if len(matches) >= limit or depth > 8:
                return
            for label, child in _child_nodes(part, node, kind):
                name = comutil.name_of(child)
                if name and name not in seen and fnmatch.fnmatch(name, pattern):
                    seen.add(name)
                    matches.append(
                        {"name": name, "node_kind": label, "reference_token": "name:%s" % name}
                    )
                    if len(matches) >= limit:
                        return
                visit(child, label, depth + 1)

        visit(part, "part", 0)
        return result.ok(
            {"pattern": pattern, "count": len(matches), "matches": matches},
            hint="Names are case-sensitive and match CATIA's tree exactly." if not matches else "",
        )

    # ── visibility, naming, deletion ─────────────────────────────────────────

    @tool(
        "catia_show_element",
        "Make an element visible in the 3D view. Also the fix when catia_list_faces or "
        "catia_list_edges returns nothing: CATIA's search skips hidden geometry.",
        idempotent=True,
        group="tree",
    )
    def catia_show_element(
        token: Annotated[str, Field(description="Reference token of the element to show.")],
    ) -> dict:
        return _set_visibility(session, token, SHOW)

    @tool(
        "catia_hide_element",
        "Hide an element in the 3D view without deleting it.",
        idempotent=True,
        group="tree",
    )
    def catia_hide_element(
        token: Annotated[str, Field(description="Reference token of the element to hide.")],
    ) -> dict:
        return _set_visibility(session, token, NO_SHOW)

    @tool(
        "catia_rename_element",
        "Rename a tree element. Worth doing for anything you will reference later - a "
        "stable name survives model edits, unlike a face or edge index.",
        group="tree",
    )
    def catia_rename_element(
        token: Annotated[str, Field(description="Reference token of the element to rename.")],
        new_name: Annotated[str, Field(description="The new name.")],
    ) -> dict:
        resolved = refs.resolve(session, token)
        old = comutil.name_of(resolved.obj)
        try:
            resolved.obj.Name = new_name
        except Exception as exc:
            raise errors.OperationFailedError(
                "CATIA rejected the name %r: %s" % (new_name, errors.com_message(exc)),
                remediation="CATIA names must be unique in the document and cannot be empty.",
            ) from exc
        if session.state.last_feature_name == old:
            session.state.last_feature_name = new_name
        if session.state.last_sketch_name == old:
            session.state.last_sketch_name = new_name
        return result.ok(
            {"old_name": old, "new_name": comutil.name_of(resolved.obj)},
            message="Renamed %s to %s." % (old, new_name),
        )

    @tool(
        "catia_delete_element",
        "Delete a tree element. Deleting a feature that later features depend on will put "
        "those into error, so check catia_list_features afterwards.",
        destructive=True,
        group="tree",
    )
    def catia_delete_element(
        token: Annotated[str, Field(description="Reference token of the element to delete.")],
    ) -> dict:
        resolved = refs.resolve(session, token)
        name = comutil.name_of(resolved.obj)
        selection = session.selection()
        selection.Clear()
        selection.Add(resolved.obj)
        selection.Delete()
        selection.Clear()
        if session.state.last_feature_name == name:
            session.state.last_feature_name = ""
        if session.state.last_sketch_name == name:
            session.state.last_sketch_name = ""
        return result.ok(
            {"deleted": name},
            message="Deleted %s." % name,
            hint="Run catia_list_features with include_errors=true to check for fallout.",
        )

    @tool(
        "catia_select_elements",
        "Highlight elements in the CATIA window. Purely visual - useful to show a person "
        "at the workstation which geometry you are about to modify.",
        group="tree",
    )
    def catia_select_elements(
        tokens: Annotated[
            list[str], Field(description="Reference tokens to highlight together.")
        ],
        replace: Annotated[
            bool, Field(description="Clear the existing selection first.")
        ] = True,
    ) -> dict:
        selection = session.selection()
        if replace:
            selection.Clear()
        added: list[str] = []
        failed: list[dict[str, str]] = []
        for token in tokens:
            try:
                resolved = refs.resolve(session, token)
                selection.Add(resolved.obj)
                added.append(resolved.label)
            except errors.CatiaError as exc:
                failed.append({"token": token, "error": exc.message})
        return result.ok(
            {"selected": added, "failed": failed, "selection_count": comutil.com_count(selection)},
            message="Highlighted %d element(s)." % len(added),
        )

    @tool(
        "catia_update",
        "Force CATIA to recompute the active part or assembly. Call this after a batch of "
        "edits, or to surface which feature is in error.",
        idempotent=True,
        group="tree",
    )
    def catia_update() -> dict:
        doc = session.active_document()
        kind = session.document_kind(doc)
        if kind == "part":
            session.update_part(doc.Part)
        elif kind == "product":
            try:
                doc.Product.Update()
            except Exception as exc:
                raise errors.OperationFailedError(
                    "Assembly update failed: %s" % errors.com_message(exc)
                ) from exc
        else:
            raise errors.WrongDocumentTypeError(
                "Only Part and Product documents can be updated; this is a %s document." % kind
            )
        session.refresh_view()
        return result.ok({"document_kind": kind}, message="Update completed with no errors.")


def _update_status(part: Any, shape: Any) -> str:
    """Best-effort read of whether a feature is currently in error."""
    for member in ("get_UpdateError", "UpdateError"):
        value = comutil.safe(shape, member)
        if value is not None:
            try:
                return "error" if bool(value) else "ok"
            except Exception:
                pass
    # No direct property on this release. Asking the part to update just this
    # object is the only reliable probe: it is a no-op for a feature that is
    # already computed, and raises for one that is in error.
    try:
        part.UpdateObject(shape)
        return "ok"
    except Exception:
        return "error"


def _set_visibility(session: Any, token: str, state: int) -> dict[str, Any]:
    resolved = refs.resolve(session, token)
    selection = session.selection()
    selection.Clear()
    selection.Add(resolved.obj)
    try:
        selection.VisProperties.SetShow(state)
    except Exception as exc:
        selection.Clear()
        raise errors.OperationFailedError(
            "Could not change visibility: %s" % errors.com_message(exc)
        ) from exc
    selection.Clear()
    session.refresh_view()
    return result.ok(
        {"element": resolved.label, "visible": state == SHOW},
        message="%s is now %s." % (resolved.label, "visible" if state == SHOW else "hidden"),
    )


def _measure_summary(session: Any, measurable: Any, kind: str) -> dict[str, Any]:
    app = session.app
    out: dict[str, Any] = {}
    try:
        cog = comutil.out_doubles(measurable, "GetCOG", 3, app=app)
        out["centroid"] = result.round_xyz(cog)
    except Exception:
        pass
    if kind == "face":
        area = comutil.safe(measurable, "Area")
        if area is not None:
            out["area_mm2"] = round(float(area), 4)
    elif kind == "edge":
        length = comutil.safe(measurable, "Length")
        if length is not None:
            out["length_mm"] = round(float(length), 4)
    return out
