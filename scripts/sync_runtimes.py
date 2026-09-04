#!/usr/bin/env python3
"""Create or roll forward the dedicated per-repo MCP Fargate services.

Idempotent repair/migration tool: for every enabled repo in the registry it
ensures a dedicated service exists and runs the CURRENT image/config (useful
after `cdk deploy` changed the entrypoint/image), then records the service
identifiers back onto the registry item (runtime_* attribute names are kept
for console compatibility).

Usage:  uv run python scripts/sync_runtimes.py [--region ap-northeast-2]
"""

from __future__ import annotations

import argparse
import sys

import boto3

from common import STACK_NAME, ensure_repo_runtime, stack_outputs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--region", default="ap-northeast-2")
    ap.add_argument("--stack", default=STACK_NAME)
    args = ap.parse_args()

    outputs = stack_outputs(args.region, args.stack)
    table = boto3.resource("dynamodb", region_name=args.region).Table(outputs["RepoRegistryTable"])

    items, kwargs = [], {}
    while True:
        page = table.scan(**kwargs)
        items += page.get("Items", [])
        if "LastEvaluatedKey" not in page:
            break
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    failures = 0
    for item in sorted(items, key=lambda x: x.get("repo_id", "")):
        repo_id = item["repo_id"]
        if item.get("enabled") != "1":
            print(f"skip {repo_id} (disabled)")
            continue
        if item.get("dedicated_runtime") == "0":
            print(f"skip {repo_id} (registered with --no-runtime)")
            continue
        try:
            rt = ensure_repo_runtime(
                repo_id, outputs, args.region, wait=True,
                cpu=int(item.get("service_cpu", 0) or 512),
                memory=int(item.get("service_memory", 0) or 2048),
            )
        except Exception as exc:
            # One repo's failure must not strand the rest on a stale entrypoint.
            print(f"{repo_id}: FAILED ({exc})")
            failures += 1
            continue
        table.update_item(
            Key={"repo_id": repo_id},
            UpdateExpression="SET runtime_name = :n, runtime_arn = :a, runtime_id = :i",
            ExpressionAttributeValues={":n": rt["runtime_name"], ":a": rt["runtime_arn"], ":i": rt["runtime_id"]},
        )
        print(f"{repo_id}: {rt['runtime_id']} [{rt['status']}]")
        if rt["status"] != "READY":
            failures += 1
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
