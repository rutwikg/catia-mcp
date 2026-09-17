# catia-mcp

[![License](https://img.shields.io/badge/license-AGPL--3.0-blue)](LICENSE)

A Model Context Protocol server that lets an AI assistant drive **CATIA** on a
Windows machine through its COM automation interface.

It targets **CATIA V5** (roughly R14 through V5-6R20xx) and adapts to whatever
release and licence bundle it finds rather than assuming one. 161 tools cover
document handling, sketching, Part Design, dress-up, patterns and booleans,
Generative Shape Design, assemblies, measurement, Knowledge parameters and
formulas, drafting, viewing and data exchange — plus a VBScript escape hatch to
everything else in the CATIA object model.

```
catia_connect → catia_new_part → catia_create_sketch(support="xy")
              → catia_sketch_centered_rectangle(width=80, height=50)
              → catia_pad(length=20)
              → catia_fillet(edges=["edge@40,25,10"], radius=6)
              → catia_screenshot()          # returns the picture, not a path
```

---

## Why this exists

Driving CATIA from a script means solving five problems. None of them are
exotic — every client that talks to CATIA over COM meets them — but each one
fails *quietly*, which is what makes them expensive:

**1. Measurements silently return zeros.**
CATIA declares `Measurable::GetCOG`, `GetInertia`, `GetBoundingBox` and
`Product::Position::GetComponents` as taking an `[in]` array that it then writes
into. Hand a late-bound Python list to one of those and CATIA fills a *copy* —
the call succeeds, and the caller reads back the zeros it passed in. Centre of
gravity, inertia, bounding boxes and component positions are all affected.
This server reads those arrays through a `VT_BYREF` VARIANT, and cross-checks
an all-zero answer against a VBScript trampoline evaluated inside CATIA before
believing it.

**2. Naming an edge is not the same as selecting it.**
CATIA's own handle for a face or an edge is a BRep name like
`RSur:(Face:(Brp:(Pad.1;0:(Brp:(Sketch.1;2)))...)`, which is unstable across
releases *and* across edits to the model. Without a way to resolve geometry
live, a client ends up filleting "the last feature" instead of the edge that
was asked for.

**3. COM threading is easy to get wrong.**
`pythoncom.CoInitialize()` is per-thread, while MCP handlers run on a thread
pool — so a proxy obtained on one thread is routinely used from another. And
CATIA rejects calls outright with `RPC_E_CALL_REJECTED` whenever a modal dialog
is open, which a native client handles with an `IMessageFilter` and Python
cannot.

**4. Enumeration values are not exposed.**
CATIA's automation constants cannot be read through COM, so every client
hardcodes the integers; when one is wrong the failure is often *silent* — a
capture written as CGM into a `.jpg`, a minimal-propagation fillet where you
asked for tangency.

**5. Prose results are hard to chain.**
`"Pad created: 20 mm (normal). Feature: 'Pad.1'"` is fine for a human and poor
for a model that has to decide what to do next.

Everything below is how this server addresses them.

---

## What is different

### One COM apartment, with busy-retry

Every CATIA call is marshalled onto a single dedicated STA thread that owns
`CoInitialize`. Transient rejections (`RPC_E_CALL_REJECTED`,
`RPC_E_SERVERCALL_RETRYLATER`) are retried with bounded exponential backoff —
the job a native client gives to `IMessageFilter`. Lost connections are detected
and transparently re-attached. See `catia_mcp/core/apartment.py`.

### A reference grammar for geometry

The hardest part of scripting CATIA is *pointing at things*. BRep names like
`RSur:(Face:(Brp:(Pad.1;0:(Brp:(Sketch.1;2)))...)` are unstable across releases
and across edits. Instead, every tool that needs geometry takes a token:

| Token | Means |
|---|---|
| `xy` `yz` `zx` | the part's origin planes |
| `last` | the most recent feature this server created |
| `sketch:Sketch.2` | a sketch by name |
| `body` / `body:PartBody` | the main body, or a body by name |
| `Pad.1` / `name:Pad.1` | any tree element by its CATIA name |
| `face#3` `edge#7` `vertex#2` | the n-th topological element |
| `face@12,0,40` `edge@0,0,10` | whichever face/edge is **nearest that point** |

Index tokens are cheap but any model change renumbers them. Proximity tokens
survive edits, which makes them the right choice for anything referenced more
than once — `catia_list_faces` and `catia_list_edges` report the centroids to
aim at, along with area, length and the outward normal of planar faces.

The same tokens work inside an assembly: `Selection` returns product-context
references, so `catia_assembly_constraint(elements=["face@0,0,10","face@0,0,50"])`
constrains real geometry across two components.

### Adaptive invocation instead of one hardcoded signature

`ShapeFactory` and `HybridShapeFactory` grew `...FromRef` overloads and extra
parameters across releases. `comutil.try_variants` walks a list of candidate
calls and keeps the first CATIA accepts, re-raising immediately on *busy* or
*disconnected* (which say nothing about whether the signature was right). One
code path serves old and new releases; when every candidate fails you get all
of the attempts back, not just the last error.

### Constants that are probed, not trusted

Where a wrong enumeration fails silently, the value is discovered at runtime.
`catia_screenshot` writes one throwaway capture per candidate integer and reads
the file's magic bytes to learn what that integer means *on this installation*
(`catia_capture_formats` shows the result). Everything else can be corrected
per-site through a JSON file named by `CATIA_MCP_CONSTANTS`, without patching
code.

### Structured results with remediation

Every tool returns JSON with an `ok` flag. Failures carry a stable error code, a
decoded HRESULT, and a `remediation` field:

```json
{
  "ok": false,
  "error": {
    "code": "catia_busy",
    "message": "CATIA is busy and rejected the call (Call was rejected by callee.)",
    "remediation": "CATIA rejected the call because it is busy - usually a modal
                    dialog is open, or a command is still running. Dismiss any
                    dialog in the CATIA window, then retry."
  }
}
```

Nothing escapes as a protocol-level error, so a model always gets something it
can read and act on.

### Screenshots come back as images

`catia_screenshot` returns an actual MCP image (downscaled PNG when Pillow is
installed), so the assistant can *look* at the model rather than inferring its
shape from the feature tree.

### Diagnosis before blame

`catia_check_environment` works with no CATIA connection at all: Python version
and bitness, whether pywin32 imported, whether `CATIA.Application` is registered
in the COM class table, which MCP SDK is in use, and a list of concrete
problems. `catia_capabilities` then probes what the installation can actually
do — licences, not release numbers, decide whether GSD or Sheet Metal work.

### Connecting does not launch CATIA by default

A cold CATIA start takes minutes and checks out a licence. `catia_connect`
attaches to a running session; passing `launch=true` is an explicit choice.

---

## Requirements

- Windows, with CATIA installed and **started at least once as the current user**
  (that is what registers the `CATIA.Application` COM class)
- Python 3.10+
- `pywin32`, `mcp>=1.2`, `pydantic>=2` — plus optional `pillow` for smaller screenshots

CATIA's automation server is out-of-process, so 32-bit/64-bit Python both work.
CATIA and Python must run as the **same Windows user at the same elevation** —
a CATIA started as administrator is invisible to a non-elevated client.

## Install

```bash
pip install -e ".[images]"
```

or, without the package install:

```bash
pip install -r requirements.txt
```

## Check it works — before involving CATIA

```bash
python -m catia_mcp --doctor
```

```bash
python scripts/protocol_check.py
```

`protocol_check.py` starts the server over stdio, performs the MCP handshake,
lists the tools, validates every input schema, and confirms that calling a CATIA
tool with no CATIA returns a structured error rather than a crash. It needs
neither CATIA nor a licence.

## Check it works — with CATIA running

Start CATIA, then:

```bash
python scripts/live_smoke.py
```

It builds a small part end to end — sketch, pad, fillet an edge chosen by
proximity, drill a hole, measure it, screenshot it, export STEP — and prints
PASS / FAIL / SKIP per step. Steps needing a licence you do not have are
reported as SKIP, so the output doubles as a capability report for your seat.
Add `--close` to discard the scratch part afterwards.

## Drive it by hand

The MCP Inspector gives you a browser UI over every tool — pick one, fill in
its arguments from a generated form, fire it, and read the raw JSON-RPC:

```bash
npx @modelcontextprotocol/inspector uv run python catia_mcp/server.py
```

This is the fastest way to isolate a problem, because there is no model in the
loop deciding anything: you supply the exact arguments and see the exact
result. `--cli` and `--tui` give the same thing headless and in the terminal.

## Wire it to a client

The simplest route, which needs no extra tooling:

```bash
python scripts/install_client_config.py --dry-run
```

```bash
python scripts/install_client_config.py
```

It writes the entry into the Claude desktop app's `claude_desktop_config.json`,
backing the file up first. `--target code` targets Claude Code's `~/.claude.json`
instead, `--target project` writes a `.mcp.json` beside the repository,
`--target all` does every one, and `--remove` undoes it. The interpreter it
records is **the one you ran it with** — which is the one that has `pywin32` and
`mcp` installed, and the usual cause of a server that starts and then cannot do
anything.

> `claude mcp add` will not work unless you have separately installed the Claude
> Code CLI (`npm install -g @anthropic-ai/claude-code`). It is a different
> package from the desktop app. The script above avoids needing it.

To write the configuration by hand instead, add this to
`claude_desktop_config.json` — on Windows at
`%APPDATA%\Claude\claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "catia": {
      "command": "C:\\path\\to\\python.exe",
      "args": ["-m", "catia_mcp"],
      "env": { "PYTHONPATH": "C:\\path\\to\\catia-mcp" }
    }
  }
}
```

Use the **full path** to the interpreter you installed the dependencies into,
not bare `python`; the client does not inherit your shell's PATH. Restart the
client afterwards.

An HTTP transport is available for remote or multi-client use:

```bash
python -m catia_mcp --transport http --port 8765
```

---

## The tools

161 tools in 14 groups. `python -m catia_mcp --list-tools` prints the full
inventory as JSON.

| Group | n | Covers |
|---|---|---|
| `session` | 11 | connect, disconnect, status, environment diagnosis, capability probe, reference help, VBScript execution, undo/redo, batch mode |
| `document` | 11 | new/open/save/save-as/close/activate for parts, products and drawings; document info; part number, revision and definition |
| `tree` | 15 | describe the spec tree, list bodies / features / faces / edges / vertices, resolve and verify a reference token, find by wildcard, show/hide, rename, delete, select, update |
| `sketch` | 16 | sketches on planes **or faces**, points, lines, circles, arcs, ellipses, rectangles, centred rectangles, polygons, slots, polylines, splines, constraints, geometry listing |
| `part_design` | 10 | pad, pocket, shaft, groove, rib, slot, stiffener, holes (five types, from a point or a sketch), solid combine — with up-to-next / up-to-last / up-to-plane / up-to-surface limits and thin walls |
| `dressup` | 10 | edge fillet, variable fillet, face-face fillet, tritangent fillet, chamfer, shell, thickness, draft, thread, remove-face |
| `transform` | 8 | rectangular / circular / user patterns, mirror, translate-rotate-symmetry-scale, new body, boolean add/remove/intersect/union-trim, split |
| `gsd` | 28 | geometrical sets, points, lines, circles, splines, polylines, helices, planes, extrude, revolve, sweep, multi-section, fill, offset, blend, join, split, trim, intersect, project, extract, healing, transforms, thick-surface, close-surface, axis systems |
| `assembly` | 12 | insert components, new parts and sub-assemblies, list, remove, read and set placement, nine constraint kinds, list/delete constraints, bill of materials, update |
| `measure` | 9 | measure any element, minimum distance with contact points, angle, bounding box, mass properties, point coordinates, face plane and normal, list and apply materials |
| `knowledge` | 12 | list/get/set/create/delete parameters, list relations, create formulas, activate/deactivate, design tables and configurations |
| `drafting` | 7 | sheets, generative views in seven orientations, text, scale, regenerate, delete |
| `view` | 8 | standard orientations, fit, zoom, background colour, screenshot, capture-format probe, render style, window list |
| `exchange` | 4 | export (STEP, IGES, STL, VRML, 3D XML, CGR, model, and DXF/DWG/PDF/CGM/SVG for drawings), export every open document, bill of materials to CSV, format list |

### Trimming the tool surface

161 tools is a lot of context. Restrict it with `CATIA_MCP_GROUPS`:

```bash
CATIA_MCP_GROUPS=document,tree,sketch,part_design,dressup python -m catia_mcp
```

The `session` group is always registered so connection and diagnosis stay
available. With the example above the server exposes 73 tools instead of 161.

---

## Configuration

| Variable | Effect |
|---|---|
| `CATIA_MCP_GROUPS` | comma-separated tool groups to register; default is all |
| `CATIA_MCP_CONSTANTS` | path to a JSON file overriding CATIA enumeration values |
| `CATIA_MCP_LOG` | also write logs to this file |
| `CATIA_MCP_LOG_LEVEL` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `CATIA_MCP_TRANSPORT` | `stdio` (default), `http`, `sse` |

Logs go to stderr, never stdout — stdout carries the MCP transport.

---

## Notes, limits and honest caveats

- **Sheet Metal has no dedicated tools.** `catia_capabilities` reports whether
  the `SheetMetalFactory` is available, but the wall/bend/unfold API varies
  enough between releases that shipping unverified signatures would be worse
  than pointing you at `catia_run_script`. The same applies to DMU kinematics
  and clash analysis; for clearance checking, `catia_measure_distance` between
  two components works well and needs no extra licence.

- **`Selection.Search` only finds visible geometry.** If `catia_list_faces`
  returns nothing, the body is probably hidden — `catia_show_element` fixes it.
  This is a CATIA behaviour, not a bug here.

- **Face and edge indices are not stable.** Adding a fillet renumbers every
  face. Use `face@x,y,z` for anything you reference twice.

- **`catia_start_command` and `catia_undo` are best-effort.** CATIA gives
  automation clients no way to confirm an interactive command ran, and the
  command names follow the user-interface language.

- **Pattern direction defaults.** `catia_rect_pattern` and `catia_circ_pattern`
  will use CATIA's own defaults if you do not pass direction or axis tokens.
  Pass an edge token when the direction matters.

- **This server can modify and delete work.** It is designed for a CATIA session
  you are supervising. Tools that delete or overwrite are annotated
  `destructiveHint` so a client can prompt before running them.

---

## Layout

```
catia_mcp/
  compat.py               MCP SDK shim (MCPServer 1.29+ / FastMCP earlier)
  server.py               entry point, transports, tool inventory
  core/
    apartment.py          single STA thread, busy-retry
    comutil.py            by-ref arrays, adaptive invocation, defensive reads
    connection.py         attach/launch, version detection, capability probing
    errors.py             HRESULT decoding and the error taxonomy
    refs.py               the reference-token grammar and topology search
    capture.py            runtime probing of the capture formats
    constants.py          CATIA enumerations, overridable per site
    result.py             the result envelope
  tools/                  14 tool modules, one per domain
scripts/
  install_client_config.py  register the server with an MCP client
  protocol_check.py         MCP conformance check, no CATIA needed
  live_smoke.py             end-to-end build against a real CATIA
tests/test_offline.py     36 tests, no CATIA and no Windows required
```

## Development

```bash
python -m pytest tests/ -q
ruff check catia_mcp
python scripts/protocol_check.py
```

## Licence

**GNU Affero General Public License v3.0 or later** (AGPL-3.0-or-later). See
[LICENSE](LICENSE).

The AGPL's network clause is the point: if you run a modified version of this
server so that others interact with it over a network, you have to offer them
its source. Using it privately, or driving your own CATIA with it, carries no
such obligation.

CATIA is a registered trademark of Dassault Systèmes. This project automates
CATIA; it neither includes nor replaces it, and is not affiliated with or
endorsed by them. You need your own CATIA installation and licence.
