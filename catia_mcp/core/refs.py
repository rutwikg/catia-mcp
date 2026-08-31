"""Resolving human/model-written reference tokens into CATIA references.

Selecting geometry is the part of CATIA automation that most often defeats a
scripted client, because the natural handle - a BRep name like
``RSur:(Face:(Brp:(Pad.1;0:(Brp:(Sketch.1;2)))...)`` - is unstable across
releases *and* across edits to the model. This module gives the model a small
token grammar instead, backed by live ``Selection.Search`` results:

===========================  =================================================
``xy`` ``yz`` ``zx``          the part's origin planes
``last``                      the most recent feature this server created
``body`` / ``body:PartBody``  a body by name, or the part's main body
``sketch:Sketch.2``           a sketch by name (bare ``sketch`` = last created)
``name:Pad.1`` / ``Pad.1``    any tree element, by the name shown in CATIA
``face#3`` ``edge#7``         the n-th face/edge of the current solid
``face@12,0,40``              the face whose centre of gravity is nearest a point
``edge@0,0,10``               likewise for edges
``vertex#2`` ``vertex@x,y,z`` likewise for vertices
``point:10,20,30``            a literal coordinate triple (where one is allowed)
===========================  =================================================

Index tokens (``face#3``) are cheap but only stable until the model changes;
proximity tokens (``face@x,y,z``) survive edits and are what a model should
prefer when it already knows roughly where the geometry is.
"""

from __future__ import annotations

import logging
import math
import re
from collections.abc import Iterable
from typing import Any

from catia_mcp.core import comutil, errors

logger = logging.getLogger("catia_mcp.refs")

PLANE_TOKENS = {
    "xy": "PlaneXY",
    "yz": "PlaneYZ",
    "zx": "PlaneZX",
    "xz": "PlaneZX",
}

TOPOLOGY_SEARCH = {
    "face": ("Topology.CGMFace,all", "Topology.Face,all"),
    "edge": ("Topology.CGMEdge,all", "Topology.Edge,all"),
    "vertex": ("Topology.CGMVertex,all", "Topology.Vertex,all"),
}

_TOPO_INDEX = re.compile(r"^(face|edge|vertex)\s*#\s*(\d+)$", re.IGNORECASE)
_TOPO_NEAR = re.compile(r"^(face|edge|vertex)\s*@\s*(.+)$", re.IGNORECASE)
_COORDS = re.compile(r"^\s*point\s*:\s*(.+)$", re.IGNORECASE)


class Resolved:
    """A resolved token: the CATIA object, a Reference to it, and a label."""

    __slots__ = ("obj", "reference", "label", "kind")

    def __init__(self, obj: Any, reference: Any, label: str, kind: str) -> None:
        self.obj = obj
        self.reference = reference
        self.label = label
        self.kind = kind

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<Resolved %s %r>" % (self.kind, self.label)


# ── helpers ──────────────────────────────────────────────────────────────────

def parse_xyz(text: str) -> tuple[float, float, float]:
    parts = [p for p in re.split(r"[,;\s]+", text.strip().strip("()[]")) if p]
    if len(parts) != 3:
        raise errors.InvalidArgumentError(
            "Expected three coordinates like '10,0,25', got %r" % text
        )
    try:
        return (float(parts[0]), float(parts[1]), float(parts[2]))
    except ValueError as exc:
        raise errors.InvalidArgumentError("Coordinates must be numbers: %r" % text) from exc


def make_reference(part: Any, obj: Any) -> Any:
    """Wrap a CATIA object in a Reference, which is what the factories want."""
    if obj is None:
        raise errors.BadReferenceError("Cannot build a reference from a null object.")
    # Already a Reference? CreateReferenceFromObject is happy to re-wrap, but on
    # some releases it errors, so try passing it straight through first.
    label, ref = comutil.try_variants(
        [
            ("CreateReferenceFromObject", lambda: part.CreateReferenceFromObject(obj)),
            ("identity", lambda: obj),
        ],
        what="build a reference",
    )
    logger.debug("make_reference used %s", label)
    return ref


def origin_plane(part: Any, token: str) -> Any:
    member = PLANE_TOKENS[token.lower()]
    origin = comutil.safe(part, "OriginElements")
    if origin is None:
        raise errors.UnsupportedCapabilityError("This part exposes no OriginElements.")
    plane = comutil.safe(origin, member)
    if plane is None:
        raise errors.ElementNotFoundError("Origin plane %s is not available." % member)
    return plane


def find_named(session: Any, name: str) -> Any:
    """Locate any tree element by the name CATIA shows for it."""
    part = comutil.safe(session.active_document(), "Part")

    # FindObjectByName is the cheap path but only exists on Part and only for
    # some element families.
    if part is not None:
        found = comutil.safe_call(part, "FindObjectByName", name)
        if found is not None:
            return found

    selection = session.selection()
    escaped = name.replace("'", "''")
    for query in ("Name=%s,all" % escaped, "Name=%s,sel" % escaped):
        try:
            selection.Clear()
            selection.Search(query)
            if comutil.com_count(selection) > 0:
                obj = selection.Item(1).Value
                selection.Clear()
                return obj
        except Exception as exc:
            logger.debug("Search %r failed: %s", query, exc)
    try:
        selection.Clear()
    except Exception:
        pass

    raise errors.ElementNotFoundError("No element named %r in the active document." % name)


def _search_topology(session: Any, kind: str) -> list[Any]:
    """Return the Selection's selected-element wrappers for every face/edge/vertex."""
    selection = session.selection()
    queries = TOPOLOGY_SEARCH[kind]
    last_error: BaseException | None = None
    for query in queries:
        try:
            selection.Clear()
            selection.Search(query)
        except Exception as exc:
            last_error = exc
            continue
        count = comutil.com_count(selection)
        items = []
        for index in range(1, count + 1):
            try:
                items.append(selection.Item(index))
            except Exception:
                continue
        return items
    if last_error is not None:
        raise errors.translate(last_error)
    return []


def topology_entries(
    session: Any,
    kind: str,
    *,
    with_metrics: bool = True,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """Enumerate the faces / edges / vertices of the active document.

    Each entry carries the 1-based index used by ``face#n`` tokens, plus - when
    ``with_metrics`` is on - the centre of gravity that ``face@x,y,z`` matches
    against, and area or length.
    """
    if kind not in TOPOLOGY_SEARCH:
        raise errors.InvalidArgumentError("kind must be one of face, edge, vertex.")

    part = comutil.safe(session.active_document(), "Part")
    app = session.app
    items = _search_topology(session, kind)
    entries: list[dict[str, Any]] = []

    for index, item in enumerate(items[:limit], start=1):
        value = comutil.safe(item, "Value")
        reference = comutil.safe(item, "Reference")
        if reference is None and part is not None and value is not None:
            reference = comutil.safe_call(part, "CreateReferenceFromObject", value)
        entry: dict[str, Any] = {
            "index": index,
            "token": "%s#%d" % (kind, index),
            "type": str(comutil.safe(item, "Type", "") or ""),
            "owner": str(comutil.safe(comutil.safe(item, "LeafProduct"), "Name", "") or ""),
            "_object": value,
            "_reference": reference,
        }
        if with_metrics and reference is not None:
            entry.update(_metrics(session, app, reference, kind))
        entries.append(entry)

    try:
        session.selection().Clear()
    except Exception:
        pass
    return entries


def _metrics(session: Any, app: Any, reference: Any, kind: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    try:
        measurable = session.measurable(reference)
    except Exception:
        return out

    try:
        cog = comutil.out_doubles(measurable, "GetCOG", 3, app=app)
        out["centroid"] = {
            "x": round(cog[0], 4),
            "y": round(cog[1], 4),
            "z": round(cog[2], 4),
        }
    except Exception:
        pass

    if kind == "face":
        area = comutil.safe(measurable, "Area")
        if area is not None:
            out["area_mm2"] = round(float(area), 4)
        # A planar face also reports its plane, which gives the outward normal -
        # exactly what "sketch on the top face" needs.
        try:
            plane = comutil.out_doubles(measurable, "GetPlane", 9, app=app)
            normal = cross(plane[3:6], plane[6:9])
            if any(abs(c) > 1e-9 for c in normal):
                out["planar"] = True
                out["normal"] = {
                    "x": round(normal[0], 6),
                    "y": round(normal[1], 6),
                    "z": round(normal[2], 6),
                }
        except Exception:
            out["planar"] = False
    elif kind == "edge":
        length = comutil.safe(measurable, "Length")
        if length is not None:
            out["length_mm"] = round(float(length), 4)
    elif kind == "vertex":
        try:
            point = comutil.out_doubles(measurable, "GetPoint", 3, app=app)
            out["position"] = {
                "x": round(point[0], 4),
                "y": round(point[1], 4),
                "z": round(point[2], 4),
            }
        except Exception:
            pass
    return out


def cross(a: Iterable[float], b: Iterable[float]) -> list[float]:
    """Normalised cross product; a zero vector when the inputs are parallel."""
    ax, ay, az = list(a)[:3]
    bx, by, bz = list(b)[:3]
    n = [ay * bz - az * by, az * bx - ax * bz, ax * by - ay * bx]
    norm = math.sqrt(sum(c * c for c in n))
    return [c / norm for c in n] if norm > 1e-12 else n


def nearest_topology(
    session: Any, kind: str, point: tuple[float, float, float]
) -> dict[str, Any]:
    entries = topology_entries(session, kind, with_metrics=True)
    best: dict[str, Any] | None = None
    best_distance = float("inf")
    for entry in entries:
        centre = entry.get("centroid") or entry.get("position")
        if not centre:
            continue
        distance = math.dist(
            (centre["x"], centre["y"], centre["z"]), point
        )
        if distance < best_distance:
            best_distance = distance
            best = entry
    if best is None:
        raise errors.ElementNotFoundError(
            "Found no %s with a measurable position in the active document. "
            "Is the solid hidden, or is the document empty?" % kind
        )
    best = dict(best)
    best["distance_from_query_mm"] = round(best_distance, 4)
    return best


# ── the resolver ─────────────────────────────────────────────────────────────

def resolve(session: Any, token: str, *, part: Any = None) -> Resolved:
    """Turn a reference token into a :class:`Resolved`."""
    if token is None or not str(token).strip():
        raise errors.BadReferenceError("An empty reference token was given.")
    text = str(token).strip()
    lowered = text.lower()

    if part is None:
        part = comutil.safe(session.active_document(), "Part")

    def wrap(obj: Any, label: str, kind: str) -> Resolved:
        if part is None:
            return Resolved(obj, obj, label, kind)
        return Resolved(obj, make_reference(part, obj), label, kind)

    # origin planes
    if lowered in PLANE_TOKENS or lowered.startswith("plane:"):
        key = lowered.split(":", 1)[-1]
        if key in PLANE_TOKENS:
            if part is None:
                raise errors.WrongDocumentTypeError(
                    "Origin planes only exist inside a Part document."
                )
            return wrap(origin_plane(part, key), key.upper() + " plane", "plane")

    # the feature this server made most recently
    if lowered == "last":
        name = session.state.last_feature_name
        if not name:
            raise errors.BadReferenceError(
                "'last' was used but this server has not created a feature yet."
            )
        return wrap(find_named(session, name), name, "feature")

    if lowered in ("sketch", "last_sketch"):
        name = session.state.last_sketch_name
        if not name:
            raise errors.BadReferenceError(
                "'sketch' was used but no sketch has been created in this session. "
                "Pass sketch:<name> instead."
            )
        return wrap(find_named(session, name), name, "sketch")

    if lowered in ("body", "mainbody", "main_body"):
        if part is None:
            raise errors.WrongDocumentTypeError("Bodies only exist inside a Part document.")
        body = comutil.safe(part, "MainBody")
        if body is None:
            raise errors.ElementNotFoundError("This part has no main body.")
        return wrap(body, comutil.name_of(body, "PartBody"), "body")

    match = _TOPO_INDEX.match(text)
    if match:
        kind = match.group(1).lower()
        index = int(match.group(2))
        entries = topology_entries(session, kind, with_metrics=False)
        if not entries:
            raise errors.ElementNotFoundError(
                "The active document exposes no %s geometry. Create a solid first, and "
                "make sure it is visible - Selection.Search skips hidden geometry." % kind
            )
        if index < 1 or index > len(entries):
            raise errors.ElementNotFoundError(
                "%s#%d is out of range; the model has %d %s(s)."
                % (kind, index, len(entries), kind)
            )
        entry = entries[index - 1]
        return Resolved(entry["_object"], entry["_reference"], entry["token"], kind)

    match = _TOPO_NEAR.match(text)
    if match:
        kind = match.group(1).lower()
        point = parse_xyz(match.group(2))
        entry = nearest_topology(session, kind, point)
        label = "%s#%d (%.2f mm from query point)" % (
            kind,
            entry["index"],
            entry["distance_from_query_mm"],
        )
        return Resolved(entry["_object"], entry["_reference"], label, kind)

    match = _COORDS.match(text)
    if match:
        raise errors.BadReferenceError(
            "'point:x,y,z' is a literal coordinate, not a reference to existing geometry. "
            "Create a point first with catia_gsd_point, or use 'vertex@x,y,z' to pick the "
            "nearest existing vertex."
        )

    for prefix, kind in (("name:", "element"), ("sketch:", "sketch"), ("body:", "body")):
        if lowered.startswith(prefix):
            name = text[len(prefix):].strip()
            if kind == "body" and part is not None:
                bodies = comutil.safe(part, "Bodies")
                body = comutil.safe_call(bodies, "Item", name) if bodies is not None else None
                if body is not None:
                    return wrap(body, name, "body")
            return wrap(find_named(session, name), name, kind)

    # Bare name: the common case, and the one CATIA users type.
    return wrap(find_named(session, text), text, "element")


def resolve_reference(session: Any, token: str, *, part: Any = None) -> Any:
    return resolve(session, token, part=part).reference


def resolve_object(session: Any, token: str, *, part: Any = None) -> Any:
    return resolve(session, token, part=part).obj


GRAMMAR_HELP = {
    "tokens": [
        {"token": "xy | yz | zx", "means": "the part's origin planes"},
        {"token": "last", "means": "the most recent feature this server created"},
        {"token": "sketch", "means": "the most recent sketch this server created"},
        {"token": "sketch:Sketch.2", "means": "a sketch by name"},
        {"token": "body | body:PartBody", "means": "the main body, or a body by name"},
        {"token": "Pad.1 | name:Pad.1", "means": "any tree element by its CATIA name"},
        {"token": "face#3 | edge#7 | vertex#2", "means": "n-th topological element (1-based)"},
        {
            "token": "face@12,0,40",
            "means": "the face whose centre of gravity is closest to that point",
        },
        {"token": "edge@0,0,10", "means": "the closest edge to that point"},
    ],
    "guidance": (
        "Index tokens are only valid until the model changes - adding a fillet renumbers "
        "every face. Proximity tokens survive edits, so prefer 'face@x,y,z' once you know "
        "roughly where the geometry is. Call catia_list_faces or catia_list_edges to see "
        "both the current indices and the centroids to aim at."
    ),
    "caveat": (
        "Selection.Search only finds geometry that is visible. If a body is hidden, "
        "show it with catia_show_element before selecting faces or edges on it."
    ),
}
