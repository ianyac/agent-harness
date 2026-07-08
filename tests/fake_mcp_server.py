"""A minimal MCP server: newline-delimited JSON-RPC 2.0 over stdio.

Run as a real child process by the tests (and the lesson's live smoke),
so the client exercises real pipes, framing, timeouts, and process
death — not an in-process double.
"""

import json
import sys
import time

TOOLS = [
    {
        "name": "echo",
        "description": "Echo the text back.",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "shout",
        "description": "Uppercase the text.",
        "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}},
        # no annotations: the bridge must default read_only to False
    },
    {
        "name": "ping",
        "description": "Answer pong.",
        "inputSchema": {"type": "object"},  # legal: properties omitted
    },
    {
        "name": "slow",
        "description": "Sleep, then answer.",
        "inputSchema": {
            "type": "object",
            "properties": {"seconds": {"type": "number"}},
        },
    },
    {
        "name": "crash",
        "description": "Exit without replying.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "fail",
        "description": "Return an isError result.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "huge",
        "description": "Return a very long text.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def text_result(text, is_error=False):
    result: dict = {"content": [{"type": "text", "text": text}]}
    if is_error:
        result["isError"] = True
    return result


def handle_call(name, arguments):
    if name == "echo":
        return text_result(arguments.get("text", ""))
    if name == "shout":
        return text_result(arguments.get("text", "").upper())
    if name == "ping":
        return text_result("pong")
    if name == "slow":
        time.sleep(arguments.get("seconds", 5))
        return text_result("finally")
    if name == "crash":
        sys.exit(1)
    if name == "fail":
        return text_result("deliberate failure", is_error=True)
    if name == "huge":
        return text_result("x" * 20_000)
    return None  # unknown tool -> JSON-RPC error


def reply(msg_id, result=None, error=None):
    body = {"jsonrpc": "2.0", "id": msg_id}
    if error is not None:
        body["error"] = error
    else:
        body["result"] = result
    print(json.dumps(body), flush=True)


def main():
    for line in sys.stdin:
        msg = json.loads(line)
        method, msg_id = msg.get("method"), msg.get("id")
        if msg_id is None:
            continue  # a notification (e.g. notifications/initialized)
        if method == "initialize":
            reply(
                msg_id,
                {
                    "protocolVersion": msg["params"]["protocolVersion"],
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "fake", "version": "0"},
                },
            )
        elif method == "tools/list":
            reply(msg_id, {"tools": TOOLS})
        elif method == "tools/call":
            name = msg["params"]["name"]
            result = handle_call(name, msg["params"].get("arguments", {}))
            if result is None:
                reply(msg_id, error={"code": -32602, "message": f"unknown tool: {name}"})
            else:
                reply(msg_id, result)
        else:
            reply(msg_id, error={"code": -32601, "message": f"unknown method: {method}"})


if __name__ == "__main__":
    main()
