"""Reclaim obsolete immutable artifacts; active/in-flight versions are pinned."""

import hashlib
import time

from boto3.dynamodb.types import TypeSerializer
from group_access import get_item

_ser = TypeSerializer()


def _typed(item):
    return {k: _ser.serialize(v) for k, v in item.items()}


def sweep(ddb, s3, platform, registry, bucket, *, now=None):
    now = time.time() if now is None else now
    deleted = 0
    for root in ("groups/", "source-versions/"):
        key = {"pk": "SYSTEM#GROUP_GC", "sk": root}
        state = get_item(ddb, platform, key) or {}
        params = {"Bucket": bucket, "Prefix": root, "MaxKeys": 1000}
        cursor = state.get("cursor", "")
        if isinstance(cursor, str) and cursor.startswith(root):
            params["StartAfter"] = cursor
        page = s3.list_objects_v2(**params)
        objects, cache, discard = page.get("Contents", []), {}, []
        for obj in objects:
            # A worker lasts <=15min. This age floor also protects a new
            # version before its final active pointer becomes visible.
            if now - obj["LastModified"].timestamp() < 3600:
                continue
            parts = obj["Key"].split("/")
            if root == "groups/":
                if len(parts) < 5 or parts[2] != "versions":
                    continue
                identity, version = parts[1], parts[3]
                if identity not in cache:
                    cache[identity] = get_item(ddb, platform, {"pk": f"GROUP#{identity}", "sk": "META"})
                meta = cache[identity] or {}
                pins = {meta.get("active_version"), meta.get("build_id")} if meta.get("status") != "DELETED" else set()
            else:
                if len(parts) < 4:
                    continue
                identity, version = parts[1], parts[2]
                if identity not in cache:
                    cache[identity] = get_item(ddb, registry, {"repo_id": identity})
                meta = cache[identity] or {}
                pins = {meta.get("active_source_version")}
                if meta.get("build_arn"):
                    pins.add(hashlib.sha256(meta["build_arn"].encode()).hexdigest()[:32])
                if meta.get("enabled") != "1":
                    pins = set()
            if version not in pins:
                discard.append({"Key": obj["Key"]})
        if discard:
            result = s3.delete_objects(Bucket=bucket, Delete={"Objects": discard, "Quiet": True})
            if result.get("Errors"):
                # Keep the old cursor so a later tick can retry failed keys.
                continue
            deleted += len(discard)
        next_cursor = objects[-1]["Key"] if page.get("IsTruncated") and objects else ""
        ddb.put_item(TableName=platform, Item=_typed({**key, "cursor": next_cursor}))
    return {"obsolete_objects_deleted": deleted}
