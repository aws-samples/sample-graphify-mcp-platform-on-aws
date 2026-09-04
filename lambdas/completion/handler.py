"""Build-completion handler.

Triggered by the EventBridge "CodeBuild Build State Change" event (SUCCEEDED /
FAILED / FAULT / TIMED_OUT / STOPPED) for the graph-build project. Drives all
terminal state transitions in DynamoDB — the buildspec itself never touches
the table, and only the event path reliably observes TIMED_OUT/STOPPED.

Every write is identity-guarded on build_arn = the event's build ARN:
EventBridge delivery is at-least-once and unordered, so a duplicate or stale
event for a superseded build must not clobber the state of a newer one.

On success last_built_sha advances; on failure it is left untouched so the
next poll retries the same SHA instead of recording a failed build as done.
"""

from __future__ import annotations

import os
import time

import boto3

TABLE_NAME = os.environ["TABLE_NAME"]
GRAPH_BUCKET = os.environ["GRAPH_BUCKET"]
# graphify's serve-time graph cap (GRAPHIFY_MAX_GRAPH_BYTES default). A build
# can succeed with a bigger graph.json, but the MCP task then refuses to load
# it and every tool call fails — so the row is marked TOO_LARGE, not READY.
GRAPH_SERVE_CAP_BYTES = int(os.environ.get("GRAPH_SERVE_CAP_BYTES", str(512 * 1024 * 1024)))

dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(TABLE_NAME)
s3 = boto3.client("s3")


def _iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _guarded_update(repo_id: str, build_arn: str, **kwargs) -> bool:
    """update_item gated on this event belonging to the currently claimed build."""
    try:
        table.update_item(
            Key={"repo_id": repo_id},
            ConditionExpression="build_arn = :event_arn",
            **kwargs,
        )
        return True
    except dynamodb.meta.client.exceptions.ConditionalCheckFailedException:
        print(f"stale/duplicate build event ignored for {repo_id} ({build_arn})")
        return False


def handler(event, context):
    detail = event.get("detail", {})
    status = detail.get("build-status", "")
    build_arn = detail.get("build-id", "")  # the event carries the full ARN
    env_vars = (
        detail.get("additional-information", {})
        .get("environment", {})
        .get("environment-variables", [])
    )
    env = {e.get("name"): e.get("value") for e in env_vars}
    repo_id = env.get("REPO_ID")
    target_sha = env.get("TARGET_SHA", "")
    source_type = env.get("SOURCE_TYPE", "git") or "git"

    if not repo_id:
        print(f"ignoring build event without REPO_ID (build={build_arn})")
        return {"ignored": True}

    if status == "SUCCEEDED":
        if source_type != "git":
            # Non-git sources have no commit: the build computes a corpus
            # content hash and publishes it (AFTER the graph) as source_hash.
            # That object — not the advisory TARGET_SHA the claimer guessed —
            # is what the poller's change detection compares against, so it is
            # what last_built_sha must record. On a skipped url build it is
            # simply the unchanged previous hash.
            hash_key = f"repos/{repo_id}/latest/source_hash"
            try:
                target_sha = (
                    s3.get_object(Bucket=GRAPH_BUCKET, Key=hash_key)["Body"].read().decode().strip()
                    or target_sha
                )
            except Exception as exc:
                print(f"source_hash read failed for {hash_key} ({exc}); falling back to TARGET_SHA")

        key = f"repos/{repo_id}/latest/graphify-out/graph.json"
        try:
            head = s3.head_object(Bucket=GRAPH_BUCKET, Key=key)
            graph_etag = head.get("ETag", "")
            graph_bytes = int(head.get("ContentLength", 0))
        except Exception as exc:
            # A SUCCEEDED build whose artifact is missing is a failure:
            # never mark READY pointing at nothing.
            print(f"head_object failed for {key}: {exc}")
            _guarded_update(
                repo_id,
                build_arn,
                UpdateExpression="SET #s = :failed, last_error = :err, updated_at = :iso",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":failed": "FAILED",
                    ":err": f"build succeeded but graph missing at s3://{GRAPH_BUCKET}/{key}"[:1000],
                    ":iso": _iso(),
                    ":event_arn": build_arn,
                },
            )
            return {"repo_id": repo_id, "status": "FAILED", "reason": "artifact-missing"}

        # The build skips the source snapshot above its size cap; surface that
        # so the console can say why search_code/read_source are missing.
        try:
            s3.head_object(Bucket=GRAPH_BUCKET, Key=f"repos/{repo_id}/latest/src.tar.gz")
            has_snapshot = True
        except Exception:
            has_snapshot = False

        too_large = graph_bytes > GRAPH_SERVE_CAP_BYTES
        new_status = "TOO_LARGE" if too_large else "READY"
        values = {
            ":st": new_status,
            ":sha": target_sha,
            ":key": key,
            ":etag": graph_etag,
            ":bytes": graph_bytes,
            ":snap": has_snapshot,
            ":iso": _iso(),
            ":event_arn": build_arn,
        }
        if too_large:
            expr = (
                "SET #s = :st, last_built_sha = :sha, graph_s3_key = :key, graph_etag = :etag, "
                "graph_bytes = :bytes, has_snapshot = :snap, updated_at = :iso, last_error = :err"
            )
            values[":err"] = (
                f"graph.json is {graph_bytes} bytes; the MCP server refuses graphs above "
                f"{GRAPH_SERVE_CAP_BYTES} bytes, so tool calls fail. Shrink the graph (prune_paths) "
                "or split the source."
            )[:1000]
        else:
            expr = (
                "SET #s = :st, last_built_sha = :sha, graph_s3_key = :key, graph_etag = :etag, "
                "graph_bytes = :bytes, has_snapshot = :snap, updated_at = :iso REMOVE last_error"
            )
        applied = _guarded_update(
            repo_id,
            build_arn,
            UpdateExpression=expr,
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues=values,
        )
        if applied:
            print(f"{repo_id}: {new_status} at {target_sha} ({graph_bytes} bytes, snapshot={has_snapshot})")
        return {"repo_id": repo_id, "status": new_status if applied else "ignored", "sha": target_sha}

    applied = _guarded_update(
        repo_id,
        build_arn,
        UpdateExpression="SET #s = :failed, last_error = :err, updated_at = :iso",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":failed": "FAILED",
            ":err": f"build {status}: {build_arn}"[:1000],
            ":iso": _iso(),
            ":event_arn": build_arn,
        },
    )
    if applied:
        print(f"{repo_id}: FAILED ({status}, build={build_arn})")
    return {"repo_id": repo_id, "status": "FAILED" if applied else "ignored", "build_status": status}
