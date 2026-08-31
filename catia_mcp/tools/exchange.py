"""Data exchange: exporting to neutral formats and reporting what is possible."""

from __future__ import annotations

import csv
import logging
import os
from typing import Annotated, Any

from pydantic import Field

from catia_mcp.core import comutil, constants, errors, result
from catia_mcp.tools.base import registrar

logger = logging.getLogger("catia_mcp.tools.exchange")


def register(mcp: Any, session: Any) -> None:
    tool = registrar(mcp, session)

    @tool(
        "catia_list_export_formats",
        "List the neutral formats this server knows how to ask CATIA for, and which "
        "document types each applies to. Whether a given format actually works also "
        "depends on the licences installed.",
        connect=False,
        readonly=True,
        group="exchange",
    )
    def catia_list_export_formats() -> dict:
        rows = []
        seen: set[str] = set()
        for name, spec in sorted(constants.EXPORT_FORMATS.items()):
            signature = "%s|%s" % (spec["key"], spec["label"])
            if signature in seen:
                continue
            seen.add(signature)
            rows.append(
                {
                    "format": name,
                    "catia_key": spec["key"],
                    "extension": spec["ext"],
                    "applies_to": spec["kinds"],
                    "description": spec["label"],
                }
            )
        return result.ok({"count": len(rows), "formats": rows})

    @tool(
        "catia_export",
        "Export the active document to a neutral format - STEP, IGES, STL, VRML, 3D XML, "
        "CGR for 3D documents, and DXF, DWG, PDF, CGM or SVG for drawings. The format is "
        "taken from the file extension unless you name it explicitly.",
        group="exchange",
    )
    def catia_export(
        path: Annotated[
            str, Field(description="Destination file path, including the extension.")
        ],
        format: Annotated[
            str,
            Field(
                description=(
                    "Format name, e.g. 'step', 'stl', 'iges'. Empty derives it from the "
                    "file extension."
                )
            ),
        ] = "",
        overwrite: Annotated[
            bool, Field(description="Replace the file if it already exists.")
        ] = True,
    ) -> dict:
        doc = session.active_document()
        kind = session.document_kind(doc)
        full = os.path.abspath(os.path.expanduser(path))

        key = (format or os.path.splitext(full)[1].lstrip(".")).lower()
        spec = constants.EXPORT_FORMATS.get(key)
        if spec is None:
            raise errors.InvalidArgumentError(
                "Unknown export format %r. Call catia_list_export_formats to see the "
                "supported names." % key
            )
        if kind not in spec["kinds"]:
            raise errors.WrongDocumentTypeError(
                "%s export applies to %s documents, but the active document is a %s "
                "document." % (spec["label"], " or ".join(spec["kinds"]), kind)
            )

        if os.path.exists(full) and not overwrite:
            raise errors.InvalidArgumentError(
                "%s already exists and overwrite is false." % full
            )
        parent = os.path.dirname(full)
        if parent:
            os.makedirs(parent, exist_ok=True)

        # CATIA may raise a modal overwrite prompt; suppress alerts around the call,
        # then restore whatever the caller had set rather than forcing them back on.
        previous_batch_mode = session.batch_mode
        session.set_visual_batching(quiet=True)
        try:
            _, _ = comutil.try_variants(
                [
                    ("ExportData", lambda: doc.ExportData(full, spec["key"])),
                    ("SaveAs", lambda: doc.SaveAs(full)),
                ],
                what="export to %s" % spec["label"],
            )
        finally:
            session.set_visual_batching(quiet=previous_batch_mode)

        if not os.path.exists(full):
            raise errors.OperationFailedError(
                "CATIA reported success but no file appeared at %s." % full,
                remediation=(
                    "The translator for this format may not be licensed. Try a different "
                    "format, or check the CATIA window for an error dialog."
                ),
            )
        size = os.path.getsize(full)
        return result.ok(
            {
                "path": full,
                "format": key,
                "catia_key": spec["key"],
                "size_bytes": size,
                "size_human": _human(size),
                "source_document": comutil.name_of(doc),
            },
            message="Exported %s to %s (%s)." % (comutil.name_of(doc), full, _human(size)),
        )

    @tool(
        "catia_export_bom_csv",
        "Write the active assembly's bill of materials to a CSV file: part number, "
        "quantity, nomenclature, revision, definition and source file.",
        group="exchange",
    )
    def catia_export_bom_csv(
        path: Annotated[str, Field(description="Destination .csv path.")],
        max_depth: Annotated[int, Field(description="Recursion depth.", ge=1, le=12)] = 10,
    ) -> dict:
        product = session.active_product()
        rows: dict[str, dict[str, Any]] = {}

        def walk(node: Any, depth: int) -> None:
            if depth > max_depth:
                return
            for child in comutil.com_iter(comutil.safe(node, "Products")):
                part_number = str(
                    comutil.safe(child, "PartNumber", "") or comutil.name_of(child)
                )
                row = rows.setdefault(
                    part_number,
                    {
                        "part_number": part_number,
                        "quantity": 0,
                        "nomenclature": str(comutil.safe(child, "Nomenclature", "") or ""),
                        "revision": str(comutil.safe(child, "Revision", "") or ""),
                        "definition": str(comutil.safe(child, "Definition", "") or ""),
                        "file": "",
                    },
                )
                row["quantity"] += 1
                reference = comutil.safe(child, "ReferenceProduct")
                source = comutil.safe(reference, "Parent") if reference is not None else None
                file_path = str(comutil.safe(source, "FullName", "") or "")
                if file_path and not row["file"]:
                    row["file"] = file_path
                walk(child, depth + 1)

        walk(product, 1)
        full = os.path.abspath(os.path.expanduser(path))
        parent = os.path.dirname(full)
        if parent:
            os.makedirs(parent, exist_ok=True)

        fields = ["part_number", "quantity", "nomenclature", "revision", "definition", "file"]
        with open(full, "w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            for row in sorted(rows.values(), key=lambda r: r["part_number"]):
                writer.writerow(row)

        return result.ok(
            {
                "path": full,
                "rows": len(rows),
                "total_instances": sum(r["quantity"] for r in rows.values()),
            },
            message="Wrote %d bill-of-materials rows to %s." % (len(rows), full),
        )

    @tool(
        "catia_export_all_open",
        "Export every open 3D document to a directory in one format. Useful for turning a "
        "session's worth of work into STEP files in a single call.",
        group="exchange",
    )
    def catia_export_all_open(
        directory: Annotated[str, Field(description="Destination directory.")],
        format: Annotated[str, Field(description="Format name, e.g. 'step' or 'stl'.")] = "step",
    ) -> dict:
        key = format.lower()
        spec = constants.EXPORT_FORMATS.get(key)
        if spec is None:
            raise errors.InvalidArgumentError("Unknown export format %r." % format)
        target = os.path.abspath(os.path.expanduser(directory))
        os.makedirs(target, exist_ok=True)

        exported: list[dict[str, Any]] = []
        skipped: list[dict[str, str]] = []
        previous_batch_mode = session.batch_mode
        session.set_visual_batching(quiet=True)
        try:
            for doc in list(comutil.com_iter(session.documents)):
                name = comutil.name_of(doc)
                kind = session.document_kind(doc)
                if kind not in spec["kinds"]:
                    skipped.append({"document": name, "reason": "%s document" % kind})
                    continue
                stem = os.path.splitext(name)[0] or "document"
                destination = os.path.join(target, stem + spec["ext"])
                try:
                    doc.ExportData(destination, spec["key"])
                    exported.append(
                        {
                            "document": name,
                            "path": destination,
                            "size_bytes": os.path.getsize(destination)
                            if os.path.exists(destination)
                            else 0,
                        }
                    )
                except Exception as exc:
                    skipped.append({"document": name, "reason": errors.com_message(exc)})
        finally:
            session.set_visual_batching(quiet=previous_batch_mode)

        return result.ok(
            {"format": key, "directory": target, "exported": exported, "skipped": skipped},
            message="Exported %d document(s)." % len(exported),
        )


def _human(size: int) -> str:
    if size >= 1024 * 1024:
        return "%.1f MB" % (size / (1024.0 * 1024.0))
    if size >= 1024:
        return "%.1f KB" % (size / 1024.0)
    return "%d bytes" % size
