"""Source-group management; no clients or infrastructure are created on import.

Integration:
    handle(event, ident, method, path, *, platform, registry, ddb, s3,
           lambda_client, worker_fn, platform_table, registry_table, bucket,
           mcp_base, cognito, user_pool_id) -> (status, JSON-compatible dict)
    list_for_user(ident, *, ddb, platform_table, registry_table, mcp_base="",
                  **unused_dependencies) -> list[dict]

``ddb`` is the low-level DynamoDB client. ``platform`` and ``registry`` are
accepted for compatibility with the platform router. Metadata reads deliberately
call group_access.require_access(check_versions=False); data routes must perform
their own current-version checks. Owners with revoked source access receive
minimal recovery metadata in GET/list and may POST configuration/removals or
DELETE through the ownership recovery path. No previous build data is returned.

All mutations except create require expected_revision. Create and rebuild also
require idempotency_key (8..128 ASCII letters, digits, "_" or "-"). Sources live
in one META.sources array. GROUP#/MEMBER# rows discover members only: USER#/GROUP#
grants, strongly read again, remain authoritative.

Build reservation atomically writes META and GROUP#/BUILD#<build_id>. The ledger
pins source_versions, source_description_versions and group configuration.
Version artifacts are source-versions/<sid>/<version>/manifest.json, graph.json
and src.tar.gz (the source publisher's contract). No source rebuild is scheduled.
Worker invocation is exactly
{group_id, build_id, revision, requested_by}, InvocationType="Event".

A replay NEVER invokes the worker again, including ambiguous invoke failures.
The 900-second reservation survives configuration edits. The worker must fence
completion on BOTH build_id and revision, and leave the build ledger intact.
No SDK client with automatic invoke retries should be passed by the router.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import time
from decimal import Decimal

from boto3.dynamodb.types import TypeDeserializer, TypeSerializer

from group_access import GroupError, get_item, require_access, source_access


MAX_GROUPS = 20
MAX_SOURCES = 8
BUILD_LEASE_SECONDS = 900
_GROUP_ID = re.compile(r"grp_[0-9a-f]{32}")
_TOKEN = re.compile(r"[A-Za-z0-9_-]{8,128}")
_SOURCE_ID = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,199}")
_SUB = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}")
_EMAIL = re.compile(r'[^@\s"\\]+@[^@\s"\\]+\.[^@\s"\\]+')
_SERIALIZER = TypeSerializer()
_DESERIALIZER = TypeDeserializer()
_CONFIG_FIELDS = ("name", "description", "sources", "llm_enabled", "model")
_SAFE_USAGE = ("input_tokens", "output_tokens", "total_tokens", "candidate_pairs",
               "relations", "duration_seconds", "model_calls", "model_retries",
               "input_tokens_reserved", "output_tokens_reserved", "reused_pairs",
               "unknown_usage_attempts", "max_token_outputs", "reasoning_blocks")
_SAFE_ERRORS = frozenset("""
ACCESS_DENIED ARTIFACT_TRUNCATED BUILD_INPUT_CHANGED BUILD_FAILED BUILD_TIMEOUT
BYTE_LIMIT_EXCEEDED CHECKSUM_MISMATCH DEADLINE_EXCEEDED GROUP_CHANGED IMMUTABLE_CONFLICT
INVALID_ARTIFACT INVALID_BUILD_CONFIG INVALID_BUILD_LEDGER INVALID_ENGINE_RESULT
INVALID_EVENT INVALID_MEMBERSHIP INVALID_SNAPSHOT INVALID_SOURCE_MANIFEST
SOURCE_VERSION_CHANGED SOURCE_VERSION_MISSING STALE_INPUT STATUS_UPDATE_FAILED
FILE_LIMIT GRAPH_LIMIT INVALID_EVIDENCE INVALID_GRAPH INVALID_INPUT SNAPSHOT_LIMIT
SOURCE_LIMIT TEXT_LIMIT UNSAFE_SNAPSHOT BUDGET_EXCEEDED CANDIDATE_LIMIT COMPARISON_LIMIT
EDGE_LIMIT EVIDENCE_INDEX_LIMIT EVIDENCE_LINE_LIMIT MODEL_ERROR MODEL_OUTPUT_INVALID
MODEL_UNAVAILABLE MODEL_USAGE_UNKNOWN MODEL_MAX_TOKENS NODE_LIMIT LLM_UNAVAILABLE
""".split())


class _Error(GroupError):
    """A locally authored, safe-to-display API validation error."""


def _typed(value):
    return {key: _SERIALIZER.serialize(item) for key, item in value.items()}


def _decoded(value):
    return {key: _DESERIALIZER.deserialize(item) for key, item in value.items()}


def _json(value):
    if isinstance(value, Decimal):
        return int(value) if value == int(value) else float(value)
    if isinstance(value, dict):
        return {key: _json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json(item) for item in value]
    return value


def _key(gid, sk="META"):
    return {"pk": f"GROUP#{gid}", "sk": sk}


def _grant_key(sub, gid):
    return {"pk": f"USER#{sub}", "sk": f"GROUP#{gid}"}


def _code(exc):
    return getattr(exc, "response", {}).get("Error", {}).get("Code", "")


def _conflict(exc):
    if _code(exc) in ("ConditionalCheckFailedException", "TransactionConflictException"):
        return True
    if _code(exc) == "TransactionCanceledException":
        reasons = getattr(exc, "response", {}).get("CancellationReasons", [])
        return not reasons or any(r.get("Code") in (
            "ConditionalCheckFailed", "TransactionConflict",
        ) for r in reasons)
    return False


def _body(event):
    raw = event.get("body") or "{}"
    try:
        if event.get("isBase64Encoded"):
            raw = base64.b64decode(raw, validate=True).decode("utf-8")
        if len(raw) > 32768:
            raise _Error(400, "request body is too large")
        body = json.loads(raw)
    except (ValueError, TypeError, UnicodeError, binascii.Error):
        raise _Error(400, "request body must be a JSON object") from None
    if not isinstance(body, dict):
        raise _Error(400, "request body must be a JSON object")
    return body


def _text(value, field, maximum, *, required=False):
    if not isinstance(value, str) or len(value) > maximum or "\x00" in value:
        raise _Error(400, f"{field} must be text of at most {maximum} characters")
    value = value.strip()
    if required and not value:
        raise _Error(400, f"{field} is required")
    return value


def validate_source_description(value):
    """Optional shared validator for the router's source-description endpoint."""
    return _text(value, "description", 500)


def _revision(body):
    revision = body.get("expected_revision")
    if type(revision) is not int or revision < 1:
        raise _Error(400, "expected_revision must be a positive integer")
    return revision


def _token(body):
    token = body.get("idempotency_key")
    if not isinstance(token, str) or not _TOKEN.fullmatch(token):
        raise _Error(400, "idempotency_key must be 8..128 letters, digits, '_' or '-'")
    return token


def _digest(*parts):
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def _config(body, previous=None):
    allowed = set(_CONFIG_FIELDS) | {"expected_revision", "idempotency_key"}
    if set(body) - allowed:
        raise _Error(400, "unsupported group field")
    config = {field: (previous or {}).get(field, default) for field, default in (
        ("name", ""), ("description", ""), ("sources", []),
        ("llm_enabled", False), ("model", ""),
    )}
    config.update({field: body[field] for field in _CONFIG_FIELDS if field in body})
    config["name"] = _text(config["name"], "name", 120, required=True)
    config["description"] = _text(config["description"], "description", 1000)
    if type(config["llm_enabled"]) is not bool:
        raise _Error(400, "llm_enabled must be a boolean")
    model = _text(config["model"], "model", 200)
    if model and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]*", model):
        raise _Error(400, "invalid model")
    config["model"] = model
    sources = config["sources"]
    if not isinstance(sources, list) or len(sources) > MAX_SOURCES:
        raise _Error(400, f"sources must be an array with at most {MAX_SOURCES} entries")
    previous_sources = {
        source["source_id"]: source for source in (previous or {}).get("sources", [])
        if isinstance(source, dict) and isinstance(source.get("source_id"), str)
    }
    seen, clean = set(), []
    for source in sources:
        if not isinstance(source, dict) or set(source) - {"source_id", "role", "description"}:
            raise _Error(400, "invalid source entry")
        sid = source.get("source_id")
        if (not isinstance(sid, str) or not _SOURCE_ID.fullmatch(sid)
                or sid in ("all", "__all__", ".", "..") or sid in seen):
            raise _Error(400, "source_id must identify a unique individual source")
        seen.add(sid)
        prior = previous_sources.get(sid, {})
        clean.append({
            "source_id": sid,
            "role": _text(source.get("role", prior.get("role", "")), "source role", 64),
            "description": validate_source_description(source.get("description", prior.get("description", ""))),
        })
    config["sources"] = clean
    return config


def _check(table, key, expression, values=None, names=None):
    args = {"TableName": table, "Key": _typed(key), "ConditionExpression": expression}
    if values:
        args["ExpressionAttributeValues"] = _typed(values)
    if names:
        args["ExpressionAttributeNames"] = names
    return {"ConditionCheck": args}


def _matches(table, key, row, fields):
    clauses, values, names = ["attribute_exists(#key)"], {}, {"#key": next(iter(key))}
    for i, field in enumerate(fields):
        name, value = f"#f{i}", f":v{i}"
        names[name] = field
        if field in row:
            clauses.append(f"{name} = {value}")
            values[value] = row[field]
        else:
            clauses.append(f"attribute_not_exists({name})")
    return _check(table, key, " AND ".join(clauses), values, names)


def _active(ddb, table, sub):
    if get_item(ddb, table, {"pk": f"USER#{sub}", "sk": "DELETED"}):
        raise _Error(403, "account is unavailable")
    return _check(table, {"pk": f"USER#{sub}", "sk": "DELETED"},
                  "attribute_not_exists(pk)")


def _source_access(ddb, table, registry_table, sub, sources):
    """Read every source and grant strongly; return transaction freshness guards."""
    rows, checks = {}, []
    for source in sources:
        sid = source["source_id"]
        row = source_access(ddb, table, registry_table, sub, sid)
        if row.get("graph_scope") != "public":
            key = {"pk": f"USER#{sub}", "sk": f"REPO#{sid}"}
            grant = get_item(ddb, table, key)
            if not grant or str(grant.get("status", "")).upper() in (
                "DELETED", "REVOKED", "DISABLED",
            ):
                raise _Error(403, "one or more sources are unavailable or not accessible")
            checks.append(_matches(table, key, grant, ("status",)))
        checks.append(_matches(registry_table, {"repo_id": sid}, row,
                               ("enabled", "graph_scope", "active_source_version",
                                "description_version")))
        rows[sid] = row
    return rows, checks


def _authority(ddb, table, sub, gid, *, owner=False):
    grant = get_item(ddb, table, _grant_key(sub, gid))
    roles = ("owner",) if owner else ("owner", "editor")
    if not grant or grant.get("role") not in roles:
        raise _Error(403, "group write access is required")
    return _matches(table, _grant_key(sub, gid), grant, ("role", "status"))


def _access(ddb, table, registry_table, sub, gid, *, write=False, recovery=False):
    try:
        return require_access(ddb, table, registry_table, sub, gid,
                              write=write, check_versions=False), False
    except GroupError as exc:
        if not recovery or exc.status not in (403, 404, 409):
            raise
        # The grant alone is insufficient: both authoritative owner fields
        # must match. This path never authorizes any artifact/data request.
        meta = get_item(ddb, table, _key(gid))
        grant = get_item(ddb, table, _grant_key(sub, gid))
        if (not meta or meta.get("status") == "DELETED" or meta.get("owner_sub") != sub
                or not grant or grant.get("role") != "owner"):
            raise exc
        return meta, True


def _query(ddb, table, pk, prefix):
    items, cursor = [], None
    while True:
        args = {
            "TableName": table, "ConsistentRead": True,
            "KeyConditionExpression": "pk = :p AND begins_with(sk, :s)",
            "ExpressionAttributeValues": _typed({":p": pk, ":s": prefix}),
        }
        if cursor:
            args["ExclusiveStartKey"] = cursor
        page = ddb.query(**args)
        items.extend(_decoded(row) for row in page.get("Items", []))
        cursor = page.get("LastEvaluatedKey")
        if not cursor:
            return items


def _limit(ddb, table, sub):
    # Best effort by contract: concurrent creations with distinct tokens can
    # exceed the cap. A replay is checked before calling this function.
    count = 0
    for grant in _query(ddb, table, f"USER#{sub}", "GROUP#"):
        gid = grant.get("sk", "")[6:]
        if grant.get("role") not in ("owner", "editor", "viewer"):
            continue
        meta = get_item(ddb, table, _key(gid))
        if meta and meta.get("status") != "DELETED":
            count += 1
    if count >= MAX_GROUPS:
        raise _Error(409, f"an account may have at most {MAX_GROUPS} groups")


def _transaction(ddb, operations):
    # Identical guards can occur for public sources checked for both inviter
    # and invitee. DynamoDB permits each item only once per transaction.
    unique, seen = [], {}
    for operation in operations:
        if "ConditionCheck" in operation:
            guard = operation["ConditionCheck"]
            identity = (guard["TableName"], json.dumps(guard["Key"], sort_keys=True))
            if identity in seen:
                if seen[identity] != guard:
                    raise _Error(409, "source or access changed; reload and retry")
                continue
            seen[identity] = guard
        unique.append(operation)
    ddb.transact_write_items(TransactItems=unique)


def _put(table, row, condition=None, values=None):
    args = {"TableName": table, "Item": _typed(row)}
    if condition:
        args["ConditionExpression"] = condition
    if values:
        args["ExpressionAttributeValues"] = _typed(values)
    return {"Put": args}


def _meta_update(table, gid, previous, fields, *, owner=None, lease_before=None, remove=()):
    # Sparse discovery index avoids scanning unrelated usage/key records.
    # Every edit also upgrades groups created before the index was introduced.
    fields = dict(fields)
    if fields.get("status") == "DELETED":
        remove = tuple(dict.fromkeys((*remove, "gsi1pk", "gsi1sk")))
    else:
        fields.update(gsi1pk="GROUPS", gsi1sk=gid)
    names = {"#revision": "revision", "#status": "status"}
    values = {":expected": previous["revision"], ":deleted": "DELETED"}
    clauses = ["attribute_exists(pk)", "#revision = :expected", "#status <> :deleted"]
    assignments = []
    for index, (field, value) in enumerate(fields.items()):
        name, placeholder = f"#u{index}", f":u{index}"
        names[name] = field
        values[placeholder] = value
        assignments.append(f"{name} = {placeholder}")
    if owner is not None:
        clauses.append("owner_sub = :owner")
        values[":owner"] = owner
    if lease_before is not None:
        clauses.append("(attribute_not_exists(build_started_at) OR build_started_at <= :expired"
                       " OR build_started_at = :zero)")
        values[":expired"], values[":zero"] = lease_before, 0
    expression = "SET " + ", ".join(assignments)
    if remove:
        aliases = []
        for index, field in enumerate(remove):
            alias = f"#remove{index}"
            names[alias] = field
            aliases.append(alias)
        expression += " REMOVE " + ", ".join(aliases)
    return {"Update": {
        "TableName": table, "Key": _typed(_key(gid)),
        "ConditionExpression": " AND ".join(clauses),
        "UpdateExpression": expression,
        "ExpressionAttributeNames": names,
        "ExpressionAttributeValues": _typed(values),
    }}


def _view(meta, sub, grant, mcp_base, *, recovery=False, current_versions=None,
          current_descriptions=None):
    if not grant or grant.get("role") not in ("owner", "editor", "viewer"):
        raise _Error(403, "group access denied")
    if recovery and (meta.get("owner_sub") != sub or grant.get("role") != "owner"):
        raise _Error(403, "group recovery access denied")
    fields = ("group_id", "owner_sub", "name", "description", "revision",
              "llm_enabled", "model", "status", "created_at", "updated_at")
    result = {field: meta[field] for field in fields if field in meta}
    result.update(kind="group", server_id=meta["group_id"],
                  role=grant.get("role", "viewer"), sources=meta.get("sources", []))
    active = (not recovery and meta.get("active_revision") == meta.get("revision")
              and meta.get("status") in ("READY", "PARTIAL") and bool(meta.get("active_version")))
    if current_versions is not None:
        active = (active and bool(current_versions) and all(current_versions.values())
                  and current_versions == meta.get("active_source_versions"))
    if current_descriptions is not None and "active_source_descriptions" in meta:
        active = active and current_descriptions == meta["active_source_descriptions"]
    result["data_ready"] = active
    result["stale"] = not active
    if recovery:
        result["access_recovery"] = True
        result["sources"] = [{"source_id": source["source_id"]}
                             for source in meta.get("sources", [])]
        return _json(result)
    for field in ("active_revision", "active_version", "active_source_versions", "active_source_descriptions",
                  "build_id", "build_started_at"):
        if field in meta:
            result[field] = meta[field]
    # Build failures can contain SDK URLs, credentials or untrusted source
    # text. Expose a stable message instead of raw stored diagnostics.
    if meta.get("last_error"):
        result["last_error"] = "Group build failed; review the build or rebuild the group."
        if meta["last_error"] in _SAFE_ERRORS:
            result["last_error_code"] = meta["last_error"]
    if active and meta.get("status") == "PARTIAL":
        result["partial_reasons"] = sorted({r for r in meta.get("partial_reasons", [])
                                             if isinstance(r, str) and r in _SAFE_ERRORS})
    usage = meta.get("usage")
    if isinstance(usage, dict):
        result["usage"] = {key: _json(value) for key, value in usage.items()
                           if key in _SAFE_USAGE and isinstance(value, (int, float, Decimal))
                           and not isinstance(value, bool)}
    if mcp_base:
        result["mcp_url"] = f"{mcp_base.rstrip('/')}/mcp/{meta['group_id']}"
    return _json(result)


def list_for_user(ident, *, ddb, platform_table, registry_table, mcp_base="", **_unused):
    """List accessible metadata (including stale groups); raise on service errors."""
    sub = ident["sub"]
    _active(ddb, platform_table, sub)
    result = []
    for discovered in _query(ddb, platform_table, f"USER#{sub}", "GROUP#"):
        gid = discovered.get("sk", "")[6:]
        if not _GROUP_ID.fullmatch(gid):
            continue
        try:
            meta, recovering = _access(ddb, platform_table, registry_table, sub, gid,
                                       recovery=True)
        except GroupError as exc:
            if exc.status in (403, 404):
                continue
            raise
        grant = get_item(ddb, platform_table, _grant_key(sub, gid))
        if not grant:
            continue
        if recovering:
            result.append(_view(meta, sub, grant, mcp_base, recovery=True))
            continue
        try:
            rows, _ = _source_access(ddb, platform_table, registry_table, sub, meta["sources"])
        except (GroupError, _Error) as exc:
            if exc.status in (403, 404):
                if meta.get("owner_sub") == sub and grant.get("role") == "owner":
                    result.append(_view(meta, sub, grant, mcp_base, recovery=True))
                continue
            raise
        current = {sid: row.get("active_source_version", "") for sid, row in rows.items()}
        descriptions = {sid: row.get("description_version", 0) for sid, row in rows.items()}
        result.append(_view(meta, sub, grant, mcp_base, current_versions=current,
                            current_descriptions=descriptions))
    return sorted(result, key=lambda group: (group.get("name", "").casefold(), group["group_id"]))


def _create(body, sub, ctx):
    ddb, table, registry_table = ctx["ddb"], ctx["platform_table"], ctx["registry_table"]
    token, config = _token(body), _config(body)
    gid = "grp_" + _digest("create", sub, token)[:32]
    request_hash = _digest(config)
    existing = get_item(ddb, table, _key(gid))
    if existing:
        if existing.get("owner_sub") != sub or existing.get("create_request_hash") != request_hash:
            raise _Error(409, "idempotency_key was already used for a different request")
        meta, _ = _access(ddb, table, registry_table, sub, gid)
        grant = get_item(ddb, table, _grant_key(sub, gid))
        return 200, {"group": _view(meta, sub, grant, ctx["mcp_base"]), "replayed": True}
    _limit(ddb, table, sub)
    _, checks = _source_access(ddb, table, registry_table, sub, config["sources"])
    now = int(time.time())
    meta = dict(_key(gid), **config, group_id=gid, owner_sub=sub, revision=1,
                gsi1pk="GROUPS", gsi1sk=gid,
                status="DRAFT", active_revision=0, active_version="",
                active_source_versions={}, created_at=now, updated_at=now,
                create_request_hash=request_hash)
    grant = dict(_grant_key(sub, gid), role="owner", created_at=now,
                 gsi1pk=f"GROUP#{gid}", gsi1sk=f"USER#{sub}")
    member = dict(_key(gid, f"MEMBER#{sub}"), sub=sub, role="owner")
    operations = [_put(table, meta, "attribute_not_exists(pk)"),
                  _put(table, grant, "attribute_not_exists(pk)"),
                  _put(table, member, "attribute_not_exists(pk)"),
                  _active(ddb, table, sub), *checks]
    try:
        _transaction(ddb, operations)
    except Exception as exc:
        if not _conflict(exc):
            raise
        # A concurrently committed request with this token is a replay, not
        # a partial creation or a reason to mint a second group.
        committed = get_item(ddb, table, _key(gid))
        if committed and committed.get("create_request_hash") == request_hash:
            return _create(body, sub, ctx)
        raise _Error(409, "group or source access changed; retry the request") from None
    return 201, {"group": _view(meta, sub, grant, ctx["mcp_base"])}


def _next_fields(meta):
    return {"revision": int(meta["revision"]) + 1,
            "status": "STALE" if meta.get("active_version") else "DRAFT",
            "updated_at": int(time.time()), "last_error": ""}


def _expect(meta, body):
    if _revision(body) != meta["revision"]:
        raise _Error(409, "group revision changed; reload the group and retry")


def _edit(body, sub, gid, ctx, *, delete=False):
    ddb, table, registry_table = ctx["ddb"], ctx["platform_table"], ctx["registry_table"]
    meta, recovering = _access(ddb, table, registry_table, sub, gid, write=True, recovery=True)
    owner_recovery = recovering
    _expect(meta, body)
    if delete and meta.get("owner_sub") != sub:
        raise _Error(403, "only the group owner can delete the group")
    authority = _authority(ddb, table, sub, gid, owner=delete or recovering)
    checks = []
    fields = _next_fields(meta)
    if delete:
        if set(body) - {"expected_revision"}:
            raise _Error(400, "unsupported deletion field")
        fields["status"] = "DELETED"
    else:
        config = _config(body, meta)
        if recovering:
            previous_ids = {source["source_id"] for source in meta.get("sources", [])}
            if any(source["source_id"] not in previous_ids for source in config["sources"]):
                raise _Error(403, "restore source access before adding sources to the group")
            # Keeping a revoked source permits configuration repair, but not
            # artifact access; removing all revoked sources restores normal access.
            try:
                _, checks = _source_access(ddb, table, registry_table, sub, config["sources"])
                recovering = False
            except (GroupError, _Error) as exc:
                if exc.status != 403:
                    raise
        else:
            _, checks = _source_access(ddb, table, registry_table, sub, config["sources"])
        fields.update(config)
    _transaction(ddb, [
        _meta_update(table, gid, meta, fields, owner=sub if delete or owner_recovery else None),
        authority, _active(ddb, table, sub), *checks,
    ])
    if delete:
        # Tombstone is authoritative and fences all workers. Existing grants
        # no longer authorize access and do not count toward the active cap.
        return 200, {"group_id": gid, "deleted": True, "revision": fields["revision"]}
    updated = get_item(ddb, table, _key(gid))
    grant = get_item(ddb, table, _grant_key(sub, gid))
    return 200, {"group": _view(updated, sub, grant, ctx["mcp_base"], recovery=recovering)}


def _members(body, sub, gid, method, target, ctx):
    ddb, table, registry_table = ctx["ddb"], ctx["platform_table"], ctx["registry_table"]
    meta, _ = _access(ddb, table, registry_table, sub, gid, write=True)
    if meta.get("owner_sub") != sub:
        raise _Error(403, "only the group owner can manage members")
    authority = _authority(ddb, table, sub, gid, owner=True)
    if method == "GET":
        members = []
        for pointer in _query(ddb, table, f"GROUP#{gid}", "MEMBER#"):
            member_sub = pointer.get("sub") or pointer["sk"][7:]
            grant = get_item(ddb, table, _grant_key(member_sub, gid))
            if not grant or grant.get("role") not in ("owner", "editor", "viewer"):
                continue
            # Return only explicitly selected member fields, never Cognito
            # users, attributes, full grant rows or other users' source lists.
            member = {"sub": member_sub, "role": grant["role"], "you": member_sub == sub}
            if grant.get("invited_email"):
                member["email"] = grant["invited_email"]
            members.append(member)
        return 200, {"group_id": gid, "revision": int(meta["revision"]), "members": members}
    _expect(meta, body)
    actor_rows, actor_checks = _source_access(ddb, table, registry_table, sub, meta["sources"])
    del actor_rows
    operations = [authority, _active(ddb, table, sub), *actor_checks]
    if method == "POST":
        if set(body) - {"expected_revision", "email", "role"}:
            raise _Error(400, "unsupported member field")
        email = _text(body.get("email", ""), "email", 254, required=True).lower()
        role = body.get("role", "viewer")
        if not _EMAIL.fullmatch(email) or role not in ("viewer", "editor"):
            raise _Error(400, "valid email and role viewer or editor are required")
        response = ctx["cognito"].list_users(
            UserPoolId=ctx["user_pool_id"], Filter=f'email = "{email}"', Limit=2,
        )
        users = response.get("Users", [])
        if len(users) != 1 or response.get("PaginationToken"):
            raise _Error(404, "email must resolve to exactly one active platform user")
        user = users[0]
        attributes = {attribute["Name"]: attribute["Value"]
                      for attribute in user.get("Attributes", [])}
        target = attributes.get("sub", "")
        if (not _SUB.fullmatch(target) or user.get("Enabled") is not True
                or attributes.get("email", "").lower() != email
                or attributes.get("email_verified") != "true"):
            raise _Error(404, "email must resolve to exactly one active platform user")
        if target == meta["owner_sub"]:
            raise _Error(400, "the owner role cannot be changed")
        operations.append(_active(ddb, table, target))
        try:
            _, target_checks = _source_access(ddb, table, registry_table, target, meta["sources"])
        except (GroupError, _Error) as exc:
            if exc.status == 403:
                raise _Error(403, "member must already have access to every source") from None
            raise
        operations.extend(target_checks)
        existing = get_item(ddb, table, _grant_key(target, gid))
        if not existing:
            _limit(ddb, table, target)
        grant = dict(_grant_key(target, gid), role=role, invited_email=email,
                     invited_by_sub=sub, created_at=(existing or {}).get("created_at", int(time.time())),
                     gsi1pk=f"GROUP#{gid}", gsi1sk=f"USER#{target}")
        operations.extend([
            _put(table, grant),
            _put(table, dict(_key(gid, f"MEMBER#{target}"), sub=target, role=role)),
        ])
        result = {"sub": target, "email": email, "role": role}
    else:
        if set(body) - {"expected_revision"} or not _SUB.fullmatch(target):
            raise _Error(400, "valid member sub and expected_revision are required")
        if target == meta["owner_sub"]:
            raise _Error(400, "the group owner cannot be removed")
        if not get_item(ddb, table, _grant_key(target, gid)):
            raise _Error(404, "group member not found")
        operations.extend([
            {"Delete": {"TableName": table, "Key": _typed(_grant_key(target, gid))}},
            {"Delete": {"TableName": table, "Key": _typed(_key(gid, f"MEMBER#{target}"))}},
        ])
        result = {"sub": target, "removed": True}
    fields = _next_fields(meta)
    operations.insert(0, _meta_update(table, gid, meta, fields, owner=sub))
    _transaction(ddb, operations)
    return (201 if method == "POST" else 200), {
        "group_id": gid, "revision": fields["revision"], "member": result,
    }


def _build_response(ledger, *, replayed=False):
    result = {field: _json(ledger[field]) for field in
              ("group_id", "build_id", "revision", "status") if field in ledger}
    result["replayed"] = replayed
    if ledger.get("dispatch_failed"):
        result["error"] = "Build dispatch could not be confirmed; retry with a new key after the lease expires."
    elif ledger.get("last_error"):
        result["error"] = "Group build failed; correct the source or settings and rebuild."
        if ledger["last_error"] in _SAFE_ERRORS:
            result["error_code"] = ledger["last_error"]
    return (503 if ledger.get("status") == "FAILED" and ledger.get("dispatch_failed") else 202), result


def _dispatch_failed(ctx, meta, ledger):
    """Keep the reservation on ambiguous invoke failures; never race a worker."""
    ddb, table = ctx["ddb"], ctx["platform_table"]
    message = "Build dispatch could not be confirmed."
    ddb.update_item(
        TableName=table, Key=_typed(_key(meta["group_id"], "BUILD#" + ledger["build_id"])),
        UpdateExpression="SET #s = :failed, dispatch_failed = :yes, last_error = :err",
        ConditionExpression="#s = :queued",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues=_typed({":failed": "FAILED", ":queued": "QUEUED",
                                           ":yes": True, ":err": message}),
    )
    # This does not modify active pointers. A worker may already be running
    # after a network timeout; only change the still-matching BUILDING row.
    try:
        ddb.update_item(
            TableName=table, Key=_typed(_key(meta["group_id"])),
            UpdateExpression="SET #s = :failed, last_error = :err",
            ConditionExpression="build_id = :bid AND revision = :rev AND #s = :building",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues=_typed({":bid": ledger["build_id"], ":rev": ledger["revision"],
                                               ":building": "BUILDING", ":failed": "FAILED", ":err": message}),
        )
    except Exception as exc:
        if not _conflict(exc):
            raise


def _rebuild(body, sub, gid, ctx):
    ddb, table, registry_table = ctx["ddb"], ctx["platform_table"], ctx["registry_table"]
    if set(body) - {"expected_revision", "idempotency_key"}:
        raise _Error(400, "unsupported rebuild field")
    token, expected = _token(body), _revision(body)
    meta, _ = _access(ddb, table, registry_table, sub, gid, write=True)
    build_id = "bld_" + _digest("rebuild", sub, gid, token)[:32]
    ledger_key = _key(gid, "BUILD#" + build_id)
    existing = get_item(ddb, table, ledger_key)
    if existing:
        if existing.get("revision") != expected or existing.get("requested_by") != sub:
            raise _Error(409, "idempotency_key was already used for a different request")
        return _build_response(existing, replayed=True)
    _expect(meta, body)
    if not ctx["worker_fn"]:
        raise _Error(503, "group build worker is unavailable")
    if not meta.get("sources"):
        raise _Error(400, "add at least one source before rebuilding")
    now = int(time.time())
    started = meta.get("build_started_at", 0)
    if started and now - int(started) < BUILD_LEASE_SECONDS:
        raise _Error(409, "a group build is already reserved; retry after its 900-second lease")
    rows, checks = _source_access(ddb, table, registry_table, sub, meta["sources"])
    versions, description_versions = {}, {}
    for sid, row in rows.items():
        version = row.get("active_source_version")
        if not isinstance(version, str) or not _VERSION.fullmatch(version) or ".." in version:
            raise _Error(409, "source has no immutable snapshot version; rebuild the source first")
        prefix = f"source-versions/{sid}/{version}"
        for suffix in ("manifest.json", "graph.json", "src.tar.gz"):
            try:
                ctx["s3"].head_object(Bucket=ctx["bucket"], Key=f"{prefix}/{suffix}")
            except Exception as exc:
                if _code(exc) in ("404", "NoSuchKey", "NotFound"):
                    raise _Error(409, "source snapshot version is missing; rebuild the source first") from None
                raise _Error(503, "source snapshot version could not be verified") from None
        versions[sid] = version
        description_versions[sid] = row.get("description_version", 0)
    ledger = dict(ledger_key, group_id=gid, build_id=build_id, revision=expected,
                  requested_by=sub, status="QUEUED", created_at=now,
                  source_versions=versions, source_description_versions=description_versions,
                  **{field: meta.get(field) for field in _CONFIG_FIELDS})
    fields = {"status": "BUILDING", "build_id": build_id, "build_started_at": now,
              "last_error": "", "updated_at": now}
    try:
        _transaction(ddb, [
            _meta_update(table, gid, meta, fields, lease_before=now - BUILD_LEASE_SECONDS,
                         remove=("worker_token", "worker_lease_expires_at")),
            _put(table, ledger, "attribute_not_exists(pk)"),
            _authority(ddb, table, sub, gid), _active(ddb, table, sub), *checks,
        ])
    except Exception as exc:
        if not _conflict(exc):
            raise
        existing = get_item(ddb, table, ledger_key)
        if existing:
            if existing.get("revision") != expected or existing.get("requested_by") != sub:
                raise _Error(409, "idempotency_key was already used for a different request") from None
            return _build_response(existing, replayed=True)
        raise _Error(409, "group, build, or source changed; reload and retry") from None
    payload = {"group_id": gid, "build_id": build_id, "revision": expected, "requested_by": sub}
    try:
        response = ctx["lambda_client"].invoke(
            FunctionName=ctx["worker_fn"], InvocationType="Event",
            Payload=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        )
        if response.get("StatusCode") != 202:
            raise _Error(503, "group build dispatch was not accepted")
    except Exception:
        try:
            _dispatch_failed(ctx, meta, ledger)
        except Exception:
            # The QUEUED ledger remains the durable no-reinvoke guard even
            # when recording the diagnostic itself fails.
            pass
        return 503, {"group_id": gid, "build_id": build_id, "revision": expected,
                     "error": "Build dispatch could not be confirmed; retry with a new key after the lease expires."}
    try:
        ddb.update_item(
            TableName=table, Key=_typed(ledger_key),
            UpdateExpression="SET dispatched = :yes, dispatched_at = :now",
            ConditionExpression="attribute_exists(pk) AND build_id = :bid",
            ExpressionAttributeValues=_typed({":yes": True, ":now": int(time.time()), ":bid": build_id}),
        )
    except Exception:
        # Invocation was accepted. Failure of this informational marker must
        # not mark the running build failed or cause another invocation.
        pass
    return _build_response(ledger)


def handle(event, ident, method, path, *, platform, registry, ddb, s3,
           lambda_client, worker_fn, platform_table, registry_table, bucket,
           mcp_base, cognito, user_pool_id):
    """Dispatch the management routes only, always returning a safe JSON body."""
    ctx = dict(ddb=ddb, s3=s3, lambda_client=lambda_client, worker_fn=worker_fn,
               platform_table=platform_table, registry_table=registry_table,
               bucket=bucket, mcp_base=mcp_base, cognito=cognito, user_pool_id=user_pool_id)
    try:
        sub = ident.get("sub")
        if not isinstance(sub, str) or not _SUB.fullmatch(sub):
            raise _Error(401, "authenticated identity is required")
        _active(ddb, platform_table, sub)
        segments = path.rstrip("/").split("/")
        method = method.upper()
        if segments == ["", "groups"]:
            if method == "GET":
                return 200, {"groups": list_for_user(ident, **ctx)}
            if method == "POST":
                return _create(_body(event), sub, ctx)
        elif len(segments) >= 3 and segments[:2] == ["", "groups"]:
            gid = segments[2]
            if not _GROUP_ID.fullmatch(gid):
                raise _Error(404, "group not found")
            if len(segments) == 3:
                if method == "GET":
                    meta, recovering = _access(ddb, platform_table, registry_table, sub, gid,
                                              recovery=True)
                    grant = get_item(ddb, platform_table, _grant_key(sub, gid))
                    if not grant:
                        raise _Error(404, "group not found")
                    if recovering:
                        return 200, {"group": _view(meta, sub, grant, mcp_base, recovery=True)}
                    try:
                        rows, _ = _source_access(ddb, platform_table, registry_table, sub, meta["sources"])
                    except (GroupError, _Error) as exc:
                        if (exc.status in (403, 404) and meta.get("owner_sub") == sub
                                and grant.get("role") == "owner"):
                            return 200, {"group": _view(meta, sub, grant, mcp_base, recovery=True)}
                        raise
                    current = {sid: row.get("active_source_version", "") for sid, row in rows.items()}
                    descriptions = {sid: row.get("description_version", 0) for sid, row in rows.items()}
                    return 200, {"group": _view(meta, sub, grant, mcp_base, current_versions=current,
                                              current_descriptions=descriptions)}
                if method in ("POST", "DELETE"):
                    return _edit(_body(event), sub, gid, ctx, delete=method == "DELETE")
            elif segments[3:] == ["rebuild"] and method == "POST":
                return _rebuild(_body(event), sub, gid, ctx)
            elif (segments[3:] == ["members"] and method in ("GET", "POST")):
                return _members(_body(event) if method == "POST" else {}, sub, gid, method, "", ctx)
            elif len(segments) == 5 and segments[3] == "members" and method == "DELETE":
                return _members(_body(event), sub, gid, method, segments[4], ctx)
        return 404, {"error": "group management route not found"}
    except _Error as exc:
        return exc.status, {"error": str(exc)}
    except GroupError as exc:
        # Shared module messages may include source identifiers or provider
        # details. Status is preserved, text is intentionally not echoed.
        status = exc.status if exc.status in (400, 401, 403, 404, 409, 503) else 503
        message = {400: "invalid group request", 401: "authentication required",
                   403: "group or source access denied", 404: "group not found",
                   409: "group state changed; reload and retry"}.get(status, "group access is unavailable")
        return status, {"error": message}
    except Exception as exc:
        if _conflict(exc):
            return 409, {"error": "group or access changed; reload and retry"}
        return 503, {"error": "group management is temporarily unavailable"}
