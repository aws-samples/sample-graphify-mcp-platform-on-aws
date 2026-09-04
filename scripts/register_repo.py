#!/usr/bin/env python3
"""Register a git repo for graphify graph building + polling.

Resolves the repo's real default branch when --ref is omitted (never assumes
'main' — e.g. graphify's own default branch is 'v8'), writes the registry
item, and starts the first build immediately so the graph exists without
waiting for a poll tick.

Usage:
  uv run python scripts/register_repo.py --url https://github.com/psf/requests
  uv run python scripts/register_repo.py --url ... --ref develop --poll-interval 900
  uv run python scripts/register_repo.py --url ... --auth-secret graphify/pat/myorg   # private repo
                                          # (secret must be JSON: {"token": "<PAT>"})
"""

from __future__ import annotations

import argparse
import sys
import time

import boto3

from common import (
    PROVIDER_AUTH_USERS,
    STACK_NAME,
    detect_provider,
    ensure_repo_runtime,
    github_head_sha,
    github_repo_info,
    make_repo_id,
    smart_http_refs,
    stack_outputs,
    validate_task_size,
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True, help="git clone URL (https, any smart-HTTP git server)")
    ap.add_argument("--ref", default="", help="branch to track (default: repo's default branch)")
    ap.add_argument(
        "--provider",
        default="auto",
        choices=["auto", "github", "gitlab", "bitbucket", "generic"],
        help="git host kind; picks the change-detection path and the PAT Basic-auth username (auto: detect from host)",
    )
    ap.add_argument(
        "--trigger",
        default="poll",
        choices=["poll", "webhook"],
        help="poll (default; works for any repo) or webhook (push-triggered; OWNED GitHub repos only — you must be able to add a repo webhook)",
    )
    ap.add_argument("--poll-interval", type=int, default=None, help="seconds between change checks (default: 900 for poll, 21600 safety poll for webhook)")
    ap.add_argument("--auth-secret", default="", help='Secrets Manager secret name holding a PAT (private repos); must start with graphify/ and be JSON {"token": "<PAT>"}')
    ap.add_argument("--force", action="store_true", help="overwrite an existing registration (may orphan an in-flight build)")
    ap.add_argument("--build-compute", default="", help="CodeBuild computeTypeOverride, e.g. BUILD_GENERAL1_MEDIUM for monorepos")
    ap.add_argument("--build-timeout-minutes", type=int, default=0)
    ap.add_argument("--region", default="ap-northeast-2")
    ap.add_argument("--stack", default=STACK_NAME)
    ap.add_argument("--no-build", action="store_true", help="register only; let the poller start the first build")
    ap.add_argument("--no-runtime", action="store_true", help="skip the dedicated per-repo MCP service (hub-only serving)")
    ap.add_argument("--service-cpu", type=int, default=None, help="Fargate task vCPU units for the repo's MCP service (256/512/1024/2048/4096; default 512, or the stored value under --force)")
    ap.add_argument("--service-memory", type=int, default=None, help="Fargate task memory MiB (must be valid for the cpu tier; default 2048, or the stored value under --force); bump for large graphs")
    args = ap.parse_args()

    if args.auth_secret and not args.auth_secret.startswith("graphify/"):
        ap.error("--auth-secret must start with 'graphify/' (the build role is scoped to that prefix)")
    if args.build_timeout_minutes and not 5 <= args.build_timeout_minutes <= 480:
        ap.error("--build-timeout-minutes must be within CodeBuild's 5..480 range")
    reg_token = ""
    if args.auth_secret:
        # Fail at registration, not at clone time: CodeBuild consumes the
        # secret as '<name>:token', which requires the JSON {"token": ...} shape.
        import json as _json

        sm = boto3.client("secretsmanager", region_name=args.region)
        try:
            raw = sm.get_secret_value(SecretId=args.auth_secret)["SecretString"]
            parsed = _json.loads(raw)
            reg_token = str(parsed.get("token", "")).strip()
            assert isinstance(parsed, dict) and reg_token
        except Exception as exc:
            ap.error(f'--auth-secret {args.auth_secret} must exist and be JSON {{"token": "<PAT>"}} ({exc})')

    outputs = stack_outputs(args.region, args.stack)
    table_name = outputs["RepoRegistryTable"]
    project_name = outputs["GraphBuildProject"]
    bucket = outputs["GraphBucketName"]

    git_url = args.url.rstrip("/")
    # Normalize like make_repo_id (strip trailing .git) so the stored git_url
    # matches the derived id and a later platform registration pools correctly.
    if git_url.endswith(".git"):
        git_url = git_url[:-4]
    provider = args.provider if args.provider != "auto" else detect_provider(git_url)
    is_github = provider == "github"
    auth_user = PROVIDER_AUTH_USERS.get(provider, "git")
    if args.trigger == "webhook" and not is_github:
        ap.error("--trigger webhook currently supports GitHub push payloads only; use polling for other hosts")
    poll_interval = args.poll_interval if args.poll_interval is not None else (21600 if args.trigger == "webhook" else 900)
    if poll_interval < 60:
        ap.error("--poll-interval must be >= 60 seconds")
    if args.provider == "auto":
        print(f"provider: {provider} (auto-detected; override with --provider)")

    ref = args.ref
    if not ref:
        if is_github:
            try:
                ref = github_repo_info(git_url, reg_token)["default_branch"]
            except Exception as exc:
                print(f"GitHub API default-branch lookup failed ({exc}); falling back to info/refs")
                ref, _ = smart_http_refs(git_url, reg_token, auth_user)
        else:
            ref, _ = smart_http_refs(git_url, reg_token, auth_user)
        if not ref:
            print("could not resolve default branch; pass --ref explicitly", file=sys.stderr)
            return 1
        print(f"resolved default branch: {ref}")

    head_sha = ""
    if is_github:
        try:
            head_sha = github_head_sha(git_url, ref, reg_token)
        except Exception as exc:
            # The unauthenticated API quota (60/h per IP) exhausts easily;
            # the git smart-HTTP endpoint is not governed by it.
            print(f"GitHub API head lookup failed ({exc}); falling back to info/refs")
    if not head_sha:
        _, refs = smart_http_refs(git_url, reg_token, auth_user)
        head_sha = refs.get(f"refs/heads/{ref}", "") or refs.get(ref, "")
        if not head_sha:
            print(f"ref refs/heads/{ref} not found on remote", file=sys.stderr)
            return 1

    repo_id = make_repo_id(git_url, ref)
    now = int(time.time())
    iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))

    table = boto3.resource("dynamodb", region_name=args.region).Table(table_name)

    # Task sizing: an explicit flag wins; --force without flags KEEPS the
    # stored size (a console-sized big repo must not be silently downsized to
    # the defaults by a --force run that only changes the ref). Validate the
    # pair before it is persisted — a Fargate-illegal combo on the row would
    # poison every later repair path.
    stored = table.get_item(Key={"repo_id": repo_id}).get("Item") or {} if args.force else {}
    service_cpu = args.service_cpu if args.service_cpu is not None else int(stored.get("service_cpu", 0) or 512)
    service_memory = args.service_memory if args.service_memory is not None else int(stored.get("service_memory", 0) or 2048)
    try:
        validate_task_size(service_cpu, service_memory)
    except ValueError as exc:
        ap.error(str(exc))

    item = {
        "repo_id": repo_id,
        "git_url": git_url,
        "provider": provider,
        "ref": ref,
        "enabled": "1",
        # CLI registrations are operator-owned public repos; the hub-merge
        # step only includes rows explicitly marked public (fail-closed).
        "graph_scope": "private" if args.auth_secret else "public",
        "subscriber_count": 1,
        "trigger": args.trigger,
        "dedicated_runtime": "0" if args.no_runtime else "1",
        "poll_interval_seconds": poll_interval,
        "next_poll_at": now + poll_interval,
        "last_seen_sha": head_sha,
        "status": "REGISTERED",
        "service_cpu": service_cpu,
        "service_memory": service_memory,
        "created_at": iso,
        "updated_at": iso,
    }
    if args.auth_secret:
        item["auth_secret_name"] = args.auth_secret
    if args.build_compute:
        item["build_compute"] = args.build_compute
    if args.build_timeout_minutes:
        item["build_timeout_minutes"] = args.build_timeout_minutes

    if args.force:
        existing = table.get_item(Key={"repo_id": repo_id}).get("Item")
        if not existing:
            # Brand-new: a full put writes graph_scope, subscriber_count=1, etc.
            # (an update-only upsert would leave graph_scope unset -> a private
            # repo would leak into the fail-closed hub merge).
            table.put_item(Item=item)
        else:
            # Existing row: keep platform-owned counters/runtime/build state
            # (a full put would drop runtime_arn -> 404s, and reset the count),
            # but the CLI still OWNS graph_scope and auth_secret_name — never
            # preserve those, or a public->private --force would leave the row
            # public and leak the now-private repo into the hub.
            preserve = {"repo_id", "subscriber_count", "created_by_sub",
                        "runtime_name", "runtime_arn", "runtime_id",
                        "last_built_sha", "last_built_at", "build_id", "build_arn"}
            sets = {k: v for k, v in item.items() if k not in preserve}
            optional = {"auth_secret_name", "build_compute", "build_timeout_minutes"}
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
            existing = table.get_item(Key={"repo_id": repo_id}).get("Item", {})
            if existing.get("git_url") != git_url or existing.get("ref") != ref:
                print(
                    f"repo_id collision: {repo_id} is already registered for "
                    f"{existing.get('git_url')}@{existing.get('ref')} — refusing to overwrite",
                    file=sys.stderr,
                )
                return 1
            print(
                f"{repo_id} already registered (status={existing.get('status')}, "
                f"last_built={str(existing.get('last_built_sha', ''))[:12]}). "
                "Use --force to overwrite (may orphan an in-flight build).",
                file=sys.stderr,
            )
            return 1
    print(f"registered {repo_id} (ref={ref}, head={head_sha[:12]})")

    if not args.no_runtime:
        # Dedicated per-repo MCP Fargate service (default). Spun up in parallel
        # with the first build; both are usually ready within a couple of minutes.
        print("creating dedicated MCP service (image pull + graph load run in parallel with the build)...")
        try:
            rt = ensure_repo_runtime(
                repo_id, outputs, args.region, wait=False,
                cpu=service_cpu, memory=service_memory,
            )
            table.update_item(
                Key={"repo_id": repo_id},
                UpdateExpression="SET runtime_name = :n, runtime_arn = :a, runtime_id = :i",
                ExpressionAttributeValues={":n": rt["runtime_name"], ":a": rt["runtime_arn"], ":i": rt["runtime_id"]},
            )
            print(f"service: {rt['runtime_id']} [{rt['status']}]")
            print(f"mcp    : {outputs.get('McpDataApiUrl', '<McpDataApiUrl output missing>')}/mcp/{repo_id}  (X-Graphify-Key)")
        except Exception as exc:
            # Registration and the first build remain valid; sync_runtimes.py
            # repairs the service later.
            print(f"WARNING: dedicated service not ready ({exc}); run scripts/sync_runtimes.py to repair")

    if args.no_build:
        print("first build deferred to the poller (next tick)")
        return 0

    env = [
        {"name": "REPO_ID", "value": repo_id, "type": "PLAINTEXT"},
        {"name": "GIT_URL", "value": git_url, "type": "PLAINTEXT"},
        {"name": "TARGET_SHA", "value": head_sha, "type": "PLAINTEXT"},
        {"name": "GRAPH_BUCKET", "value": bucket, "type": "PLAINTEXT"},
        {"name": "PROVIDER", "value": provider, "type": "PLAINTEXT"},
        {"name": "GIT_REF", "value": ref, "type": "PLAINTEXT"},
        # Part of the build contract: EVERY StartBuild call site passes it. A
        # --force re-register of a pruned repo (e.g. litellm) would otherwise
        # kick a first build that silently un-prunes the graph.
        {"name": "PRUNE_PATHS", "value": str(stored.get("prune_paths", "")), "type": "PLAINTEXT"},
    ]
    if args.auth_secret:
        env.append({"name": "GIT_TOKEN", "value": f"{args.auth_secret}:token", "type": "SECRETS_MANAGER"})
    kwargs = {"projectName": project_name, "environmentVariablesOverride": env}
    if args.build_compute:
        kwargs["computeTypeOverride"] = args.build_compute
    if args.build_timeout_minutes:
        kwargs["timeoutInMinutesOverride"] = args.build_timeout_minutes

    cb = boto3.client("codebuild", region_name=args.region)
    build = cb.start_build(**kwargs)["build"]
    build_id = build["id"]
    table.update_item(
        Key={"repo_id": repo_id},
        # build_arn is load-bearing: the completion Lambda's identity guard
        # only applies terminal state when the event's ARN matches it.
        UpdateExpression="SET #s = :b, build_id = :bid, build_arn = :arn, build_started_at = :now",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":b": "BUILDING", ":bid": build_id, ":arn": build["arn"], ":now": now},
    )
    print(f"build started: {build_id}")
    print(f"watch:  aws codebuild batch-get-builds --ids '{build_id}' --region {args.region} --query 'builds[0].buildStatus'")
    print(f"graph:  s3://{bucket}/repos/{repo_id}/latest/graphify-out/graph.json")
    print(f"query:  POST {outputs.get('McpDataApiUrl', '').rstrip('/')}/mcp/{repo_id} with an X-Graphify-Key header (see scripts/smoke_test.py)")

    if args.trigger == "webhook":
        webhook_url = outputs.get("WebhookUrl", "<WebhookUrl output missing — redeploy the stack>")
        secret_arn = outputs.get("WebhookSecretArn", "")
        print("\n== GitHub webhook 설정 (repo Settings → Webhooks → Add webhook) ==")
        print(f"  Payload URL : {webhook_url}")
        print("  Content type: application/json")
        print("  Events      : Just the push event")
        print("  Secret      : run ->")
        print(f"    aws secretsmanager get-secret-value --secret-id '{secret_arn}' --region {args.region} --query SecretString --output text")
        print(f"  안전 폴링    : {poll_interval}s 간격으로 webhook 유실을 보완합니다")
    return 0


if __name__ == "__main__":
    sys.exit(main())
