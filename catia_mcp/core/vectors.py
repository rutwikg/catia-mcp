"""Small 3D vector helpers, used to keep degenerate geometry out of CATIA.

CATIA's update engine rejects a direction pair it cannot build a plane or an
axis from - two parallel vectors, or one of zero length - with

    Colinear directions : cannot build a plane or an axis.

That arrives as a *modal dialog* during ``Update()``, not as a return value, so
it blocks every subsequent automation call until somebody clicks OK. The only
robust answer is never to hand CATIA such a pair in the first place, which is
what these helpers are for.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence

# Below this, a vector is treated as having no direction at all.
ZERO_TOLERANCE = 1e-9

# sin(angle) below this counts as parallel. About 0.006 degrees - tight enough
# not to reject legitimately close directions, loose enough to catch the
# floating-point near-misses that make CATIA fail.
PARALLEL_TOLERANCE = 1e-7


def as_vector(values: Iterable[float] | None, length: int = 3) -> list[float]:
    """Coerce an input to a list of floats, padding or truncating to ``length``."""
    if values is None:
        return [0.0] * length
    out = [float(v) for v in list(values)[:length]]
    out.extend([0.0] * (length - len(out)))
    return out


def magnitude(vector: Sequence[float]) -> float:
    return math.sqrt(sum(float(v) * float(v) for v in vector))


def is_zero(vector: Sequence[float], tolerance: float = ZERO_TOLERANCE) -> bool:
    return magnitude(vector) <= tolerance


def normalize(vector: Sequence[float]) -> list[float] | None:
    """Unit vector, or ``None`` when the input has no usable direction."""
    norm = magnitude(vector)
    if norm <= ZERO_TOLERANCE:
        return None
    return [float(v) / norm for v in vector]


def dot(a: Sequence[float], b: Sequence[float]) -> float:
    return sum(float(x) * float(y) for x, y in zip(a, b, strict=False))


def cross(a: Sequence[float], b: Sequence[float]) -> list[float]:
    ax, ay, az = (float(v) for v in list(a)[:3])
    bx, by, bz = (float(v) for v in list(b)[:3])
    return [ay * bz - az * by, az * bx - ax * bz, ax * by - ay * bx]


def unit_cross(a: Sequence[float], b: Sequence[float]) -> list[float] | None:
    """Normalised cross product, or ``None`` when the inputs are parallel."""
    return normalize(cross(a, b))


def are_parallel(a: Sequence[float], b: Sequence[float]) -> bool:
    """True when two vectors are parallel, antiparallel, or either is zero.

    Compares against the *normalised* cross product, so the answer does not
    depend on how long the inputs happen to be - which a bare
    ``magnitude(cross(a, b)) < tol`` test would.
    """
    unit_a = normalize(a)
    unit_b = normalize(b)
    if unit_a is None or unit_b is None:
        return True
    return magnitude(cross(unit_a, unit_b)) <= PARALLEL_TOLERANCE


def reject(vector: Sequence[float], normal: Sequence[float]) -> list[float]:
    """The component of ``vector`` lying in the plane whose normal is ``normal``."""
    unit_normal = normalize(normal)
    if unit_normal is None:
        return [float(v) for v in vector]
    scale = dot(vector, unit_normal)
    return [float(v) - scale * n for v, n in zip(vector, unit_normal, strict=False)]


def angle_between_deg(a: Sequence[float], b: Sequence[float]) -> float | None:
    unit_a = normalize(a)
    unit_b = normalize(b)
    if unit_a is None or unit_b is None:
        return None
    return math.degrees(math.acos(max(-1.0, min(1.0, dot(unit_a, unit_b)))))


def describe(vector: Sequence[float], digits: int = 4) -> str:
    """Render a vector for an error message."""
    return "(%s)" % ", ".join("%g" % round(float(v), digits) for v in vector)


def check_direction_pair(
    first: Sequence[float],
    second: Sequence[float],
    *,
    first_label: str,
    second_label: str,
) -> str:
    """Return a problem description, or '' when the pair is usable.

    This is the guard that stops CATIA's modal "Colinear directions" dialog.
    """
    if is_zero(first):
        return "%s is a zero-length vector %s, which has no direction." % (
            first_label,
            describe(first),
        )
    if is_zero(second):
        return "%s is a zero-length vector %s, which has no direction." % (
            second_label,
            describe(second),
        )
    if are_parallel(first, second):
        angle = angle_between_deg(first, second)
        return (
            "%s %s and %s %s are colinear (%.4f degrees apart). CATIA cannot build a "
            "plane or an axis from two parallel directions."
            % (
                first_label,
                describe(first),
                second_label,
                describe(second),
                angle if angle is not None else 0.0,
            )
        )
    return ""
