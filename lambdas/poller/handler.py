"""Change-detection poller.

Runs on an EventBridge schedule (one tick for all repos). For each enabled
repo whose next_poll_at is due:
  1. Resolve the current head SHA of the tracked ref.
     - source_type=files: no git — "head" is the sha256 manifest hash of the
       S3 listing under uploads/<repo_id>/ (key+ETag+size; the SAME recipe the
       build's fetch_uploads.py publishes as source_hash, which the completion
       Lambda records as last_built_sha). An empty upload folder never builds.
     - source_type=url: change detection requires actually crawling, which
       only the build plane can afford — every due tick claims a crawl-build
       and the BUILD skips itself when the crawled content hash matches the
       previously published one.
     - GitHub: GET /repos/{o}/{r}/commits/{ref} with Accept:
       application/vnd.github.sha and If-None-Match:"<last_built_sha>" —
       the body IS the SHA and the ETag IS the SHA, so a 304 means
       "head still equals what we built" (and, when authenticated, the 304
       does not count against the rate limit). The conditional value is
       deliberately last_BUILT_sha, never last_seen_sha: a failed build must
       be retried on the next tick, and a repo registered with --no-build
       must still get its first build.
     - Any other host (or a rate-limited GitHub API): git smart-HTTP ref
       advertisement (GET <url>/info/refs?service=git-upload-pack), no git
       binary needed.
  2. If it differs from last_built_sha, claim the build with a conditional
     UpdateItem (double-build guard) and StartBuild. A repo stuck in
     BUILDING is reclaimed only when CodeBuild says the build is no longer
     running (BatchGetBuilds liveness check), with a 12 h absolute backstop.

Stdlib-only HTTP (urllib) so the Lambda needs no bundled dependencies.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

import boto3

TABLE_NAME = os.environ["TABLE_NAME"]
PROJECT_NAME = os.environ["PROJECT_NAME"]
GRAPH_BUCKET = os.environ["GRAPH_BUCKET"]
# Optional account-wide GitHub token (Secrets Manager ARN) used when a repo
# has no per-repo auth secret. Recommended even for public repos: the
# unauthenticated GitHub API limit is 60/hr per (shared AWS egress) IP.
GITHUB_TOKEN_SECRET_ARN = os.environ.get("GITHUB_TOKEN_SECRET_ARN", "")

# Absolute backstop for reclaiming a BUILDING lock whose CodeBuild record
# has vanished; the primary mechanism is the BatchGetBuilds liveness check.
STALE_BUILD_BACKSTOP_SECONDS = 12 * 3600

dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(TABLE_NAME)
codebuild = boto3.client("codebuild")
secrets = boto3.client("secretsmanager")
s3 = boto3.client("s3")

_GITHUB_RE = re.compile(r"github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?/?$")
# Basic-auth username each provider expects when the password is a PAT.
_AUTH_USERS = {"github": "x-access-token", "gitlab": "oauth2", "bitbucket": "x-token-auth"}
_token_cache: dict[str, str] = {}


def _get_secret_token(secret_id: str, strict: bool) -> str:
    """Read a PAT from Secrets Manager.

    strict=True (per-repo secrets, which CodeBuild also consumes as
    '<name>:token') requires the JSON {"token": ...} shape so a wrong shape
    fails loudly at poll time instead of only at clone time. strict=False
    (the account-wide poll token, never seen by CodeBuild) also accepts a
    raw string or a 'pat' key.
    """
    if secret_id in _token_cache:
        return _token_cache[secret_id]
    value = secrets.get_secret_value(SecretId=secret_id)["SecretString"]
    token = ""
    try:
        parsed = json.loads(value)
    except (ValueError, TypeError):
        parsed = None
    if isinstance(parsed, dict):
        token = str(parsed.get("token") or ("" if strict else parsed.get("pat") or "")).strip()
    elif not strict:
        token = value.strip()
    if not token:
        raise ValueError(
            f"secret {secret_id} must be JSON {{\"token\": \"<PAT>\"}}"
            + ("" if strict else " (or a raw token string)")
        )
    _token_cache[secret_id] = token
    return token


def _http_get(url: str, headers: dict[str, str], timeout: int = 10):
    req = urllib.request.Request(url, headers=headers)
    return urllib.request.urlopen(req, timeout=timeout)  # noqa: S310 (https only)


def github_head_sha(git_url: str, ref: str, last_built: str, token: str) -> str | None:
    """Return the head SHA, or None when it still equals last_built (304)."""
    m = _GITHUB_RE.search(git_url)
    if not m:
        raise ValueError(f"not a GitHub URL: {git_url}")
    owner, repo = m.group(1), m.group(2)
    url = (
        f"https://api.github.com/repos/{owner}/{repo}/commits/"
        f"{urllib.parse.quote(ref, safe='')}"
    )
    headers = {
        "Accept": "application/vnd.github.sha",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "graphify-mcp-poller",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if last_built:
        headers["If-None-Match"] = f'"{last_built}"'
    try:
        with _http_get(url, headers) as resp:
            return resp.read().decode().strip()
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            return None
        raise


def smart_http_head_sha(git_url: str, ref: str, token: str = "", auth_user: str = "git") -> str | None:
    """Provider-agnostic head resolution via git smart-HTTP ref advertisement.

    Any git server that speaks smart HTTP works (GitLab, Bitbucket,
    Gitea/Forgejo, cgit, GitHub Enterprise, ...). A PAT rides as Basic auth
    with the provider-appropriate username. The '.git' suffix is tried first,
    then without it (servers differ on which form they answer).
    """
    base = git_url.rstrip("/")
    if base.endswith(".git"):
        base = base[:-4]
    headers = {"User-Agent": "git/2.40 graphify-mcp-poller"}
    if token:
        headers["Authorization"] = "Basic " + base64.b64encode(f"{auth_user}:{token}".encode()).decode()
    body = ""
    for suffix in (".git", ""):
        try:
            with _http_get(f"{base}{suffix}/info/refs?service=git-upload-pack", headers) as resp:
                body = resp.read().decode("utf-8", errors="replace")
            break
        except urllib.error.HTTPError as exc:
            if suffix == ".git" and exc.code in (400, 404):
                continue
            raise
    want = f"refs/heads/{ref}" if not ref.startswith("refs/") else ref
    for m in re.finditer(r"([0-9a-f]{40})\s+([^\x00\s]+)", body):
        if m.group(2) == want:
            return m.group(1)
    return None


def files_manifest_hash(repo_id: str) -> str | None:
    """Manifest hash of a files-source repo's upload prefix, or None when empty.

    sha256 over sorted "<rel>\\t<etag>\\t<size>" lines, folder markers (keys
    ending "/") excluded. MUST stay byte-identical to the recipe in
    cdk/build_scripts/fetch_uploads.py — that script publishes the same hash
    as repos/<id>/latest/source_hash, the completion Lambda records it as
    last_built_sha, and this function's output is compared against it.
    """
    prefix = f"uploads/{repo_id}/"
    entries = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=GRAPH_BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            rel = obj["Key"][len(prefix):]
            if not rel or rel.endswith("/"):
                continue
            entries.append((rel, obj.get("ETag", "").strip('"'), int(obj.get("Size", 0))))
    if not entries:
        return None  # nothing uploaded (yet) — never build an empty corpus
    manifest = "\n".join(f"{rel}\t{etag}\t{size}" for rel, etag, size in sorted(entries))
    return hashlib.sha256(manifest.encode()).hexdigest()


def resolve_head(item: dict, token: str) -> str | None:
    """Return the head SHA when it differs from last_built_sha, else None."""
    git_url = item["git_url"]
    ref = item.get("ref", "main")
    last_built = item.get("last_built_sha", "")
    provider = item.get("provider", "github")
    if provider == "github":
        try:
            return github_head_sha(git_url, ref, last_built, token)
        except urllib.error.HTTPError as exc:
            # 403/429 = rate limited (likely unauthenticated on a shared AWS
            # egress IP). The git smart-HTTP endpoint has a separate quota.
            if exc.code not in (403, 429):
                raise
    auth_user = _AUTH_USERS.get(provider, "git")
    sha = smart_http_head_sha(git_url, ref, token, auth_user)
    return None if (sha is None or sha == last_built) else sha


def build_is_dead(item: dict, now: int) -> bool:
    """Is the BUILDING claim held by a build that is no longer running?"""
    started = int(item.get("build_started_at", 0))
    if started and now - started > STALE_BUILD_BACKSTOP_SECONDS:
        return True
    build_id = item.get("build_id", "")
    if not build_id:
        # StartBuild bookkeeping never landed; give it 15 min of grace.
        return started < now - 900
    try:
        builds = codebuild.batch_get_builds(ids=[build_id])["builds"]
    except Exception as exc:
        print(f"batch_get_builds failed for {build_id}: {exc}")
        return False  # be conservative: don't reclaim on lookup failure
    return not builds or builds[0].get("buildStatus") != "IN_PROGRESS"


def claim_build(item: dict, sha: str, interval: int, now: int) -> bool:
    """Conditional claim so overlapping ticks can't start two builds."""
    if item.get("status") == "BUILDING":
        # Takeover of a dead build: guarded on the exact claim we observed,
        # so two concurrent reclaimers cannot both win.
        condition = "#s = :building AND build_started_at = :seen_started"
        extra = {":seen_started": item.get("build_started_at", 0)}
    else:
        condition = "attribute_not_exists(#s) OR #s <> :building"
        extra = {}
    try:
        table.update_item(
            Key={"repo_id": item["repo_id"]},
            UpdateExpression=(
                "SET #s = :building, build_started_at = :now, "
                "last_seen_sha = :sha, next_poll_at = :next, updated_at = :iso"
            ),
            ConditionExpression=condition,
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":building": "BUILDING",
                ":now": now,
                ":sha": sha,
                ":next": now + interval,
                ":iso": _iso(now),
                **extra,
            },
        )
        return True
    except dynamodb.meta.client.exceptions.ConditionalCheckFailedException:
        return False


def start_build(item: dict, sha: str) -> dict:
    env = [
        {"name": "REPO_ID", "value": item["repo_id"], "type": "PLAINTEXT"},
        {"name": "GIT_URL", "value": item.get("git_url", ""), "type": "PLAINTEXT"},
        {"name": "TARGET_SHA", "value": sha, "type": "PLAINTEXT"},
        {"name": "GRAPH_BUCKET", "value": GRAPH_BUCKET, "type": "PLAINTEXT"},
        # PROVIDER picks the PAT Basic-auth username; GIT_REF backs the
        # ref-fetch fallback on servers without allow-reachable-sha1-in-want.
        {"name": "PROVIDER", "value": item.get("provider", "github"), "type": "PLAINTEXT"},
        {"name": "GIT_REF", "value": item.get("ref", "HEAD"), "type": "PLAINTEXT"},
        # MUST ride on every build path (poller/webhook/console/scripts): a
        # rebuild that omits it silently un-prunes the graph back to full size
        # (litellm: 77MB -> 259MB), ballooning the merged hub graph and turning
        # every post-sync reload into a data-plane timeout.
        {"name": "PRUNE_PATHS", "value": item.get("prune_paths", ""), "type": "PLAINTEXT"},
        # Branches the buildspec: git (default) | files (S3 uploads) | url (crawl).
        {"name": "SOURCE_TYPE", "value": item.get("source_type", "git"), "type": "PLAINTEXT"},
        # Document sources only: "1" routes markdown through the Bedrock
        # Sonnet 5 semantic pass. MUST ride on every build path like
        # PRUNE_PATHS — a poller rebuild that omitted it would silently
        # flip an LLM source back to the quick-scan graph.
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
    # Per-repo clone credential resolved inside CodeBuild, never through here.
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


def _iso(epoch: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def handler(event, context):
    now = int(time.time())
    due = table.query(
        IndexName="due-index",
        KeyConditionExpression=(
            boto3.dynamodb.conditions.Key("enabled").eq("1")
            & boto3.dynamodb.conditions.Key("next_poll_at").lte(now)
        ),
    )["Items"]

    results = []
    for item in due:
        repo_id = item["repo_id"]
        interval = int(item.get("poll_interval_seconds", 900))
        try:
            if item.get("status") == "BUILDING" and not build_is_dead(item, now):
                table.update_item(
                    Key={"repo_id": repo_id},
                    UpdateExpression="SET next_poll_at = :next",
                    ExpressionAttributeValues={":next": now + interval},
                )
                results.append({"repo_id": repo_id, "action": "build-in-flight"})
                continue

            source_type = item.get("source_type", "git")
            if source_type == "files":
                sha = files_manifest_hash(repo_id)
                if sha:
                    # Same suffix the build publishes as source_hash (buildspec
                    # files fingerprint): flipping llm_images/llm_model/llm_extract
                    # is a change, so the next tick rebuilds — including after a
                    # build that was already in flight when the setting changed.
                    sha = (f"{sha}|img={'1' if item.get('llm_images') == '1' else '0'}"
                           f"|model={item.get('llm_model', '')}"
                           f"|llm={'1' if item.get('llm_extract') == '1' else '0'}")
                if sha == item.get("last_built_sha", ""):
                    sha = None
            elif source_type == "url":
                # Only a crawl can tell whether the site changed; claim a
                # crawl-build every due tick — the build skips itself when the
                # crawled content hash matches the published source_hash.
                sha = f"crawl-{now}"
            else:
                token = ""
                if item.get("auth_secret_name"):
                    token = _get_secret_token(item["auth_secret_name"], strict=True)
                elif GITHUB_TOKEN_SECRET_ARN:
                    token = _get_secret_token(GITHUB_TOKEN_SECRET_ARN, strict=False)
                sha = resolve_head(item, token)
            if sha is None or sha == item.get("last_built_sha", ""):
                # If we got here past a dead BUILDING claim, clear it so the
                # status stops lying and the liveness check stops re-running.
                clear_dead = item.get("status") == "BUILDING"
                table.update_item(
                    Key={"repo_id": repo_id},
                    UpdateExpression="SET next_poll_at = :next, updated_at = :iso"
                    + (", last_seen_sha = :sha" if sha else "")
                    + (", #s = :failed, last_error = :err" if clear_dead else ""),
                    **({"ExpressionAttributeNames": {"#s": "status"}} if clear_dead else {}),
                    ExpressionAttributeValues={
                        ":next": now + interval,
                        ":iso": _iso(now),
                        **({":sha": sha} if sha else {}),
                        **(
                            {":failed": "FAILED", ":err": "stale BUILDING claim cleared; head equals last_built"}
                            if clear_dead
                            else {}
                        ),
                    },
                )
                results.append({"repo_id": repo_id, "action": "no-change"})
                continue

            if not claim_build(item, sha, interval, now):
                results.append({"repo_id": repo_id, "action": "claim-lost"})
                continue

            try:
                build = start_build(item, sha)
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
                raise
            table.update_item(
                Key={"repo_id": repo_id},
                UpdateExpression="SET build_id = :bid, build_arn = :arn",
                ExpressionAttributeValues={":bid": build["id"], ":arn": build["arn"]},
            )
            results.append({"repo_id": repo_id, "action": "build-started", "sha": sha, "build_id": build["id"]})
        except Exception as exc:
            print(f"poll error for {repo_id}: {exc}")
            try:
                table.update_item(
                    Key={"repo_id": repo_id},
                    UpdateExpression="SET next_poll_at = :next, last_error = :err, updated_at = :iso",
                    ExpressionAttributeValues={
                        ":next": now + interval,
                        ":err": str(exc)[:1000],
                        ":iso": _iso(now),
                    },
                )
            except Exception:
                pass
            results.append({"repo_id": repo_id, "action": "error", "error": str(exc)[:200]})

    print(json.dumps({"due": len(due), "results": results}, default=str))
    return {"due": len(due), "results": results}
