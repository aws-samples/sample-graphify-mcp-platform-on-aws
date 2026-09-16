"""Offline tests: real shared ACLs, atomic fake DynamoDB, and immutable fake S3.

Run: .venv/bin/python -B -m unittest discover -s tests -p test_group_worker.py
"""

import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import re
import socket
import sys
import tarfile
import types
import unittest
from unittest import mock

import boto3
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer


ROOT = Path(__file__).resolve().parents[1]
PLATFORM, REGISTRY, BUCKET = "offline-platform", "offline-registry", "offline-bucket"
GID, BID, SUB = "grp_" + "a" * 32, "bld_" + "b" * 32, "offline-user"
NOW = 1_800_000_000
SERIALIZER, DESERIALIZER = TypeSerializer(), TypeDeserializer()
MISSING = object()


def typed(item):
    return {k: SERIALIZER.serialize(v) for k, v in item.items()}


def decoded(item):
    return {k: DESERIALIZER.deserialize(v) for k, v in item.items()}


def data(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


class AwsError(Exception):
    def __init__(self, code, reasons=None):
        super().__init__("NEVER EXPOSE private-source.txt s3://secret-bucket or model prompt")
        self.response = {"Error": {"Code": code}}
        if reasons:
            self.response["CancellationReasons"] = reasons


class Condition:
    """Strict parser for the actual worker condition expressions."""

    def __init__(self, expression, item, names, values):
        self.tokens = re.findall(r"attribute_not_exists|attribute_exists|[#:]?\w+|[=<(),]", expression)
        assert "".join(self.tokens) == re.sub(r"\s+", "", expression), expression
        self.index = 0
        self.item, self.names, self.values = item, names, values

    def take(self, expected=None):
        result = self.tokens[self.index]
        self.index += 1
        if expected is not None:
            assert result == expected, (result, expected)
        return result

    def peek(self, value):
        return self.index < len(self.tokens) and self.tokens[self.index] == value

    def operand(self):
        token = self.take()
        if token.startswith(":"):
            return self.values[token]
        return self.item.get(self.names.get(token, token), MISSING)

    def atom(self):
        if self.peek("("):
            self.take("(")
            result = self.disjunction()
            self.take(")")
            return result
        if self.peek("attribute_exists") or self.peek("attribute_not_exists"):
            function = self.take()
            self.take("(")
            present = self.operand() is not MISSING
            self.take(")")
            return present if function == "attribute_exists" else not present
        left, operation, right = self.operand(), self.take(), self.operand()
        if left is MISSING or right is MISSING:
            return False
        if operation == "=":
            return left == right
        if operation == "<":
            return left < right
        raise AssertionError(operation)

    def conjunction(self):
        result = self.atom()
        while self.peek("AND"):
            self.take("AND")
            rhs = self.atom()
            result = result and rhs
        return result

    def disjunction(self):
        result = self.conjunction()
        while self.peek("OR"):
            self.take("OR")
            rhs = self.conjunction()
            result = result or rhs
        return result

    def evaluate(self):
        result = self.disjunction()
        assert self.index == len(self.tokens)
        return result


class FakeDDB:
    def __init__(self):
        self.rows, self.reads, self.updates, self.transactions = {}, [], [], []
        self.before_update = self.before_transaction = None

    @staticmethod
    def address(table, key):
        return table, tuple(sorted(key.items()))

    def put(self, table, key, **fields):
        self.rows[self.address(table, key)] = {**key, **copy.deepcopy(fields)}

    def row(self, table, key):
        return self.rows.get(self.address(table, key))

    def delete(self, table, key):
        self.rows.pop(self.address(table, key), None)

    def get_item(self, *, TableName, Key, ConsistentRead):
        assert ConsistentRead is True
        key = decoded(Key)
        self.reads.append((TableName, key))
        item = self.row(TableName, key)
        return {"Item": typed(copy.deepcopy(item))} if item else {}

    def allowed(self, request):
        return Condition(
            request["ConditionExpression"],
            self.row(request["TableName"], decoded(request["Key"])) or {},
            request.get("ExpressionAttributeNames", {}),
            decoded(request.get("ExpressionAttributeValues", {})),
        ).evaluate()

    def apply(self, request):
        key, values = decoded(request["Key"]), decoded(request["ExpressionAttributeValues"])
        names = request.get("ExpressionAttributeNames", {})
        row = self.rows.setdefault(self.address(request["TableName"], key), dict(key))
        setting, _, removing = request["UpdateExpression"].partition(" REMOVE ")
        assert setting.startswith("SET ")
        for part in setting[4:].split(","):
            name, token = (v.strip() for v in part.split(" = "))
            row[names.get(name, name)] = copy.deepcopy(values[token])
        for name in filter(None, (part.strip() for part in removing.split(","))):
            row.pop(names.get(name, name), None)

    def update_item(self, **request):
        self.updates.append(copy.deepcopy(request))
        if self.before_update:
            self.before_update(request)
        if not self.allowed(request):
            raise AwsError("ConditionalCheckFailedException")
        self.apply(request)
        return {}

    def transact_write_items(self, *, TransactItems):
        self.transactions.append(copy.deepcopy(TransactItems))
        if self.before_transaction:
            self.before_transaction()
        addresses = [
            self.address(request["TableName"], decoded(request["Key"]))
            for item in TransactItems for request in item.values()
        ]
        assert len(set(addresses)) == len(addresses), "Duplicate transaction item"
        if not all(self.allowed(request) for item in TransactItems for request in item.values()):
            raise AwsError("TransactionCanceledException", [{"Code": "ConditionalCheckFailed"}])
        for item in TransactItems:
            if "Update" in item:
                self.apply(item["Update"])
        return {}


class FakeS3:
    def __init__(self):
        self.objects, self.reads, self.writes, self.bodies = {}, [], [], []
        self.after_get = self.after_put = None
        self.lengths = {}

    def get_object(self, *, Bucket, Key):
        assert Bucket == BUCKET
        self.reads.append(Key)
        if Key not in self.objects:
            raise AwsError("NoSuchKey")
        body = io.BytesIO(self.objects[Key])
        self.bodies.append(body)
        result = {"Body": body}
        length = self.lengths.get(Key, len(self.objects[Key]))
        if length is not None:
            result["ContentLength"] = length
        if self.after_get:
            self.after_get(Key)
        return result

    def put_object(self, *, Bucket, Key, Body, ContentType, IfNoneMatch):
        assert Bucket == BUCKET and IfNoneMatch == "*"
        assert isinstance(Body, bytes)
        self.writes.append((Key, Body, ContentType))
        if Key in self.objects:
            raise AwsError("PreconditionFailed")
        self.objects[Key] = Body
        if self.after_put:
            self.after_put(Key)
        return {}


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class WorkerTests(unittest.TestCase):
    def setUp(self):
        for patcher in (
            mock.patch.object(socket, "create_connection", side_effect=AssertionError("No network")),
            mock.patch.object(boto3, "client", side_effect=AssertionError("No live clients")),
            mock.patch.object(boto3, "resource", side_effect=AssertionError("No live resources")),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.access = load("lambdas/shared/python/group_access.py", "worker_test_access")
        self.engine = types.ModuleType("engine")

        class EngineError(Exception):
            def __init__(self, code, message):
                super().__init__(message)
                self.code = code

        self.engine.EngineError = EngineError
        self.engine.MAX_FILE_BYTES = 2 * 1024 * 1024
        self.engine.MAX_TEXT_BYTES = self.engine.MAX_GRAPH_BYTES = 32 * 1024 * 1024
        self.engine.MAX_SNAPSHOT_BYTES = 200 * 1024 * 1024
        self.engine.parse_snapshot = mock.Mock(side_effect=lambda raw: json.loads(raw))
        self.output = {
            "graph": {
                "directed": True, "multigraph": True,
                "nodes": [{"id": "source-a:original", "source_id": "source-a", "source_version": "v1"}],
                "links": [], "relations": [],
            },
            "pair_cache": {"completed-pair": {"decisions": []}},
            "stats": {"nodes": 1}, "usage": {"input_tokens": 0}, "limits": {"max_sources": 8},
            "partial": False, "partial_reasons": [],
        }
        self.engine.build_group = mock.Mock(side_effect=lambda **kwargs: copy.deepcopy(self.output))
        with mock.patch.dict(sys.modules, {"group_access": self.access, "engine": self.engine}):
            self.worker = load("lambdas/group_worker/handler.py", "worker_under_test")
        patcher = mock.patch.object(self.worker.time, "time", return_value=NOW)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.ddb, self.s3 = FakeDDB(), FakeS3()
        self.meta_key = {"pk": f"GROUP#{GID}", "sk": "META"}
        self.grant_key = {"pk": f"USER#{SUB}", "sk": f"GROUP#{GID}"}
        self.deleted_key = {"pk": f"USER#{SUB}", "sk": "DELETED"}
        self.ddb.put(PLATFORM, self.meta_key, group_id=GID, status="BUILDING", build_id=BID,
                     revision=3, owner_sub=SUB, sources=[], description="Group description",
                     llm_enabled=False, model="", active_version="old",
                     active_revision=2, active_source_versions={"source-a": "v0"},
                     active_source_descriptions={"source-a": 1},
                     active_manifest_key=f"groups/{GID}/versions/old/manifest.json",
                     active_graph_key=f"groups/{GID}/versions/old/graph.json")
        self.ddb.put(PLATFORM, self.grant_key, role="editor")
        self.add_source()
        self.prior_active = {k: copy.deepcopy(v) for k, v in self.meta.items() if k.startswith("active_")}
        self.event = {"group_id": GID, "build_id": BID, "revision": 3, "requested_by": SUB}
        self.context = mock.Mock()
        self.context.get_remaining_time_in_millis.return_value = 120_000

    @property
    def meta(self):
        return self.ddb.row(PLATFORM, self.meta_key)

    def source(self, sid="source-a"):
        return self.ddb.row(REGISTRY, {"repo_id": sid})

    def source_grant(self, sid="source-a"):
        return {"pk": f"USER#{SUB}", "sk": f"REPO#{sid}"}

    def add_source(self, sid="source-a", *, files=None, public=False):
        self.meta["sources"].append({"source_id": sid, "role": "backend", "description": "Member context"})
        self.ddb.put(REGISTRY, {"repo_id": sid}, enabled="1", graph_scope="public" if public else "private",
                     active_source_version="v1", source_epoch=1, acl_epoch=1,
                     description="Common context", description_version=2)
        if not public:
            self.ddb.put(PLATFORM, self.source_grant(sid), role="reader")
        prefix = f"source-versions/{sid}/v1/"
        graph = data({"nodes": [{"id": "original", "label": "API", "file": "README.md"}], "links": []})
        snapshot = data(files if files is not None else {"README.md": "REQ-001\r\n한글\n", "empty.txt": ""})
        self.s3.objects[prefix + "graph.json"] = graph
        self.s3.objects[prefix + "src.tar.gz"] = snapshot
        self.s3.objects[prefix + "manifest.json"] = data({
            "schema_version": 1, "repo_id": sid, "version": "v1",
            "build_id": "source-build:offline", "created_at": "2026-09-15T00:00:00Z",
            "graph_key": prefix + "graph.json", "snapshot_key": prefix + "src.tar.gz",
            "graph_sha256": digest(graph), "snapshot_sha256": digest(snapshot),
        })

    def manifest(self):
        return json.loads(self.s3.objects[f"groups/{GID}/versions/{BID}/manifest.json"])

    def rewrite_source_manifest(self, **changes):
        key = "source-versions/source-a/v1/manifest.json"
        manifest = json.loads(self.s3.objects[key])
        manifest.update(changes)
        self.s3.objects[key] = data(manifest)

    def run_worker(self, **kwargs):
        return self.worker.run_build(
            self.event, self.context, ddb=self.ddb, s3=self.s3,
            platform_table=PLATFORM, registry_table=REGISTRY, bucket=BUCKET, **kwargs,
        )

    def assert_prior_active(self):
        self.assertEqual({k: v for k, v in self.meta.items() if k.startswith("active_")}, self.prior_active)

    def queue_ledger(self):
        key = {"pk": f"GROUP#{GID}", "sk": f"BUILD#{BID}"}
        self.ddb.put(
            PLATFORM, key, group_id=GID, build_id=BID, revision=3, requested_by=SUB,
            status="QUEUED", created_at=NOW, source_versions={"source-a": "v1"},
            source_description_versions={"source-a": 2},
            **{field: self.meta[field] for field in ("sources", "description", "llm_enabled", "model")},
        )
        return self.ddb.row(PLATFORM, key)

    def test_normal_pipeline_checksums_lines_metadata_and_atomic_ledger(self):
        ledger = self.queue_ledger()
        original_ledger = copy.deepcopy(ledger)
        self.meta["build_started_at"] = NOW
        self.assertEqual(self.run_worker()["status"], "READY")
        manifest = self.manifest()
        self.assertEqual(manifest["source_versions"], {"source-a": "v1"})
        self.assertEqual(manifest["source_descriptions"], {"source-a": 2})
        self.assertEqual(manifest["graph_sha256"], digest(self.s3.objects[manifest["graph_key"]]))
        for key in ("stats", "usage", "limits", "partial", "partial_reasons", "pair_cache"):
            self.assertEqual(manifest[key], self.output[key])
        file = manifest["files"]["source-a"]["README.md"]
        raw = "REQ-001\r\n한글\n".encode()
        self.assertEqual(self.s3.objects[file["key"]], raw)
        self.assertEqual(file["sha256"], digest(raw))
        self.assertEqual(file["line_count"], 2)
        self.assertTrue(file["key"].endswith(f"/files/source-a/{digest(b'README.md')}.txt"))
        self.assertEqual(manifest["files"]["source-a"]["empty.txt"]["line_count"], 0)
        self.assertEqual(self.meta["active_version"], BID)
        self.assertEqual(self.meta["active_source_descriptions"], {"source-a": 2})
        self.assertNotIn("worker_token", self.meta)
        self.assertNotIn("worker_lease_expires_at", self.meta)
        self.assertEqual(self.meta["build_started_at"], 0)
        self.assertEqual(self.meta["usage"], manifest["usage"])
        self.assertEqual(ledger["status"], "READY")
        self.assertEqual(ledger["ended_at"], NOW)
        self.assertEqual(ledger["usage"], manifest["usage"])
        for key, value in original_ledger.items():
            if key != "status":
                self.assertEqual(ledger[key], value)
        self.assertEqual(len(self.ddb.transactions), 1)
        args = self.engine.build_group.call_args.kwargs
        self.assertEqual(args["model_id"], self.worker.DEFAULT_MODEL)
        self.assertEqual(args["sources"][0]["common_description"], "Common context")
        self.assertEqual(args["sources"][0]["description"], "Member context")
        self.assertEqual(args["sources"][0]["description_version"], 2)
        self.assertIsNone(args["converse"])
        self.assertEqual(args["remaining_ms"](), 120_000)
        self.assertTrue(all(body.closed for body in self.s3.bodies))
        self.assertFalse(any("/latest/" in key for key in self.s3.reads))

    def test_invalid_event_has_no_storage_access(self):
        for change in (
            {"group_id": "../group"}, {"build_id": "x/../../foreign"},
            {"revision": True}, {"revision": "3"}, {"revision": 0},
            {"requested_by": ""}, {"sub": "another-user"},
        ):
            with self.subTest(change=change):
                result = self.worker.run_build(
                    {**self.event, **change}, self.context, ddb=self.ddb, s3=self.s3,
                    platform_table=PLATFORM, registry_table=REGISTRY, bucket=BUCKET,
                )
                self.assertEqual(result["error"], "INVALID_EVENT")
        self.assertFalse(self.ddb.reads)
        self.assertFalse(self.ddb.updates)

    def test_manifest_identity_version_containment_schema_and_hash_validation(self):
        original = self.s3.objects["source-versions/source-a/v1/manifest.json"]
        for change in (
            {"repo_id": "foreign"}, {"version": "v2"}, {"schema_version": 2},
            {"graph_key": "repos/source-a/latest/graph.json"},
            {"snapshot_key": "source-versions/source-a/v2/src.tar.gz"},
            {"graph_key": "source-versions/source-a/v1/../v2/graph.json"},
            {"graph_key": "source-versions/foreign/v1/graph.json"},
            {"snapshot_key": "source-versions/source-a/v1/manifest.json"},
            {"graph_sha256": "not-a-checksum"}, {"build_id": ""}, {"created_at": ""},
        ):
            with self.subTest(change=change):
                self.meta["status"] = "BUILDING"
                self.s3.objects["source-versions/source-a/v1/manifest.json"] = original
                self.rewrite_source_manifest(**change)
                self.assertEqual(self.run_worker()["error"], "INVALID_SOURCE_MANIFEST")
                self.assert_prior_active()
        self.engine.build_group.assert_not_called()
        self.assertFalse(self.s3.writes)

    def test_checksum_and_missing_old_version_never_fall_back_to_latest(self):
        for suffix in ("graph.json", "src.tar.gz", "manifest.json"):
            key = "source-versions/source-a/v1/" + suffix
            original = self.s3.objects[key]
            with self.subTest(suffix=suffix):
                if suffix != "manifest.json":
                    self.s3.objects[key] = original + b" "
                    self.meta["status"] = "BUILDING"
                    self.assertEqual(self.run_worker()["error"], "CHECKSUM_MISMATCH")
                self.s3.objects.pop(key)
                self.s3.objects["repos/source-a/latest/" + suffix] = original
                self.meta["status"] = "BUILDING"
                self.assertEqual(self.run_worker()["error"], "SOURCE_VERSION_MISSING")
                self.s3.objects[key] = original
                self.assert_prior_active()
        self.assertFalse(any("/latest/" in key for key in self.s3.reads))
        self.engine.build_group.assert_not_called()

    def test_stale_before_read_does_not_claim(self):
        self.meta["revision"] = 4
        self.assertEqual(self.run_worker()["status"], "STALE")
        self.assertFalse(self.s3.reads)
        self.assertFalse(self.ddb.updates)

    def test_stale_during_read_stops_model(self):
        self.s3.after_get = lambda key: self.meta.update(revision=4)
        self.assertEqual(self.run_worker()["status"], "STALE")
        self.engine.build_group.assert_not_called()
        self.assertFalse(self.s3.writes)
        self.assert_prior_active()

    def test_stale_during_engine_stops_publication(self):
        def build(**kwargs):
            self.meta["build_id"] = "newer-build"
            return copy.deepcopy(self.output)
        self.engine.build_group.side_effect = build
        self.assertEqual(self.run_worker()["status"], "STALE")
        self.assertFalse(self.s3.writes)
        self.assert_prior_active()

    def test_stale_after_artifacts_prevents_pointer_cas(self):
        def change(key):
            if key.endswith("/manifest.json"):
                self.meta["revision"] = 4
        self.s3.after_put = change
        self.assertEqual(self.run_worker()["status"], "STALE")
        self.assertFalse(self.ddb.transactions)
        self.assert_prior_active()

    def test_acl_revocation_before_artifacts_and_during_engine(self):
        self.ddb.delete(PLATFORM, self.source_grant())
        self.assertEqual(self.run_worker()["error"], "ACCESS_DENIED")
        self.assertFalse(self.s3.reads)
        self.assertNotIn("worker_token", self.meta)
        self.ddb.put(PLATFORM, self.source_grant(), role="reader")
        self.meta["status"] = "BUILDING"
        def build(**kwargs):
            self.ddb.delete(PLATFORM, self.grant_key)
            return copy.deepcopy(self.output)
        self.engine.build_group.side_effect = build
        self.assertEqual(self.run_worker()["error"], "ACCESS_DENIED")
        self.assertFalse(self.s3.writes)
        self.assert_prior_active()

    def test_atomic_guards_close_acl_version_epoch_and_description_races(self):
        changes = {
            "version": lambda: self.source().update(active_source_version="v2"),
            "epoch": lambda: self.source().update(source_epoch=2),
            "acl epoch": lambda: self.source().update(acl_epoch=2),
            "disabled": lambda: self.source().update(enabled="0"),
            "description": lambda: self.source().update(description="changed"),
            "description version": lambda: self.source().update(description_version=3),
            "source deleted": lambda: self.ddb.delete(REGISTRY, {"repo_id": "source-a"}),
            "source grant": lambda: self.ddb.delete(PLATFORM, self.source_grant()),
            "group grant": lambda: self.ddb.delete(PLATFORM, self.grant_key),
            "group downgrade": lambda: self.ddb.row(PLATFORM, self.grant_key).update(role="viewer"),
            "deleted user": lambda: self.ddb.put(PLATFORM, self.deleted_key, deleted=True),
        }
        original = copy.deepcopy(self.ddb.rows)
        for name, change in changes.items():
            with self.subTest(name=name):
                self.ddb.rows = copy.deepcopy(original)
                def race():
                    self.ddb.before_transaction = None  # Only race the publication transaction.
                    change()
                self.ddb.before_transaction = race
                self.assertEqual(self.run_worker()["error"], "STALE_INPUT")
                self.assertEqual(self.meta["status"], "FAILED")
                self.assertNotIn("worker_token", self.meta)
                self.assert_prior_active()

    def test_absent_epoch_is_fenced_and_newer_group_is_not_failed(self):
        self.source().pop("acl_epoch")
        def race():
            self.ddb.before_transaction = None
            self.source()["acl_epoch"] = 1
        self.ddb.before_transaction = race
        self.assertEqual(self.run_worker()["error"], "STALE_INPUT")
        self.meta["status"] = "BUILDING"
        def newer():
            self.ddb.before_transaction = None
            self.meta.update(revision=4, build_id="newer")
        self.ddb.before_transaction = newer
        self.assertEqual(self.run_worker()["status"], "STALE")
        self.assertEqual(self.meta["status"], "BUILDING")
        self.assert_prior_active()

    def test_ledger_rejects_wrong_actor_and_sources_changed_while_queued(self):
        ledger = self.queue_ledger()
        ledger["requested_by"] = "other"
        self.assertEqual(self.run_worker()["error"], "INVALID_BUILD_LEDGER")
        self.assertEqual(ledger["status"], "QUEUED")
        self.meta["status"] = "BUILDING"
        ledger["requested_by"] = SUB
        self.source()["active_source_version"] = "v2"
        self.assertEqual(self.run_worker()["error"], "SOURCE_VERSION_CHANGED")
        self.assertEqual(ledger["status"], "FAILED")
        self.assertFalse(self.s3.reads)
        self.assert_prior_active()

    def test_early_acl_failure_also_terminates_matching_ledger(self):
        ledger = self.queue_ledger()
        self.ddb.delete(PLATFORM, self.source_grant())
        self.assertEqual(self.run_worker()["error"], "ACCESS_DENIED")
        self.assertEqual(ledger["status"], "FAILED")
        self.assertEqual(ledger["last_error"], "ACCESS_DENIED")
        self.assertEqual(self.meta["build_started_at"], 0)
        self.assertFalse(self.s3.reads)

    def test_full_relation_evidence_and_fractional_usage_survive_publication(self):
        quote = "REQ-001"
        relation = {
            "id": "edge-1", "source": "source-a:original", "target": "source-b:original",
            "relation": "shared_requirement", "confidence_score": 0.9,
            "evidence": [{
                "source_id": "source-a", "source_version": "v1", "file": "README.md",
                "line_start": 1, "line_end": 1, "quote": quote, "sha256": digest(quote.encode()),
            }],
        }
        self.output["graph"]["links"] = [copy.deepcopy(relation)]
        self.output["graph"]["relations"] = [copy.deepcopy(relation)]
        self.output["usage"]["estimated_cost_usd"] = 0.001
        self.assertEqual(self.run_worker()["status"], "READY")
        graph = json.loads(self.s3.objects[self.manifest()["graph_key"]])
        self.assertEqual(graph["links"], [relation])
        self.assertEqual(graph["relations"], [relation])
        self.assertEqual(float(self.meta["usage"]["estimated_cost_usd"]), 0.001)

    def test_low_level_requests_pass_offline_botocore_shape_validation(self):
        from botocore.session import get_session
        from botocore.validate import validate_parameters
        self.queue_ledger()
        self.assertEqual(self.run_worker()["status"], "READY")
        service = get_session().get_service_model("dynamodb")
        for request in self.ddb.updates:
            validate_parameters(request, service.operation_model("UpdateItem").input_shape)
        for transaction in self.ddb.transactions:
            validate_parameters(
                {"TransactItems": transaction}, service.operation_model("TransactWriteItems").input_shape,
            )

    def test_failure_sanitizes_codes_and_updates_ledger_preserving_inputs(self):
        ledger = self.queue_ledger()
        self.meta["build_started_at"] = NOW
        self.engine.build_group.side_effect = RuntimeError("secret prompt/token s3://private")
        result = self.run_worker()
        self.assertEqual(result["error"], "BUILD_FAILED")
        self.assertEqual(self.meta["last_error"], "BUILD_FAILED")
        self.assertNotIn("secret", str(result))
        self.assertEqual(self.meta["build_started_at"], 0)
        self.assertEqual(ledger["status"], "FAILED")
        self.assertEqual(ledger["source_versions"], {"source-a": "v1"})
        self.assertEqual(ledger["last_error"], "BUILD_FAILED")
        self.assertEqual(ledger["ended_at"], NOW)
        self.assertNotIn("worker_token", self.meta)
        self.assert_prior_active()

    def test_partial_and_missing_model_cannot_report_complete_analysis(self):
        self.output.update(partial=True, partial_reasons=["BUDGET_EXCEEDED"])
        self.assertEqual(self.run_worker()["status"], "PARTIAL")
        self.assertEqual(self.manifest()["partial_reasons"], ["BUDGET_EXCEEDED"])
        # A fresh output prefix avoids deliberately conflicting immutable bytes.
        self.event["build_id"] = self.meta["build_id"] = "second-build"
        self.meta.update(status="BUILDING", llm_enabled=True)
        self.output.update(partial=False, partial_reasons=[])
        self.assertEqual(self.run_worker()["status"], "PARTIAL")
        manifest = json.loads(self.s3.objects[f"groups/{GID}/versions/second-build/manifest.json"])
        self.assertIn("LLM_UNAVAILABLE", manifest["partial_reasons"])

    def test_disabled_model_is_not_passed_to_engine(self):
        converse = mock.Mock(side_effect=AssertionError("Disabled LLM"))
        self.assertEqual(self.run_worker(converse=converse)["status"], "READY")
        self.assertIsNone(self.engine.build_group.call_args.kwargs["converse"])
        converse.assert_not_called()

    def test_prior_cache_requires_exact_path_group_and_version(self):
        key = self.meta["active_manifest_key"]
        manifest = {"group_id": GID, "version": "old", "build_id": "old", "pair_cache": {"old-pair": {}}}
        original = copy.deepcopy(self.ddb.rows)
        for changes in ({}, {"group_id": "foreign"}, {"version": "wrong"}, {"build_id": "wrong"}):
            with self.subTest(changes=changes):
                self.ddb.rows = copy.deepcopy(original)
                self.s3.objects[key] = data({**manifest, **changes})
                self.run_worker()
                expected = None if changes else manifest
                self.assertEqual(self.engine.build_group.call_args.kwargs["previous_manifest"], expected)
        self.ddb.rows = copy.deepcopy(original)
        foreign = f"groups/grp_{'f' * 32}/versions/old/manifest.json"
        self.meta["active_manifest_key"] = foreign
        self.run_worker()
        self.assertNotIn(foreign, self.s3.reads)
        self.assertIsNone(self.engine.build_group.call_args.kwargs["previous_manifest"])

    def test_graph_header_stream_and_aggregate_byte_caps(self):
        key = "source-versions/source-a/v1/graph.json"
        original_cap = self.engine.MAX_GRAPH_BYTES
        for length in (None, len(self.s3.objects[key])):
            with self.subTest(length=length):
                self.meta["status"] = "BUILDING"
                self.engine.MAX_GRAPH_BYTES = 8
                self.s3.lengths[key] = length
                self.assertEqual(self.run_worker()["error"], "BYTE_LIMIT_EXCEEDED")
                self.assertTrue(all(body.closed for body in self.s3.bodies))
        self.engine.MAX_GRAPH_BYTES = original_cap
        self.add_source("source-b", public=True)
        self.engine.MAX_GRAPH_BYTES = 2 * len(self.s3.objects[key]) - 1
        self.meta["status"] = "BUILDING"
        self.assertEqual(self.run_worker()["error"], "BYTE_LIMIT_EXCEEDED")
        self.engine.build_group.assert_not_called()
        self.assertFalse(self.s3.writes)

    def test_text_file_snapshot_and_output_byte_caps(self):
        for limit in ("MAX_TEXT_BYTES", "MAX_FILE_BYTES", "MAX_SNAPSHOT_BYTES"):
            with self.subTest(limit=limit):
                self.meta["status"] = "BUILDING"
                original = getattr(self.engine, limit)
                setattr(self.engine, limit, 1)
                self.assertEqual(self.run_worker()["error"], "BYTE_LIMIT_EXCEEDED")
                setattr(self.engine, limit, original)
        self.add_source("source-b", files={"README.md": "한" * 10})
        self.engine.MAX_TEXT_BYTES = 35
        self.meta["status"] = "BUILDING"
        self.assertEqual(self.run_worker()["error"], "BYTE_LIMIT_EXCEEDED")
        self.engine.MAX_TEXT_BYTES = 32 * 1024 * 1024
        self.engine.MAX_GRAPH_BYTES = 500
        self.output["graph"]["nodes"][0]["label"] = "x" * 1000
        self.meta["status"] = "BUILDING"
        self.assertEqual(self.run_worker()["error"], "BYTE_LIMIT_EXCEEDED")
        self.assertFalse(self.s3.writes)

    def test_truncated_or_duplicate_key_json_artifacts_are_rejected(self):
        key = "source-versions/source-a/v1/manifest.json"
        self.s3.lengths[key] = len(self.s3.objects[key]) + 1
        self.assertEqual(self.run_worker()["error"], "ARTIFACT_TRUNCATED")
        self.meta["status"] = "BUILDING"
        self.s3.lengths.clear()
        self.s3.objects[key] = b'{"repo_id":"source-a","repo_id":"source-b"}'
        self.assertEqual(self.run_worker()["error"], "INVALID_ARTIFACT")
        self.engine.build_group.assert_not_called()

    def test_engine_error_codes_only_are_recorded(self):
        for code in ("UNSAFE_SNAPSHOT", "private path /token"):
            with self.subTest(code=code):
                self.meta["status"] = "BUILDING"
                self.engine.parse_snapshot.side_effect = self.engine.EngineError(code, "private file content")
                result = self.run_worker()
                expected = code if code == "UNSAFE_SNAPSHOT" else "BUILD_FAILED"
                self.assertEqual(result["error"], expected)
                self.assertEqual(self.meta["last_error"], expected)
        self.engine.build_group.assert_not_called()

    def test_identical_immutable_replay_succeeds_and_different_bytes_fail(self):
        before = copy.deepcopy(self.meta)
        self.assertEqual(self.run_worker()["status"], "READY")
        objects = copy.deepcopy(self.s3.objects)
        self.ddb.rows[self.ddb.address(PLATFORM, self.meta_key)] = copy.deepcopy(before)
        self.assertEqual(self.run_worker()["status"], "READY")
        self.assertEqual(self.s3.objects, objects)
        key = f"groups/{GID}/versions/{BID}/graph.json"
        for raw in (b"different", b"x" * 1000):
            with self.subTest(size=len(raw)):
                self.ddb.rows[self.ddb.address(PLATFORM, self.meta_key)] = copy.deepcopy(before)
                self.s3.objects[key] = raw
                self.assertEqual(self.run_worker()["error"], "IMMUTABLE_CONFLICT")
                self.assertEqual(self.s3.objects[key], raw)
                self.assert_prior_active()

    def test_live_lease_and_missing_expiry_block_duplicate_before_reads(self):
        for expiry in (NOW + 960, NOW, None):
            with self.subTest(expiry=expiry):
                self.meta["worker_token"] = "other-worker"
                if expiry is None:
                    self.meta.pop("worker_lease_expires_at", None)
                else:
                    self.meta["worker_lease_expires_at"] = expiry
                result = self.run_worker()
                self.assertEqual(result["status"], "BUSY")
                self.assertTrue(result["ignored"])
                self.assertEqual(self.meta["worker_token"], "other-worker")
        self.assertFalse(self.s3.reads)
        self.engine.build_group.assert_not_called()

    def test_inflight_duplicate_cannot_double_model_cost(self):
        duplicates = []
        def build(**kwargs):
            self.assertGreaterEqual(self.meta["worker_lease_expires_at"], NOW + 960)
            self.assertTrue(self.meta["worker_token"])
            reads = len(self.s3.reads)
            duplicates.append(self.run_worker(converse=mock.Mock()))
            self.assertEqual(len(self.s3.reads), reads)
            return copy.deepcopy(self.output)
        self.meta["llm_enabled"] = True
        self.engine.build_group.side_effect = build
        self.assertEqual(self.run_worker(converse=mock.Mock())["status"], "READY")
        self.assertEqual(duplicates[0]["status"], "BUSY")
        self.engine.build_group.assert_called_once()

    def test_expired_lease_reclaimed_and_completed_duplicate_skipped(self):
        self.meta.update(worker_token="expired", worker_lease_expires_at=NOW - 1)
        self.assertEqual(self.run_worker()["status"], "READY")
        lease = decoded(self.ddb.updates[0]["ExpressionAttributeValues"])
        self.assertNotEqual(lease[":token"], "expired")
        self.assertEqual(lease[":expiry"], NOW + 960)
        self.assertNotIn("worker_lease_expires_at", self.meta)
        reads = len(self.s3.reads)
        self.assertEqual(self.run_worker()["status"], "STALE")
        self.assertEqual(len(self.s3.reads), reads)
        self.engine.build_group.assert_called_once()

    def test_failure_and_publication_cannot_overwrite_replacement_worker(self):
        before = copy.deepcopy(self.ddb.rows)
        def fail(**kwargs):
            self.meta.update(worker_token="replacement", worker_lease_expires_at=NOW + 2000)
            raise RuntimeError("private")
        self.engine.build_group.side_effect = fail
        self.assertEqual(self.run_worker()["status"], "STALE")
        self.assertEqual(self.meta["status"], "BUILDING")
        self.assertEqual(self.meta["worker_token"], "replacement")
        self.ddb.rows = copy.deepcopy(before)
        self.engine.build_group.side_effect = lambda **kwargs: copy.deepcopy(self.output)
        def race():
            self.ddb.before_transaction = None
            self.meta["worker_token"] = "replacement"
        self.ddb.before_transaction = race
        self.assertEqual(self.run_worker()["status"], "STALE")
        self.assertEqual(self.meta["worker_token"], "replacement")
        self.assertEqual(self.meta["status"], "BUILDING")
        self.assert_prior_active()

    def test_model_attempt_guard_blocks_actual_second_call_after_supersession(self):
        self.meta["llm_enabled"] = True
        def actual_model(**kwargs):
            self.meta.update(build_id="new-build", worker_token="new-worker")
            return {}
        converse = mock.Mock(side_effect=actual_model)
        def build(**kwargs):
            kwargs["converse"](messages=[], modelId=kwargs["model_id"])
            # The real engine can retry a guard failure; none may reach Bedrock.
            for _ in range(2):
                with self.assertRaises(self.worker._Stale):
                    kwargs["converse"](messages=[], modelId=kwargs["model_id"])
            return copy.deepcopy(self.output)
        self.engine.build_group.side_effect = build
        self.assertEqual(self.run_worker(converse=converse)["status"], "STALE")
        converse.assert_called_once()
        self.assertFalse(self.s3.writes)

    def test_model_attempt_guard_blocks_revoke_without_second_model_call(self):
        self.meta["llm_enabled"] = True
        def actual_model(**kwargs):
            self.ddb.delete(PLATFORM, self.source_grant())
            return {}
        converse = mock.Mock(side_effect=actual_model)
        def build(**kwargs):
            kwargs["converse"](messages=[])
            with self.assertRaises(self.access.GroupError):
                kwargs["converse"](messages=[])
            return copy.deepcopy(self.output)
        self.engine.build_group.side_effect = build
        self.assertEqual(self.run_worker(converse=converse)["error"], "ACCESS_DENIED")
        converse.assert_called_once()
        self.assertFalse(self.s3.writes)

    def test_deadline_before_read_after_parse_and_during_upload_fails_owned_claim(self):
        self.context.get_remaining_time_in_millis.return_value = 5_000
        self.assertEqual(self.run_worker()["error"], "DEADLINE_EXCEEDED")
        self.assertFalse(self.s3.reads)
        self.meta["status"] = "BUILDING"
        self.context.get_remaining_time_in_millis.return_value = 120_000
        def parse(raw):
            self.context.get_remaining_time_in_millis.return_value = 5_000
            return json.loads(raw)
        self.engine.parse_snapshot.side_effect = parse
        self.assertEqual(self.run_worker()["error"], "DEADLINE_EXCEEDED")
        self.engine.build_group.assert_not_called()
        self.meta["status"] = "BUILDING"
        self.context.get_remaining_time_in_millis.return_value = 120_000
        self.engine.parse_snapshot.side_effect = lambda raw: json.loads(raw)
        self.s3.after_put = lambda key: setattr(
            self.context.get_remaining_time_in_millis, "return_value", 5_000,
        )
        self.assertEqual(self.run_worker()["error"], "DEADLINE_EXCEEDED")
        self.assertEqual(len(self.s3.writes), 1)
        self.assertNotIn("worker_token", self.meta)
        self.assert_prior_active()

    def test_handler_creates_only_lazy_low_level_clients_for_disabled_llm(self):
        clients = {"dynamodb": self.ddb, "s3": self.s3}
        with mock.patch.object(boto3, "client", side_effect=lambda name, **kwargs: clients[name]) as factory:
            with mock.patch.dict("os.environ", {
                "PLATFORM_TABLE": PLATFORM, "REGISTRY_TABLE": REGISTRY, "GRAPH_BUCKET": BUCKET,
            }):
                self.assertEqual(self.worker.handler(self.event, self.context)["status"], "READY")
        self.assertEqual([call.args[0] for call in factory.call_args_list], ["dynamodb", "s3"])
        self.assertIsNone(self.engine.build_group.call_args.kwargs["converse"])

    def test_handler_bedrock_retry_and_remaining_time_configuration(self):
        self.meta["llm_enabled"] = True
        self.context.get_remaining_time_in_millis.return_value = 40_000
        model = mock.Mock()
        clients = {"dynamodb": self.ddb, "s3": self.s3, "bedrock-runtime": model}
        def build(**kwargs):
            kwargs["converse"](modelId=kwargs["model_id"], messages=[])
            return copy.deepcopy(self.output)
        self.engine.build_group.side_effect = build
        with mock.patch.object(boto3, "client", side_effect=lambda name, **kwargs: clients[name]) as factory:
            with mock.patch.dict("os.environ", {
                "PLATFORM_TABLE": PLATFORM, "REGISTRY_TABLE": REGISTRY, "GRAPH_BUCKET": BUCKET,
            }):
                self.assertEqual(self.worker.handler(self.event, self.context)["status"], "READY")
        config = factory.call_args.kwargs["config"]
        self.assertEqual(config.retries["total_max_attempts"], 1)
        self.assertEqual(config.read_timeout, 30)
        self.assertLessEqual(config.connect_timeout, 3)
        model.converse.assert_called_once()

    def test_reconcile_dispatch_uses_parent_helper_without_worker_clients(self):
        helper = types.ModuleType("reconcile")
        helper.reconcile = mock.Mock(return_value={"reconciled": True})
        event = {"reconcile": True}
        with mock.patch.dict(sys.modules, {"reconcile": helper}):
            self.assertEqual(self.worker.handler(event, self.context), {"reconciled": True})
        helper.reconcile.assert_called_once_with(event, self.context)

    def test_actual_engine_tar_pipeline_preserves_utf8_originals(self):
        self.worker.engine = load("lambdas/group_worker/engine.py", "real_worker_engine")
        raw = "REQ-001\r\n한글\n".encode()
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w:gz") as tar:
            info = tarfile.TarInfo("./README.md")
            info.size = len(raw)
            tar.addfile(info, io.BytesIO(raw))
        snapshot = archive.getvalue()
        self.s3.objects["source-versions/source-a/v1/src.tar.gz"] = snapshot
        self.rewrite_source_manifest(snapshot_sha256=digest(snapshot))
        self.assertEqual(self.run_worker()["status"], "READY")
        entry = self.manifest()["files"]["source-a"]["README.md"]
        self.assertEqual(self.s3.objects[entry["key"]], raw)
        graph = json.loads(self.s3.objects[self.manifest()["graph_key"]])
        self.assertTrue(graph["directed"])
        self.assertTrue(graph["multigraph"])
        self.assertEqual(graph["nodes"][0]["source_version"], "v1")


if __name__ == "__main__":
    unittest.main()
