"""REST API REQUEST authorizer for the MCP data plane.

Validates the platform-issued key in X-Graphify-Key against the platform
table and returns an IAM policy scoped to exactly the /mcp/{serverId}
resources the key may reach, plus the usageIdentifierKey that drives the
per-key usage plan (apiKeySource=AUTHORIZER).

Result caching is OFF (TTL 0) so revocation is immediate; every request
costs one strongly-consistent GetItem, which is the price of that guarantee.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import time
import zlib

import boto3

TABLE_NAME = os.environ["PLATFORM_TABLE"]
REGISTRY_TABLE = os.environ["REGISTRY_TABLE"]

_ddb = boto3.client("dynamodb")

# gfy_{live|test}_{kid:12 Crockford32}_{secret:43 base64url}{crc:6 base62}
KEY_RE = re.compile(r"^gfy_(live|test)_([0-9A-HJKMNP-TV-Z]{12})_([A-Za-z0-9_-]{43})([A-Za-z0-9]{6})$")
_B62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"


def b62_crc(payload: str) -> str:
    n = zlib.crc32(payload.encode())
    out = []
    for _ in range(6):
        out.append(_B62[n % 62])
        n //= 62
    return "".join(reversed(out))


def usage_identifier_for(kid: str) -> str:
    # Deterministic, NON-secret value registered as an API Gateway API key at
    # issuance time; >=20 chars per API GW rules. Never shown to clients.
    return f"gfyusage-{kid}-graphify"


def _header(event: dict, name: str) -> str:
    headers = event.get("headers") or {}
    lname = name.lower()
    for k, v in headers.items():
        if k.lower() == lname:
            return v or ""
    return ""


def _policy(effect: str, resources: list[str], context: dict, usage_key: str = "") -> dict:
    doc = {
        "principalId": context.get("kid", "anonymous"),
        "policyDocument": {
            "Version": "2012-10-17",
            "Statement": [
                {"Action": "execute-api:Invoke", "Effect": effect, "Resource": resources}
            ],
        },
        "context": {k: str(v) for k, v in context.items()},
    }
    if usage_key:
        doc["usageIdentifierKey"] = usage_key
    return doc


def _arn_base(method_arn: str) -> str:
    # arn:aws:execute-api:region:acct:apiId/stage/METHOD/path...
    prefix, _, tail = method_arn.rpartition(":")
    api_id, stage = tail.split("/", 2)[:2]
    return f"{prefix}:{api_id}/{stage}"


def handler(event: dict, _ctx) -> dict:
    method_arn = event["methodArn"]
    deny = lambda reason: _policy("Deny", [method_arn], {"kid": "denied", "reason": reason})

    raw_key = _header(event, "X-Graphify-Key").strip()
    m = KEY_RE.fullmatch(raw_key)
    if not m:
        # Structurally invalid: reject without a DB read. 401 (missing) vs
        # 403 (present but bad) both end the request; REST maps a raised
        # "Unauthorized" to 401.
        raise Exception("Unauthorized")
    _, kid, _, crc = m.group(1), m.group(2), m.group(3), m.group(4)
    if b62_crc(raw_key[: -6]) != crc:
        raise Exception("Unauthorized")

    item = _ddb.get_item(
        TableName=TABLE_NAME,
        Key={"pk": {"S": f"AKEY#{kid}"}, "sk": {"S": "META"}},
        ConsistentRead=True,
    ).get("Item")
    if not item:
        return deny("unknown_key")

    stored = item.get("key_hash", {}).get("B", b"")
    presented = hashlib.sha256(raw_key.encode()).digest()
    if not stored or not hmac.compare_digest(bytes(stored), presented):
        return deny("hash_mismatch")

    now = int(time.time())
    if item.get("status", {}).get("S") != "active":
        return deny("revoked")
    expires_at = int(item.get("expires_at", {}).get("N", "0") or 0)
    if expires_at and now >= expires_at:
        return deny("expired")

    server_id = (event.get("pathParameters") or {}).get("serverId", "")
    scope_type = item.get("scope_type", {}).get("S", "ALL")
    scope_ids = set(item.get("scope_server_ids", {}).get("SS", []) or [])
    if scope_type == "SERVERS" and server_id not in scope_ids:
        return deny("out_of_scope")

    owner_sub = item.get("owner_sub", {}).get("S", "")
    # Tenant isolation on the data plane: the hub ("all") serves only the
    # merged PUBLIC graph, so it needs no per-repo grant. Any other serverId
    # is a repo_id — a PRIVATE (PAT-cloned) repo may be reached only by a key
    # whose owner holds a grant on it. Public pooled repos stay open to any
    # valid key (they are already searchable through the hub). This closes
    # the cross-tenant read that key-scope alone does not: an ALL-scope key
    # otherwise matched POST/mcp/* for every tenant's private runtime.
    if server_id != "all":
        # ConsistentRead so a repo/grant written milliseconds earlier (console
        # register -> immediate connect) is visible, not a stale replica miss.
        reg = _ddb.get_item(
            TableName=REGISTRY_TABLE,
            Key={"repo_id": {"S": server_id}},
            ConsistentRead=True,
        ).get("Item")
        if not reg or reg.get("enabled", {}).get("S") != "1":
            return deny("unknown_server")
        graph_scope = reg.get("graph_scope", {}).get("S", "public")
        if graph_scope != "public":
            if not owner_sub:
                return deny("private_requires_grant")
            grant = _ddb.get_item(
                TableName=TABLE_NAME,
                Key={"pk": {"S": f"USER#{owner_sub}"}, "sk": {"S": f"REPO#{server_id}"}},
                ConsistentRead=True,
            ).get("Item")
            if not grant:
                return deny("no_grant_for_private_repo")

    base = _arn_base(method_arn)
    if scope_type == "ALL":
        resources = [f"{base}/POST/mcp/*"]
    else:
        resources = [f"{base}/POST/mcp/{sid}" for sid in sorted(scope_ids)]

    context = {
        "kid": kid,
        "ownerSub": item.get("owner_sub", {}).get("S", ""),
        "tier": item.get("tier", {}).get("S", "standard"),
        "scopeType": scope_type,
        "scopeServerIds": ",".join(sorted(scope_ids)) if scope_ids else "*",
    }
    return _policy("Allow", resources, context, usage_identifier_for(kid))
