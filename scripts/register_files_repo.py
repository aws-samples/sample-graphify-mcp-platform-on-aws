#!/usr/bin/env python3
"""Register a static-file-folder source (source_type=files) for graph building.

Creates the registry row plus its S3 upload prefix
(s3://<bucket>/uploads/files__<name>/); you sync any folder of supported
files (code and/or markdown docs) there and the poller rebuilds the graph
whenever the folder content changes (S3 listing ETag manifest hash). With
--path the script syncs a local folder right away and starts the first build
so the graph exists without waiting for a poll tick.

Operator CLI registrations are graph_scope=public (hub-merged), same as
register_repo.py without a PAT. Console users get siloed private files repos
via POST /repos {"source_type": "files"} instead.

Usage:
  uv run python scripts/register_files_repo.py --name myproject --path ~/src/myproject
  uv run python scripts/register_files_repo.py --name notes            # register only; sync later
  aws s3 sync <folder> s3://<bucket>/uploads/files__<name>/ --delete   # later syncs
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time

import boto3

from common import (
    STACK_NAME,
    ensure_repo_runtime,
    files_repo_id,
    stack_outputs,
    validate_task_size,
)

# aws s3 sync patterns match the path relative to the folder, so each junk dir
# needs a top-level and a nested ("*/") form.
_JUNK_DIRS = [".git", ".venv", "venv", "node_modules", "__pycache__", ".idea", ".vscode", "cdk.out", "dist"]
DEFAULT_EXCLUDES = [f"{d}/*" for d in _JUNK_DIRS] + [f"*/{d}/*" for d in _JUNK_DIRS] + [".DS_Store", "*/.DS_Store"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True, help="repo name slug ([a-z0-9_-], 1-40 chars); repo_id becomes files__<name>")
    ap.add_argument("--path", default="", help="local folder to sync now (starts the first build immediately)")
    ap.add_argument("--exclude", action="append", default=[], help=f"extra aws s3 sync --exclude pattern (defaults: {DEFAULT_EXCLUDES})")
    ap.add_argument("--prune-paths", default="", help="space-separated repo-relative dirs to drop before extraction")
    ap.add_argument("--poll-interval", type=int, default=900, help="seconds between S3 change checks (default 900)")
    ap.add_argument("--force", action="store_true", help="overwrite an existing registration")
    ap.add_argument("--no-runtime", action="store_true", help="skip the dedicated per-repo MCP service (hub-only serving)")
    ap.add_argument("--service-cpu", type=int, default=512)
    ap.add_argument("--service-memory", type=int, default=2048)
    ap.add_argument("--region", default="ap-northeast-2")
    ap.add_argument("--stack", default=STACK_NAME)
    args = ap.parse_args()

    name = args.name.strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,39}", name):
        ap.error("--name must be 1-40 chars of [a-z0-9_-]")
    if args.poll_interval < 60:
        ap.error("--poll-interval must be >= 60 seconds")
    for p in args.prune_paths.split():
        if p.startswith("/") or ".." in p.split("/"):
            ap.error(f"invalid prune path: {p!r}")
    try:
        validate_task_size(args.service_cpu, args.service_memory)
    except ValueError as exc:
        ap.error(str(exc))

    outputs = stack_outputs(args.region, args.stack)
    bucket = outputs["GraphBucketName"]
    repo_id = files_repo_id(name)
    upload_prefix = f"uploads/{repo_id}/"
    s3_url = f"s3://{bucket}/{upload_prefix}"

    now = int(time.time())
    iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
    item = {
        "repo_id": repo_id,
        "git_url": s3_url,
        "provider": "s3",
        "ref": "",
        "source_type": "files",
        "enabled": "1",
        # CLI registrations are operator-owned; public joins the hub merge
        # (fail-closed graph_scope filter) like register_repo.py without a PAT.
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
            print(f"{repo_id} already registered — use --force to overwrite, or just sync:\n"
                  f"  aws s3 sync <folder> {s3_url} --delete", file=sys.stderr)
            return 1

    # Folder marker so the prefix is browsable before the first sync.
    boto3.client("s3", region_name=args.region).put_object(Bucket=bucket, Key=upload_prefix, Body=b"")
    print(f"registered {repo_id}")
    print(f"upload : {s3_url}")

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

    excludes = DEFAULT_EXCLUDES + args.exclude
    sync_cmd = ["aws", "s3", "sync", args.path or "<local-folder>", s3_url, "--delete"]
    for pat in excludes:
        sync_cmd += ["--exclude", pat]

    if not args.path:
        print("\nsync your folder, then the poller builds automatically:")
        print("  " + " ".join(sync_cmd))
        return 0

    print(f"\nsyncing {args.path} -> {s3_url}")
    # aws s3 sync exits 2 when some files were skipped (dangling symlinks,
    # unreadable files) but everything else transferred — warn, don't abort.
    rc = subprocess.run(sync_cmd).returncode
    if rc == 2:
        print("WARNING: aws s3 sync skipped some files (exit 2); continuing")
    elif rc != 0:
        print(f"aws s3 sync failed (exit {rc})", file=sys.stderr)
        return 1

    # First build immediately (the poller would otherwise wait a full interval).
    env = [
        {"name": "REPO_ID", "value": repo_id, "type": "PLAINTEXT"},
        {"name": "GIT_URL", "value": s3_url, "type": "PLAINTEXT"},
        {"name": "TARGET_SHA", "value": f"manual-{now}", "type": "PLAINTEXT"},
        {"name": "GRAPH_BUCKET", "value": bucket, "type": "PLAINTEXT"},
        {"name": "PROVIDER", "value": "s3", "type": "PLAINTEXT"},
        {"name": "GIT_REF", "value": "", "type": "PLAINTEXT"},
        # Part of the build contract: EVERY StartBuild call site passes it.
        {"name": "PRUNE_PATHS", "value": item.get("prune_paths", ""), "type": "PLAINTEXT"},
        {"name": "SOURCE_TYPE", "value": "files", "type": "PLAINTEXT"},
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
    print(f"build started: {build['id']}")
    print(f"watch:  aws codebuild batch-get-builds --ids '{build['id']}' --region {args.region} --query 'builds[0].buildStatus'")
    print(f"query:  POST {outputs.get('McpDataApiUrl', '').rstrip('/')}/mcp/{repo_id} with an X-Graphify-Key header")
    return 0


if __name__ == "__main__":
    sys.exit(main())
