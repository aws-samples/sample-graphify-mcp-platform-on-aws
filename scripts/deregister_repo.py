#!/usr/bin/env python3
"""Deregister a repo: delete its dedicated MCP runtime, disable polling, and
optionally purge its graphs from S3.

Usage:
  uv run python scripts/deregister_repo.py --repo-id github__psf__requests__main
  uv run python scripts/deregister_repo.py --repo-id ... --purge   # also delete S3 graphs + registry item
"""

from __future__ import annotations

import argparse
import sys
import time

import boto3

from common import STACK_NAME, delete_repo_runtime, stack_outputs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo-id", required=True)
    ap.add_argument("--purge", action="store_true", help="also delete S3 graphs and the registry item (default: keep data, disable polling)")
    ap.add_argument("--region", default="ap-northeast-2")
    ap.add_argument("--stack", default=STACK_NAME)
    args = ap.parse_args()

    outputs = stack_outputs(args.region, args.stack)
    table = boto3.resource("dynamodb", region_name=args.region).Table(outputs["RepoRegistryTable"])
    item = table.get_item(Key={"repo_id": args.repo_id}).get("Item")
    if not item:
        print(f"not registered: {args.repo_id}", file=sys.stderr)
        return 1

    # Operator override: this force-disables regardless of platform ref-counting.
    # If console users are subscribed, warn — their grants (in the platform
    # table) will be orphaned and their MCP access dies immediately.
    subs = int(item.get("subscriber_count", 0) or 0)
    if subs > 0:
        print(f"WARNING: {subs} platform subscriber(s) hold a grant on this repo; "
              "force-deregistering revokes their MCP access and leaves orphaned grant rows. "
              "Prefer the console 'Leave' flow (ref-counted teardown) unless this is an operator override.")

    # Disable FIRST: if the runtime delete fails midway, the poller must not
    # keep rebuilding a repo whose runtime is half-torn-down.
    table.update_item(
        Key={"repo_id": args.repo_id},
        UpdateExpression="SET enabled = :off, updated_at = :iso",
        ExpressionAttributeValues={":off": "0", ":iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
    )
    print("polling disabled")
    try:
        if delete_repo_runtime(args.repo_id, args.region):
            print("dedicated runtime deleted")
        else:
            print("no dedicated runtime found")
    except Exception as exc:
        print(f"WARNING: runtime delete failed ({exc}); re-run this script to retry")

    print("note: repos/__all__ still contains this repo until the next build of any repo refreshes the merge")
    if args.purge:
        s3 = boto3.resource("s3", region_name=args.region)
        bucket = s3.Bucket(outputs["GraphBucketName"])
        # uploads/ only exists for files-source repos; deleting an empty prefix is a no-op.
        for prefix in (f"repos/{args.repo_id}/", f"history/{args.repo_id}/", f"uploads/{args.repo_id}/"):
            deleted = bucket.objects.filter(Prefix=prefix).delete()
            print(f"purged s3://{outputs['GraphBucketName']}/{prefix} ({sum(len(d.get('Deleted', [])) for d in deleted)} objects)")
        table.delete_item(Key={"repo_id": args.repo_id})
        print("registry item deleted")
    else:
        table.update_item(
            Key={"repo_id": args.repo_id},
            UpdateExpression="SET updated_at = :iso REMOVE runtime_name, runtime_arn, runtime_id",
            ExpressionAttributeValues={":iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
        )
        print("graphs retained (use --purge to delete data); the merged __all__ drops this repo on the next build")
    return 0


if __name__ == "__main__":
    sys.exit(main())
