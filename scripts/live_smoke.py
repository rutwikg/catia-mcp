"""Live smoke test against a running CATIA.

Unlike ``tests/test_offline.py`` this one needs a real CATIA session. It drives
the server's tool functions directly - no MCP transport in the way - so a
failure points straight at the CATIA call that broke.

    python scripts/live_smoke.py                 # build in a scratch part, keep it open
    python scripts/live_smoke.py --close         # close the scratch part afterwards
    python scripts/live_smoke.py --launch        # let the script start CATIA

Each step reports PASS, FAIL or SKIP with the reason. Steps that depend on a
licence you may not have (Generative Shape Design, drafting) are reported as
SKIP rather than FAIL, so the output tells you what your installation supports.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from catia_mcp.server import build_server  # noqa: E402

TOOLS: dict = {}
RESULTS: list[tuple[str, str, str]] = []


class Collector:
    """Stands in for the MCP server, capturing the registered callables."""

    def tool(self, **kwargs):
        name = kwargs["name"]

        def decorate(fn):
            TOOLS[name] = fn
            return fn

        return decorate


def call(name: str, **arguments):
    fn = TOOLS.get(name)
    if fn is None:
        raise AssertionError("tool %s is not registered" % name)
    return fn(**arguments)


def step(label: str, name: str, expect_keys: tuple[str, ...] = (), **arguments):
    """Run one tool and record the outcome."""
    try:
        payload = call(name, **arguments)
    except Exception as exc:  # noqa: BLE001 - reported, not raised
        RESULTS.append((label, "FAIL", "%s raised %s" % (name, exc)))
        return None

    if isinstance(payload, list):  # screenshot returns [image, json]
        text = next((p for p in payload if isinstance(p, str)), "{}")
        payload = json.loads(text)

    if not isinstance(payload, dict):
        RESULTS.append((label, "FAIL", "unexpected return type %s" % type(payload).__name__))
        return None

    if payload.get("ok") is not True:
        error = payload.get("error", {})
        status = "SKIP" if error.get("code") == "unsupported_capability" else "FAIL"
        RESULTS.append((label, status, "%s: %s" % (error.get("code"), error.get("message"))))
        return None

    missing = [key for key in expect_keys if key not in payload]
    if missing:
        RESULTS.append((label, "FAIL", "result is missing %s" % ", ".join(missing)))
        return payload

    detail = payload.get("message") or ""
    for warning in payload.get("warnings", []) or []:
        detail += " [warning: %s]" % warning
    RESULTS.append((label, "PASS", detail.strip()))
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Live CATIA smoke test.")
    parser.add_argument("--launch", action="store_true", help="Start CATIA if it is not running.")
    parser.add_argument("--close", action="store_true", help="Close the scratch part at the end.")
    parser.add_argument(
        "--keep-files", action="store_true", help="Keep the exported STEP and screenshot."
    )
    args = parser.parse_args()

    build_server(Collector())
    print("Registered %d tools.\n" % len(TOOLS))

    environment = call("catia_check_environment")
    print("Environment: python %s (%s-bit), pywin32=%s, CATIA registered=%s"
          % (environment.get("python"), environment.get("python_bits"),
             environment.get("pywin32_available"),
             environment.get("catia_progid_registered")))
    for problem in environment.get("problems", []):
        print("  ! %s" % problem)
    print()

    connected = step("connect", "catia_connect", ("catia",), launch=args.launch)
    if connected is None:
        _report()
        print("\nCannot continue without a CATIA connection.")
        return 1
    print("CATIA: %s\n" % connected["catia"].get("marketing_name"))

    capabilities = step("capabilities", "catia_capabilities", ("capabilities",))

    step("new part", "catia_new_part", ("document",), name="SmokeTestPart")
    step("sketch on XY", "catia_create_sketch", ("sketch",), support="xy", name="BaseSketch")
    step("rectangle", "catia_sketch_centered_rectangle", (),
         center_x=0, center_y=0, width=80, height=50)
    step("pad", "catia_pad", ("created",), length=20, name="BasePad")

    faces = step("list faces", "catia_list_faces", ("faces",))
    if faces:
        print("   faces found: %d" % faces["count"])
        planar = [f for f in faces["faces"] if f.get("planar")]
        print("   planar faces: %d" % len(planar))

    edges = step("list edges", "catia_list_edges", ("edges",))
    if edges and edges["count"]:
        print("   edges found: %d" % edges["count"])

    # Fillet the vertical edge nearest a top corner, using a proximity token.
    step("fillet by proximity", "catia_fillet", ("created",),
         edges=["edge@40,25,10"], radius=6, name="CornerFillet")

    step("bounding box", "catia_bounding_box", ("dimensions_mm",), element="body")
    bbox = call("catia_bounding_box", element="body")
    if bbox.get("ok"):
        dims = bbox["dimensions_mm"]
        print("   bounding box: %.1f x %.1f x %.1f mm" % (dims["x"], dims["y"], dims["z"]))
        if abs(dims["x"] - 80) > 0.5 or abs(dims["y"] - 50) > 0.5 or abs(dims["z"] - 20) > 0.5:
            RESULTS.append(
                ("bounding box values", "FAIL",
                 "expected about 80 x 50 x 20 mm, got %.1f x %.1f x %.1f"
                 % (dims["x"], dims["y"], dims["z"])))
        else:
            RESULTS.append(("bounding box values", "PASS", "matches the modelled size"))

    mass = step("mass properties", "catia_mass_properties", (), density=7850)
    if mass:
        print("   volume: %s mm3, mass: %s kg"
              % (mass.get("volume_mm3"), mass.get("mass_kg")))
        # A quick independent check that GetCOG really came back filled in.
        cog = mass.get("center_of_gravity_mm") or mass.get("centroid_mm")
        if cog and all(abs(v) < 1e-9 for v in cog.values()):
            RESULTS.append(
                ("centre of gravity", "FAIL",
                 "came back as exactly (0,0,0), which is what a failed by-reference "
                 "array read looks like"))
        elif cog:
            RESULTS.append(("centre of gravity", "PASS", "%s" % cog))

    step("hole", "catia_hole", ("created",),
         face="face@0,0,20", x=0, y=0, z=20, diameter=10, depth=10, name="CentreHole")

    step("parameters", "catia_list_parameters", ("parameters",), filter="Length")
    step("create parameter", "catia_create_parameter", ("name",),
         name="PlateThickness", parameter_type="length", value=20)

    step("view", "catia_set_view", (), orientation="isometric")

    shot_dir = tempfile.mkdtemp(prefix="catia_smoke_")
    shot = step("screenshot", "catia_screenshot", ("path",),
                path=os.path.join(shot_dir, "smoke.png"), return_image=False)
    if shot:
        print("   screenshot: %s (%d bytes)" % (shot["path"], shot["size_bytes"]))

    step("capture formats", "catia_capture_formats", ("formats",))

    step("describe tree", "catia_describe_tree", ("tree",))
    step("list features", "catia_list_features", ("features",))

    if capabilities and capabilities["capabilities"].get("gsd"):
        step("geometrical set", "catia_gsd_create_geoset", ("created",), name="SmokeGeoSet")
        step("gsd point", "catia_gsd_point", ("created",),
             mode="coordinates", x=0, y=0, z=60, name="TopPoint")
        step("gsd plane", "catia_gsd_plane", ("created",),
             mode="offset", reference="xy", offset=40, name="OffsetPlane")
    else:
        RESULTS.append(("gsd", "SKIP", "Generative Shape Design is not available here"))

    step("export STEP", "catia_export", ("path",),
         path=os.path.join(shot_dir, "smoke.stp"), format="step")

    if args.close:
        step("close part", "catia_close_document", (), save=False)

    if not args.keep_files:
        print("\nScratch files are in %s" % shot_dir)

    return _report()


def _report() -> int:
    print("\n" + "=" * 72)
    width = max((len(label) for label, _, _ in RESULTS), default=10)
    counts = {"PASS": 0, "FAIL": 0, "SKIP": 0}
    for label, status, detail in RESULTS:
        counts[status] = counts.get(status, 0) + 1
        print("%-6s %-*s  %s" % (status, width, label, detail))
    print("=" * 72)
    print("%d passed, %d failed, %d skipped" % (counts["PASS"], counts["FAIL"], counts["SKIP"]))
    return 1 if counts["FAIL"] else 0


if __name__ == "__main__":
    sys.exit(main())
