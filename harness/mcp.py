"""MCP client: foreign tools over stdio, behind the registry seam.

An MCP server is a separate process exposing tools over JSON-RPC 2.0,
newline-delimited on stdin/stdout. This module speaks just enough of the
protocol to use those tools — initialize handshake, tools/list,
tools/call — and bridges each discovered tool into an ordinary registry
Tool. Downstream nothing knows the difference: permissions gate it,
hooks wrap it, subagents inherit it, and the loop stays tool-name-free.

Error discipline splits by phase. Startup (start, list_tools, mcp_tools)
raises MCPError — a misbehaving server degrades at launch: reported,
closed, the session continues without it. Call time (call) returns error
strings, lesson 8 style: a dead or hung server is information for the
model, not a harness crash. Everything a server sends is untrusted
input, so shapes are validated before use — a malformed reply must land
in one of those two channels, never escape as a stray traceback.
"""

import json
import os
import re
import select
import signal
import subprocess
import tempfile
import time

from harness.tools.base import Tool
from harness.truncate import truncate

PROTOCOL_VERSION = "2025-03-26"
# one reply line larger than this marks a broken server: results get
# truncated to a few KB anyway, and an unbounded read buffer would let
# a single bad server OOM the whole harness
MAX_REPLY_BYTES = 10_000_000


class MCPError(Exception):
    pass


def load_config(path) -> dict[str, str]:
    """Parse mcp.json into {server name: command}.

    Missing file = no servers. Anything else malformed is a ValueError —
    the hooks.json rule: config mistakes die at startup, never
    mid-conversation, and a typo'd key must not silently disable a server.
    """
    try:
        raw = path.read_text()
    except FileNotFoundError:
        return {}
    except OSError as error:
        raise ValueError(f"cannot read {path}: {error}")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError(f"invalid JSON in {path}: {error}")
    if not isinstance(data, dict) or set(data) - {"servers"}:
        raise ValueError(f'{path}: expected {{"servers": {{name: {{"command": ...}}}}}}')
    servers = data.get("servers", {})
    if not isinstance(servers, dict):
        raise ValueError(f'{path}: "servers" must be an object')
    commands = {}
    for name, spec in servers.items():
        # names prefix tool names: banning "__" keeps <server>__<tool>
        # unambiguous, so a call can never route to the wrong server
        if not re.fullmatch(r"[A-Za-z0-9_-]+", name) or "__" in name:
            raise ValueError(
                f"{path}: server name {name!r} must use letters, digits, "
                "'-' or '_' (no '__') — it becomes a tool-name prefix"
            )
        if not isinstance(spec, dict) or set(spec) - {"command"}:
            raise ValueError(f'{path}: server {name!r} must be {{"command": "..."}}')
        command = spec.get("command")
        if not isinstance(command, str) or not command.strip():
            raise ValueError(f"{path}: server {name!r} needs a non-empty command string")
        commands[name] = command
    return commands


class MCPServer:
    """One stdio MCP server: a child process we speak JSON-RPC to."""

    def __init__(self, name: str, command: str, timeout: float = 10.0, cwd: str | None = None):
        self.name = name
        self.command = command
        self.timeout = timeout  # per request, seconds
        self.cwd = cwd  # commands resolve here (main passes the workspace)
        self._proc: subprocess.Popen | None = None
        self._stderr = None  # temp file: the server's log, kept for diagnostics
        self._buf = b""
        self._next_id = 0

    def start(self) -> None:
        """Spawn the server and run the initialize handshake."""
        self._stderr = tempfile.TemporaryFile()
        self._proc = subprocess.Popen(
            self.command,
            shell=True,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            # captured, not shown: a chatty server can't garble the REPL's
            # streamed output, but the tail is appended to death reports
            stderr=self._stderr,
            cwd=self.cwd,
            start_new_session=True,  # own process group: close() can killpg
        )
        result = self._request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "agent-harness", "version": "0"},
            },
        )
        version = result.get("protocolVersion")
        if version != PROTOCOL_VERSION:
            # mismatched semantics would surface later as garbled results;
            # incompatibility must be a clean startup-phase error
            raise MCPError(
                f"MCP server {self.name!r} speaks protocol {version!r}; "
                f"this client speaks {PROTOCOL_VERSION!r}"
            )
        self._notify("notifications/initialized")

    def list_tools(self) -> list[dict]:
        """Discover the server's tools (raises MCPError — startup phase)."""
        specs: list[dict] = []
        cursor = None
        for _ in range(100):  # a cursor loop must terminate
            params = {"cursor": cursor} if cursor is not None else {}
            result = self._request("tools/list", params)
            page = result.get("tools") or []
            if not isinstance(page, list):
                raise MCPError(f"MCP server {self.name!r} sent a non-list tools page")
            specs.extend(page)
            cursor = result.get("nextCursor")
            if not cursor:
                return specs
        raise MCPError(f"MCP server {self.name!r} never finished paginating tools/list")

    def call(self, tool: str, arguments: dict) -> str:
        """Invoke a tool; failures come back as strings, never exceptions."""
        try:
            result = self._request("tools/call", {"name": tool, "arguments": arguments})
        except MCPError as e:
            return f"Error: {e}"
        blocks = result.get("content") or []
        if not isinstance(blocks, list):
            blocks = [blocks]
        parts = []
        for block in blocks:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text") or "")
            else:
                kind = block.get("type", "unknown") if isinstance(block, dict) else "malformed"
                parts.append(f"[{kind} content omitted]")
        text = "\n".join(parts)
        # isError marks a tool-level failure inside a successful RPC
        return f"Error: {text}" if result.get("isError") else text

    def close(self) -> None:
        if self._stderr is not None:
            try:
                self._stderr.close()
            except OSError:
                pass
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
        # one deadline covers the whole round trip, the send included
        deadline = time.monotonic() + self.timeout
        self._send(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
            deadline,
        )
        return self._read_response(request_id, deadline)

    def _notify(self, method: str) -> None:
        # no id: no reply expected
        self._send({"jsonrpc": "2.0", "method": method}, time.monotonic() + self.timeout)

    def _send(self, payload: dict, deadline: float) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise MCPError(f"MCP server {self.name!r} was never started")
        try:
            fd = self._proc.stdin.fileno()
        except ValueError:  # pipe closed by close()
            raise MCPError(f"MCP server {self.name!r} is not running")
        # non-blocking writes under the request deadline: a wedged server
        # that stops draining stdin must not hang the harness on a large
        # payload — the read side's timeout would never even start
        os.set_blocking(fd, False)
        data = json.dumps(payload).encode() + b"\n"
        total = len(data)
        while data:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if len(data) < total:
                    # a half-written request corrupts the line framing;
                    # the channel is unrecoverable, so retire the server
                    self.close()
                raise MCPError(f"MCP server {self.name!r} timed out after {self.timeout}s")
            _, ready, _ = select.select([], [fd], [], remaining)
            if not ready:
                continue  # loop re-checks the deadline
            try:
                written = os.write(fd, data)
            except BlockingIOError:
                continue
            except OSError:
                raise MCPError(f"MCP server {self.name!r} is not running")
            data = data[written:]

    def _read_response(self, request_id: int, deadline: float) -> dict:
        # match replies by id, and only replies: servers interleave
        # notifications and requests of their own (whose ids live in a
        # separate namespace and may collide), and a timed-out call's
        # reply can arrive late — all must be skipped, not misread
        while True:
            line = self._read_line(deadline)
            try:
                batch = json.loads(line)
            except ValueError:  # JSONDecodeError, or UnicodeDecodeError on binary
                raise MCPError(
                    f"malformed reply from MCP server {self.name!r}: {line[:200]!r}"
                )
            # a JSON-RPC batch is an array of messages; scan it like a line
            for msg in batch if isinstance(batch, list) else [batch]:
                if not isinstance(msg, dict) or "method" in msg:
                    continue  # a notification or server-initiated request
                if msg.get("id") != request_id:
                    continue  # stale reply from a timed-out call
                if "error" in msg:
                    err = msg["error"]
                    detail = err.get("message", err) if isinstance(err, dict) else err
                    raise MCPError(f"MCP server {self.name!r} error: {detail}")
                result = msg.get("result")
                if not isinstance(result, dict):
                    raise MCPError(
                        f"MCP server {self.name!r} sent a non-object result: {result!r}"
                    )
                return result

    def _read_line(self, deadline: float) -> bytes:
        if self._proc is None or self._proc.stdout is None:
            raise MCPError(f"MCP server {self.name!r} was never started")
        try:
            fd = self._proc.stdout.fileno()
        except ValueError:
            raise MCPError(f"MCP server {self.name!r} is not running")
        # our own line buffer over os.read: select() sees the raw fd, so a
        # buffered file object can't hide bytes from the readiness check
        while b"\n" not in self._buf:
            if len(self._buf) > MAX_REPLY_BYTES:
                self.close()  # mid-line: the framing is unrecoverable
                raise MCPError(
                    f"MCP server {self.name!r} sent a reply over {MAX_REPLY_BYTES} bytes"
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MCPError(
                    f"MCP server {self.name!r} timed out after {self.timeout}s"
                )
            ready, _, _ = select.select([fd], [], [], remaining)
            if not ready:
                raise MCPError(
                    f"MCP server {self.name!r} timed out after {self.timeout}s"
                )
            chunk = os.read(fd, 65536)
            if not chunk:
                raise self._died()
            self._buf += chunk
        line, self._buf = self._buf.split(b"\n", 1)
        return line

    def _died(self) -> MCPError:
        # the server's stderr is the only diagnostic it leaves behind;
        # report its tail instead of a bare "exited unexpectedly"
        tail = b""
        if self._stderr is not None:
            try:
                self._stderr.seek(0, os.SEEK_END)
                size = self._stderr.tell()
                self._stderr.seek(max(0, size - 500))
                tail = self._stderr.read().strip()
            except (OSError, ValueError):
                pass
        detail = f": {tail.decode(errors='replace')}" if tail else ""
        return MCPError(f"MCP server {self.name!r} exited unexpectedly{detail}")


def _executor(server: MCPServer, tool_name: str, output_limit: int):
    def execute(**args) -> str:
        return truncate(server.call(tool_name, args), output_limit)

    return execute


def mcp_tools(server: MCPServer, output_limit: int = 8000) -> list[Tool]:
    """Bridge a server's discovered tools into registry Tools.

    Names are namespaced `<server>__<tool>` so foreign tools can't collide
    with builtins or across servers. read_only honors the server's declared
    readOnlyHint when it is literally True and defaults to False — a
    foreign tool with unknown side effects faces the permission gate, it
    doesn't bypass it.
    """
    tools = []
    for spec in server.list_tools():
        try:
            tool_name = spec["name"]
            schema = dict(spec.get("inputSchema") or {})
            schema.setdefault("type", "object")
            schema.setdefault("properties", {})  # legal MCP schemas may omit it
            annotations = spec.get("annotations") or {}
            tool = Tool(
                name=f"{server.name}__{tool_name}",
                description=spec.get("description") or "",
                parameters=schema,
                execute=_executor(server, tool_name, output_limit),
                # literal True only: a sloppy truthy value ("false", 1)
                # must not skip the permission gate
                read_only=annotations.get("readOnlyHint") is True,
            )
        except (AttributeError, KeyError, TypeError, ValueError) as error:
            # a bad spec is the server's fault, not a harness crash: keep
            # it inside the MCPError discipline so startup degrades
            raise MCPError(f"MCP server {server.name!r} sent a bad tool spec: {error!r}")
        tools.append(tool)
    return tools
