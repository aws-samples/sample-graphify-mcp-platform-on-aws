"""Build immutable group artifacts and atomically advance their active pointer.

The asynchronous event is {group_id, build_id, revision, requested_by}. DynamoDB
is a low-level boto3 client with typed attribute values. All service
boundaries are injectable; importing this module never creates AWS clients.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import time
import uuid
from decimal import Decimal

from boto3.dynamodb.types import TypeSerializer

import group_access

try:
    from . import engine
except ImportError:  # Lambda deploys the directory as the module search root.
    import engine


MAX_SOURCES = 8
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
DEFAULT_MODEL = "global.anthropic.claude-sonnet-5"
WORKER_LEASE_SECONDS = 960  # Lambda's 900-second maximum plus a timeout buffer.
_SERIALIZER = TypeSerializer()
_IDENTITY = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,511}\Z")
_SHA256 = re.compile(r"[a-fA-F0-9]{64}\Z")
_SAFE_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,63}\Z")
_SOURCE_FIELDS = (
    "active_source_version", "source_epoch", "acl_epoch", "enabled",
    "graph_scope", "owner_sub", "deleted_at", "description", "description_version",
)
_GROUP_FIELDS = (
    "owner_sub", "membership_version", "context_version", "acl_version",
    "sources", "description", "llm_enabled", "model", "model_id",
    "llm_model", "deleted_at", "worker_token",
)


class BuildError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


class _Stale(Exception):
    pass


def _identity(value):
    return (
        isinstance(value, str) and _IDENTITY.fullmatch(value) is not None
        and value not in {".", "..", "latest"}
    )


def _event(event):
    if not isinstance(event, dict):
        raise BuildError("INVALID_EVENT")
    gid, bid, revision = (event.get(k) for k in ("group_id", "build_id", "revision"))
    identities = [event[k] for k in ("sub", "requested_by", "requester_sub") if k in event]
    sub = identities[0] if identities else None
    if (
        not isinstance(gid, str) or not re.fullmatch(r"grp_[0-9a-f]{32}", gid)
        or not _identity(bid) or type(revision) is not int or revision < 1
        or not isinstance(sub, str) or not 1 <= len(sub) <= 256
        or any(ord(c) < 33 or c in "/\\#" for c in sub)
        or any(identity != sub for identity in identities)
    ):
        raise BuildError("INVALID_EVENT")
    return gid, bid, revision, sub


def _key(gid):
    return {"pk": f"GROUP#{gid}", "sk": "META"}


def _claimed(meta, bid, revision, token=None):
    if (
        not meta or meta.get("status") != "BUILDING"
        or meta.get("build_id") != bid or meta.get("revision") != revision
        or (token is not None and meta.get("worker_token") != token)
    ):
        raise _Stale()


def _same_fields(left, right, fields):
    return all(
        (field in left) == (field in right) and left.get(field) == right.get(field)
        for field in fields
    )


def _checkpoint(ddb, platform, registry, gid, bid, revision, sub, token, original=None, rows=None):
    meta = group_access.get_item(ddb, platform, _key(gid))
    _claimed(meta, bid, revision, token)
    meta = group_access.require_access(
        ddb, platform, registry, sub, gid, write=True, check_versions=False,
    )
    _claimed(meta, bid, revision, token)
    if original is not None and not _same_fields(original, meta, _GROUP_FIELDS):
        raise BuildError("GROUP_CHANGED")
    for sid, row in (rows or {}).items():
        current = group_access.get_item(ddb, registry, {"repo_id": sid})
        if not current or not _same_fields(row, current, _SOURCE_FIELDS):
            raise BuildError("SOURCE_VERSION_CHANGED")
    return meta


def _members(meta):
    members = meta.get("sources")
    if not isinstance(members, list) or not 1 <= len(members) <= MAX_SOURCES:
        raise BuildError("INVALID_MEMBERSHIP")
    normalized, seen = [], set()
    for item in members:
        if not isinstance(item, dict):
            raise BuildError("INVALID_MEMBERSHIP")
        sid = item.get("source_id")
        if not _identity(sid) or sid in seen:
            raise BuildError("INVALID_MEMBERSHIP")
        seen.add(sid)
        normalized.append({**item, "source_id": sid})
    return sorted(normalized, key=lambda item: item["source_id"])


def _wire(request):
    def document(value):
        if isinstance(value, float):
            return Decimal(str(value))
        if isinstance(value, dict):
            return {k: document(v) for k, v in value.items()}
        if isinstance(value, list):
            return [document(v) for v in value]
        return value
    return {
        key: {k: _SERIALIZER.serialize(document(v)) for k, v in value.items()}
        if key in {"Key", "ExpressionAttributeValues"} else value
        for key, value in request.items()
    }


def _acquire(ddb, platform, gid, bid, revision, token):
    now = int(time.time())
    try:
        ddb.update_item(**_wire({
            "TableName": platform, "Key": _key(gid),
            "ConditionExpression": (
                "#status = :building AND build_id = :bid AND revision = :revision "
                "AND (attribute_not_exists(worker_token) OR worker_lease_expires_at < :now)"
            ),
            "UpdateExpression": "SET worker_token = :token, worker_lease_expires_at = :expiry",
            "ExpressionAttributeNames": {"#status": "status"},
            "ExpressionAttributeValues": {
                ":building": "BUILDING", ":bid": bid, ":revision": revision,
                ":token": token, ":now": now, ":expiry": now + WORKER_LEASE_SECONDS,
            },
        }))
        return True
    except Exception as exc:
        if _error_code(exc) == "ConditionalCheckFailedException":
            return False
        raise


def _error_code(exc):
    return getattr(exc, "response", {}).get("Error", {}).get("Code")


def _deadline(remaining_ms):
    if callable(remaining_ms) and remaining_ms() < 30_000:
        raise BuildError("DEADLINE_EXCEEDED")


def _read(s3, bucket, key, cap, missing="SOURCE_VERSION_MISSING", remaining_ms=None):
    _deadline(remaining_ms)
    try:
        response = s3.get_object(Bucket=bucket, Key=key)
    except Exception as exc:
        if _error_code(exc) in {"NoSuchKey", "NotFound", "404"}:
            raise BuildError(missing) from None
        raise
    body = response["Body"]
    try:
        length = response.get("ContentLength")
        if length is not None and (length < 0 or length > cap):
            raise BuildError("BYTE_LIMIT_EXCEEDED")
        output = io.BytesIO()
        while True:
            _deadline(remaining_ms)
            chunk = body.read(min(64 * 1024, cap + 1 - output.tell()))
            if not chunk:
                break
            output.write(chunk)
            if output.tell() > cap:
                raise BuildError("BYTE_LIMIT_EXCEEDED")
        data = output.getvalue()
        if length is not None and len(data) != length:
            raise BuildError("ARTIFACT_TRUNCATED")
        return data
    finally:
        if hasattr(body, "close"):
            body.close()


def _json(data):
    def reject_constant(_):
        raise ValueError()
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError()
            result[key] = value
        return result
    try:
        value = json.loads(
            data.decode("utf-8"), parse_constant=reject_constant, object_pairs_hook=unique_object,
        )
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (ValueError, RecursionError):
        raise BuildError("INVALID_ARTIFACT") from None


def _json_bytes(value):
    def decimal(obj):
        if isinstance(obj, Decimal):
            return int(obj) if obj == int(obj) else float(obj)
        raise TypeError()
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False, default=decimal,
        ).encode("utf-8")
    except (ValueError, TypeError, RecursionError):
        raise BuildError("INVALID_ARTIFACT") from None


def _path(path):
    return (
        isinstance(path, str) and 0 < len(path.encode("utf-8")) <= 4096
        and "\\" not in path and not any(ord(c) < 32 for c in path)
        and not any(part in {"", ".", ".."} for part in path.split("/"))
    )


def _contained(key, prefix):
    return _path(key) and key.startswith(prefix) and key != prefix + "manifest.json"


def _load_source(s3, bucket, member, row, graph_budget, text_budget, remaining_ms=None):
    sid, version = member["source_id"], row.get("active_source_version")
    if not _identity(version):
        raise BuildError("SOURCE_VERSION_MISSING")
    prefix = f"source-versions/{sid}/{version}/"
    manifest = _json(_read(
        s3, bucket, prefix + "manifest.json", MAX_MANIFEST_BYTES, remaining_ms=remaining_ms,
    ))
    if (
        type(manifest.get("schema_version")) is not int or manifest["schema_version"] != 1
        or manifest.get("version") != version or manifest.get("repo_id") != sid
        or not isinstance(manifest.get("build_id"), str) or not manifest["build_id"]
        or not isinstance(manifest.get("created_at"), str) or not manifest["created_at"]
        or not _contained(manifest.get("graph_key"), prefix)
        or not _contained(manifest.get("snapshot_key"), prefix)
        or manifest["graph_key"] == manifest["snapshot_key"]
        or any(
            not isinstance(manifest.get(field), str)
            or _SHA256.fullmatch(manifest[field]) is None
            for field in ("graph_sha256", "snapshot_sha256")
        )
    ):
        raise BuildError("INVALID_SOURCE_MANIFEST")
    graph_data = _read(s3, bucket, manifest["graph_key"], graph_budget, remaining_ms=remaining_ms)
    graph_size = len(graph_data)
    if hashlib.sha256(graph_data).hexdigest() != manifest["graph_sha256"].lower():
        raise BuildError("CHECKSUM_MISMATCH")
    graph = _json(graph_data)
    del graph_data
    snapshot = _read(
        s3, bucket, manifest["snapshot_key"], engine.MAX_SNAPSHOT_BYTES, remaining_ms=remaining_ms,
    )
    if hashlib.sha256(snapshot).hexdigest() != manifest["snapshot_sha256"].lower():
        raise BuildError("CHECKSUM_MISMATCH")
    _deadline(remaining_ms)
    files = engine.parse_snapshot(snapshot)
    _deadline(remaining_ms)
    del snapshot
    if not isinstance(files, dict):
        raise BuildError("INVALID_SNAPSHOT")
    text_size = 0
    for path, text in files.items():
        if not _path(path) or not isinstance(text, str):
            raise BuildError("INVALID_SNAPSHOT")
        size = len(text.encode("utf-8"))
        text_size += size
        if size > engine.MAX_FILE_BYTES or text_size > text_budget:
            raise BuildError("BYTE_LIMIT_EXCEEDED")
    role = member.get("role", "")
    description = member.get(
        "description_override", member.get("description", ""),
    )
    if not isinstance(role, str) or not isinstance(description, str):
        raise BuildError("INVALID_MEMBERSHIP")
    return {
        "source_id": sid, "version": version, "graph": graph, "files": files,
        "role": role, "description": description,
        "common_description": row.get("description", ""),
        "description_version": int(row.get("description_version", 0)),
    }, graph_size, text_size


def _previous(s3, bucket, gid, meta):
    version = meta.get("active_version")
    if not _identity(version):
        return None
    key = f"groups/{gid}/versions/{version}/manifest.json"
    if meta.get("active_manifest_key", key) != key:
        return None
    try:
        manifest = _json(_read(s3, bucket, key, MAX_MANIFEST_BYTES, "CACHE_MISSING"))
    except BuildError:
        return None  # A missing/invalid optimization never substitutes another version.
    if (
        manifest.get("group_id") != gid or manifest.get("version") != version
        or manifest.get("build_id") != version or not isinstance(manifest.get("pair_cache"), dict)
    ):
        return None
    return manifest


def _immutable_put(s3, bucket, key, data, content_type):
    try:
        s3.put_object(
            Bucket=bucket, Key=key, Body=data, ContentType=content_type, IfNoneMatch="*",
        )
    except Exception as exc:
        if _error_code(exc) not in {"PreconditionFailed", "412", "ConditionalRequestConflict"}:
            raise
        try:
            existing = _read(s3, bucket, key, len(data), "IMMUTABLE_CONFLICT")
        except BuildError:
            raise BuildError("IMMUTABLE_CONFLICT") from None
        if existing != data:
            raise BuildError("IMMUTABLE_CONFLICT") from None


def _condition(table, key, row, fields=None):
    """Compare ACL/version snapshots, including the absence of epoch fields."""
    names, values, terms = {}, {}, []
    if row is None:
        name = next(iter(key))
        return {
            "TableName": table, "Key": key,
            "ConditionExpression": "attribute_not_exists(#pk)",
            "ExpressionAttributeNames": {"#pk": name},
        }
    for index, field in enumerate(fields if fields is not None else sorted(set(row) - set(key))):
        name, value = f"#c{index}", f":c{index}"
        names[name] = field
        if field in row:
            values[value] = row[field]
            terms.append(f"{name} = {value}")
        else:
            terms.append(f"attribute_not_exists({name})")
    names["#pk"] = next(iter(key))
    terms.append("attribute_exists(#pk)")
    result = {
        "TableName": table, "Key": key, "ConditionExpression": " AND ".join(terms),
        "ExpressionAttributeNames": names,
    }
    if values:
        result["ExpressionAttributeValues"] = values
    return result


def _guards(ddb, platform, registry, gid, sub, rows):
    """Capture the original rows used by access checks, never reverse indexes."""
    guards = []
    deleted_key = {"pk": f"USER#{sub}", "sk": "DELETED"}
    if group_access.get_item(ddb, platform, deleted_key):
        raise BuildError("ACCESS_DENIED")
    guards.append({"ConditionCheck": _condition(platform, deleted_key, None)})
    grant_key = {"pk": f"USER#{sub}", "sk": f"GROUP#{gid}"}
    grant = group_access.get_item(ddb, platform, grant_key)
    if not grant:
        raise BuildError("ACCESS_DENIED")
    guards.append({"ConditionCheck": _condition(platform, grant_key, grant)})
    for sid, row in rows.items():
        guards.append({"ConditionCheck": _condition(
            registry, {"repo_id": sid}, row, _SOURCE_FIELDS,
        )})
        if row.get("graph_scope") != "public":
            source_key = {"pk": f"USER#{sub}", "sk": f"REPO#{sid}"}
            source_grant = group_access.get_item(ddb, platform, source_key)
            if not source_grant:
                raise BuildError("ACCESS_DENIED")
            guards.append({"ConditionCheck": _condition(platform, source_key, source_grant)})
    return guards


def _assign(request, fields, remove=()):
    terms = []
    for index, (field, value) in enumerate(fields.items()):
        name, token = f"#u{index}", f":u{index}"
        request["ExpressionAttributeNames"][name] = field
        request.setdefault("ExpressionAttributeValues", {})[token] = value
        terms.append(f"{name} = {token}")
    request["UpdateExpression"] = "SET " + ", ".join(terms)
    if remove:
        names = []
        for index, field in enumerate(remove):
            name = f"#r{index}"
            request["ExpressionAttributeNames"][name] = field
            names.append(name)
        request["UpdateExpression"] += " REMOVE " + ", ".join(names)
    return request


def _ledger_matches(ledger, gid, bid, revision, sub):
    return bool(ledger) and all(ledger.get(k) == v for k, v in {
        "group_id": gid, "build_id": bid, "revision": revision, "requested_by": sub,
    }.items()) and ledger.get("status") in {"QUEUED", "BUILDING"}


def _ledger_update(platform, gid, bid, ledger, status, result, code=None):
    request = _condition(
        platform, {"pk": f"GROUP#{gid}", "sk": f"BUILD#{bid}"}, ledger,
        ("group_id", "build_id", "revision", "requested_by", "status",
         "source_versions", "source_description_versions"),
    )
    fields = {
        "status": status, "usage": result.get("usage", {}), "stats": result.get("stats", {}),
        "partial_reasons": result.get("partial_reasons", []),
        "ended_at": int(time.time()),
    }
    if code:
        fields["last_error"] = code
    return {"Update": _assign(request, fields, () if code else ("last_error",))}


def _transaction(ddb, items):
    try:
        ddb.transact_write_items(TransactItems=[
            {operation: _wire(request) for operation, request in item.items()}
            for item in items
        ])
    except Exception as exc:
        if _error_code(exc) == "TransactionCanceledException":
            reasons = getattr(exc, "response", {}).get("CancellationReasons", [])
            if any(reason.get("Code") == "ConditionalCheckFailed" for reason in reasons):
                raise BuildError("STALE_INPUT") from None
        if _error_code(exc) == "ConditionalCheckFailedException":
            raise BuildError("STALE_INPUT") from None
        raise


def _finish(ddb, platform, meta, gid, bid, revision, manifest, guards, ledger):
    condition = _condition(
        platform, _key(gid), meta, ("status", "build_id", "revision") + _GROUP_FIELDS,
    )
    fields = {
        "status": "PARTIAL" if manifest["partial"] else "READY",
        "active_version": bid, "active_revision": revision,
        "active_source_versions": manifest["source_versions"],
        "active_source_descriptions": manifest["source_descriptions"],
        "active_manifest_key": f"groups/{gid}/versions/{bid}/manifest.json",
        "active_graph_key": manifest["graph_key"],
        "build_started_at": 0, "usage": manifest["usage"], "stats": manifest["stats"],
        "partial_reasons": manifest.get("partial_reasons", []),
        "updated_at": int(time.time()),
    }
    _assign(condition, fields, ("last_error", "worker_token", "worker_lease_expires_at"))
    items = [{"Update": condition}, *guards]
    if ledger is not None:
        items.append(_ledger_update(platform, gid, bid, ledger, fields["status"], manifest))
    _transaction(ddb, items)
    return fields["status"]


def _failed(ddb, platform, gid, bid, revision, sub, token, code, ledger, result):
    try:
        request = {
            "TableName": platform, "Key": _key(gid),
            "ConditionExpression": (
                "#status = :building AND build_id = :bid AND revision = :revision AND worker_token = :token"
            ),
            "ExpressionAttributeNames": {"#status": "status"},
            "ExpressionAttributeValues": {
                ":building": "BUILDING", ":bid": bid, ":revision": revision,
                ":token": token,
            },
        }
        usage, stats = (result or {}).get("usage", {}), (result or {}).get("stats", {})
        _assign(request, {
            "status": "FAILED", "last_error": code, "build_started_at": 0,
            "usage": usage, "stats": stats, "updated_at": int(time.time()),
            "partial_reasons": [],
        }, ("worker_token", "worker_lease_expires_at"))
        items = [{"Update": request}]
        if _ledger_matches(ledger, gid, bid, revision, sub):
            items.append(_ledger_update(platform, gid, bid, ledger, "FAILED", result or {}, code))
        _transaction(ddb, items)
        return True
    except Exception as exc:
        if isinstance(exc, BuildError) and exc.code == "STALE_INPUT":
            return False
        raise BuildError("STATUS_UPDATE_FAILED") from None


def run_build(event, context, *, ddb, s3, platform_table, registry_table, bucket, converse=None):
    """Run an already-claimed build; return only public status and safe error codes."""
    try:
        gid, bid, revision, sub = _event(event)
    except BuildError as exc:
        return {"status": "FAILED", "error": exc.code}
    identity = {"group_id": gid, "build_id": bid}
    token = uuid.uuid4().hex
    acquired = False
    ledger, result = None, None
    try:
        _claimed(group_access.get_item(ddb, platform_table, _key(gid)), bid, revision)
        if not _acquire(ddb, platform_table, gid, bid, revision, token):
            return {**identity, "status": "BUSY", "ignored": True}
        acquired = True
        ledger = group_access.get_item(
            ddb, platform_table, {"pk": f"GROUP#{gid}", "sk": f"BUILD#{bid}"},
        )
        if ledger is not None and not _ledger_matches(ledger, gid, bid, revision, sub):
            raise BuildError("INVALID_BUILD_LEDGER")
        meta = _checkpoint(ddb, platform_table, registry_table, gid, bid, revision, sub, token)
        if ledger is not None and not _same_fields(ledger, meta, ("sources", "description", "llm_enabled", "model")):
            raise BuildError("BUILD_INPUT_CHANGED")
        members = _members(meta)
        rows = {}
        for member in members:
            sid = member["source_id"]
            row = group_access.get_item(ddb, registry_table, {"repo_id": sid})
            if not row:
                raise BuildError("SOURCE_VERSION_MISSING")
            rows[sid] = row
        if ledger is not None and (
            ledger.get("source_versions") != {sid: row.get("active_source_version") for sid, row in rows.items()}
            or ledger.get("source_description_versions") != {
                sid: int(row.get("description_version", 0)) for sid, row in rows.items()
            }
        ):
            raise BuildError("SOURCE_VERSION_CHANGED")
        guards = _guards(ddb, platform_table, registry_table, gid, sub, rows)
        # A revoke between require_access and snapshotting must fail before S3.
        _checkpoint(ddb, platform_table, registry_table, gid, bid, revision, sub, token, meta, rows)
        sources, graph_bytes, text_bytes = [], 0, 0
        remaining_ms = getattr(context, "get_remaining_time_in_millis", None)
        for member in members:
            source, graph_size, text_size = _load_source(
                s3, bucket, member, rows[member["source_id"]],
                engine.MAX_GRAPH_BYTES - graph_bytes, engine.MAX_TEXT_BYTES - text_bytes, remaining_ms,
            )
            sources.append(source)
            graph_bytes += graph_size
            text_bytes += text_size
        llm_enabled = meta.get("llm_enabled", False)
        description, model = meta.get("description", ""), meta.get(
            "model", meta.get("model_id", meta.get("llm_model", DEFAULT_MODEL)),
        ) or DEFAULT_MODEL
        if type(llm_enabled) is not bool or not isinstance(description, str) or not isinstance(model, str):
            raise BuildError("INVALID_BUILD_CONFIG")
        _deadline(remaining_ms)
        previous = _previous(s3, bucket, gid, meta)
        _checkpoint(ddb, platform_table, registry_table, gid, bid, revision, sub, token, meta, rows)

        def checked_converse(**kwargs):
            _checkpoint(
                ddb, platform_table, registry_table, gid, bid, revision, sub, token, meta, rows,
            )
            _deadline(remaining_ms)
            return converse(**kwargs)

        result = engine.build_group(
            group_id=gid, sources=sources, description=description,
            llm_enabled=llm_enabled, model_id=model,
            converse=checked_converse if llm_enabled and callable(converse) else None,
            previous_manifest=previous,
            remaining_ms=remaining_ms,
        )
        if (
            not isinstance(result, dict) or type(result.get("partial")) is not bool
            or any(not isinstance(result.get(k), dict) for k in ("graph", "pair_cache", "stats", "usage", "limits"))
            or not isinstance(result.get("partial_reasons"), list)
            or any(not isinstance(reason, str) for reason in result["partial_reasons"])
            or any(not isinstance(result["graph"].get(k), list) for k in ("nodes", "links", "relations"))
        ):
            raise BuildError("INVALID_ENGINE_RESULT")
        reasons = list(result["partial_reasons"])
        if llm_enabled and not callable(converse) and "LLM_UNAVAILABLE" not in reasons:
            reasons.append("LLM_UNAVAILABLE")
        prefix = f"groups/{gid}/versions/{bid}/"
        graph_data = _json_bytes(result["graph"])
        if len(graph_data) > engine.MAX_GRAPH_BYTES:
            raise BuildError("BYTE_LIMIT_EXCEEDED")
        files, uploads = {}, []
        for source in sources:
            sid = source["source_id"]
            files[sid] = {}
            for path, text in sorted(source["files"].items()):
                data = text.encode("utf-8")
                key = f"{prefix}files/{sid}/{hashlib.sha256(path.encode('utf-8')).hexdigest()}.txt"
                files[sid][path] = {
                    "key": key, "sha256": hashlib.sha256(data).hexdigest(),
                    "line_count": len(text.splitlines()),
                }
                uploads.append((key, data))
        manifest = {
            "group_id": gid, "build_id": bid, "version": bid, "revision": revision,
            "source_versions": {source["source_id"]: source["version"] for source in sources},
            "source_descriptions": {source["source_id"]: source["description_version"] for source in sources},
            "graph_key": prefix + "graph.json", "files": files,
            "graph_sha256": hashlib.sha256(graph_data).hexdigest(),
            **{k: result[k] for k in ("stats", "usage", "limits", "pair_cache")},
            "partial": result["partial"] or bool(reasons), "partial_reasons": reasons,
        }
        manifest_data = _json_bytes(manifest)
        if len(manifest_data) > MAX_MANIFEST_BYTES:
            raise BuildError("BYTE_LIMIT_EXCEEDED")
        _checkpoint(ddb, platform_table, registry_table, gid, bid, revision, sub, token, meta, rows)
        for key, data in uploads:
            _deadline(remaining_ms)
            _immutable_put(s3, bucket, key, data, "text/plain; charset=utf-8")
        _deadline(remaining_ms)
        _immutable_put(s3, bucket, prefix + "graph.json", graph_data, "application/json")
        _deadline(remaining_ms)
        _immutable_put(s3, bucket, prefix + "manifest.json", manifest_data, "application/json")
        # Recheck access immediately before the atomic version/ACL guarded CAS.
        _checkpoint(ddb, platform_table, registry_table, gid, bid, revision, sub, token, meta, rows)
        _deadline(remaining_ms)
        status = _finish(ddb, platform_table, meta, gid, bid, revision, manifest, guards, ledger)
        return {**identity, "status": status, "version": bid}
    except _Stale:
        return {**identity, "status": "STALE", "ignored": True}
    except Exception as exc:
        if isinstance(exc, group_access.GroupError):
            code = "ACCESS_DENIED"
        elif isinstance(exc, (BuildError, engine.EngineError)):
            code = exc.code if isinstance(exc.code, str) and _SAFE_CODE.fullmatch(exc.code) else "BUILD_FAILED"
        else:
            code = "BUILD_FAILED"
        if not acquired:
            raise BuildError(code) from None
        outcome = result if isinstance(result, dict) and all(
            isinstance(result.get(k), dict) for k in ("usage", "stats")
        ) else None
        applied = _failed(ddb, platform_table, gid, bid, revision, sub, token, code, ledger, outcome)
        return {**identity, "status": "FAILED" if applied else "STALE", "error": code}


_services = None


def handler(event, context):
    if isinstance(event, dict) and event.get("reconcile"):
        try:
            from .reconcile import reconcile
        except ImportError:
            from reconcile import reconcile
        return reconcile(event, context)
    global _services
    if _services is None:
        import boto3
        from botocore.config import Config

        config = Config(connect_timeout=3, read_timeout=10, retries={"total_max_attempts": 2})
        _services = (
            boto3.client("dynamodb", config=config), boto3.client("s3", config=config),
        )
    bedrock = None
    bedrock_timeout = None

    def converse(**kwargs):
        nonlocal bedrock, bedrock_timeout
        remaining = getattr(context, "get_remaining_time_in_millis", lambda: 900_000)()
        timeout = min(60, int(remaining / 1000) - 10)
        if timeout < 1:
            raise TimeoutError("BUILD_DEADLINE")
        if bedrock is None or bedrock_timeout != timeout:
            import boto3
            from botocore.config import Config
            # The engine owns retry accounting; SDK retries would hide token spend.
            bedrock = boto3.client("bedrock-runtime", config=Config(
                connect_timeout=min(3, timeout), read_timeout=timeout,
                retries={"total_max_attempts": 1},
            ))
            bedrock_timeout = timeout
        return bedrock.converse(**kwargs)

    return run_build(
        event, context, ddb=_services[0], s3=_services[1],
        platform_table=os.environ["PLATFORM_TABLE"],
        registry_table=os.environ["REGISTRY_TABLE"], bucket=os.environ["GRAPH_BUCKET"],
        converse=converse,
    )
