"""Publish a graph/snapshot pair from ONE build for versioned group reads."""

import base64
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path


GRAPH_CAP = 32 * 1024 * 1024
SNAPSHOT_CAP = 200 * 1024 * 1024


def _digest(path):
    sha = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            sha.update(chunk)
    return sha.hexdigest()


def publish(s3, registry, bucket, repo_id, build_id, build_arn, graph, snapshot):
    item = registry.get_item(Key={"repo_id": repo_id}, ConsistentRead=True).get("Item")
    if not item or item.get("enabled") != "1" or item.get("build_arn") != build_arn:
        return {"state": "stale"}
    version = hashlib.sha256(build_arn.encode()).hexdigest()[:32]
    prefix = f"source-versions/{repo_id}/{version}"
    graph, snapshot = Path(graph), Path(snapshot)
    problem = ""
    for path, limit in ((graph, GRAPH_CAP), (snapshot, SNAPSHOT_CAP)):
        if not path.is_file():
            problem = "group source version unavailable: graph or snapshot missing"
        elif not 0 < path.stat().st_size <= limit:
            problem = "group source version unavailable: graph/snapshot exceeds group limits"
    condition = "build_arn = :arn AND enabled = :enabled"
    values = {":arn": build_arn, ":enabled": "1"}
    if problem:
        try:
            registry.update_item(
                Key={"repo_id": repo_id}, ConditionExpression=condition,
                UpdateExpression="SET group_version_error = :err REMOVE active_source_version",
                ExpressionAttributeValues={**values, ":err": problem},
            )
        except registry.meta.client.exceptions.ConditionalCheckFailedException:
            return {"state": "stale"}
        return {"state": "unavailable", "reason": problem}
    manifest = {
        "schema_version": 1, "repo_id": repo_id, "version": version,
        "build_id": build_id, "created_at": datetime.now(timezone.utc).isoformat(),
    }
    for kind, path, filename in (("graph", graph, "graph.json"), ("snapshot", snapshot, "src.tar.gz")):
        sha = _digest(path)
        key = f"{prefix}/{filename}"
        with path.open("rb") as stream:
            try:
                s3.put_object(Bucket=bucket, Key=key, Body=stream, IfNoneMatch="*",
                              Metadata={"sha256": sha},
                              ChecksumSHA256=base64.b64encode(bytes.fromhex(sha)).decode(),
                              ContentType="application/json" if kind == "graph" else "application/gzip")
            except Exception as exc:
                if getattr(exc, "response", {}).get("Error", {}).get("Code") != "PreconditionFailed":
                    raise
                prior = s3.head_object(Bucket=bucket, Key=key)
                if prior.get("Metadata", {}).get("sha256") != sha:
                    raise RuntimeError("immutable source version content changed") from None
        manifest[f"{kind}_key"], manifest[f"{kind}_sha256"] = key, sha
    payload = json.dumps(manifest, sort_keys=True).encode()
    # Only this build can write this prefix; retries reuse graph and snapshot.
    s3.put_object(Bucket=bucket, Key=f"{prefix}/manifest.json", Body=payload,
                  ContentType="application/json")
    try:
        registry.update_item(
            Key={"repo_id": repo_id}, ConditionExpression=condition,
            UpdateExpression="SET active_source_version = :version REMOVE group_version_error",
            ExpressionAttributeValues={**values, ":version": version},
        )
    except registry.meta.client.exceptions.ConditionalCheckFailedException:
        return {"state": "stale", "version": version}
    return {"state": "published", "version": version}


def main():
    import boto3
    s3 = boto3.client("s3")
    registry = boto3.resource("dynamodb").Table(os.environ["REGISTRY_TABLE"])
    result = publish(
        s3, registry, os.environ["GRAPH_BUCKET"], os.environ["REPO_ID"],
        os.environ["CODEBUILD_BUILD_ID"], os.environ["CODEBUILD_BUILD_ARN"],
        Path(os.environ.get("GRAPHIFY_OUT", "/tmp/work/graphify-out")) / "graph.json",
        Path("/tmp/work/src.tar.gz"),
    )
    print(json.dumps(result))


if __name__ == "__main__":
    main()
