#!/usr/bin/env python3
"""Register a public docs-site URL (source_type=url) for graph building.

The build plane crawls the URL — sitemap.xml first, else link-following —
within the same host + path prefix, converts pages to markdown, and builds
the graph. The poller re-crawls every --poll-interval; a crawl whose content
hash matches the previously published one skips the rebuild.

Usage:
  uv run python scripts/register_url_repo.py --url https://hatch.pypa.io/1.18/ --max-pages 50
"""

from __future__ import annotations

import argparse
import re
import sys
import time

import boto3

from common import (
    STACK_NAME,
    ensure_repo_runtime,
    make_url_repo_id,
    stack_outputs,
    validate_task_size,
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True, help="public https docs URL; crawl scope = same host + path prefix")
    ap.add_argument("--max-pages", type=int, default=200, help="crawl page cap (1..500, default 200)")
    ap.add_argument("--poll-interval", type=int, default=21600, help="seconds between re-crawls (default 21600; every poll runs a crawl-build)")
    ap.add_argument("--prune-paths", default="", help="space-separated corpus-relative dirs to drop before extraction")
    ap.add_argument("--force", action="store_true", help="overwrite an existing registration")
    ap.add_argument("--no-runtime", action="store_true", help="skip the dedicated per-repo MCP service (hub-only serving)")
    ap.add_argument("--no-build", action="store_true", help="register only; let the poller start the first crawl")
    ap.add_argument("--service-cpu", type=int, default=512)
    ap.add_argument("--service-memory", type=int, default=2048)
    ap.add_argument("--region", default="ap-northeast-2")
    ap.add_argument("--stack", default=STACK_NAME)
    args = ap.parse_args()

    url = args.url.strip().rstrip("/")
    if not re.match(r"^https://[^\s/]+\.[^\s/]+(/[^\s]*)?$", url):
        ap.error("--url must be a public https URL")
    if not 1 <= args.max_pages <= 500:
        ap.error("--max-pages must be 1..500")
    if args.poll_interval < 900:
        ap.error("--poll-interval must be >= 900 seconds (each poll runs a crawl-build)")
    for p in args.prune_paths.split():
        if p.startswith("/") or ".." in p.split("/"):
            ap.error(f"invalid prune path: {p!r}")
    try:
        validate_task_size(args.service_cpu, args.service_memory)
    except ValueError as exc:
        ap.error(str(exc))

    outputs = stack_outputs(args.region, args.stack)
    bucket = outputs["GraphBucketName"]
    repo_id = make_url_repo_id(url)

    now = int(time.time())
    iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
    item = {
        "repo_id": repo_id,
        "git_url": url,
        "provider": "url",
        "ref": "",
        "source_type": "url",
        "crawl_max_pages": args.max_pages,
        "enabled": "1",
        "graph_scope": "public",
        "subscriber_count": 1,
        "trigger": "poll",
        "dedicated_runtime": "0" if args.no_runtime else "1",
        "poll_interval_seconds": args.poll_interval,
        "next_poll_at": now + args.poll_interval,
        "status": "REGISTERED",
        "service_cpu": args.service_cpu,
        "service_memory": args.service_memory,
        "created_at": iso,
        "updated_at": iso,
    }
    if args.prune_paths.strip():
        item["prune_paths"] = " ".join(sorted(set(args.prune_paths.split())))[:1000]

    table = boto3.resource("dynamodb", region_name=args.region).Table(outputs["RepoRegistryTable"])
    if args.force:
        existing = table.get_item(Key={"repo_id": repo_id}).get("Item")
        if not existing:
            table.put_item(Item=item)
        else:
            # Keep platform-owned counters/runtime/build state (a full put
            # would drop runtime_arn -> 404s and reset the subscriber count,
            # tearing a pooled row out from under console subscribers).
            # Mirrors register_repo.py's --force semantics.
            preserve = {"repo_id", "subscriber_count", "created_by_sub",
                        "runtime_name", "runtime_arn", "runtime_id",
                        "last_built_sha", "last_built_at", "build_id", "build_arn"}
            sets = {k: v for k, v in item.items() if k not in preserve}
            optional = {"prune_paths"}
            removes = [k for k in optional if k not in item and k in existing]
            names = {f"#k{i}": k for i, k in enumerate(sets)}
            values = {f":v{i}": v for i, (k, v) in enumerate(sets.items())}
            expr = "SET " + ", ".join(f"#k{i} = :v{i}" for i in range(len(sets)))
            for j, k in enumerate(removes):
                names[f"#r{j}"] = k
            if removes:
                expr += " REMOVE " + ", ".join(f"#r{j}" for j in range(len(removes)))
            table.update_item(Key={"repo_id": repo_id}, UpdateExpression=expr,
                              ExpressionAttributeNames=names, ExpressionAttributeValues=values)
    else:
        try:
            table.put_item(Item=item, ConditionExpression="attribute_not_exists(repo_id)")
        except table.meta.client.exceptions.ConditionalCheckFailedException:
            print(f"{repo_id} already registered — use --force to overwrite", file=sys.stderr)
            return 1
    print(f"registered {repo_id} (scope {url}, cap {args.max_pages} pages)")

    if not args.no_runtime:
        print("creating dedicated MCP service...")
        try:
            rt = ensure_repo_runtime(repo_id, outputs, args.region, wait=False,
                                     cpu=args.service_cpu, memory=args.service_memory)
            table.update_item(
                Key={"repo_id": repo_id},
                UpdateExpression="SET runtime_name = :n, runtime_arn = :a, runtime_id = :i",
                ExpressionAttributeValues={":n": rt["runtime_name"], ":a": rt["runtime_arn"], ":i": rt["runtime_id"]},
            )
            print(f"service: {rt['runtime_id']} [{rt['status']}]")
        except Exception as exc:
            print(f"WARNING: dedicated service not ready ({exc}); run scripts/sync_runtimes.py to repair")

    if args.no_build:
        print("first crawl deferred to the poller (next due tick)")
        return 0

    env = [
        {"name": "REPO_ID", "value": repo_id, "type": "PLAINTEXT"},
        {"name": "GIT_URL", "value": url, "type": "PLAINTEXT"},
        {"name": "TARGET_SHA", "value": f"crawl-{now}", "type": "PLAINTEXT"},
        {"name": "GRAPH_BUCKET", "value": bucket, "type": "PLAINTEXT"},
        {"name": "PROVIDER", "value": "url", "type": "PLAINTEXT"},
        {"name": "GIT_REF", "value": "", "type": "PLAINTEXT"},
        # Part of the build contract: EVERY StartBuild call site passes it.
        {"name": "PRUNE_PATHS", "value": item.get("prune_paths", ""), "type": "PLAINTEXT"},
        {"name": "SOURCE_TYPE", "value": "url", "type": "PLAINTEXT"},
        {"name": "SOURCE_URL", "value": url, "type": "PLAINTEXT"},
        {"name": "CRAWL_MAX_PAGES", "value": str(args.max_pages), "type": "PLAINTEXT"},
    ]
    cb = boto3.client("codebuild", region_name=args.region)
    build = cb.start_build(projectName=outputs["GraphBuildProject"], environmentVariablesOverride=env)["build"]
    table.update_item(
        Key={"repo_id": repo_id},
        # build_arn is load-bearing: the completion Lambda's identity guard
        # only applies terminal state when the event's ARN matches it.
        UpdateExpression="SET #s = :b, build_id = :bid, build_arn = :arn, build_started_at = :now",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":b": "BUILDING", ":bid": build["id"], ":arn": build["arn"], ":now": now},
    )
    print(f"crawl build started: {build['id']}")
    print(f"watch:  aws codebuild batch-get-builds --ids '{build['id']}' --region {args.region} --query 'builds[0].buildStatus'")
    print(f"query:  POST {outputs.get('McpDataApiUrl', '').rstrip('/')}/mcp/{repo_id} with an X-Graphify-Key header")
    return 0


if __name__ == "__main__":
    sys.exit(main())
