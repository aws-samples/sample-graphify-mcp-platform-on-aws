#!/usr/bin/env python3
"""Print the MCP client configuration for the deployed platform data plane.

Emits one mcpServers block containing:
  - "graphify-all"  — the hub service, whose default graph is the merged
                      all-repos graph (one query searches every repo).
  - one entry per registered repo's dedicated service (searches that repo only).

Every entry is plain streamable HTTP against the API-key data plane
(https://.../v1/mcp/<serverId>) — mint a key in the platform console (or via
POST /keys) and substitute it for <YOUR_API_KEY>.
"""

from __future__ import annotations

import argparse
import json

import boto3

from common import STACK_NAME, stack_outputs


def _entry(base_url: str, server_id: str) -> dict:
    return {
        "type": "http",
        "url": f"{base_url}/mcp/{server_id}",
        "headers": {"X-Graphify-Key": "<YOUR_API_KEY>"},
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--region", default="ap-northeast-2")
    ap.add_argument("--stack", default=STACK_NAME)
    ap.add_argument("--hub-only", action="store_true", help="emit only the hub entry")
    args = ap.parse_args()

    outputs = stack_outputs(args.region, args.stack)
    base_url = outputs["McpDataApiUrl"].rstrip("/")
    servers = {"graphify-all": _entry(base_url, "all")}

    if not args.hub_only:
        table = boto3.resource("dynamodb", region_name=args.region).Table(outputs["RepoRegistryTable"])
        items, kwargs = [], {}
        while True:
            page = table.scan(**kwargs)
            items += page.get("Items", [])
            if "LastEvaluatedKey" not in page:
                break
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
        for item in sorted(items, key=lambda x: x.get("repo_id", "")):
            # dedicated_runtime == "0" (--no-runtime) repos have no dedicated
            # service — they are reachable only through the graphify-all hub.
            if (
                item.get("enabled") == "1"
                and item.get("repo_id") != "__all__"
                and item.get("dedicated_runtime") != "0"
            ):
                servers[item["repo_id"]] = _entry(base_url, item["repo_id"])

    print(json.dumps({"mcpServers": servers}, indent=2))
    print("\n# Add to .mcp.json (Claude Code) or the equivalent MCP config of your client.")
    print("# Replace <YOUR_API_KEY> with a key minted in the platform console (X-Graphify-Key).")
    print("# graphify-all: one query searches EVERY repo (merged graph; node ids carry a per-repo tag).")
    print("# <repo_id> entries: scoped to that single repository.")


if __name__ == "__main__":
    main()
