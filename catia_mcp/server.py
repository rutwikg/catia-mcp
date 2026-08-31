"""Server entry point.

Run with::

    python -m catia_mcp             # stdio, the usual MCP transport
    catia-mcp --transport http      # HTTP, for remote or multi-client use
    catia-mcp --list-tools          # offline inventory, no CATIA needed
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from typing import Any

from catia_mcp import __version__, compat
from catia_mcp.core.apartment import APARTMENT
from catia_mcp.core.connection import SESSION
from catia_mcp.tools import MODULES
from catia_mcp.tools.base import REGISTERED

logger = logging.getLogger("catia_mcp")

INSTRUCTIONS = """
This server drives CATIA on this Windows machine through its COM automation
interface. It targets CATIA V5 (R14 through V5-6Rxxxx) and adapts to whatever
release and licences it finds.

Start here
  1. catia_connect - attaches to a CATIA that is already running. Pass
     launch=true only if you want this server to start CATIA itself.
  2. catia_check_environment - run this first if catia_connect fails; it
     diagnoses Python bitness, pywin32 and COM registration without CATIA.
  3. catia_capabilities - reports which workbenches this installation exposes.

Referring to geometry
  Tools that need to point at geometry take a reference token. Origin planes
  are 'xy', 'yz', 'zx'. Tree elements are 'name:Pad.1' or just 'Pad.1'. Faces
  and edges are 'face#3' / 'edge#7' by index, or 'face@12,0,40' / 'edge@0,0,10'
  to pick whichever is nearest a point. Index tokens are invalidated by any
  change to the model; proximity tokens are not, so prefer them. Call
  catia_reference_help for the full grammar, and catia_list_faces or
  catia_list_edges to discover what is there.

Working effectively
  - catia_describe_tree tells you what is in an unfamiliar document.
  - Every result is JSON with an 'ok' flag. Failures carry an error code and a
    'remediation' field saying what to do about it - read it rather than
    retrying blindly.
  - catia_screenshot returns an actual image, so you can check that geometry
    looks the way you intended instead of inferring it from the feature tree.
  - Wrap long sequences of edits in catia_set_batch_mode(true) and turn it off
    again afterwards.
  - Anything this server has no tool for is still reachable with
    catia_run_script, which evaluates VBScript inside CATIA.

Units are millimetres and degrees throughout unless a field says otherwise.
""".strip()


def configure_logging(level: str = "INFO", log_file: str = "") -> None:
    """Log to stderr and optionally a file - never stdout, which carries MCP traffic."""
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    path = log_file or os.environ.get("CATIA_MCP_LOG", "")
    if path:
        try:
            directory = os.path.dirname(os.path.abspath(path))
            if directory:
                os.makedirs(directory, exist_ok=True)
            handlers.append(logging.FileHandler(path, encoding="utf-8"))
        except OSError as exc:  # pragma: no cover
            print("Could not open log file %s: %s" % (path, exc), file=sys.stderr)

    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        handlers=handlers,
        force=True,
    )


def build_server(mcp: Any = None) -> Any:
    """Create the MCP server and register every tool module.

    ``mcp`` may be supplied to register the tools against something other than
    a real MCP server - scripts/live_smoke.py passes a collector so it can call
    the tool functions directly, with no transport in the way.
    """
    if mcp is None:
        mcp = compat.build_server(
            name="catia-mcp",
            version=__version__,
            instructions=INSTRUCTIONS,
        )
    for module in MODULES:
        module.register(mcp, SESSION)
    logger.info(
        "Registered %d tools across %d modules (MCP SDK: %s)",
        len(REGISTERED),
        len(MODULES),
        compat.SDK_FLAVOUR,
    )
    return mcp


def tool_inventory() -> dict[str, Any]:
    """Group the registered tools by domain, for offline inspection."""
    if not REGISTERED:
        build_server()
    groups: dict[str, list[dict[str, Any]]] = {}
    for entry in REGISTERED:
        groups.setdefault(entry["group"], []).append(
            {"name": entry["name"], "readonly": entry["readonly"]}
        )
    return {
        "version": __version__,
        "tool_count": len(REGISTERED),
        "group_count": len(groups),
        "groups": {name: sorted(items, key=lambda i: i["name"]) for name, items in
                   sorted(groups.items())},
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="catia-mcp",
        description="MCP server for CATIA (V5 and compatible releases) over COM.",
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "http", "sse"],
        default=os.environ.get("CATIA_MCP_TRANSPORT", "stdio"),
        help="Transport to serve on. Default: stdio.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Host for the http transport.")
    parser.add_argument(
        "--port", type=int, default=8765, help="Port for the http transport."
    )
    parser.add_argument(
        "--log-level", default=os.environ.get("CATIA_MCP_LOG_LEVEL", "INFO"),
        help="DEBUG, INFO, WARNING or ERROR.",
    )
    parser.add_argument("--log-file", default="", help="Also write logs to this file.")
    parser.add_argument(
        "--list-tools",
        action="store_true",
        help="Print the tool inventory as JSON and exit. Needs neither CATIA nor Windows.",
    )
    parser.add_argument(
        "--doctor",
        action="store_true",
        help="Print an environment diagnosis as JSON and exit.",
    )
    parser.add_argument("--version", action="version", version="catia-mcp %s" % __version__)
    args = parser.parse_args(argv)

    configure_logging(args.log_level, args.log_file)

    if args.list_tools:
        print(json.dumps(tool_inventory(), indent=2))
        return

    if args.doctor:
        report = SESSION.environment_report()
        report["mcp_sdk"] = compat.sdk_info()
        report["tools"] = len(REGISTERED) or tool_inventory()["tool_count"]
        print(json.dumps(report, indent=2))
        return

    mcp = build_server()
    logger.info("Starting catia-mcp %s on %s transport", __version__, args.transport)

    try:
        if args.transport == "stdio":
            mcp.run("stdio")
        elif args.transport == "sse":
            mcp.run("sse", host=args.host, port=args.port)
        else:
            mcp.run("streamable-http", host=args.host, port=args.port)
    except KeyboardInterrupt:  # pragma: no cover
        logger.info("Interrupted; shutting down.")
    finally:
        APARTMENT.shutdown()


if __name__ == "__main__":
    main()
