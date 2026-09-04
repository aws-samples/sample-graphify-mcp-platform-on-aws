"""Graphify platform management API (HTTP API v2 + Cognito JWT authorizer).

All routes require a Cognito access token; ownership is enforced here in
application code — every handler resolves resources through the caller's
grant rows (USER#<sub>/REPO#..., USER#<sub>/KEY#...) in the platform table,
never directly from a client-supplied id.

Tenancy model:
  - public repo (no PAT): POOLED — one build/graph/runtime shared by every
    subscriber; registration by a second user is an idempotent join.
  - private repo (PAT):   SILOED — repo_id gets a __u<sub8> owner suffix,
    graph_scope=private keeps it out of the merged hub graph (fail-closed
    filter in the buildspec), and the graph is served only by its dedicated
    runtime.
"""

from __future__ import annotations

import json
import os
import re
import time
from decimal import Decimal

import boto3
from botocore.config import Config

import gitreg
import keys as keysmod
import runtimes

REGION = os.environ["AWS_REGION"]
PLATFORM_TABLE = os.environ["PLATFORM_TABLE"]
REGISTRY_TABLE = os.environ["REGISTRY_TABLE"]
PROJECT_NAME = os.environ["PROJECT_NAME"]
GRAPH_BUCKET = os.environ["GRAPH_BUCKET"]
MCP_BASE_URL = os.environ["MCP_BASE_URL"].rstrip("/")
USAGE_PLAN_ID = os.environ["USAGE_PLAN_ID"]
USER_POOL_ID = os.environ["USER_POOL_ID"]
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")
WEBHOOK_SECRET_ARN = os.environ.get("WEBHOOK_SECRET_ARN", "")
# In-VPC data-plane proxy, invoked directly for the console's source viewer.
MCP_PROXY_FN = os.environ.get("MCP_PROXY_FN", "")
# Per-repo Fargate service creation (runtimes.ensure_repo_runtime).
RUNTIME_ENV = {
    "ECS_CLUSTER": os.environ["ECS_CLUSTER"],
    "TASK_IMAGE": os.environ["TASK_IMAGE"],
    "TASK_ROLE_ARN": os.environ["TASK_ROLE_ARN"],
    "TASK_EXEC_ROLE_ARN": os.environ["TASK_EXEC_ROLE_ARN"],
    "TASK_SUBNETS": os.environ["TASK_SUBNETS"],
    "TASK_SECURITY_GROUP": os.environ["TASK_SECURITY_GROUP"],
    "CLOUDMAP_NAMESPACE_ID": os.environ["CLOUDMAP_NAMESPACE_ID"],
    "SERVICE_LOG_GROUP": os.environ["SERVICE_LOG_GROUP"],
    "GRAPH_BUCKET": GRAPH_BUCKET,
}

MAX_ACTIVE_KEYS = 10
# Bedrock model ids a document source may pick for LLM extraction. Every
# entry is an Anthropic inference profile the CodeBuild role may invoke and
# is vision-capable (llm_images). The first is the default the buildspec
# falls back to when the row carries no llm_model.
LLM_MODELS = (
    "global.anthropic.claude-sonnet-5",
    "global.anthropic.claude-opus-5",
    "global.anthropic.claude-sonnet-4-6",
    "global.anthropic.claude-opus-4-8",
    "global.anthropic.claude-haiku-4-5-20251001-v1:0",
)
LLM_DEFAULT_MODEL = LLM_MODELS[0]
DEFAULT_KEY_EXPIRES_DAYS = 365
# Ceiling on any poll interval (1 week, the console's top preset). Without it a
# huge value materializes into next_poll_at and strands the row's polling.
URL_POLL_MAX = 604800

_ddb = boto3.resource("dynamodb", region_name=REGION)
_ddbc = boto3.client("dynamodb", region_name=REGION)
_platform = _ddb.Table(PLATFORM_TABLE)
_registry = _ddb.Table(REGISTRY_TABLE)
_codebuild = boto3.client("codebuild", region_name=REGION)
_secrets = boto3.client("secretsmanager", region_name=REGION)
_cognito = boto3.client("cognito-idp", region_name=REGION)
_lambda = boto3.client("lambda", region_name=REGION, config=Config(read_timeout=27, retries={"max_attempts": 0}))
# Explicit sigv4 so generate_presigned_post always emits browser-usable forms.
_s3 = boto3.client("s3", region_name=REGION, config=Config(signature_version="s3v4"))


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _json_default(o):
    if isinstance(o, Decimal):
        return int(o) if o == int(o) else float(o)
    if isinstance(o, set):
        return sorted(o)
    if isinstance(o, (bytes, bytearray)):
        return ""
    raise TypeError(str(type(o)))


def _resp(status: int, body) -> dict:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, default=_json_default, ensure_ascii=False),
    }


def _claims(event: dict) -> dict:
    claims = ((event.get("requestContext") or {}).get("authorizer") or {}).get("jwt", {}).get("claims", {})
    if not claims.get("sub"):
        raise ApiError(401, "no identity in request context")
    groups_raw = claims.get("cognito:groups", "")
    if isinstance(groups_raw, list):
        groups = [str(g) for g in groups_raw]
    else:
        groups = [g for g in re.split(r"[\[\]\s,]+", str(groups_raw)) if g]
    return {"sub": claims["sub"], "username": claims.get("username", ""), "groups": groups}


def _reject_if_deleted(sub: str) -> None:
    """Block a management request whose caller has been offboarded.

    AdminDeleteUser invalidates the refresh token but NOT an already-issued
    access token — the HTTP API JWT authorizer validates it offline for up to
    its 60-min lifetime and never consults Cognito. _offboard_user writes a
    USER#<sub>/DELETED tombstone first; this fails the residual session closed
    so it cannot mint keys, join repos, or register sources after deletion.
    Fails OPEN on a lookup error: a transient DynamoDB blip must not lock the
    whole platform out, and key/grant revocation is the primary safeguard."""
    try:
        if _platform.get_item(Key={"pk": f"USER#{sub}", "sk": "DELETED"}).get("Item"):
            raise ApiError(403, "this account no longer exists")
    except ApiError:
        raise
    except Exception:
        pass


def _body(event: dict) -> dict:
    raw = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        import base64 as _b64

        raw = _b64.b64decode(raw).decode()
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError
        return parsed
    except ValueError:
        raise ApiError(400, "request body must be a JSON object") from None


def _grants(sub: str, prefix: str) -> list[dict]:
    items, kwargs = [], {}
    while True:
        page = _platform.query(
            KeyConditionExpression="pk = :p AND begins_with(sk, :s)",
            ExpressionAttributeValues={":p": f"USER#{sub}", ":s": prefix},
            **kwargs,
        )
        items.extend(page.get("Items", []))
        if "LastEvaluatedKey" not in page:
            return items
        kwargs = {"ExclusiveStartKey": page["LastEvaluatedKey"]}


def _require_grant(sub: str, repo_id: str) -> dict:
    grant = _platform.get_item(Key={"pk": f"USER#{sub}", "sk": f"REPO#{repo_id}"}).get("Item")
    if not grant:
        raise ApiError(404, f"no such repo in your account: {repo_id}")
    return grant


def _registry_items(repo_ids: list[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for i in range(0, len(repo_ids), 100):
        chunk = repo_ids[i : i + 100]
        resp = _ddb.batch_get_item(RequestItems={REGISTRY_TABLE: {"Keys": [{"repo_id": r} for r in chunk]}})
        for item in resp.get("Responses", {}).get(REGISTRY_TABLE, []):
            out[item["repo_id"]] = item
    return out


def _as_bool(value) -> bool:
    """JSON booleans, plus the usual string spellings; "false"/"0" are False."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    s = str(value or "").strip().lower()
    if s in ("1", "true", "yes", "on"):
        return True
    if s in ("", "0", "false", "no", "off", "none", "null"):
        return False
    raise ApiError(400, f"expected a boolean, got {value!r}")


# LLM document builds run far longer than the 60-min project default (a 4 MB
# corpus is ~1 h cold); an LLM source that has no explicit override gets this.
LLM_BUILD_TIMEOUT_MINUTES = 120
# Markdown-corpus size above which an LLM build falls back to the quick-scan
# (buildspec LLM_CORPUS_CAP_MB); per-source override 1..512 MB.
LLM_CORPUS_CAP_MB_DEFAULT = 64
LLM_CORPUS_CAP_MB_MAX = 512


def _parse_corpus_cap(value) -> int:
    try:
        cap = int(value)
    except (TypeError, ValueError):
        raise ApiError(400, "corpus_cap_mb must be an integer") from None
    if not 1 <= cap <= LLM_CORPUS_CAP_MB_MAX:
        raise ApiError(400, f"corpus_cap_mb must be 1..{LLM_CORPUS_CAP_MB_MAX}")
    return cap


def _parse_llm_model(value) -> str:
    """Validate a Bedrock model id against LLM_MODELS; '' means the default."""
    model = str(value or "").strip()
    if model and model not in LLM_MODELS:
        raise ApiError(400, f"llm_model must be one of: {', '.join(LLM_MODELS)}")
    return model


def _apply_llm_options(item: dict, body: dict) -> None:
    """Registration-time LLM knobs for a document source (url|files).

    llm_images is files-only: the crawler strips <img> tags, so a url source
    never has raster images to send. The default model is stored as ABSENT
    (not as its id) so the buildspec fallback and the url fingerprint stay
    identical for sources that never picked one."""
    if _as_bool(body.get("llm_images")):
        if item.get("source_type") != "files":
            raise ApiError(400, "llm_images applies to files sources only (crawled sites carry no images)")
        item["llm_images"] = "1"
    model = _parse_llm_model(body.get("llm_model"))
    if model and model != LLM_DEFAULT_MODEL:
        item["llm_model"] = model
    if body.get("llm_corpus_cap_mb") is not None:
        cap = _parse_corpus_cap(body["llm_corpus_cap_mb"])
        if cap != LLM_CORPUS_CAP_MB_DEFAULT:
            item["llm_corpus_cap_mb"] = cap
    if item.get("llm_extract") == "1" and not item.get("build_timeout_minutes"):
        item["build_timeout_minutes"] = LLM_BUILD_TIMEOUT_MINUTES


def _repo_view(item: dict) -> dict:
    return {
        "repo_id": item["repo_id"],
        "source_type": item.get("source_type", "git"),
        "git_url": item.get("git_url", ""),
        "ref": item.get("ref", ""),
        "provider": item.get("provider", ""),
        "trigger": item.get("trigger", "poll"),
        "graph_scope": item.get("graph_scope", "public"),
        "enabled": item.get("enabled", "0") == "1",
        "status": item.get("status", ""),
        # Serving-limit facts recorded by the completion Lambda: TOO_LARGE rows
        # carry graph_bytes above the task's cap; has_snapshot=False means the
        # source tarball exceeded the build's cap (no search_code/read_source).
        "graph_bytes": int(item.get("graph_bytes", 0) or 0),
        "has_snapshot": item.get("has_snapshot"),
        "last_built_sha": (item.get("last_built_sha") or "")[:12],
        "last_built_at": item.get("last_built_at", ""),
        "subscriber_count": item.get("subscriber_count", 0),
        "crawl_max_pages": int(item.get("crawl_max_pages", 0) or 0),
        "poll_interval_seconds": int(item.get("poll_interval_seconds", 0) or 0),
        "llm_extract": item.get("llm_extract") == "1",
        "llm_images": item.get("llm_images") == "1",
        "llm_model": item.get("llm_model") or LLM_DEFAULT_MODEL,
        "llm_corpus_cap_mb": int(item.get("llm_corpus_cap_mb", 0) or 0) or LLM_CORPUS_CAP_MB_DEFAULT,
        "created_at": item.get("created_at", ""),
        "runtime_id": item.get("runtime_id", ""),
        "server_id": item["repo_id"],
        "server_name": item.get("server_name", ""),
        "mcp_url": f"{MCP_BASE_URL}/mcp/{item['repo_id']}",
    }


def _start_build(item: dict, head_sha: str) -> dict:
    env = [
        {"name": "REPO_ID", "value": item["repo_id"], "type": "PLAINTEXT"},
        {"name": "GIT_URL", "value": item.get("git_url", ""), "type": "PLAINTEXT"},
        {"name": "TARGET_SHA", "value": head_sha, "type": "PLAINTEXT"},
        {"name": "GRAPH_BUCKET", "value": GRAPH_BUCKET, "type": "PLAINTEXT"},
        {"name": "PROVIDER", "value": item.get("provider", "github"), "type": "PLAINTEXT"},
        {"name": "GIT_REF", "value": item.get("ref", ""), "type": "PLAINTEXT"},
        # Space-separated repo-relative dirs to drop before extraction (shrinks
        # an oversized graph so the runtime doesn't OOM). Empty by default.
        {"name": "PRUNE_PATHS", "value": item.get("prune_paths", ""), "type": "PLAINTEXT"},
        # Branches the buildspec: git (default) | files (S3 uploads) | url (crawl).
        {"name": "SOURCE_TYPE", "value": item.get("source_type", "git"), "type": "PLAINTEXT"},
        # Document sources only: "1" routes markdown through the Bedrock
        # Sonnet 5 semantic pass instead of the no-LLM quick-scan.
        {"name": "LLM_EXTRACT", "value": "1" if item.get("llm_extract") == "1" else "0", "type": "PLAINTEXT"},
        # Same contract: raster images through the vision path, and the
        # Bedrock model id (empty = the buildspec default).
        {"name": "LLM_IMAGES", "value": "1" if item.get("llm_images") == "1" else "0", "type": "PLAINTEXT"},
        {"name": "LLM_MODEL", "value": item.get("llm_model", ""), "type": "PLAINTEXT"},
        {"name": "LLM_CORPUS_CAP_MB", "value": str(item.get("llm_corpus_cap_mb", "") or ""), "type": "PLAINTEXT"},
    ]
    if item.get("source_type") == "url":
        env += [
            {"name": "SOURCE_URL", "value": item.get("git_url", ""), "type": "PLAINTEXT"},
            {"name": "CRAWL_MAX_PAGES", "value": str(item.get("crawl_max_pages", 200)), "type": "PLAINTEXT"},
        ]
    if item.get("auth_secret_name"):
        env.append({"name": "GIT_TOKEN", "value": f"{item['auth_secret_name']}:token", "type": "SECRETS_MANAGER"})
    # Per-repo build sizing, as the poller/webhook forward it (a console
    # rebuild of a big repo must not silently drop to the default tier).
    kwargs = {"projectName": PROJECT_NAME, "environmentVariablesOverride": env}
    if item.get("build_compute"):
        kwargs["computeTypeOverride"] = item["build_compute"]
    if item.get("build_timeout_minutes"):
        kwargs["timeoutInMinutesOverride"] = int(item["build_timeout_minutes"])
    build = _codebuild.start_build(**kwargs)["build"]
    _registry.update_item(
        Key={"repo_id": item["repo_id"]},
        # build_arn is load-bearing: the completion Lambda's identity guard
        # only applies terminal state when the event's ARN matches it.
        UpdateExpression="SET #s = :b, build_id = :bid, build_arn = :arn, build_started_at = :now",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":b": "BUILDING", ":bid": build["id"], ":arn": build["arn"], ":now": int(time.time())},
    )
    return build


def _parse_prune_paths(body: dict) -> str:
    # Optional non-core dirs to drop before extraction (shrinks an oversized
    # graph so the runtime doesn't OOM). Repo-relative, no absolute/.. paths.
    prune_raw = body.get("prune_paths", [])
    if isinstance(prune_raw, str):
        prune_raw = prune_raw.split()
    if not isinstance(prune_raw, list):
        raise ApiError(400, "prune_paths must be a list of repo-relative dirs")
    prune_paths = []
    for p in prune_raw:
        p = str(p).strip().strip("/")
        if not p or p.startswith("/") or ".." in p.split("/"):
            raise ApiError(400, f"invalid prune path: {p!r}")
        prune_paths.append(p)
    return " ".join(sorted(set(prune_paths)))[:1000]


_SERVER_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,47}")


def _parse_server_name(body: dict) -> str:
    """Optional custom MCP-server name used in the console's connect commands
    (`claude mcp add <name> …`). Command-safe token, no spaces; empty means
    fall back to the console's derived graphify-<slug> name."""
    raw = str(body.get("server_name", "")).strip()
    if not raw:
        return ""
    if not _SERVER_NAME_RE.fullmatch(raw):
        raise ApiError(400, "server_name must be 1-48 chars of letters, digits, . _ - (no spaces)")
    return raw


def _parse_task_size(body: dict) -> tuple[int, int]:
    # Optional Fargate task sizing for the repo's dedicated MCP service —
    # bump memory for repos whose graph needs more resident RAM.
    try:
        service_cpu = int(body.get("service_cpu") or runtimes.DEFAULT_CPU)
        service_memory = int(body.get("service_memory") or runtimes.DEFAULT_MEMORY)
        runtimes.validate_task_size(service_cpu, service_memory)
    except (TypeError, ValueError) as exc:
        raise ApiError(400, f"invalid task size: {exc}") from None
    return service_cpu, service_memory


def _join_enabled_repo(sub: str, repo_id: str, git_url: str, ref: str) -> dict:
    """Idempotent pooled join of an ENABLED PUBLIC registry row."""
    if _platform.get_item(Key={"pk": f"USER#{sub}", "sk": f"REPO#{repo_id}"}).get("Item"):
        return _resp(200, {"repo_id": repo_id, "joined": True, "already_registered": True})
    # NB: a transaction may touch each item only once, so the "repo still
    # exists, is enabled AND IS STILL PUBLIC" check rides as the Update's
    # condition. graph_scope is load-bearing: scopes are mutable now, so a
    # row flipped private between the caller's read and this write must fail
    # closed — a join would otherwise mint a foreign grant on a private
    # source (the grant is exactly what the data-plane authorizer honors).
    try:
        _ddbc.transact_write_items(TransactItems=[
            {"Put": {
                "TableName": PLATFORM_TABLE,
                "Item": _grant_item_typed(sub, repo_id, git_url, ref, "public"),
                "ConditionExpression": "attribute_not_exists(pk)",
            }},
            {"Update": {
                "TableName": REGISTRY_TABLE,
                "Key": {"repo_id": {"S": repo_id}},
                "UpdateExpression": "ADD subscriber_count :inc",
                "ConditionExpression": "attribute_exists(repo_id) AND enabled = :one AND graph_scope = :pub",
                "ExpressionAttributeValues": {":inc": {"N": "1"}, ":one": {"S": "1"}, ":pub": {"S": "public"}},
            }},
        ])
    except _ddbc.exceptions.TransactionCanceledException:
        # A concurrent join by the same user (grant Put condition failed)
        # is the idempotent success; a registry condition failure means
        # it was disabled out from under us — retryable as a revive.
        if _platform.get_item(Key={"pk": f"USER#{sub}", "sk": f"REPO#{repo_id}"}).get("Item"):
            return _resp(200, {"repo_id": repo_id, "joined": True, "already_registered": True})
        raise ApiError(409, f"{repo_id} changed state concurrently; retry") from None
    return _resp(200, {"repo_id": repo_id, "joined": True, "pooled": True,
                       "mcp_url": f"{MCP_BASE_URL}/mcp/{repo_id}"})


def _ensure_runtime_info(repo_id: str, item: dict) -> dict:
    """Best-effort dedicated-service creation; registration stays valid on failure."""
    try:
        rt = runtimes.ensure_repo_runtime(
            repo_id, RUNTIME_ENV,
            cpu=int(item.get("service_cpu", 0) or runtimes.DEFAULT_CPU),
            memory=int(item.get("service_memory", 0) or runtimes.DEFAULT_MEMORY),
        )
        _registry.update_item(
            Key={"repo_id": repo_id},
            UpdateExpression="SET runtime_name = :n, runtime_arn = :a, runtime_id = :i",
            ExpressionAttributeValues={":n": rt["runtime_name"], ":a": rt["runtime_arn"], ":i": rt["runtime_id"]},
        )
        return {"runtime_id": rt["runtime_id"], "runtime_status": rt["status"]}
    except Exception as exc:  # console retries via rebuild/status
        return {"runtime_error": f"{type(exc).__name__}: {exc}"}


_FILES_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,39}")

_SYNC_EXCLUDES = (
    '--exclude ".git/*" --exclude ".venv/*" --exclude "node_modules/*" '
    '--exclude "__pycache__/*" --exclude ".DS_Store"'
)


def _sync_command(s3_url: str) -> str:
    return f"aws s3 sync <local-folder> {s3_url} --delete {_SYNC_EXCLUDES}"


def _register_files_repo(sub: str, body: dict) -> dict:
    """files source: a per-repo S3 upload prefix the user syncs documents to.

    SILOED per user (the repo_id embeds the sub, graph_scope=private):
    uploads are user content, so they never join the shared hub graph and the
    data plane demands the owner's grant. No first build is started — the
    poller's files_manifest_hash change detection builds once the first sync
    lands under the upload prefix.
    """
    name = str(body.get("name", "")).strip().lower()
    if not _FILES_NAME_RE.fullmatch(name):
        raise ApiError(400, "name must be 1-40 chars of [a-z0-9_-]")
    requested_scope = str(body.get("graph_scope") or "private")
    if requested_scope not in ("public", "private"):
        raise ApiError(400, "graph_scope must be public|private")
    prune_paths_str = _parse_prune_paths(body)
    service_cpu, service_memory = _parse_task_size(body)
    try:
        poll_interval = int(body.get("poll_interval") or 900)
    except (TypeError, ValueError):
        raise ApiError(400, "poll_interval must be an integer") from None
    if not 60 <= poll_interval <= URL_POLL_MAX:
        raise ApiError(400, f"poll_interval must be 60..{URL_POLL_MAX} seconds")

    repo_id = f"files__{name}__u{sub.replace('-', '')[:8]}"
    upload_prefix = f"uploads/{repo_id}/"
    s3_url = f"s3://{GRAPH_BUCKET}/{upload_prefix}"

    existing = _registry.get_item(Key={"repo_id": repo_id}).get("Item")
    if existing and existing.get("enabled") == "1":
        # The id embeds the caller's sub, so an enabled row is their own silo;
        # re-put the grant (idempotent) in case an earlier attempt half-failed.
        _ddbc.put_item(TableName=PLATFORM_TABLE,
                       Item=_grant_item_typed(sub, repo_id, s3_url, "", "private"))
        return _resp(200, {"repo_id": repo_id, "already_registered": True,
                           "upload_prefix": s3_url, "sync_command": _sync_command(s3_url),
                           "mcp_url": f"{MCP_BASE_URL}/mcp/{repo_id}"})
    revive = bool(existing)

    now = int(time.time())
    iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
    item = {
        "repo_id": repo_id, "git_url": s3_url, "provider": "s3", "ref": "",
        "source_type": "files",
        "enabled": "1", "trigger": "poll", "dedicated_runtime": "1",
        "graph_scope": requested_scope, "created_by_sub": sub, "subscriber_count": 1,
        "poll_interval_seconds": poll_interval,
        # First check soon after registration so the first sync builds quickly.
        "next_poll_at": now + 120,
        "status": "REGISTERED",
        "service_cpu": service_cpu, "service_memory": service_memory,
        "created_at": iso, "updated_at": iso,
    }
    if prune_paths_str:
        item["prune_paths"] = prune_paths_str
    server_name = _parse_server_name(body)
    if server_name:
        item["server_name"] = server_name
    if body.get("llm_extract"):
        item["llm_extract"] = "1"
    _apply_llm_options(item, body)
    registry_put = {"TableName": REGISTRY_TABLE, "Item": _to_typed(item)}
    if revive:
        registry_put["ConditionExpression"] = "enabled = :zero"
        registry_put["ExpressionAttributeValues"] = {":zero": {"S": "0"}}
    else:
        registry_put["ConditionExpression"] = "attribute_not_exists(repo_id)"
    try:
        _ddbc.transact_write_items(TransactItems=[
            {"Put": registry_put},
            {"Put": {"TableName": PLATFORM_TABLE,
                     "Item": _grant_item_typed(sub, repo_id, s3_url, "", requested_scope)}},
        ])
    except _ddbc.exceptions.TransactionCanceledException:
        raise ApiError(409, f"{repo_id} changed state concurrently; retry") from None

    # Folder marker so the prefix is browsable before the first sync.
    _s3.put_object(Bucket=GRAPH_BUCKET, Key=upload_prefix, Body=b"")

    runtime_info = _ensure_runtime_info(repo_id, item)
    return _resp(201, {
        "repo_id": repo_id, "source_type": "files", "graph_scope": requested_scope,
        "joined": False, "upload_prefix": s3_url,
        "sync_command": _sync_command(s3_url),
        "note": f"sync files to the upload prefix; the poller detects changes and rebuilds (interval {poll_interval}s)",
        "mcp_url": f"{MCP_BASE_URL}/mcp/{repo_id}", **runtime_info,
    })


def _register_url_repo(sub: str, body: dict) -> dict:
    """url source: a public docs site crawled into markdown on the build plane.

    Public POOLED like a public git repo (a second registration joins). The
    poller starts a crawl-build every poll interval; the build skips itself
    when the crawled content hash matches the published source_hash.
    """
    url = str(body.get("url") or body.get("git_url") or "").strip().rstrip("/")
    if not re.match(r"^https://[^\s/]+\.[^\s/]+(/[^\s]*)?$", url):
        raise ApiError(400, "url must be a public https docs URL")
    try:
        max_pages = int(body.get("max_pages") or 200)
    except (TypeError, ValueError):
        raise ApiError(400, "max_pages must be an integer") from None
    if not 1 <= max_pages <= 500:
        raise ApiError(400, "max_pages must be 1..500")
    prune_paths_str = _parse_prune_paths(body)
    service_cpu, service_memory = _parse_task_size(body)
    # Every due poll starts a crawl-build, so the floor sits above git's.
    try:
        poll_interval = int(body.get("poll_interval") or 21600)
    except (TypeError, ValueError):
        raise ApiError(400, "poll_interval must be an integer") from None
    if not 900 <= poll_interval <= URL_POLL_MAX:
        raise ApiError(400, f"poll_interval must be 900..{URL_POLL_MAX} seconds for url sources (each poll crawls)")

    requested_scope = str(body.get("graph_scope") or "public")
    if requested_scope not in ("public", "private"):
        raise ApiError(400, "graph_scope must be public|private")
    private = requested_scope == "private"

    repo_id = gitreg.make_url_repo_id(url)
    if private:
        # Siloed like a PAT git repo: own row, own graph, never hub-merged.
        repo_id = f"{repo_id}__u{sub.replace('-', '')[:8]}"
    existing = _registry.get_item(Key={"repo_id": repo_id}).get("Item")
    revive = False
    if existing:
        same = existing.get("git_url") == url and existing.get("source_type") == "url"
        # Mirror the git path: joining requires the existing row to actually
        # BE public — a url source flipped private keeps its pooled-shape id,
        # and a public re-registration of that id must 409, never join.
        pooled = existing.get("graph_scope", "public") == "public" and not private
        own_silo = private and existing.get("created_by_sub") == sub
        if not (same and (pooled or own_silo)):
            raise ApiError(409, f"repo_id collision: {repo_id} already registered differently")
        if existing.get("enabled") != "1":
            revive = True
        elif private:
            # The id embeds the caller's sub — an enabled row is their own.
            _ddbc.put_item(TableName=PLATFORM_TABLE,
                           Item=_grant_item_typed(sub, repo_id, url, "", "private"))
            return _resp(200, {"repo_id": repo_id, "already_registered": True,
                               "mcp_url": f"{MCP_BASE_URL}/mcp/{repo_id}"})
        else:
            return _join_enabled_repo(sub, repo_id, url, "")

    now = int(time.time())
    iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
    item = {
        "repo_id": repo_id, "git_url": url, "provider": "url", "ref": "",
        "source_type": "url", "crawl_max_pages": max_pages,
        "enabled": "1", "trigger": "poll", "dedicated_runtime": "1",
        "graph_scope": requested_scope, "created_by_sub": sub, "subscriber_count": 1,
        "poll_interval_seconds": poll_interval, "next_poll_at": now + poll_interval,
        "status": "REGISTERED",
        "service_cpu": service_cpu, "service_memory": service_memory,
        "created_at": iso, "updated_at": iso,
    }
    if prune_paths_str:
        item["prune_paths"] = prune_paths_str
    server_name = _parse_server_name(body)
    if server_name:
        item["server_name"] = server_name
    if body.get("llm_extract"):
        item["llm_extract"] = "1"
    _apply_llm_options(item, body)
    registry_put = {"TableName": REGISTRY_TABLE, "Item": _to_typed(item)}
    if revive:
        registry_put["ConditionExpression"] = "enabled = :zero"
        registry_put["ExpressionAttributeValues"] = {":zero": {"S": "0"}}
    else:
        registry_put["ConditionExpression"] = "attribute_not_exists(repo_id)"
    try:
        _ddbc.transact_write_items(TransactItems=[
            {"Put": registry_put},
            {"Put": {"TableName": PLATFORM_TABLE,
                     "Item": _grant_item_typed(sub, repo_id, url, "", requested_scope)}},
        ])
    except _ddbc.exceptions.TransactionCanceledException:
        raise ApiError(409, f"{repo_id} was registered concurrently; retry to join it") from None

    runtime_info = _ensure_runtime_info(repo_id, item)
    build = _start_build(item, f"crawl-{now}")
    return _resp(201, {
        "repo_id": repo_id, "source_type": "url", "graph_scope": requested_scope,
        "joined": False, "crawl_max_pages": max_pages, "build_id": build["id"],
        "mcp_url": f"{MCP_BASE_URL}/mcp/{repo_id}", **runtime_info,
    })


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------

def get_me(event, ident, _params):
    repos = _grants(ident["sub"], "REPO#")
    keys = _grants(ident["sub"], "KEY#")
    return _resp(200, {
        "sub": ident["sub"],
        "username": ident["username"],
        "groups": ident["groups"],
        "is_admin": "admin" in ident["groups"],
        "repo_count": len(repos),
        "key_count": len(keys),
        "mcp_base_url": MCP_BASE_URL,
        "hub_server_id": "all",
        "llm_models": list(LLM_MODELS),
        "llm_default_model": LLM_DEFAULT_MODEL,
        "llm_corpus_cap_mb_default": LLM_CORPUS_CAP_MB_DEFAULT,
    })


def _scope_editable(item: dict, sub: str) -> bool:
    """Whether THIS caller may change the row's graph_scope (mirrors
    change_scope's guards, minus the transient subscriber-count check so the
    button shows and the server explains a 409)."""
    return bool(item.get("created_by_sub") == sub and not item.get("auth_secret_name"))


def _can_manage(item: dict, ident: dict) -> bool:
    """Who may edit a row's cosmetic/operational settings (server name, crawl
    config): its creator, OR an admin for an OWNERLESS row (CLI/operator
    registrations carry no created_by_sub, so otherwise no one could name or
    tune them from the console). Never lets an admin override another USER's
    owned source."""
    if item.get("created_by_sub") == ident["sub"]:
        return True
    return not item.get("created_by_sub") and "admin" in ident["groups"]


def list_repos(event, ident, _params):
    grants = _grants(ident["sub"], "REPO#")
    repo_ids = [g["sk"].removeprefix("REPO#") for g in grants]
    reg = _registry_items(repo_ids)
    views = []
    for r in repo_ids:
        if r not in reg:
            continue
        v = _repo_view(reg[r])
        v["scope_editable"] = _scope_editable(reg[r], ident["sub"])
        v["owned"] = reg[r].get("created_by_sub") == ident["sub"]
        v["manageable"] = _can_manage(reg[r], ident)
        views.append(v)
    return _resp(200, {"repos": views})


def get_repo(event, ident, params):
    repo_id = params["repoId"]
    _require_grant(ident["sub"], repo_id)
    item = _registry.get_item(Key={"repo_id": repo_id}).get("Item")
    if not item:
        raise ApiError(404, "registry row missing")
    view = _repo_view(item)
    view["scope_editable"] = _scope_editable(item, ident["sub"])
    view["owned"] = item.get("created_by_sub") == ident["sub"]
    view["manageable"] = _can_manage(item, ident)
    if item.get("runtime_id"):
        view["runtime_status"] = runtimes.runtime_status(item["runtime_id"])
    view["build_id"] = item.get("build_id", "")
    view["last_error"] = item.get("last_error", "")
    return _resp(200, view)


def register_repo(event, ident, _params):
    sub = ident["sub"]
    body = _body(event)
    source_type = str(body.get("source_type") or "git")
    if source_type == "files":
        return _register_files_repo(sub, body)
    if source_type == "url":
        return _register_url_repo(sub, body)
    if source_type != "git":
        raise ApiError(400, "source_type must be git|url|files")
    if body.get("llm_extract"):
        # LLM semantic extraction is a DOCUMENT-corpus feature; code repos
        # keep the deterministic AST-only build.
        raise ApiError(400, "llm_extract applies to url|files sources only")
    if body.get("llm_images") or body.get("llm_model"):
        raise ApiError(400, "llm_images/llm_model apply to url|files sources only")
    git_url = str(body.get("git_url", "")).strip().rstrip("/")
    if not re.match(r"^https://[^\s]+$", git_url):
        raise ApiError(400, "git_url must be an https clone URL")
    # Normalize the same way make_repo_id derives the id (strip a trailing
    # .git), so "…/tools.git" and "…/tools" pool onto one repo instead of the
    # second subscriber hitting a permanent collision 409.
    if git_url.endswith(".git"):
        git_url = git_url[:-4]
    provider = body.get("provider") or gitreg.detect_provider(git_url)
    if provider not in ("github", "gitlab", "bitbucket", "generic"):
        raise ApiError(400, "provider must be github|gitlab|bitbucket|generic")
    trigger = body.get("trigger", "poll")
    if trigger not in ("poll", "webhook"):
        raise ApiError(400, "trigger must be poll|webhook")
    if trigger == "webhook" and provider != "github":
        raise ApiError(400, "webhook trigger supports GitHub push payloads only")
    prune_paths_str = _parse_prune_paths(body)
    service_cpu, service_memory = _parse_task_size(body)
    # Validate the optional name up front — before the PAT secret is written —
    # so a bad name 400s without leaving an orphaned secret behind.
    server_name = _parse_server_name(body)

    auth_token = str(body.get("auth_token", "")).strip()
    requested_scope = str(body.get("graph_scope") or "")
    if requested_scope not in ("", "public", "private"):
        raise ApiError(400, "graph_scope must be public|private")
    if auth_token and requested_scope == "public":
        # Fail-closed tenancy: a PAT-cloned repo's content must never join the
        # shared hub graph or open to every member's key.
        raise ApiError(400, "a PAT-cloned repo cannot be public; remove the PAT or keep it private")
    # PAT forces private; a public repo may also be registered private (siloed
    # to this account, excluded from the hub) by explicit choice.
    private = bool(auth_token) or requested_scope == "private"
    if trigger == "webhook" and private:
        # A private repo gets a __u<sub8>-suffixed repo_id the webhook Lambda
        # (which derives the id from owner/repo/branch only) can never match,
        # so every push would be silently ignored. Poll it instead.
        raise ApiError(400, "webhook trigger is unavailable for private (siloed) repos; use polling")

    try:
        ref, head_sha = gitreg.resolve_ref_and_sha(git_url, str(body.get("ref", "")).strip(), provider, auth_token)
    except Exception as exc:
        raise ApiError(422, f"could not resolve repo/ref: {exc}") from None

    repo_id = gitreg.make_repo_id(git_url, ref)
    if private:
        repo_id = f"{repo_id}__u{sub.replace('-', '')[:8]}"

    existing = _registry.get_item(Key={"repo_id": repo_id}).get("Item")
    revive = False
    if existing:
        same = existing.get("git_url") == git_url and existing.get("ref") == ref
        pooled = existing.get("graph_scope", "public") == "public" and not private
        own_silo = private and existing.get("created_by_sub") == sub
        if not (same and (pooled or own_silo)):
            raise ApiError(409, f"repo_id collision: {repo_id} already registered differently")
        if existing.get("enabled") != "1":
            # Torn down after its last subscriber left — revive instead of
            # refusing, or the repo_id would be permanently unusable.
            revive = True
        else:
            return _join_enabled_repo(sub, repo_id, git_url, ref)

    # Brand-new registration (or revival of a torn-down row). The clone
    # secret exists only when a PAT was supplied — a private-by-choice
    # registration of a public repo clones anonymously.
    secret_name = ""
    if auth_token:
        secret_name = f"graphify/pat/{repo_id}"
        payload = json.dumps({"token": auth_token})
        try:
            _secrets.create_secret(Name=secret_name, SecretString=payload,
                                   Description=f"graphify clone PAT for {repo_id}")
        except _secrets.exceptions.ResourceExistsException:
            _secrets.put_secret_value(SecretId=secret_name, SecretString=payload)

    now = int(time.time())
    iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
    poll_interval = int(body.get("poll_interval") or (21600 if trigger == "webhook" else 900))
    if not 60 <= poll_interval <= URL_POLL_MAX:
        raise ApiError(400, f"poll_interval must be 60..{URL_POLL_MAX} seconds")
    item = {
        "repo_id": repo_id, "git_url": git_url, "provider": provider, "ref": ref,
        "enabled": "1", "trigger": trigger, "dedicated_runtime": "1",
        "graph_scope": "private" if private else "public",
        "created_by_sub": sub, "subscriber_count": 1,
        "poll_interval_seconds": poll_interval, "next_poll_at": now + poll_interval,
        "last_seen_sha": head_sha, "status": "REGISTERED",
        "service_cpu": service_cpu, "service_memory": service_memory,
        "created_at": iso, "updated_at": iso,
    }
    if prune_paths_str:
        item["prune_paths"] = prune_paths_str
    if secret_name:
        item["auth_secret_name"] = secret_name
    if server_name:
        item["server_name"] = server_name
    registry_put = {
        "TableName": REGISTRY_TABLE,
        "Item": _to_typed(item),
    }
    if revive:
        # Overwrite the disabled row, but only while it is still disabled —
        # a concurrent revival loses cleanly instead of double-registering.
        registry_put["ConditionExpression"] = "enabled = :zero"
        registry_put["ExpressionAttributeValues"] = {":zero": {"S": "0"}}
    else:
        registry_put["ConditionExpression"] = "attribute_not_exists(repo_id)"
    try:
        _ddbc.transact_write_items(TransactItems=[
            {"Put": registry_put},
            {"Put": {
                "TableName": PLATFORM_TABLE,
                "Item": _grant_item_typed(sub, repo_id, git_url, ref, item["graph_scope"]),
            }},
        ])
    except _ddbc.exceptions.TransactionCanceledException:
        raise ApiError(409, f"{repo_id} was registered concurrently; retry to join it") from None

    runtime_info = _ensure_runtime_info(repo_id, item)
    build = _start_build(item, head_sha)
    out = {
        "repo_id": repo_id, "ref": ref, "head_sha": head_sha[:12], "joined": False,
        "graph_scope": item["graph_scope"], "build_id": build["id"],
        "mcp_url": f"{MCP_BASE_URL}/mcp/{repo_id}", **runtime_info,
    }
    if trigger == "webhook":
        out["webhook"] = {"payload_url": WEBHOOK_URL, "content_type": "application/json",
                          "events": ["push"], "secret_hint": "GET /webhook-info returns the shared secret"}
    return _resp(201, out)


def _to_typed(item: dict) -> dict:
    typed = {}
    for k, v in item.items():
        if isinstance(v, str):
            typed[k] = {"S": v}
        elif isinstance(v, bool):
            typed[k] = {"BOOL": v}
        elif isinstance(v, (int, float, Decimal)):
            typed[k] = {"N": str(v)}
        elif isinstance(v, bytes):
            typed[k] = {"B": v}
        else:
            raise TypeError(f"unsupported attr {k}: {type(v)}")
    return typed


def _grant_item_typed(sub: str, repo_id: str, git_url: str, ref: str, scope: str) -> dict:
    iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return _to_typed({
        "pk": f"USER#{sub}", "sk": f"REPO#{repo_id}",
        "gsi1pk": f"REPO#{repo_id}", "gsi1sk": f"USER#{sub}",
        "git_url": git_url, "ref": ref, "graph_scope": scope, "created_at": iso,
    })


def _teardown_source(repo_id: str, item: dict, cloudmap_retry: bool = True) -> dict:
    """Destroy a source its creator owns: delete EVERY grant (creator + all
    members), disable the registry row, and delete the Fargate service.

    Used when the creator deletes a PRIVATE source. Members reach a private
    source only through a grant the creator minted, so a plain "leave" that
    left subscriber_count > 0 would strand them with live, still-rebuilding
    access to a row nobody can manage or reclaim (its id collides on
    re-registration, so the creator can never recover it — the 409-forever
    trap the review found). Private rows take no concurrent joins (join_repo
    404s on them, add_member is creator-only), so a non-transactional cascade
    is safe: no join can race in and be clobbered.
    """
    grants = _repo_grants(repo_id)
    with _platform.batch_writer() as bw:
        for g in grants:
            bw.delete_item(Key={"pk": g["pk"], "sk": g["sk"]})
    try:
        _registry.update_item(
            Key={"repo_id": repo_id},
            UpdateExpression="SET enabled = :z, subscriber_count = :zero, updated_at = :t",
            ConditionExpression="enabled = :one",
            ExpressionAttributeValues={
                ":z": "0", ":one": "1", ":zero": 0,
                ":t": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            },
        )
        try:
            runtimes.delete_repo_runtime(repo_id, item.get("runtime_id", ""), cloudmap_retry=cloudmap_retry)
        except Exception:
            pass
    except _registry.meta.client.exceptions.ConditionalCheckFailedException:
        pass  # already disabled by a concurrent delete
    return _resp(200, {"repo_id": repo_id, "remaining_subscribers": 0,
                       "torn_down": True, "removed_grants": len(grants)})


def delete_repo(event, ident, params):
    sub, repo_id = ident["sub"], params["repoId"]
    _require_grant(sub, repo_id)
    reg = _registry.get_item(Key={"repo_id": repo_id}).get("Item") or {}
    # The creator deleting a PRIVATE source destroys it for everyone (cascade
    # every grant + the service). A private source's members exist only by the
    # creator's grant, so "creator leaves but members keep access" is
    # incoherent — and the old leave path left exactly that: an orphaned,
    # un-tear-down-able row the creator was locked out of. Public pooled rows
    # are shared infrastructure whose "creator" is only the first registrant,
    # so there the creator merely leaves (teardown still happens at count 0).
    if (reg.get("created_by_sub") == sub
            and reg.get("graph_scope", "public") != "public"
            and reg.get("enabled") == "1"):
        return _teardown_source(repo_id, reg)
    # Grant delete + decrement in one transaction, with the delete conditional
    # on the grant still existing — a duplicate/concurrent DELETE cancels
    # instead of decrementing twice (which would tear a repo out from under
    # other subscribers).
    try:
        _ddbc.transact_write_items(TransactItems=[
            {"Delete": {
                "TableName": PLATFORM_TABLE,
                "Key": {"pk": {"S": f"USER#{sub}"}, "sk": {"S": f"REPO#{repo_id}"}},
                "ConditionExpression": "attribute_exists(pk)",
            }},
            {"Update": {
                "TableName": REGISTRY_TABLE,
                "Key": {"repo_id": {"S": repo_id}},
                "UpdateExpression": "ADD subscriber_count :neg",
                # ADD on a missing item CREATES it — guard so a DELETE against
                # an already-purged registry row can't resurrect a phantom.
                "ConditionExpression": "attribute_exists(repo_id)",
                "ExpressionAttributeValues": {":neg": {"N": "-1"}},
            }},
        ])
    except _ddbc.exceptions.TransactionCanceledException:
        raise ApiError(404, f"no such repo in your account: {repo_id}") from None

    # ConsistentRead: TransactWriteItems returns no values, and a stale replica
    # read here would skip the teardown of a repo that just hit 0 subscribers.
    fresh = _registry.get_item(Key={"repo_id": repo_id}, ConsistentRead=True).get("Item") or {}
    remaining = int(fresh.get("subscriber_count", 0))
    torn_down = False
    if remaining <= 0:
        # Teardown is conditional: a join that raced in between (count back
        # above 0, or already re-enabled) must not have its runtime deleted.
        try:
            _registry.update_item(
                Key={"repo_id": repo_id},
                UpdateExpression="SET enabled = :z, updated_at = :t",
                ConditionExpression="subscriber_count <= :zero AND enabled = :one",
                ExpressionAttributeValues={
                    ":z": "0", ":one": "1", ":zero": 0,
                    ":t": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                },
            )
            try:
                runtimes.delete_repo_runtime(repo_id, fresh.get("runtime_id", ""))
            except Exception:
                pass
            torn_down = True
        except _registry.meta.client.exceptions.ConditionalCheckFailedException:
            remaining = int((_registry.get_item(Key={"repo_id": repo_id}).get("Item") or {}).get("subscriber_count", 0))
    return _resp(200, {"repo_id": repo_id, "remaining_subscribers": max(remaining, 0), "torn_down": torn_down})


def set_server_name(event, ident, params):
    """Set or clear a source's custom MCP-server name (creator-only).

    The name only labels the console's connect commands and server list; an
    empty value clears it back to the derived graphify-<slug>. Creator-gated
    like change_scope so a pooled row's shared name is the creator's call."""
    sub, repo_id = ident["sub"], params["repoId"]
    _require_grant(sub, repo_id)
    item = _registry.get_item(Key={"repo_id": repo_id}).get("Item")
    if not item or item.get("enabled") != "1":
        raise ApiError(404, "repo not found or disabled")
    if not _can_manage(item, ident):
        raise ApiError(403, "only the source's creator (or an admin, for an ownerless source) can rename its server")
    name = _parse_server_name(_body(event))
    iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    if name:
        _registry.update_item(
            Key={"repo_id": repo_id},
            UpdateExpression="SET server_name = :n, updated_at = :t",
            ExpressionAttributeValues={":n": name, ":t": iso})
    else:
        _registry.update_item(
            Key={"repo_id": repo_id},
            UpdateExpression="REMOVE server_name SET updated_at = :t",
            ExpressionAttributeValues={":t": iso})
    return _resp(200, {"repo_id": repo_id, "server_name": name})


def set_crawl_config(event, ident, params):
    """Adjust a url source's crawl settings after creation (max pages, and
    optionally the re-crawl interval). Manageable by the creator (or an admin
    for an ownerless row). The change takes effect on the next crawl-build —
    the poller reads crawl_max_pages / poll_interval_seconds off the row."""
    sub, repo_id = ident["sub"], params["repoId"]
    _require_grant(sub, repo_id)
    item = _registry.get_item(Key={"repo_id": repo_id}).get("Item")
    if not item or item.get("enabled") != "1":
        raise ApiError(404, "repo not found or disabled")
    if item.get("source_type") != "url":
        raise ApiError(400, "crawl settings apply to url sources only")
    if not _can_manage(item, ident):
        raise ApiError(403, "only the source's creator (or an admin, for an ownerless source) can change crawl settings")
    body = _body(event)
    sets, values = [], {":t": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    if body.get("max_pages") is not None:
        try:
            mp = int(body["max_pages"])
        except (TypeError, ValueError):
            raise ApiError(400, "max_pages must be an integer") from None
        if not 1 <= mp <= 500:
            raise ApiError(400, "max_pages must be 1..500")
        sets.append("crawl_max_pages = :mp")
        values[":mp"] = mp
    if body.get("poll_interval") is not None:
        try:
            pi = int(body["poll_interval"])
        except (TypeError, ValueError):
            raise ApiError(400, "poll_interval must be an integer") from None
        if not 900 <= pi <= URL_POLL_MAX:
            raise ApiError(400, f"poll_interval must be 900..{URL_POLL_MAX} seconds")
        sets.append("poll_interval_seconds = :pi")
        values[":pi"] = pi
        # Reschedule so a SHORTER interval takes effect now instead of waiting
        # out an already-materialized far-future tick; a longer one never
        # strands the row (min keeps the sooner of the two).
        now = int(time.time())
        existing_npa = int(item.get("next_poll_at", 0) or 0)
        sets.append("next_poll_at = :npa")
        values[":npa"] = min(existing_npa, now + pi) if existing_npa else now + pi
    if not sets:
        raise ApiError(400, "provide max_pages and/or poll_interval")
    _registry.update_item(
        Key={"repo_id": repo_id},
        UpdateExpression="SET " + ", ".join(sets) + ", updated_at = :t",
        ExpressionAttributeValues=values)
    fresh = _registry.get_item(Key={"repo_id": repo_id}).get("Item") or {}
    return _resp(200, {"repo_id": repo_id,
                       "crawl_max_pages": int(fresh.get("crawl_max_pages", 0) or 0),
                       "poll_interval_seconds": int(fresh.get("poll_interval_seconds", 0) or 0),
                       "note": "takes effect on the next crawl (use 재빌드 to apply now)"})


def set_llm_extract(event, ident, params):
    """Configure the Bedrock semantic pass for a DOCUMENT source.

    Body keys are optional and independent — an omitted key keeps its current
    value, so the console can flip one knob without re-sending the others:
      enabled: bool   — LLM extraction on/off (off = deterministic quick-scan)
      images:  bool   — send raster images through the vision path (files only)
      model:   str    — one of LLM_MODELS (default when omitted/empty)
    Gated like crawl settings (creator, or admin for an ownerless row) and
    url|files only. A rebuild starts right away whenever the resulting graph
    would differ (the pass was toggled, or it is on and images/model changed)
    — without it the change would sit inert until the next content change."""
    sub, repo_id = ident["sub"], params["repoId"]
    _require_grant(sub, repo_id)
    item = _registry.get_item(Key={"repo_id": repo_id}).get("Item")
    if not item or item.get("enabled") != "1":
        raise ApiError(404, "repo not found or disabled")
    if item.get("source_type") not in ("url", "files"):
        raise ApiError(400, "llm_extract applies to url|files sources only")
    if not _can_manage(item, ident):
        raise ApiError(403, "only the source's creator (or an admin, for an ownerless source) can change LLM extraction")
    body = _body(event)
    cur = {
        "enabled": item.get("llm_extract") == "1",
        "images": item.get("llm_images") == "1",
        "model": item.get("llm_model") or LLM_DEFAULT_MODEL,
        "corpus_cap_mb": int(item.get("llm_corpus_cap_mb", 0) or 0) or LLM_CORPUS_CAP_MB_DEFAULT,
    }
    new = dict(cur)
    if "enabled" in body:
        new["enabled"] = _as_bool(body["enabled"])
    if "images" in body:
        new["images"] = _as_bool(body["images"])
        if new["images"] and item.get("source_type") != "files":
            raise ApiError(400, "llm_images applies to files sources only (crawled sites carry no images)")
    if "model" in body:
        new["model"] = _parse_llm_model(body["model"]) or LLM_DEFAULT_MODEL
    if body.get("corpus_cap_mb") is not None and body.get("corpus_cap_mb") != "":
        new["corpus_cap_mb"] = _parse_corpus_cap(body["corpus_cap_mb"])

    def _out(cfg: dict) -> dict:
        return {"llm_extract": cfg["enabled"], "llm_images": cfg["images"], "llm_model": cfg["model"],
                "llm_corpus_cap_mb": cfg["corpus_cap_mb"]}

    if new == cur:
        # No-op: double-clicks / repeated saves must not burn a full-corpus
        # Bedrock build each time.
        return _resp(200, {"repo_id": repo_id, **_out(new), "changed": False})
    iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    sets, removes, values = ["updated_at = :t"], [], {":t": iso}
    if new["enabled"]:
        sets.append("llm_extract = :e"); values[":e"] = "1"
    else:
        removes.append("llm_extract")
    if new["images"]:
        sets.append("llm_images = :i"); values[":i"] = "1"
    else:
        removes.append("llm_images")
    if new["model"] != LLM_DEFAULT_MODEL:
        sets.append("llm_model = :m"); values[":m"] = new["model"]
    else:
        removes.append("llm_model")  # default is stored as absent (see _apply_llm_options)
    if new["corpus_cap_mb"] != LLM_CORPUS_CAP_MB_DEFAULT:
        sets.append("llm_corpus_cap_mb = :cap"); values[":cap"] = new["corpus_cap_mb"]
    else:
        removes.append("llm_corpus_cap_mb")
    if new["enabled"] and not cur["enabled"] and not item.get("build_timeout_minutes"):
        # First time LLM is switched on: give the build the time it needs.
        sets.append("build_timeout_minutes = :bt"); values[":bt"] = LLM_BUILD_TIMEOUT_MINUTES
    _registry.update_item(
        Key={"repo_id": repo_id},
        UpdateExpression="SET " + ", ".join(sets) + (" REMOVE " + ", ".join(removes) if removes else ""),
        ExpressionAttributeValues=values,
    )
    out = {"repo_id": repo_id, **_out(new), "changed": True}
    if not (new["enabled"] != cur["enabled"] or new["enabled"]):
        # LLM stays off: images/model were stored for later, the graph is unchanged.
        out["rebuild"] = False
        return _resp(200, out)
    window = 60 * max(60, int(item.get("build_timeout_minutes", 0) or 0), LLM_BUILD_TIMEOUT_MINUTES if new["enabled"] else 0)
    if item.get("status") == "BUILDING" and int(item.get("build_started_at", 0)) > time.time() - window:
        # A build is already racing; don't stack a second full LLM build on
        # the same S3 prefix. The settings are part of the published source
        # fingerprint (files) / skip fingerprint (url), so the poller rebuilds
        # on its next tick after the running build finishes.
        out["rebuild"] = False
        out["note"] = "a build is in flight; the poller rebuilds with the new settings after it finishes"
        return _resp(200, out)
    try:
        build = _start_build({**item,
                              "llm_extract": "1" if new["enabled"] else "",
                              "llm_images": "1" if new["images"] else "",
                              "llm_model": "" if new["model"] == LLM_DEFAULT_MODEL else new["model"],
                              "llm_corpus_cap_mb": "" if new["corpus_cap_mb"] == LLM_CORPUS_CAP_MB_DEFAULT else new["corpus_cap_mb"]},
                             f"manual-{int(time.time())}")
        out["rebuild"] = True
        out["build_id"] = build["id"]
    except Exception as exc:
        out["rebuild_error"] = f"{type(exc).__name__}: {exc}"
    return _resp(200, out)


def rebuild_repo(event, ident, params):
    sub, repo_id = ident["sub"], params["repoId"]
    _require_grant(sub, repo_id)
    item = _registry.get_item(Key={"repo_id": repo_id}).get("Item")
    if not item or item.get("enabled") != "1":
        raise ApiError(404, "repo not found or disabled")
    if item.get("status") == "BUILDING" and int(item.get("build_started_at", 0)) > time.time() - 1800:
        raise ApiError(409, "a build is already in flight")
    if item.get("source_type", "git") != "git":
        # Non-git: TARGET_SHA is advisory — the build computes the corpus
        # content hash itself and the completion Lambda records source_hash.
        head_sha = f"manual-{int(time.time())}"
    else:
        token = ""
        if item.get("auth_secret_name"):
            try:
                token = json.loads(_secrets.get_secret_value(SecretId=item["auth_secret_name"])["SecretString"]).get("token", "")
            except Exception:
                pass
        try:
            _, head_sha = gitreg.resolve_ref_and_sha(item["git_url"], item["ref"], item.get("provider", "github"), token)
        except Exception as exc:
            raise ApiError(422, f"could not resolve head sha: {exc}") from None
    build = _start_build(dict(item), head_sha)
    out = {"repo_id": repo_id, "build_id": build["id"], "target_sha": head_sha[:12]}
    # Rebuild doubles as service repair: a registration whose service creation
    # failed (or was skipped) gets its dedicated MCP server here.
    if not item.get("runtime_arn") and item.get("dedicated_runtime", "1") != "0":
        try:
            rt = runtimes.ensure_repo_runtime(
                repo_id, RUNTIME_ENV,
                cpu=int(item.get("service_cpu", 0) or runtimes.DEFAULT_CPU),
                memory=int(item.get("service_memory", 0) or runtimes.DEFAULT_MEMORY),
            )
            _registry.update_item(
                Key={"repo_id": repo_id},
                UpdateExpression="SET runtime_name = :n, runtime_arn = :a, runtime_id = :i",
                ExpressionAttributeValues={":n": rt["runtime_name"], ":a": rt["runtime_arn"], ":i": rt["runtime_id"]},
            )
            out["runtime_status"] = rt["status"]
        except Exception as exc:
            out["runtime_error"] = f"{type(exc).__name__}: {exc}"
    return _resp(202, out)


def change_scope(event, ident, params):
    """Flip a source between public (pooled, hub-merged, any-key-readable)
    and private (grant-gated, hub-excluded).

    Guard rails, in order of what they protect:
      - creator-only: pooled repos are shared infrastructure; only the row's
        creator may change what every subscriber sees (CLI/operator rows have
        no created_by_sub and are managed outside the console).
      - PAT repos can never go public — their content would join the shared
        hub graph (the exact leak the fail-closed merge filter exists for).
      - public -> private is refused while other members subscribe (it would
        yank a shared graph out of the hub and behind the creator's grants).
      - the flip triggers a rebuild: the authorizer reads the row per request
        (immediate), but the hub's merged __all__ graph only changes when a
        build's merge step re-reads the registry — without a rebuild a
        now-private graph would LINGER in the hub indefinitely.
    """
    sub, repo_id = ident["sub"], params["repoId"]
    _require_grant(sub, repo_id)
    body = _body(event)
    target = body.get("graph_scope")
    if target not in ("public", "private"):
        raise ApiError(400, "graph_scope must be public|private")
    item = _registry.get_item(Key={"repo_id": repo_id}).get("Item")
    if not item or item.get("enabled") != "1":
        raise ApiError(404, "repo not found or disabled")
    if item.get("created_by_sub") != sub:
        raise ApiError(403, "only the source's creator can change its scope")
    if item.get("auth_secret_name") and target == "public":
        raise ApiError(400, "a PAT-cloned repo cannot be made public")
    current = item.get("graph_scope", "public")
    if current == target:
        return _resp(200, {"repo_id": repo_id, "graph_scope": target, "changed": False})
    if target == "private" and int(item.get("subscriber_count", 0) or 0) > 1:
        raise ApiError(409, "other members subscribe to this source; it cannot be made private")

    cond = "graph_scope = :cur AND enabled = :one"
    values = {":t": target, ":cur": current, ":one": "1",
              ":iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    if target == "private":
        cond += " AND subscriber_count <= :max_subs"
        values[":max_subs"] = 1
    try:
        _registry.update_item(
            Key={"repo_id": repo_id},
            UpdateExpression="SET graph_scope = :t, updated_at = :iso",
            ConditionExpression=cond,
            ExpressionAttributeValues=values,
        )
    except _registry.meta.client.exceptions.ConditionalCheckFailedException:
        raise ApiError(409, f"{repo_id} changed state concurrently; retry") from None
    # Keep the (informational) scope on the creator's grant row in step.
    try:
        _platform.update_item(
            Key={"pk": f"USER#{sub}", "sk": f"REPO#{repo_id}"},
            UpdateExpression="SET graph_scope = :t",
            ExpressionAttributeValues={":t": target},
        )
    except Exception:
        pass

    out = {"repo_id": repo_id, "graph_scope": target, "changed": True, "rebuild": False}
    # Refresh the hub merge. Skip when nothing was ever built (nothing to add
    # or remove) or a build is already in flight (its merge step may or may
    # not see the flip; the next poll-driven build settles it either way).
    building = item.get("status") == "BUILDING" and int(item.get("build_started_at", 0)) > time.time() - 1800
    if item.get("last_built_sha") and not building:
        sha = item["last_built_sha"] if item.get("source_type", "git") == "git" else f"manual-{int(time.time())}"
        try:
            build = _start_build({**item, "graph_scope": target}, sha)
            out["rebuild"] = True
            out["build_id"] = build["id"]
        except Exception as exc:  # scope already flipped; the poller heals the merge later
            out["rebuild_error"] = f"{type(exc).__name__}: {exc}"
    return _resp(200, out)


# ---------------------------------------------------------------------------
# graph explorer: presigned read access to a source's published graph
# ---------------------------------------------------------------------------

# Presigned GET lifetime. S3 checks expiry when the request ARRIVES, so this
# only has to cover "API response -> fetch starts"; a URL is a bearer token
# for one object, so keep it short and never log or persist it.
_GRAPH_URL_TTL = 300
# Raw graph.json fallback cap (builds older than make_viz.py have no bundle).
# graph.json is ~30x the bundle; past this the console asks for a rebuild.
_GRAPH_FALLBACK_MAX_BYTES = 32 * 1024 * 1024
_VIZ_META_MAX_BYTES = 64 * 1024
# Per-user daily cap on graph loads (presigns), modeled on the playground's
# token budget. Bundles are ~1MB and the raw fallback is capped, so a count
# bounds egress; the API-GW route throttle only caps the platform-wide rate.
_GRAPH_DAILY_LOADS = 500
# repo_ids are minted by gitreg/_register_*: [A-Za-z0-9._-] joined by "__".
# Defensive re-check before the id becomes an S3 key (mirrors _safe_upload_path).
_GRAPH_REPO_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,159}")


def _graph_out_prefix(repo_id: str) -> str:
    if repo_id == "all":
        return "repos/__all__/latest/graphify-out/"
    if not _GRAPH_REPO_ID_RE.fullmatch(repo_id) or ".." in repo_id:
        raise ApiError(400, "invalid repo id")
    return f"repos/{repo_id}/latest/graphify-out/"


def _graph_budget(sub: str) -> None:
    """429 past the caller's daily graph-load cap; metered per presign."""
    day = time.strftime("%Y-%m-%d", time.gmtime())
    try:
        out = _ddbc.update_item(
            TableName=PLATFORM_TABLE,
            Key={"pk": {"S": f"USAGE#GRAPHVIEW#{sub}"}, "sk": {"S": f"D#{day}"}},
            UpdateExpression="ADD loads :one SET #ttl = :ttl",
            ExpressionAttributeNames={"#ttl": "ttl"},
            ExpressionAttributeValues={":one": {"N": "1"}, ":ttl": {"N": str(int(time.time()) + 90 * 86400)}},
            ReturnValues="UPDATED_NEW",
        )
        used = int(out.get("Attributes", {}).get("loads", {}).get("N", "0"))
    except Exception as exc:  # metering must never block the console
        print(f"graph budget metering failed: {type(exc).__name__}: {exc}")
        return
    if used > _GRAPH_DAILY_LOADS:
        raise ApiError(429, f"daily graph-load limit reached ({_GRAPH_DAILY_LOADS}/day) — resets at 00:00 UTC")


def _head_object(key: str) -> dict | None:
    # The role has s3:List* on the bucket, so a missing key is a clean 404; a
    # 403 would be a real authorization fault and must surface, not read as
    # "no graph yet".
    try:
        return _s3.head_object(Bucket=GRAPH_BUCKET, Key=key)
    except _s3.exceptions.ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
            return None
        raise


def _presign_get(key: str, filename: str) -> str:
    # Exact server-derived key only. The response-header overrides are part of
    # the signature: even if the URL is opened in a tab, an attacker-authored
    # graph is never served as text/html from the S3 origin, and the bearer
    # URL never lands in the on-disk HTTP cache.
    return _s3.generate_presigned_url(
        "get_object",
        Params={
            "Bucket": GRAPH_BUCKET, "Key": key,
            "ResponseContentType": "application/json",
            "ResponseContentDisposition": f'attachment; filename="{filename}"',
            "ResponseCacheControl": "private, no-store",
        },
        ExpiresIn=_GRAPH_URL_TTL,
    )


def _iso(dt) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt else ""


# A viz bundle older than graph.json by more than this is a leftover from a
# build whose make_viz step failed (the bundle is published in the same build,
# seconds apart) — serve the raw graph instead of a stale picture.
_VIZ_STALE_SECONDS = 900


def _graph_view_response(ident: dict, repo_id: str, item: dict, *, expose_error: bool) -> dict:
    """Where the console's graph explorer loads a source's graph from.

    Prefers the build's compact viz bundle (viz.json, gzip-encoded, with a
    precomputed layout); falls back to the raw graph.json for builds that
    predate make_viz.py (size-capped). Returns short-lived presigned S3 GET
    URLs — the bundle is far too large to proxy through the 6MB HTTP API
    payload cap, and S3 CORS admits the console origin for GET. Not-built-yet
    is a typed `state`, not a 404 (the caller already passed the access gate,
    and the console shows build progress instead of "no such source")."""
    prefix = _graph_out_prefix(repo_id)
    status = str(item.get("status", ""))
    out = {
        "repo_id": repo_id,
        "source_type": item.get("source_type", "git"),
        "graph_scope": item.get("graph_scope", "public"),
        "git_url": item.get("git_url", "") if item.get("source_type", "git") == "git" else "",
        "ref": item.get("ref", ""),
        "status": status,
        "last_built_at": item.get("last_built_at", ""),
        "last_built_sha": (item.get("last_built_sha") or "")[:12],
        # Build error text is the owner's/subscribers' business (my sources);
        # the catalog preview withholds it like list_catalog does.
        "last_error": item.get("last_error", "") if (expose_error and status.upper() in ("FAILED", "TOO_LARGE")) else "",
        "expires_in": _GRAPH_URL_TTL,
    }
    bundle_head = _head_object(prefix + "viz.json")
    graph_head = _head_object(prefix + "graph.json")
    if bundle_head and graph_head:
        lag = (graph_head["LastModified"] - bundle_head["LastModified"]).total_seconds()
        if lag > _VIZ_STALE_SECONDS:
            print(f"viz bundle for {repo_id} is {int(lag)}s older than graph.json — serving the raw graph")
            bundle_head = None
    meta: dict = {}
    if bundle_head:
        # The header is optional polish (stats); a missing/oversized/broken
        # meta must not hide a good bundle.
        meta_head = _head_object(prefix + "viz-meta.json")
        if meta_head and int(meta_head.get("ContentLength", 0)) <= _VIZ_META_MAX_BYTES:
            try:
                parsed = json.loads(_s3.get_object(Bucket=GRAPH_BUCKET, Key=prefix + "viz-meta.json")["Body"].read())
                if isinstance(parsed, dict):
                    meta = parsed
            except Exception as exc:
                print(f"viz-meta unreadable for {repo_id}: {type(exc).__name__}: {exc}")
    if not bundle_head and not graph_head:
        # Nothing published yet: REGISTERED/BUILDING is "pending", FAILED
        # with no earlier publish is "empty"; the console polls like pollBuilds.
        out["state"] = "empty" if status.upper() == "FAILED" else "pending"
        return _resp(200, out)
    _graph_budget(ident["sub"])
    out["state"] = "ready"
    if bundle_head:
        out["viz"] = {
            "stats": meta.get("stats") or {},
            "layout": meta.get("layout"),
            "generated_at": meta.get("generated_at", ""),
            "built_at_commit": meta.get("built_at_commit", ""),
            "bytes": int(bundle_head.get("ContentLength", 0)),
            "raw_bytes": int(meta.get("bundle_raw_bytes") or 0),
            "etag": str(bundle_head.get("ETag", "")).strip('"'),
            "last_modified": _iso(bundle_head.get("LastModified")),
            "url": _presign_get(prefix + "viz.json", "viz.json"),
        }
    if graph_head:
        size = int(graph_head.get("ContentLength", 0))
        out["graph"] = {
            "bytes": size,
            "etag": str(graph_head.get("ETag", "")).strip('"'),
            "last_modified": _iso(graph_head.get("LastModified")),
            "max_bytes": _GRAPH_FALLBACK_MAX_BYTES,
            # Only mint the heavy fallback when the console may actually use it.
            "url": _presign_get(prefix + "graph.json", "graph.json")
            if (not bundle_head and size <= _GRAPH_FALLBACK_MAX_BYTES) else None,
        }
    resp = _resp(200, out)
    resp["headers"]["Cache-Control"] = "no-store"   # the body carries bearer URLs
    return resp


def get_graph_viz(event, ident, params):
    """My sources + the hub. Grant-gated like every other /repos/{id}/* route
    (a strict subset of what the data plane discloses); the hub ('all') is
    the merged PUBLIC graph every member already reaches via list_servers.
    enabled == "1" is load-bearing: the S3 bytes outlive a torn-down row."""
    repo_id = params["repoId"]
    if repo_id == "all":
        item = {"repo_id": "all", "status": "READY", "source_type": "hub", "graph_scope": "public"}
    else:
        _require_grant(ident["sub"], repo_id)
        item = _registry.get_item(Key={"repo_id": repo_id}).get("Item")
        if not item or item.get("enabled") != "1":
            raise ApiError(404, "repo not found or disabled")
    return _graph_view_response(ident, repo_id, item, expose_error=True)


# Console source viewer: read_source without an API key. The graph explorer
# shows a node's code by asking the repo's own MCP task, and the caller is a
# Cognito member, not a key holder — so the platform Lambda invokes the
# in-VPC proxy Lambda directly with a synthesized authorizer context, after
# applying the SAME access rule as the graph routes (private → grant holder;
# public → any member, like the data plane). Only read_source is forwarded.
_SOURCE_DAILY_READS = 3000
_SOURCE_MAX_LINES = 400


def _source_budget(sub: str) -> None:
    day = time.strftime("%Y-%m-%d", time.gmtime())
    try:
        out = _ddbc.update_item(
            TableName=PLATFORM_TABLE,
            Key={"pk": {"S": f"USAGE#GRAPHSRC#{sub}"}, "sk": {"S": f"D#{day}"}},
            UpdateExpression="ADD reads :one SET #ttl = :ttl",
            ExpressionAttributeNames={"#ttl": "ttl"},
            ExpressionAttributeValues={":one": {"N": "1"}, ":ttl": {"N": str(int(time.time()) + 90 * 86400)}},
            ReturnValues="UPDATED_NEW",
        )
        used = int(out.get("Attributes", {}).get("reads", {}).get("N", "0"))
    except Exception as exc:
        print(f"source budget metering failed: {type(exc).__name__}: {exc}")
        return
    if used > _SOURCE_DAILY_READS:
        raise ApiError(429, f"daily source-read limit reached ({_SOURCE_DAILY_READS}/day) — resets at 00:00 UTC")


def read_repo_source(event, ident, params):
    repo_id = params["repoId"]
    if repo_id == "all":
        raise ApiError(400, "the hub has no source snapshot — read from the node's own repo")
    if not _GRAPH_REPO_ID_RE.fullmatch(repo_id):
        raise ApiError(400, "invalid repo id")
    item = _registry.get_item(Key={"repo_id": repo_id}).get("Item")
    if not item or item.get("enabled") != "1":
        raise ApiError(404, "repo not found or disabled")
    if item.get("graph_scope") != "public":
        _require_grant(ident["sub"], repo_id)
    if not MCP_PROXY_FN:
        raise ApiError(503, "source viewer is not configured")
    body = _body(event)
    file = str(body.get("file", "")).strip()
    if not file or len(file) > 512 or "\x00" in file:
        raise ApiError(400, "file is required")
    try:
        start = max(1, int(body.get("start_line") or 1))
        end = int(body.get("end_line") or 0)
    except (TypeError, ValueError):
        raise ApiError(400, "start_line/end_line must be integers") from None
    if end and end - start + 1 > _SOURCE_MAX_LINES:
        end = start + _SOURCE_MAX_LINES - 1
    _source_budget(ident["sub"])
    rpc = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
           "params": {"name": "read_source", "arguments": {"file": file, "start_line": start, **({"end_line": end} if end else {})}}}
    proxy_event = {
        "pathParameters": {"serverId": repo_id},
        "requestContext": {"authorizer": {"kid": "console", "ownerSub": ident["sub"], "scopeServerIds": repo_id}},
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(rpc),
        "isBase64Encoded": False,
    }
    try:
        res = _lambda.invoke(FunctionName=MCP_PROXY_FN, Payload=json.dumps(proxy_event).encode())
        payload = json.loads(res["Payload"].read() or b"{}")
    except Exception as exc:
        raise ApiError(502, f"source read failed: {type(exc).__name__}") from None
    if not isinstance(payload, dict) or "statusCode" not in payload:
        raise ApiError(502, "source read failed: bad proxy response")
    status = int(payload.get("statusCode") or 500)
    try:
        env = json.loads(payload.get("body") or "{}")
    except json.JSONDecodeError:
        env = {}
    if status >= 400 or (isinstance(env, dict) and env.get("error")):
        msg = (env.get("error") or {}).get("message") if isinstance(env, dict) else ""
        raise ApiError(502 if status >= 500 else status, msg or f"MCP HTTP {status}")
    texts = [c.get("text", "") for c in ((env.get("result") or {}).get("content") or []) if isinstance(c, dict) and c.get("type") == "text"]
    return _resp(200, {"repo_id": repo_id, "text": "\n".join(texts)})


def get_catalog_graph(event, ident, params):
    """Catalog preview: any member may view an enabled PUBLIC source's graph
    before subscribing — the same predicate as list_catalog (and the data
    plane already lets every valid key query public repos). Private sources
    404 here exactly like an unknown id (no existence oracle)."""
    repo_id = params["repoId"]
    if repo_id == "all":
        raise ApiError(404, "no such public source")
    item = _registry.get_item(Key={"repo_id": repo_id}).get("Item")
    # Fail-closed like the hub merge filter: a row must SAY public.
    if not item or item.get("enabled") != "1" or item.get("graph_scope") != "public":
        raise ApiError(404, "no such public source")
    return _graph_view_response(ident, repo_id, item, expose_error=False)


# ---------------------------------------------------------------------------
# files-source upload management (browser uploads via presigned S3 POST)
# ---------------------------------------------------------------------------

_UPLOAD_MAX_FILES = 200            # per presign/delete request
_UPLOAD_MAX_FILE_BYTES = 100 * 1024 * 1024
_UPLOAD_LIST_CAP = 2000
_UPLOAD_URL_TTL = 900


def _safe_upload_path(p) -> str:
    """A key-safe repo-relative file path (the segment rules mirror the build's
    fetch_uploads.py guards — a path rejected there must not be mintable here)."""
    p = str(p or "")
    if not p or len(p) > 512 or "\\" in p or any(ord(c) < 32 or ord(c) == 127 for c in p):
        raise ApiError(400, f"invalid file path: {p[:80]!r}")
    for seg in p.split("/"):
        # 240 UTF-8 bytes: the build materializes each segment as an ext4
        # filename (NAME_MAX 255 BYTES — a 90-char Korean name is already
        # 270), and s3transfer's download temp-suffix eats ~9 more.
        if seg in ("", ".", "..") or len(seg.encode("utf-8")) > 240:
            raise ApiError(400, f"invalid file path (bad or too-long segment): {p[:80]!r}")
    return p


def _own_files_repo(sub: str, repo_id: str) -> dict:
    """Gate for every upload-management route: only the files source's CREATOR
    may write. Non-creators (subscribers of a public files source, or anyone
    guessing ids) never get write access — so the only person who can put
    content into a hub-merged (public) files corpus is its creator, which is
    the same trust as registering a public git repo they control. CLI/operator
    rows have no created_by_sub and are managed outside the console."""
    _require_grant(sub, repo_id)
    item = _registry.get_item(Key={"repo_id": repo_id}).get("Item")
    if not item or item.get("enabled") != "1":
        raise ApiError(404, "repo not found or disabled")
    if item.get("source_type") != "files":
        raise ApiError(400, "uploads are only available on files sources")
    if item.get("created_by_sub") != sub:
        raise ApiError(403, "uploads are limited to the files source you created")
    return item


def presign_uploads(event, ident, params):
    sub, repo_id = ident["sub"], params["repoId"]
    _own_files_repo(sub, repo_id)
    body = _body(event)
    files = body.get("files")
    if not isinstance(files, list) or not files:
        raise ApiError(400, "files must be a non-empty list of {path, size}")
    if len(files) > _UPLOAD_MAX_FILES:
        raise ApiError(400, f"at most {_UPLOAD_MAX_FILES} files per request")
    uploads = []
    for f in files:
        if not isinstance(f, dict):
            raise ApiError(400, "files must be objects {path, size}")
        path = _safe_upload_path(f.get("path"))
        try:
            size = int(f.get("size") or 0)
        except (TypeError, ValueError):
            raise ApiError(400, f"invalid size for {path}") from None
        if not 0 <= size <= _UPLOAD_MAX_FILE_BYTES:
            raise ApiError(400, f"{path}: exceeds the {_UPLOAD_MAX_FILE_BYTES // (1024 * 1024)}MB per-file cap")
        # content-length-range is enforced by S3 at POST time — a presigned
        # PUT could not cap the actual body size.
        post = _s3.generate_presigned_post(
            Bucket=GRAPH_BUCKET,
            Key=f"uploads/{repo_id}/{path}",
            Conditions=[["content-length-range", 0, _UPLOAD_MAX_FILE_BYTES]],
            ExpiresIn=_UPLOAD_URL_TTL,
        )
        uploads.append({"path": path, "url": post["url"], "fields": post["fields"]})
    return _resp(200, {"uploads": uploads, "expires_in": _UPLOAD_URL_TTL})


def list_uploads(event, ident, params):
    sub, repo_id = ident["sub"], params["repoId"]
    _own_files_repo(sub, repo_id)
    prefix = f"uploads/{repo_id}/"
    files, total_bytes, truncated = [], 0, False
    paginator = _s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=GRAPH_BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            rel = obj["Key"][len(prefix):]
            if not rel or rel.endswith("/"):
                continue  # folder markers
            total_bytes += int(obj.get("Size", 0))
            if len(files) >= _UPLOAD_LIST_CAP:
                truncated = True
                continue
            files.append({
                "path": rel,
                "size": int(obj.get("Size", 0)),
                "last_modified": obj["LastModified"].strftime("%Y-%m-%dT%H:%M:%SZ"),
            })
    files.sort(key=lambda x: x["path"])
    return _resp(200, {"files": files, "total_bytes": total_bytes, "truncated": truncated})


def delete_uploads(event, ident, params):
    sub, repo_id = ident["sub"], params["repoId"]
    _own_files_repo(sub, repo_id)
    body = _body(event)
    paths = body.get("paths")
    if not isinstance(paths, list) or not paths:
        raise ApiError(400, "paths must be a non-empty list")
    if len(paths) > _UPLOAD_MAX_FILES:
        raise ApiError(400, f"at most {_UPLOAD_MAX_FILES} paths per request")
    keys = [{"Key": f"uploads/{repo_id}/{_safe_upload_path(p)}"} for p in paths]
    resp = _s3.delete_objects(Bucket=GRAPH_BUCKET, Delete={"Objects": keys, "Quiet": True})
    errors = resp.get("Errors", [])
    if errors:
        raise ApiError(502, f"failed to delete {len(errors)} object(s)")
    return _resp(200, {"deleted": len(keys)})


def list_catalog(event, ident, _params):
    """Every enabled PUBLIC server — the discovery surface for members.

    Same fail-closed filter as the hub merge (enabled=1 AND graph_scope=public,
    a row missing graph_scope stays hidden), so the catalog shows exactly the
    set a member could already reach through the hub — joining adds it to
    their own lists and the playground picker. Private servers never appear
    here; they are shared by the creator through the members API below.
    """
    sub = ident["sub"]
    granted = {g["sk"].removeprefix("REPO#") for g in _grants(sub, "REPO#")}
    items, kwargs = [], {}
    while True:
        page = _registry.query(
            IndexName="due-index",
            KeyConditionExpression="enabled = :one",
            FilterExpression="graph_scope = :pub",
            ExpressionAttributeValues={":one": "1", ":pub": "public"},
            **kwargs,
        )
        items.extend(page.get("Items", []))
        if "LastEvaluatedKey" not in page:
            break
        kwargs = {"ExclusiveStartKey": page["LastEvaluatedKey"]}
    servers = []
    for it in sorted(items, key=lambda i: i["repo_id"]):
        servers.append({
            "repo_id": it["repo_id"],
            "source_type": it.get("source_type", "git"),
            "git_url": it.get("git_url", ""),
            "ref": it.get("ref", ""),
            "status": it.get("status", ""),
            "has_snapshot": it.get("has_snapshot"),
            "subscriber_count": int(it.get("subscriber_count", 0) or 0),
            "created_at": it.get("created_at", ""),
            "server_name": it.get("server_name", ""),
            "joined": it["repo_id"] in granted,
            "owned": it.get("created_by_sub") == sub,
            # Creator's email ("" for an ownerless CLI/operator row). Emails are
            # already visible to every member through search_users.
            "owner": _owner_label(it.get("created_by_sub", "")),
        })
    return _resp(200, {"servers": servers})


# sub -> (label, expires_at). The console refreshes the catalog every 12 s, so
# the Cognito lookup behind the owner column is memoized across warm
# invocations; a miss (deleted user / lookup error) is retried sooner.
_OWNER_LABEL_TTL, _OWNER_MISS_TTL = 900, 60
_owner_labels: dict[str, tuple[str, float]] = {}


def _owner_label(sub: str) -> str:
    if not sub:
        return ""
    now = time.time()
    hit = _owner_labels.get(sub)
    if hit and hit[1] > now:
        return hit[0]
    email = _email_for_sub(sub, {})
    _owner_labels[sub] = (email or f"u{sub[:8]}…", now + (_OWNER_LABEL_TTL if email else _OWNER_MISS_TTL))
    return _owner_labels[sub][0]


def join_repo(event, ident, params):
    """Subscribe to an enabled PUBLIC server from the catalog."""
    sub, repo_id = ident["sub"], params["repoId"]
    item = _registry.get_item(Key={"repo_id": repo_id}).get("Item")
    if not item or item.get("enabled") != "1" or item.get("graph_scope", "") != "public":
        # One 404 for missing/disabled/private alike: a private server's
        # existence is not disclosed to non-members.
        raise ApiError(404, f"no public server: {repo_id}")
    # Re-checks enabled AND public inside the transaction — a concurrent
    # scope flip between the read above and the write fails closed.
    return _join_enabled_repo(sub, repo_id, item.get("git_url", ""), item.get("ref", ""))


def _owned_repo(sub: str, repo_id: str) -> dict:
    item = _registry.get_item(Key={"repo_id": repo_id}).get("Item")
    # One 404 for missing, disabled, AND not-yours alike: a distinct 403 for
    # "exists but not yours" is an existence oracle — a private repo_id is
    # `files__<name>__u<creator-sub8>`, and list_members hands out member
    # sub8s, so a split response would let a member enumerate other private
    # sources. Only the creator ever passes this gate.
    if (not item or item.get("enabled") != "1"
            or item.get("created_by_sub") != sub):
        raise ApiError(404, f"no such repo you manage: {repo_id}")
    return item


def _repo_grants(repo_id: str) -> list[dict]:
    """Full grant rows for everyone subscribed to repo_id.

    entity-index is KEYS_ONLY, so the query yields only key attrs; the grant
    bodies (invited_email, graph_scope, created_at) come from a follow-up
    BatchGet against the base table.
    """
    keys, kwargs = [], {}
    while True:
        page = _platform.query(
            IndexName="entity-index",
            KeyConditionExpression="gsi1pk = :p",
            ExpressionAttributeValues={":p": f"REPO#{repo_id}"},
            **kwargs,
        )
        keys.extend({"pk": it["pk"], "sk": it["sk"]} for it in page.get("Items", []))
        if "LastEvaluatedKey" not in page:
            break
        kwargs = {"ExclusiveStartKey": page["LastEvaluatedKey"]}
    grants = []
    for i in range(0, len(keys), 100):
        resp = _ddb.batch_get_item(RequestItems={PLATFORM_TABLE: {"Keys": keys[i : i + 100]}})
        grants.extend(resp.get("Responses", {}).get(PLATFORM_TABLE, []))
    return grants


def _email_for_sub(sub: str, cache: dict) -> str:
    """Resolve a Cognito user's email from their sub (memoized per request).

    Members added via add_member already carry invited_email; this fills the
    gap for the owner and any grant minted without a stored email, so the
    members list identifies people by email, never by an opaque sub."""
    if sub in cache:
        return cache[sub]
    email = ""
    try:
        users = _cognito.list_users(
            UserPoolId=USER_POOL_ID, Filter=f'sub = "{sub}"', Limit=1
        ).get("Users", [])
        if users:
            email = next((a["Value"] for a in users[0].get("Attributes", []) if a["Name"] == "email"), "")
    except Exception:  # a deleted user / lookup failure falls back to the short sub
        pass
    cache[sub] = email
    return email


def list_members(event, ident, params):
    sub, repo_id = ident["sub"], params["repoId"]
    item = _owned_repo(sub, repo_id)
    members = []
    email_cache: dict[str, str] = {}
    for g in _repo_grants(repo_id):
        m_sub = g["pk"].removeprefix("USER#")
        kind = ("owner" if m_sub == item.get("created_by_sub")
                else "member" if g.get("graph_scope") == "member" else "subscriber")
        email = g.get("invited_email") or _email_for_sub(m_sub, email_cache)
        members.append({
            "sub": m_sub,
            "email": email,
            "label": email or f"u{m_sub[:8]}…",
            "kind": kind,
            "added_at": g.get("created_at", ""),
            "you": m_sub == sub,
        })
    members.sort(key=lambda m: (m["kind"] != "owner", m["added_at"]))
    return _resp(200, {"repo_id": repo_id, "members": members,
                       "graph_scope": item.get("graph_scope", "public")})


_EMAIL_RE = re.compile(r'[^@\s"\\]+@[^@\s"\\]+\.[^@\s"\\]+')


def add_member(event, ident, params):
    """Creator grants a platform user access to this server (by email).

    This is how a PRIVATE source is shared: the grant is exactly what the
    data-plane authorizer honors (ConsistentRead, TTL 0), so the member's own
    API keys reach the server immediately. The graph stays out of the hub —
    graph_scope is untouched. Works on public rows too (a no-op convenience;
    anyone could join those via the catalog).
    """
    sub, repo_id = ident["sub"], params["repoId"]
    item = _owned_repo(sub, repo_id)
    email = str(_body(event).get("email", "")).strip().lower()
    # The character class also bans '"' and '\' — they would escape the
    # Cognito ListUsers filter string below.
    if not _EMAIL_RE.fullmatch(email):
        raise ApiError(400, "valid email required")
    users = _cognito.list_users(
        UserPoolId=USER_POOL_ID, Filter=f'email = "{email}"', Limit=2
    ).get("Users", [])
    if not users:
        raise ApiError(404, f"{email} is not a platform user yet — invite them first (Admin tab)")
    target = next((a["Value"] for a in users[0].get("Attributes", []) if a["Name"] == "sub"), "")
    if not target:
        raise ApiError(502, "could not resolve the user's id")
    if target == sub:
        raise ApiError(400, "you already own this source")
    grant = _grant_item_typed(target, repo_id, item.get("git_url", ""), item.get("ref", ""), "member")
    grant["invited_email"] = {"S": email}
    grant["invited_by_sub"] = {"S": sub}
    try:
        _ddbc.transact_write_items(TransactItems=[
            {"Put": {
                "TableName": PLATFORM_TABLE,
                "Item": grant,
                "ConditionExpression": "attribute_not_exists(pk)",
            }},
            {"Update": {
                "TableName": REGISTRY_TABLE,
                "Key": {"repo_id": {"S": repo_id}},
                "UpdateExpression": "ADD subscriber_count :inc",
                "ConditionExpression": "attribute_exists(repo_id) AND enabled = :one",
                "ExpressionAttributeValues": {":inc": {"N": "1"}, ":one": {"S": "1"}},
            }},
        ])
    except _ddbc.exceptions.TransactionCanceledException:
        if _platform.get_item(Key={"pk": f"USER#{target}", "sk": f"REPO#{repo_id}"}).get("Item"):
            raise ApiError(409, f"{email} already has access") from None
        raise ApiError(409, f"{repo_id} changed state concurrently; retry") from None
    return _resp(201, {"repo_id": repo_id, "email": email, "granted": True})


def remove_member(event, ident, params):
    sub, repo_id = ident["sub"], params["repoId"]
    item = _owned_repo(sub, repo_id)
    target = str(_body(event).get("sub", "")).strip()
    if not re.fullmatch(r"[0-9a-fA-F-]{16,64}", target):
        raise ApiError(400, "member sub required (from GET /repos/{id}/members)")
    if target == item.get("created_by_sub"):
        raise ApiError(400, "the owner cannot be removed — delete the source instead")
    # Delete ONLY a grant this creator minted via add_member (graph_scope =
    # "member"). Without that condition remove_member would delete ANY
    # non-owner grant — including a peer tenant's `public` joiner grant on a
    # pooled public row, whose "creator" is merely its first registrant. That
    # would let the first registrant evict every co-subscriber, drive
    # subscriber_count down to 1, and thereby defeat change_scope's
    # public->private guard (or tear the shared service down). The condition
    # also makes the decrement safe: only a real member grant is ever removed,
    # so the creator's own grant always survives and the count stays >= 1
    # (no zero-subscriber teardown case reachable here).
    try:
        _ddbc.transact_write_items(TransactItems=[
            {"Delete": {
                "TableName": PLATFORM_TABLE,
                "Key": {"pk": {"S": f"USER#{target}"}, "sk": {"S": f"REPO#{repo_id}"}},
                "ConditionExpression": "attribute_exists(pk) AND graph_scope = :member",
                "ExpressionAttributeValues": {":member": {"S": "member"}},
            }},
            {"Update": {
                "TableName": REGISTRY_TABLE,
                "Key": {"repo_id": {"S": repo_id}},
                "UpdateExpression": "ADD subscriber_count :neg",
                "ConditionExpression": "attribute_exists(repo_id)",
                "ExpressionAttributeValues": {":neg": {"N": "-1"}},
            }},
        ])
    except _ddbc.exceptions.TransactionCanceledException:
        raise ApiError(404, "no such member on this source") from None
    return _resp(200, {"repo_id": repo_id, "removed": f"u{target[:8]}…"})


def list_servers(event, ident, _params):
    grants = _grants(ident["sub"], "REPO#")
    repo_ids = [g["sk"].removeprefix("REPO#") for g in grants]
    reg = _registry_items(repo_ids)
    servers = [{
        "server_id": "all",
        "kind": "hub",
        "description": "merged graph of every public repo on the platform",
        "mcp_url": f"{MCP_BASE_URL}/mcp/all",
        "runtime_status": "READY",
    }]
    for rid in repo_ids:
        item = reg.get(rid)
        if not item:
            continue
        servers.append({
            "server_id": rid,
            "kind": "repo",
            "git_url": item.get("git_url", ""),
            # The console hides a files source's origin line (its git_url is an
            # internal s3://uploads path); without source_type it falls back to
            # "git" and prints that bucket path.
            "source_type": item.get("source_type", "git"),
            "ref": item.get("ref", ""),
            "graph_scope": item.get("graph_scope", "public"),
            "build_status": item.get("status", ""),
            "has_snapshot": item.get("has_snapshot"),
            "server_name": item.get("server_name", ""),
            "owned": item.get("created_by_sub") == ident["sub"],
            "manageable": _can_manage(item, ident),
            "mcp_url": f"{MCP_BASE_URL}/mcp/{rid}",
            "runtime_status": runtimes.runtime_status(item["runtime_id"]) if item.get("runtime_id") else "NONE",
        })
    return _resp(200, {"servers": servers})


def list_keys(event, ident, _params):
    pointers = _grants(ident["sub"], "KEY#")
    kids = [p["sk"].removeprefix("KEY#") for p in pointers]
    out = []
    for i in range(0, len(kids), 100):
        resp = _ddb.batch_get_item(RequestItems={PLATFORM_TABLE: {
            "Keys": [{"pk": f"AKEY#{k}", "sk": "META"} for k in kids[i : i + 100]]}})
        for item in resp.get("Responses", {}).get(PLATFORM_TABLE, []):
            out.append({
                "kid": item["pk"].removeprefix("AKEY#"),
                "name": item.get("name", ""),
                "key_prefix": item.get("key_prefix", ""),
                "last4": item.get("last4", ""),
                "status": item.get("status", ""),
                "scope_type": item.get("scope_type", "ALL"),
                "scope_server_ids": sorted(item.get("scope_server_ids") or []),
                "created_at": item.get("created_at", ""),
                "expires_at": item.get("expires_at", 0),
                "revoked_at": item.get("revoked_at", ""),
            })
    out.sort(key=lambda k: k.get("created_at", ""), reverse=True)
    return _resp(200, {"keys": out})


def create_key(event, ident, _params):
    sub = ident["sub"]
    body = _body(event)
    name = str(body.get("name", "")).strip()[:60] or "unnamed"
    scope = body.get("scope", "all")
    expires_days = int(body.get("expires_days") or DEFAULT_KEY_EXPIRES_DAYS)
    if not 1 <= expires_days <= 730:
        raise ApiError(400, "expires_days must be 1..730")

    # "all"/"*" (string) = wildcard over every server the key's owner may reach.
    # A LIST — including ["all"] — is an explicit allow-list, so ["all"] mints a
    # hub-only key (the console's way to scope to the hub) rather than silently
    # widening to platform-wide.
    if scope in ("all", "*"):
        scope_type, scope_ids = "ALL", []
    elif isinstance(scope, list) and scope:
        granted = {g["sk"].removeprefix("REPO#") for g in _grants(sub, "REPO#")} | {"all"}
        bad = [s for s in scope if s not in granted]
        if bad:
            raise ApiError(403, f"scope contains servers you have no grant for: {bad}")
        scope_type, scope_ids = "SERVERS", sorted(set(scope))
    else:
        raise ApiError(400, 'scope must be "all" or a non-empty list of server ids')

    # Count only keys that are active AND not past expiry — revoked or lapsed
    # keys must not permanently consume a slot (rotation would dead-end).
    pointers = _grants(sub, "KEY#")
    active = 0
    now_ts = int(time.time())
    if pointers:
        kids = [p["sk"].removeprefix("KEY#") for p in pointers]
        for i in range(0, len(kids), 100):
            # _ddb is a resource client -> native (untyped) keys and attrs.
            resp = _ddb.batch_get_item(RequestItems={PLATFORM_TABLE: {
                "Keys": [{"pk": f"AKEY#{k}", "sk": "META"} for k in kids[i : i + 100]]}})
            for it in resp.get("Responses", {}).get(PLATFORM_TABLE, []):
                exp = int(it.get("expires_at", 0) or 0)
                if it.get("status") == "active" and (exp == 0 or exp > now_ts):
                    active += 1
    if active >= MAX_ACTIVE_KEYS:
        raise ApiError(409, f"active key limit reached ({MAX_ACTIVE_KEYS}); revoke an old key first")

    minted = keysmod.mint_key()
    kid = minted["kid"]
    apigw_key_id = keysmod.register_usage_key(kid, USAGE_PLAN_ID)
    now = int(time.time())
    iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
    akey = {
        "pk": f"AKEY#{kid}", "sk": "META",
        "key_hash": minted["key_hash"],
        "owner_sub": sub, "status": "active", "tier": "standard",
        "scope_type": scope_type, "name": name,
        "key_prefix": minted["key_prefix"], "last4": minted["last4"],
        "created_at": iso, "expires_at": now + expires_days * 86400,
        "apigw_key_id": apigw_key_id,
    }
    item_kwargs = {"Item": akey}
    if scope_ids:
        akey["scope_server_ids"] = set(scope_ids)
    try:
        _platform.put_item(**item_kwargs, ConditionExpression="attribute_not_exists(pk)")
    except Exception:
        keysmod.delete_usage_key(apigw_key_id)
        raise
    _platform.put_item(Item={"pk": f"USER#{sub}", "sk": f"KEY#{kid}", "created_at": iso})
    return _resp(201, {
        "kid": kid,
        "api_key": minted["plaintext"],
        "note": "store this key now — it is shown exactly once and cannot be recovered",
        "activation": "the key becomes active within ~1 minute (API Gateway usage-plan propagation)",
        "header": "X-Graphify-Key",
        "scope_type": scope_type, "scope_server_ids": scope_ids,
        "expires_at": akey["expires_at"],
        "example": f'claude mcp add --transport http graphify-all {MCP_BASE_URL}/mcp/all --header "X-Graphify-Key: {minted["plaintext"]}"',
    })


def revoke_key(event, ident, params):
    sub, kid = ident["sub"], params["kid"]
    if not _platform.get_item(Key={"pk": f"USER#{sub}", "sk": f"KEY#{kid}"}).get("Item"):
        raise ApiError(404, f"no such key: {kid}")
    item = _platform.get_item(Key={"pk": f"AKEY#{kid}", "sk": "META"}).get("Item")
    if not item:
        raise ApiError(404, f"key metadata missing: {kid}")
    now = int(time.time())
    _platform.update_item(
        Key={"pk": f"AKEY#{kid}", "sk": "META"},
        UpdateExpression="SET #s = :r, revoked_at = :t, revoked_by = :u, #ttl = :ttl",
        ExpressionAttributeNames={"#s": "status", "#ttl": "ttl"},
        ExpressionAttributeValues={
            ":r": "revoked",
            ":t": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
            ":u": sub,
            ":ttl": now + 90 * 86400,
        },
    )
    if item.get("apigw_key_id"):
        keysmod.delete_usage_key(item["apigw_key_id"])
    # Keep the USER#/KEY# pointer: list_keys and get_usage are pointer-driven,
    # so deleting it would erase the revoked key's audit row AND this month's
    # usage from the console. The slot is freed by counting only status=active
    # keys in create_key, not by dropping the pointer.
    return _resp(200, {"kid": kid, "status": "revoked", "effective": "immediately"})


def get_usage(event, ident, _params):
    month = (event.get("queryStringParameters") or {}).get("month", "")
    if not re.fullmatch(r"\d{4}-\d{2}", month or ""):
        month = time.strftime("%Y-%m", time.gmtime())
    kids = [p["sk"].removeprefix("KEY#") for p in _grants(ident["sub"], "KEY#")]
    usage = {}
    for kid in kids:
        page = _platform.query(
            KeyConditionExpression="pk = :p AND begins_with(sk, :s)",
            ExpressionAttributeValues={":p": f"USAGE#KEY#{kid}", ":s": f"D#{month}"},
        )
        days, servers, total, errors = {}, {}, 0, 0
        for row in page.get("Items", []):
            day = row["sk"].removeprefix("D#")
            req = int(row.get("req", 0))
            days[day] = req
            total += req
            errors += int(row.get("err", 0))
            for attr, val in row.items():
                if attr.startswith("s#"):
                    sid = attr[2:]
                    servers[sid] = servers.get(sid, 0) + int(val)
        usage[kid] = {"total": total, "errors": errors, "days": days, "servers": servers}
    return _resp(200, {"month": month, "keys": usage})


def webhook_info(event, ident, _params):
    # The push HMAC secret is platform-wide and, with it, anyone can forge a
    # signed push for ANY webhook repo (the webhook Lambda trusts repo_id from
    # the signed body). Restrict it to admins until per-repo secrets land.
    if "admin" not in ident["groups"]:
        raise ApiError(403, "the webhook secret is available to admins only")
    secret_value = ""
    if WEBHOOK_SECRET_ARN:
        try:
            secret_value = _secrets.get_secret_value(SecretId=WEBHOOK_SECRET_ARN)["SecretString"]
        except Exception:
            pass
    return _resp(200, {
        "payload_url": WEBHOOK_URL,
        "content_type": "application/json",
        "events": ["push"],
        "secret": secret_value,
        "note": "GitHub repo Settings > Webhooks > Add webhook; only trigger=webhook repos react to pushes",
    })


def admin_invite(event, ident, _params):
    if "admin" not in ident["groups"]:
        raise ApiError(403, "admin group required")
    body = _body(event)
    email = str(body.get("email", "")).strip().lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise ApiError(400, "valid email required")
    group = "admin" if body.get("admin") else "member"
    try:
        _cognito.admin_create_user(
            UserPoolId=USER_POOL_ID,
            Username=email,
            UserAttributes=[{"Name": "email", "Value": email}, {"Name": "email_verified", "Value": "true"}],
            DesiredDeliveryMediums=["EMAIL"],
        )
    except _cognito.exceptions.UsernameExistsException:
        raise ApiError(409, f"{email} already exists") from None
    _cognito.admin_add_user_to_group(UserPoolId=USER_POOL_ID, Username=email, GroupName=group)
    return _resp(201, {"email": email, "group": group, "note": "temporary password sent by email"})


def _require_admin(ident: dict) -> None:
    if "admin" not in ident["groups"]:
        raise ApiError(403, "admin group required")


def _user_attr(u: dict, name: str) -> str:
    return next((a["Value"] for a in u.get("Attributes", []) if a["Name"] == name), "")


def _resolve_user(target_sub: str) -> dict:
    """Resolve the authoritative Cognito user from a sub.

    Admin ops (delete/reset) key on Username, but the console works in subs;
    resolving server-side means a stale or spoofed client value can't target
    the wrong account. Returns the raw Cognito user (Username/UserStatus/attrs)."""
    users = _cognito.list_users(
        UserPoolId=USER_POOL_ID, Filter=f'sub = "{target_sub}"', Limit=1
    ).get("Users", [])
    if not users:
        raise ApiError(404, "no such user")
    return users[0]


def list_admin_users(event, ident, _params):
    _require_admin(ident)
    users, kwargs = [], {}
    while True:
        page = _cognito.list_users(UserPoolId=USER_POOL_ID, Limit=60, **kwargs)
        for u in page.get("Users", []):
            sub = _user_attr(u, "sub")
            try:
                groups = [g["GroupName"] for g in _cognito.admin_list_groups_for_user(
                    UserPoolId=USER_POOL_ID, Username=u["Username"]).get("Groups", [])]
            except Exception:
                groups = []
            users.append({
                "sub": sub,
                "email": _user_attr(u, "email"),
                "status": u.get("UserStatus", ""),
                "enabled": u.get("Enabled", True),
                "is_admin": "admin" in groups,
                "created_at": u["UserCreateDate"].strftime("%Y-%m-%dT%H:%M:%SZ") if u.get("UserCreateDate") else "",
                "you": sub == ident["sub"],
            })
        token = page.get("PaginationToken")
        if not token:
            break
        kwargs = {"PaginationToken": token}
    users.sort(key=lambda x: x.get("created_at", ""))
    return _resp(200, {"users": users})


def admin_reset_password(event, ident, _params):
    _require_admin(ident)
    target_sub = str(_body(event).get("sub", "")).strip()
    if not re.fullmatch(r"[0-9a-fA-F-]{16,64}", target_sub):
        raise ApiError(400, "sub required")
    u = _resolve_user(target_sub)
    username, status = u["Username"], u.get("UserStatus", "")
    email = _user_attr(u, "email")
    # A user who was invited but never signed in is in FORCE_CHANGE_PASSWORD
    # and has no password to reset — admin_reset_user_password throws
    # NotAuthorizedException. That is the most common reason to press this
    # button (the temp-password email got lost), so re-send the invite
    # instead of 500ing. RESEND's admin_create_user needs the EMAIL alias
    # (this pool's real Username is a system UUID, which RESEND rejects with
    # "Username should be an email").
    try:
        if status == "FORCE_CHANGE_PASSWORD":
            _cognito.admin_create_user(
                UserPoolId=USER_POOL_ID, Username=email or username,
                MessageAction="RESEND", DesiredDeliveryMediums=["EMAIL"])
            return _resp(200, {"sub": target_sub, "reset": True, "resent_invite": True,
                               "note": "the user had not signed in yet — the invitation (temporary password) was re-sent"})
        _cognito.admin_reset_user_password(UserPoolId=USER_POOL_ID, Username=username)
        return _resp(200, {"sub": target_sub, "reset": True,
                           "note": "a password-reset email was sent to the user"})
    except _cognito.exceptions.NotAuthorizedException:
        raise ApiError(409, "this user's account state does not allow a password reset") from None
    except _cognito.exceptions.InvalidParameterException as exc:
        raise ApiError(409, f"cannot reset this user's password: {exc}") from None


_TOMBSTONE_TTL_DAYS = 7


def _offboard_user(target_sub: str) -> dict:
    """Fully offboard a user before deletion: tombstone the sub (so a still-live
    JWT can't mint fresh credentials), revoke every API key (kills data-plane
    access at once), and drop every repo grant (decrementing subscriber_count,
    tearing down a private source the user created). Leaves no key that still
    authorizes and no grant that still counts."""
    now = int(time.time())
    iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
    # Tombstone FIRST: AdminDeleteUser kills the refresh token but the access
    # token stays valid offline for up to its 60-min lifetime (the management
    # API's JWT authorizer never consults Cognito). Without this a just-deleted
    # user could POST /keys — minting a long-lived data-plane key owned by a
    # sub with no console row, hence unrevokable — or POST /repos/{id}/join to
    # pin subscriber_count with a permanent grant. _reject_if_deleted (checked
    # on every management request) fails those closed the instant this lands.
    _platform.put_item(Item={"pk": f"USER#{target_sub}", "sk": "DELETED",
                             "deleted_at": iso, "ttl": now + _TOMBSTONE_TTL_DAYS * 86400})
    revoked = 0
    for ptr in _grants(target_sub, "KEY#"):
        kid = ptr["sk"].removeprefix("KEY#")
        akey = _platform.get_item(Key={"pk": f"AKEY#{kid}", "sk": "META"}).get("Item")
        if not akey or akey.get("status") != "active":
            continue
        if akey.get("apigw_key_id"):
            try:
                keysmod.delete_usage_key(akey["apigw_key_id"])
            except Exception:
                pass
        _platform.update_item(
            Key={"pk": f"AKEY#{kid}", "sk": "META"},
            UpdateExpression="SET #s = :r, revoked_at = :t, #ttl = :ttl",
            ExpressionAttributeNames={"#s": "status", "#ttl": "ttl"},
            ExpressionAttributeValues={":r": "revoked", ":t": iso, ":ttl": now + 90 * 86400},
        )
        revoked += 1
    dropped = 0
    for g in _grants(target_sub, "REPO#"):
        repo_id = g["sk"].removeprefix("REPO#")
        reg = _registry.get_item(Key={"repo_id": repo_id}).get("Item") or {}
        if (reg.get("created_by_sub") == target_sub
                and reg.get("graph_scope", "public") != "public"
                and reg.get("enabled") == "1"):
            # cloudmap_retry=False: bulk teardown must not accumulate the
            # 6s/source Cloud Map sleep and blow the 28s request budget.
            _teardown_source(repo_id, reg, cloudmap_retry=False)  # deletes ALL grants incl. this one
            dropped += 1
            continue
        try:
            _ddbc.transact_write_items(TransactItems=[
                {"Delete": {"TableName": PLATFORM_TABLE,
                            "Key": {"pk": {"S": f"USER#{target_sub}"}, "sk": {"S": f"REPO#{repo_id}"}},
                            "ConditionExpression": "attribute_exists(pk)"}},
                {"Update": {"TableName": REGISTRY_TABLE, "Key": {"repo_id": {"S": repo_id}},
                            "UpdateExpression": "ADD subscriber_count :neg",
                            "ConditionExpression": "attribute_exists(repo_id)",
                            "ExpressionAttributeValues": {":neg": {"N": "-1"}}}},
            ])
        except _ddbc.exceptions.TransactionCanceledException:
            continue
        dropped += 1
        fresh = _registry.get_item(Key={"repo_id": repo_id}, ConsistentRead=True).get("Item") or {}
        if int(fresh.get("subscriber_count", 0)) <= 0:
            try:
                _registry.update_item(
                    Key={"repo_id": repo_id},
                    UpdateExpression="SET enabled = :z, updated_at = :t",
                    ConditionExpression="subscriber_count <= :zero AND enabled = :one",
                    ExpressionAttributeValues={":z": "0", ":one": "1", ":zero": 0, ":t": iso})
                try:
                    runtimes.delete_repo_runtime(repo_id, fresh.get("runtime_id", ""))
                except Exception:
                    pass
            except _registry.meta.client.exceptions.ConditionalCheckFailedException:
                pass
    return {"revoked_keys": revoked, "dropped_grants": dropped}


def admin_delete_user(event, ident, _params):
    _require_admin(ident)
    target_sub = str(_body(event).get("sub", "")).strip()
    if not re.fullmatch(r"[0-9a-fA-F-]{16,64}", target_sub):
        raise ApiError(400, "sub required")
    if target_sub == ident["sub"]:
        raise ApiError(400, "you cannot delete your own account")
    u = _resolve_user(target_sub)
    username, resolved = u["Username"], _user_attr(u, "sub") or target_sub
    stats = _offboard_user(resolved)
    _cognito.admin_delete_user(UserPoolId=USER_POOL_ID, Username=username)
    return _resp(200, {"sub": target_sub, "deleted": True, **stats})


def search_users(event, ident, _params):
    """Prefix search over platform users' emails, for the member-add input.

    Available to any signed-in user (the platform is invite-only): returns
    only email + sub for emails starting with the query, capped small. Not an
    admin listing — it never exposes status, groups, or full-directory dumps.
    """
    q = str((event.get("queryStringParameters") or {}).get("q", "")).strip().lower()
    if len(q) < 2:
        return _resp(200, {"users": []})
    if not re.fullmatch(r"[^\s\"\\]{2,64}", q):
        raise ApiError(400, "invalid query")
    users = _cognito.list_users(
        UserPoolId=USER_POOL_ID, Filter=f'email ^= "{q}"', Limit=10
    ).get("Users", [])
    return _resp(200, {"users": [
        {"email": _user_attr(u, "email"), "sub": _user_attr(u, "sub")}
        for u in users if _user_attr(u, "email")
    ]})


ROUTES = [
    ("GET", re.compile(r"^/me$"), get_me),
    ("GET", re.compile(r"^/repos$"), list_repos),
    ("POST", re.compile(r"^/repos$"), register_repo),
    ("GET", re.compile(r"^/repos/(?P<repoId>[^/]+)$"), get_repo),
    ("DELETE", re.compile(r"^/repos/(?P<repoId>[^/]+)$"), delete_repo),
    ("POST", re.compile(r"^/repos/(?P<repoId>[^/]+)/rebuild$"), rebuild_repo),
    ("POST", re.compile(r"^/repos/(?P<repoId>[^/]+)/scope$"), change_scope),
    ("POST", re.compile(r"^/repos/(?P<repoId>[^/]+)/name$"), set_server_name),
    ("POST", re.compile(r"^/repos/(?P<repoId>[^/]+)/crawl$"), set_crawl_config),
    ("POST", re.compile(r"^/repos/(?P<repoId>[^/]+)/llm$"), set_llm_extract),
    ("GET", re.compile(r"^/repos/(?P<repoId>[^/]+)/graph$"), get_graph_viz),
    ("GET", re.compile(r"^/catalog/(?P<repoId>[^/]+)/graph$"), get_catalog_graph),
    ("POST", re.compile(r"^/repos/(?P<repoId>[^/]+)/source$"), read_repo_source),
    ("GET", re.compile(r"^/repos/(?P<repoId>[^/]+)/uploads$"), list_uploads),
    ("POST", re.compile(r"^/repos/(?P<repoId>[^/]+)/uploads$"), presign_uploads),
    ("POST", re.compile(r"^/repos/(?P<repoId>[^/]+)/uploads/delete$"), delete_uploads),
    ("GET", re.compile(r"^/catalog$"), list_catalog),
    ("POST", re.compile(r"^/repos/(?P<repoId>[^/]+)/join$"), join_repo),
    ("GET", re.compile(r"^/repos/(?P<repoId>[^/]+)/members$"), list_members),
    ("POST", re.compile(r"^/repos/(?P<repoId>[^/]+)/members$"), add_member),
    ("POST", re.compile(r"^/repos/(?P<repoId>[^/]+)/members/remove$"), remove_member),
    ("GET", re.compile(r"^/servers$"), list_servers),
    ("GET", re.compile(r"^/keys$"), list_keys),
    ("POST", re.compile(r"^/keys$"), create_key),
    ("DELETE", re.compile(r"^/keys/(?P<kid>[A-Z0-9]{12})$"), revoke_key),
    ("GET", re.compile(r"^/usage$"), get_usage),
    ("GET", re.compile(r"^/webhook-info$"), webhook_info),
    ("GET", re.compile(r"^/users/search$"), search_users),
    ("POST", re.compile(r"^/admin/invites$"), admin_invite),
    ("GET", re.compile(r"^/admin/users$"), list_admin_users),
    ("POST", re.compile(r"^/admin/users/reset-password$"), admin_reset_password),
    ("POST", re.compile(r"^/admin/users/delete$"), admin_delete_user),
]


def handler(event: dict, _ctx) -> dict:
    method = (event.get("requestContext") or {}).get("http", {}).get("method", "")
    path = event.get("rawPath", "") or "/"
    try:
        ident = _claims(event)
        _reject_if_deleted(ident["sub"])
        for m, pattern, fn in ROUTES:
            if m != method:
                continue
            match = pattern.fullmatch(path)
            if match:
                return fn(event, ident, match.groupdict())
        raise ApiError(404, f"no route: {method} {path}")
    except ApiError as exc:
        return _resp(exc.status, {"error": str(exc)})
    except Exception as exc:  # noqa: BLE001 — one edge; surface a structured 500
        print(f"UNHANDLED {method} {path}: {type(exc).__name__}: {exc}")
        return _resp(500, {"error": f"internal error: {type(exc).__name__}"})
