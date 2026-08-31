"""Connection lifecycle, diagnostics and the raw scripting escape hatch."""

from __future__ import annotations

import logging
from typing import Annotated, Any

from pydantic import Field

from catia_mcp import compat
from catia_mcp.core import comutil, constants, errors, refs, result
from catia_mcp.tools.base import registrar

logger = logging.getLogger("catia_mcp.tools.session")


def register(mcp: Any, session: Any) -> None:
    tool = registrar(mcp, session)

    @tool(
        "catia_connect",
        "Attach to a running CATIA session. Call this first. By default it only attaches "
        "to a CATIA that is already open; pass launch=true to start one (slow, and it "
        "checks out a licence). Returns the detected release and family so later calls "
        "can be tailored to it.",
        connect=False,
        idempotent=True,
        group="session",
    )
    def catia_connect(
        launch: Annotated[
            bool, Field(description="Start CATIA if no running instance is found.")
        ] = False,
        visible: Annotated[
            bool, Field(description="Show the CATIA window when launching it.")
        ] = True,
    ) -> dict:
        info = session.connect(launch=launch, visible=visible)
        version = info["catia"]
        return result.ok(
            info,
            message="Connected to %s" % version.get("marketing_name"),
            hint=(
                "Call catia_capabilities to see which workbenches this installation "
                "exposes, and catia_reference_help for the geometry-selection grammar."
            ),
        )

    @tool(
        "catia_disconnect",
        "Release this server's COM reference to CATIA. CATIA itself keeps running and "
        "no document is closed.",
        connect=False,
        idempotent=True,
        group="session",
    )
    def catia_disconnect() -> dict:
        return result.ok(session.disconnect(), message="Released the CATIA connection.")

    @tool(
        "catia_status",
        "Report the current connection: CATIA release, the active document and its type, "
        "how many documents are open, and the geometry this server created most recently.",
        connect=False,
        readonly=True,
        group="session",
    )
    def catia_status() -> dict:
        if session.app is None:
            return result.ok(
                {"connected": False},
                message="Not connected to CATIA.",
                hint="Call catia_connect.",
            )
        payload: dict[str, Any] = {
            "connected": True,
            "catia": session.version.as_dict(),
            "documents_open": comutil.com_count(comutil.safe(session.app, "Documents")),
        }
        try:
            doc = session.active_document()
            payload["active_document"] = {
                "name": comutil.name_of(doc),
                "kind": session.document_kind(doc),
                "path": str(comutil.safe(doc, "FullName", "") or ""),
                "saved": bool(comutil.safe(doc, "Saved", False)),
            }
        except errors.CatiaError:
            payload["active_document"] = None
        payload["session_state"] = {
            "last_sketch": session.state.last_sketch_name,
            "last_feature": session.state.last_feature_name,
            "last_geoset": session.state.last_geoset_name,
        }
        return result.ok(payload)

    @tool(
        "catia_check_environment",
        "Diagnose the host before blaming CATIA: Python version and bitness, whether "
        "pywin32 imported, whether the CATIA.Application COM class is registered, and "
        "which MCP SDK is in use. Works without a CATIA connection - run it first when "
        "catia_connect fails.",
        connect=False,
        readonly=True,
        group="session",
    )
    def catia_check_environment() -> dict:
        report = session.environment_report()
        report["mcp_sdk"] = compat.sdk_info()
        report["constants_override_file"] = constants.override_source()

        problems: list[str] = []
        if report["platform"] != "win32":
            problems.append(
                "Not running on Windows. CATIA automation is Windows COM only."
            )
        if not report["pywin32_available"]:
            problems.append(
                "pywin32 did not import (%s). Run: pip install pywin32"
                % report.get("pywin32_error")
            )
        if report.get("catia_progid_registered") is False:
            problems.append(
                "The COM class 'CATIA.Application' is not registered on this machine, so "
                "CATIA is either not installed or was never started once as this user."
            )
        report["problems"] = problems
        report["healthy"] = not problems
        return result.ok(
            report,
            message="Environment looks usable." if not problems else "Found %d problem(s)."
            % len(problems),
        )

    @tool(
        "catia_capabilities",
        "Probe what this specific CATIA installation can do - Part Design, Generative "
        "Shape Design, Sheet Metal, Assembly, Drafting, measurement, kinematics and the "
        "in-process script bridge. Licences, not release numbers, decide most of these, "
        "so this is probed by attempting the real calls.",
        readonly=True,
        group="session",
    )
    def catia_capabilities(
        refresh: Annotated[
            bool, Field(description="Re-probe instead of using the cached answer.")
        ] = False,
    ) -> dict:
        caps = session.capabilities(refresh=refresh)
        return result.ok(
            {"catia": session.version.as_dict(), "capabilities": caps},
            hint=(
                "A capability reported as null could not be probed because no document of "
                "the matching type is open."
            ),
        )

    @tool(
        "catia_reference_help",
        "Explain the reference-token grammar used by every tool that needs to point at "
        "geometry (planes, faces, edges, sketches, features). Read this before guessing "
        "at a selector.",
        connect=False,
        readonly=True,
        group="session",
    )
    def catia_reference_help() -> dict:
        return result.ok(refs.GRAMMAR_HELP)

    @tool(
        "catia_run_script",
        "Run a VBScript function inside CATIA and return its value. This is the escape "
        "hatch to the whole CATIA object model, including anything this server has no "
        "dedicated tool for and any API that only exists on some releases. The script "
        "must define the named function; CATIA is passed in automatically as the first "
        "parameter unless you pass your own. Returns whatever the function returns.",
        destructive=True,
        group="session",
    )
    def catia_run_script(
        script: Annotated[
            str,
            Field(
                description=(
                    "Full VBScript source, defining the function named below. Example: "
                    "'Function Run(catia)\\n  Run = catia.Documents.Count\\nEnd Function'"
                )
            ),
        ],
        function: Annotated[
            str, Field(description="Name of the function inside the script to call.")
        ] = "Run",
        pass_application: Annotated[
            bool,
            Field(
                description=(
                    "Pass the CATIA Application object as the first argument to the "
                    "function. Turn this off if your function takes no arguments."
                )
            ),
        ] = True,
    ) -> dict:
        app = session.require()
        if not comutil.has_member(comutil.safe(app, "SystemService"), "Evaluate"):
            raise errors.UnsupportedCapabilityError(
                "SystemService.Evaluate is not exposed by this CATIA release, so scripts "
                "cannot be evaluated in process."
            )
        params = [app] if pass_application else []
        value = comutil.evaluate_script(app, script, function, params)
        return result.ok(
            {"function": function, "value": _jsonable(value)},
            message="Script executed.",
        )

    @tool(
        "catia_start_command",
        "Trigger a CATIA interactive command by its name, the way a menu item would - "
        "for example 'Undo', 'Redo', 'Fit All In' or 'Isometric View'. Useful for the "
        "handful of behaviours CATIA exposes nowhere else in the automation API. The "
        "command name must match the language of the CATIA user interface.",
        destructive=True,
        group="session",
    )
    def catia_start_command(
        command: Annotated[
            str, Field(description="Interactive command name, e.g. 'Undo' or 'Fit All In'.")
        ],
    ) -> dict:
        app = session.require()
        if not comutil.has_member(app, "StartCommand"):
            raise errors.UnsupportedCapabilityError(
                "Application.StartCommand is not available on this CATIA release."
            )
        app.StartCommand(command)
        return result.ok(
            {"command": command},
            message="Sent '%s' to CATIA." % command,
            hint=(
                "StartCommand is fire-and-forget: CATIA reports no result, and it fails "
                "silently when the command name does not match the UI language."
            ),
        )

    @tool(
        "catia_undo",
        "Undo the last operation(s) in CATIA. Implemented through the interactive Undo "
        "command, so it is best-effort: CATIA gives automation clients no way to confirm "
        "how far the stack actually rewound.",
        destructive=True,
        group="session",
    )
    def catia_undo(
        times: Annotated[int, Field(description="How many steps to undo.", ge=1, le=50)] = 1,
    ) -> dict:
        app = session.require()
        if not comutil.has_member(app, "StartCommand"):
            raise errors.UnsupportedCapabilityError(
                "Application.StartCommand is not available, so Undo cannot be driven."
            )
        for _ in range(times):
            app.StartCommand("Undo")
        return result.ok(
            {"requested_steps": times},
            message="Requested %d undo step(s)." % times,
            hint="Verify with catia_list_features that the model is in the state you expect.",
        )

    @tool(
        "catia_redo",
        "Redo the last undone operation(s) in CATIA. Best-effort, like catia_undo.",
        destructive=True,
        group="session",
    )
    def catia_redo(
        times: Annotated[int, Field(description="How many steps to redo.", ge=1, le=50)] = 1,
    ) -> dict:
        app = session.require()
        for _ in range(times):
            app.StartCommand("Redo")
        return result.ok({"requested_steps": times}, message="Requested %d redo step(s)." % times)

    @tool(
        "catia_set_batch_mode",
        "Suspend or resume CATIA's screen refresh and file-alert dialogs. Turning batch "
        "mode on makes long sequences of modelling calls markedly faster and stops modal "
        "save/overwrite prompts from blocking automation. Always turn it back off when "
        "you are done, or the user is left with a frozen-looking viewport.",
        idempotent=True,
        group="session",
    )
    def catia_set_batch_mode(
        enabled: Annotated[
            bool, Field(description="True to suspend refresh and alerts, False to restore.")
        ],
    ) -> dict:
        session.set_visual_batching(quiet=enabled)
        return result.ok(
            {"batch_mode": enabled},
            message="Batch mode %s." % ("enabled" if enabled else "disabled"),
            hint="Remember to call this again with enabled=false before handing back control."
            if enabled
            else "",
        )


def _jsonable(value: Any) -> Any:
    """Coerce whatever CATIA handed back into something JSON can carry."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    try:
        return str(value)
    except Exception:  # pragma: no cover
        return repr(value)
