"""Tool registration helpers.

Every tool in this server is registered through :func:`registrar`, which gives
all of them the same three guarantees:

* the body runs on the COM apartment thread (see ``core.apartment``);
* a live CATIA connection is established first, unless the tool opts out;
* nothing raises - failures come back as the structured envelope in
  ``core.result``, so the model can read the error code and remediation.
"""

from __future__ import annotations

import functools
import logging
import os
from collections.abc import Callable
from typing import Any

from catia_mcp.core import comutil, errors, result

logger = logging.getLogger("catia_mcp.tools")

try:  # ToolAnnotations arrived in MCP SDK 1.5; degrade quietly without it.
    from mcp.types import ToolAnnotations
except Exception:  # pragma: no cover
    ToolAnnotations = None  # type: ignore[assignment]

REGISTERED: list[dict[str, Any]] = []
SKIPPED: list[dict[str, str]] = []

# Groups always registered, whatever CATIA_MCP_GROUPS says: without them the
# model cannot connect, diagnose a failure, or learn the reference grammar.
ALWAYS_ON = {"session"}


def enabled_groups() -> set[str] | None:
    """Groups to register, from CATIA_MCP_GROUPS. ``None`` means all of them.

    A full CATIA surface is over 150 tools. Clients with a tight context budget
    can narrow it to, say, ``session,document,tree,sketch,part_design`` without
    losing the ability to diagnose or connect.
    """
    raw = os.environ.get("CATIA_MCP_GROUPS", "").strip()
    if not raw:
        return None
    wanted = {part.strip().lower() for part in raw.split(",") if part.strip()}
    return wanted | ALWAYS_ON if wanted else None


def registrar(mcp: Any, session: Any) -> Callable[..., Callable[[Callable], Callable]]:
    """Build the ``@tool(...)`` decorator bound to one server and session."""

    def tool(
        name: str,
        description: str,
        *,
        connect: bool = True,
        readonly: bool = False,
        destructive: bool = False,
        idempotent: bool = False,
        group: str = "",
    ) -> Callable[[Callable], Callable]:
        def decorate(fn: Callable) -> Callable:
            resolved_group = group or fn.__module__.rsplit(".", 1)[-1]
            allowed = enabled_groups()
            if allowed is not None and resolved_group not in allowed:
                SKIPPED.append({"name": name, "group": resolved_group})
                return fn

            @functools.wraps(fn)
            def entry(*args: Any, **kwargs: Any) -> Any:
                def body() -> Any:
                    if connect:
                        session.ensure()
                    return fn(*args, **kwargs)

                return session.call(body)

            guarded = result.tool(entry, name)

            kwargs: dict[str, Any] = {"name": name, "description": description}
            if ToolAnnotations is not None:
                try:
                    kwargs["annotations"] = ToolAnnotations(
                        title=name.replace("catia_", "").replace("_", " ").title(),
                        readOnlyHint=readonly,
                        destructiveHint=destructive and not readonly,
                        idempotentHint=idempotent,
                        openWorldHint=False,
                    )
                except Exception:  # pragma: no cover - older annotation shapes
                    kwargs.pop("annotations", None)

            try:
                mcp.tool(**kwargs)(guarded)
            except TypeError:
                # Very old SDKs take only (name, description).
                kwargs.pop("annotations", None)
                mcp.tool(**kwargs)(guarded)

            REGISTERED.append(
                {"name": name, "group": resolved_group, "readonly": readonly}
            )
            return fn

        return decorate

    return tool


# ── small shared helpers used by many tool modules ───────────────────────────

def feature_summary(session: Any, obj: Any, *, kind: str = "feature") -> dict[str, Any]:
    """Standard 'what did I just create' payload."""
    name = comutil.name_of(obj)
    if name:
        session.state.note_feature(name)
    return {
        "created": name,
        "kind": kind,
        "reference_token": "name:%s" % name if name else None,
    }


def rename(obj: Any, new_name: str | None) -> str:
    """Apply a caller-supplied name, tolerating CATIA's naming restrictions."""
    if not new_name:
        return comutil.name_of(obj)
    try:
        obj.Name = new_name
    except Exception as exc:
        logger.info("Could not rename to %r: %s", new_name, errors.com_message(exc))
    return comutil.name_of(obj)


def require_positive(value: float, label: str) -> float:
    if value is None or float(value) <= 0:
        raise errors.InvalidArgumentError("%s must be greater than zero (got %r)." % (label, value))
    return float(value)


def set_parameter_value(owner: Any, member: str, value: float) -> bool:
    """Set a CATIA length/angle parameter, whether it is a scalar or an object.

    Feature properties such as ``Pad.FirstLimit.Dimension`` are Parameter
    objects with a ``.Value``; a few are plain doubles. Handle both.
    """
    try:
        target = getattr(owner, member)
    except Exception:
        return False
    try:
        target.Value = float(value)
        return True
    except Exception:
        pass
    try:
        setattr(owner, member, float(value))
        return True
    except Exception:
        return False
