"""Camera control and screen capture.

The screenshot tool returns the picture to the model as an MCP image, not just
a file path, so a model can actually look at what it built. The capture format
enumeration is probed rather than assumed - see ``core.capture`` for why that
matters.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from typing import Annotated, Any

from pydantic import Field

from catia_mcp import compat
from catia_mcp.core import capture, comutil, constants, errors, result
from catia_mcp.core.refs import resolve
from catia_mcp.tools.base import registrar

logger = logging.getLogger("catia_mcp.tools.view")

try:  # Pillow is optional; without it we hand back CATIA's own format.
    from PIL import Image as PILImage

    HAS_PILLOW = True
except Exception:  # pragma: no cover
    PILImage = None  # type: ignore[assignment]
    HAS_PILLOW = False


def _viewer(session: Any) -> Any:
    app = session.require()
    window = comutil.safe(app, "ActiveWindow")
    if window is None:
        raise errors.NoActiveDocumentError("CATIA has no active window.")
    viewer = comutil.safe(window, "ActiveViewer")
    if viewer is None:
        raise errors.NoActiveDocumentError(
            "The active window has no 3D viewer. Drawing documents use a 2D view instead."
        )
    return viewer


def register(mcp: Any, session: Any) -> None:
    tool = registrar(mcp, session)

    @tool(
        "catia_set_view",
        "Point the camera at a standard orientation - front, back, top, bottom, left, "
        "right, isometric, iso_rear or dimetric - and fit the model in the window.",
        idempotent=True,
        group="view",
    )
    def catia_set_view(
        orientation: Annotated[
            str,
            Field(
                description=(
                    "front | back | top | bottom | left | right | isometric | iso_rear | "
                    "dimetric."
                )
            ),
        ] = "isometric",
        fit: Annotated[bool, Field(description="Zoom to fit afterwards.")] = True,
    ) -> dict:
        key = orientation.strip().lower()
        if key not in constants.VIEW_ORIENTATIONS:
            raise errors.InvalidArgumentError(
                "Unknown orientation %r. Valid: %s"
                % (orientation, ", ".join(sorted(constants.VIEW_ORIENTATIONS)))
            )
        viewer = _viewer(session)
        viewpoint = comutil.safe(viewer, "Viewpoint3D")
        if viewpoint is None:
            raise errors.UnsupportedCapabilityError(
                "The active viewer exposes no 3D viewpoint."
            )
        spec = constants.VIEW_ORIENTATIONS[key]
        warnings: list[str] = []
        for method, vector in (
            ("PutSightDirection", spec["sight"]),
            ("PutUpDirection", spec["up"]),
        ):
            try:
                getattr(viewpoint, method)(comutil.in_doubles(vector))
            except Exception:
                try:
                    getattr(viewpoint, method)(list(vector))
                except Exception as exc:
                    warnings.append("%s failed (%s)." % (method, errors.com_message(exc)))
        if fit:
            comutil.safe_call(viewer, "Reframe")
        comutil.safe_call(viewer, "Update")
        return result.ok(
            {"orientation": key, "sight": list(spec["sight"]), "up": list(spec["up"])},
            message="View set to %s." % key,
            warnings=warnings,
        )

    @tool(
        "catia_fit_all",
        "Zoom and centre the view so everything visible fits in the window.",
        idempotent=True,
        group="view",
    )
    def catia_fit_all() -> dict:
        viewer = _viewer(session)
        viewer.Reframe()
        comutil.safe_call(viewer, "Update")
        return result.ok({}, message="Fitted the view to the model.")

    @tool(
        "catia_zoom",
        "Zoom the 3D view in or out by a number of steps, or reframe on one element.",
        group="view",
    )
    def catia_zoom(
        steps: Annotated[
            int,
            Field(description="Positive zooms in, negative zooms out.", ge=-20, le=20),
        ] = 1,
        on_element: Annotated[
            str,
            Field(
                description=(
                    "Optional reference token to centre on; the element is selected and "
                    "the view reframed around it."
                )
            ),
        ] = "",
    ) -> dict:
        viewer = _viewer(session)
        focused = None
        if on_element:
            resolved = resolve(session, on_element)
            selection = session.selection()
            selection.Clear()
            selection.Add(resolved.obj)
            focused = resolved.label
            comutil.safe_call(viewer, "Reframe")
        for _ in range(abs(int(steps))):
            comutil.safe_call(viewer, "ZoomIn" if steps > 0 else "ZoomOut")
        comutil.safe_call(viewer, "Update")
        return result.ok(
            {"steps": steps, "focused_on": focused},
            message="Zoomed %s." % ("in" if steps > 0 else "out"),
        )

    @tool(
        "catia_set_background_color",
        "Set the 3D viewer's background colour. A white background gives much more "
        "legible screenshots than CATIA's default gradient.",
        idempotent=True,
        group="view",
    )
    def catia_set_background_color(
        red: Annotated[float, Field(description="Red, 0 to 1.", ge=0, le=1)] = 1.0,
        green: Annotated[float, Field(description="Green, 0 to 1.", ge=0, le=1)] = 1.0,
        blue: Annotated[float, Field(description="Blue, 0 to 1.", ge=0, le=1)] = 1.0,
    ) -> dict:
        viewer = _viewer(session)
        colour = [float(red), float(green), float(blue)]
        _, _ = comutil.try_variants(
            [
                ("PutBackgroundColor(variant)",
                 lambda: viewer.PutBackgroundColor(comutil.in_doubles(colour))),
                ("PutBackgroundColor(list)", lambda: viewer.PutBackgroundColor(colour)),
            ],
            what="set the background colour",
        )
        comutil.safe_call(viewer, "Update")
        return result.ok({"rgb": colour}, message="Background colour set.")

    @tool(
        "catia_screenshot",
        "Capture the CATIA 3D viewport and return it as an image you can look at, "
        "optionally saving it to a file as well. Use it to check that geometry came out "
        "the way you intended - it is far more reliable than reasoning about the feature "
        "tree alone.",
        readonly=True,
        group="view",
    )
    def catia_screenshot(
        path: Annotated[
            str,
            Field(
                description=(
                    "Where to save the image. Leave empty to capture to a temporary file "
                    "and only return the picture."
                )
            ),
        ] = "",
        orientation: Annotated[
            str,
            Field(
                description=(
                    "Optionally set a standard view first: front, top, isometric, and so "
                    "on. Empty keeps the current camera."
                )
            ),
        ] = "",
        fit: Annotated[bool, Field(description="Fit the model in the window first.")] = True,
        return_image: Annotated[
            bool, Field(description="Return the picture itself, not just the file path.")
        ] = True,
        max_pixels: Annotated[
            int,
            Field(
                description=(
                    "Longest edge of the returned image in pixels; larger captures are "
                    "downscaled to keep the response small. Needs Pillow."
                ),
                ge=128,
                le=4096,
            ),
        ] = 1024,
    ) -> Any:
        if orientation:
            catia_set_view(orientation=orientation, fit=fit)
        elif fit:
            comutil.safe_call(_viewer(session), "Reframe")

        viewer = _viewer(session)
        comutil.safe_call(viewer, "Update")

        wanted = None
        if path:
            wanted = os.path.splitext(path)[1].lstrip(".").lower() or None
        format_name, format_code = capture.CAPTURE_FORMATS.get(viewer, wanted)

        if path:
            destination = os.path.abspath(os.path.expanduser(path))
            parent = os.path.dirname(destination)
            if parent:
                os.makedirs(parent, exist_ok=True)
        else:
            handle, destination = tempfile.mkstemp(
                prefix="catia_capture_", suffix="." + format_name
            )
            os.close(handle)

        try:
            viewer.CaptureToFile(format_code, destination)
        except Exception as exc:
            raise errors.OperationFailedError(
                "CATIA could not capture the viewport: %s" % errors.com_message(exc)
            ) from exc

        if not os.path.exists(destination):
            raise errors.OperationFailedError(
                "CATIA reported success but wrote no file to %s." % destination
            )

        payload: dict[str, Any] = {
            "ok": True,
            "path": destination,
            "format": format_name,
            "size_bytes": os.path.getsize(destination),
            "temporary": not bool(path),
        }

        if not return_image:
            return payload

        data, image_format, dimensions = _prepare_image(destination, max_pixels)
        if dimensions:
            payload["pixels"] = {"width": dimensions[0], "height": dimensions[1]}
        payload["returned_format"] = image_format
        if not HAS_PILLOW:
            payload["note"] = (
                "Pillow is not installed, so the image is returned in CATIA's own format "
                "and was not downscaled. Install it with: pip install pillow"
            )
        try:
            image = compat.make_image(data, image_format)
        except Exception as exc:  # pragma: no cover - SDK without image support
            payload["image_error"] = str(exc)
            return payload
        return [image, json.dumps(payload)]

    @tool(
        "catia_capture_formats",
        "Report which image formats this CATIA installation can actually write, as "
        "determined by probing rather than by assuming the documented enumeration.",
        readonly=True,
        group="view",
    )
    def catia_capture_formats(
        reprobe: Annotated[bool, Field(description="Discard the cached result and probe again.")]
        = False,
    ) -> dict:
        if reprobe:
            capture.CAPTURE_FORMATS.reset()
        known = capture.CAPTURE_FORMATS.known()
        if known is None:
            viewer = _viewer(session)
            capture.CAPTURE_FORMATS.get(viewer)
            known = capture.CAPTURE_FORMATS.known() or {}
        return result.ok(
            {"formats": known, "preferred_order": list(capture.PREFERRED)},
            hint="These are the values catia_screenshot will use for each format name.",
        )

    @tool(
        "catia_set_render_style",
        "Change how the model is drawn: shaded, shaded with edges, wireframe, or a "
        "few CATIA variants. Falls back to the interactive command when the release does "
        "not expose the property.",
        group="view",
    )
    def catia_set_render_style(
        style: Annotated[
            str,
            Field(
                description=(
                    "shaded | shaded_with_edges | wireframe | hidden_line_removal | "
                    "shaded_with_material."
                )
            ),
        ] = "shaded_with_edges",
    ) -> dict:
        codes = {
            "wireframe": (0, "Wireframe"),
            "shaded": (1, "Shading"),
            "shaded_with_edges": (2, "Shading with Edges"),
            "hidden_line_removal": (3, "Hidden Line Removal"),
            "shaded_with_material": (4, "Shading with Material"),
        }
        key = style.strip().lower()
        if key not in codes:
            raise errors.InvalidArgumentError(
                "style must be one of %s." % ", ".join(sorted(codes))
            )
        code, command = codes[key]
        viewer = _viewer(session)
        try:
            viewer.RenderingMode = code
            comutil.safe_call(viewer, "Update")
            return result.ok({"style": key, "method": "RenderingMode"},
                             message="Render style set to %s." % key)
        except Exception:
            pass
        app = session.require()
        if comutil.has_member(app, "StartCommand"):
            app.StartCommand(command)
            return result.ok(
                {"style": key, "method": "StartCommand"},
                message="Sent the '%s' command to CATIA." % command,
                hint=(
                    "StartCommand depends on the CATIA interface language, so this may "
                    "silently do nothing on a non-English installation."
                ),
            )
        raise errors.UnsupportedCapabilityError(
            "This CATIA release exposes neither Viewer.RenderingMode nor StartCommand."
        )

    @tool(
        "catia_list_windows",
        "List CATIA's open windows and which document each shows.",
        readonly=True,
        group="view",
    )
    def catia_list_windows() -> dict:
        app = session.require()
        windows = []
        for window in comutil.com_iter(comutil.safe(app, "Windows")):
            windows.append(
                {
                    "caption": str(comutil.safe(window, "Caption", "") or ""),
                    "name": comutil.name_of(window),
                    "viewers": comutil.com_count(comutil.safe(window, "Viewers")),
                }
            )
        active = comutil.safe(app, "ActiveWindow")
        return result.ok(
            {
                "count": len(windows),
                "active": str(comutil.safe(active, "Caption", "") or ""),
                "windows": windows,
            }
        )


def _prepare_image(path: str, max_pixels: int) -> tuple[bytes, str, tuple[int, int] | None]:
    """Load the capture, optionally downscale it, and return PNG bytes."""
    with open(path, "rb") as handle:
        raw = handle.read()

    if not HAS_PILLOW:
        extension = os.path.splitext(path)[1].lstrip(".").lower() or "png"
        return raw, "jpeg" if extension in ("jpg", "jpeg") else extension, None

    import io

    with PILImage.open(io.BytesIO(raw)) as image:
        image = image.convert("RGB")
        width, height = image.size
        longest = max(width, height)
        if longest > max_pixels:
            scale = max_pixels / float(longest)
            image = image.resize(
                (max(1, int(width * scale)), max(1, int(height * scale))),
                PILImage.LANCZOS,
            )
        buffer = io.BytesIO()
        image.save(buffer, format="PNG", optimize=True)
        return buffer.getvalue(), "png", image.size
