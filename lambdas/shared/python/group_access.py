"""Shared, fail-closed source-group authorization for every Python surface."""

import re

from boto3.dynamodb.types import TypeDeserializer, TypeSerializer


GROUP_ID_RE = re.compile(r"^grp_[0-9a-f]{32}$")
MAX_SOURCES = 8
_serializer = TypeSerializer()
_deserializer = TypeDeserializer()


class GroupError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def is_group_id(value):
    return isinstance(value, str) and bool(GROUP_ID_RE.fullmatch(value))


def get_item(ddb, table, key):
    item = ddb.get_item(
        TableName=table,
        Key={k: _serializer.serialize(v) for k, v in key.items()},
        ConsistentRead=True,
    ).get("Item")
    return {k: _deserializer.deserialize(v) for k, v in item.items()} if item else None


def source_access(ddb, platform_table, registry_table, sub, source_id):
    if not isinstance(source_id, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,200}", source_id):
        raise GroupError(400, "invalid source ID")
    source = get_item(ddb, registry_table, {"repo_id": source_id})
    if not source or source.get("enabled") != "1":
        raise GroupError(403, "a group source is missing or disabled")
    if source.get("graph_scope") != "public" and not get_item(
        ddb, platform_table, {"pk": f"USER#{sub}", "sk": f"REPO#{source_id}"}
    ):
        raise GroupError(403, "access to every group source is required")
    return source


def require_access(ddb, platform_table, registry_table, sub, gid, *,
                   write=False, check_versions=False):
    if not sub or not is_group_id(gid):
        raise GroupError(404, "group not found")
    if get_item(ddb, platform_table, {"pk": f"USER#{sub}", "sk": "DELETED"}):
        raise GroupError(403, "this account no longer exists")
    grant = get_item(ddb, platform_table, {"pk": f"USER#{sub}", "sk": f"GROUP#{gid}"})
    if not grant or grant.get("role") not in ("owner", "editor", "viewer"):
        raise GroupError(403, "group membership required")
    meta = get_item(ddb, platform_table, {"pk": f"GROUP#{gid}", "sk": "META"})
    if not meta or meta.get("status") == "DELETED":
        raise GroupError(404, "group not found")
    if write and grant["role"] not in ("owner", "editor"):
        raise GroupError(403, "group edit permission required")
    sources = meta.get("sources", [])
    if not isinstance(sources, list) or len(sources) > MAX_SOURCES:
        raise GroupError(409, "invalid group source configuration")
    ids = [s.get("source_id") for s in sources if isinstance(s, dict)]
    if len(ids) != len(sources) or len(ids) != len(set(ids)):
        raise GroupError(409, "invalid group source configuration")
    current, descriptions = {}, {}
    for sid in ids:
        source = source_access(ddb, platform_table, registry_table, sub, sid)
        current[sid] = source.get("active_source_version", "")
        descriptions[sid] = int(source.get("description_version", 0))
    if check_versions:
        if (meta.get("status") not in ("READY", "PARTIAL")
                or not meta.get("active_version")
                or meta.get("active_revision") != meta.get("revision")
                or not sources or not all(current.values())
                or current != meta.get("active_source_versions")
                or ("active_source_descriptions" in meta
                    and descriptions != meta["active_source_descriptions"])):
            raise GroupError(409, "group graph is not current; rebuild the group")
    return meta
