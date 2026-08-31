"""Error taxonomy and COM HRESULT decoding.

CATIA reports almost every problem as a generic ``pywintypes.com_error``. The
useful information is buried in the HRESULT and in the ``excepinfo`` tuple.
This module turns that into a stable, machine-readable error code plus a
remediation hint the model can act on without a human round trip.
"""

from __future__ import annotations

import re
from typing import Any

# HRESULTs that mean "CATIA is busy, try again".
# These are raised while a modal dialog is open, while the user is dragging the
# viewport, or while CATIA is mid-update. They are transient by definition.
RPC_E_CALL_REJECTED = -2147418111  # 0x80010001
RPC_E_SERVERCALL_RETRYLATER = -2147417846  # 0x8001010A
RPC_E_SERVERCALL_REJECTED = -2147417843  # 0x8001010D
RPC_E_CANTCALLOUT_ININPUTSYNCCALL = -2147417842  # 0x8001010E

RETRYABLE_HRESULTS = frozenset(
    {
        RPC_E_CALL_REJECTED,
        RPC_E_SERVERCALL_RETRYLATER,
        RPC_E_SERVERCALL_REJECTED,
        RPC_E_CANTCALLOUT_ININPUTSYNCCALL,
    }
)

# HRESULTs that mean "the CATIA process went away".
RPC_E_DISCONNECTED = -2147417848  # 0x80010108
RPC_S_SERVER_UNAVAILABLE = -2147023174  # 0x800706BA
RPC_E_SERVER_DIED = -2147417851  # 0x80010105
CO_E_OBJNOTCONNECTED = -2147220995  # 0x800401FD

DEAD_HRESULTS = frozenset(
    {
        RPC_E_DISCONNECTED,
        RPC_S_SERVER_UNAVAILABLE,
        RPC_E_SERVER_DIED,
        CO_E_OBJNOTCONNECTED,
    }
)

DISP_E_MEMBERNOTFOUND = -2147352573  # 0x80020003
DISP_E_UNKNOWNNAME = -2147352570  # 0x80020006
DISP_E_BADPARAMCOUNT = -2147352562  # 0x8002000E
DISP_E_TYPEMISMATCH = -2147352571  # 0x80020005

MISSING_MEMBER_HRESULTS = frozenset(
    {DISP_E_MEMBERNOTFOUND, DISP_E_UNKNOWNNAME, DISP_E_BADPARAMCOUNT}
)


class CatiaError(Exception):
    """Base class for every error this server reports to the model."""

    code = "catia_error"
    remediation = ""

    def __init__(self, message: str, *, remediation: str = "", details: Any = None) -> None:
        super().__init__(message)
        self.message = message
        if remediation:
            self.remediation = remediation
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.remediation:
            payload["remediation"] = self.remediation
        if self.details is not None:
            payload["details"] = self.details
        return payload


class NotConnectedError(CatiaError):
    code = "not_connected"
    remediation = (
        "Call catia_connect first. If CATIA is not running, either start it manually "
        "or call catia_connect with launch=true."
    )


class PlatformError(CatiaError):
    code = "unsupported_platform"
    remediation = (
        "This server drives CATIA through Windows COM. It must run on Windows, in a "
        "Python interpreter that has pywin32 installed."
    )


class CatiaBusyError(CatiaError):
    code = "catia_busy"
    remediation = (
        "CATIA rejected the call because it is busy - usually a modal dialog is open, "
        "or a command is still running. Dismiss any dialog in the CATIA window, then retry."
    )


class CatiaGoneError(CatiaError):
    code = "catia_disconnected"
    remediation = "The CATIA process is no longer reachable. Call catia_connect to reattach."


class WrongDocumentTypeError(CatiaError):
    code = "wrong_document_type"
    remediation = (
        "Switch to a document of the required type with catia_activate_document, or "
        "create one with catia_new_part / catia_new_product / catia_new_drawing."
    )


class NoActiveDocumentError(CatiaError):
    code = "no_active_document"
    remediation = "Create or open a document first (catia_new_part, catia_open_document)."


class ElementNotFoundError(CatiaError):
    code = "element_not_found"
    remediation = (
        "Use catia_describe_tree, catia_list_features, catia_list_faces or catia_list_edges "
        "to discover valid names and reference tokens."
    )


class BadReferenceError(CatiaError):
    code = "bad_reference"
    remediation = (
        "Reference tokens look like 'xy', 'name:Pad.1', 'face#3', 'edge@10,0,5' or 'last'. "
        "Call catia_reference_help for the full grammar."
    )


class UnsupportedCapabilityError(CatiaError):
    code = "unsupported_capability"
    remediation = (
        "This CATIA installation or release does not expose the required workbench or API. "
        "Call catia_capabilities to see what is available here."
    )


class InvalidArgumentError(CatiaError):
    code = "invalid_argument"


class OperationFailedError(CatiaError):
    """A CATIA API call was made correctly but CATIA refused the operation."""

    code = "operation_failed"
    remediation = (
        "CATIA rejected the geometric operation. Check that the referenced geometry still "
        "exists, that the profile is closed where required, and that the values are "
        "physically achievable (for example a fillet radius larger than the adjacent face)."
    )


# CATIA reports geometric refusals as free text on an otherwise generic
# HRESULT. Recognising the common ones turns "operation failed" into something
# a caller can act on.
GEOMETRY_HINTS: tuple[tuple[str, str], ...] = (
    (
        r"(?i)colinear|collinear",
        "Two directions given to CATIA were parallel (or one was zero length), so it "
        "could not build a plane or an axis from them. Check any direction vectors, "
        "axis references or three-point constructions in the call - three points on a "
        "straight line define no plane, and two parallel edges define no axis system.",
    ),
    (
        r"(?i)not closed|open profile",
        "The profile is not closed. catia_sketch_geometry lists the sketch elements; the "
        "rectangle, polygon and polyline tools weld their corner points explicitly, so "
        "prefer those over drawing separate lines.",
    ),
    (
        r"(?i)self.?intersect",
        "The profile intersects itself. Simplify it, or build the shape from two features.",
    ),
    (
        r"(?i)no (solid|material)|empty result",
        "The operation produced no material. Check the direction and depth - a pocket "
        "that misses the solid entirely fails this way.",
    ),
    (
        r"(?i)radius.*too|too (large|big)",
        "The value is too large for the surrounding geometry. catia_list_edges reports "
        "each edge's length as a rough ceiling for a fillet radius.",
    ),
)


def geometry_hint(text: str) -> str:
    """Map CATIA's own wording onto actionable advice, when it is recognisable."""
    for pattern, advice in GEOMETRY_HINTS:
        if re.search(pattern, text):
            return advice
    return ""


def hresult_of(exc: BaseException) -> int | None:
    """Extract the HRESULT from a pywin32 com_error, if there is one."""
    args = getattr(exc, "args", None)
    if not args:
        return None
    first = args[0]
    if isinstance(first, int):
        return first
    return None


def _scode_of(exc: BaseException) -> int | None:
    """CATIA often stashes the real failure code in excepinfo[5] (scode)."""
    args = getattr(exc, "args", None)
    if not args or len(args) < 3:
        return None
    excepinfo = args[2]
    if isinstance(excepinfo, tuple) and len(excepinfo) >= 6 and isinstance(excepinfo[5], int):
        return excepinfo[5]
    return None


def com_message(exc: BaseException) -> str:
    """Best-effort human-readable text out of a com_error."""
    args = getattr(exc, "args", None)
    if not args:
        return str(exc)
    parts: list[str] = []
    if len(args) >= 2 and isinstance(args[1], str) and args[1]:
        parts.append(args[1].strip())
    if len(args) >= 3 and isinstance(args[2], tuple):
        excepinfo = args[2]
        # excepinfo = (wCode, source, description, helpFile, helpContext, scode)
        for idx in (1, 2):
            if len(excepinfo) > idx and isinstance(excepinfo[idx], str) and excepinfo[idx]:
                text = excepinfo[idx].strip()
                if text and text not in parts:
                    parts.append(text)
    if not parts:
        return str(exc)
    return " - ".join(parts)


def is_retryable(exc: BaseException) -> bool:
    return hresult_of(exc) in RETRYABLE_HRESULTS


def is_dead(exc: BaseException) -> bool:
    return hresult_of(exc) in DEAD_HRESULTS


def is_missing_member(exc: BaseException) -> bool:
    """True when CATIA does not expose the method/property we tried to use.

    This is how a release difference shows up at runtime, and it is the signal
    the adaptive-invocation helper uses to move on to the next candidate call.
    """
    if isinstance(exc, AttributeError):
        return True
    if hresult_of(exc) in MISSING_MEMBER_HRESULTS:
        return True
    return bool(re.search(r"(?i)member not found|unknown name|bad variable type", str(exc)))


def translate(exc: BaseException) -> CatiaError:
    """Map any exception raised inside a COM call onto our taxonomy."""
    if isinstance(exc, CatiaError):
        return exc

    hr = hresult_of(exc)
    scode = _scode_of(exc)
    text = com_message(exc)

    if hr in RETRYABLE_HRESULTS:
        return CatiaBusyError("CATIA is busy and rejected the call (%s)" % text)
    if hr in DEAD_HRESULTS:
        return CatiaGoneError("Lost the connection to CATIA (%s)" % text)
    if hr in MISSING_MEMBER_HRESULTS or isinstance(exc, AttributeError):
        return UnsupportedCapabilityError(
            "CATIA does not expose that API on this release (%s)" % text
        )

    details: dict[str, Any] = {"exception": type(exc).__name__}
    if hr is not None:
        details["hresult"] = "0x%08X" % (hr & 0xFFFFFFFF)
    if scode is not None and scode != hr:
        details["scode"] = "0x%08X" % (scode & 0xFFFFFFFF)

    failure = OperationFailedError(text or str(exc), details=details)
    hint = geometry_hint(text or str(exc))
    if hint:
        failure.remediation = hint
    return failure
