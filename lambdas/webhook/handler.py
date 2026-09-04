"""GitHub push-webhook receiver (push-triggered builds for OWNED repos).

Exposed via a Lambda Function URL. Authenticity is enforced by the GitHub
webhook HMAC (X-Hub-Signature-256 over the raw body, constant-time compare)
before anything is parsed — the shared secret lives in Secrets Manager and
is only ever shown to the operator, so only someone who can configure the
repo's webhooks (i.e. an owner/admin) can drive this endpoint. That is also
the ownership gate: unowned public repos stay on the polling path.

Only registry items registered with trigger=webhook are acted on; everything
else is acknowledged and ignored. Webhook repos keep a slow safety poll
(default 6 h) because GitHub delivery is at-least-once, not guaranteed.

The claim/StartBuild logic mirrors lambdas/poller/handler.py — keep the two
in sync when changing either.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import time

import boto3

TABLE_NAME = os.environ["TABLE_NAME"]
PROJECT_NAME = os.environ["PROJECT_NAME"]
GRAPH_BUCKET = os.environ["GRAPH_BUCKET"]
WEBHOOK_SECRET_ARN = os.environ["WEBHOOK_SECRET_ARN"]

dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(TABLE_NAME)
codebuild = boto3.client("codebuild")
secrets = boto3.client("secretsmanager")

_secret_cache: dict[str, bytes] = {}


def _webhook_secret() -> bytes:
    if "v" not in _secret_cache:
        _secret_cache["v"] = secrets.get_secret_value(SecretId=WEBHOOK_SECRET_ARN)[
            "SecretString"
        ].encode()
    return _secret_cache["v"]


def _sanitize(part: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", part)


def make_repo_id(owner: str, repo: str, ref: str) -> str:
    # Must stay identical to scripts/common.py:make_repo_id for GitHub URLs.
    return "__".join(_sanitize(p) for p in ("github", owner, repo, ref))


def _iso(epoch: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _resp(code: int, body: dict):
    return {"statusCode": code, "headers": {"content-type": "application/json"}, "body": json.dumps(body)}


def claim_build(item: dict, sha: str, now: int) -> bool:
    try:
        table.update_item(
            Key={"repo_id": item["repo_id"]},
            UpdateExpression=(
                "SET #s = :building, build_started_at = :now, "
                "last_seen_sha = :sha, next_poll_at = :next, updated_at = :iso"
            ),
            ConditionExpression="attribute_not_exists(#s) OR #s <> :building",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":building": "BUILDING",
                ":now": now,
                ":sha": sha,
                ":next": now + int(item.get("poll_interval_seconds", 21600)),
                ":iso": _iso(now),
            },
        )
        return True
    except dynamodb.meta.client.exceptions.ConditionalCheckFailedException:
        return False


def start_build(item: dict, sha: str) -> dict:
    env = [
        {"name": "REPO_ID", "value": item["repo_id"], "type": "PLAINTEXT"},
        {"name": "GIT_URL", "value": item["git_url"], "type": "PLAINTEXT"},
        {"name": "TARGET_SHA", "value": sha, "type": "PLAINTEXT"},
        {"name": "GRAPH_BUCKET", "value": GRAPH_BUCKET, "type": "PLAINTEXT"},
        {"name": "PROVIDER", "value": item.get("provider", "github"), "type": "PLAINTEXT"},
        {"name": "GIT_REF", "value": item.get("ref", "HEAD"), "type": "PLAINTEXT"},
        # Keep in sync with the poller: omitting PRUNE_PATHS on any build path
        # silently un-prunes the graph back to full size.
        {"name": "PRUNE_PATHS", "value": item.get("prune_paths", ""), "type": "PLAINTEXT"},
    ]
    if item.get("auth_secret_name"):
        env.append(
            {"name": "GIT_TOKEN", "value": f"{item['auth_secret_name']}:token", "type": "SECRETS_MANAGER"}
        )
    kwargs = {"projectName": PROJECT_NAME, "environmentVariablesOverride": env}
    if item.get("build_compute"):
        kwargs["computeTypeOverride"] = item["build_compute"]
    if item.get("build_timeout_minutes"):
        kwargs["timeoutInMinutesOverride"] = int(item["build_timeout_minutes"])
    return codebuild.start_build(**kwargs)["build"]


def handler(event, context):
    req = (event.get("requestContext") or {}).get("http") or {}
    if req.get("method", "POST") != "POST":
        return _resp(405, {"error": "POST only"})

    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    body_raw = event.get("body") or ""
    body = base64.b64decode(body_raw) if event.get("isBase64Encoded") else body_raw.encode()

    # HMAC gate first — nothing is parsed before authenticity is proven.
    expected = "sha256=" + hmac.new(_webhook_secret(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, headers.get("x-hub-signature-256", "")):
        return _resp(401, {"error": "invalid signature"})

    gh_event = headers.get("x-github-event", "")
    if gh_event == "ping":
        return _resp(200, {"pong": True})
    if gh_event != "push":
        return _resp(200, {"ignored": f"event {gh_event or 'unknown'}"})

    try:
        payload = json.loads(body)
    except ValueError:
        return _resp(400, {"error": "invalid JSON"})

    ref = str(payload.get("ref", ""))
    after = str(payload.get("after", ""))
    full_name = str((payload.get("repository") or {}).get("full_name", ""))
    if payload.get("deleted") or after == "0" * 40:
        return _resp(200, {"ignored": "ref deleted"})
    if not ref.startswith("refs/heads/") or "/" not in full_name or not re.fullmatch(r"[0-9a-f]{40}", after):
        return _resp(400, {"error": "unsupported push payload"})

    branch = ref[len("refs/heads/"):]
    owner, repo = full_name.split("/", 1)
    repo_id = make_repo_id(owner, repo, branch)

    item = table.get_item(Key={"repo_id": repo_id}).get("Item")
    if not item or item.get("trigger") != "webhook" or item.get("enabled") != "1":
        # Signed but unregistered (or poll-mode) — acknowledge, do nothing.
        print(f"ignored push for {repo_id} (not webhook-registered)")
        return _resp(200, {"ignored": repo_id})

    if after == str(item.get("last_built_sha", "")):
        return _resp(200, {"no_change": after})

    now = int(time.time())
    if not claim_build(item, after, now):
        # A build is in flight; let the poller re-check shortly after it ends.
        table.update_item(
            Key={"repo_id": repo_id},
            UpdateExpression="SET last_seen_sha = :sha, next_poll_at = :next, updated_at = :iso",
            ExpressionAttributeValues={":sha": after, ":next": now + 120, ":iso": _iso(now)},
        )
        print(f"{repo_id}: build in flight, deferred {after[:12]} to poller")
        return _resp(202, {"deferred_to_poll": after})

    try:
        build = start_build(item, after)
    except Exception as exc:
        table.update_item(
            Key={"repo_id": repo_id},
            UpdateExpression="SET #s = :failed, last_error = :err, updated_at = :iso",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":failed": "FAILED",
                ":err": f"StartBuild failed: {exc}"[:1000],
                ":iso": _iso(now),
            },
        )
        print(f"{repo_id}: StartBuild failed: {exc}")
        return _resp(500, {"error": "StartBuild failed"})

    table.update_item(
        Key={"repo_id": repo_id},
        UpdateExpression="SET build_id = :bid, build_arn = :arn",
        ExpressionAttributeValues={":bid": build["id"], ":arn": build["arn"]},
    )
    print(f"{repo_id}: build {build['id']} started at {after[:12]} (push)")
    return _resp(200, {"build_started": build["id"], "sha": after})
