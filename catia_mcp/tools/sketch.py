"""Sketcher: creating sketches and the 2D profiles that drive solid features.

Edition handling
----------------
CATIA sketches must be "open for edition" before geometry can be added and
closed again before a pad or pocket can consume them. Opening and closing
around every single primitive is slow and makes CATIA recompute the profile
repeatedly, so this module keeps one sketch open across calls and closes it
automatically the moment anything else needs the sketch finished. Solid tools
call :func:`ensure_closed` for exactly that reason.
"""

from __future__ import annotations

import logging
import math
from typing import Annotated, Any

from pydantic import Field

from catia_mcp.core import comutil, constants, errors, refs, result, vectors
from catia_mcp.tools.base import registrar, rename

logger = logging.getLogger("catia_mcp.tools.sketch")

# The apartment thread is the only thread that touches these, so plain
# module-level state is safe.
_OPEN: dict[str, Any] = {"sketch": None, "factory": None, "name": ""}

_AXIS_FALLBACK = (
    "Create the sketch without origin/horizontal_direction and draw the geometry at the "
    "offset you want instead, or make a construction plane with catia_gsd_plane "
    "(mode='offset') and sketch on that. Both avoid touching the sketch axis system."
)

CONSTRAINT_KINDS: dict[str, dict[str, Any]] = {
    "horizontal": {"const": "catCstTypeHorizontality", "elements": 1, "dimensioned": False},
    "vertical": {"const": "catCstTypeVerticality", "elements": 1, "dimensioned": False},
    "radius": {"const": "catCstTypeRadius", "elements": 1, "dimensioned": True},
    "diameter": {"const": "catCstTypeRadius", "elements": 1, "dimensioned": True,
                 "halve": True},
    "length": {"const": "catCstTypeLength", "elements": 1, "dimensioned": True},
    "distance": {"const": "catCstTypeDistance", "elements": 2, "dimensioned": True},
    "angle": {"const": "catCstTypeAngle", "elements": 2, "dimensioned": True, "degrees": True},
    "parallel": {"const": "catCstTypeParallelism", "elements": 2, "dimensioned": False},
    "perpendicular": {"const": "catCstTypePerpendicularity", "elements": 2,
                      "dimensioned": False},
    "tangent": {"const": "catCstTypeTangency", "elements": 2, "dimensioned": False},
    "coincidence": {"const": "catCstTypeOn", "elements": 2, "dimensioned": False},
    "concentric": {"const": "catCstTypeConcentricity", "elements": 2, "dimensioned": False},
    "symmetry": {"const": "catCstTypeSymmetry", "elements": 3, "dimensioned": False},
}


# ── edition state, shared with the solid tools ───────────────────────────────

def ensure_closed(session: Any) -> str:
    """Close any sketch this server left open. Returns its name, or ''."""
    sketch = _OPEN.get("sketch")
    if sketch is None:
        return ""
    name = _OPEN.get("name", "")
    try:
        sketch.CloseEdition()
    except Exception as exc:
        logger.info("CloseEdition on %s failed: %s", name, exc)
    _OPEN.update({"sketch": None, "factory": None, "name": ""})
    session.state.last_sketch_name = name or session.state.last_sketch_name
    return name


def reset_edition_state() -> None:
    _OPEN.update({"sketch": None, "factory": None, "name": ""})


def _sketch_by_name(session: Any, name: str) -> Any:
    part = session.active_part()
    for body in comutil.com_iter(comutil.safe(part, "Bodies")):
        found = comutil.safe_call(comutil.safe(body, "Sketches"), "Item", name)
        if found is not None:
            return found
    for holder in comutil.com_iter(comutil.safe(part, "HybridBodies")):
        found = comutil.safe_call(comutil.safe(holder, "HybridSketches"), "Item", name)
        if found is not None:
            return found
    return refs.find_named(session, name)


def _target_sketch(session: Any, name: str = "") -> Any:
    """Resolve the sketch a drawing call should go into."""
    wanted = name or _OPEN.get("name") or session.state.last_sketch_name
    if not wanted:
        raise errors.ElementNotFoundError(
            "No sketch to draw into. Create one first with catia_create_sketch.",
            remediation="catia_create_sketch(support='xy') starts a sketch on the XY plane.",
        )
    if _OPEN.get("name") == wanted and _OPEN.get("sketch") is not None:
        return _OPEN["sketch"]
    return _sketch_by_name(session, wanted)


def _factory(session: Any, name: str = "") -> tuple[Any, Any]:
    """Return ``(sketch, Factory2D)`` with the sketch open for edition."""
    sketch = _target_sketch(session, name)
    sketch_name = comutil.name_of(sketch)
    if _OPEN.get("name") == sketch_name and _OPEN.get("factory") is not None:
        return sketch, _OPEN["factory"]
    if _OPEN.get("sketch") is not None:
        ensure_closed(session)
    try:
        factory = sketch.OpenEdition()
    except Exception as exc:
        raise errors.OperationFailedError(
            "Could not open sketch %r for edition: %s" % (sketch_name, errors.com_message(exc)),
            remediation=(
                "The sketch may already be open in the CATIA user interface. Exit the "
                "Sketcher workbench in CATIA and retry."
            ),
        ) from exc
    _OPEN.update({"sketch": sketch, "factory": factory, "name": sketch_name})
    session.state.last_sketch_name = sketch_name
    return sketch, factory


def _geometry_summary(sketch: Any) -> dict[str, Any]:
    elements = comutil.safe(sketch, "GeometricElements")
    return {
        "sketch": comutil.name_of(sketch),
        "element_count": comutil.com_count(elements),
        "reference_token": "sketch:%s" % comutil.name_of(sketch),
    }


def _sketch_element(sketch: Any, token: str) -> Any:
    """Resolve a 2D element inside a sketch by name, index, or 'Line.1.start'."""
    elements = comutil.safe(sketch, "GeometricElements")
    if elements is None:
        raise errors.ElementNotFoundError("Sketch has no geometric elements.")

    text = str(token).strip()
    suffix = ""
    for tail in (".start", ".end", ".center"):
        if text.lower().endswith(tail):
            suffix = tail[1:]
            text = text[: -len(tail)]
            break

    base: Any = None
    if text.isdigit():
        base = comutil.safe_call(elements, "Item", int(text))
    if base is None:
        base = comutil.safe_call(elements, "Item", text)
    if base is None:
        raise errors.ElementNotFoundError(
            "No element %r in sketch %r. Call catia_sketch_geometry to list them."
            % (token, comutil.name_of(sketch))
        )

    if not suffix:
        return base
    member = {"start": "StartPoint", "end": "EndPoint", "center": "CenterPoint"}[suffix]
    point = comutil.safe(base, member)
    if point is None:
        raise errors.ElementNotFoundError(
            "%r has no %s point." % (text, suffix)
        )
    return point


def register(mcp: Any, session: Any) -> None:
    tool = registrar(mcp, session)

    # ── sketch lifecycle ─────────────────────────────────────────────────────

    @tool(
        "catia_create_sketch",
        "Create a sketch on a plane or a planar face and open it for drawing. The support "
        "is a reference token: 'xy', 'yz', 'zx' for the origin planes, 'face#3' or "
        "'face@x,y,z' to sketch directly on a face of the solid, or the name of a "
        "construction plane. Subsequent catia_sketch_* calls draw into this sketch until "
        "you close it or create another one.",
        group="sketch",
    )
    def catia_create_sketch(
        support: Annotated[
            str,
            Field(
                description=(
                    "Reference token for the sketch plane: 'xy' | 'yz' | 'zx' | 'face#3' | "
                    "'face@10,0,25' | 'name:Plane.1'."
                )
            ),
        ] = "xy",
        name: Annotated[str, Field(description="Name for the new sketch.")] = "",
        body: Annotated[
            str, Field(description="Body to create the sketch in. Defaults to the in-work body.")
        ] = "",
        origin: Annotated[
            list[float] | None,
            Field(
                description=(
                    "Optional 3D origin [x,y,z] for the sketch axis system, in part "
                    "coordinates. Use it to place a sketch away from the plane's origin "
                    "without needing a construction plane."
                )
            ),
        ] = None,
        horizontal_direction: Annotated[
            list[float] | None,
            Field(description="Optional 3D vector [x,y,z] for the sketch's H axis."),
        ] = None,
    ) -> dict:
        ensure_closed(session)
        part = session.active_part()
        support_ref = refs.resolve(session, support, part=part)

        if body:
            container = comutil.safe_call(comutil.safe(part, "Bodies"), "Item", body)
            if container is None:
                raise errors.ElementNotFoundError("No body called %r." % body)
        else:
            container = comutil.safe(part, "InWorkObject") or comutil.safe(part, "MainBody")

        sketches = comutil.safe(container, "Sketches") or comutil.safe(
            container, "HybridSketches"
        )
        if sketches is None:
            raise errors.OperationFailedError(
                "%r cannot hold sketches." % comutil.name_of(container)
            )

        try:
            sketch = sketches.Add(support_ref.reference)
        except Exception as exc:
            raise errors.OperationFailedError(
                "CATIA refused to start a sketch on %s: %s"
                % (support_ref.label, errors.com_message(exc)),
                remediation=(
                    "The support must be a plane or a planar face. catia_list_faces reports "
                    "'planar': true for faces that qualify."
                ),
            ) from exc

        warnings: list[str] = []
        if origin is not None or horizontal_direction is not None:
            try:
                applied = _apply_axis_data(session, sketch, origin, horizontal_direction)
            except errors.CatiaError:
                # A sketch whose axis could not be set would fail the next
                # update with a modal dialog, so remove it and report why.
                _discard_sketch(session, sketch)
                raise
            if not applied:
                warnings.append(
                    "Could not reposition the sketch axis; it stays at the support's origin."
                )

        final_name = rename(sketch, name)
        session.state.last_sketch_name = final_name
        _OPEN.update({"sketch": sketch, "factory": sketch.OpenEdition(), "name": final_name})

        return result.ok(
            {
                "sketch": final_name,
                "support": support_ref.label,
                "reference_token": "sketch:%s" % final_name,
                "open_for_edition": True,
            },
            message="Sketch %r opened on %s." % (final_name, support_ref.label),
            warnings=warnings,
            hint=(
                "Draw with catia_sketch_rectangle / circle / line, then create a solid with "
                "catia_pad or catia_pocket - they close the sketch for you."
            ),
        )

    @tool(
        "catia_close_sketch",
        "Finish editing the current sketch. The solid tools do this automatically, so you "
        "only need it when you want to inspect a finished sketch or switch to another one.",
        idempotent=True,
        group="sketch",
    )
    def catia_close_sketch() -> dict:
        name = ensure_closed(session)
        if not name:
            return result.ok({"closed": None}, message="No sketch was open.")
        session.refresh_view()
        return result.ok({"closed": name}, message="Closed sketch %r." % name)

    @tool(
        "catia_sketch_geometry",
        "List the 2D elements of a sketch with their names, types and coordinates, plus "
        "its constraints. Use the element names in catia_sketch_constraint.",
        readonly=True,
        group="sketch",
    )
    def catia_sketch_geometry(
        sketch: Annotated[
            str, Field(description="Sketch name. Defaults to the most recent sketch.")
        ] = "",
    ) -> dict:
        target = _target_sketch(session, sketch)
        elements = []
        for item in comutil.com_iter(comutil.safe(target, "GeometricElements")):
            entry: dict[str, Any] = {
                "name": comutil.name_of(item),
                "construction": bool(comutil.safe(item, "Construction", False)),
            }
            centre = comutil.safe(item, "CenterPoint")
            radius = comutil.safe(item, "Radius")
            if radius is not None:
                entry["radius"] = round(float(radius), 4)
            for label, member in (("start", "StartPoint"), ("end", "EndPoint")):
                point = comutil.safe(item, member)
                coords = _point_coords(point)
                if coords:
                    entry[label] = coords
            coords = _point_coords(centre) or _point_coords(item)
            if coords:
                entry["center" if centre is not None else "position"] = coords
            elements.append(entry)

        constraints = []
        for cst in comutil.com_iter(comutil.safe(target, "Constraints")):
            entry = {"name": comutil.name_of(cst)}
            dimension = comutil.safe(cst, "Dimension")
            if dimension is not None:
                value = comutil.safe(dimension, "Value")
                if value is not None:
                    entry["value"] = round(float(value), 4)
            constraints.append(entry)

        payload = _geometry_summary(target)
        payload["elements"] = elements
        payload["constraints"] = constraints
        return result.ok(payload)

    # ── 2D primitives ────────────────────────────────────────────────────────

    @tool(
        "catia_sketch_point",
        "Add a 2D point to the current sketch. Points are useful as hole centres and as "
        "constraint anchors.",
        group="sketch",
    )
    def catia_sketch_point(
        x: Annotated[float, Field(description="H coordinate in the sketch plane, in mm.")],
        y: Annotated[float, Field(description="V coordinate in the sketch plane, in mm.")],
        sketch: Annotated[str, Field(description="Target sketch. Defaults to the open one.")] = "",
    ) -> dict:
        target, factory = _factory(session, sketch)
        point = factory.CreatePoint(float(x), float(y))
        return result.ok(
            {"element": comutil.name_of(point), "x": x, "y": y, **_geometry_summary(target)},
            message="Added a point at (%.3f, %.3f)." % (x, y),
        )

    @tool(
        "catia_sketch_line",
        "Add a straight line between two points in the current sketch. Set construction=true "
        "for a reference line that does not take part in the profile, and axis=true to make "
        "it the sketch's revolution axis (what catia_shaft and catia_groove revolve around).",
        group="sketch",
    )
    def catia_sketch_line(
        x1: Annotated[float, Field(description="Start H coordinate, mm.")],
        y1: Annotated[float, Field(description="Start V coordinate, mm.")],
        x2: Annotated[float, Field(description="End H coordinate, mm.")],
        y2: Annotated[float, Field(description="End V coordinate, mm.")],
        construction: Annotated[
            bool, Field(description="Make it a construction (reference) line.")
        ] = False,
        axis: Annotated[
            bool, Field(description="Use this line as the sketch's revolution axis.")
        ] = False,
        sketch: Annotated[str, Field(description="Target sketch.")] = "",
    ) -> dict:
        target, factory = _factory(session, sketch)
        line = factory.CreateLine(float(x1), float(y1), float(x2), float(y2))
        warnings: list[str] = []
        if construction or axis:
            try:
                line.Construction = True
            except Exception:
                warnings.append("Could not mark the line as construction geometry.")
        if axis:
            try:
                target.CenterLine = line
            except Exception as exc:
                warnings.append(
                    "Could not set the line as the revolution axis (%s)."
                    % errors.com_message(exc)
                )
        return result.ok(
            {
                "element": comutil.name_of(line),
                "length_mm": round(math.dist((x1, y1), (x2, y2)), 4),
                "is_axis": axis,
                **_geometry_summary(target),
            },
            message="Added a line.",
            warnings=warnings,
        )

    @tool(
        "catia_sketch_circle",
        "Add a full circle to the current sketch.",
        group="sketch",
    )
    def catia_sketch_circle(
        center_x: Annotated[float, Field(description="Centre H coordinate, mm.")],
        center_y: Annotated[float, Field(description="Centre V coordinate, mm.")],
        radius: Annotated[float, Field(description="Radius in mm.", gt=0)],
        construction: Annotated[bool, Field(description="Construction geometry.")] = False,
        sketch: Annotated[str, Field(description="Target sketch.")] = "",
    ) -> dict:
        target, factory = _factory(session, sketch)
        _, circle = comutil.try_variants(
            [
                (
                    "CreateClosedCircle",
                    lambda: factory.CreateClosedCircle(
                        float(center_x), float(center_y), float(radius)
                    ),
                ),
                (
                    "CreateCircle",
                    lambda: factory.CreateCircle(
                        float(center_x), float(center_y), float(radius), 0.0, 2 * math.pi
                    ),
                ),
            ],
            what="create a circle",
        )
        if construction:
            comutil.safe_call(circle, "put_Construction", True)
            try:
                circle.Construction = True
            except Exception:
                pass
        return result.ok(
            {"element": comutil.name_of(circle), "radius_mm": radius, **_geometry_summary(target)},
            message="Added a circle of radius %.3f mm." % radius,
        )

    @tool(
        "catia_sketch_arc",
        "Add a circular arc, given its centre, radius and the start and end angles measured "
        "counter-clockwise from the sketch's H axis.",
        group="sketch",
    )
    def catia_sketch_arc(
        center_x: Annotated[float, Field(description="Centre H coordinate, mm.")],
        center_y: Annotated[float, Field(description="Centre V coordinate, mm.")],
        radius: Annotated[float, Field(description="Radius in mm.", gt=0)],
        start_angle: Annotated[float, Field(description="Start angle in degrees.")] = 0.0,
        end_angle: Annotated[float, Field(description="End angle in degrees.")] = 90.0,
        sketch: Annotated[str, Field(description="Target sketch.")] = "",
    ) -> dict:
        target, factory = _factory(session, sketch)
        arc = factory.CreateCircle(
            float(center_x),
            float(center_y),
            float(radius),
            math.radians(float(start_angle)),
            math.radians(float(end_angle)),
        )
        return result.ok(
            {
                "element": comutil.name_of(arc),
                "radius_mm": radius,
                "sweep_deg": round(float(end_angle) - float(start_angle), 4),
                **_geometry_summary(target),
            },
            message="Added an arc.",
        )

    @tool(
        "catia_sketch_ellipse",
        "Add an ellipse or elliptical arc to the current sketch.",
        group="sketch",
    )
    def catia_sketch_ellipse(
        center_x: Annotated[float, Field(description="Centre H coordinate, mm.")],
        center_y: Annotated[float, Field(description="Centre V coordinate, mm.")],
        major_radius: Annotated[float, Field(description="Semi-major axis, mm.", gt=0)],
        minor_radius: Annotated[float, Field(description="Semi-minor axis, mm.", gt=0)],
        rotation: Annotated[
            float, Field(description="Rotation of the major axis from H, in degrees.")
        ] = 0.0,
        start_angle: Annotated[float, Field(description="Start angle, degrees.")] = 0.0,
        end_angle: Annotated[float, Field(description="End angle, degrees.")] = 360.0,
        sketch: Annotated[str, Field(description="Target sketch.")] = "",
    ) -> dict:
        target, factory = _factory(session, sketch)
        angle = math.radians(float(rotation))
        major_dir_x = math.cos(angle)
        major_dir_y = math.sin(angle)
        ellipse = factory.CreateEllipse(
            float(center_x),
            float(center_y),
            major_dir_x,
            major_dir_y,
            float(major_radius),
            float(minor_radius),
            math.radians(float(start_angle)),
            math.radians(float(end_angle)),
        )
        return result.ok(
            {"element": comutil.name_of(ellipse), **_geometry_summary(target)},
            message="Added an ellipse.",
        )

    @tool(
        "catia_sketch_rectangle",
        "Add a closed rectangle from two opposite corners. The four lines share explicit "
        "corner points, so the profile is genuinely closed and can be padded or pocketed "
        "straight away.",
        group="sketch",
    )
    def catia_sketch_rectangle(
        x1: Annotated[float, Field(description="First corner H coordinate, mm.")],
        y1: Annotated[float, Field(description="First corner V coordinate, mm.")],
        x2: Annotated[float, Field(description="Opposite corner H coordinate, mm.")],
        y2: Annotated[float, Field(description="Opposite corner V coordinate, mm.")],
        sketch: Annotated[str, Field(description="Target sketch.")] = "",
    ) -> dict:
        target, factory = _factory(session, sketch)
        corners = [(x1, y1), (x2, y1), (x2, y2), (x1, y2)]
        names = _closed_polyline(factory, corners)
        return result.ok(
            {
                "elements": names,
                "width_mm": round(abs(float(x2) - float(x1)), 4),
                "height_mm": round(abs(float(y2) - float(y1)), 4),
                **_geometry_summary(target),
            },
            message="Added a closed rectangle.",
        )

    @tool(
        "catia_sketch_centered_rectangle",
        "Add a closed rectangle centred on a point, given its width and height.",
        group="sketch",
    )
    def catia_sketch_centered_rectangle(
        center_x: Annotated[float, Field(description="Centre H coordinate, mm.")],
        center_y: Annotated[float, Field(description="Centre V coordinate, mm.")],
        width: Annotated[float, Field(description="Total width along H, mm.", gt=0)],
        height: Annotated[float, Field(description="Total height along V, mm.", gt=0)],
        sketch: Annotated[str, Field(description="Target sketch.")] = "",
    ) -> dict:
        half_w, half_h = float(width) / 2.0, float(height) / 2.0
        target, factory = _factory(session, sketch)
        corners = [
            (center_x - half_w, center_y - half_h),
            (center_x + half_w, center_y - half_h),
            (center_x + half_w, center_y + half_h),
            (center_x - half_w, center_y + half_h),
        ]
        names = _closed_polyline(factory, corners)
        return result.ok(
            {"elements": names, "width_mm": width, "height_mm": height,
             **_geometry_summary(target)},
            message="Added a centred rectangle %.2f x %.2f mm." % (width, height),
        )

    @tool(
        "catia_sketch_polygon",
        "Add a closed regular polygon (hexagon, octagon and so on) inscribed in or "
        "circumscribed about a circle.",
        group="sketch",
    )
    def catia_sketch_polygon(
        center_x: Annotated[float, Field(description="Centre H coordinate, mm.")],
        center_y: Annotated[float, Field(description="Centre V coordinate, mm.")],
        sides: Annotated[int, Field(description="Number of sides.", ge=3, le=64)],
        radius: Annotated[float, Field(description="Radius in mm.", gt=0)],
        circumscribed: Annotated[
            bool,
            Field(
                description=(
                    "True for across-flats (radius is the inscribed circle, as with a "
                    "hex nut), False for across-corners."
                )
            ),
        ] = False,
        rotation: Annotated[
            float, Field(description="Rotation of the first vertex from H, degrees.")
        ] = 0.0,
        sketch: Annotated[str, Field(description="Target sketch.")] = "",
    ) -> dict:
        target, factory = _factory(session, sketch)
        n = int(sides)
        effective = float(radius) / math.cos(math.pi / n) if circumscribed else float(radius)
        start = math.radians(float(rotation))
        corners = [
            (
                float(center_x) + effective * math.cos(start + 2 * math.pi * i / n),
                float(center_y) + effective * math.sin(start + 2 * math.pi * i / n),
            )
            for i in range(n)
        ]
        names = _closed_polyline(factory, corners)
        return result.ok(
            {
                "elements": names,
                "sides": n,
                "vertex_radius_mm": round(effective, 4),
                **_geometry_summary(target),
            },
            message="Added a %d-sided polygon." % n,
        )

    @tool(
        "catia_sketch_slot",
        "Add a closed slot (obround) profile: two parallel lines capped by semicircles, "
        "defined by the centres of the two end arcs and the slot radius.",
        group="sketch",
    )
    def catia_sketch_slot(
        x1: Annotated[float, Field(description="First arc centre H coordinate, mm.")],
        y1: Annotated[float, Field(description="First arc centre V coordinate, mm.")],
        x2: Annotated[float, Field(description="Second arc centre H coordinate, mm.")],
        y2: Annotated[float, Field(description="Second arc centre V coordinate, mm.")],
        radius: Annotated[float, Field(description="Slot radius (half its width), mm.", gt=0)],
        sketch: Annotated[str, Field(description="Target sketch.")] = "",
    ) -> dict:
        target, factory = _factory(session, sketch)
        dx, dy = float(x2) - float(x1), float(y2) - float(y1)
        span = math.hypot(dx, dy)
        if span < 1e-9:
            raise errors.InvalidArgumentError(
                "The two arc centres coincide; a slot needs a non-zero length."
            )
        ux, uy = dx / span, dy / span
        nx, ny = -uy, ux  # left-hand normal
        r = float(radius)

        p1 = factory.CreatePoint(float(x1) + nx * r, float(y1) + ny * r)
        p2 = factory.CreatePoint(float(x2) + nx * r, float(y2) + ny * r)
        p3 = factory.CreatePoint(float(x2) - nx * r, float(y2) - ny * r)
        p4 = factory.CreatePoint(float(x1) - nx * r, float(y1) - ny * r)

        base_angle = math.atan2(uy, ux)
        arc_a = factory.CreateCircle(
            float(x2), float(y2), r, base_angle + math.pi / 2, base_angle - math.pi / 2
        )
        arc_b = factory.CreateCircle(
            float(x1), float(y1), r, base_angle - math.pi / 2, base_angle + math.pi / 2
        )
        line_a = factory.CreateLine(
            float(x1) + nx * r, float(y1) + ny * r, float(x2) + nx * r, float(y2) + ny * r
        )
        line_b = factory.CreateLine(
            float(x2) - nx * r, float(y2) - ny * r, float(x1) - nx * r, float(y1) - ny * r
        )

        warnings: list[str] = []
        for element, start, end in ((line_a, p1, p2), (line_b, p3, p4)):
            if not _link_endpoints(element, start, end):
                warnings.append("Could not weld one slot line to its end points.")
        for arc, start, end in ((arc_a, p2, p3), (arc_b, p4, p1)):
            if not _link_endpoints(arc, start, end):
                warnings.append("Could not weld one slot arc to its end points.")

        return result.ok(
            {
                "elements": [comutil.name_of(e) for e in (line_a, arc_a, line_b, arc_b)],
                "length_mm": round(span + 2 * r, 4),
                "width_mm": round(2 * r, 4),
                **_geometry_summary(target),
            },
            message="Added a slot.",
            warnings=warnings,
        )

    @tool(
        "catia_sketch_polyline",
        "Add a chain of connected lines through a list of points, optionally closing it "
        "back to the first point. Shared end points are created explicitly, so a closed "
        "polyline is immediately usable as a pad or pocket profile.",
        group="sketch",
    )
    def catia_sketch_polyline(
        points: Annotated[
            list[list[float]],
            Field(description="Points as [[x1,y1],[x2,y2],...] in sketch coordinates, mm."),
        ],
        closed: Annotated[bool, Field(description="Join the last point back to the first.")] = True,
        sketch: Annotated[str, Field(description="Target sketch.")] = "",
    ) -> dict:
        pairs = _as_pairs(points, minimum=2)
        target, factory = _factory(session, sketch)
        if closed:
            names = _closed_polyline(factory, pairs)
        else:
            names = _open_polyline(factory, pairs)
        return result.ok(
            {"elements": names, "vertices": len(pairs), "closed": closed,
             **_geometry_summary(target)},
            message="Added a polyline through %d points." % len(pairs),
        )

    @tool(
        "catia_sketch_spline",
        "Add a spline through a list of control points.",
        group="sketch",
    )
    def catia_sketch_spline(
        points: Annotated[
            list[list[float]],
            Field(description="Control points as [[x1,y1],[x2,y2],...], mm."),
        ],
        sketch: Annotated[str, Field(description="Target sketch.")] = "",
    ) -> dict:
        pairs = _as_pairs(points, minimum=3)
        target, factory = _factory(session, sketch)
        controls = [factory.CreateControlPoint(px, py) for px, py in pairs]
        _, spline = comutil.try_variants(
            [
                ("tuple", lambda: factory.CreateSpline(tuple(controls))),
                ("list", lambda: factory.CreateSpline(controls)),
            ],
            what="create a spline",
        )
        return result.ok(
            {"element": comutil.name_of(spline), "control_points": len(controls),
             **_geometry_summary(target)},
            message="Added a spline through %d control points." % len(controls),
        )

    # ── constraints ──────────────────────────────────────────────────────────

    @tool(
        "catia_sketch_constraint",
        "Add a constraint to the current sketch. Dimensional kinds (distance, length, "
        "radius, diameter, angle) take a value; geometric kinds (horizontal, vertical, "
        "parallel, perpendicular, tangent, coincidence, concentric, symmetry) do not. "
        "Element names come from catia_sketch_geometry; append '.start', '.end' or "
        "'.center' to reference a point of an element.",
        group="sketch",
    )
    def catia_sketch_constraint(
        kind: Annotated[
            str,
            Field(
                description=(
                    "One of: horizontal, vertical, radius, diameter, length, distance, "
                    "angle, parallel, perpendicular, tangent, coincidence, concentric, "
                    "symmetry."
                )
            ),
        ],
        elements: Annotated[
            list[str],
            Field(
                description=(
                    "Sketch element names, e.g. ['Line.1'] or ['Line.1','Line.3'] or "
                    "['Line.1.start','Circle.1.center']."
                )
            ),
        ],
        value: Annotated[
            float | None,
            Field(description="Dimension value: mm for lengths, degrees for angles."),
        ] = None,
        reference_only: Annotated[
            bool, Field(description="Create a driven (measured) dimension instead of a driving one.")
        ] = False,
        sketch: Annotated[str, Field(description="Target sketch.")] = "",
    ) -> dict:
        key = kind.strip().lower()
        if key not in CONSTRAINT_KINDS:
            raise errors.InvalidArgumentError(
                "Unknown constraint kind %r. Valid kinds: %s"
                % (kind, ", ".join(sorted(CONSTRAINT_KINDS)))
            )
        spec = CONSTRAINT_KINDS[key]
        expected = spec["elements"]
        if len(elements) != expected:
            raise errors.InvalidArgumentError(
                "A %s constraint needs exactly %d element(s); %d given."
                % (key, expected, len(elements))
            )
        if spec["dimensioned"] and value is None:
            raise errors.InvalidArgumentError("A %s constraint needs a value." % key)

        target = _target_sketch(session, sketch)
        # Constraints cannot be added while the sketch is open for edition on
        # some releases, and are always safe once it is closed.
        if _OPEN.get("name") == comutil.name_of(target):
            ensure_closed(session)
            target = _sketch_by_name(session, comutil.name_of(target))

        part = session.active_part()
        references = [
            part.CreateReferenceFromObject(_sketch_element(target, token)) for token in elements
        ]
        constraint_type = constants.const(spec["const"])
        constraints = target.Constraints

        if expected == 1:
            call = lambda: constraints.AddMonoEltCst(constraint_type, references[0])  # noqa: E731
        elif expected == 2:
            call = lambda: constraints.AddBiEltCst(  # noqa: E731
                constraint_type, references[0], references[1]
            )
        else:
            call = lambda: constraints.AddTriEltCst(  # noqa: E731
                constraint_type, references[0], references[1], references[2]
            )

        try:
            constraint = call()
        except Exception as exc:
            raise errors.OperationFailedError(
                "CATIA rejected the %s constraint: %s" % (key, errors.com_message(exc)),
                remediation=(
                    "Check that the element types suit the constraint (a radius needs a "
                    "circle, an angle needs two lines) and that the sketch is not already "
                    "over-constrained."
                ),
            ) from exc

        applied_value = None
        if value is not None:
            numeric = float(value) / 2.0 if spec.get("halve") else float(value)
            dimension = comutil.safe(constraint, "Dimension")
            if dimension is not None:
                try:
                    dimension.Value = numeric
                    applied_value = numeric
                except Exception as exc:
                    logger.info("Could not set constraint value: %s", exc)
        if reference_only:
            try:
                constraint.Mode = constants.const("catCstModeDrivenDimension")
            except Exception:
                pass

        session.update_part(part)
        return result.ok(
            {
                "constraint": comutil.name_of(constraint),
                "kind": key,
                "elements": elements,
                "value": applied_value,
                "driving": not reference_only,
            },
            message="Added a %s constraint." % key,
        )

    @tool(
        "catia_sketch_delete_element",
        "Delete one 2D element from a sketch.",
        destructive=True,
        group="sketch",
    )
    def catia_sketch_delete_element(
        element: Annotated[str, Field(description="Element name, e.g. 'Line.3'.")],
        sketch: Annotated[str, Field(description="Target sketch.")] = "",
    ) -> dict:
        target = _target_sketch(session, sketch)
        if _OPEN.get("name") == comutil.name_of(target):
            ensure_closed(session)
            target = _sketch_by_name(session, comutil.name_of(target))
        obj = _sketch_element(target, element)
        selection = session.selection()
        selection.Clear()
        selection.Add(obj)
        selection.Delete()
        selection.Clear()
        return result.ok(
            {"deleted": element, **_geometry_summary(target)},
            message="Deleted %s." % element,
        )


# ── helpers ──────────────────────────────────────────────────────────────────

def _as_pairs(points: Any, minimum: int) -> list[tuple[float, float]]:
    pairs: list[tuple[float, float]] = []
    for entry in points or []:
        values = list(entry)
        if len(values) < 2:
            raise errors.InvalidArgumentError(
                "Each point needs two numbers [x, y]; got %r." % (entry,)
            )
        pairs.append((float(values[0]), float(values[1])))
    if len(pairs) < minimum:
        raise errors.InvalidArgumentError(
            "At least %d points are required; %d given." % (minimum, len(pairs))
        )
    return pairs


def _link_endpoints(element: Any, start: Any, end: Any) -> bool:
    """Weld a 2D element to explicit Point2D objects so the profile is connected."""
    ok = True
    for member, point in (("StartPoint", start), ("EndPoint", end)):
        try:
            setattr(element, member, point)
        except Exception as exc:
            logger.debug("Could not set %s: %s", member, exc)
            ok = False
    return ok


def _open_polyline(factory: Any, pairs: list[tuple[float, float]]) -> list[str]:
    points = [factory.CreatePoint(px, py) for px, py in pairs]
    names: list[str] = []
    for index in range(len(pairs) - 1):
        (x1, y1), (x2, y2) = pairs[index], pairs[index + 1]
        line = factory.CreateLine(x1, y1, x2, y2)
        _link_endpoints(line, points[index], points[index + 1])
        names.append(comutil.name_of(line))
    return names


def _closed_polyline(factory: Any, pairs: list[tuple[float, float]]) -> list[str]:
    points = [factory.CreatePoint(px, py) for px, py in pairs]
    count = len(pairs)
    names: list[str] = []
    for index in range(count):
        (x1, y1) = pairs[index]
        (x2, y2) = pairs[(index + 1) % count]
        line = factory.CreateLine(x1, y1, x2, y2)
        _link_endpoints(line, points[index], points[(index + 1) % count])
        names.append(comutil.name_of(line))
    return names


def _discard_sketch(session: Any, sketch: Any) -> None:
    """Remove a sketch we are abandoning, so no broken feature is left behind."""
    reset_edition_state()
    try:
        sketch.CloseEdition()
    except Exception:
        pass
    try:
        selection = session.selection()
        selection.Clear()
        selection.Add(sketch)
        selection.Delete()
        selection.Clear()
    except Exception as exc:
        logger.info("Could not remove the abandoned sketch: %s", exc)
    session.state.last_sketch_name = ""


def _point_coords(point: Any) -> dict[str, float] | None:
    if point is None:
        return None
    x = comutil.safe(point, "X")
    y = comutil.safe(point, "Y")
    if x is None or y is None:
        return None
    try:
        return {"x": round(float(x), 4), "y": round(float(y), 4)}
    except Exception:
        return None


def _apply_axis_data(
    session: Any, sketch: Any, origin: list[float] | None, horizontal: list[float] | None
) -> bool:
    """Reposition a sketch's axis system in 3D, via SetAbsoluteAxisData.

    ``GetAbsoluteAxisData`` returns nine doubles: the origin, then the H
    direction, then the V direction. Every one of them has to be valid, because
    CATIA does not validate what is written - it stores the numbers, reports
    success, and only fails later during ``Update()``, as a modal
    "Colinear directions : cannot build a plane or an axis" dialog that blocks
    all further automation.

    So this refuses to write anything it cannot show is well formed: the
    current axis must be readable, and a caller-supplied H direction is
    projected into the sketch plane with V re-derived from the plane normal,
    which keeps the pair orthogonal by construction.
    """
    try:
        current = list(
            comutil.out_doubles(sketch, "GetAbsoluteAxisData", 9, app=session.app)
        )
    except Exception as exc:
        raise errors.OperationFailedError(
            "Could not read the sketch's current axis system (%s), so it cannot be "
            "repositioned safely." % errors.com_message(exc),
            remediation=_AXIS_FALLBACK,
        ) from exc

    if len(current) != 9:
        raise errors.OperationFailedError(
            "CATIA returned %d values for the sketch axis instead of 9." % len(current),
            remediation=_AXIS_FALLBACK,
        )

    h_axis = current[3:6]
    v_axis = current[6:9]
    normal = vectors.unit_cross(h_axis, v_axis)
    if normal is None:
        # An all-zero read is indistinguishable from a by-reference write that
        # never landed. Either way there is no sound basis to write from.
        raise errors.OperationFailedError(
            "The sketch's current axis directions read back as %s and %s, which do not "
            "define a plane. Repositioning from that would leave CATIA with colinear "
            "directions and fail the next update."
            % (vectors.describe(h_axis), vectors.describe(v_axis)),
            remediation=_AXIS_FALLBACK,
        )

    data = list(current)

    if origin is not None:
        point = vectors.as_vector(origin)
        if len(list(origin)) < 3:
            raise errors.InvalidArgumentError(
                "origin needs three numbers [x, y, z]; got %r." % (origin,)
            )
        data[0:3] = point

    if horizontal is not None:
        if len(list(horizontal)) < 3:
            raise errors.InvalidArgumentError(
                "horizontal_direction needs three numbers [x, y, z]; got %r." % (horizontal,)
            )
        wanted = vectors.as_vector(horizontal)
        if vectors.is_zero(wanted):
            raise errors.InvalidArgumentError(
                "horizontal_direction %s is a zero-length vector, which has no direction."
                % vectors.describe(wanted)
            )
        in_plane = vectors.normalize(vectors.reject(wanted, normal))
        if in_plane is None:
            raise errors.InvalidArgumentError(
                "horizontal_direction %s is perpendicular to the sketch plane (normal %s), "
                "so it has no component lying in it. The H axis must lie *in* the sketch "
                "plane." % (vectors.describe(wanted), vectors.describe(normal)),
                remediation=(
                    "Pick a direction in the plane - for a sketch on 'xy' that means a "
                    "vector with a non-zero X or Y component - or sketch on a different "
                    "support."
                ),
            )
        new_v = vectors.unit_cross(normal, in_plane)
        if new_v is None:  # pragma: no cover - unreachable given the checks above
            raise errors.InvalidArgumentError(
                "Could not derive a V axis perpendicular to %s." % vectors.describe(in_plane)
            )
        data[3:6] = in_plane
        data[6:9] = new_v

    problem = vectors.check_direction_pair(
        data[3:6], data[6:9], first_label="the sketch H axis", second_label="the V axis"
    )
    if problem:  # pragma: no cover - the construction above rules this out
        raise errors.InvalidArgumentError(problem, remediation=_AXIS_FALLBACK)

    try:
        sketch.SetAbsoluteAxisData(comutil.in_doubles(data))
        return True
    except Exception:
        try:
            sketch.SetAbsoluteAxisData(data)
            return True
        except Exception as exc:
            logger.info("SetAbsoluteAxisData failed: %s", exc)
            return False
