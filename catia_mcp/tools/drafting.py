"""Drafting: 2D drawing sheets and generative views of a 3D document.

Generative views are driven through ``DrawingView.GenerativeBehavior``, whose
``DefineFrontView`` takes the two in-plane direction vectors of the view. Every
standard orientation - front, top, right, isometric - is just a different pair
of vectors, which is why one tool covers them all.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from pydantic import Field

from catia_mcp.core import comutil, errors, result
from catia_mcp.tools.base import registrar, rename

logger = logging.getLogger("catia_mcp.tools.drafting")

PAPER_SIZES = {
    "A0": 0, "A1": 1, "A2": 2, "A3": 3, "A4": 4,
    "A": 5, "B": 6, "C": 7, "D": 8, "E": 9, "F": 10,
}

# (x direction of the view, y direction of the view) in 3D part coordinates.
VIEW_DIRECTIONS: dict[str, tuple[tuple[float, float, float], tuple[float, float, float]]] = {
    "front": ((1.0, 0.0, 0.0), (0.0, 0.0, 1.0)),
    "back": ((-1.0, 0.0, 0.0), (0.0, 0.0, 1.0)),
    "top": ((1.0, 0.0, 0.0), (0.0, -1.0, 0.0)),
    "bottom": ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0)),
    "left": ((0.0, -1.0, 0.0), (0.0, 0.0, 1.0)),
    "right": ((0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
    "isometric": ((0.707, 0.707, 0.0), (-0.408, 0.408, 0.816)),
}


def _sheet(session: Any, name: str = "") -> Any:
    doc = session.active_drawing()
    sheets = doc.Sheets
    if name:
        found = comutil.safe_call(sheets, "Item", name)
        if found is None:
            raise errors.ElementNotFoundError(
                "No sheet called %r. Available: %s"
                % (name, ", ".join(comutil.name_of(s) for s in comutil.com_iter(sheets)))
            )
        return found
    active = comutil.safe(sheets, "ActiveSheet")
    if active is not None:
        return active
    if comutil.com_count(sheets) == 0:
        raise errors.ElementNotFoundError("The drawing has no sheets.")
    return sheets.Item(1)


def _source_3d(session: Any, document_name: str) -> Any:
    """Find the 3D document a generative view should be built from."""
    documents = session.documents
    candidates = []
    for doc in comutil.com_iter(documents):
        kind = session.document_kind(doc)
        if kind not in ("part", "product"):
            continue
        candidates.append(doc)
        if document_name and comutil.name_of(doc).lower() == document_name.lower():
            return comutil.safe(doc, "Product") or comutil.safe(doc, "Part")
    if document_name:
        raise errors.ElementNotFoundError(
            "No open 3D document called %r." % document_name
        )
    if not candidates:
        raise errors.ElementNotFoundError(
            "No Part or Product document is open, so there is nothing to draw views of.",
            remediation="Open the 3D model first with catia_open_document.",
        )
    chosen = candidates[0]
    return comutil.safe(chosen, "Product") or comutil.safe(chosen, "Part")


def register(mcp: Any, session: Any) -> None:
    tool = registrar(mcp, session)

    @tool(
        "catia_drawing_list_sheets",
        "List the sheets of the active drawing, with their paper size, scale and views.",
        readonly=True,
        group="drafting",
    )
    def catia_drawing_list_sheets() -> dict:
        doc = session.active_drawing()
        sheets = []
        for sheet in comutil.com_iter(doc.Sheets):
            views = [
                {
                    "name": comutil.name_of(view),
                    "x": comutil.safe(view, "x"),
                    "y": comutil.safe(view, "y"),
                    "scale": comutil.safe(view, "Scale"),
                }
                for view in comutil.com_iter(comutil.safe(sheet, "Views"))
            ]
            sheets.append(
                {
                    "name": comutil.name_of(sheet),
                    "paper_size_code": comutil.safe(sheet, "PaperSize"),
                    "scale": comutil.safe(sheet, "Scale"),
                    "view_count": len(views),
                    "views": views,
                }
            )
        return result.ok({"count": len(sheets), "sheets": sheets})

    @tool(
        "catia_drawing_add_sheet",
        "Add a sheet to the active drawing and make it current.",
        group="drafting",
    )
    def catia_drawing_add_sheet(
        name: Annotated[str, Field(description="Name for the sheet.")] = "",
        paper_size: Annotated[str, Field(description="A0-A4 or A-F.")] = "A3",
        scale: Annotated[float, Field(description="Sheet scale, e.g. 1.0 or 0.5.", gt=0)] = 1.0,
        landscape: Annotated[bool, Field(description="Landscape orientation.")] = True,
    ) -> dict:
        doc = session.active_drawing()
        sheet = doc.Sheets.Add(name or "Sheet")
        warnings: list[str] = []
        code = PAPER_SIZES.get(paper_size.upper())
        if code is not None:
            try:
                sheet.PaperSize = code
            except Exception as exc:
                warnings.append("Could not set the paper size (%s)." % exc)
        try:
            sheet.Scale = float(scale)
        except Exception:
            warnings.append("Could not set the sheet scale.")
        try:
            sheet.Orientation = 0 if landscape else 1
        except Exception:
            warnings.append("Could not set the orientation.")
        final = rename(sheet, name)
        comutil.safe_call(sheet, "Activate")
        return result.ok(
            {"created": final, "paper_size": paper_size, "scale": scale},
            message="Added sheet %r." % final,
            warnings=warnings,
        )

    @tool(
        "catia_drawing_add_view",
        "Add a generative view of an open 3D document to a drawing sheet. Choose a "
        "standard orientation, where the view goes on the sheet, and its scale. Call "
        "catia_drawing_update afterwards to make CATIA compute the geometry.",
        group="drafting",
    )
    def catia_drawing_add_view(
        orientation: Annotated[
            str,
            Field(description="front | back | top | bottom | left | right | isometric."),
        ] = "front",
        x: Annotated[float, Field(description="View centre X on the sheet, mm.")] = 150.0,
        y: Annotated[float, Field(description="View centre Y on the sheet, mm.")] = 150.0,
        scale: Annotated[float, Field(description="View scale.", gt=0)] = 1.0,
        source_document: Annotated[
            str,
            Field(
                description=(
                    "Name of the open 3D document to draw. Empty uses the first Part or "
                    "Product that is open."
                )
            ),
        ] = "",
        sheet: Annotated[str, Field(description="Sheet name. Empty uses the active sheet.")] = "",
        name: Annotated[str, Field(description="Name for the view.")] = "",
    ) -> dict:
        key = orientation.strip().lower()
        if key not in VIEW_DIRECTIONS:
            raise errors.InvalidArgumentError(
                "orientation must be one of %s." % ", ".join(sorted(VIEW_DIRECTIONS))
            )
        target_sheet = _sheet(session, sheet)
        source = _source_3d(session, source_document)
        views = target_sheet.Views
        view = views.Add(name or "%s view" % key.title())

        behaviour = comutil.safe(view, "GenerativeBehavior")
        if behaviour is None:
            raise errors.UnsupportedCapabilityError(
                "This drawing view exposes no GenerativeBehavior, so a generative view "
                "cannot be built. The Generative Drafting licence may be missing."
            )
        try:
            behaviour.Document = source
        except Exception as exc:
            raise errors.OperationFailedError(
                "Could not bind the 3D document to the view: %s" % errors.com_message(exc)
            ) from exc

        x_dir, y_dir = VIEW_DIRECTIONS[key]
        _, _ = comutil.try_variants(
            [
                (
                    "DefineFrontView",
                    lambda: behaviour.DefineFrontView(
                        x_dir[0], x_dir[1], x_dir[2], y_dir[0], y_dir[1], y_dir[2]
                    ),
                ),
            ],
            what="orient the generative view",
        )

        warnings: list[str] = []
        for member, value in (("x", x), ("y", y), ("Scale", scale)):
            try:
                setattr(view, member, float(value))
            except Exception:
                warnings.append("Could not set %s on the view." % member)
        try:
            behaviour.Update()
        except Exception as exc:
            warnings.append(
                "The view was created but did not compute: %s" % errors.com_message(exc)
            )

        return result.ok(
            {
                "created": comutil.name_of(view),
                "orientation": key,
                "sheet": comutil.name_of(target_sheet),
                "position": {"x": x, "y": y},
                "scale": scale,
                "source": comutil.name_of(source),
            },
            message="Added a %s view." % key,
            warnings=warnings,
            hint="Call catia_drawing_update to regenerate every view on the sheet.",
        )

    @tool(
        "catia_drawing_add_text",
        "Place a text annotation on a drawing sheet - a note, a title or a revision mark.",
        group="drafting",
    )
    def catia_drawing_add_text(
        text: Annotated[str, Field(description="The text to place.")],
        x: Annotated[float, Field(description="X position on the sheet, mm.")] = 20.0,
        y: Annotated[float, Field(description="Y position on the sheet, mm.")] = 20.0,
        height: Annotated[float, Field(description="Character height, mm.", gt=0)] = 5.0,
        sheet: Annotated[str, Field(description="Sheet name.")] = "",
        view: Annotated[
            str, Field(description="View to place the text in. Empty uses the sheet's main view.")
        ] = "",
    ) -> dict:
        target_sheet = _sheet(session, sheet)
        views = target_sheet.Views
        if view:
            container = comutil.safe_call(views, "Item", view)
            if container is None:
                raise errors.ElementNotFoundError("No view called %r." % view)
        else:
            container = comutil.safe(views, "ActiveView") or (
                views.Item(1) if comutil.com_count(views) else None
            )
        if container is None:
            raise errors.ElementNotFoundError(
                "The sheet has no view to place text in. Add one first."
            )

        texts = comutil.safe(container, "Texts")
        if texts is None:
            raise errors.UnsupportedCapabilityError("This view exposes no Texts collection.")
        annotation = texts.Add(text, float(x), float(y))
        warnings: list[str] = []
        try:
            annotation.SetFontSize(0, 0, float(height))
        except Exception:
            warnings.append("Could not set the character height.")
        return result.ok(
            {
                "created": comutil.name_of(annotation),
                "text": text,
                "position": {"x": x, "y": y},
                "view": comutil.name_of(container),
            },
            message="Added a text annotation.",
            warnings=warnings,
        )

    @tool(
        "catia_drawing_set_scale",
        "Change the scale of a sheet or of one view.",
        group="drafting",
    )
    def catia_drawing_set_scale(
        scale: Annotated[float, Field(description="New scale, e.g. 0.5 for half size.", gt=0)],
        sheet: Annotated[str, Field(description="Sheet name. Empty uses the active sheet.")] = "",
        view: Annotated[
            str, Field(description="View name. Empty changes the whole sheet's scale.")
        ] = "",
    ) -> dict:
        target_sheet = _sheet(session, sheet)
        if view:
            target = comutil.safe_call(target_sheet.Views, "Item", view)
            if target is None:
                raise errors.ElementNotFoundError("No view called %r." % view)
            label = "view %s" % comutil.name_of(target)
        else:
            target = target_sheet
            label = "sheet %s" % comutil.name_of(target_sheet)
        try:
            target.Scale = float(scale)
        except Exception as exc:
            raise errors.OperationFailedError(
                "Could not set the scale: %s" % errors.com_message(exc)
            ) from exc
        return result.ok(
            {"target": label, "scale": scale}, message="Set the scale of %s to %s." % (label, scale)
        )

    @tool(
        "catia_drawing_update",
        "Regenerate every generative view on a sheet from the current state of the 3D "
        "model. Run this after changing the part.",
        idempotent=True,
        group="drafting",
    )
    def catia_drawing_update(
        sheet: Annotated[str, Field(description="Sheet name. Empty updates the active sheet.")] = "",
        all_sheets: Annotated[bool, Field(description="Update every sheet in the drawing.")] = False,
    ) -> dict:
        doc = session.active_drawing()
        targets = (
            list(comutil.com_iter(doc.Sheets)) if all_sheets else [_sheet(session, sheet)]
        )
        updated: list[str] = []
        warnings: list[str] = []
        for target in targets:
            for view in comutil.com_iter(comutil.safe(target, "Views")):
                behaviour = comutil.safe(view, "GenerativeBehavior")
                if behaviour is None:
                    continue
                try:
                    behaviour.Update()
                    updated.append(comutil.name_of(view))
                except Exception as exc:
                    warnings.append(
                        "%s did not update: %s"
                        % (comutil.name_of(view), errors.com_message(exc))
                    )
        session.refresh_view()
        return result.ok(
            {"sheets": [comutil.name_of(t) for t in targets], "views_updated": updated},
            message="Updated %d view(s)." % len(updated),
            warnings=warnings,
        )

    @tool(
        "catia_drawing_delete_view",
        "Delete a view from a drawing sheet.",
        destructive=True,
        group="drafting",
    )
    def catia_drawing_delete_view(
        view: Annotated[str, Field(description="View name.")],
        sheet: Annotated[str, Field(description="Sheet name.")] = "",
    ) -> dict:
        target_sheet = _sheet(session, sheet)
        target = comutil.safe_call(target_sheet.Views, "Item", view)
        if target is None:
            raise errors.ElementNotFoundError("No view called %r." % view)
        selection = session.selection()
        selection.Clear()
        selection.Add(target)
        selection.Delete()
        selection.Clear()
        return result.ok({"deleted": view}, message="Deleted view %s." % view)
