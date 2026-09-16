"""Offline contracts for the shared group reader, using the real ACL helper.

Run: .venv/bin/python -B -m unittest discover -s tests -p test_group_query.py
"""

import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import socket
import sys
import unittest
from unittest import mock

from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import ClientError


ROOT = Path(__file__).resolve().parents[1]
GID = "grp_" + "a" * 32
PLATFORM, REGISTRY, BUCKET, SUB = "platform", "registry", "private-graphs", "reader"
VERSION = "build-1"
SERIALIZER, DESERIALIZER = TypeSerializer(), TypeDeserializer()


def digest(value):
    return hashlib.sha256(value.encode("utf-8") if isinstance(value, str) else value).hexdigest()


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")


def load_query():
    directory = ROOT / "lambdas/shared/python"
    access_spec = importlib.util.spec_from_file_location("group_access", directory / "group_access.py")
    access = importlib.util.module_from_spec(access_spec)
    access_spec.loader.exec_module(access)
    spec = importlib.util.spec_from_file_location("offline_group_query", directory / "group_query.py")
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(sys.modules, {"group_access": access}):
        spec.loader.exec_module(module)
    return module


class MemoryDynamo:
    """Every reader request must use the low-level consistent-read boundary."""

    def __init__(self):
        self.rows = {}
        self.calls = []

    def put(self, table, key, value):
        self.rows[table, tuple(sorted(key.items()))] = copy.deepcopy({**key, **value})

    def remove(self, table, key):
        self.rows.pop((table, tuple(sorted(key.items()))), None)

    def get_item(self, **kwargs):
        if kwargs.get("ConsistentRead") is not True:
            raise AssertionError("An ACL/metadata read was not strongly consistent")
        self.calls.append(copy.deepcopy(kwargs))
        key = {field: DESERIALIZER.deserialize(value) for field, value in kwargs["Key"].items()}
        row = self.rows.get((kwargs["TableName"], tuple(sorted(key.items()))))
        if row is None:
            return {}
        return {"Item": {field: SERIALIZER.serialize(value) for field, value in copy.deepcopy(row).items()}}


class BoundedBody(io.BytesIO):
    def read(self, size=-1):
        if size < 0:
            raise AssertionError("Unbounded artifact read")
        return super().read(size)


class MemoryS3:
    def __init__(self):
        self.objects = {}
        self.calls = []
        self.after_get = None
        self.report_length = True
        self.bodies = []

    def get_object(self, *, Bucket, Key):
        if Bucket != BUCKET:
            raise AssertionError("Unexpected bucket")
        self.calls.append(Key)
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey", "Message": "Missing offline object"}}, "GetObject")
        body = BoundedBody(self.objects[Key])
        self.bodies.append(body)
        result = {"Body": body}
        if self.report_length:
            result["ContentLength"] = len(self.objects[Key])
        if self.after_get:
            self.after_get(Key)
        return result


class GroupQueryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_query()

    def setUp(self):
        self.module._CACHE.clear()
        blocker = mock.patch.object(socket, "create_connection", side_effect=AssertionError("No network"))
        blocker.start()
        self.addCleanup(blocker.stop)
        self.ddb, self.s3 = MemoryDynamo(), MemoryS3()
        self.sources = [
            {"source_id": "sourceA", "role": "backend", "description": "Order API"},
            {"source_id": "sourceB", "role": "frontend", "description": "Order UI"},
            {"source_id": "planning", "role": "planning", "description": "Requirements"},
            {"source_id": "qa", "role": "qa", "description": "QA acceptance tests"},
        ]
        self.versions = {source["source_id"]: source["source_id"] + "-v1" for source in self.sources}
        self.meta = {
            "group_id": GID, "revision": 1, "active_revision": 1,
            "active_version": VERSION, "active_source_versions": self.versions,
            "status": "READY", "sources": self.sources,
            "active_source_descriptions": {sid: 0 for sid in self.versions},
        }
        self.ddb.put(PLATFORM, {"pk": "USER#" + SUB, "sk": "GROUP#" + GID}, {"role": "viewer"})
        for sid, version in self.versions.items():
            self.ddb.put(REGISTRY, {"repo_id": sid}, {
                "enabled": "1", "graph_scope": "private", "active_source_version": version,
            })
            self.ddb.put(PLATFORM, {"pk": "USER#" + SUB, "sk": "REPO#" + sid}, {"role": "reader"})
        self.texts = {
            "sourceA": {
                "src/api.py": 'def submit():\r\n    # REQ-101 order\r\n    return "서울"\r\n',
                "README.md": "API README; shared original path\n",
            },
            "sourceB": {
                "src/client.ts": 'export function submit() {\n  return fetch("/orders"); // REQ-101\n}\n',
                "README.md": "UI README; shared original path\n",
            },
            "planning": {"plan.md": "# Plan\nREQ-101: accept orders\nAPI contract POST /orders\n"},
            "qa": {"qa.md": "# Tests\nTC-101: verifies REQ-101\n"},
        }

        def node(sid, original, label, file, type="function"):
            return {
                "id": sid + ":" + (original or "anchor"), "original_id": original,
                "label": label, "type": type, "source_id": sid,
                "source_version": self.versions[sid], "source_file": file,
            }

        self.graph = {
            "directed": True, "multigraph": True,
            "nodes": [
                node("sourceA", "duplicate", "submit", "src/api.py"),
                node("sourceB", "duplicate", "submit", "src/client.ts"),
                node("planning", "REQ-101", "REQ-101", "plan.md", "requirement"),
                node("qa", None, "TC-101", "qa.md", "source_anchor"),
            ],
            "links": [], "relations": [],
        }
        self.add_relation("plan-api", "planning:REQ-101", "sourceA:duplicate",
                          "shared_requirement", [("planning", "plan.md", 2, 2), ("sourceA", "src/api.py", 2, 2)])
        self.add_relation("api-ui", "sourceA:duplicate", "sourceB:duplicate",
                          "shared_contract", [("sourceA", "src/api.py", 1, 2), ("sourceB", "src/client.ts", 1, 2)])
        self.add_relation("qa-api", "qa:anchor", "sourceA:duplicate",
                          "shared_test_case", [("qa", "qa.md", 2, 2), ("sourceA", "src/api.py", 2, 2)])
        self.graph["links"].append({
            "id": "second-api-ui", "source": "sourceA:duplicate", "target": "sourceB:duplicate",
            "relation": "same_type_as", "evidence_kind": "INFERRED", "confidence_score": 0.5,
        })
        self.publish()

    def add_relation(self, eid, source, target, relation, quotes):
        evidence = []
        for sid, file, start, end in quotes:
            quote = "\n".join(self.texts[sid][file].splitlines()[start - 1:end])
            evidence.append({
                "source_id": sid, "source_version": self.versions[sid], "file": file,
                "line_start": start, "line_end": end, "quote": quote, "sha256": digest(quote),
            })
        edge = {
            "id": eid, "source": source, "target": target, "relation": relation,
            "evidence_kind": "EXTRACTED", "evidence": evidence,
            "method": "deterministic", "review_status": "unreviewed",
        }
        self.graph["links"].append(copy.deepcopy(edge))
        self.graph["relations"].append(edge)

    @property
    def prefix(self):
        return f"groups/{GID}/versions/{self.meta['active_version']}/"

    @property
    def args(self):
        return (self.ddb, PLATFORM, REGISTRY, self.s3, BUCKET, SUB, GID)

    def save_meta(self):
        self.ddb.put(PLATFORM, {"pk": "GROUP#" + GID, "sk": "META"}, self.meta)

    def save_manifest(self):
        self.s3.objects[self.prefix + "manifest.json"] = json_bytes(self.manifest)

    def publish(self):
        files = {}
        for sid, mapping in self.texts.items():
            files[sid] = {}
            for path, text in mapping.items():
                key = self.prefix + f"files/{sid}/{digest(path)}.txt"
                raw = text.encode("utf-8")
                self.s3.objects[key] = raw
                files[sid][path] = {"key": key, "sha256": digest(raw), "line_count": len(text.splitlines())}
        raw = json_bytes(self.graph)
        self.manifest = {
            "group_id": GID, "version": self.meta["active_version"],
            "build_id": self.meta["active_version"], "revision": self.meta["revision"],
            "source_versions": self.versions, "graph_key": self.prefix + "graph.json",
            "source_descriptions": self.meta["active_source_descriptions"],
            "graph_sha256": digest(raw), "files": files,
            "stats": {"nodes": len(self.graph["nodes"]), "links": len(self.graph["links"]),
                      "relations": len(self.graph["relations"])},
            "usage": {"input_tokens": 321, "output_tokens": 45},
            "limits": {"max_nodes": 50_000}, "partial": False, "partial_reasons": [],
        }
        self.s3.objects[self.prefix + "graph.json"] = raw
        self.meta.update(active_manifest_key=self.prefix + "manifest.json", active_graph_key=self.prefix + "graph.json")
        self.save_manifest()
        self.save_meta()

    def rpc(self, method="tools/call", params=None, **fields):
        return self.module.handle_rpc(*self.args, {
            "jsonrpc": "2.0", "id": 1, "method": method, **({"params": params} if params is not None else {}), **fields,
        })

    def call(self, tool_name, **args):
        return self.rpc(params={"name": tool_name, "arguments": args})

    def data(self, tool_name, **args):
        response = self.call(tool_name, **args)
        self.assertFalse(response["result"]["isError"], response)
        data = response["result"]["structuredContent"]
        self.assertEqual(json.loads(response["result"]["content"][0]["text"]), data)
        self.assertEqual(data["group_version"], self.meta["active_version"])
        # Results must survive the ordinary proxy JSON encoder (DDB uses Decimal).
        json.dumps(response)
        return data

    def assert_tool_error(self, tool_name, status=400, **args):
        response = self.call(tool_name, **args)
        self.assertEqual(response["jsonrpc"], "2.0")
        self.assertEqual(response["id"], 1)
        self.assertNotIn("error", response)
        self.assertTrue(response["result"]["isError"])
        self.assertEqual(response["result"]["structuredContent"]["status"], status, response)
        return response

    def assert_http_error(self, status, function, **kwargs):
        with self.assertRaises(self.module.GroupError) as raised:
            function(*self.args, **kwargs)
        self.assertEqual(raised.exception.status, status, str(raised.exception))

    def remove_source_access(self, sid="sourceB"):
        self.ddb.remove(PLATFORM, {"pk": "USER#" + SUB, "sk": "REPO#" + sid})

    def test_initialize_ping_and_tool_list_work_for_pending_groups(self):
        self.meta.update(status="PENDING", active_revision=0, active_version="")
        self.save_meta()
        response = self.rpc("initialize", {"protocolVersion": "2025-03-26"})
        self.assertEqual(response["result"]["protocolVersion"], "2025-03-26")
        self.assertEqual(self.rpc("ping")["result"], {})
        tools = self.rpc("tools/list")["result"]["tools"]
        self.assertEqual({tool["name"] for tool in tools}, {
            "query_graph", "get_node", "find_path", "get_neighbors", "get_relation", "read_source", "search_code",
        })
        self.assertTrue(all(not tool["inputSchema"]["additionalProperties"] for tool in tools))
        self.assert_tool_error("get_node", 409, node_id="sourceA:duplicate")
        self.assertEqual(self.s3.calls, [])

    def test_initialized_notification_never_executes_a_tool_or_returns_a_reply(self):
        for method in ("notifications/initialized", "tools/call", "unknown"):
            result = self.module.handle_rpc(*self.args, {
                "jsonrpc": "2.0", "method": method,
                "params": {"name": "get_node", "arguments": {"node_id": "sourceA:duplicate"}},
            })
            self.assertIsNone(result)
        self.assertEqual(self.s3.calls, [])

    def test_protocol_errors_use_jsonrpc_envelopes(self):
        for rpc in (None, [], {}, {"jsonrpc": "1.0", "method": "ping"}, {"jsonrpc": "2.0", "method": "ping", "id": True}):
            self.assertEqual(self.module.handle_rpc(*self.args, rpc)["error"]["code"], -32600)
        self.assertEqual(self.rpc("unknown")["error"]["code"], -32601)
        self.assertEqual(self.rpc("ping", [])["error"]["code"], -32602)
        self.assert_tool_error("unknown")
        self.assert_tool_error("get_node")

    def test_query_searches_actual_graph_metadata_and_returns_usage(self):
        data = self.data("query_graph", query="submit", max_results=1)
        self.assertEqual(data["total_matches"], 2)
        self.assertEqual(len(data["nodes"]), 1)
        self.assertTrue(data["links"])
        self.assertTrue(data["partial"])
        self.assertEqual(data["usage"], self.manifest["usage"])
        self.assertEqual(data["stats"], self.manifest["stats"])
        self.assertEqual(data["revision"], 1)

    def test_query_discovers_relation_terms_absent_from_node_labels(self):
        result = self.data("query_graph", query="shared_requirement")
        self.assertEqual({node["id"] for node in result["nodes"]},
                         {"planning:REQ-101", "sourceA:duplicate"})
        self.assertEqual([edge["id"] for edge in result["relations"]], ["plan-api"])
        self.assertEqual(result["total_relation_matches"], 1)

    def test_query_discovers_requirement_signal_without_named_requirement_node(self):
        # A source graph can have only generic file/headings while references
        # are present in the stored relation evidence.
        for node in self.graph["nodes"]:
            node["original_id"] = "original-" + node["source_id"]
            node["label"] = "generic"
        self.texts["planning"]["plan.md"] = "# Plan\nREQ-9099: accept orders\n"
        self.texts["sourceA"]["src/api.py"] = "def submit():\n    # REQ-9099 order\n"
        self.graph["links"] = []
        self.graph["relations"] = []
        self.add_relation("edge-only", "planning:REQ-101", "sourceA:duplicate",
                          "shared_requirement", [("planning", "plan.md", 2, 2),
                                                 ("sourceA", "src/api.py", 2, 2)])
        self.publish()
        result = self.data("query_graph", query="REQ-9099")
        self.assertEqual(len(result["nodes"]), 2)
        self.assertEqual(result["relations"][0]["id"], "edge-only")

    def test_source_scoped_ids_and_ambiguous_names_never_pick_the_first(self):
        data = self.data("get_node", name="submit")
        self.assertTrue(data["ambiguous"])
        self.assertEqual({node["source_id"] for node in data["candidates"]}, {"sourceA", "sourceB"})
        self.assertNotIn("node", data)
        for sid in ("sourceA", "sourceB"):
            node = self.data("get_node", node_id=sid + ":duplicate")["node"]
            self.assertEqual(node["source_id"], sid)
            self.assertEqual(node["original_id"], "duplicate")
        self.assert_tool_error("get_node", node_id="sourceA:duplicate", name="submit")

    def test_anchor_nodes_and_typed_original_ids_survive_graph_read(self):
        self.assertIsNone(self.data("get_node", node_id="qa:anchor")["node"]["original_id"])
        self.graph["nodes"].extend([
            {**self.graph["nodes"][0], "id": "sourceA:int", "original_id": 1},
            {**self.graph["nodes"][0], "id": "sourceA:string", "original_id": "1"},
        ])
        self.meta["active_version"] = "typed-ids"
        self.publish()
        self.assertEqual(self.data("get_node", node_id="sourceA:int")["node"]["original_id"], 1)
        self.assertEqual(self.data("get_node", node_id="sourceA:string")["node"]["original_id"], "1")

    def test_path_traces_planning_code_ui_and_qa_using_real_edges(self):
        for target in ("sourceB:duplicate", "qa:anchor"):
            result = self.data("find_path", source="planning:REQ-101", target=target, max_hops=2)
            self.assertTrue(result["found"])
            self.assertEqual(result["path"][0]["id"], "planning:REQ-101")
            self.assertEqual(result["path"][1]["id"], "sourceA:duplicate")
            self.assertEqual(result["path"][-1]["id"], target)
            self.assertEqual(len(result["links"]), 2)
        result = self.data("find_path", source="planning:REQ-101", target="qa:anchor", max_hops=1)
        self.assertFalse(result["found"])
        self.assert_tool_error("find_path", source="planning:REQ-101", target="qa:anchor", max_hops=9)

    def test_path_and_neighbors_preserve_ambiguity_and_parallel_edge_direction(self):
        for name, args in (
            ("find_path", {"source": "submit", "target": "qa:anchor"}),
            ("get_neighbors", {"node_id": "submit"}),
        ):
            self.assertTrue(self.data(name, **args)["ambiguous"])
        result = self.data("get_neighbors", node_id="sourceA:duplicate", direction="out")
        self.assertEqual({edge["id"] for edge in result["links"]}, {"api-ui", "second-api-ui"})
        self.assertEqual(len(result["nodes"]), 1)
        self.assertTrue(all(edge["source"] == "sourceA:duplicate" for edge in result["links"]))
        self.assertEqual(self.data("find_path", source="qa:anchor", target="qa:anchor")["hops"], 0)

    def test_relation_returns_exact_quote_hash_and_both_source_provenances(self):
        relation = self.data("get_relation", edge_id="api-ui")["relation"]
        self.assertEqual(relation, self.graph["relations"][1])
        self.assertEqual(relation["evidence"][0]["quote"], "def submit():\n    # REQ-101 order")
        self.assertEqual({item["source_id"] for item in relation["evidence"]}, {"sourceA", "sourceB"})
        for item in relation["evidence"]:
            self.assertEqual(item["sha256"], digest(item["quote"]))
            self.assertEqual(item["source_version"], self.versions[item["source_id"]])
        self.assertTrue(all(body.closed for body in self.s3.bodies))

    def test_source_reads_preserve_crlf_unicode_trailing_newlines_and_source_identity(self):
        data = self.data("read_source", source_id="sourceA", file="src/api.py", start_line=1, end_line=3, group_version=VERSION)
        self.assertEqual(data["text"], self.texts["sourceA"]["src/api.py"])
        self.assertEqual(data["sha256"], digest(data["text"]))
        self.assertEqual(data["quote_sha256"], digest(data["text"]))
        self.assertEqual((data["start_line"], data["end_line"], data["line_count"]), (1, 3, 3))
        for sid in ("sourceA", "sourceB"):
            result = self.module.get_source(*self.args, source_id=sid, file="README.md")
            self.assertEqual(result["text"], self.texts[sid]["README.md"])
            self.assertEqual(result["source_version"], self.versions[sid])
            json.dumps(result)

    def test_read_limits_preserve_very_long_lines_and_empty_files(self):
        self.texts["sourceA"]["long.txt"] = "X" * 50_000 + "\n" + "".join(f"{i}\n" for i in range(500))
        self.texts["sourceA"]["empty.txt"] = ""
        self.publish()
        data = self.data("read_source", source_id="sourceA", file="long.txt")
        self.assertEqual(data["end_line"], 400)
        self.assertTrue(data["text"].startswith("X" * 50_000 + "\n"))
        self.assertEqual(len(data["text"].splitlines()), 400)
        empty = self.data("read_source", source_id="sourceA", file="empty.txt")
        self.assertEqual((empty["text"], empty["line_count"]), ("", 0))
        for bounds in ({"start_line": 0}, {"start_line": True}, {"end_line": 401},
                       {"start_line": 501, "end_line": 500}, {"start_line": 600}):
            self.assert_tool_error("read_source", source_id="sourceA", file="long.txt", **bounds)

    def test_search_is_literal_and_reports_exact_source_lines(self):
        self.texts["sourceA"]["literal.txt"] = "(a+)+$ is literal\n" + "a" * 30_000 + "!\n"
        self.publish()
        result = self.data("search_code", pattern="(a+)+$", source_id="sourceA", glob="*.txt")
        self.assertEqual(len(result["matches"]), 1)
        self.assertEqual(result["matches"][0]["quote"], "(a+)+$ is literal\n")
        result = self.data("search_code", pattern="REQ-101", source_id="sourceB", glob="src/*.ts")
        self.assertEqual(result["files_scanned"], 1)
        self.assertEqual(result["matches"][0]["quote"], self.texts["sourceB"]["src/client.ts"].splitlines(keepends=True)[1])
        self.assertEqual(result["matches"][0]["group_version"], VERSION)
        self.assertEqual(result["matches"][0]["line"], 2)
        self.assertEqual(result["matches"][0]["sha256"], digest(result["matches"][0]["quote"]))

    def test_search_has_result_file_byte_and_time_budgets(self):
        self.assertTrue(self.data("search_code", pattern="REQ-101", max_results=1)["partial"])
        with mock.patch.object(self.module, "SEARCH_MAX_FILES", 1):
            result = self.data("search_code", pattern="not present")
            self.assertTrue(result["partial"])
            self.assertEqual(result["files_scanned"], 1)
        with mock.patch.object(self.module, "SEARCH_MAX_BYTES", self.module.SOURCE_MAX_BYTES):
            result = self.data("search_code", pattern="not present")
            self.assertTrue(result["partial"])
            self.assertEqual(result["files_scanned"], 1)
        with mock.patch.object(self.module.time, "monotonic", side_effect=[0, 10]):
            result = self.data("search_code", pattern="anything")
            self.assertEqual(result["stop_reasons"], ["time_limit"])
            self.assertEqual(result["files_scanned"], 0)

    def test_all_user_storage_runtime_and_regex_overrides_are_rejected(self):
        for key in ("bucket", "key", "project_path", "runtime", "host", "s3_key", "regex"):
            self.assert_tool_error("search_code", pattern="submit", **{key: "evil"})
        for path in ("../secret", "/secret", "a/../secret", "a//secret", "./secret",
                     "a\\secret", "%2e%2e/secret", "s3://bucket/key", "a\0secret", "C:/secret"):
            self.assert_tool_error("read_source", source_id="sourceA", file=path)
        self.assertEqual(self.s3.calls, [])

    def test_normal_percent_and_colon_document_names_remain_readable(self):
        file = "docs/100% 보장: 안내.md"
        self.texts["sourceA"][file] = "정확한 원문\n"
        self.publish()
        result = self.module.get_source(*self.args, source_id="sourceA", file=file)
        self.assertEqual(result["text"], "정확한 원문\n")

    def test_escaped_response_size_is_bounded_before_lambda_serialization(self):
        self.texts["sourceA"]["long.txt"] = '"' * 600000
        self.publish()
        with self.assertRaises(self.module.GroupError) as error:
            self.module.get_source(*self.args, source_id="sourceA", file="long.txt")
        self.assertEqual(error.exception.status, 413)

    def test_large_pages_shrink_and_keep_the_actual_next_offset(self):
        for node in self.graph["nodes"]:
            node["label"] += "x" * 1000
        self.publish()
        with mock.patch.object(self.module, "MAX_RESPONSE_BYTES", 3500):
            result = self.module.get_graph_page(*self.args, limit=500)
        self.assertGreater(len(result["nodes"]), 0)
        self.assertLess(len(result["nodes"]), 4)
        self.assertEqual(result["next_offset"], len(result["nodes"]))

    def test_tampered_manifest_keys_are_rejected_before_fetching_outside_prefix(self):
        entry = self.manifest["files"]["sourceA"]["src/api.py"]
        for malicious in (
            "private/other-tenant/source.txt",
            self.prefix + f"files/sourceB/{digest('src/api.py')}.txt",
            self.prefix + "files/sourceA/../../graph.json",
            self.prefix + "files/sourceA/not-a-hash.txt",
            self.prefix.replace(VERSION, "other-version") + f"files/sourceA/{digest('src/api.py')}.txt",
        ):
            with self.subTest(key=malicious):
                entry["key"] = malicious
                self.save_manifest()
                self.s3.calls.clear()
                self.assert_tool_error("get_node", 409, node_id="sourceA:duplicate")
                self.assertEqual(self.s3.calls, [self.prefix + "manifest.json"])

    def test_manifest_file_paths_and_graph_keys_are_validated_even_for_unread_files(self):
        original = copy.deepcopy(self.manifest)
        for change in (
            lambda manifest: manifest["files"]["qa"].update({"../hidden": manifest["files"]["qa"]["qa.md"]}),
            lambda manifest: manifest.update(graph_key="private/graph.json"),
            lambda manifest: manifest.update(group_id="grp_" + "b" * 32),
            lambda manifest: manifest.update(revision=2),
            lambda manifest: manifest.update(version="other"),
            lambda manifest: manifest["source_versions"].update(sourceB="changed"),
        ):
            self.manifest = copy.deepcopy(original)
            change(self.manifest)
            self.save_manifest()
            self.assert_tool_error("query_graph", 409, query="submit")

    def test_source_tampering_and_bad_quotes_fail_closed(self):
        key = self.manifest["files"]["sourceA"]["src/api.py"]["key"]
        self.s3.objects[key] += b"tampered"
        self.assert_tool_error("read_source", 409, source_id="sourceA", file="src/api.py")
        self.assert_tool_error("get_relation", 409, edge_id="api-ui")
        self.publish()
        self.graph["relations"][1]["evidence"][0]["quote"] = "invented statement"
        self.graph["relations"][1]["evidence"][0]["sha256"] = digest("invented statement")
        self.meta["active_version"] = "invented-quote"
        self.publish()
        self.assert_tool_error("get_relation", 409, edge_id="api-ui")

    def test_quote_checksum_line_range_and_evidence_sources_are_validated(self):
        original = copy.deepcopy(self.graph)
        for change in (
            lambda item: item.update(sha256="0" * 64),
            lambda item: item.update(line_end=9000),
            lambda item: item.update(source_id="qa"),
            lambda item: item.update(source_version="outdated"),
        ):
            self.graph = copy.deepcopy(original)
            change(self.graph["relations"][0]["evidence"][0])
            self.publish()
            self.assert_tool_error("get_node", 409, node_id="sourceA:duplicate")

    def test_graph_digest_missing_snapshot_and_invalid_json_have_safe_errors(self):
        graph_key = self.prefix + "graph.json"
        self.s3.objects[graph_key] += b" "
        self.assert_tool_error("get_node", 409, node_id="sourceA:duplicate")
        self.publish()
        del self.s3.objects[graph_key]
        result = self.assert_tool_error("get_node", 409, node_id="sourceA:duplicate")
        self.assertNotIn("latest", json.dumps(result))
        self.publish()
        self.s3.objects[self.prefix + "manifest.json"] = b'{"files":{},"files":{}}'
        self.assert_tool_error("get_node", 409, node_id="sourceA:duplicate")

    def test_no_source_or_group_version_fallback_is_possible(self):
        for name, args in (
            ("get_node", {"node_id": "sourceA:duplicate"}),
            ("read_source", {"source_id": "sourceA", "file": "src/api.py"}),
            ("search_code", {"pattern": "submit"}),
        ):
            self.assert_tool_error(name, 409, group_version="stale", **args)
        self.assertEqual(self.s3.calls, [])
        del self.s3.objects[self.manifest["files"]["sourceA"]["src/api.py"]["key"]]
        self.assert_tool_error("read_source", 409, source_id="sourceA", file="src/api.py")
        self.assertTrue(all(key.startswith(self.prefix) for key in self.s3.calls))

    def test_stale_current_source_versions_and_revision_gate_all_derived_data(self):
        self.ddb.put(REGISTRY, {"repo_id": "sourceB"}, {
            "enabled": "1", "graph_scope": "private", "active_source_version": "new-source-version",
        })
        self.assert_tool_error("get_node", 409, node_id="sourceA:duplicate")
        self.assert_http_error(409, self.module.get_graph_page)
        self.assertEqual(self.s3.calls, [])
        self.setUp_registry_version()
        self.meta["revision"] += 1
        self.save_meta()
        self.assert_tool_error("read_source", 409, source_id="sourceA", file="src/api.py")

    def test_description_version_changes_and_mismatched_manifests_block_derived_data(self):
        self.manifest["source_descriptions"] = {sid: 1 for sid in self.versions}
        self.save_manifest()
        self.assert_tool_error("get_node", 409, node_id="sourceA:duplicate")
        self.publish()
        self.ddb.put(REGISTRY, {"repo_id": "sourceB"}, {
            "enabled": "1", "graph_scope": "private",
            "active_source_version": self.versions["sourceB"], "description_version": 1,
        })
        self.s3.calls.clear()
        self.assert_tool_error("get_node", 409, node_id="sourceA:duplicate")
        self.assertEqual(self.s3.calls, [])

    def setUp_registry_version(self):
        self.ddb.put(REGISTRY, {"repo_id": "sourceB"}, {
            "enabled": "1", "graph_scope": "private", "active_source_version": self.versions["sourceB"],
        })

    def test_acl_denial_prevents_all_data_for_an_inaccessible_group_member(self):
        self.remove_source_access()
        self.assert_tool_error("get_node", 403, node_id="sourceA:duplicate")
        self.assert_http_error(403, self.module.get_graph_page)
        self.assertEqual(self.rpc("tools/list")["error"]["data"]["status"], 403)
        self.assertEqual(self.s3.calls, [])

    def test_acl_is_rechecked_before_returning_even_on_cache_hits(self):
        self.data("get_node", node_id="sourceA:duplicate")
        graph_reads = self.s3.calls.count(self.prefix + "graph.json")
        self.s3.after_get = lambda _key: self.remove_source_access()
        response = self.assert_tool_error("get_node", 403, node_id="sourceA:duplicate")
        self.assertNotIn("group_version", response["result"]["structuredContent"])
        self.assertNotIn("sourceA:duplicate", json.dumps(response))
        self.assertEqual(self.s3.calls.count(self.prefix + "graph.json"), graph_reads)

    def test_source_read_and_http_page_recheck_acl_after_loading(self):
        for function, kwargs in (
            (self.module.get_source, {"source_id": "sourceA", "file": "src/api.py"}),
            (self.module.get_graph_page, {}),
        ):
            self.ddb.put(PLATFORM, {"pk": "USER#" + SUB, "sk": "REPO#sourceB"}, {"role": "reader"})
            self.s3.after_get = lambda _key: self.remove_source_access()
            self.assert_http_error(403, function, **kwargs)

    def test_acl_and_source_version_changes_during_graph_load_deny_return(self):
        def change_version(key):
            if key.endswith("graph.json"):
                self.ddb.put(REGISTRY, {"repo_id": "sourceB"}, {
                    "enabled": "1", "graph_scope": "private", "active_source_version": "new",
                })
        self.s3.after_get = change_version
        self.assert_tool_error("find_path", 409, source="planning:REQ-101", target="qa:anchor")

    def test_pages_pin_versions_and_do_not_mix_after_rebuild(self):
        page = self.module.get_graph_page(*self.args, limit=2)
        self.assertEqual(page["next_offset"], 2)
        self.assertEqual(page["sources"], self.sources)
        second = self.module.get_graph_page(*self.args, offset=2, limit=2, group_version=page["version"])
        self.assertIsNone(second["next_offset"])
        self.assertEqual(page["nodes"] + second["nodes"], self.graph["nodes"])
        self.assert_http_error(400, self.module.get_graph_page, offset=2)
        self.meta.update(active_version="build-2", revision=2, active_revision=2)
        self.publish()
        self.assert_http_error(409, self.module.get_graph_page, offset=2, group_version=page["version"])
        for kind in ("nodes", "links", "relations"):
            result = self.module.get_graph_page(*self.args, kind=kind, group_version="build-2")
            self.assertEqual(result[kind], self.graph[kind])
        json.dumps(result)

    def test_active_version_changes_during_request_reject_prepared_page(self):
        def advance(key):
            if key.endswith("graph.json"):
                self.meta.update(active_version="build-2")
                self.save_meta()
        self.s3.after_get = advance
        self.assert_http_error(409, self.module.get_graph_page)

    def test_cache_is_immutable_bounded_to_two_and_returns_detached_data(self):
        result = self.module.get_graph_page(*self.args)
        result["nodes"][0]["label"] = "caller mutation"
        self.assertEqual(self.data("get_node", node_id="sourceA:duplicate")["node"]["label"], "submit")
        for version in ("build-2", "build-3"):
            self.meta["active_version"] = version
            self.publish()
            self.data("get_node", node_id="sourceA:duplicate")
        self.assertEqual(len(self.module._CACHE), 2)
        self.assertEqual([key[-1] for key in self.module._CACHE], ["build-2", "build-3"])
        self.manifest["usage"]["input_tokens"] += 1
        self.save_manifest()
        self.assert_tool_error("get_node", 409, node_id="sourceA:duplicate")

    def test_partial_build_keeps_partial_status_usage_and_reasons(self):
        self.meta["status"] = "PARTIAL"
        self.save_meta()
        self.manifest.update(partial=True, partial_reasons=["candidate_limit"])
        self.save_manifest()
        result = self.data("query_graph", query="REQ-101")
        self.assertTrue(result["partial"])
        self.assertEqual(result["partial_reasons"], ["candidate_limit"])
        self.assertEqual(result["status"], "PARTIAL")

    def test_graph_and_source_reads_enforce_byte_caps_even_without_content_length(self):
        with mock.patch.object(self.module, "GRAPH_MAX_BYTES", 20):
            self.assert_tool_error("get_node", 413, node_id="sourceA:duplicate")
        self.s3.report_length = False
        with mock.patch.object(self.module, "GRAPH_MAX_BYTES", 20):
            self.assert_tool_error("get_node", 413, node_id="sourceA:duplicate")
        with mock.patch.object(self.module, "SOURCE_MAX_BYTES", 20):
            self.assert_tool_error("read_source", 413, source_id="sourceA", file="src/api.py")
        self.assertTrue(all(body.closed for body in self.s3.bodies))

    def test_duplicate_graph_ids_missing_endpoints_and_wrong_source_versions_are_rejected(self):
        original = copy.deepcopy(self.graph)
        for change in (
            lambda graph: graph["nodes"].append(copy.deepcopy(graph["nodes"][0])),
            lambda graph: graph["links"][0].update(target="missing"),
            lambda graph: graph["nodes"][0].update(source_version="old"),
            lambda graph: graph["links"].append(copy.deepcopy(graph["links"][0])),
            lambda graph: graph["nodes"][0].update(source_id=["bad"]),
        ):
            self.graph = copy.deepcopy(original)
            change(self.graph)
            self.publish()
            self.assert_tool_error("query_graph", 409, query="submit")

    def test_public_source_still_requires_active_registry_and_private_source_acl(self):
        self.ddb.put(REGISTRY, {"repo_id": "sourceB"}, {
            "enabled": "1", "graph_scope": "public", "active_source_version": self.versions["sourceB"],
        })
        self.remove_source_access()
        self.data("get_node", node_id="sourceA:duplicate")
        self.remove_source_access("sourceA")
        self.assert_tool_error("get_node", 403, node_id="sourceB:duplicate")

    def test_user_deletion_group_grant_revocation_and_source_disable_block_cached_data(self):
        self.data("get_node", node_id="sourceA:duplicate")
        for revoke in (
            lambda: self.ddb.put(PLATFORM, {"pk": "USER#" + SUB, "sk": "DELETED"}, {"deleted": True}),
            lambda: self.ddb.remove(PLATFORM, {"pk": "USER#" + SUB, "sk": "GROUP#" + GID}),
            lambda: self.ddb.put(REGISTRY, {"repo_id": "sourceA"}, {"enabled": "0"}),
        ):
            saved = copy.deepcopy(self.ddb.rows)
            revoke()
            self.assert_tool_error("get_node", 403, node_id="sourceA:duplicate")
            self.ddb.rows = saved

    def test_real_offline_composer_output_supports_queries_relations_and_raw_text(self):
        # This pure producer/consumer contract catches drift in hashed IDs,
        # source anchors, normalized quotes, and copied graph fields.
        spec = importlib.util.spec_from_file_location(
            "offline_group_engine_for_query", ROOT / "lambdas/group_worker/engine.py",
        )
        engine = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(engine)
        inputs = []
        for source in self.sources:
            sid = source["source_id"]
            nodes = []
            if sid in ("sourceA", "sourceB"):
                file = next(iter(self.texts[sid]))
                nodes = [{
                    "id": 1, "label": "submit", "type": "function", "source_file": file,
                    "line_start": 1, "line_end": 3,
                }]
            inputs.append({
                **source, "version": self.versions[sid], "files": self.texts[sid],
                "graph": {"directed": True, "multigraph": True, "nodes": nodes, "links": []},
            })
        built = engine.build_group(group_id=GID, sources=inputs)
        self.assertTrue(built["graph"]["relations"])
        self.graph = built["graph"]
        self.publish()
        self.manifest.update({field: built[field] for field in ("stats", "usage", "limits", "partial", "partial_reasons")})
        self.save_manifest()
        self.assertTrue(self.data("get_node", name="submit")["ambiguous"])
        self.assertEqual(self.data("get_node", node_id=engine.node_id("sourceA", 1))["node"]["original_id"], 1)
        for edge in self.graph["relations"]:
            result = self.data("get_relation", edge_id=edge["id"])
            self.assertEqual(result["relation"], edge)
            for item in result["relation"]["evidence"]:
                source = self.data(
                    "read_source", source_id=item["source_id"], file=item["file"],
                    start_line=item["line_start"], end_line=item["line_end"], group_version=VERSION,
                )
                self.assertEqual("\n".join(source["text"].splitlines()), item["quote"])


if __name__ == "__main__":
    unittest.main()
