"""End-to-end check that the server speaks MCP, without needing CATIA.

Spawns ``python -m catia_mcp`` over stdio, performs the initialise handshake,
lists the tools, and calls the two tools that work with no CATIA connection.
Run it after any change to the tool surface::

    python scripts/protocol_check.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROTOCOL_VERSION = "2025-06-18"


class StdioClient:
    def __init__(self, argv: list[str]):
        env = dict(os.environ, PYTHONPATH=ROOT, PYTHONIOENCODING="utf-8")
        self.process = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=ROOT,
            env=env,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        self._id = 0

    def send(self, method: str, params: dict | None = None, notify: bool = False):
        message: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        if not notify:
            self._id += 1
            message["id"] = self._id
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()
        if notify:
            return None
        return self._read(self._id)

    def _read(self, expect_id: int, timeout: float = 30.0):
        assert self.process.stdout is not None
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = self.process.stdout.readline()
            if not line:
                raise RuntimeError(
                    "Server closed stdout. stderr:\n%s" % self._drain_stderr()
                )
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                # Anything non-JSON on stdout would corrupt the transport.
                raise RuntimeError(
                    "Non-JSON on stdout, which breaks MCP: %r" % line[:200]
                ) from exc
            if payload.get("id") == expect_id:
                return payload
        raise TimeoutError("No response to request %d" % expect_id)

    def _drain_stderr(self) -> str:
        assert self.process.stderr is not None
        try:
            return self.process.stderr.read() or ""
        except Exception:
            return ""

    def close(self) -> None:
        try:
            if self.process.stdin:
                self.process.stdin.close()
            self.process.wait(timeout=10)
        except Exception:
            self.process.kill()


def content_text(payload: dict) -> str:
    parts = []
    for block in payload.get("result", {}).get("content", []):
        if block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "\n".join(parts)


def main() -> int:
    client = StdioClient([sys.executable, "-m", "catia_mcp"])
    failures: list[str] = []
    try:
        initialised = client.send(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "protocol-check", "version": "1.0"},
            },
        )
        info = initialised.get("result", {}).get("serverInfo", {})
        print("initialize   : %s %s" % (info.get("name"), info.get("version")))
        instructions = initialised.get("result", {}).get("instructions") or ""
        print("instructions : %d characters" % len(instructions))

        client.send("notifications/initialized", {}, notify=True)

        listed = client.send("tools/list", {})
        tools = listed.get("result", {}).get("tools", [])
        print("tools/list   : %d tools" % len(tools))
        if len(tools) < 100:
            failures.append("expected well over 100 tools, got %d" % len(tools))

        for tool in tools:
            schema = tool.get("inputSchema") or {}
            if schema.get("type") != "object":
                failures.append("%s has no object input schema" % tool.get("name"))
            if not tool.get("description"):
                failures.append("%s has no description" % tool.get("name"))

        by_name = {tool["name"]: tool for tool in tools}
        for expected in (
            "catia_connect",
            "catia_check_environment",
            "catia_reference_help",
            "catia_list_faces",
            "catia_fillet",
            "catia_screenshot",
        ):
            if expected not in by_name:
                failures.append("missing tool %s" % expected)

        # Spot-check that parameter descriptions survived into the schema.
        fillet = by_name.get("catia_fillet", {})
        properties = (fillet.get("inputSchema") or {}).get("properties", {})
        if "edges" not in properties or not properties.get("radius", {}).get("description"):
            failures.append("catia_fillet schema lost its parameter descriptions")

        called = client.send(
            "tools/call", {"name": "catia_check_environment", "arguments": {}}
        )
        text = content_text(called)
        report = json.loads(text) if text.strip().startswith("{") else {}
        print(
            "environment  : python %s (%s-bit), pywin32=%s, CATIA registered=%s"
            % (
                report.get("python"),
                report.get("python_bits"),
                report.get("pywin32_available"),
                report.get("catia_progid_registered"),
            )
        )
        if report.get("ok") is not True:
            failures.append("catia_check_environment did not return ok")

        called = client.send("tools/call", {"name": "catia_reference_help", "arguments": {}})
        help_text = content_text(called)
        if "face@" not in help_text:
            failures.append("catia_reference_help did not return the token grammar")
        print("reference    : grammar returned, %d characters" % len(help_text))

        # A failure must come back as structured data, not a protocol error.
        called = client.send("tools/call", {"name": "catia_status", "arguments": {}})
        status = json.loads(content_text(called))
        print("status       : connected=%s" % status.get("connected"))

        called = client.send(
            "tools/call", {"name": "catia_list_faces", "arguments": {}}
        )
        text = content_text(called)
        payload = json.loads(text) if text.strip().startswith("{") else {}
        if payload.get("ok") is not False or "error" not in payload:
            failures.append(
                "calling a CATIA tool with no CATIA should return a structured error, got: %s"
                % text[:200]
            )
        else:
            print(
                "no-CATIA path: error code %r, remediation present=%s"
                % (
                    payload["error"]["code"],
                    bool(payload["error"].get("remediation")),
                )
            )
    finally:
        client.close()

    print()
    if failures:
        print("FAILED:")
        for failure in failures:
            print("  - %s" % failure)
        return 1
    print("All protocol checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
