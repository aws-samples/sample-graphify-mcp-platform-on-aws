"""Per-repo MCP service lifecycle on ECS Fargate, Lambda-adapted.

Each registered repo gets one always-warm Fargate service (desiredCount=1)
running the shared graphify MCP image, addressable inside the VPC as
<service_name>.<namespace> via Cloud Map. The public function names keep the
historical runtime_* vocabulary so handler.py, the registry attributes, and
the console's JSON fields stay unchanged:

  runtime_name / runtime_id  -> the ECS+Cloud Map service name
  runtime_arn                -> the ECS service ARN
  status                     -> READY / STARTING / DELETING / NONE
"""

from __future__ import annotations

import hashlib
import os
import re
import time

import boto3

REGION = os.environ["AWS_REGION"]

_ecs = boto3.client("ecs", region_name=REGION)
_sd = boto3.client("servicediscovery", region_name=REGION)

# Fargate cpu -> allowed memory (MiB) range, step 1024 above 2GB.
_FARGATE_MEM = {
    256: (512, 2048),
    512: (1024, 4096),
    1024: (2048, 8192),
    2048: (4096, 16384),
    4096: (8192, 30720),
}
DEFAULT_CPU = 512
DEFAULT_MEMORY = 2048


def validate_task_size(cpu: int, memory: int) -> None:
    rng = _FARGATE_MEM.get(cpu)
    if not rng:
        raise ValueError(f"cpu must be one of {sorted(_FARGATE_MEM)} (got {cpu})")
    # cpu=256 is the one tier with non-1024-step options (512/1024/2048);
    # every other tier requires an exact 1024-MiB multiple in [lo, hi].
    if cpu == 256:
        if memory not in (512, 1024, 2048):
            raise ValueError(f"memory for cpu=256 must be 512, 1024 or 2048 MiB (got {memory})")
        return
    lo, hi = rng
    if not (lo <= memory <= hi) or memory % 1024 != 0:
        raise ValueError(f"memory for cpu={cpu} must be {lo}..{hi} MiB in 1024 steps (got {memory})")


def runtime_name_for(repo_id: str) -> str:
    """Deterministic ECS/Cloud Map service name (also its DNS label).

    Lowercased — DNS labels are case-insensitive — with the hash tail keeping
    distinct repo_ids apart after squashing. Keep in sync with
    lambdas/mcp_proxy/handler.py:service_name_for and scripts/common.py.
    """
    slug = re.sub(r"[^a-z0-9_]", "_", repo_id.lower())
    tail = hashlib.sha1(repo_id.encode(), usedforsecurity=False).hexdigest()[:6]
    return f"g_{slug[:39]}_{tail}"


def _find_cloudmap_service(namespace_id: str, name: str) -> str:
    token = None
    while True:
        kwargs = {
            "Filters": [{"Name": "NAMESPACE_ID", "Values": [namespace_id], "Condition": "EQ"}],
            "MaxResults": 100,
        }
        if token:
            kwargs["NextToken"] = token
        page = _sd.list_services(**kwargs)
        for svc in page.get("Services", []):
            if svc.get("Name") == name:
                return svc["Arn"]
        token = page.get("NextToken")
        if not token:
            return ""


def _ensure_cloudmap_service(namespace_id: str, name: str) -> str:
    arn = _find_cloudmap_service(namespace_id, name)
    if arn:
        return arn
    try:
        created = _sd.create_service(
            Name=name,
            NamespaceId=namespace_id,
            DnsConfig={
                "RoutingPolicy": "MULTIVALUE",
                "DnsRecords": [{"Type": "A", "TTL": 10}],
            },
            # ECS manages instance health off task state; a TCP/HTTP checker
            # can't reach into the VPC anyway.
            HealthCheckCustomConfig={"FailureThreshold": 1},
            Description=f"graphify MCP service discovery for {name}",
        )
        return created["Service"]["Arn"]
    except _sd.exceptions.ServiceAlreadyExists:
        return _find_cloudmap_service(namespace_id, name)


def _describe(cluster: str, name: str) -> dict | None:
    resp = _ecs.describe_services(cluster=cluster, services=[name])
    for svc in resp.get("services", []):
        if svc.get("status") != "INACTIVE":
            return svc
    return None


def _service_status(svc: dict | None) -> str:
    if not svc:
        return "NONE"
    if svc.get("status") == "DRAINING":
        return "DELETING"
    # runningCount alone lies during a roll (the OLD task is still counted the
    # instant update_service returns) — the service is READY only once a single
    # settled deployment is running.
    deployments = svc.get("deployments") or []
    if len(deployments) != 1 or deployments[0].get("rolloutState", "COMPLETED") != "COMPLETED":
        return "STARTING"
    if int(svc.get("runningCount", 0)) >= max(1, int(svc.get("desiredCount", 1))):
        return "READY"
    return "STARTING"


def ensure_repo_runtime(repo_id: str, env: dict, cpu: int = DEFAULT_CPU, memory: int = DEFAULT_MEMORY) -> dict:
    """Create or roll forward the repo's dedicated MCP Fargate service.

    env must carry ECS_CLUSTER / TASK_IMAGE / TASK_ROLE_ARN / TASK_EXEC_ROLE_ARN /
    TASK_SUBNETS / TASK_SECURITY_GROUP / CLOUDMAP_NAMESPACE_ID /
    SERVICE_LOG_GROUP / GRAPH_BUCKET (injected from stack outputs via Lambda
    environment). Non-blocking: service converges async; the console polls.
    """
    validate_task_size(cpu, memory)
    cluster = env["ECS_CLUSTER"]
    name = runtime_name_for(repo_id)

    registry_arn = _ensure_cloudmap_service(env["CLOUDMAP_NAMESPACE_ID"], name)

    td = _ecs.register_task_definition(
        family=name,
        networkMode="awsvpc",
        requiresCompatibilities=["FARGATE"],
        cpu=str(cpu),
        memory=str(memory),
        runtimePlatform={"cpuArchitecture": "ARM64", "operatingSystemFamily": "LINUX"},
        executionRoleArn=env["TASK_EXEC_ROLE_ARN"],
        taskRoleArn=env["TASK_ROLE_ARN"],
        containerDefinitions=[
            {
                "name": "mcp",
                "image": env["TASK_IMAGE"],
                "essential": True,
                "portMappings": [{"containerPort": 8000, "protocol": "tcp"}],
                "environment": [
                    {"name": "GRAPH_BUCKET", "value": env["GRAPH_BUCKET"]},
                    {"name": "REPO_IDS", "value": repo_id},
                    {"name": "DEFAULT_REPO_ID", "value": repo_id},
                    # Arms code-search for THIS repo only; the hub never sets
                    # it, so the tools can't reach serverId "all"
                    # (authorizer-exempt from grants).
                    {"name": "CODE_SEARCH_REPO", "value": repo_id},
                    {"name": "GRAPHIFY_MAX_CONTEXTS", "value": "2"},
                    {"name": "SYNC_INTERVAL_SECONDS", "value": "180"},
                ],
                "logConfiguration": {
                    "logDriver": "awslogs",
                    "options": {
                        "awslogs-group": env["SERVICE_LOG_GROUP"],
                        "awslogs-region": REGION,
                        "awslogs-stream-prefix": name,
                    },
                },
            }
        ],
    )["taskDefinition"]["taskDefinitionArn"]

    net = {
        "awsvpcConfiguration": {
            "subnets": [s for s in env["TASK_SUBNETS"].split(",") if s],
            "securityGroups": [env["TASK_SECURITY_GROUP"]],
            # Public IP for outbound-only traffic (ECR pull); the SG admits
            # nothing but the data-plane proxy Lambda.
            "assignPublicIp": "ENABLED",
        }
    }

    existing = _describe(cluster, name)
    for _ in range(4):  # brief grace for a mid-teardown service; console polls later
        if not existing or existing.get("status") != "DRAINING":
            break
        time.sleep(3)
        existing = _describe(cluster, name)

    if existing and existing.get("status") == "DRAINING":
        raise RuntimeError(f"service {name} is still draining from a previous delete; retry shortly")

    if existing:
        svc = _ecs.update_service(
            cluster=cluster,
            service=name,
            taskDefinition=td,
            desiredCount=1,
            networkConfiguration=net,
        )["service"]
    else:
        svc = _ecs.create_service(
            cluster=cluster,
            serviceName=name,
            taskDefinition=td,
            desiredCount=1,
            launchType="FARGATE",
            networkConfiguration=net,
            serviceRegistries=[{"registryArn": registry_arn}],
            # Single memory-heavy task: stop-then-start on roll (never two
            # graph-resident tasks at once).
            deploymentConfiguration={"maximumPercent": 100, "minimumHealthyPercent": 0},
            enableECSManagedTags=True,
        )["service"]

    return {
        "runtime_name": name,
        "runtime_arn": svc["serviceArn"],
        "runtime_id": name,
        "status": _service_status(svc),
    }


def runtime_status(runtime_id: str) -> str:
    """runtime_id is the ECS service name (legacy runtime ids report UNKNOWN)."""
    cluster = os.environ.get("ECS_CLUSTER", "")
    if not cluster or not runtime_id:
        return "UNKNOWN"
    try:
        return _service_status(_describe(cluster, runtime_id))
    except Exception:
        return "UNKNOWN"


def delete_repo_runtime(repo_id: str, runtime_id: str = "", cloudmap_retry: bool = True) -> bool:
    cluster = os.environ.get("ECS_CLUSTER", "")
    name = runtime_id or runtime_name_for(repo_id)
    # A legacy row may still carry a pre-Fargate runtime id; the derived
    # service name is the one that actually exists on the cluster.
    if not re.fullmatch(r"g_[a-z0-9_]+_[0-9a-f]{6}", name):
        name = runtime_name_for(repo_id)
    deleted = False
    try:
        _ecs.delete_service(cluster=cluster, service=name, force=True)
        deleted = True
    except (_ecs.exceptions.ServiceNotFoundException, _ecs.exceptions.ServiceNotActiveException):
        pass
    # Cloud Map cleanup is best-effort: ECS deregisters instances async, so
    # deletion can race ResourceInUse — a leftover empty service is harmless
    # and gets reused verbatim on re-registration. cloudmap_retry=False skips
    # the sleep-retries so a BULK caller (offboarding a user with many
    # sources) can't accumulate 6s/source and blow the 28s request budget.
    try:
        ns_id = os.environ.get("CLOUDMAP_NAMESPACE_ID", "")
        if ns_id:
            arn = _find_cloudmap_service(ns_id, name)
            if arn:
                sd_id = arn.rsplit("/", 1)[-1]
                attempts = 3 if cloudmap_retry else 1
                for attempt in range(attempts):
                    try:
                        _sd.delete_service(Id=sd_id)
                        break
                    except _sd.exceptions.ResourceInUse:
                        if cloudmap_retry and attempt < attempts - 1:
                            time.sleep(3)
    except Exception:
        pass
    return deleted
