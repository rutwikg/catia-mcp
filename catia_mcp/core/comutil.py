"""Low-level COM plumbing that the rest of the server relies on.

Three problems are solved here.

**By-reference output arrays.** CATIA declares its most useful measurement
methods with an ``[in]`` ``CATSafeArrayVariant`` that it writes into --
``Measurable::GetCOG``, ``GetBoundingBox``, ``GetInertia``, ``Move::GetComponents``,
``Viewpoint3D::GetSightDirection`` and friends. Handing a Python list to a
late-bound call gives CATIA a *copy*, so the values it writes are discarded and
the caller silently reads back zeros. Two independent mechanisms are used
instead: a ``VARIANT`` flagged ``VT_BYREF`` (fast, works on modern pywin32) and,
as a fallback, a one-shot VBScript evaluated inside CATIA itself.

**Release drift.** ``ShapeFactory`` and ``HybridShapeFactory`` gained
``...FromRef`` overloads and extra parameters over the years. ``try_variants``
walks a list of candidate calls and keeps the first that CATIA accepts, so one
code path serves V5R16 and V5-6R2024.

**Defensive reads.** Anything that walks the specification tree has to tolerate
members that simply are not there on a given object.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator, Sequence
from typing import Any

from catia_mcp.core import errors

logger = logging.getLogger("catia_mcp.comutil")

try:  # pragma: no cover - Windows only
    import pythoncom
    import win32com.client
    from win32com.client import VARIANT

    HAS_PYWIN32 = True
    PYWIN32_IMPORT_ERROR: str | None = None
except Exception as _exc:  # pragma: no cover - non-Windows dev boxes
    pythoncom = None  # type: ignore[assignment]
    win32com = None  # type: ignore[assignment]
    VARIANT = None  # type: ignore[assignment]
    HAS_PYWIN32 = False
    PYWIN32_IMPORT_ERROR = str(_exc)


UNSET = object()


# ── defensive attribute access ───────────────────────────────────────────────

def safe(obj: Any, attr: str, default: Any = None) -> Any:
    """Read ``obj.attr`` and return ``default`` if CATIA refuses."""
    try:
        return getattr(obj, attr)
    except Exception:
        return default


def safe_call(obj: Any, method: str, *args: Any, default: Any = None) -> Any:
    """Invoke ``obj.method(*args)`` and return ``default`` if CATIA refuses."""
    try:
        return getattr(obj, method)(*args)
    except Exception:
        return default


def has_member(obj: Any, name: str) -> bool:
    """True when the COM object exposes ``name``.

    Late-bound dispatch objects answer ``hasattr`` optimistically, so the value
    has to actually be fetched to know.
    """
    try:
        getattr(obj, name)
        return True
    except Exception:
        return False


def com_iter(collection: Any) -> Iterator[Any]:
    """Iterate a CATIA collection, which is 1-based and not Python-iterable."""
    if collection is None:
        return
    try:
        count = int(collection.Count)
    except Exception:
        return
    for index in range(1, count + 1):
        try:
            yield collection.Item(index)
        except Exception as exc:  # a deleted or unresolved item
            logger.debug("Skipping collection item %d: %s", index, exc)


def com_count(collection: Any) -> int:
    try:
        return int(collection.Count)
    except Exception:
        return 0


def name_of(obj: Any, default: str = "") -> str:
    value = safe(obj, "Name", default)
    return str(value) if value is not None else default


# ── adaptive invocation across CATIA releases ────────────────────────────────

def try_variants(
    variants: Sequence[tuple[str, Callable[[], Any]]],
    *,
    what: str,
) -> tuple[str, Any]:
    """Try each candidate call in order; return ``(label, result)`` of the first
    that succeeds.

    ``RPC_E_CALL_REJECTED`` and lost-connection errors are re-raised
    immediately - they say nothing about whether the signature was right, and
    retrying a different overload against a busy CATIA only compounds the
    problem. Everything else advances to the next candidate.
    """
    first_error: BaseException | None = None
    attempted: list[str] = []

    for label, call in variants:
        try:
            return label, call()
        except BaseException as exc:  # noqa: BLE001 - classified below
            if errors.is_retryable(exc) or errors.is_dead(exc):
                raise
            attempted.append("%s (%s)" % (label, errors.com_message(exc)))
            if first_error is None:
                first_error = exc
            logger.debug("Variant %r for %s failed: %s", label, what, exc)

    translated = errors.translate(first_error) if first_error else None
    message = "Could not %s on this CATIA release." % what
    raise errors.OperationFailedError(
        message,
        details={
            "attempts": attempted,
            "first_error": translated.to_dict() if translated else None,
        },
    )


# ── by-reference output arrays ───────────────────────────────────────────────

_VBA_TEMPLATE = """Function {func}(o)
    Dim buf({last})
    o.{method}{args} buf
    {func} = buf
End Function
"""


def out_doubles(
    obj: Any,
    method: str,
    count: int,
    *,
    app: Any = None,
    args: Sequence[Any] = (),
) -> list[float]:
    """Call ``obj.method(*args, out_array)`` and return the array CATIA filled.

    Tries a ``VT_BYREF`` VARIANT first, then a VBScript trampoline evaluated
    inside CATIA. Raises if neither mechanism produces a plausible result.
    """
    errors_seen: list[str] = []

    variant_result = _out_doubles_variant(obj, method, count, args)
    if variant_result is not None:
        if any(abs(v) > 1e-12 for v in variant_result):
            return variant_result
        # An all-zero answer is legitimate (a point at the origin), but it is
        # also exactly what a failed by-ref write looks like. Cross-check with
        # the script bridge when one is available; otherwise accept it.
        if app is None:
            return variant_result
        script_result = _out_doubles_script(app, obj, method, count, args, errors_seen)
        return script_result if script_result is not None else variant_result

    if app is not None:
        script_result = _out_doubles_script(app, obj, method, count, args, errors_seen)
        if script_result is not None:
            return script_result

    raise errors.UnsupportedCapabilityError(
        "Could not read the output array from %s(). Neither a by-reference VARIANT "
        "nor the CATIA script bridge worked here." % method,
        details={"attempts": errors_seen} if errors_seen else None,
    )


def _out_doubles_variant(
    obj: Any, method: str, count: int, args: Sequence[Any]
) -> list[float] | None:
    if not HAS_PYWIN32:
        return None
    try:
        buffer = VARIANT(
            pythoncom.VT_BYREF | pythoncom.VT_ARRAY | pythoncom.VT_R8, [0.0] * count
        )
        getattr(obj, method)(*args, buffer)
        values = list(buffer.value or [])
    except Exception as exc:
        if errors.is_retryable(exc) or errors.is_dead(exc):
            raise
        logger.debug("VT_BYREF path failed for %s: %s", method, exc)
        return None
    if len(values) < count:
        return None
    return [float(v) for v in values[:count]]


def _out_doubles_script(
    app: Any,
    obj: Any,
    method: str,
    count: int,
    args: Sequence[Any],
    errors_seen: list[str],
) -> list[float] | None:
    """Run a VBScript inside CATIA that owns the array and returns it by value."""
    func = "catia_mcp_out"
    # Literal-only extra arguments: everything we need this for (GetPointOnCurve
    # ratios, for instance) is numeric.
    arg_text = ""
    if args:
        arg_text = " " + ", ".join(_vb_literal(a) for a in args) + ","
    script = _VBA_TEMPLATE.format(func=func, last=max(count - 1, 0), method=method, args=arg_text)
    try:
        result = app.SystemService.Evaluate(script, 0, func, [obj])
    except Exception as exc:
        if errors.is_retryable(exc) or errors.is_dead(exc):
            raise
        errors_seen.append("script bridge: %s" % errors.com_message(exc))
        return None
    try:
        values = [float(v) for v in result]
    except Exception:
        errors_seen.append("script bridge returned %r" % (result,))
        return None
    if len(values) < count:
        errors_seen.append("script bridge returned %d of %d values" % (len(values), count))
        return None
    return values[:count]


def _vb_literal(value: Any) -> str:
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, (int, float)):
        return repr(value)
    text = str(value).replace('"', '""')
    return '"%s"' % text


def in_doubles(values: Sequence[float]) -> Any:
    """Package a sequence of floats as a SAFEARRAY CATIA will accept as input."""
    data = [float(v) for v in values]
    if not HAS_PYWIN32:
        return data
    try:
        return VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, data)
    except Exception:
        return data


def evaluate_script(app: Any, script: str, function: str, params: Sequence[Any] = ()) -> Any:
    """Evaluate a VBScript snippet inside CATIA and return its value.

    ``SystemService.Evaluate`` compiles the text in-process, so this is the
    escape hatch to any part of the CATIA object model that has no convenient
    late-bound form - and to anything this server has no dedicated tool for.
    """
    last_error: BaseException | None = None
    for language in (0, 1, 2):
        try:
            return app.SystemService.Evaluate(script, language, function, list(params))
        except Exception as exc:
            if errors.is_retryable(exc) or errors.is_dead(exc):
                raise
            last_error = exc
    raise errors.translate(last_error) if last_error else errors.OperationFailedError(
        "SystemService.Evaluate is unavailable on this CATIA release."
    )
