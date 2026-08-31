"""Document lifecycle: create, open, save, close, activate, describe."""

from __future__ import annotations

import logging
import os
from typing import Annotated, Any

from pydantic import Field

from catia_mcp.core import comutil, constants, errors, result
from catia_mcp.tools.base import registrar

logger = logging.getLogger("catia_mcp.tools.document")

_PAPER_SIZES = {
    "A0": 0,
    "A1": 1,
    "A2": 2,
    "A3": 3,
    "A4": 4,
    "A": 5,
    "B": 6,
    "C": 7,
    "D": 8,
    "E": 9,
    "F": 10,
}

_DRAWING_STANDARDS = {"ISO": 0, "ANSI": 1, "JIS": 2, "ASME": 3}


def _doc_payload(session: Any, doc: Any) -> dict[str, Any]:
    return {
        "name": comutil.name_of(doc),
        "kind": session.document_kind(doc),
        "path": str(comutil.safe(doc, "FullName", "") or ""),
        "saved": bool(comutil.safe(doc, "Saved", False)),
        "read_only": bool(comutil.safe(doc, "ReadOnly", False)),
    }


def _find_document(session: Any, name: str | None) -> Any:
    documents = session.documents
    if not name:
        return session.active_document()
    target = name.strip().lower()
    for doc in comutil.com_iter(documents):
        doc_name = comutil.name_of(doc).lower()
        full = str(comutil.safe(doc, "FullName", "") or "").lower()
        if target in (doc_name, full) or os.path.basename(full) == target:
            return doc
    raise errors.ElementNotFoundError(
        "No open document called %r. Open documents: %s"
        % (name, ", ".join(comutil.name_of(d) for d in comutil.com_iter(documents)) or "none")
    )


def register(mcp: Any, session: Any) -> None:
    tool = registrar(mcp, session)

    @tool(
        "catia_list_documents",
        "List every document currently open in CATIA, with its type, file path and "
        "whether it has unsaved changes.",
        readonly=True,
        group="document",
    )
    def catia_list_documents() -> dict:
        documents = [_doc_payload(session, d) for d in comutil.com_iter(session.documents)]
        active = None
        try:
            active = comutil.name_of(session.active_document())
        except errors.CatiaError:
            pass
        return result.ok({"count": len(documents), "active": active, "documents": documents})

    @tool(
        "catia_new_part",
        "Create a new empty CATPart document and make it active. This is the starting "
        "point for any solid or surface modelling.",
        group="document",
    )
    def catia_new_part(
        name: Annotated[
            str, Field(description="Name for the part inside the tree (not the file name).")
        ] = "",
    ) -> dict:
        doc = session.documents.Add(constants.DOC_TYPE_PART)
        part = comutil.safe(doc, "Part")
        if name and part is not None:
            try:
                part.Name = name
            except Exception as exc:
                logger.info("Could not rename part: %s", exc)
        session.state.last_sketch_name = ""
        session.state.last_feature_name = ""
        session.state.last_geoset_name = ""
        return result.ok(
            {"document": _doc_payload(session, doc), "part_name": comutil.name_of(part)},
            message="Created a new part.",
            hint="Sketch on an origin plane next: catia_create_sketch with plane='xy'.",
        )

    @tool(
        "catia_new_product",
        "Create a new empty CATProduct (assembly) document and make it active.",
        group="document",
    )
    def catia_new_product(
        part_number: Annotated[
            str, Field(description="Part number for the root product.")
        ] = "",
    ) -> dict:
        doc = session.documents.Add(constants.DOC_TYPE_PRODUCT)
        product = comutil.safe(doc, "Product")
        if part_number and product is not None:
            try:
                product.PartNumber = part_number
            except Exception as exc:
                logger.info("Could not set part number: %s", exc)
        return result.ok(
            {"document": _doc_payload(session, doc), "part_number": comutil.safe(product, "PartNumber", "")},
            message="Created a new product.",
            hint="Add components with catia_add_component or catia_add_new_part.",
        )

    @tool(
        "catia_new_drawing",
        "Create a new CATDrawing document. Optionally sets the drafting standard and the "
        "first sheet's paper size.",
        group="document",
    )
    def catia_new_drawing(
        standard: Annotated[
            str, Field(description="Drafting standard: ISO, ANSI, JIS or ASME.")
        ] = "ISO",
        paper_size: Annotated[
            str, Field(description="Paper size for sheet 1: A0-A4 or A-F.")
        ] = "A3",
        landscape: Annotated[bool, Field(description="Landscape orientation.")] = True,
    ) -> dict:
        doc = session.documents.Add(constants.DOC_TYPE_DRAWING)
        warnings: list[str] = []

        code = _DRAWING_STANDARDS.get(standard.upper())
        if code is not None:
            try:
                doc.Standard = code
            except Exception as exc:
                warnings.append("Could not apply standard %s (%s)." % (standard, exc))

        sheets = comutil.safe(doc, "Sheets")
        sheet = comutil.safe_call(sheets, "Item", 1) if sheets is not None else None
        if sheet is not None:
            size = _PAPER_SIZES.get(paper_size.upper())
            if size is not None:
                try:
                    sheet.PaperSize = size
                except Exception as exc:
                    warnings.append("Could not set paper size %s (%s)." % (paper_size, exc))
            try:
                sheet.Orientation = 0 if landscape else 1
            except Exception:
                warnings.append("Could not set sheet orientation.")

        return result.ok(
            {"document": _doc_payload(session, doc), "sheets": comutil.com_count(sheets)},
            message="Created a new drawing.",
            warnings=warnings,
            hint="Add views with catia_drawing_add_view once a 3D document is also open.",
        )

    @tool(
        "catia_open_document",
        "Open an existing file in CATIA. Handles native documents (.CATPart, .CATProduct, "
        ".CATDrawing) and every neutral format CATIA can read directly, such as .stp, "
        ".igs, .stl and .model.",
        group="document",
    )
    def catia_open_document(
        path: Annotated[str, Field(description="Absolute path to the file to open.")],
        activate: Annotated[
            bool, Field(description="Bring the document to the front after opening.")
        ] = True,
    ) -> dict:
        full = os.path.abspath(os.path.expanduser(path))
        if not os.path.exists(full):
            raise errors.InvalidArgumentError("No such file: %s" % full)
        try:
            doc = session.documents.Open(full)
        except Exception as exc:
            raise errors.OperationFailedError(
                "CATIA refused to open %s: %s" % (full, errors.com_message(exc)),
                remediation=(
                    "Check that the format is one this CATIA licence can read, and that "
                    "the file is not already open or locked by another user."
                ),
            ) from exc
        if activate:
            comutil.safe_call(doc, "Activate")
        session.state.last_document_path = full
        return result.ok(
            {"document": _doc_payload(session, doc)},
            message="Opened %s" % os.path.basename(full),
        )

    @tool(
        "catia_save_document",
        "Save a document. With no path it saves in place (and fails for a document that "
        "has never been saved); with a path it does Save As, which also converts format "
        "when the extension differs.",
        group="document",
    )
    def catia_save_document(
        path: Annotated[
            str,
            Field(
                description=(
                    "Destination path for Save As. Leave empty to save in place. "
                    "The directory is created if needed."
                )
            ),
        ] = "",
        document: Annotated[
            str, Field(description="Which open document to save. Defaults to the active one.")
        ] = "",
    ) -> dict:
        doc = _find_document(session, document or None)
        if path:
            full = os.path.abspath(os.path.expanduser(path))
            parent = os.path.dirname(full)
            if parent:
                os.makedirs(parent, exist_ok=True)
            try:
                doc.SaveAs(full)
            except Exception as exc:
                raise errors.OperationFailedError(
                    "Save As failed for %s: %s" % (full, errors.com_message(exc)),
                    remediation=(
                        "If CATIA raised an overwrite prompt, call catia_set_batch_mode "
                        "with enabled=true first so file alerts are suppressed."
                    ),
                ) from exc
            saved_to = full
        else:
            existing = str(comutil.safe(doc, "FullName", "") or "")
            if not existing or not os.path.isabs(existing) or not os.path.splitext(existing)[1]:
                raise errors.InvalidArgumentError(
                    "This document has never been saved, so there is nowhere to save it to. "
                    "Pass an explicit path."
                )
            doc.Save()
            saved_to = existing

        size = os.path.getsize(saved_to) if os.path.exists(saved_to) else None
        return result.ok(
            {"path": saved_to, "size_bytes": size, "document": _doc_payload(session, doc)},
            message="Saved to %s" % saved_to,
        )

    @tool(
        "catia_save_all",
        "Save every open document that has unsaved changes and already has a file path. "
        "Documents that have never been saved are reported back and skipped.",
        group="document",
    )
    def catia_save_all() -> dict:
        saved: list[str] = []
        skipped: list[dict[str, str]] = []
        for doc in list(comutil.com_iter(session.documents)):
            name = comutil.name_of(doc)
            full = str(comutil.safe(doc, "FullName", "") or "")
            if not full or not os.path.isabs(full):
                skipped.append({"document": name, "reason": "never saved - needs an explicit path"})
                continue
            if comutil.safe(doc, "Saved", False):
                skipped.append({"document": name, "reason": "no unsaved changes"})
                continue
            try:
                doc.Save()
                saved.append(full)
            except Exception as exc:
                skipped.append({"document": name, "reason": errors.com_message(exc)})
        return result.ok(
            {"saved": saved, "skipped": skipped},
            message="Saved %d document(s)." % len(saved),
        )

    @tool(
        "catia_close_document",
        "Close a document. Unsaved changes are discarded unless save=true, so check "
        "catia_list_documents first if you are unsure.",
        destructive=True,
        group="document",
    )
    def catia_close_document(
        document: Annotated[
            str, Field(description="Document name to close. Defaults to the active one.")
        ] = "",
        save: Annotated[
            bool, Field(description="Save before closing (requires an existing file path).")
        ] = False,
    ) -> dict:
        doc = _find_document(session, document or None)
        name = comutil.name_of(doc)
        unsaved = not bool(comutil.safe(doc, "Saved", True))
        if save:
            full = str(comutil.safe(doc, "FullName", "") or "")
            if not full or not os.path.isabs(full):
                raise errors.InvalidArgumentError(
                    "%s has never been saved; save it with an explicit path before closing."
                    % name
                )
            doc.Save()
        doc.Close()
        return result.ok(
            {"closed": name, "had_unsaved_changes": unsaved, "saved_first": save},
            message="Closed %s." % name,
        )

    @tool(
        "catia_activate_document",
        "Bring an already-open document to the front and make it the target of subsequent "
        "modelling calls.",
        idempotent=True,
        group="document",
    )
    def catia_activate_document(
        document: Annotated[str, Field(description="Name or full path of the document.")],
    ) -> dict:
        doc = _find_document(session, document)
        doc.Activate()
        return result.ok(
            {"document": _doc_payload(session, doc)},
            message="Activated %s." % comutil.name_of(doc),
        )

    @tool(
        "catia_document_info",
        "Describe the active document in detail: type, path, save state, and for parts "
        "and products the tree contents and product properties (part number, revision, "
        "definition, nomenclature).",
        readonly=True,
        group="document",
    )
    def catia_document_info() -> dict:
        doc = session.active_document()
        payload = _doc_payload(session, doc)
        kind = payload["kind"]

        if kind == "part":
            part = doc.Part
            payload["part"] = {
                "name": comutil.name_of(part),
                "bodies": comutil.com_count(comutil.safe(part, "Bodies")),
                "geometrical_sets": comutil.com_count(comutil.safe(part, "HybridBodies")),
                "parameters": comutil.com_count(comutil.safe(part, "Parameters")),
                "relations": comutil.com_count(comutil.safe(part, "Relations")),
                "main_body": comutil.name_of(comutil.safe(part, "MainBody")),
            }
        elif kind == "product":
            product = doc.Product
            payload["product"] = {
                "part_number": str(comutil.safe(product, "PartNumber", "") or ""),
                "components": comutil.com_count(comutil.safe(product, "Products")),
                "constraints": comutil.com_count(comutil.safe(product, "Connections", None)),
            }
        elif kind == "drawing":
            sheets = comutil.safe(doc, "Sheets")
            payload["drawing"] = {
                "sheets": comutil.com_count(sheets),
                "sheet_names": [comutil.name_of(s) for s in comutil.com_iter(sheets)],
            }

        properties = _product_properties(doc)
        if properties:
            payload["properties"] = properties
        return result.ok(payload)

    @tool(
        "catia_set_document_properties",
        "Set the product identity fields CATIA stores on a Part or Product document: "
        "part number, revision, definition, nomenclature and description. These are what "
        "flow into a bill of materials and a drawing title block.",
        group="document",
    )
    def catia_set_document_properties(
        part_number: Annotated[str, Field(description="Part number.")] = "",
        revision: Annotated[str, Field(description="Revision string.")] = "",
        definition: Annotated[str, Field(description="Definition / description field.")] = "",
        nomenclature: Annotated[str, Field(description="Nomenclature field.")] = "",
        description: Annotated[str, Field(description="Reference description.")] = "",
    ) -> dict:
        doc = session.active_document()
        product = comutil.safe(doc, "Product")
        if product is None:
            raise errors.WrongDocumentTypeError(
                "Only Part and Product documents carry these properties."
            )
        applied: dict[str, str] = {}
        warnings: list[str] = []
        for member, value in (
            ("PartNumber", part_number),
            ("Revision", revision),
            ("Definition", definition),
            ("Nomenclature", nomenclature),
            ("DescriptionRef", description),
        ):
            if not value:
                continue
            try:
                setattr(product, member, value)
                applied[member] = value
            except Exception as exc:
                warnings.append("%s: %s" % (member, errors.com_message(exc)))
        return result.ok(
            {"applied": applied, "properties": _product_properties(doc)},
            message="Updated %d propert(ies)." % len(applied),
            warnings=warnings,
        )


def _product_properties(doc: Any) -> dict[str, Any]:
    product = comutil.safe(doc, "Product")
    if product is None:
        return {}
    out: dict[str, Any] = {}
    for member in ("PartNumber", "Revision", "Definition", "Nomenclature", "DescriptionRef"):
        value = comutil.safe(product, member)
        if value:
            out[member] = str(value)
    return out
