"""MCP data-plane proxy: API Gateway (REST, buffered) -> Fargate MCP service.

The upstreams are stateless streamable-HTTP MCP servers in json_response mode
running as always-warm ECS Fargate tasks, so a request/response byte-through
proxy is sufficient — no SSE relaying. serverId "all" targets the hub service
(merged public graph); any other serverId is a repo_id whose dedicated service
is addressed by its deterministic Cloud Map DNS name (derived here from the
repo_id — no registry attribute needed — but the registry row still gates
enabled=1). Tasks hold the graph resident, so warm calls are sub-second; a
task mid-restart shows up as a fast connection error, which is retried.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import socket
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

import boto3
from botocore.config import Config

PLATFORM_TABLE = os.environ["PLATFORM_TABLE"]
REGISTRY_TABLE = os.environ["REGISTRY_TABLE"]
HUB_HOST = os.environ["HUB_HOST"]
SERVICE_DNS_SUFFIX = os.environ["SERVICE_DNS_SUFFIX"]
SERVICE_PORT = int(os.environ.get("SERVICE_PORT", "8000"))

# API Gateway cuts the integration at 29s; leave headroom for usage writes.
UPSTREAM_TIMEOUT = 26

_ddb = boto3.client("dynamodb", config=Config(retries={"max_attempts": 1}))

_enabled_cache: dict[str, tuple[bool, float]] = {}
_ENABLED_TTL = 60.0

USAGE_TTL_DAYS = 400


def service_name_for(repo_id: str) -> str:
    """Deterministic Cloud Map/ECS service name for a repo.

    Lowercased (DNS labels are case-insensitive and Cloud Map lowercases
    registrations), non [a-z0-9_] squashed to _, hash tail keeps distinct
    repo_ids from colliding after squashing. Keep in sync with
    lambdas/platform_api/runtimes.py and scripts/common.py.
    """
    slug = re.sub(r"[^a-z0-9_]", "_", repo_id.lower())
    tail = hashlib.sha1(repo_id.encode(), usedforsecurity=False).hexdigest()[:6]
    return f"g_{slug[:39]}_{tail}"


def _header(event: dict, name: str) -> str:
    headers = event.get("headers") or {}
    lname = name.lower()
    for k, v in headers.items():
        if k.lower() == lname:
            return v or ""
    return ""


def _resp(status: int, body: str = "", content_type: str = "application/json", extra: dict | None = None) -> dict:
    headers = {"Content-Type": content_type, **(extra or {})}
    return {"statusCode": status, "headers": headers, "body": body}


def _jsonrpc_error(status: int, code: int, message: str, req_id=None) -> dict:
    body = json.dumps({"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}})
    return _resp(status, body)


def resolve_host(server_id: str) -> str | None:
    if server_id == "all":
        return HUB_HOST
    cached = _enabled_cache.get(server_id)
    if cached and time.monotonic() - cached[1] < _ENABLED_TTL:
        enabled = cached[0]
    else:
        item = _ddb.get_item(
            TableName=REGISTRY_TABLE, Key={"repo_id": {"S": server_id}}
        ).get("Item")
        enabled = bool(item) and item.get("enabled", {}).get("S") == "1"
        _enabled_cache[server_id] = (enabled, time.monotonic())
    if not enabled:
        return None
    return f"{service_name_for(server_id)}.{SERVICE_DNS_SUFFIX}"


def record_usage(kid: str, owner: str, server_id: str, is_error: bool) -> None:
    """Display counters — best-effort, never fail the MCP request."""
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    ttl = int(time.time()) + USAGE_TTL_DAYS * 86400
    err = "1" if is_error else "0"
    # Flat s#<serverId> attribute (ADD on a nested map path throws when the
    # parent is absent — flat attributes avoid a two-step init on the hot path).
    for pk, breakdown in (
        (f"USAGE#KEY#{kid}", f"s#{server_id}"),
        (f"USAGE#SRV#{server_id}", f"k#{kid}"),
    ):
        try:
            _ddb.update_item(
                TableName=PLATFORM_TABLE,
                Key={"pk": {"S": pk}, "sk": {"S": f"D#{day}"}},
                UpdateExpression="ADD #r :one, #e :err, #b :one SET #t = if_not_exists(#t, :ttl), #o = if_not_exists(#o, :own)",
                ExpressionAttributeNames={"#r": "req", "#e": "err", "#b": breakdown, "#t": "ttl", "#o": "owner_sub"},
                ExpressionAttributeValues={
                    ":one": {"N": "1"},
                    ":err": {"N": err},
                    ":ttl": {"N": str(ttl)},
                    ":own": {"S": owner},
                },
                ReturnValues="NONE",
            )
        except Exception:
            pass


def _forward(host: str, body: str, session_id: str) -> tuple[int, str, str, str]:
    """POST the JSON-RPC body to the service; returns (status, body, ctype, mcp_session).

    One SHARED 26s deadline across all attempts — never per-attempt. A retry
    is only worth taking on connection-level failures (task restarting / DNS
    not yet registered), but those are not guaranteed to fail fast (a peer can
    reset an established connection seconds in), so every attempt gets only
    the REMAINING budget and the loop stops when it runs out. Without this,
    3 x 26s would blow both the 29s API Gateway cut (losing the JSON-RPC error
    envelope) and the 60s Lambda timeout (losing the usage-counter write).
    """
    url = f"http://{host}:{SERVICE_PORT}/mcp"
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    deadline = time.monotonic() + UPSTREAM_TIMEOUT
    last_exc: Exception | None = None
    for attempt in range(3):
        remaining = deadline - time.monotonic()
        if remaining <= 0.5:
            break
        req = urllib.request.Request(url, data=body.encode(), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=remaining) as resp:
                out = resp.read().decode("utf-8", errors="replace")
                return (
                    resp.status,
                    out,
                    resp.headers.get("Content-Type", "application/json"),
                    resp.headers.get("Mcp-Session-Id", ""),
                )
        except urllib.error.HTTPError as exc:
            out = exc.read().decode("utf-8", errors="replace")
            return (
                exc.code,
                out,
                exc.headers.get("Content-Type", "application/json") if exc.headers else "application/json",
                exc.headers.get("Mcp-Session-Id", "") if exc.headers else "",
            )
        except (ConnectionError, socket.gaierror, urllib.error.URLError) as exc:
            # URLError wraps timeouts too — a spent budget never retries.
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, (TimeoutError, socket.timeout)):
                raise
            last_exc = exc
            if attempt < 2:
                time.sleep(min(0.5 * (attempt + 1), max(deadline - time.monotonic(), 0)))
    if last_exc is None:
        raise TimeoutError("upstream budget exhausted")
    raise last_exc


def handler(event: dict, _ctx) -> dict:
    server_id = (event.get("pathParameters") or {}).get("serverId", "")
    auth = (event.get("requestContext") or {}).get("authorizer") or {}
    kid = auth.get("kid", "")
    owner = auth.get("ownerSub", "")

    # Defense in depth: re-check scope from the authorizer context so serving
    # never depends on the authorizer's policy shape alone.
    scope = auth.get("scopeServerIds", "*")
    if scope != "*" and server_id not in scope.split(","):
        return _jsonrpc_error(403, -32001, "API key is not scoped to this server")

    host = resolve_host(server_id)
    if not host:
        return _jsonrpc_error(404, -32001, f"unknown MCP server '{server_id}' (not registered or disabled)")

    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode("utf-8", errors="replace")
    try:
        rpc = json.loads(body) if body.strip() else {}
    except json.JSONDecodeError:
        return _jsonrpc_error(400, -32700, "parse error: request body is not valid JSON")
    req_id = rpc.get("id") if isinstance(rpc, dict) else None
    is_notification = isinstance(rpc, dict) and "id" not in rpc

    # Every service is pinned to exactly one graph (a repo's, or the hub's
    # merged PUBLIC graph), so a tool-call project_path is never legitimate
    # here — and a client-supplied one could pivot to another graph path on
    # the task filesystem. Strip it before forwarding.
    if isinstance(rpc, dict) and isinstance(rpc.get("params"), dict):
        args = rpc["params"].get("arguments")
        if isinstance(args, dict) and "project_path" in args:
            args.pop("project_path", None)
            body = json.dumps(rpc)

    client_session = _header(event, "Mcp-Session-Id").strip()

    upstream_error = False
    try:
        status, out, content_type, mcp_session = _forward(host, body, client_session)

        # json_response mode answers with a bare JSON envelope; tolerate an
        # SSE-framed reply anyway (data: lines) and unwrap it.
        if out.lstrip().startswith("data:") or "\ndata:" in out:
            for line in out.splitlines():
                if line.startswith("data:"):
                    out = line[5:].strip()
                    content_type = "application/json"
                    break

        if not out.strip():
            # Notifications return no body; 202 is the streamable-HTTP answer.
            status = 202 if is_notification else status
            record_usage(kid, owner, server_id, is_error=False)
            return _resp(status, "")

        try:
            upstream_error = isinstance(json.loads(out), dict) and "error" in json.loads(out)
        except json.JSONDecodeError:
            upstream_error = status >= 400
        record_usage(kid, owner, server_id, is_error=upstream_error)
        extra = {"Mcp-Session-Id": mcp_session} if mcp_session else {}
        return _resp(status, out, content_type, extra)
    except (TimeoutError, socket.timeout):
        record_usage(kid, owner, server_id, is_error=True)
        return _jsonrpc_error(504, -32000, "upstream MCP server timed out", req_id)
    except Exception as exc:  # noqa: BLE001 — single edge, fail as JSON-RPC
        record_usage(kid, owner, server_id, is_error=True)
        reason = getattr(exc, "reason", None)
        if isinstance(reason, (TimeoutError, socket.timeout)):
            return _jsonrpc_error(504, -32000, "upstream MCP server timed out", req_id)
        return _jsonrpc_error(
            502, -32000,
            f"upstream MCP server unavailable (task may be starting): {type(exc).__name__}",
            req_id,
        )
