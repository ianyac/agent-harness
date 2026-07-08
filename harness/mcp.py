"""MCP client: foreign tools over stdio, behind the registry seam.

An MCP server is a separate process exposing tools over JSON-RPC 2.0,
newline-delimited on stdin/stdout. This module speaks just enough of the
protocol to use those tools — initialize handshake, tools/list,
tools/call — and bridges each discovered tool into an ordinary registry
Tool. Downstream nothing knows the difference: permissions gate it,
hooks wrap it, subagents inherit it, and the loop stays tool-name-free.

Error discipline splits by phase. Startup (start, list_tools) raises
MCPError — a misconfigured server should die at launch like a malformed
hooks.json, never mid-conversation. Call time (call) returns error
strings, lesson 8 style: a dead or hung server is information for the
model, not a harness crash.
"""

import json
import os
import select
import signal
import subprocess
import time

from harness.tools.base import Tool
from harness.truncate import truncate

PROTOCOL_VERSION = "2025-03-26"


class MCPError(Exception):
    pass


class MCPServer:
    """One stdio MCP server: a child process we speak JSON-RPC to."""

    def __init__(self, name: str, command: str, timeout: float = 10.0):
        self.name = name
        self.command = command
        self.timeout = timeout  # per request, seconds
        self._proc: subprocess.Popen | None = None
        self._buf = b""
        self._next_id = 0

    def start(self) -> None:
        """Spawn the server and run the initialize handshake."""
        self._proc = subprocess.Popen(
            self.command,
            shell=True,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            # stderr is the server's log channel (MCP convention); we drop
            # it so a chatty server can't garble the REPL's streamed output
            stderr=subprocess.DEVNULL,
            start_new_session=True,  # own process group: close() can killpg
        )
        self._request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "agent-harness", "version": "0"},
            },
        )
        self._notify("notifications/initialized")

    def list_tools(self) -> list[dict]:
        """Discover the server's tools (raises MCPError — startup phase)."""
        return self._request("tools/list", {}).get("tools", [])

    def call(self, tool: str, arguments: dict) -> str:
        """Invoke a tool; failures come back as strings, never exceptions."""
        try:
            result = self._request("tools/call", {"name": tool, "arguments": arguments})
        except MCPError as e:
            return f"Error: {e}"
        parts = []
        for block in result.get("content", []):
            if block.get("type") == "text":
                parts.append(block.get("text", ""))
            else:
                parts.append(f"[{block.get('type', 'unknown')} content omitted]")
        text = "\n".join(parts)
        # isError marks a tool-level failure inside a successful RPC
        return f"Error: {text}" if result.get("isError") else text

    def close(self) -> None:
        if self._proc is None:
            return
        if self._proc.poll() is None:
            try:
                os.killpg(self._proc.pid, signal.SIGTERM)
                self._proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                os.killpg(self._proc.pid, signal.SIGKILL)
                self._proc.wait()
            except ProcessLookupError:
                pass
        # close the pipes ourselves: a write that died on EPIPE can leave
        # bytes in the stdin buffer, and GC's flush-on-close would raise
        for pipe in (self._proc.stdin, self._proc.stdout):
            if pipe is not None:
                try:
                    pipe.close()
                except OSError:
                    pass

    # --- JSON-RPC plumbing -------------------------------------------------

    def _request(self, method: str, params: dict) -> dict:
        self._next_id += 1
        request_id = self._next_id
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        return self._read_response(request_id, time.monotonic() + self.timeout)

    def _notify(self, method: str) -> None:
        self._send({"jsonrpc": "2.0", "method": method})  # no id: no reply

    def _send(self, payload: dict) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise MCPError(f"MCP server {self.name!r} was never started")
        line = json.dumps(payload) + "\n"
        try:
            self._proc.stdin.write(line.encode())
            self._proc.stdin.flush()
        except (OSError, ValueError):  # dead pipe / closed file
            raise MCPError(f"MCP server {self.name!r} is not running")

    def _read_response(self, request_id: int, deadline: float) -> dict:
        # match replies by id: a server may interleave notifications, and a
        # timed-out call's reply can arrive late — both must be skipped, not
        # misread as the answer to the current request
        while True:
            line = self._read_line(deadline)
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                raise MCPError(
                    f"malformed reply from MCP server {self.name!r}: {line[:200]!r}"
                )
            if not isinstance(msg, dict) or msg.get("id") != request_id:
                continue
            if "error" in msg:
                err = msg["error"]
                detail = err.get("message", err) if isinstance(err, dict) else err
                raise MCPError(f"MCP server {self.name!r} error: {detail}")
            return msg.get("result") or {}

    def _read_line(self, deadline: float) -> bytes:
        if self._proc is None or self._proc.stdout is None:
            raise MCPError(f"MCP server {self.name!r} was never started")
        stdout = self._proc.stdout
        # our own line buffer over os.read: select() sees the raw fd, so a
        # buffered file object can't hide bytes from the readiness check
        while b"\n" not in self._buf:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MCPError(
                    f"MCP server {self.name!r} timed out after {self.timeout}s"
                )
            ready, _, _ = select.select([stdout], [], [], remaining)
            if not ready:
                raise MCPError(
                    f"MCP server {self.name!r} timed out after {self.timeout}s"
                )
            chunk = os.read(stdout.fileno(), 65536)
            if not chunk:
                raise MCPError(f"MCP server {self.name!r} exited unexpectedly")
            self._buf += chunk
        line, self._buf = self._buf.split(b"\n", 1)
        return line


def _executor(server: MCPServer, tool_name: str, output_limit: int):
    def execute(**args) -> str:
        return truncate(server.call(tool_name, args), output_limit)

    return execute


def mcp_tools(server: MCPServer, output_limit: int = 8000) -> list[Tool]:
    """Bridge a server's discovered tools into registry Tools.

    Names are namespaced `<server>__<tool>` so foreign tools can't collide
    with builtins or each other. read_only honors the server's declared
    readOnlyHint when present and defaults to False — a foreign tool with
    unknown side effects faces the permission gate, it doesn't bypass it.
    """
    tools = []
    for spec in server.list_tools():
        schema = dict(spec.get("inputSchema") or {})
        schema.setdefault("type", "object")
        schema.setdefault("properties", {})  # legal MCP schemas may omit it
        annotations = spec.get("annotations") or {}
        tools.append(
            Tool(
                name=f"{server.name}__{spec['name']}",
                description=spec.get("description", ""),
                parameters=schema,
                execute=_executor(server, spec["name"], output_limit),
                read_only=annotations.get("readOnlyHint", False),
            )
        )
    return tools
