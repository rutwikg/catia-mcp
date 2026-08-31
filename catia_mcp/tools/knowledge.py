"""Knowledge: parameters, formulas, relations and design tables.

This is what turns a model into a configurable one. Every dimension a feature
tool creates is reachable here by name, so a part built through this server can
be re-driven without recreating any geometry.
"""

from __future__ import annotations

import logging
import os
from typing import Annotated, Any

from pydantic import Field

from catia_mcp.core import comutil, errors, result
from catia_mcp.tools.base import registrar

logger = logging.getLogger("catia_mcp.tools.knowledge")

PARAMETER_TYPES = {
    "real": ("CreateReal", float),
    "integer": ("CreateInteger", int),
    "string": ("CreateString", str),
    "boolean": ("CreateBoolean", bool),
    "length": ("CreateDimension", float),
    "angle": ("CreateDimension", float),
}

DIMENSION_MAGNITUDES = {"length": "LENGTH", "angle": "ANGLE"}


def _parameters(session: Any) -> Any:
    doc = session.active_document()
    for holder in (comutil.safe(doc, "Part"), comutil.safe(doc, "Product"), doc):
        collection = comutil.safe(holder, "Parameters")
        if collection is not None:
            return collection
    raise errors.WrongDocumentTypeError(
        "The active document exposes no Parameters collection."
    )


def _relations(session: Any) -> Any:
    doc = session.active_document()
    for holder in (comutil.safe(doc, "Part"), comutil.safe(doc, "Product"), doc):
        collection = comutil.safe(holder, "Relations")
        if collection is not None:
            return collection
    raise errors.WrongDocumentTypeError(
        "The active document exposes no Relations collection."
    )


def _parameter_entry(param: Any, include_value: bool = True) -> dict[str, Any]:
    entry: dict[str, Any] = {"name": comutil.name_of(param)}
    if include_value:
        value = comutil.safe(param, "Value")
        if value is not None:
            entry["value"] = value if not isinstance(value, float) else round(value, 6)
        text = comutil.safe_call(param, "ValueAsString")
        if text:
            entry["display"] = str(text)
    comment = comutil.safe(param, "Comment")
    if comment:
        entry["comment"] = str(comment)
    formula = comutil.safe(param, "OptionalFormula")
    if formula is not None:
        entry["driven_by_formula"] = comutil.name_of(formula)
    return entry


def register(mcp: Any, session: Any) -> None:
    tool = registrar(mcp, session)

    # ── parameters ───────────────────────────────────────────────────────────

    @tool(
        "catia_list_parameters",
        "List the document's parameters with their current values. Feature dimensions "
        "appear here under names such as 'Pad.1\\FirstLimit\\Length', which is how you "
        "drive an existing model without editing its features.",
        readonly=True,
        group="knowledge",
    )
    def catia_list_parameters(
        filter: Annotated[
            str, Field(description="Case-insensitive substring to match against names.")
        ] = "",
        user_only: Annotated[
            bool,
            Field(
                description=(
                    "Show only user-created parameters, hiding the many automatic "
                    "feature dimensions."
                )
            ),
        ] = False,
        limit: Annotated[int, Field(description="Maximum to return.", ge=1, le=2000)] = 300,
    ) -> dict:
        collection = _parameters(session)
        needle = filter.lower()
        entries: list[dict[str, Any]] = []
        total = comutil.com_count(collection)

        user_names: set[str] = set()
        if user_only:
            root = comutil.safe(collection, "RootParameterSet")
            for param in comutil.com_iter(comutil.safe(root, "DirectParameters")):
                user_names.add(comutil.name_of(param))

        for param in comutil.com_iter(collection):
            name = comutil.name_of(param)
            if needle and needle not in name.lower():
                continue
            if user_only and name not in user_names:
                continue
            entries.append(_parameter_entry(param))
            if len(entries) >= limit:
                break

        return result.ok(
            {"total_in_document": total, "returned": len(entries), "parameters": entries},
            hint=(
                "Change one with catia_set_parameter, then call catia_update to recompute "
                "the model."
            ),
        )

    @tool(
        "catia_get_parameter",
        "Read one parameter by its exact name, including its formula if it is driven by one.",
        readonly=True,
        group="knowledge",
    )
    def catia_get_parameter(
        name: Annotated[str, Field(description="Exact parameter name.")],
    ) -> dict:
        collection = _parameters(session)
        param = comutil.safe_call(collection, "Item", name)
        if param is None:
            raise errors.ElementNotFoundError(
                "No parameter called %r. Use catia_list_parameters with a filter to find it."
                % name
            )
        return result.ok(_parameter_entry(param))

    @tool(
        "catia_set_parameter",
        "Change a parameter's value and recompute the model. Give either a number, or a "
        "string with a unit such as '25mm' or '30deg' - the string form is safer because "
        "CATIA does the unit conversion itself.",
        group="knowledge",
    )
    def catia_set_parameter(
        name: Annotated[str, Field(description="Exact parameter name.")],
        value: Annotated[
            float | None, Field(description="Numeric value, in CATIA's internal units (mm, deg).")
        ] = None,
        text_value: Annotated[
            str,
            Field(description="Value with an explicit unit, e.g. '25mm', '3.5in', '45deg'."),
        ] = "",
        update: Annotated[bool, Field(description="Recompute the model afterwards.")] = True,
    ) -> dict:
        if value is None and not text_value:
            raise errors.InvalidArgumentError("Give either value or text_value.")
        collection = _parameters(session)
        param = comutil.safe_call(collection, "Item", name)
        if param is None:
            raise errors.ElementNotFoundError("No parameter called %r." % name)

        previous = comutil.safe(param, "Value")
        try:
            if text_value:
                param.ValuateFromString(text_value)
            else:
                param.Value = value
        except Exception as exc:
            raise errors.OperationFailedError(
                "Could not set %s: %s" % (name, errors.com_message(exc)),
                remediation=(
                    "A parameter driven by a formula cannot be set directly - deactivate "
                    "the formula first with catia_set_relation_active."
                ),
            ) from exc

        warnings: list[str] = []
        if update:
            doc = session.active_document()
            kind = session.document_kind(doc)
            try:
                if kind == "part":
                    session.update_part(doc.Part)
                elif kind == "product":
                    doc.Product.Update()
            except errors.CatiaError as exc:
                warnings.append(
                    "The value was set but the model failed to update: %s. The old value "
                    "was %r." % (exc.message, previous)
                )
            session.refresh_view()

        return result.ok(
            {
                "name": name,
                "previous_value": previous,
                "new_value": comutil.safe(param, "Value"),
                "display": comutil.safe_call(param, "ValueAsString"),
            },
            message="Set %s." % name,
            warnings=warnings,
        )

    @tool(
        "catia_create_parameter",
        "Create a user parameter to drive the model - a named length, angle, number, "
        "string or flag that formulas and feature dimensions can refer to.",
        group="knowledge",
    )
    def catia_create_parameter(
        name: Annotated[str, Field(description="Name for the new parameter.")],
        parameter_type: Annotated[
            str, Field(description="length | angle | real | integer | string | boolean.")
        ] = "length",
        value: Annotated[
            float, Field(description="Initial numeric value (mm for length, degrees for angle).")
        ] = 0.0,
        text_value: Annotated[
            str, Field(description="Initial value for a string parameter.")
        ] = "",
        comment: Annotated[str, Field(description="Optional description.")] = "",
    ) -> dict:
        key = parameter_type.strip().lower()
        if key not in PARAMETER_TYPES:
            raise errors.InvalidArgumentError(
                "parameter_type must be one of %s." % ", ".join(sorted(PARAMETER_TYPES))
            )
        collection = _parameters(session)
        method, caster = PARAMETER_TYPES[key]

        if key in DIMENSION_MAGNITUDES:
            _, param = comutil.try_variants(
                [
                    (
                        "CreateDimension",
                        lambda: collection.CreateDimension(
                            name, DIMENSION_MAGNITUDES[key], float(value)
                        ),
                    ),
                    ("CreateReal", lambda: collection.CreateReal(name, float(value))),
                ],
                what="create a %s parameter" % key,
            )
        elif key == "string":
            param = collection.CreateString(name, text_value)
        elif key == "boolean":
            param = collection.CreateBoolean(name, bool(value))
        else:
            param = getattr(collection, method)(name, caster(value))

        if comment:
            try:
                param.Comment = comment
            except Exception:
                pass
        return result.ok(
            _parameter_entry(param),
            message="Created parameter %r." % comutil.name_of(param),
            hint="Drive a feature with it using catia_create_formula.",
        )

    @tool(
        "catia_delete_parameter",
        "Delete a user parameter.",
        destructive=True,
        group="knowledge",
    )
    def catia_delete_parameter(
        name: Annotated[str, Field(description="Parameter name.")],
    ) -> dict:
        collection = _parameters(session)
        param = comutil.safe_call(collection, "Item", name)
        if param is None:
            raise errors.ElementNotFoundError("No parameter called %r." % name)
        selection = session.selection()
        selection.Clear()
        selection.Add(param)
        selection.Delete()
        selection.Clear()
        return result.ok({"deleted": name}, message="Deleted parameter %s." % name)

    # ── relations ────────────────────────────────────────────────────────────

    @tool(
        "catia_list_relations",
        "List the document's relations - formulas, rules, checks and design tables - with "
        "their expressions and whether they are active.",
        readonly=True,
        group="knowledge",
    )
    def catia_list_relations() -> dict:
        collection = _relations(session)
        entries = []
        for relation in comutil.com_iter(collection):
            entry: dict[str, Any] = {
                "name": comutil.name_of(relation),
                "reference_token": "name:%s" % comutil.name_of(relation),
            }
            for label, member in (
                ("expression", "Expression"),
                ("comment", "Comment"),
                ("type", "Type"),
            ):
                value = comutil.safe(relation, member)
                if value not in (None, ""):
                    entry[label] = str(value)
            activity = comutil.safe(relation, "Activity")
            if activity is not None:
                entry["active"] = bool(activity)
            entries.append(entry)
        return result.ok({"count": len(entries), "relations": entries})

    @tool(
        "catia_create_formula",
        "Drive one parameter from an expression involving others. The expression is CATIA "
        "Knowledge syntax and must carry units, for example 'Width * 2 + 5mm'. Feature "
        "dimensions can be the target, so this is how you make a model self-adjusting.",
        group="knowledge",
    )
    def catia_create_formula(
        target_parameter: Annotated[
            str,
            Field(
                description=(
                    "Exact name of the parameter to drive, e.g. 'Pad.1\\FirstLimit\\Length'."
                )
            ),
        ],
        expression: Annotated[
            str, Field(description="CATIA Knowledge expression, with units: 'Width * 2 + 5mm'.")
        ],
        name: Annotated[str, Field(description="Name for the formula.")] = "",
        comment: Annotated[str, Field(description="Optional description.")] = "",
    ) -> dict:
        parameters = _parameters(session)
        relations = _relations(session)
        target = comutil.safe_call(parameters, "Item", target_parameter)
        if target is None:
            raise errors.ElementNotFoundError(
                "No parameter called %r to drive." % target_parameter
            )
        try:
            formula = relations.CreateFormula(
                name or "", comment or "", target, expression
            )
        except Exception as exc:
            raise errors.OperationFailedError(
                "CATIA rejected the formula: %s" % errors.com_message(exc),
                remediation=(
                    "Knowledge expressions need units on literals ('5mm', not '5') and the "
                    "referenced parameters must already exist. Check names with "
                    "catia_list_parameters."
                ),
            ) from exc

        doc = session.active_document()
        if session.document_kind(doc) == "part":
            session.update_part(doc.Part)
        return result.ok(
            {
                "created": comutil.name_of(formula),
                "target": target_parameter,
                "expression": expression,
                "resulting_value": comutil.safe(target, "Value"),
            },
            message="Created formula %r." % comutil.name_of(formula),
        )

    @tool(
        "catia_set_relation_active",
        "Activate or deactivate a formula or rule. Deactivating a formula is what lets you "
        "set its target parameter by hand again.",
        group="knowledge",
    )
    def catia_set_relation_active(
        name: Annotated[str, Field(description="Relation name.")],
        active: Annotated[bool, Field(description="True to activate, False to deactivate.")],
    ) -> dict:
        collection = _relations(session)
        relation = comutil.safe_call(collection, "Item", name)
        if relation is None:
            raise errors.ElementNotFoundError("No relation called %r." % name)
        try:
            relation.Activity = bool(active)
        except Exception as exc:
            raise errors.OperationFailedError(
                "Could not change the relation's activity: %s" % errors.com_message(exc)
            ) from exc
        return result.ok(
            {"relation": name, "active": bool(active)},
            message="%s %s." % ("Activated" if active else "Deactivated", name),
        )

    @tool(
        "catia_delete_relation",
        "Delete a formula, rule, check or design table.",
        destructive=True,
        group="knowledge",
    )
    def catia_delete_relation(
        name: Annotated[str, Field(description="Relation name.")],
    ) -> dict:
        collection = _relations(session)
        relation = comutil.safe_call(collection, "Item", name)
        if relation is None:
            raise errors.ElementNotFoundError("No relation called %r." % name)
        selection = session.selection()
        selection.Clear()
        selection.Add(relation)
        selection.Delete()
        selection.Clear()
        return result.ok({"deleted": name}, message="Deleted relation %s." % name)

    # ── design tables ────────────────────────────────────────────────────────

    @tool(
        "catia_add_design_table",
        "Attach a design table (an Excel or tab-separated file) whose columns drive the "
        "document's parameters. Each row becomes a selectable configuration.",
        group="knowledge",
    )
    def catia_add_design_table(
        path: Annotated[
            str, Field(description="Absolute path to the .xls, .xlsx or .txt design table.")
        ],
        name: Annotated[str, Field(description="Name for the design table relation.")] = "",
        copy_data: Annotated[
            bool,
            Field(
                description=(
                    "Copy the data into the CATIA document instead of linking to the file "
                    "on disk."
                )
            ),
        ] = False,
        comment: Annotated[str, Field(description="Optional description.")] = "",
    ) -> dict:
        full = os.path.abspath(os.path.expanduser(path))
        if not os.path.exists(full):
            raise errors.InvalidArgumentError("No such file: %s" % full)
        relations = _relations(session)
        _, table = comutil.try_variants(
            [
                (
                    "CreateDesignTable(4)",
                    lambda: relations.CreateDesignTable(
                        name or os.path.basename(full), comment or "", bool(copy_data), full
                    ),
                ),
                (
                    "CreateDesignTable(3)",
                    lambda: relations.CreateDesignTable(
                        name or os.path.basename(full), comment or "", full
                    ),
                ),
            ],
            what="attach a design table",
        )
        return result.ok(
            {
                "created": comutil.name_of(table),
                "file": full,
                "configurations": comutil.safe(table, "ConfigurationsNb"),
            },
            message="Attached design table %r." % comutil.name_of(table),
            hint="Switch between rows with catia_set_design_table_configuration.",
        )

    @tool(
        "catia_set_design_table_configuration",
        "Select which row of a design table drives the model, then recompute.",
        group="knowledge",
    )
    def catia_set_design_table_configuration(
        name: Annotated[str, Field(description="Design table relation name.")],
        configuration: Annotated[
            int, Field(description="1-based row number to apply.", ge=1)
        ],
    ) -> dict:
        relations = _relations(session)
        table = comutil.safe_call(relations, "Item", name)
        if table is None:
            raise errors.ElementNotFoundError("No design table called %r." % name)
        count = comutil.safe(table, "ConfigurationsNb")
        if count is not None and configuration > int(count):
            raise errors.InvalidArgumentError(
                "Configuration %d is out of range; the table has %d row(s)."
                % (configuration, int(count))
            )
        try:
            table.Configuration = int(configuration)
        except Exception as exc:
            raise errors.OperationFailedError(
                "Could not switch configuration: %s" % errors.com_message(exc)
            ) from exc
        doc = session.active_document()
        if session.document_kind(doc) == "part":
            session.update_part(doc.Part)
        session.refresh_view()
        return result.ok(
            {"design_table": name, "configuration": int(configuration), "row_count": count},
            message="Applied configuration %d." % configuration,
        )

    @tool(
        "catia_list_design_tables",
        "List the design tables attached to the document, with their row counts and the "
        "row currently applied.",
        readonly=True,
        group="knowledge",
    )
    def catia_list_design_tables() -> dict:
        relations = _relations(session)
        tables = []
        for relation in comutil.com_iter(relations):
            count = comutil.safe(relation, "ConfigurationsNb")
            if count is None:
                continue
            tables.append(
                {
                    "name": comutil.name_of(relation),
                    "configurations": int(count),
                    "current_configuration": comutil.safe(relation, "Configuration"),
                    "file": str(comutil.safe(relation, "SheetName", "") or ""),
                }
            )
        return result.ok({"count": len(tables), "design_tables": tables})
