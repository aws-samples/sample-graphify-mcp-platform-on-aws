#!/usr/bin/env python3
"""End-to-end smoke test against the deployed MCP data plane.

Sends raw MCP JSON-RPC through the API-key data plane
(https://.../v1/mcp/<serverId> -> proxy Lambda -> Fargate MCP service):
tools/list, then graph_stats / query_graph / god_nodes tool calls. Needs a
platform API key (mint one in the console or via POST /keys) supplied with
--api-key or the GRAPHIFY_API_KEY env var.

Usage:
  GRAPHIFY_API_KEY=gfy_... uv run python scripts/smoke_test.py            # hub ("all")
  GRAPHIFY_API_KEY=gfy_... uv run python scripts/smoke_test.py --repo-id github__psf__requests__main
  ... --question "how does session retry work" --node Session
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

from common import STACK_NAME, stack_outputs


def invoke(base_url: str, server_id: str, api_key: str, payload: dict) -> dict:
    req = urllib.request.Request(
        f"{base_url}/mcp/{server_id}",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "X-Graphify-Key": api_key,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {body[:500]}") from None
    if body.lstrip().startswith("{"):
        return json.loads(body)
    for line in body.splitlines():  # tolerate SSE framing
        if line.startswith("data:"):
            return json.loads(line[5:].strip())
    raise RuntimeError(f"unparseable response: {body[:500]!r}")


def tool_text(result: dict) -> str:
    content = result.get("result", {}).get("content", [])
    return "\n".join(c.get("text", "") for c in content if c.get("type") == "text")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--region", default="ap-northeast-2")
    ap.add_argument("--stack", default=STACK_NAME)
    ap.add_argument("--repo-id", default="", help="target this repo's dedicated service (default: the hub, serverId 'all')")
    ap.add_argument("--api-key", default=os.environ.get("GRAPHIFY_API_KEY", ""), help="platform API key (or set GRAPHIFY_API_KEY)")
    ap.add_argument("--question", default="what are the main entry points", help="query_graph question")
    ap.add_argument("--node", default="", help="optional label for a get_node call")
    args = ap.parse_args()

    if not args.api_key:
        ap.error("an API key is required: pass --api-key or set GRAPHIFY_API_KEY (mint one in the console)")

    outputs = stack_outputs(args.region, args.stack)
    base_url = outputs["McpDataApiUrl"].rstrip("/")
    server_id = args.repo_id or "all"

    print(f"endpoint: {base_url}/mcp/{server_id}\nscope   : {args.repo_id or 'hub default (__all__ merged graph)'}\n")

    r = invoke(base_url, server_id, args.api_key, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    tools = [t["name"] for t in r.get("result", {}).get("tools", [])]
    print(f"tools/list -> {len(tools)} tools: {tools}\n")
    if "query_graph" not in tools:
        print("FAIL: query_graph missing from tools/list", file=sys.stderr)
        return 1

    r = invoke(base_url, server_id, args.api_key, {
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "graph_stats", "arguments": {}},
    })
    print(f"graph_stats ->\n{tool_text(r)}\n")

    r = invoke(base_url, server_id, args.api_key, {
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "query_graph", "arguments": {"question": args.question, "token_budget": 800}},
    })
    text = tool_text(r)
    print(f"query_graph({args.question!r}) ->\n{text[:1200]}\n")

    r = invoke(base_url, server_id, args.api_key, {
        "jsonrpc": "2.0", "id": 4, "method": "tools/call",
        "params": {"name": "god_nodes", "arguments": {"top_n": 5}},
    })
    print(f"god_nodes ->\n{tool_text(r)}\n")

    if args.node:
        r = invoke(base_url, server_id, args.api_key, {
            "jsonrpc": "2.0", "id": 5, "method": "tools/call",
            "params": {"name": "get_node", "arguments": {"label": args.node}},
        })
        print(f"get_node({args.node!r}) ->\n{tool_text(r)}\n")

    print("SMOKE TEST PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
