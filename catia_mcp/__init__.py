"""catia-mcp — a version-adaptive Model Context Protocol server for CATIA.

Targets CATIA V5 (R14 through V5-6Rxxxx) over the Windows COM automation API,
and degrades gracefully on CATIA V6 / 3DEXPERIENCE where the same ProgID is
exposed.  Every CATIA interaction is funnelled through a single STA apartment
thread, guarded by busy-retry logic, and described by a structured result
envelope so that a language model can chain calls reliably.
"""

__version__ = "1.0.0"
__all__ = ["__version__"]
