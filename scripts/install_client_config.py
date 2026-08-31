"""Register this server with an MCP client, without needing the Claude CLI.

The ``claude`` command is a separate npm package from the Claude desktop app,
so ``claude mcp add`` is unavailable on most machines. This writes the same
configuration directly.

    python scripts/install_client_config.py --dry-run     # show the change
    python scripts/install_client_config.py               # apply it
    python scripts/install_client_config.py --target all  # every client found
    python scripts/install_client_config.py --remove      # undo

The interpreter running this script is the one written into the configuration,
which is what you want: it is the interpreter that has pywin32 and mcp
installed. The repository path is likewise taken from this file's location, so
the result is correct wherever the repository was cloned.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER_KEY = "catia"


def desktop_config_path() -> str:
    """Where the Claude desktop app keeps its MCP configuration."""
    if sys.platform == "win32":
        base = os.environ.get("APPDATA", os.path.expanduser("~\\AppData\\Roaming"))
        return os.path.join(base, "Claude", "claude_desktop_config.json")
    if sys.platform == "darwin":
        return os.path.expanduser(
            "~/Library/Application Support/Claude/claude_desktop_config.json"
        )
    return os.path.expanduser("~/.config/Claude/claude_desktop_config.json")


def code_config_path() -> str:
    """Where Claude Code keeps its user-level configuration."""
    return os.path.expanduser("~/.claude.json")


def project_config_path() -> str:
    """A project-scoped config, picked up when working inside this repository."""
    return os.path.join(REPO_ROOT, ".mcp.json")


TARGETS = {
    "desktop": ("Claude desktop app", desktop_config_path),
    "code": ("Claude Code (user level)", code_config_path),
    "project": ("this repository (.mcp.json)", project_config_path),
}


def server_entry(log: bool) -> dict:
    entry: dict = {
        "command": sys.executable,
        "args": ["-m", "catia_mcp"],
        "env": {"PYTHONPATH": REPO_ROOT},
    }
    if log:
        entry["env"]["CATIA_MCP_LOG"] = os.path.join(REPO_ROOT, "catia_mcp.log")
        entry["env"]["CATIA_MCP_LOG_LEVEL"] = "INFO"
    return entry


def load(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read().strip()
        return json.loads(text) if text else {}
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(
            "Could not read %s: %s\nFix or move that file, then run this again." % (path, exc)
        ) from exc


def backup(path: str) -> str | None:
    """Copy the existing config aside before touching it."""
    if not os.path.exists(path):
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    destination = "%s.backup-%s" % (path, stamp)
    shutil.copy2(path, destination)
    return destination


def apply(path: str, label: str, entry: dict, remove: bool, dry_run: bool) -> bool:
    config = load(path)
    servers = config.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}

    existing = servers.get(SERVER_KEY)
    if remove:
        if existing is None:
            print("  %-32s no %r entry to remove" % (label, SERVER_KEY))
            return False
        action = "remove"
    elif existing == entry:
        print("  %-32s already up to date" % label)
        return False
    else:
        action = "update" if existing is not None else "add"

    print("  %-32s %s %r" % (label, action, SERVER_KEY))
    print("      file: %s" % path)
    if not remove:
        print("      command: %s -m catia_mcp" % entry["command"])
        print("      PYTHONPATH: %s" % entry["env"]["PYTHONPATH"])
    if existing is not None and not remove:
        print("      replacing: %s" % json.dumps(existing))

    if dry_run:
        return True

    if remove:
        servers.pop(SERVER_KEY, None)
    else:
        servers[SERVER_KEY] = entry
    config["mcpServers"] = servers

    saved = backup(path)
    if saved:
        print("      backup: %s" % saved)

    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(config, handle, indent=2)
        handle.write("\n")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Register catia-mcp with an MCP client without the Claude CLI."
    )
    parser.add_argument(
        "--target",
        choices=[*TARGETS, "all"],
        default="desktop",
        help="Which client to configure. Default: desktop.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Show the change, write nothing.")
    parser.add_argument("--remove", action="store_true", help="Remove the entry instead.")
    parser.add_argument(
        "--no-log", action="store_true", help="Do not configure a log file for the server."
    )
    args = parser.parse_args()

    entry = server_entry(log=not args.no_log)
    chosen = list(TARGETS) if args.target == "all" else [args.target]

    print("Repository : %s" % REPO_ROOT)
    print("Interpreter: %s" % sys.executable)
    print()

    # Fail early on the mistake that produces a server which starts and then
    # cannot do anything: the wrong interpreter.
    missing = [
        module
        for module in ("mcp", "pydantic")
        if not _importable(module)
    ]
    if missing and not args.remove:
        print(
            "WARNING: this interpreter cannot import %s, so the server will not start.\n"
            "         Install the dependencies with:\n"
            '           "%s" -m pip install -e "%s[images]"\n'
            % (" or ".join(missing), sys.executable, REPO_ROOT)
        )

    changed = False
    for name in chosen:
        label, resolver = TARGETS[name]
        changed |= apply(resolver(), label, entry, args.remove, args.dry_run)

    print()
    if args.dry_run:
        print("Dry run: nothing was written. Re-run without --dry-run to apply.")
    elif changed:
        print("Done. Restart the client so it picks up the change.")
    else:
        print("Nothing to do.")
    return 0


def _importable(module: str) -> bool:
    import importlib.util

    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


if __name__ == "__main__":
    sys.exit(main())
