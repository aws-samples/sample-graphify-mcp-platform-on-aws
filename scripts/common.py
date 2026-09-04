"""Shared helpers for the operational scripts."""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

import boto3

# CloudFormation stack name; GRAPHIFY_STACK_NAME overrides it for deployments
# created under another name (cdk/app.py reads the same variable).
STACK_NAME = os.environ.get("GRAPHIFY_STACK_NAME", "GraphifyMcpPlatform")
GITHUB_RE = re.compile(r"github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?/?$")

# Basic-auth username each provider expects when the password is a PAT.
PROVIDER_AUTH_USERS = {"github": "x-access-token", "gitlab": "oauth2", "bitbucket": "x-token-auth"}


def detect_provider(git_url: str) -> str:
    """Best-effort provider detection; override with --provider when hosting
    is self-managed (a GitHub Enterprise host is 'generic' here on purpose —
    the GitHub REST path only understands github.com)."""
    host = urllib.parse.urlparse(git_url).netloc.lower()
    if host == "github.com" or host.endswith(".github.com"):
        return "github"
    if host == "gitlab.com" or "gitlab" in host:
        return "gitlab"
    if host == "bitbucket.org":
        return "bitbucket"
    return "generic"


def stack_outputs(region: str, stack_name: str = STACK_NAME) -> dict[str, str]:
    cfn = boto3.client("cloudformation", region_name=region)
    stacks = cfn.describe_stacks(StackName=stack_name)["Stacks"]
    return {o["OutputKey"]: o["OutputValue"] for o in stacks[0].get("Outputs", [])}


def http_get(url: str, headers: dict[str, str] | None = None, timeout: int = 15) -> tuple[int, bytes]:
    req = urllib.request.Request(url, headers={"User-Agent": "aws-graphify-mcp-platform", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def github_repo_info(git_url: str, token: str = "") -> dict:
    m = GITHUB_RE.search(git_url)
    if not m:
        raise ValueError(f"not a GitHub URL: {git_url}")
    owner, repo = m.group(1), m.group(2)
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    status, body = http_get(f"https://api.github.com/repos/{owner}/{repo}", headers)
    if status != 200:
        raise RuntimeError(f"GitHub API {status} for {owner}/{repo}: {body[:200]!r}")
    return json.loads(body)


def github_head_sha(git_url: str, ref: str, token: str = "") -> str:
    m = GITHUB_RE.search(git_url)
    owner, repo = m.group(1), m.group(2)
    headers = {"Accept": "application/vnd.github.sha", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    status, body = http_get(
        f"https://api.github.com/repos/{owner}/{repo}/commits/{urllib.parse.quote(ref, safe='')}",
        headers,
    )
    if status != 200:
        raise RuntimeError(f"GitHub API {status} resolving {ref}: {body[:200]!r}")
    return body.decode().strip()


def smart_http_refs(
    git_url: str, token: str = "", auth_user: str = "git"
) -> tuple[str | None, dict[str, str]]:
    """Return (default_branch, {ref: sha}) via git smart-HTTP ref advertisement.

    Works against any git server that speaks smart HTTP (GitHub, GitLab,
    Bitbucket, Gitea/Forgejo, cgit, Azure DevOps, ...). A PAT rides as Basic
    auth with the provider-appropriate username. Tries the '.git' suffix
    first, then without it (some servers only answer one form).
    """
    import base64 as _b64

    base = git_url.rstrip("/")
    if base.endswith(".git"):
        base = base[:-4]
    headers = {}
    if token:
        headers["Authorization"] = "Basic " + _b64.b64encode(f"{auth_user}:{token}".encode()).decode()
    status, body_b = 0, b""
    for suffix in (".git", ""):
        status, body_b = http_get(f"{base}{suffix}/info/refs?service=git-upload-pack", headers)
        if status == 200:
            break
    if status != 200:
        raise RuntimeError(f"info/refs returned {status} for {git_url}")
    body = body_b.decode("utf-8", errors="replace")
    default = None
    m = re.search(r"symref=HEAD:refs/heads/([^\x00 \n]+)", body)
    if m:
        default = m.group(1)
    refs = {m.group(2): m.group(1) for m in re.finditer(r"([0-9a-f]{40})\s+(refs/[^\x00\s]+)", body)}
    return default, refs


def make_repo_id(git_url: str, ref: str) -> str:
    """Filesystem/S3-safe slug, e.g. github__psf__requests__main."""
    m = GITHUB_RE.search(git_url)
    if m:
        parts = ["github", m.group(1), m.group(2), ref]
    else:
        p = urllib.parse.urlparse(git_url)
        parts = [p.netloc] + [seg for seg in p.path.strip("/").removesuffix(".git").split("/") if seg] + [ref]
    return "__".join(re.sub(r"[^A-Za-z0-9._-]", "_", part) for part in parts)


def make_url_repo_id(url: str) -> str:
    """Registry id for a url docs source, e.g. url__hatch.pypa.io__1.18.
    Keep in sync with lambdas/platform_api/gitreg.py:make_url_repo_id."""
    p = urllib.parse.urlsplit(url)
    parts = ["url", p.netloc.lower()] + [seg for seg in p.path.strip("/").split("/") if seg]
    return "__".join(re.sub(r"[^A-Za-z0-9._-]", "_", part) for part in parts)[:100]


def files_repo_id(name: str) -> str:
    """Registry id for an operator-registered (CLI) files source."""
    return f"files__{name}"


# ---------------------------------------------------------------------------
# Per-repo MCP Fargate services (created dynamically at registration; the hub
# service is the only one CDK manages). Same lifecycle the platform API's
# lambdas/platform_api/runtimes.py implements — kept behaviorally in sync.
# ---------------------------------------------------------------------------

def runtime_name_for(repo_id: str) -> str:
    """Deterministic ECS/Cloud Map service name (also its DNS label):
    lowercased, non [a-z0-9_] squashed, collision-proofed with a hash tail.
    Keep in sync with lambdas/platform_api/runtimes.py and
    lambdas/mcp_proxy/handler.py."""
    import hashlib

    slug = re.sub(r"[^a-z0-9_]", "_", repo_id.lower())
    tail = hashlib.sha1(repo_id.encode(), usedforsecurity=False).hexdigest()[:6]
    return f"g_{slug[:39]}_{tail}"


# Fargate cpu -> allowed memory (MiB). Keep in sync with
# lambdas/platform_api/runtimes.py (the console path enforces the same table).
_FARGATE_MEM = {
    256: (512, 2048),
    512: (1024, 4096),
    1024: (2048, 8192),
    2048: (4096, 16384),
    4096: (8192, 30720),
}


def validate_task_size(cpu: int, memory: int) -> None:
    rng = _FARGATE_MEM.get(cpu)
    if not rng:
        raise ValueError(f"cpu must be one of {sorted(_FARGATE_MEM)} (got {cpu})")
    if cpu == 256:
        if memory not in (512, 1024, 2048):
            raise ValueError(f"memory for cpu=256 must be 512, 1024 or 2048 MiB (got {memory})")
        return
    lo, hi = rng
    if not (lo <= memory <= hi) or memory % 1024 != 0:
        raise ValueError(f"memory for cpu={cpu} must be {lo}..{hi} MiB in 1024 steps (got {memory})")


def ensure_repo_runtime(
    repo_id: str,
    outputs: dict[str, str],
    region: str,
    session=None,
    wait: bool = True,
    cpu: int = 512,
    memory: int = 2048,
) -> dict:
    """Create (or roll to the current image/config) the repo's MCP service.

    Reuses the stack's image, roles, cluster and namespace; REPO_IDS pins the
    served set to this one repo. Idempotent: safe to re-run after redeploys to
    roll the service onto the latest image. Returns
    {runtime_name, runtime_arn, runtime_id, status} where the runtime_* names
    are kept for registry/console compatibility (they mean the ECS service).
    """
    import time as _t

    validate_task_size(cpu, memory)
    sess = session or boto3
    ecs = sess.client("ecs", region_name=region)
    sd = sess.client("servicediscovery", region_name=region)
    cluster = outputs["EcsClusterName"]
    name = runtime_name_for(repo_id)

    def _find_cloudmap() -> str:
        token = None
        while True:
            kw = {
                "Filters": [{"Name": "NAMESPACE_ID", "Values": [outputs["CloudMapNamespaceId"]], "Condition": "EQ"}],
                "MaxResults": 100,
            }
            if token:
                kw["NextToken"] = token
            page = sd.list_services(**kw)
            for svc in page.get("Services", []):
                if svc.get("Name") == name:
                    return svc["Arn"]
            token = page.get("NextToken")
            if not token:
                return ""

    registry_arn = _find_cloudmap()
    if not registry_arn:
        try:
            registry_arn = sd.create_service(
                Name=name,
                NamespaceId=outputs["CloudMapNamespaceId"],
                DnsConfig={"RoutingPolicy": "MULTIVALUE", "DnsRecords": [{"Type": "A", "TTL": 10}]},
                HealthCheckCustomConfig={"FailureThreshold": 1},
                Description=f"graphify MCP service discovery for {name}",
            )["Service"]["Arn"]
        except sd.exceptions.ServiceAlreadyExists:
            registry_arn = _find_cloudmap()

    td = ecs.register_task_definition(
        family=name,
        networkMode="awsvpc",
        requiresCompatibilities=["FARGATE"],
        cpu=str(cpu),
        memory=str(memory),
        runtimePlatform={"cpuArchitecture": "ARM64", "operatingSystemFamily": "LINUX"},
        executionRoleArn=outputs["TaskExecRoleArn"],
        taskRoleArn=outputs["TaskRoleArn"],
        containerDefinitions=[{
            "name": "mcp",
            "image": outputs["TaskImageUri"],
            "essential": True,
            "portMappings": [{"containerPort": 8000, "protocol": "tcp"}],
            "environment": [
                {"name": "GRAPH_BUCKET", "value": outputs["GraphBucketName"]},
                {"name": "REPO_IDS", "value": repo_id},
                {"name": "DEFAULT_REPO_ID", "value": repo_id},
                # Arms the source code-search tools for THIS one repo — never
                # the hub, so they can't land on serverId "all".
                {"name": "CODE_SEARCH_REPO", "value": repo_id},
                {"name": "GRAPHIFY_MAX_CONTEXTS", "value": "2"},
                {"name": "SYNC_INTERVAL_SECONDS", "value": "180"},
            ],
            "logConfiguration": {
                "logDriver": "awslogs",
                "options": {
                    "awslogs-group": outputs["ServiceLogGroup"],
                    "awslogs-region": region,
                    "awslogs-stream-prefix": name,
                },
            },
        }],
    )["taskDefinition"]["taskDefinitionArn"]

    net = {"awsvpcConfiguration": {
        "subnets": [s for s in outputs["TaskSubnets"].split(",") if s],
        "securityGroups": [outputs["TaskSecurityGroup"]],
        "assignPublicIp": "ENABLED",
    }}

    def _describe():
        resp = ecs.describe_services(cluster=cluster, services=[name])
        for svc in resp.get("services", []):
            if svc.get("status") != "INACTIVE":
                return svc
        return None

    existing = _describe()
    for _ in range(24):  # wait out a DRAINING service from a recent delete
        if not existing or existing.get("status") != "DRAINING":
            break
        _t.sleep(5)
        existing = _describe()
    if existing and existing.get("status") == "DRAINING":
        raise RuntimeError(f"service {name} still draining; retry shortly")

    if existing:
        svc = ecs.update_service(
            cluster=cluster, service=name, taskDefinition=td, desiredCount=1,
            networkConfiguration=net,
        )["service"]
    else:
        svc = ecs.create_service(
            cluster=cluster, serviceName=name, taskDefinition=td, desiredCount=1,
            launchType="FARGATE", networkConfiguration=net,
            serviceRegistries=[{"registryArn": registry_arn}],
            deploymentConfiguration={"maximumPercent": 100, "minimumHealthyPercent": 0},
            enableECSManagedTags=True,
        )["service"]

    def _status(s) -> str:
        if not s:
            return "NONE"
        if s.get("status") == "DRAINING":
            return "DELETING"
        # runningCount alone lies during a roll (the OLD task is still counted
        # right after update_service) — READY requires the NEW task definition
        # to be the single settled deployment with a task running.
        deployments = s.get("deployments") or []
        primary = next((d for d in deployments if d.get("status") == "PRIMARY"), None)
        settled = (
            len(deployments) == 1
            and primary is not None
            and primary.get("taskDefinition") == td
            and primary.get("rolloutState", "COMPLETED") == "COMPLETED"
        )
        return "READY" if settled and int(s.get("runningCount", 0)) >= 1 else "STARTING"

    status = _status(svc)
    if wait:
        for _ in range(60):  # image pull + graph load can take a few minutes
            if status == "READY":
                break
            _t.sleep(5)
            status = _status(_describe())
    return {"runtime_name": name, "runtime_arn": svc["serviceArn"], "runtime_id": name, "status": status}


def delete_repo_runtime(repo_id: str, region: str, session=None) -> bool:
    import time as _t

    sess = session or boto3
    ecs = sess.client("ecs", region_name=region)
    sd = sess.client("servicediscovery", region_name=region)
    outputs = stack_outputs(region)
    cluster = outputs["EcsClusterName"]
    name = runtime_name_for(repo_id)
    deleted = False
    try:
        ecs.delete_service(cluster=cluster, service=name, force=True)
        deleted = True
    except (ecs.exceptions.ServiceNotFoundException, ecs.exceptions.ServiceNotActiveException):
        pass
    # Cloud Map cleanup is best-effort: ECS deregisters instances async; a
    # leftover empty service is harmless and reused on re-registration.
    try:
        token, arn = None, ""
        while not arn:
            kw = {"Filters": [{"Name": "NAMESPACE_ID", "Values": [outputs["CloudMapNamespaceId"]], "Condition": "EQ"}],
                  "MaxResults": 100}
            if token:
                kw["NextToken"] = token
            page = sd.list_services(**kw)
            arn = next((s["Arn"] for s in page.get("Services", []) if s.get("Name") == name), "")
            token = page.get("NextToken")
            if not token:
                break
        if arn:
            for attempt in range(6):
                try:
                    sd.delete_service(Id=arn.rsplit("/", 1)[-1])
                    break
                except sd.exceptions.ResourceInUse:
                    if attempt < 5:
                        _t.sleep(5)
    except Exception:
        pass
    return deleted
