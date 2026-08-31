"""The single result envelope every tool returns.

A model chains CAD operations far more reliably when each call answers three
questions in a fixed shape: did it work, what exactly was created (so the next
call can reference it), and what should be tried next if it did not.
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Callable
from typing import Any

from catia_mcp.core import errors

logger = logging.getLogger("catia_mcp.result")


def ok(
    data: dict[str, Any] | None = None,
    *,
    message: str = "",
    hint: str = "",
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"ok": True}
    if message:
        payload["message"] = message
    if data:
        payload.update(data)
    if hint:
        payload["hint"] = hint
    if warnings:
        payload["warnings"] = warnings
    return payload


def fail(error: errors.CatiaError) -> dict[str, Any]:
    return {"ok": False, "error": error.to_dict()}


def tool(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap a tool implementation so no exception ever escapes as a protocol error.

    An MCP transport-level error is opaque to the model; a structured failure it
    can read is not. Everything is translated into the taxonomy in
    ``catia_mcp.core.errors`` and returned as data.
    """

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            result = fn(*args, **kwargs)
        except errors.CatiaError as exc:
            logger.info("%s failed: %s", fn.__name__, exc.message)
            return fail(exc)
        except Exception as exc:  # noqa: BLE001 - deliberate catch-all boundary
            translated = errors.translate(exc)
            logger.exception("%s raised", fn.__name__)
            return fail(translated)
        if isinstance(result, dict) and "ok" not in result:
            result = ok(result)
        return result

    return wrapper


def round_xyz(values: Any, digits: int = 4) -> dict[str, float]:
    x, y, z = (float(v) for v in list(values)[:3])
    return {"x": round(x, digits), "y": round(y, digits), "z": round(z, digits)}
