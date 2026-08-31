# Changelog

## 1.0.0

First release. A ground-up implementation, written after reviewing
[an earlier implementation](https://example.invalid/prior-art);
no code is shared with it.

### Correctness

- **By-reference output arrays are actually read.** CATIA declares
  `Measurable::GetCOG`, `GetInertia`, `GetBoundingBox`,
  `GetMinimumDistancePoints`, `GetPlane` and `Product::Position::GetComponents`
  as taking an `[in]` array it writes into. A late-bound Python list receives a
  copy, so those calls appear to succeed while returning the zeros they were
  given. They are now read through a `VT_BYREF` VARIANT, with a VBScript
  trampoline evaluated inside CATIA as a second, independent mechanism.
- **Real topological selection.** Fillet, chamfer, draft, shell, thickness,
  thread, hole and remove-face resolve genuine references through
  `Selection.Search`, addressed by index (`edge#4`) or by proximity to a point
  (`edge@20,0,10`).
- **COM threading.** All CATIA calls run on one dedicated STA apartment thread
  that owns `CoInitialize`, with bounded exponential backoff on
  `RPC_E_CALL_REJECTED` and `RPC_E_SERVERCALL_RETRYLATER`, and transparent
  re-attachment after a lost connection.
- **Capture formats are probed, not assumed.** `CatCaptureFormat` values are
  discovered by writing one throwaway capture per candidate and reading the
  file's magic bytes, so a screenshot cannot silently be written in the wrong
  format.

### Compatibility

- Version and service-pack detection through `SystemConfiguration`, with a
  caption-parsing fallback.
- Runtime capability probing per workbench, because licences rather than
  release numbers decide what is available.
- Adaptive invocation: factory calls try each known signature in turn, so one
  code path serves old and new releases.
- Site-overridable enumeration values via `CATIA_MCP_CONSTANTS`.
- MCP SDK shim covering both `MCPServer` (1.29+) and `FastMCP` (earlier).

### Interface

- Structured JSON results throughout, with a stable error taxonomy, decoded
  HRESULTs and a `remediation` field on every failure.
- `catia_screenshot` returns an MCP image, downscaled through Pillow when it is
  installed.
- `catia_check_environment` diagnoses the host with no CATIA connection.
- `catia_reference_help` documents the geometry-selection grammar.
- `catia_run_script` evaluates VBScript inside CATIA as an escape hatch.
- Tool annotations mark read-only, destructive and idempotent tools.
- `CATIA_MCP_GROUPS` trims the registered tool surface for tight context budgets.
- Connecting no longer launches CATIA implicitly; `launch=true` is explicit.

### Coverage

161 tools across 14 groups, adding drafting, Knowledge parameters and formulas,
design tables, materials and mass properties, boolean body operations,
transformation features, bills of materials, CSV export, axis systems, helices,
and considerably wider Part Design and GSD coverage.

### Testing

- 36 offline tests covering error classification, adaptive invocation, the
  reference grammar, the result envelope, assembly matrix maths and
  capture-format probing. No CATIA, no Windows, no COM required.
- `scripts/protocol_check.py` performs a real MCP handshake over stdio and
  validates every tool schema.
- `scripts/live_smoke.py` builds a part end to end against a running CATIA and
  reports PASS/FAIL/SKIP per step.
