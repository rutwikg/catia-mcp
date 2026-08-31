"""Compatibility shim across MCP Python SDK generations.

The high-level server class was called ``FastMCP`` up to SDK 1.2x and is called
``MCPServer`` from 1.29 onwards; the ``Image`` helper moved with it. Both expose
the same ``.tool()`` decorator and ``.run()`` entry point, so one shim keeps the
rest of the codebase free of version checks - and keeps this server installable
next to whichever SDK a user already has.
"""

from __future__ import annotations

from typing import Any

_IMPORT_ERROR: str | None = None
ServerClass: Any = None
ImageClass: Any = None
SDK_FLAVOUR = "unknown"

try:  # SDK >= 1.29
    from mcp.server import MCPServer as _MCPServer
    from mcp.server.mcpserver import Image as _Image

    ServerClass = _MCPServer
    ImageClass = _Image
    SDK_FLAVOUR = "mcpserver"
except Exception as exc:  # pragma: no cover - depends on installed SDK
    _IMPORT_ERROR = str(exc)

if ServerClass is None:
    try:  # SDK < 1.29
        from mcp.server.fastmcp import FastMCP as _FastMCP
        from mcp.server.fastmcp import Image as _Image  # type: ignore[no-redef]

        ServerClass = _FastMCP
        ImageClass = _Image
        SDK_FLAVOUR = "fastmcp"
        _IMPORT_ERROR = None
    except Exception as exc:  # pragma: no cover
        _IMPORT_ERROR = "%s / %s" % (_IMPORT_ERROR, exc)


def build_server(**kwargs: Any) -> Any:
    if ServerClass is None:  # pragma: no cover - broken install
        raise RuntimeError(
            "No usable MCP server class found in the installed 'mcp' package (%s). "
            "Install a supported SDK with: pip install 'mcp>=1.2'" % _IMPORT_ERROR
        )
    return ServerClass(**kwargs)


def make_image(data: bytes, fmt: str) -> Any:
    """Wrap raw bytes so the SDK emits an MCP ImageContent block."""
    if ImageClass is None:  # pragma: no cover
        raise RuntimeError("The installed MCP SDK exposes no Image helper.")
    return ImageClass(data=data, format=fmt)


def sdk_info() -> dict[str, Any]:
    version = "unknown"
    try:
        from importlib.metadata import version as _version

        version = _version("mcp")
    except Exception:
        pass
    return {"flavour": SDK_FLAVOUR, "mcp_version": version, "import_error": _IMPORT_ERROR}
