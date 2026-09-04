#!/usr/bin/env python3
"""Roll every per-repo MCP Fargate service forward to the stack's current
image, optionally re-running each repo's build (e.g. to publish src.tar.gz
snapshots).

The hub service is stack-managed and updates with `cdk deploy`; per-repo
services are created dynamically at registration and keep their task
definition until updated — this script closes that gap after an image change.

Usage:
  uv run python scripts/update_repo_runtimes.py            # update runtimes
  uv run python scripts/update_repo_runtimes.py --rebuild  # + start builds
  uv run python scripts/update_repo_runtimes.py --rebuild --repo-id github__psf__requests__main   # one repo
"""

from __future__ import annotations

import argparse
import sys
import time

import boto3

from common import STACK_NAME, ensure_repo_runtime, stack_outputs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--region", default="ap-northeast-2")
    ap.add_argument("--stack", default=STACK_NAME)
    ap.add_argument("--rebuild", action="store_true", help="also start a CodeBuild for each repo")
    ap.add_argument("--repo-id", action="append", default=[], help="limit to this repo (repeatable)")
    ap.add_argument("--force", action="store_true", help="start a build even if one is already in flight for the repo")
    args = ap.parse_args()

    outputs = stack_outputs(args.region, args.stack)
    ddb = boto3.resource("dynamodb", region_name=args.region)
    table = ddb.Table(outputs["RepoRegistryTable"])
    cb = boto3.client("codebuild", region_name=args.region)

    rows, kwargs = [], {}
    while True:
        page = table.scan(**kwargs)
        rows.extend(page.get("Items", []))
        if "LastEvaluatedKey" not in page:
            break
        kwargs = {"ExclusiveStartKey": page["LastEvaluatedKey"]}
    # dedicated_runtime == "0" = registered with --no-runtime (hub-only): the
    # operator opted out of an always-warm dedicated service — never mint one.
    rows = [
        r for r in rows
        if r.get("enabled") == "1" and r.get("repo_id") != "__all__" and r.get("dedicated_runtime") != "0"
        and (not args.repo_id or r.get("repo_id") in args.repo_id)
    ]
    print(f"{len(rows)} enabled repo(s)")

    failures = 0
    for row in rows:
        rid = row["repo_id"]
        try:
            info = ensure_repo_runtime(
                rid, outputs, args.region, wait=False,
                cpu=int(row.get("service_cpu", 0) or 512),
                memory=int(row.get("service_memory", 0) or 2048),
            )
            table.update_item(
                Key={"repo_id": rid},
                UpdateExpression="SET runtime_name = :n, runtime_arn = :a, runtime_id = :i",
                ExpressionAttributeValues={":n": info["runtime_name"], ":a": info["runtime_arn"], ":i": info["runtime_id"]},
            )
            print(f"[service] {rid}: {info['runtime_id']} {info['status']}")
        except Exception as exc:
            failures += 1
            print(f"[service] {rid}: FAILED {type(exc).__name__}: {exc}")
            continue

        if not args.rebuild:
            continue
        # Claiming build_id/build_arn below would make the completion Lambda
        # ignore the in-flight build's result — leave a live build alone.
        window = 60 * max(60, int(row.get("build_timeout_minutes", 0) or 0))
        if (row.get("status") == "BUILDING" and int(row.get("build_started_at", 0) or 0) > time.time() - window
                and not args.force):
            print(f"[build]   {rid}: skipped (build in flight: {row.get('build_id', '?')}; use --force)")
            continue
        source_type = row.get("source_type", "git")
        if source_type == "git":
            sha = row.get("last_built_sha") or row.get("last_seen_sha") or ""
            if not sha:
                print(f"[build]   {rid}: skipped (no known sha)")
                continue
        else:
            # Non-git: TARGET_SHA is advisory — the build computes the corpus
            # content hash itself and completion records source_hash.
            sha = f"manual-{int(time.time())}"
        env = [
            {"name": "REPO_ID", "value": rid, "type": "PLAINTEXT"},
            {"name": "GIT_URL", "value": row.get("git_url", ""), "type": "PLAINTEXT"},
            {"name": "TARGET_SHA", "value": sha, "type": "PLAINTEXT"},
            {"name": "GRAPH_BUCKET", "value": outputs["GraphBucketName"], "type": "PLAINTEXT"},
            {"name": "PROVIDER", "value": row.get("provider", "github"), "type": "PLAINTEXT"},
            {"name": "GIT_REF", "value": row.get("ref", ""), "type": "PLAINTEXT"},
            {"name": "PRUNE_PATHS", "value": row.get("prune_paths", ""), "type": "PLAINTEXT"},
            {"name": "SOURCE_TYPE", "value": source_type, "type": "PLAINTEXT"},
            # Rides every build path (like PRUNE_PATHS): omitting it would
            # silently rebuild an LLM source as a quick-scan graph.
            {"name": "LLM_EXTRACT", "value": "1" if row.get("llm_extract") == "1" else "0", "type": "PLAINTEXT"},
            {"name": "LLM_IMAGES", "value": "1" if row.get("llm_images") == "1" else "0", "type": "PLAINTEXT"},
            {"name": "LLM_MODEL", "value": row.get("llm_model", ""), "type": "PLAINTEXT"},
            {"name": "LLM_CORPUS_CAP_MB", "value": str(row.get("llm_corpus_cap_mb", "") or ""), "type": "PLAINTEXT"},
        ]
        if source_type == "url":
            env += [
                {"name": "SOURCE_URL", "value": row.get("git_url", ""), "type": "PLAINTEXT"},
                {"name": "CRAWL_MAX_PAGES", "value": str(row.get("crawl_max_pages", 200)), "type": "PLAINTEXT"},
            ]
        if row.get("auth_secret_name"):
            env.append({"name": "GIT_TOKEN", "value": f"{row['auth_secret_name']}:token", "type": "SECRETS_MANAGER"})
        # Per-repo build sizing, exactly as the poller/webhook forward it.
        kwargs = {"projectName": outputs["GraphBuildProject"], "environmentVariablesOverride": env}
        if row.get("build_compute"):
            kwargs["computeTypeOverride"] = row["build_compute"]
        if row.get("build_timeout_minutes"):
            kwargs["timeoutInMinutesOverride"] = int(row["build_timeout_minutes"])
        try:
            build = cb.start_build(**kwargs)["build"]
            # Claim the build in the registry: the completion Lambda's status
            # writes are guarded on build_arn matching, so an unclaimed manual
            # build finishes without ever flipping status off FAILED/READY.
            table.update_item(
                Key={"repo_id": rid},
                UpdateExpression=(
                    "SET #s = :building, build_started_at = :now, "
                    "build_id = :bid, build_arn = :arn, updated_at = :iso"
                ),
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":building": "BUILDING",
                    ":now": int(time.time()),
                    ":bid": build["id"],
                    ":arn": build["arn"],
                    ":iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                },
            )
            print(f"[build]   {rid}: {build['id']} @ {sha[:12]}")
        except Exception as exc:
            failures += 1
            print(f"[build]   {rid}: FAILED {type(exc).__name__}: {exc}")

    print("done" + (f" ({failures} failure(s))" if failures else ""))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
