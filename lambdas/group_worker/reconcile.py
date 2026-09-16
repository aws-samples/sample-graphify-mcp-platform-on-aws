"""One group-index page per tick; one automatic dispatch per input signature.

The cursor is fenced for Lambda's maximum lifetime. Partial pages checkpoint
the last processed key, including rejected/malformed groups. Build claims and
their worker-compatible ledgers are atomic with current source/ACL guards.
An ambiguous claim or invoke is never retried; expired reservations become
terminal failures and the ledger remains the durable no-redispatch marker.
"""

from decimal import Decimal
import hashlib
import json
import os
import re
import time
import uuid

from boto3.dynamodb.types import TypeSerializer, TypeDeserializer
from group_access import GroupError, get_item, is_group_id, require_access, source_access

PAGE_SIZE = 100
_CURSOR_FIELDS = ("pk", "sk", "gsi1pk", "gsi1sk")
LEASE_SECONDS = 960
MIN_REMAINING_MS = 15000
_STATE_KEY = {"pk": "SYSTEM#GROUP_RECONCILE", "sk": "CURSOR"}
_CONFIG_FIELDS = ("name", "sources", "description", "llm_enabled", "model")
_META_FIELDS = (
    "group_id", "owner_sub", "revision", "status", "sources", "description", "name",
    "llm_enabled", "model", "model_id", "llm_model", "deleted_at",
    "membership_version", "context_version", "acl_version", "active_version",
    "active_revision", "active_source_versions", "active_source_descriptions",
    "build_id", "build_started_at", "worker_token", "worker_lease_expires_at",
    "last_auto_signature",
)
_SOURCE_FIELDS = (
    "enabled", "graph_scope", "active_source_version", "description_version",
    "description", "source_epoch", "acl_epoch", "owner_sub", "deleted_at",
)
_INACTIVE = {"DELETED", "REVOKED", "DISABLED"}
_ser, _de = TypeSerializer(), TypeDeserializer()


class _Deferred(Exception):
    pass


def _wire(data):
    return {k: _ser.serialize(v) for k, v in data.items()}


def _number(value):
    if (
        isinstance(value, bool) or not isinstance(value, (int, Decimal))
        or (isinstance(value, Decimal) and not value.is_finite())
        or value < 0 or value != int(value)
    ):
        raise ValueError("Invalid nonnegative integer")
    return int(value)


def _budget(remaining):
    if remaining() < MIN_REMAINING_MS:
        raise _Deferred()


def _condition(table, key, row, fields=None):
    """Snapshot comparison, including absence of fields such as worker_token."""
    request = {
        "TableName": table, "Key": _wire(key),
        "ConditionExpression": "attribute_not_exists(#pk)" if row is None else "attribute_exists(#pk)",
        "ExpressionAttributeNames": {"#pk": next(iter(key))},
    }
    if row is None:
        return request
    values = {}
    for index, field in enumerate(fields if fields is not None else sorted(set(row) - set(key))):
        name, value = f"#c{index}", f":c{index}"
        request["ExpressionAttributeNames"][name] = field
        if field in row:
            request["ConditionExpression"] += f" AND {name} = {value}"
            values[value] = row[field]
        else:
            request["ConditionExpression"] += f" AND attribute_not_exists({name})"
    if values:
        request["ExpressionAttributeValues"] = _wire(values)
    return request


def _assign(request, fields, remove=()):
    names = request["ExpressionAttributeNames"]
    values = request.setdefault("ExpressionAttributeValues", {})
    terms = []
    for index, (field, value) in enumerate(fields.items()):
        alias, token = f"#u{index}", f":u{index}"
        names[alias], values[token] = field, _ser.serialize(value)
        terms.append(f"{alias} = {token}")
    request["UpdateExpression"] = "SET " + ", ".join(terms)
    if remove:
        aliases = []
        for index, field in enumerate(remove):
            alias = f"#r{index}"
            names[alias] = field
            aliases.append(alias)
        request["UpdateExpression"] += " REMOVE " + ", ".join(aliases)
    return request


def _key(gid):
    return {"pk": f"GROUP#{gid}", "sk": "META"}


def _leased(meta, now):
    started = _number(meta.get("build_started_at", 0))
    expires = _number(meta.get("worker_lease_expires_at", 0))
    return (started > 0 and now <= started + LEASE_SECONDS) or (expires > 0 and now <= expires)


def _expire(ddb, platform, meta, now):
    """Fence a timed-out worker and close its ledger without starting new work."""
    gid, bid = meta["group_id"], meta.get("build_id")
    if not isinstance(bid, str) or not bid:
        raise ValueError("Missing build reservation")
    ledger_key = {"pk": f"GROUP#{gid}", "sk": f"BUILD#{bid}"}
    ledger = get_item(ddb, platform, ledger_key)
    if not meta.get("build_started_at") and not meta.get("worker_lease_expires_at"):
        # A legacy reservation can omit the META clock. Use a persisted ledger
        # clock when available; absence of a clock is not proof of a timeout.
        created = _number((ledger or {}).get("created_at", 0))
        if not created or now <= created + LEASE_SECONDS:
            return False
    request = _assign(_condition(platform, _key(gid), meta, _META_FIELDS), {
        "status": "FAILED", "last_error": "BUILD_TIMEOUT", "build_started_at": 0, "updated_at": now,
    }, ("worker_token", "worker_lease_expires_at"))
    operations = [{"Update": request}]
    if ledger and ledger.get("status") in ("QUEUED", "BUILDING"):
        operations.append({"Update": _assign(_condition(platform, ledger_key, ledger), {
            "status": "FAILED", "last_error": "BUILD_TIMEOUT", "ended_at": now,
            "usage": ledger.get("usage", {}), "stats": ledger.get("stats", {}),
        })})
    else:
        # A ledger appearing/completing between the read and the timeout also
        # invalidates this decision. Never rewrite an already terminal ledger.
        operations.append({"ConditionCheck": _condition(platform, ledger_key, ledger)})
    ddb.transact_write_items(TransactItems=operations)
    return True


def _inputs_and_guards(ddb, platform, registry, meta, sub, remaining):
    gid = meta["group_id"]
    deleted_key = {"pk": f"USER#{sub}", "sk": "DELETED"}
    if get_item(ddb, platform, deleted_key):
        raise GroupError(403, "Account unavailable")
    guards = [{"ConditionCheck": _condition(platform, deleted_key, None)}]
    grant_key = {"pk": f"USER#{sub}", "sk": f"GROUP#{gid}"}
    grant = get_item(ddb, platform, grant_key)
    if not grant or grant.get("role") not in ("owner", "editor") or str(grant.get("status", "")).upper() in _INACTIVE:
        raise GroupError(403, "Group write permission required")
    guards.append({"ConditionCheck": _condition(platform, grant_key, grant, ("role", "status"))})
    versions, descriptions = {}, {}
    for member in meta["sources"]:
        _budget(remaining)
        sid = member["source_id"]
        row = source_access(ddb, platform, registry, sub, sid)
        version = row.get("active_source_version")
        if (
            not isinstance(version, str) or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,511}", version)
            or ".." in version or version == "latest" or row.get("deleted_at")
        ):
            raise GroupError(409, "Source has no usable immutable version")
        versions[sid] = version
        descriptions[sid] = _number(row.get("description_version", 0))
        guards.append({"ConditionCheck": _condition(
            registry, {"repo_id": sid}, row, _SOURCE_FIELDS,
        )})
        if row.get("graph_scope") != "public":
            source_key = {"pk": f"USER#{sub}", "sk": f"REPO#{sid}"}
            source_grant = get_item(ddb, platform, source_key)
            if not source_grant or str(source_grant.get("status", "")).upper() in _INACTIVE:
                raise GroupError(403, "Source permission required")
            guards.append({"ConditionCheck": _condition(
                platform, source_key, source_grant, ("status",),
            )})
    return versions, descriptions, guards


def _dispatch_marker(ddb, platform, ledger, now, accepted):
    # Informational only: an accepted worker can run even when the invoke
    # response was lost. Keep QUEUED until the worker or timeout closes it.
    key = {"pk": ledger["pk"], "sk": ledger["sk"]}
    request = _condition(platform, key, ledger, ("build_id", "revision", "status"))
    fields = {"dispatched": True, "dispatched_at": now} if accepted else {
        "dispatch_uncertain": True, "last_error": "DISPATCH_UNCONFIRMED",
    }
    try:
        ddb.update_item(**_assign(request, fields))
    except Exception:
        pass


def _process(ddb, invoke, platform, registry, worker, gid, now, remaining):
    # The eventually consistent index is discovery only. Lease and ACL
    # decisions use fresh, strongly consistent base-table reads.
    meta = get_item(ddb, platform, _key(gid))
    if not meta or meta.get("group_id") != gid or meta.get("status") == "DELETED" or meta.get("deleted_at"):
        return "skipped"
    if _leased(meta, now):
        return "skipped"
    if meta.get("status") == "BUILDING":
        _budget(remaining)
        return "timed_out" if _expire(ddb, platform, meta, now) else "skipped"
    if not meta.get("active_version") or meta.get("status") not in ("READY", "PARTIAL", "STALE", "FAILED"):
        return "skipped"
    sub = meta.get("owner_sub")
    if not isinstance(sub, str) or not 1 <= len(sub) <= 256 or any(ord(c) < 33 or c in "/\\#" for c in sub):
        return "skipped"
    meta = require_access(ddb, platform, registry, sub, gid, write=True)
    if (
        meta.get("group_id") != gid or meta.get("owner_sub") != sub or meta.get("deleted_at")
        or _leased(meta, now) or not meta.get("active_version")
        or meta.get("status") not in ("READY", "PARTIAL", "STALE", "FAILED") or not meta.get("sources")
    ):
        return "skipped"
    revision = _number(meta["revision"])
    if revision < 1:
        return "skipped"
    versions, descriptions, guards = _inputs_and_guards(
        ddb, platform, registry, meta, sub, remaining,
    )
    # Status alone is not an input change. A failed manual build of the same
    # active inputs must not silently turn into a charged automatic retry.
    if (
        meta.get("active_source_versions") == versions
        and meta.get("active_source_descriptions", descriptions) == descriptions
        and meta.get("active_revision") == revision
    ):
        return "skipped"
    signature = hashlib.sha256(json.dumps({
        "revision": revision, "versions": versions, "descriptions": descriptions,
    }, sort_keys=True).encode("utf-8")).hexdigest()
    if meta.get("last_auto_signature") == signature:
        return "skipped"
    bid = "auto_" + signature[:32]
    ledger_key = {"pk": f"GROUP#{gid}", "sk": f"BUILD#{bid}"}
    if get_item(ddb, platform, ledger_key):
        return "skipped"
    ledger = {
        **ledger_key, "group_id": gid, "build_id": bid, "revision": revision,
        "requested_by": sub, "status": "QUEUED", "created_at": now,
        "source_versions": versions, "source_description_versions": descriptions,
        **{field: meta[field] for field in _CONFIG_FIELDS if field in meta},
    }
    payload = json.dumps({
        "group_id": gid, "build_id": bid, "revision": revision, "requested_by": sub,
    }, separators=(",", ":")).encode("utf-8")
    _budget(remaining)
    ddb.transact_write_items(TransactItems=[
        {"Update": _assign(_condition(platform, _key(gid), meta, _META_FIELDS), {
            "status": "BUILDING", "build_id": bid, "build_started_at": now,
            "last_auto_signature": signature, "last_error": "", "updated_at": now,
        }, ("worker_token", "worker_lease_expires_at"))},
        {"Put": {"TableName": platform, "Item": _wire(ledger),
                 "ConditionExpression": "attribute_not_exists(pk)"}},
        *guards,
    ])
    try:
        response = invoke.invoke(FunctionName=worker, InvocationType="Event", Payload=payload)
        accepted = response.get("StatusCode") == 202
    except Exception:
        accepted = False
    _dispatch_marker(ddb, platform, ledger, now, accepted)
    return "scheduled" if accepted else "failed"


def _cursor(value):
    if (
        not isinstance(value, dict) or set(value) != set(_CURSOR_FIELDS)
        or value.get("gsi1pk") != "GROUPS"
        or any(not isinstance(v, str) or not v or len(v.encode("utf-8")) > 2048 for v in value.values())
    ):
        return {}
    return value


def run(ddb, invoke, platform, registry, worker, *, now=None, remaining=lambda: 60000):
    now = _number(int(time.time()) if now is None else now)
    result = {"scheduled": 0, "skipped": 0, "failed": 0, "timed_out": 0, "processed": 0}
    if not worker or remaining() < MIN_REMAINING_MS:
        return result
    # Overlapping EventBridge deliveries must not rewind each other's cursor.
    state = get_item(ddb, platform, _STATE_KEY)
    try:
        run_expiry = _number((state or {}).get("lease_expires_at", 0))
    except ValueError:
        # Corrupt bookkeeping must not strand the entire table. The CAS below
        # still guards the exact old value while replacing it.
        run_expiry = 0
    if run_expiry > now:
        return {**result, "busy": True}
    token = uuid.uuid4().hex
    try:
        ddb.update_item(**_assign(_condition(
            platform, _STATE_KEY, state, ("cursor", "run_token", "lease_expires_at"),
        ), {"run_token": token, "lease_expires_at": now + LEASE_SECONDS}))
    except Exception:
        return {**result, "failed": 1}
    next_key = _cursor((state or {}).get("cursor"))
    try:
        params = {
            "TableName": platform, "IndexName": "entity-index", "Limit": PAGE_SIZE,
            "KeyConditionExpression": "#gpk = :groups",
            "ProjectionExpression": "#pk, #sk, #gpk, #gsk",
            "ExpressionAttributeNames": {
                "#pk": "pk", "#sk": "sk", "#gpk": "gsi1pk", "#gsk": "gsi1sk",
            },
            "ExpressionAttributeValues": _wire({":groups": "GROUPS"}),
        }
        if next_key:
            params["ExclusiveStartKey"] = _wire(next_key)
        page = ddb.query(**params)
        for typed in page.get("Items", []):
            if remaining() < MIN_REMAINING_MS:
                break
            item = {k: _de.deserialize(v) for k, v in typed.items()}
            key = _cursor({field: item.get(field) for field in _CURSOR_FIELDS})
            if not key:
                result["failed"] += 1
                continue
            gid = key["gsi1sk"]
            try:
                valid = is_group_id(gid) and key["pk"] == f"GROUP#{gid}" and key["sk"] == "META"
                outcome = _process(ddb, invoke, platform, registry, worker, gid, now, remaining) if valid else "skipped"
                result[outcome] += 1
            except _Deferred:
                break
            except GroupError:
                result["skipped"] += 1
            except Exception:
                # A poison row or ambiguous transaction does not starve the
                # rest of the table. A committed claim is never re-dispatched.
                result["failed"] += 1
            next_key = key
            result["processed"] += 1
        else:
            # Preserve all base/index keys, including on empty index pages.
            next_key = _cursor({k: _de.deserialize(v) for k, v in page.get("LastEvaluatedKey", {}).items()})
    except Exception:
        result["failed"] += 1
    finally:
        try:
            ddb.update_item(
                TableName=platform, Key=_wire(_STATE_KEY),
                ConditionExpression="run_token = :token",
                UpdateExpression="SET #cursor = :cursor, updated_at = :now REMOVE run_token, lease_expires_at",
                ExpressionAttributeNames={"#cursor": "cursor"},
                ExpressionAttributeValues=_wire({":token": token, ":cursor": next_key, ":now": now}),
            )
        except Exception:
            # The bounded run lease eventually releases even if this write
            # fails or a timed-out invocation has already been superseded.
            result["failed"] += 1
    return result


def reconcile(event, context):
    import boto3
    from botocore.config import Config
    config = Config(connect_timeout=3, read_timeout=10, retries={"total_max_attempts": 1})
    platform, registry = os.environ["PLATFORM_TABLE"], os.environ["REGISTRY_TABLE"]
    ddb = boto3.client("dynamodb", config=config)
    result = run(
        ddb, boto3.client("lambda", config=config), platform, registry,
        os.environ["AWS_LAMBDA_FUNCTION_NAME"],
        remaining=context.get_remaining_time_in_millis,
    )
    if context.get_remaining_time_in_millis() > 30000:
        try:
            try:
                from .garbage import sweep
            except ImportError:
                from garbage import sweep
            result["gc"] = sweep(
                ddb, boto3.client("s3", config=config),
                platform, registry, os.environ["GRAPH_BUCKET"],
            )
        except Exception as exc:
            # Reconciliation already completed. Keep its result and expose
            # only the error type, never storage paths or exception details.
            result["gc_error"] = type(exc).__name__
    return result
