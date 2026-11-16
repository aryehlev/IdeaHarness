#!/usr/bin/env python3
"""Minimal stdio MCP server exposing IdeaHarness's read-only research tools.

Any MCP-capable harness can use it:  ideaharness mcp
"""

from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from ideaharness import __version__  # noqa: E402
from ideaharness.tools import TOOLS, call_tool  # noqa: E402

INSTRUCTIONS = (
    "Read-only research tools for judging project ideas: GitHub repository and issue search with "
    "real star counts, Hacker News search, npm/crates/PyPI lookups, web search and page fetch."
)


def send(message: dict) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def handle(msg: dict) -> None:
    method, mid = msg.get("method"), msg.get("id")
    if mid is None:  # notifications (initialized, cancelled, ...) need no reply
        return
    params = msg.get("params") or {}
    if method == "initialize":
        result = {
            "protocolVersion": params.get("protocolVersion") or "2025-06-18",
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "ideaharness-research", "version": __version__},
            "instructions": INSTRUCTIONS,
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": [
            {
                "name": t.name,
                "description": t.description,
                "inputSchema": t.parameters,
                "annotations": {"readOnlyHint": True, "openWorldHint": True},
            }
            for t in TOOLS
        ]}
    elif method == "tools/call":
        text, is_error = call_tool(params.get("name", ""), params.get("arguments") or {})
        result = {"content": [{"type": "text", "text": text}], "isError": is_error}
    elif method == "resources/list":
        result = {"resources": []}
    elif method == "prompts/list":
        result = {"prompts": []}
    else:
        send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}})
        return
    send({"jsonrpc": "2.0", "id": mid, "result": result})


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
            continue
        for item in msg if isinstance(msg, list) else [msg]:
            if isinstance(item, dict):
                try:
                    handle(item)
                except Exception as exc:  # keep the server alive for the rest of the session
                    send({"jsonrpc": "2.0", "id": item.get("id"), "error": {"code": -32603, "message": str(exc)}})


if __name__ == "__main__":
    main()
