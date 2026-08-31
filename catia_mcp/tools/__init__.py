"""Tool modules. Each exposes ``register(mcp, session)``."""

from catia_mcp.tools import (
    assembly,
    document,
    drafting,
    dressup,
    exchange,
    gsd,
    knowledge,
    measure,
    part_design,
    session_tools,
    sketch,
    transform,
    tree,
    view,
)

# Registration order decides the order tools are advertised in, which is also
# roughly the order a model should reach for them.
MODULES = [
    session_tools,
    document,
    tree,
    sketch,
    part_design,
    dressup,
    transform,
    gsd,
    assembly,
    measure,
    knowledge,
    drafting,
    view,
    exchange,
]

__all__ = ["MODULES"]
