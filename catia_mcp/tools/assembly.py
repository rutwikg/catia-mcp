"""Assembly (Product) structure, positioning and constraints.

Assembly constraints need references expressed in the *product* context, not
the part's own. CATIA's ``Selection`` returns exactly that, which is why the
``face#n`` and ``face@x,y,z`` tokens work unchanged inside a CATProduct - each
entry from :func:`catia_mcp.core.refs.topology_entries` also reports which
component it belongs to.
"""

from __future__ import annotations

import logging
import math
import os
from typing import Annotated, Any

from pydantic import Field

from catia_mcp.core import comutil, constants, errors, refs, result
from catia_mcp.tools.base import registrar, rename

logger = logging.getLogger("catia_mcp.tools.assembly")

CONSTRAINT_KINDS: dict[str, dict[str, Any]] = {
    "coincidence": {"const": "catCstTypeReference", "elements": 2, "value": False},
    "contact": {"const": "catCstTypeSurfContact", "elements": 2, "value": False},
    "offset": {"const": "catCstTypeDistance", "elements": 2, "value": True, "unit": "mm"},
    "distance": {"const": "catCstTypeDistance", "elements": 2, "value": True, "unit": "mm"},
    "angle": {"const": "catCstTypeAngle", "elements": 2, "value": True, "unit": "deg"},
    "planar_angle": {"const": "catCstTypePlanarAngle", "elements": 2, "value": True,
                     "unit": "deg"},
    "parallel": {"const": "catCstTypeParallelism", "elements": 2, "value": False},
    "perpendicular": {"const": "catCstTypePerpendicularity", "elements": 2, "value": False},
    "fix": {"const": "catCstTypeReference", "elements": 1, "value": False},
}


def _constraints_collection(product: Any) -> Any:
    collection = comutil.safe_call(product, "Connections", "CATIAConstraints")
    if collection is None:
        collection = comutil.safe(product, "Constraints")
    if collection is None:
        raise errors.UnsupportedCapabilityError(
            "This product exposes no constraints collection."
        )
    return collection


def _find_component(product: Any, name: str) -> Any:
    """Find a component by instance name or part number, at any depth."""
    target = name.strip().lower()

    def walk(node: Any, depth: int) -> Any:
        if depth > 12:
            return None
        for child in comutil.com_iter(comutil.safe(node, "Products")):
            if comutil.name_of(child).lower() == target:
                return child
            if str(comutil.safe(child, "PartNumber", "") or "").lower() == target:
                return child
            found = walk(child, depth + 1)
            if found is not None:
                return found
        return None

    found = walk(product, 0)
    if found is None:
        raise errors.ElementNotFoundError(
            "No component called %r in the assembly. Use catia_list_components to see the "
            "instance names." % name
        )
    return found


def _position_matrix(session: Any, component: Any) -> list[float] | None:
    """Read the 12-element placement matrix (3x3 rotation + translation)."""
    position = comutil.safe(component, "Position")
    if position is None:
        return None
    try:
        return comutil.out_doubles(position, "GetComponents", 12, app=session.app)
    except Exception as exc:
        logger.info("Could not read the position matrix: %s", exc)
        return None


def _decompose(matrix: list[float]) -> dict[str, Any]:
    """Split CATIA's 12-value placement into translation and Euler angles."""
    rotation = matrix[:9]
    translation = matrix[9:12]
    # CATIA lists the rotation one axis at a time - [Xx,Xy,Xz, Yx,Yy,Yz, Zx,Zy,Zz] -
    # so each axis is a *column* of the matrix and element (row, col) is at col*3+row.
    m00, m10, m20 = rotation[0], rotation[1], rotation[2]
    m11, m21 = rotation[4], rotation[5]
    m12, m22 = rotation[7], rotation[8]

    # Inverse of R = Rz(rz) * Ry(ry) * Rx(rx), matching _euler_matrix.
    sy = math.hypot(m00, m10)
    if sy > 1e-9:
        rx = math.degrees(math.atan2(m21, m22))
        ry = math.degrees(math.atan2(-m20, sy))
        rz = math.degrees(math.atan2(m10, m00))
    else:  # gimbal lock: rx and rz are not separable, so fold everything into rx
        rx = math.degrees(math.atan2(-m12, m11))
        ry = math.degrees(math.atan2(-m20, sy))
        rz = 0.0

    return {
        "translation_mm": {
            "x": round(translation[0], 4),
            "y": round(translation[1], 4),
            "z": round(translation[2], 4),
        },
        "rotation_deg": {"rx": round(rx, 4), "ry": round(ry, 4), "rz": round(rz, 4)},
        "matrix": [round(v, 6) for v in matrix],
    }


def _euler_matrix(rx: float, ry: float, rz: float) -> list[float]:
    """Build CATIA's column-major 3x3 rotation from XYZ Euler angles in degrees."""
    a, b, c = math.radians(rx), math.radians(ry), math.radians(rz)
    ca, sa = math.cos(a), math.sin(a)
    cb, sb = math.cos(b), math.sin(b)
    cc, sc = math.cos(c), math.sin(c)
    # R = Rz * Ry * Rx, then flattened column by column.
    m = [
        [cc * cb, cc * sb * sa - sc * ca, cc * sb * ca + sc * sa],
        [sc * cb, sc * sb * sa + cc * ca, sc * sb * ca - cc * sa],
        [-sb, cb * sa, cb * ca],
    ]
    return [
        m[0][0], m[1][0], m[2][0],
        m[0][1], m[1][1], m[2][1],
        m[0][2], m[1][2], m[2][2],
    ]


def register(mcp: Any, session: Any) -> None:
    tool = registrar(mcp, session)

    # ── structure ────────────────────────────────────────────────────────────

    @tool(
        "catia_add_component",
        "Insert one or more existing CATPart or CATProduct files into the active assembly.",
        group="assembly",
    )
    def catia_add_component(
        paths: Annotated[
            list[str], Field(description="Absolute paths of the files to insert.")
        ],
        parent: Annotated[
            str,
            Field(description="Instance name of the sub-product to insert into. Defaults to the root."),
        ] = "",
    ) -> dict:
        product = session.active_product()
        target = _find_component(product, parent) if parent else product
        resolved: list[str] = []
        for path in paths:
            full = os.path.abspath(os.path.expanduser(path))
            if not os.path.exists(full):
                raise errors.InvalidArgumentError("No such file: %s" % full)
            resolved.append(full)

        before = {comutil.name_of(p) for p in comutil.com_iter(target.Products)}
        _, _ = comutil.try_variants(
            [
                (
                    "AddComponentsFromFiles",
                    lambda: target.Products.AddComponentsFromFiles(resolved, "All"),
                ),
                (
                    "AddComponentsFromFiles(tuple)",
                    lambda: target.Products.AddComponentsFromFiles(tuple(resolved), "All"),
                ),
            ],
            what="insert components from files",
        )
        after = [
            comutil.name_of(p)
            for p in comutil.com_iter(target.Products)
            if comutil.name_of(p) not in before
        ]
        session.active_document().Product.Update()
        return result.ok(
            {
                "inserted": after,
                "files": resolved,
                "parent": comutil.name_of(target),
                "component_count": comutil.com_count(target.Products),
            },
            message="Inserted %d component(s)." % len(resolved),
            hint="Position them with catia_move_component or constrain them with "
                 "catia_assembly_constraint.",
        )

    @tool(
        "catia_add_new_part",
        "Create a brand-new empty part directly inside the active assembly.",
        group="assembly",
    )
    def catia_add_new_part(
        name: Annotated[str, Field(description="Instance and part number for the new part.")] = "",
        parent: Annotated[str, Field(description="Sub-product to add it to.")] = "",
    ) -> dict:
        return _add_new(session, "Part", name, parent)

    @tool(
        "catia_add_new_product",
        "Create a new empty sub-assembly inside the active assembly.",
        group="assembly",
    )
    def catia_add_new_product(
        name: Annotated[str, Field(description="Instance and part number for the sub-assembly.")] = "",
        parent: Annotated[str, Field(description="Sub-product to add it to.")] = "",
    ) -> dict:
        return _add_new(session, "Product", name, parent)

    def _add_new(sess: Any, kind: str, name: str, parent: str) -> dict:
        product = sess.active_product()
        target = _find_component(product, parent) if parent else product
        _, component = comutil.try_variants(
            [
                ("AddNewComponent", lambda: target.Products.AddNewComponent(kind)),
                ("AddNewProduct", lambda: target.Products.AddNewProduct(name or kind)),
            ],
            what="create a new %s in the assembly" % kind.lower(),
        )
        final = rename(component, name)
        if name:
            try:
                component.PartNumber = name
            except Exception:
                pass
        return result.ok(
            {
                "created": final,
                "part_number": str(comutil.safe(component, "PartNumber", "") or ""),
                "parent": comutil.name_of(target),
            },
            message="Added a new %s to the assembly." % kind.lower(),
        )

    @tool(
        "catia_list_components",
        "List the components of the active assembly, with instance names, part numbers, "
        "source files and current positions.",
        readonly=True,
        group="assembly",
    )
    def catia_list_components(
        recursive: Annotated[bool, Field(description="Include sub-assembly contents.")] = True,
        include_position: Annotated[
            bool, Field(description="Read each component's placement matrix.")
        ] = False,
        max_depth: Annotated[int, Field(description="Recursion depth.", ge=1, le=12)] = 8,
    ) -> dict:
        product = session.active_product()

        def walk(node: Any, depth: int) -> list[dict[str, Any]]:
            entries: list[dict[str, Any]] = []
            for child in comutil.com_iter(comutil.safe(node, "Products")):
                entry: dict[str, Any] = {
                    "instance_name": comutil.name_of(child),
                    "part_number": str(comutil.safe(child, "PartNumber", "") or ""),
                    "reference_token": "name:%s" % comutil.name_of(child),
                    "depth": depth,
                }
                reference = comutil.safe(child, "ReferenceProduct")
                doc = comutil.safe(reference, "Parent") if reference is not None else None
                path = str(comutil.safe(doc, "FullName", "") or "")
                if path:
                    entry["file"] = path
                if include_position:
                    matrix = _position_matrix(session, child)
                    if matrix:
                        entry["position"] = _decompose(matrix)
                children = comutil.com_count(comutil.safe(child, "Products"))
                entry["child_count"] = children
                if recursive and children and depth < max_depth:
                    entry["children"] = walk(child, depth + 1)
                entries.append(entry)
            return entries

        components = walk(product, 1)
        return result.ok(
            {
                "root": comutil.name_of(product),
                "part_number": str(comutil.safe(product, "PartNumber", "") or ""),
                "count": len(components),
                "components": components,
            }
        )

    @tool(
        "catia_remove_component",
        "Remove a component from the assembly. The underlying file is untouched.",
        destructive=True,
        group="assembly",
    )
    def catia_remove_component(
        instance_name: Annotated[
            str, Field(description="Instance name of the component to remove.")
        ],
    ) -> dict:
        product = session.active_product()
        component = _find_component(product, instance_name)
        name = comutil.name_of(component)
        selection = session.selection()
        selection.Clear()
        selection.Add(component)
        selection.Delete()
        selection.Clear()
        return result.ok({"removed": name}, message="Removed %s from the assembly." % name)

    @tool(
        "catia_component_position",
        "Read a component's placement in the assembly: translation, Euler angles and the "
        "raw 12-value CATIA matrix.",
        readonly=True,
        group="assembly",
    )
    def catia_component_position(
        instance_name: Annotated[str, Field(description="Instance name of the component.")],
    ) -> dict:
        product = session.active_product()
        component = _find_component(product, instance_name)
        matrix = _position_matrix(session, component)
        if matrix is None:
            raise errors.UnsupportedCapabilityError(
                "Could not read the placement matrix for %s." % comutil.name_of(component)
            )
        payload = _decompose(matrix)
        payload["component"] = comutil.name_of(component)
        return result.ok(payload)

    @tool(
        "catia_move_component",
        "Move a component in the assembly. By default the translation and rotation are "
        "applied relative to where it is now; set absolute=true to place it at exactly "
        "that position and orientation instead.",
        group="assembly",
    )
    def catia_move_component(
        instance_name: Annotated[str, Field(description="Instance name of the component.")],
        x: Annotated[float, Field(description="X translation in mm.")] = 0.0,
        y: Annotated[float, Field(description="Y translation in mm.")] = 0.0,
        z: Annotated[float, Field(description="Z translation in mm.")] = 0.0,
        rx: Annotated[float, Field(description="Rotation about X in degrees.")] = 0.0,
        ry: Annotated[float, Field(description="Rotation about Y in degrees.")] = 0.0,
        rz: Annotated[float, Field(description="Rotation about Z in degrees.")] = 0.0,
        absolute: Annotated[
            bool, Field(description="Treat the values as an absolute placement.")
        ] = False,
    ) -> dict:
        product = session.active_product()
        component = _find_component(product, instance_name)
        position = comutil.safe(component, "Position")
        if position is None:
            raise errors.UnsupportedCapabilityError(
                "%s exposes no Position object." % comutil.name_of(component)
            )

        rotation = _euler_matrix(rx, ry, rz)
        if absolute:
            matrix = rotation + [float(x), float(y), float(z)]
        else:
            current = _position_matrix(session, component)
            if current is None:
                raise errors.UnsupportedCapabilityError(
                    "Could not read the current placement, so a relative move is not possible. "
                    "Pass absolute=true with the full placement instead."
                )
            if any(abs(v) > 1e-9 for v in (rx, ry, rz)):
                matrix = _compose(current[:9], rotation) + [
                    current[9] + float(x),
                    current[10] + float(y),
                    current[11] + float(z),
                ]
            else:
                matrix = current[:9] + [
                    current[9] + float(x),
                    current[10] + float(y),
                    current[11] + float(z),
                ]

        try:
            position.SetComponents(comutil.in_doubles(matrix))
        except Exception:
            try:
                position.SetComponents(matrix)
            except Exception as exc:
                raise errors.OperationFailedError(
                    "Could not set the placement: %s" % errors.com_message(exc),
                    remediation=(
                        "The component may be constrained or fixed. Delete the constraint "
                        "with catia_delete_constraint, or move it with a constraint instead."
                    ),
                ) from exc

        session.active_document().Product.Update()
        session.refresh_view()
        updated = _position_matrix(session, component)
        payload: dict[str, Any] = {"component": comutil.name_of(component), "absolute": absolute}
        if updated:
            decomposed = _decompose(updated)
            payload["position"] = decomposed
        return result.ok(payload, message="Moved %s." % comutil.name_of(component))

    # ── constraints ──────────────────────────────────────────────────────────

    @tool(
        "catia_assembly_constraint",
        "Constrain components to each other. Reference the geometry with the usual tokens - "
        "inside an assembly 'face#3' and 'face@x,y,z' resolve against every visible "
        "component, and catia_list_faces reports which component each belongs to. "
        "Dimensional kinds (offset, distance, angle) take a value.",
        group="assembly",
    )
    def catia_assembly_constraint(
        kind: Annotated[
            str,
            Field(
                description=(
                    "coincidence | contact | offset | distance | angle | planar_angle | "
                    "parallel | perpendicular | fix."
                )
            ),
        ],
        elements: Annotated[
            list[str],
            Field(
                description=(
                    "Reference tokens of the geometry to constrain, e.g. "
                    "['face@0,0,10','face@0,0,50']. One element for 'fix', two otherwise."
                )
            ),
        ],
        value: Annotated[
            float | None, Field(description="Offset in mm or angle in degrees, where applicable.")
        ] = None,
        name: Annotated[str, Field(description="Name for the constraint.")] = "",
    ) -> dict:
        key = kind.strip().lower()
        if key not in CONSTRAINT_KINDS:
            raise errors.InvalidArgumentError(
                "Unknown constraint kind %r. Valid: %s"
                % (kind, ", ".join(sorted(CONSTRAINT_KINDS)))
            )
        spec = CONSTRAINT_KINDS[key]
        if len(elements) != spec["elements"]:
            raise errors.InvalidArgumentError(
                "A %s constraint needs %d element(s); %d given."
                % (key, spec["elements"], len(elements))
            )

        product = session.active_product()
        collection = _constraints_collection(product)
        resolved = [refs.resolve(session, token) for token in elements]
        constraint_type = constants.const(spec["const"])

        try:
            if spec["elements"] == 1:
                constraint = collection.AddMonoEltCst(constraint_type, resolved[0].reference)
            else:
                constraint = collection.AddBiEltCst(
                    constraint_type, resolved[0].reference, resolved[1].reference
                )
        except Exception as exc:
            raise errors.OperationFailedError(
                "CATIA rejected the %s constraint: %s" % (key, errors.com_message(exc)),
                remediation=(
                    "Assembly constraints need geometry from two *different* components, "
                    "and the component must not already be over-constrained or fixed. "
                    "Check the 'owner' field in catia_list_faces to confirm which "
                    "component each face belongs to."
                ),
            ) from exc

        warnings: list[str] = []
        applied = None
        if spec["value"] and value is not None:
            dimension = comutil.safe(constraint, "Dimension")
            if dimension is not None:
                try:
                    dimension.Value = float(value)
                    applied = float(value)
                except Exception as exc:
                    warnings.append("Could not set the constraint value (%s)." % exc)
        final = rename(constraint, name)

        try:
            session.active_document().Product.Update()
        except Exception as exc:
            warnings.append(
                "The constraint was created but the assembly did not update: %s"
                % errors.com_message(exc)
            )
        session.refresh_view()

        return result.ok(
            {
                "created": final,
                "kind": key,
                "elements": [r.label for r in resolved],
                "value": applied,
                "unit": spec.get("unit"),
            },
            message="Added a %s constraint." % key,
            warnings=warnings,
        )

    @tool(
        "catia_list_constraints",
        "List the assembly constraints, with their type, value and current status.",
        readonly=True,
        group="assembly",
    )
    def catia_list_constraints() -> dict:
        product = session.active_product()
        collection = _constraints_collection(product)
        entries = []
        for constraint in comutil.com_iter(collection):
            entry: dict[str, Any] = {
                "name": comutil.name_of(constraint),
                "reference_token": "name:%s" % comutil.name_of(constraint),
            }
            type_value = comutil.safe(constraint, "Type")
            if type_value is not None:
                entry["type_code"] = int(type_value)
                entry["type"] = _constraint_label(int(type_value))
            dimension = comutil.safe(constraint, "Dimension")
            if dimension is not None:
                value = comutil.safe(dimension, "Value")
                if value is not None:
                    entry["value"] = round(float(value), 4)
            status = comutil.safe(constraint, "Status")
            if status is not None:
                entry["status_code"] = int(status)
            entries.append(entry)
        return result.ok({"count": len(entries), "constraints": entries})

    @tool(
        "catia_delete_constraint",
        "Delete an assembly constraint by name.",
        destructive=True,
        group="assembly",
    )
    def catia_delete_constraint(
        name: Annotated[str, Field(description="Constraint name, from catia_list_constraints.")],
    ) -> dict:
        product = session.active_product()
        collection = _constraints_collection(product)
        target = None
        for constraint in comutil.com_iter(collection):
            if comutil.name_of(constraint) == name:
                target = constraint
                break
        if target is None:
            raise errors.ElementNotFoundError("No constraint called %r." % name)
        selection = session.selection()
        selection.Clear()
        selection.Add(target)
        selection.Delete()
        selection.Clear()
        return result.ok({"deleted": name}, message="Deleted constraint %s." % name)

    # ── reporting ────────────────────────────────────────────────────────────

    @tool(
        "catia_bill_of_materials",
        "Build a bill of materials for the active assembly: every distinct part number "
        "with its quantity, source file and the instances that use it.",
        readonly=True,
        group="assembly",
    )
    def catia_bill_of_materials(
        max_depth: Annotated[int, Field(description="Recursion depth.", ge=1, le=12)] = 10,
    ) -> dict:
        product = session.active_product()
        rows: dict[str, dict[str, Any]] = {}

        def walk(node: Any, depth: int) -> None:
            if depth > max_depth:
                return
            for child in comutil.com_iter(comutil.safe(node, "Products")):
                part_number = str(comutil.safe(child, "PartNumber", "") or comutil.name_of(child))
                row = rows.setdefault(
                    part_number,
                    {
                        "part_number": part_number,
                        "quantity": 0,
                        "instances": [],
                        "nomenclature": str(comutil.safe(child, "Nomenclature", "") or ""),
                        "revision": str(comutil.safe(child, "Revision", "") or ""),
                        "definition": str(comutil.safe(child, "Definition", "") or ""),
                    },
                )
                row["quantity"] += 1
                if len(row["instances"]) < 25:
                    row["instances"].append(comutil.name_of(child))
                reference = comutil.safe(child, "ReferenceProduct")
                doc = comutil.safe(reference, "Parent") if reference is not None else None
                path = str(comutil.safe(doc, "FullName", "") or "")
                if path and "file" not in row:
                    row["file"] = path
                walk(child, depth + 1)

        walk(product, 1)
        ordered = sorted(rows.values(), key=lambda r: r["part_number"])
        return result.ok(
            {
                "assembly": str(comutil.safe(product, "PartNumber", "") or comutil.name_of(product)),
                "distinct_parts": len(ordered),
                "total_instances": sum(r["quantity"] for r in ordered),
                "items": ordered,
            }
        )

    @tool(
        "catia_update_assembly",
        "Recompute the active assembly, applying every constraint.",
        idempotent=True,
        group="assembly",
    )
    def catia_update_assembly() -> dict:
        product = session.active_product()
        try:
            product.Update()
        except Exception as exc:
            raise errors.OperationFailedError(
                "The assembly update failed: %s" % errors.com_message(exc),
                remediation=(
                    "Usually a constraint cannot be satisfied. catia_list_constraints "
                    "reports each constraint's status code."
                ),
            ) from exc
        session.refresh_view()
        return result.ok(
            {"assembly": comutil.name_of(product)}, message="Assembly updated with no errors."
        )


def _compose(current: list[float], applied: list[float]) -> list[float]:
    """Multiply two CATIA column-major 3x3 rotations: applied * current."""

    def at(matrix: list[float], row: int, col: int) -> float:
        return matrix[col * 3 + row]

    out = [0.0] * 9
    for col in range(3):
        for row in range(3):
            out[col * 3 + row] = sum(at(applied, row, k) * at(current, k, col) for k in range(3))
    return out


def _constraint_label(code: int) -> str:
    for name, value in constants.all_constants().items():
        if name.startswith("catCstType") and value == code:
            return name.replace("catCstType", "").lower()
    return "unknown"
