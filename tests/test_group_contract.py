"""Cross-module offline conformance: groups API -> worker -> group reader.

These tests reuse the individual suites' stateful service fakes and four-source
fixture, but load the real production modules together. They exercise lifecycle
handoffs rather than repeat extractor, archive, or reader validation tests.

Run: .venv/bin/python -B -m unittest discover -s tests -p test_group_contract.py
"""

import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import socket
import sys
import types
import unittest
from unittest import mock

import boto3


ROOT = Path(__file__).resolve().parents[1]


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


api_fakes = load("tests/test_groups_api.py", "contract_api_fakes")
worker_fakes = load("tests/test_group_worker.py", "contract_worker_fakes")
fixtures = load("tests/test_group_engine.py", "contract_engine_fixtures")
PLATFORM, REGISTRY = api_fakes.PLATFORM, api_fakes.REGISTRY
BUCKET, OWNER, NOW = worker_fakes.BUCKET, api_fakes.OWNER, api_fakes.NOW


class ContractS3(worker_fakes.FakeS3):
    """Add the API's read-only preflight to the worker's immutable object fake."""

    def __init__(self):
        super().__init__()
        self.heads = []

    def head_object(self, *, Bucket, Key):
        assert Bucket == BUCKET
        self.heads.append(Key)
        if Key not in self.objects:
            raise worker_fakes.AwsError("NoSuchKey")
        return {"ContentLength": len(self.objects[Key]), "ETag": '"offline"'}


class GroupContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.access = load("lambdas/shared/python/group_access.py", "contract_group_access")
        cls.engine = load("lambdas/group_worker/engine.py", "contract_group_engine")
        with mock.patch.dict(sys.modules, {"group_access": cls.access, "engine": cls.engine}), \
                mock.patch.object(boto3, "client", side_effect=AssertionError("No live clients")), \
                mock.patch.object(boto3, "resource", side_effect=AssertionError("No live resources")), \
                mock.patch.object(socket.socket, "connect", side_effect=AssertionError("No network")):
            cls.api = load("lambdas/platform_api/groups_api.py", "contract_groups_api")
            cls.worker = load("lambdas/group_worker/handler.py", "contract_group_worker")
            cls.query = load("lambdas/shared/python/group_query.py", "contract_group_query")

    def setUp(self):
        for target, attribute in (
            (boto3, "client"), (boto3, "resource"),
            (boto3.session.Session, "client"), (boto3.session.Session, "resource"),
            (socket.socket, "connect"), (socket, "create_connection"),
        ):
            patcher = mock.patch.object(target, attribute, side_effect=AssertionError("Offline contract only"))
            patcher.start()
            self.addCleanup(patcher.stop)
        clock = mock.patch.object(self.api.time, "time", return_value=NOW)
        clock.start()
        self.addCleanup(clock.stop)
        self.query._CACHE.clear()
        self.addCleanup(self.query._CACHE.clear)
        self.ddb = api_fakes.MemoryDynamo()
        self.s3 = ContractS3()
        self.lambda_client = api_fakes.MemoryLambda()
        self.context = types.SimpleNamespace(get_remaining_time_in_millis=lambda: 120_000)
        self.sources = fixtures.project()
        self.texts = {s["source_id"]: copy.deepcopy(s["files"]) for s in self.sources}
        self.dependencies = {
            "platform": mock.Mock(spec=()), "registry": mock.Mock(spec=()),
            "ddb": self.ddb, "s3": self.s3, "lambda_client": self.lambda_client,
            "worker_fn": "offline-contract-worker", "platform_table": PLATFORM,
            "registry_table": REGISTRY, "bucket": BUCKET,
            "mcp_base": "https://offline.example.test", "cognito": api_fakes.MemoryCognito(),
            "user_pool_id": "offline-pool",
        }
        self.gid = None
        for item in self.sources:
            sid = item["source_id"]
            self.ddb.seed(REGISTRY, {
                "repo_id": sid, "enabled": "1", "graph_scope": "private",
                "description": f"Common {sid} context", "description_version": 1,
                "active_source_version": "v1",
            })
            self.ddb.seed(PLATFORM, {
                "pk": f"USER#{OWNER}", "sk": f"REPO#{sid}",
                "personal_description": "Subscriber-private context must not enter group artifacts.",
            })
            self.publish_source(sid, "v1")

    def tearDown(self):
        for operation, request in self.ddb.calls:
            if operation in ("get_item", "query") and not request.get("IndexName"):
                self.assertIs(request.get("ConsistentRead"), True)
        self.assertTrue(all(body.closed for body in self.s3.bodies))

    def publish_source(self, sid, version, *, text=None):
        """Seed only immutable publisher outputs; never fabricate a group result."""
        original = next(source for source in self.sources if source["source_id"] == sid)
        if text is not None:
            path = next(iter(self.texts[sid]))
            self.texts[sid][path] = text
        graph = copy.deepcopy(original["graph"])
        for node in graph["nodes"]:
            path = node.get("source_file")
            if path in self.texts[sid]:
                node["line_end"] = len(self.texts[sid][path].splitlines())
        graph_bytes = json.dumps(graph, sort_keys=True, ensure_ascii=False).encode()
        archive = fixtures.tar_bytes([(path, text.encode()) for path, text in self.texts[sid].items()])
        prefix = f"source-versions/{sid}/{version}/"
        manifest = {
            "schema_version": 1, "repo_id": sid, "version": version,
            "graph_key": prefix + "graph.json", "snapshot_key": prefix + "src.tar.gz",
            "graph_sha256": hashlib.sha256(graph_bytes).hexdigest(),
            "snapshot_sha256": hashlib.sha256(archive).hexdigest(),
            "build_id": f"source-{sid}-{version}", "created_at": "2026-09-15T00:00:00Z",
        }
        self.s3.objects.update({
            prefix + "graph.json": graph_bytes,
            prefix + "src.tar.gz": archive,
            prefix + "manifest.json": json.dumps(manifest).encode(),
        })
        row = self.ddb.row(REGISTRY, repo_id=sid)
        row["active_source_version"] = version
        self.ddb.seed(REGISTRY, row)

    def api_request(self, method, suffix="", body=None):
        path = "/groups" + (f"/{self.gid}" if self.gid else "") + suffix
        return self.api.handle(
            {"body": json.dumps(body) if body is not None else ""},
            {"sub": OWNER}, method, path, **self.dependencies,
        )

    def create_group(self, *, llm_enabled=False):
        status, result = self.api_request("POST", body={
            "idempotency_key": "contract-create-0001",
            "name": "Pension application", "description": "Account planning, implementation, and QA",
            "llm_enabled": llm_enabled, "model": self.engine.DEFAULT_MODEL,
            "sources": [
                {"source_id": source["source_id"], "role": source["role"],
                 "description": f"Member {source['source_id']} context"}
                for source in self.sources
            ],
        })
        self.assertEqual(status, 201, result)
        self.gid = result["group"]["group_id"]
        self.assertEqual(result["group"]["status"], "DRAFT")
        self.assertFalse(result["group"]["data_ready"])
        return result["group"]

    def meta(self):
        return self.ddb.row(PLATFORM, pk=f"GROUP#{self.gid}", sk="META")

    def ledger(self, bid):
        return self.ddb.row(PLATFORM, pk=f"GROUP#{self.gid}", sk=f"BUILD#{bid}")

    def view(self):
        status, result = self.api_request("GET")
        self.assertEqual(status, 200, result)
        return result["group"]

    def enqueue(self, token):
        before = len(self.lambda_client.calls)
        status, body = self.api_request("POST", "/rebuild", {
            "expected_revision": int(self.meta()["revision"]), "idempotency_key": token,
        })
        self.assertEqual(status, 202, body)
        self.assertEqual(body["status"], "QUEUED")
        self.assertEqual(len(self.lambda_client.calls), before + 1)
        invocation = self.lambda_client.calls[-1]
        self.assertEqual(invocation["FunctionName"], "offline-contract-worker")
        self.assertEqual(invocation["InvocationType"], "Event")
        event = json.loads(invocation["Payload"])
        self.assertEqual(event, {
            "group_id": self.gid, "build_id": body["build_id"],
            "revision": int(self.meta()["revision"]), "requested_by": OWNER,
        })
        ledger = self.ledger(body["build_id"])
        self.assertEqual(ledger["source_versions"], {
            s["source_id"]: self.ddb.row(REGISTRY, repo_id=s["source_id"])["active_source_version"]
            for s in self.sources
        })
        self.assertTrue(ledger["dispatched"])
        self.assertEqual(ledger["requested_by"], OWNER)
        return event

    def run_worker(self, event, *, model=None):
        # Do not reconstruct or normalize the API's actual invocation payload.
        return self.worker.run_build(
            event, self.context, ddb=self.ddb, s3=self.s3, platform_table=PLATFORM,
            registry_table=REGISTRY, bucket=BUCKET, converse=model,
        )

    def manifest(self, bid):
        return json.loads(self.s3.objects[f"groups/{self.gid}/versions/{bid}/manifest.json"])

    @property
    def query_args(self):
        return self.ddb, PLATFORM, REGISTRY, self.s3, BUCKET, OWNER, self.gid

    def rpc(self, name, **arguments):
        return self.query.handle_rpc(*self.query_args, {
            "jsonrpc": "2.0", "id": "contract", "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        })["result"]

    def replay(self, token, revision, expected_status):
        count = len(self.lambda_client.calls)
        status, body = self.api_request("POST", "/rebuild", {
            "expected_revision": revision, "idempotency_key": token,
        })
        self.assertEqual(status, 202, body)
        self.assertTrue(body["replayed"])
        self.assertEqual(body["status"], expected_status)
        self.assertEqual(len(self.lambda_client.calls), count)
        return body

    def assert_terminal_handoff(self, event, status):
        meta, ledger, manifest = self.meta(), self.ledger(event["build_id"]), self.manifest(event["build_id"])
        view = self.view()
        for item in (meta, ledger, view):
            self.assertEqual(item["status"], status)
        self.assertEqual(meta["active_version"], manifest["version"])
        self.assertEqual(meta["active_revision"], manifest["revision"])
        self.assertEqual(meta["active_source_versions"], ledger["source_versions"])
        self.assertEqual(meta["active_source_versions"], manifest["source_versions"])
        self.assertEqual(meta["active_source_descriptions"], manifest["source_descriptions"])
        self.assertEqual(meta["active_source_descriptions"], ledger["source_description_versions"])
        self.assertEqual(meta["stats"], manifest["stats"])
        self.assertEqual(ledger["stats"], manifest["stats"])
        self.assertEqual(meta["usage"], manifest["usage"])
        self.assertEqual(ledger["usage"], manifest["usage"])
        self.assertEqual(meta["build_started_at"], 0)
        self.assertNotIn("worker_token", meta)
        self.assertNotIn("worker_lease_expires_at", meta)
        self.assertEqual(ledger["ended_at"], NOW)
        self.assertTrue(view["data_ready"])
        self.assertFalse(view["stale"])
        self.assertEqual(view["usage"]["input_tokens"], manifest["usage"]["input_tokens"])
        self.assertEqual(view["usage"]["output_tokens"], manifest["usage"]["output_tokens"])
        self.assertEqual(view["usage"]["unknown_usage_attempts"], manifest["usage"]["unknown_usage_attempts"])
        self.assertTrue(all(type(value) in (int, float) for value in view["usage"].values()))
        self.assertNotIn("pair_cache", view)
        self.assertNotIn("active_graph_key", view)
        self.assertNotIn("model_id", view["usage"])
        self.assertNotIn("prompt", view["usage"])
        json.dumps(view, allow_nan=False)
        graph_bytes = self.s3.objects[manifest["graph_key"]]
        self.assertEqual(manifest["graph_sha256"], hashlib.sha256(graph_bytes).hexdigest())
        self.assertNotIn("Subscriber-private context", json.dumps(manifest))
        return manifest

    def test_four_source_api_dispatch_worker_completion_and_reader_contract(self):
        self.create_group(llm_enabled=True)
        token = "contract-build-success"
        event = self.enqueue(token)
        model = fixtures.Model()
        self.assertEqual(self.run_worker(event, model=model)["status"], "READY")
        manifest = self.assert_terminal_handoff(event, "READY")
        self.assertGreater(len(model.calls), 0)
        self.assertGreater(self.view()["usage"]["input_tokens"], 0)
        self.replay(token, event["revision"], "READY")

        page = self.query.get_graph_page(*self.query_args, limit=2)
        self.assertEqual(page["version"], event["build_id"])
        self.assertEqual(page["stats"], manifest["stats"])
        second = self.query.get_graph_page(
            *self.query_args, offset=page["next_offset"], limit=500, group_version=page["version"],
        )
        nodes = page["nodes"] + second["nodes"]
        self.assertEqual({node["source_id"] for node in nodes}, {s["source_id"] for s in self.sources})
        self.assertEqual(len({node["id"] for node in nodes}), len(nodes))
        links = self.query.get_graph_page(*self.query_args, kind="links", group_version=page["version"])
        self.assertTrue(any(link["evidence_kind"] == "INFERRED" for link in links["links"]))
        result = self.rpc("query_graph", query="backend", group_version=page["version"])
        self.assertFalse(result["isError"], result)
        self.assertEqual(result["structuredContent"]["status"], "READY")
        path, text = next(iter(self.texts["backend"].items()))
        result = self.rpc("read_source", source_id="backend", file=path, group_version=page["version"])
        self.assertFalse(result["isError"], result)
        self.assertEqual(result["structuredContent"]["text"], text)
        self.assertEqual(result["structuredContent"]["source_version"], manifest["source_versions"]["backend"])
        self.assertEqual(result["structuredContent"]["sha256"], manifest["files"]["backend"][path]["sha256"])

    def test_versions_descriptions_and_revocation_propagate_across_all_three_surfaces(self):
        self.create_group()
        first = self.enqueue("contract-version-one")
        self.assertEqual(self.run_worker(first)["status"], "READY")
        self.query.get_graph_page(*self.query_args)  # Warm the reader with real worker output.
        path, text = next(iter(self.texts["backend"].items()))
        self.publish_source("backend", "v2", text=text + "# Only in backend v2\n")
        self.assertFalse(self.view()["data_ready"])
        reads = len(self.s3.reads)
        with self.assertRaises(self.access.GroupError) as error:
            self.query.get_graph_page(*self.query_args)
        self.assertEqual(error.exception.status, 409)
        self.assertEqual(len(self.s3.reads), reads)
        denied = self.rpc("read_source", source_id="backend", file=path)
        self.assertTrue(denied["isError"])
        self.assertEqual(denied["structuredContent"]["status"], 409)

        second = self.enqueue("contract-version-two")  # Same clock: terminal build reset the API lease.
        self.assertEqual(self.run_worker(second)["status"], "READY")
        manifest = self.assert_terminal_handoff(second, "READY")
        self.assertEqual(manifest["source_versions"], {
            source["source_id"]: "v2" if source["source_id"] == "backend" else "v1"
            for source in self.sources
        })
        fresh = self.query.get_source(
            *self.query_args, source_id="backend", file=path, group_version=second["build_id"],
        )
        self.assertIn("Only in backend v2", fresh["text"])
        self.assertEqual(fresh["source_version"], "v2")
        old = self.rpc("read_source", source_id="backend", file=path, group_version=first["build_id"])
        self.assertTrue(old["isError"])
        self.assertEqual(old["structuredContent"]["status"], 409)

        row = self.ddb.row(REGISTRY, repo_id="qa")
        row.update(description="Changed common QA context", description_version=2)
        self.ddb.seed(REGISTRY, row)
        self.assertFalse(self.view()["data_ready"])
        with self.assertRaises(self.access.GroupError) as error:
            self.query.get_graph_page(*self.query_args)
        self.assertEqual(error.exception.status, 409)
        third = self.enqueue("contract-description-two")
        self.assertEqual(self.run_worker(third)["status"], "READY")
        manifest = self.assert_terminal_handoff(third, "READY")
        self.assertEqual(manifest["source_descriptions"]["qa"], 2)
        self.query.get_graph_page(*self.query_args)

        self.ddb.remove(PLATFORM, pk=f"USER#{OWNER}", sk="REPO#qa")
        recovery = self.view()
        self.assertTrue(recovery["access_recovery"])
        self.assertFalse(recovery["data_ready"])
        self.assertNotIn("usage", recovery)
        self.assertNotIn("active_version", recovery)
        reads = len(self.s3.reads)
        with self.assertRaises(self.access.GroupError) as error:
            self.query.get_graph_page(*self.query_args)
        self.assertEqual(error.exception.status, 403)
        denied = self.rpc("read_source", source_id="backend", file=path)
        self.assertTrue(denied["isError"])
        self.assertEqual(denied["structuredContent"]["status"], 403)
        self.assertNotIn("version", denied["structuredContent"])
        self.assertEqual(len(self.s3.reads), reads)
        status, _ = self.api_request("POST", "/rebuild", {
            "expected_revision": third["revision"], "idempotency_key": "contract-revoked-source",
        })
        self.assertEqual(status, 403)

    def test_source_changes_while_queued_fail_the_confirmed_build_not_its_dispatch(self):
        self.create_group()
        first = self.enqueue("contract-prior-active")
        self.assertEqual(self.run_worker(first)["status"], "READY")
        previous_version = self.meta()["active_version"]
        token = "contract-changed-queued-input"
        event = self.enqueue(token)
        self.publish_source("backend", "v2")
        writes = len(self.s3.writes)
        outcome = self.run_worker(event)
        self.assertEqual(outcome["status"], "FAILED", outcome)
        self.assertEqual(outcome["error"], "SOURCE_VERSION_CHANGED")
        self.assertEqual(len(self.s3.writes), writes)
        self.assertEqual(self.meta()["active_version"], previous_version)
        self.assertEqual(self.meta()["build_started_at"], 0)
        ledger = self.ledger(event["build_id"])
        self.assertEqual(ledger["status"], "FAILED")
        self.assertTrue(ledger["dispatched"])
        self.assertEqual(ledger["ended_at"], NOW)
        self.assertEqual(ledger["source_versions"]["backend"], "v1")
        view = self.view()
        self.assertEqual(view["status"], "FAILED")
        self.assertEqual(view["last_error_code"], "SOURCE_VERSION_CHANGED")
        self.assertFalse(view["data_ready"])
        replay = self.replay(token, event["revision"], "FAILED")
        self.assertEqual(replay["error_code"], "SOURCE_VERSION_CHANGED")
        replacement = self.enqueue("contract-retry-current-input")
        self.assertEqual(self.run_worker(replacement)["status"], "READY")
        self.assert_terminal_handoff(replacement, "READY")
        self.assertNotIn(
            "dispatch could not", replay.get("error", "").lower(),
            "A confirmed worker failure must not be presented as an ambiguous dispatch failure.",
        )

    def test_partial_model_result_remains_partial_in_ledger_api_replay_and_tools(self):
        self.create_group(llm_enabled=True)
        token = "contract-partial-model"
        event = self.enqueue(token)
        model = fixtures.Model("error")
        self.assertEqual(self.run_worker(event, model=model)["status"], "PARTIAL")
        manifest = self.assert_terminal_handoff(event, "PARTIAL")
        self.assertTrue(manifest["partial"])
        self.assertIn("MODEL_ERROR", manifest["partial_reasons"])
        self.assertGreater(len(model.calls), 0)
        self.assertEqual(manifest["usage"]["model_calls"], len(model.calls))
        self.assertEqual(manifest["usage"]["unknown_usage_attempts"], len(model.calls))
        self.assertIn("MODEL_USAGE_UNKNOWN", manifest["partial_reasons"])
        self.replay(token, event["revision"], "PARTIAL")
        page = self.query.get_graph_page(*self.query_args)
        self.assertEqual(page["status"], "PARTIAL")
        self.assertTrue(page["partial"])
        self.assertIn("MODEL_ERROR", page["partial_reasons"])
        path = next(iter(self.texts["planning"]))
        source = self.rpc(
            "read_source", source_id="planning", file=path, group_version=event["build_id"],
        )
        self.assertFalse(source["isError"], source)
        self.assertTrue(source["structuredContent"]["partial"])
        self.assertEqual(source["structuredContent"]["status"], "PARTIAL")
        self.assertNotIn("private data and credentials", json.dumps(self.view()))
        self.assertNotIn("private data and credentials", json.dumps(manifest))


if __name__ == "__main__":
    unittest.main()
