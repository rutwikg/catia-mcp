# Changelog

## 1.0.1

### Fixed

- **`catia_create_sketch` could crash CATIA when given `origin` or
  `horizontal_direction`.** `_apply_axis_data` read the sketch's current axis
  with `out_doubles(...)` but without `app=`, so there was no script-bridge
  fallback; when the by-reference write did not land, `out_doubles` returned
  nine zeros and - with no `app` to cross-check against - accepted them. Only
  the caller's origin was then overwritten, leaving the H and V directions at
  `(0,0,0)`: zero length and mutually colinear. `SetAbsoluteAxisData` stores
  whatever it is given and reports success, so the failure surfaced later at
  `Update()` as a modal *"Colinear directions : cannot build a plane or an
  axis"* dialog, which blocks every subsequent COM call and leaves the session
  wedged.

  The read now passes `app=` so it can fall back to the script bridge; an axis
  that cannot be read, or that reads back as degenerate, is refused instead of
  written; a supplied H direction is projected into the sketch plane with V
  re-derived from the plane normal, so the pair is orthogonal by construction;
  and a direction that is zero, or perpendicular to the plane, is rejected with
  an explanation rather than passed through. A sketch that cannot be positioned
  is removed rather than left behind in a state that would fail the next update.

- **A feature whose update failed was left in the tree**, so every later
  `Update()` re-raised the same modal dialog and one bad call wedged the whole
  session. Failed features and GSD elements are now rolled back, returning the
  model to its previous good state; the result says whether the rollback
  happened. Set `CATIA_MCP_KEEP_FAILED_FEATURES=1` to keep them for debugging.

- **`catia_gsd_axis_system` accepted colinear or zero-length axes**, which CATIA
  stores without complaint and rejects at update time. They are now validated
  up front and reported as `invalid_argument`.

### Added

- `catia_mcp/core/vectors.py`: degeneracy checks used to keep unbuildable
  direction pairs away from CATIA. `are_parallel` normalises before taking the
  cross product, so the answer does not depend on vector length - a naive
  `magnitude(cross(a, b)) < tol` test wrongly calls two small perpendicular
  vectors parallel.
- CATIA's own wording for common geometric refusals - colinear directions, open
  profiles, self-intersections, empty results, oversized values - is now
  recognised and turned into targeted remediation text.
- 15 further tests, including one that reproduces the exact crash: a
  by-reference read that never lands must never be written back.


## 1.0.0

First release.

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
