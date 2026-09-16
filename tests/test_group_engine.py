"""Offline fixtures for pinned source composition and bounded relation analysis.

Run: .venv/bin/python -B -m unittest discover -s tests -p test_group_engine.py
"""

import copy
import gzip
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import socket
import tarfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("offline_group_engine", ROOT / "lambdas/group_worker/engine.py")
engine = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(engine)
GID = "grp_" + "a" * 32


def source(sid, text="REQ-123", *, path="README.md", role="", nodes=None, links=None, version="v1"):
    return {
        "source_id": sid, "version": version, "files": {path: text}, "role": role,
        "graph": {
            "directed": True,
            "nodes": nodes if nodes is not None else [
                {"id": "duplicate-id", "label": sid, "type": "function", "source_file": path,
                 "line_start": 1, "line_end": len(text.splitlines())}
            ],
            "links": links or [],
        },
    }


def project():
    return [
        source("frontend", "// REQ-123\nfetch('https://pension-api.example/accounts', {method:'GET'});\n",
               role="frontend", path="src/account.ts"),
        source("backend", "# REQ-123\n# GET https://pension-api.example/accounts\n"
               "@app.get('/accounts')\ndef list_accounts():\n    pass\n",
               role="backend", path="api/account.py"),
        source("planning", "# REQ-123 Account list\nGET https://pension-api.example/accounts\n"
               "QA-101: empty account list\n", role="planning", path="requirements.md"),
        source("qa", "# QA-101\nRequirement REQ-123\nGET https://pension-api.example/accounts\n",
               role="qa", path="cases.md"),
    ]


def tar_bytes(entries):
    """entries: (path, bytes, typeflag?, linkname?) without touching disk."""
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for entry in entries:
            name, data, *options = entry
            info = tarfile.TarInfo(name)
            info.size = len(data)
            if options:
                info.type = options[0]
                info.linkname = options[1] if len(options) > 1 else ""
            archive.addfile(info, io.BytesIO(data))
    return output.getvalue()


class Model:
    def __init__(self, behavior="valid"):
        self.behavior, self.calls = behavior, []
        self.replies = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if self.behavior == "error":
            raise RuntimeError("private data and credentials must never appear in diagnostics")
        candidates = json.loads(kwargs["messages"][0]["content"][0]["text"])["candidates"]
        decisions = [
            {"candidate_id": candidate["id"], "relation": "related_to",
             **({"evidence_refs": [0, 1]} if self.behavior == "compact"
                else {"evidence": candidate["evidence"]})}
            for candidate in candidates
        ]
        if self.behavior == "bad_quote":
            decisions[0]["evidence"][0]["quote"] = "not in source"
        if self.behavior == "bad_range":
            decisions[0]["evidence"][0]["line_start"] += 1
        if self.behavior == "unsupported":
            decisions[0]["relation"] = "proves_correctness"
        text = "not json" if self.behavior == "bad_json" else json.dumps({"relations": decisions})
        self.replies.append(text)
        return {
            "output": {"message": {"content": [{"text": text}]}},
            "usage": {"inputTokens": 100, "outputTokens": 50}, "stopReason": "end_turn",
        }


class EngineTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(socket, "create_connection", side_effect=AssertionError("No network"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def build(self, sources=None, **kwargs):
        return engine.build_group(group_id=GID, sources=sources or project(), **kwargs)

    def test_korean_particles_do_not_hide_requirement_or_test_identifiers(self):
        result = self.build([source("a", "REQ-100을 TC-100으로 검증한다."),
                             source("b", "REQ-100: 가입\nTC-100: 가입 검사")])
        self.assertEqual({edge["relation"] for edge in result["graph"]["relations"]},
                         {"shared_requirement", "shared_test_case"})
        self.assertFalse(result["partial"])

    def test_identifiers_inside_larger_ascii_identifiers_do_not_match(self):
        result = self.build([source("a", "PRE-REQ-100 REQ-100-OLD XTC-100 TC-100_A"),
                             source("b", "REQ-100 TC-100")])
        self.assertEqual(result["graph"]["relations"], [])

    def test_project_relations_connect_fe_be_planning_qa_with_two_exact_quotes(self):
        sources = project()
        result = self.build(sources)
        self.assertFalse(result["partial"])
        relations = result["graph"]["relations"]
        self.assertEqual({edge["relation"] for edge in relations},
                         {"shared_requirement", "shared_contract", "shared_test_case"})
        required_pairs = {
            frozenset(("frontend", "backend")), frozenset(("backend", "planning")),
            frozenset(("planning", "qa")),
        }
        self.assertTrue(required_pairs.issubset({
            frozenset(e["source_id"] for e in edge["evidence"]) for edge in relations
        }))
        for edge in relations:
            self.assertEqual(len(edge["evidence"]), 2)
            self.assertEqual(edge["evidence_kind"], "EXTRACTED")
            self.assertIn(edge, result["graph"]["links"])
            for evidence in edge["evidence"]:
                self.assertTrue(engine.validate_evidence(evidence, sources))
                self.assertEqual(evidence["sha256"], hashlib.sha256(evidence["quote"].encode()).hexdigest())
        self.assertNotIn("implements_requirement", {edge["relation"] for edge in relations})

    def test_duplicate_ids_files_and_labels_are_namespaced_and_directions_parallel_edges_preserved(self):
        nodes = [{"id": "one", "label": "Same", "source_file": "README.md"},
                 {"id": "two", "label": "Same", "source_file": "README.md"}]
        edges = [
            {"id": "edge", "source": "one", "target": "two", "relation": "calls", "weight": 2},
            {"id": "edge", "source": "one", "target": "two", "relation": "imports", "weight": 3},
            {"id": "edge", "source": "two", "target": "one", "relation": "returns"},
        ]
        sources = [source("a", "one", nodes=nodes, links=edges),
                   source("b", "two", nodes=nodes, links=edges)]
        original = copy.deepcopy(sources)
        graph = self.build(sources)["graph"]
        self.assertEqual(sources, original)
        self.assertEqual(len(graph["nodes"]), 4)
        self.assertEqual(len(graph["links"]), 6)
        self.assertEqual(len({node["id"] for node in graph["nodes"]}), 4)
        self.assertEqual(len({edge["id"] for edge in graph["links"]}), 6)
        for sid in ("a", "b"):
            preserved = [edge for edge in graph["links"] if edge["source_id"] == sid]
            self.assertEqual([edge["relation"] for edge in preserved], ["calls", "imports", "returns"])
            self.assertEqual(preserved[0]["source"], preserved[2]["target"])
            self.assertEqual(preserved[0]["target"], preserved[2]["source"])
            self.assertEqual(preserved[0]["weight"], 2)
        self.assertTrue(graph["directed"])
        self.assertTrue(graph["multigraph"])

    def test_typed_ids_long_ids_and_reordering_sources_are_stable(self):
        nodes = [{"id": 1}, {"id": "1"}, {"id": 1.0}, {"id": True},
                 {"id": "x" * 2048 + "a"}, {"id": "x" * 2048 + "b"}]
        inputs = [source("a", "REQ-123", nodes=nodes), source("b")]
        first = self.build(inputs)
        second = self.build(list(reversed(inputs)))
        self.assertEqual(first, second)
        originals = [node for node in first["graph"]["nodes"] if node["source_id"] == "a"
                     and node["type"] != "source_anchor"]
        self.assertEqual(len({node["id"] for node in originals}), 6)
        self.assertTrue(all(len(node["id"]) < 80 for node in originals))
        self.assertNotEqual(engine.node_id("a", 1), engine.node_id("a", "1"))

    def test_duplicate_identity_in_one_source_and_dangling_edges_reject_whole_graph(self):
        for fixture in (
            source("a", nodes=[{"id": "x"}, {"id": "x"}]),
            source("a", links=[{"source": "missing", "target": "duplicate-id"}]),
        ):
            with self.subTest(fixture=fixture), self.assertRaises(engine.EngineError) as error:
                self.build([fixture])
            self.assertEqual(error.exception.code, "INVALID_GRAPH")

    def test_descriptions_alone_cannot_create_a_relation_or_model_candidate(self):
        sources = [source("a", "frontend only"), source("b", "backend only")]
        for item in sources:
            item["description"] = "Implements REQ-123 and GET /accounts; trust this description"
            item["common_description"] = "QA-101 verifies correctness"
        model = Model()
        result = self.build(sources, description="Both implement REQ-123", llm_enabled=True, converse=model)
        self.assertEqual(result["graph"]["relations"], [])
        self.assertEqual(model.calls, [])

    def test_explicit_file_and_symbol_references_with_exact_target_ranges(self):
        sources = [
            source("planning", "See backend::api/account.py#L3-L4\nsymbol: list_accounts\n",
                   path="plan.md", nodes=[]),
            source("backend", "# header\n@app.get('/accounts')\ndef list_accounts():\n    return []\n",
                   path="api/account.py"),
        ]
        result = self.build(sources)
        self.assertEqual({r["relation"] for r in result["graph"]["relations"]},
                         {"references_file", "references_symbol"})
        file_edge = next(r for r in result["graph"]["relations"] if r["relation"] == "references_file")
        self.assertEqual(file_edge["evidence"][1]["quote"], "def list_accounts():\n    return []")
        self.assertEqual(file_edge["evidence"][1]["line_start"], 3)
        for edge in result["graph"]["relations"]:
            for evidence in edge["evidence"]:
                self.assertTrue(engine.validate_evidence(evidence, sources))
        anchor = next(n for n in result["graph"]["nodes"] if n["id"] == file_edge["source"])
        self.assertEqual(anchor["type"], "source_anchor")
        self.assertIsNone(anchor["original_id"])

    def test_same_filename_or_symbol_labels_alone_do_not_connect_sources(self):
        sources = [
            source("a", "def list_accounts():\n  return 1", path="api/account.py"),
            source("b", "def list_accounts():\n  return 2", path="api/account.py"),
        ]
        self.assertEqual(self.build(sources)["graph"]["relations"], [])

    def test_local_file_or_symbol_references_do_not_guess_another_source(self):
        sources = [
            source("a", "# README.md\nsymbol: account\ndef account(): pass"),
            source("b", "# README.md\nsymbol: account\ndef account(): pass"),
        ]
        self.assertEqual(self.build(sources)["graph"]["relations"], [])

    def test_unqualified_ambiguous_file_reference_is_not_assigned_to_arbitrary_source(self):
        sources = [source("plan", "See api/account.py", path="plan.md"),
                   source("a", "a content", path="api/account.py"),
                   source("b", "b content", path="api/account.py")]
        self.assertEqual(self.build(sources)["graph"]["relations"], [])

    def test_http_host_method_and_route_hints_are_conservative(self):
        a = source("a", "GET https://one.example/accounts/{id}")
        b = source("b", "GET https://two.example/accounts/:id")
        self.assertEqual(self.build([a, b])["graph"]["relations"], [])
        b["files"]["README.md"] = "POST https://one.example/accounts/:id"
        self.assertEqual(self.build([a, b])["graph"]["relations"], [])
        b["files"]["README.md"] = "GET https://one.example/accounts/:id"
        relations = self.build([a, b])["graph"]["relations"]
        self.assertEqual(len(relations), 1)
        self.assertEqual(relations[0]["relation"], "shared_contract")

    def test_approximate_or_page_locations_use_source_anchors(self):
        a = source("a", nodes=[{"id": "a", "source_file": "README.md", "source_location": "L1 (~)"}])
        b = source("b", nodes=[{"id": "b", "source_file": "README.md", "source_location": "page 1"}])
        result = self.build([a, b])
        self.assertEqual(sum(n["type"] == "source_anchor" for n in result["graph"]["nodes"]), 2)

    def test_emitted_source_file_paths_are_canonical_and_traversal_fails(self):
        a = source("a", nodes=[{"id": "a", "source_file": "./README.md", "source_location": "L1"}])
        graph = self.build([a, source("b")])["graph"]
        self.assertTrue(all(n["source_file"] == "README.md" for n in graph["nodes"]))
        self.assertFalse(any(n["type"] == "source_anchor" for n in graph["nodes"]))
        for path in ("../README.md", "a//README.md", "/tmp/README.md"):
            a["graph"]["nodes"][0]["source_file"] = path
            with self.subTest(path=path), self.assertRaises(engine.EngineError) as error:
                self.build([a])
            self.assertEqual(error.exception.code, "INVALID_GRAPH")

    def test_evidence_validation_rejects_version_hash_quote_range_and_source_forgery(self):
        sources = [source("a"), source("b")]
        evidence = self.build(sources)["graph"]["relations"][0]["evidence"][0]
        changes = {"quote": "invented", "sha256": "0" * 64, "source_version": "latest",
                   "file": "../x", "line_start": 0, "line_end": 999, "source_id": []}
        for field, value in changes.items():
            with self.subTest(field=field):
                self.assertFalse(engine.validate_evidence(dict(evidence, **{field: value}), sources))

    def test_optional_model_output_is_labelled_inferred_and_quote_validated(self):
        model = Model()
        sources = [source("a"), source("b")]
        result = self.build(sources, llm_enabled=True, converse=model)
        self.assertFalse(result["partial"])
        relations = result["graph"]["relations"]
        self.assertEqual(len(relations), 2)
        inferred = next(r for r in relations if r["evidence_kind"] == "INFERRED")
        self.assertEqual(inferred["model_id"], "global.anthropic.claude-sonnet-5")
        self.assertEqual(inferred["review_status"], "unreviewed")
        self.assertTrue(all(engine.validate_evidence(e, sources) for e in inferred["evidence"]))
        self.assertGreater(result["usage"]["input_tokens_reserved"], 100)
        self.assertEqual(result["usage"]["input_tokens"], 100)
        self.assertEqual(result["limits"]["model_concurrency"], 1)

    def test_sonnet_converse_omits_unsupported_temperature(self):
        model = Model()

        def sonnet(**kwargs):
            self.assertEqual(kwargs["inferenceConfig"], {"maxTokens": engine.MODEL_OUTPUT_TOKENS})
            return model(**kwargs)

        result = self.build([source("a"), source("b")], llm_enabled=True, converse=sonnet)
        self.assertFalse(result["partial"])
        self.assertEqual(result["usage"]["unknown_usage_attempts"], 0)

    def test_compact_batches_use_short_ids_and_store_only_canonical_decisions(self):
        text = "\n".join(f"REQ-{i}: original evidence sentence {i}" for i in range(20))
        sources = [source("a", text), source("b", text)]
        model = Model("compact")
        result = self.build(sources, llm_enabled=True, converse=model)
        self.assertFalse(result["partial"])
        self.assertEqual(len(model.calls), 3)
        decisions = next(iter(result["pair_cache"].values()))["decisions"]
        self.assertEqual(len(decisions), 20)
        self.assertEqual(len({item["candidate_id"] for item in decisions}), 20)
        for item in decisions:
            self.assertRegex(item["candidate_id"], r"^[a-f0-9]{64}$")
            self.assertNotIn("evidence_refs", item)
            self.assertTrue(all(engine.validate_evidence(e, sources) for e in item["evidence"]))
        inferred = [edge for edge in result["graph"]["relations"] if edge["evidence_kind"] == "INFERRED"]
        self.assertEqual(len(inferred), 20)
        for call, reply in zip(model.calls, model.replies):
            prompt = json.loads(call["messages"][0]["content"][0]["text"])
            candidates = prompt["candidates"]
            self.assertEqual([c["id"] for c in candidates], [f"c{i + 1}" for i in range(len(candidates))])
            self.assertTrue(all(len(c["evidence"]) == 2 for c in candidates))
            wire_items = json.loads(reply)["relations"]
            self.assertTrue(all(set(item) == {"candidate_id", "relation", "evidence_refs"}
                                and item["evidence_refs"] == [0, 1] for item in wire_items))
            self.assertNotIn("original evidence sentence", reply)
            self.assertNotIn('"quote"', reply)
            legacy_reply = json.dumps({"relations": [
                {"candidate_id": c["id"], "relation": "related_to", "evidence": c["evidence"]}
                for c in candidates
            ]})
            self.assertLess(len(reply.encode()), len(legacy_reply.encode()))
            self.assertEqual(call["inferenceConfig"], {"maxTokens": 2048})

    def test_compact_decoder_rejects_invalid_refs_mixed_protocols_and_extra_quotes(self):
        evidence = self.build([source("a"), source("b")])["graph"]["relations"][0]["evidence"]
        canonical = {"id": "a" * 64, "evidence": evidence}
        candidates = {"c1": canonical}
        valid = {"candidate_id": "c1", "relation": "related_to", "evidence_refs": [0, 1]}
        invalid = [
            dict(valid, evidence_refs=refs)
            for refs in ([False, True], [0.0, 1], [1, 0], [0], [1], [], [0, 0], [0, 1, 2], None, "0,1")
        ] + [
            dict(valid, candidate_id="c2"), dict(valid, candidate_id=canonical["id"]),
            dict(valid, evidence=evidence), dict(valid, quote="fabricated extra quote"),
            dict(valid, extra_evidence=evidence), dict(valid, reverse=1),
        ]
        for item in invalid:
            with self.subTest(item=item), self.assertRaises(ValueError):
                engine._decisions({"relations": [item]}, candidates)
        normalized = engine._decisions({"relations": [valid]}, candidates)[0]
        self.assertEqual(normalized["candidate_id"], canonical["id"])
        self.assertIs(normalized["evidence"], evidence)

    def test_canonical_full_evidence_cache_decoder_remains_strict(self):
        evidence = self.build([source("a"), source("b")])["graph"]["relations"][0]["evidence"]
        canonical = {"id": "a" * 64, "evidence": evidence}
        cached = {"candidate_id": canonical["id"], "relation": "related_to",
                  "evidence": copy.deepcopy(evidence), "reverse": False}
        normalized = engine._decisions({"relations": [cached]}, {canonical["id"]: canonical})[0]
        self.assertEqual(normalized, cached)
        self.assertIs(normalized["evidence"], evidence)
        for field, value in (("quote", "fabricated"), ("sha256", "0" * 64),
                             ("source_version", "wrong"), ("extra_quote", "fabricated")):
            tampered = copy.deepcopy(cached)
            tampered["evidence"][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                engine._decisions({"relations": [tampered]}, {canonical["id"]: canonical})
        cached["evidence"].append(copy.deepcopy(evidence[0]))
        with self.assertRaises(ValueError):
            engine._decisions({"relations": [cached]}, {canonical["id"]: canonical})

    def test_compact_reply_replays_canonical_cache_without_model_calls(self):
        sources = [source("a"), source("b")]
        first = self.build(sources, llm_enabled=True, converse=Model("compact"))
        no_calls = mock.Mock(side_effect=AssertionError("A complete canonical pair is cached"))
        second = self.build(
            sources, llm_enabled=True, converse=no_calls, previous_manifest=dict(first, group_id=GID),
        )
        no_calls.assert_not_called()
        self.assertEqual(second["usage"]["model_calls"], 0)
        self.assertEqual(second["usage"]["unknown_usage_attempts"], 0)
        self.assertEqual(second["usage"]["max_token_outputs"], 0)
        self.assertEqual(second["usage"]["reused_pairs"], 1)
        self.assertEqual(second["graph"], first["graph"])
        self.assertFalse(second["partial"])

    def test_compact_reverse_changes_direction_without_permuting_evidence_refs(self):
        model = Model("compact")

        def converse(**kwargs):
            response = model(**kwargs)
            payload = json.loads(response["output"]["message"]["content"][0]["text"])
            payload["relations"][0]["reverse"] = True
            response["output"]["message"]["content"][0]["text"] = json.dumps(payload)
            return response

        result = self.build([source("a"), source("b")], llm_enabled=True, converse=converse)
        extracted, inferred = result["graph"]["relations"]
        self.assertEqual(inferred["source"], extracted["target"])
        self.assertEqual(inferred["target"], extracted["source"])
        self.assertEqual(inferred["evidence"], list(reversed(extracted["evidence"])))
        cached = next(iter(result["pair_cache"].values()))["decisions"][0]
        self.assertTrue(cached["reverse"])
        self.assertEqual(cached["evidence"], extracted["evidence"])

    def test_max_tokens_has_distinct_partial_reason_and_preserves_usage(self):
        for raw in ('{"relations":[', '{"relations":[]}'):
            model = Model("compact")

            def converse(**kwargs):
                response = model(**kwargs)
                response["stopReason"] = "max_tokens"
                response["usage"]["outputTokens"] = 2048
                response["output"]["message"]["content"][0]["text"] = raw
                return response

            with self.subTest(raw=raw):
                result = self.build([source("a"), source("b")], llm_enabled=True, converse=converse)
                self.assertEqual(result["partial_reasons"], ["MODEL_MAX_TOKENS"])
                self.assertEqual(result["usage"]["max_token_outputs"], 1)
                self.assertEqual(result["usage"]["model_calls"], 1)
                self.assertEqual(result["usage"]["model_retries"], 0)
                self.assertEqual(result["usage"]["input_tokens"], 100)
                self.assertEqual(result["usage"]["output_tokens"], 2048)
                self.assertEqual(result["usage"]["output_tokens_reserved"], 2048)
                self.assertEqual(result["usage"]["unknown_usage_attempts"], 0)
                self.assertEqual(result["pair_cache"], {})
                self.assertEqual(len(result["graph"]["relations"]), 1)

    def content_result(self, make_blocks, *, stop_reason="end_turn"):
        model = Model("compact")

        def converse(**kwargs):
            response = model(**kwargs)
            final_text = response["output"]["message"]["content"][0]["text"]
            response["output"]["message"]["content"] = make_blocks(final_text)
            response["usage"]["outputTokens"] = 321  # Total billed usage, including reasoning.
            response["stopReason"] = stop_reason
            return response

        with mock.patch("builtins.print", side_effect=AssertionError("Model content must not be logged")):
            return self.build([source("a"), source("b")], llm_enabled=True, converse=converse)

    def test_reasoning_content_is_counted_without_returning_or_caching_hidden_text(self):
        hidden = "PRIVATE_REASONING_FIXTURE"
        signature = "PRIVATE_SIGNATURE_FIXTURE"
        for payload in (
            {"reasoningText": {"text": hidden, "signature": signature}},
            {"text": hidden, "signature": signature},
        ):
            with self.subTest(shape=tuple(payload)):
                result = self.content_result(lambda text: [{"reasoningContent": payload}, {"text": text}])
                self.assertFalse(result["partial"])
                self.assertEqual(result["usage"]["reasoning_blocks"], 1)
                self.assertEqual(result["usage"]["input_tokens"], 100)
                self.assertEqual(result["usage"]["output_tokens"], 321)
                self.assertEqual(result["usage"]["output_tokens_reserved"], 2048)
                self.assertEqual(result["usage"]["unknown_usage_attempts"], 0)
                self.assertEqual(len(result["graph"]["relations"]), 2)
                self.assertNotIn(hidden, json.dumps(result))
                self.assertNotIn(signature, json.dumps(result))
                self.assertNotIn("reasoningContent", json.dumps(result["pair_cache"]))
                cached = self.build(
                    [source("a"), source("b")], llm_enabled=True,
                    converse=mock.Mock(side_effect=AssertionError("Cached pair must not call the model")),
                    previous_manifest=dict(result, group_id=GID),
                )
                self.assertEqual(cached["usage"]["model_calls"], 0)
                self.assertEqual(cached["usage"]["reasoning_blocks"], 0)
                self.assertEqual(cached["graph"], result["graph"])

    def test_redacted_bytes_are_ignored_and_never_serialized(self):
        redacted = b"\x00PRIVATE_REDACTED_FIXTURE\xff"
        for block in (
            {"redactedContent": redacted},
            {"reasoningContent": {"redactedContent": redacted}},
        ):
            with self.subTest(shape=tuple(block)):
                result = self.content_result(lambda text: [block, {"text": text}])
                self.assertFalse(result["partial"])
                self.assertEqual(result["usage"]["reasoning_blocks"], 1)
                self.assertEqual(result["usage"]["output_tokens"], 321)
                self.assertNotIn("PRIVATE_REDACTED_FIXTURE", json.dumps(result))
                self.assertNotIn("redactedContent", json.dumps(result["pair_cache"]))

    def test_final_text_fragments_join_in_order_around_reasoning_blocks(self):
        hidden = {"reasoningContent": {"reasoningText": {"text": "PRIVATE_REASONING_FIXTURE"}}}
        layouts = (
            lambda text: [{"text": text}, hidden],
            lambda text: [hidden, {"text": text}],
            lambda text: [{"text": text[:len(text) // 2]}, hidden, {"text": text[len(text) // 2:]}],
            lambda text: [hidden, {"text": text}, {"redactedContent": b"opaque"}],
        )
        for index, layout in enumerate(layouts):
            with self.subTest(layout=index):
                result = self.content_result(layout)
                self.assertFalse(result["partial"])
                self.assertEqual(result["usage"]["reasoning_blocks"], 2 if index == 3 else 1)
                self.assertEqual(len(result["graph"]["relations"]), 2)
                self.assertNotIn("PRIVATE_REASONING_FIXTURE", json.dumps(result))

    def test_reasoning_without_final_text_is_partial_with_reported_usage_retained(self):
        cases = (
            ([{"reasoningContent": {"text": "PRIVATE_REASONING_FIXTURE"}}], 1),
            ([{"redactedContent": b"opaque"}], 1),
            ([], 0), ([{}], 0),
            ([{"text": ""}, {"reasoningContent": {"text": "PRIVATE_REASONING_FIXTURE"}}], 1),
        )
        for blocks, count in cases:
            with self.subTest(blocks=blocks):
                result = self.content_result(lambda _: blocks)
                self.assertEqual(result["partial_reasons"], ["MODEL_OUTPUT_INVALID"])
                self.assertEqual(result["usage"]["reasoning_blocks"], count)
                self.assertEqual(result["usage"]["input_tokens"], 100)
                self.assertEqual(result["usage"]["output_tokens"], 321)
                self.assertEqual(result["usage"]["unknown_usage_attempts"], 0)
                self.assertEqual(result["pair_cache"], {})
                self.assertEqual(len(result["graph"]["relations"]), 1)
                self.assertNotIn("PRIVATE_REASONING_FIXTURE", json.dumps(result))

    def test_malformed_content_and_nonstring_final_text_are_rejected(self):
        for blocks in (None, {}, "text", [None], ["text"], [{"text": None}],
                       [{"text": 1}], [{"text": b"not a string"}]):
            with self.subTest(blocks=blocks):
                result = self.content_result(lambda _: blocks)
                self.assertEqual(result["partial_reasons"], ["MODEL_OUTPUT_INVALID"])
                self.assertEqual(result["usage"]["reasoning_blocks"], 0)
                self.assertEqual(result["usage"]["output_tokens"], 321)
                self.assertEqual(result["pair_cache"], {})
        result = self.content_result(lambda text: [{"text": text}, "invalid trailing block"])
        self.assertEqual(result["partial_reasons"], ["MODEL_OUTPUT_INVALID"])

    def test_truncated_reasoning_is_counted_without_overriding_max_tokens_status(self):
        result = self.content_result(
            lambda _: [{"reasoningContent": {"redactedContent": b"opaque"}}], stop_reason="max_tokens",
        )
        self.assertEqual(result["partial_reasons"], ["MODEL_MAX_TOKENS"])
        self.assertEqual(result["usage"]["reasoning_blocks"], 1)
        self.assertEqual(result["usage"]["max_token_outputs"], 1)
        self.assertEqual(result["usage"]["output_tokens"], 321)
        self.assertEqual(result["pair_cache"], {})

    def test_failed_attempt_then_success_preserves_known_usage_and_unknown_charges(self):
        model, attempts = Model(), []

        def retry(**kwargs):
            attempts.append(kwargs)
            if len(attempts) == 1:
                raise TimeoutError("Response lost after a potentially charged request")
            return model(**kwargs)

        sources = [source("a"), source("b")]
        result = self.build(sources, llm_enabled=True, converse=retry)
        self.assertEqual(result["usage"]["model_calls"], 2)
        self.assertEqual(result["usage"]["model_retries"], 1)
        self.assertEqual(result["usage"]["unknown_usage_attempts"], 1)
        self.assertEqual(result["usage"]["input_tokens"], 100)
        self.assertEqual(result["usage"]["output_tokens"], 50)
        self.assertEqual(result["usage"]["output_tokens_reserved"], 2 * engine.MODEL_OUTPUT_TOKENS)
        reserve = len((engine._SYSTEM + attempts[0]["messages"][0]["content"][0]["text"]).encode()) + 1024
        self.assertEqual(result["usage"]["input_tokens_reserved"], 2 * reserve)
        self.assertEqual(result["partial_reasons"], ["MODEL_USAGE_UNKNOWN"])
        self.assertEqual(len(result["graph"]["relations"]), 2)
        self.assertEqual(result["pair_cache"], {})
        fresh = Model()
        rebuilt = self.build(
            sources, llm_enabled=True, converse=fresh, previous_manifest=dict(result, group_id=GID),
        )
        self.assertEqual(len(fresh.calls), 1)
        self.assertFalse(rebuilt["partial"])

    def test_missing_or_invalid_usage_retains_valid_counts_and_marks_partial(self):
        cases = [
            (None, 0, 0), ({}, 0, 0), ([], 0, 0),
            ({"inputTokens": 100}, 100, 0),
            ({"outputTokens": 50}, 0, 50),
            ({"inputTokens": -1, "outputTokens": 50}, 0, 50),
            ({"inputTokens": True, "outputTokens": 50}, 0, 50),
            ({"inputTokens": 100, "outputTokens": "50"}, 100, 0),
        ]
        for reported, known_input, known_output in cases:
            model = Model()

            def converse(**kwargs):
                response = model(**kwargs)
                if reported is None:
                    response.pop("usage")
                else:
                    response["usage"] = reported
                return response

            with self.subTest(usage=reported):
                result = self.build([source("a"), source("b")], llm_enabled=True, converse=converse)
                self.assertEqual(result["partial_reasons"], ["MODEL_USAGE_UNKNOWN"])
                self.assertEqual(result["usage"]["unknown_usage_attempts"], 1)
                self.assertEqual(result["usage"]["input_tokens"], known_input)
                self.assertEqual(result["usage"]["output_tokens"], known_output)
                self.assertEqual(result["usage"]["model_calls"], 1)
                self.assertEqual(result["usage"]["model_retries"], 0)
                self.assertEqual(result["usage"]["output_tokens_reserved"], engine.MODEL_OUTPUT_TOKENS)
                self.assertGreater(result["usage"]["input_tokens_reserved"], known_input)
                self.assertEqual(len(result["graph"]["relations"]), 2)
                self.assertEqual(result["pair_cache"], {})

    def test_explicit_zero_usage_is_distinct_from_unknown_usage(self):
        model = Model()

        def converse(**kwargs):
            response = model(**kwargs)
            response["usage"] = {"inputTokens": 0, "outputTokens": 0}
            return response

        result = self.build([source("a"), source("b")], llm_enabled=True, converse=converse)
        self.assertFalse(result["partial"])
        self.assertEqual(result["usage"]["unknown_usage_attempts"], 0)

    def test_missing_stop_reason_is_not_assumed_to_be_completed(self):
        model = Model()

        def converse(**kwargs):
            response = model(**kwargs)
            response.pop("stopReason")
            return response

        result = self.build([source("a"), source("b")], llm_enabled=True, converse=converse)
        self.assertIn("MODEL_OUTPUT_INVALID", result["partial_reasons"])
        self.assertEqual(result["usage"]["unknown_usage_attempts"], 0)
        self.assertEqual(result["usage"]["input_tokens"], 100)
        self.assertEqual(result["usage"]["output_tokens"], 50)
        self.assertEqual(len(result["graph"]["relations"]), 1)
        self.assertEqual(result["pair_cache"], {})

    def test_none_response_is_invalid_and_usage_unknown_instead_of_ready(self):
        result = self.build([source("a"), source("b")], llm_enabled=True, converse=lambda **_: None)
        self.assertIn("MODEL_OUTPUT_INVALID", result["partial_reasons"])
        self.assertIn("MODEL_USAGE_UNKNOWN", result["partial_reasons"])
        self.assertEqual(result["usage"]["unknown_usage_attempts"], 1)
        self.assertEqual(result["usage"]["model_calls"], 1)
        self.assertEqual(len(result["graph"]["relations"]), 1)
        self.assertEqual(result["pair_cache"], {})

    def test_invalid_model_outputs_and_model_errors_are_explicitly_partial(self):
        for behavior in ("bad_quote", "bad_range", "unsupported", "bad_json", "error"):
            with self.subTest(behavior=behavior):
                model = Model(behavior)
                result = self.build([source("a"), source("b")], llm_enabled=True, converse=model)
                self.assertTrue(result["partial"])
                reason = "MODEL_ERROR" if behavior == "error" else "MODEL_OUTPUT_INVALID"
                self.assertIn(reason, result["partial_reasons"])
                self.assertEqual(len(result["graph"]["relations"]), 1)
                self.assertEqual(result["pair_cache"], {})
                self.assertNotIn("private data", json.dumps(result))
                self.assertEqual(len(model.calls), 2 if behavior == "error" else 1)
                self.assertEqual(result["usage"]["output_tokens_reserved"],
                                 len(model.calls) * engine.MODEL_OUTPUT_TOKENS)
                self.assertEqual(result["usage"]["unknown_usage_attempts"],
                                 len(model.calls) if behavior == "error" else 0)

    def test_missing_model_client_is_partial_instead_of_claiming_completed_analysis(self):
        result = self.build([source("a"), source("b")], llm_enabled=True)
        self.assertEqual(result["partial_reasons"], ["MODEL_UNAVAILABLE"])

    def test_retry_is_reserved_before_dispatch_and_cannot_bypass_output_budget(self):
        model = Model("error")
        with mock.patch.object(engine, "MAX_OUTPUT_TOKENS", engine.MODEL_OUTPUT_TOKENS):
            result = self.build([source("a"), source("b")], llm_enabled=True, converse=model)
        self.assertEqual(len(model.calls), 1)
        self.assertIn("BUDGET_EXCEEDED", result["partial_reasons"])
        self.assertEqual(result["usage"]["output_tokens_reserved"], engine.MODEL_OUTPUT_TOKENS)
        self.assertEqual(result["usage"]["model_retries"], 0)

    def test_input_budget_and_deadline_are_checked_before_model_call(self):
        model = Model()
        with mock.patch.object(engine, "MAX_INPUT_TOKENS", 1):
            result = self.build([source("a"), source("b")], llm_enabled=True, converse=model)
        self.assertEqual(result["partial_reasons"], ["BUDGET_EXCEEDED"])
        self.assertEqual(model.calls, [])
        with self.assertRaises(engine.EngineError) as error:
            self.build(remaining_ms=lambda: 1)
        self.assertEqual(error.exception.code, "DEADLINE_EXCEEDED")

    def test_deadline_after_deterministic_work_retains_partial_graph_without_model_calls(self):
        sources = [source("a"), source("b")]
        model = Model()
        original = engine._Build.discover
        time_remaining = [900_000]

        def expire(build):
            original(build)
            time_remaining[0] = 1

        with mock.patch.object(engine._Build, "discover", expire):
            result = self.build(sources, llm_enabled=True, converse=model,
                                remaining_ms=lambda: time_remaining[0])
        self.assertEqual(result["partial_reasons"], ["DEADLINE_EXCEEDED"])
        self.assertEqual(len(result["graph"]["relations"]), 1)
        self.assertEqual(model.calls, [])

    def test_cache_is_same_group_pair_specific_and_changes_only_affected_pairs(self):
        sources = [source("a"), source("b"), source("c")]
        first = self.build(sources, llm_enabled=True, converse=Model())
        previous = dict(first, group_id=GID)
        sources[2]["version"] = "v2"
        model = Model()
        result = self.build(sources, llm_enabled=True, converse=model, previous_manifest=previous)
        self.assertEqual(result["usage"]["reused_pairs"], 1)
        self.assertEqual(len(model.calls), 2)
        other = Model()
        self.build(sources, llm_enabled=True, converse=other,
                   previous_manifest=dict(previous, group_id="another-group"))
        self.assertEqual(len(other.calls), 3)

    def test_cache_context_includes_descriptions_versions_models_and_prompt(self):
        sources = [source("a"), source("b")]
        first = self.build(sources, llm_enabled=True, converse=Model())
        previous = dict(first, group_id=GID)
        for change in ("description", "common_description", "description_version", "group", "model", "prompt"):
            altered, kwargs = copy.deepcopy(sources), {}
            if change == "group":
                kwargs["description"] = "Changed group context"
            elif change == "model":
                kwargs["model_id"] = "another-allowed-model"
            elif change != "prompt":
                altered[0][change] = 1 if change == "description_version" else "Changed source context"
            model = Model()
            with self.subTest(change=change), mock.patch.object(
                    engine, "PROMPT_VERSION", "v2" if change == "prompt" else engine.PROMPT_VERSION):
                result = self.build(altered, llm_enabled=True, converse=model,
                                    previous_manifest=previous, **kwargs)
            self.assertEqual(result["usage"]["reused_pairs"], 0)
            self.assertEqual(len(model.calls), 1)

    def test_tampered_cache_cannot_inject_quotes(self):
        sources = [source("a"), source("b")]
        first = self.build(sources, llm_enabled=True, converse=Model())
        previous = copy.deepcopy(dict(first, group_id=GID))
        next(iter(previous["pair_cache"].values()))["decisions"][0]["evidence"][0]["quote"] = "forged"
        model = Model()
        result = self.build(sources, llm_enabled=True, converse=model, previous_manifest=previous)
        self.assertEqual(len(model.calls), 1)
        self.assertFalse(result["partial"])

    def test_candidate_limit_is_partial_but_never_discards_original_graph_edges(self):
        sources = [source("a", "REQ-1\nREQ-2\nREQ-3"), source("b", "REQ-1\nREQ-2\nREQ-3")]
        with mock.patch.object(engine, "MAX_CANDIDATES", 1):
            result = self.build(sources)
        self.assertIn("CANDIDATE_LIMIT", result["partial_reasons"])
        self.assertEqual(result["stats"]["candidates"], 1)
        self.assertEqual(len([n for n in result["graph"]["nodes"] if n["original_id"] is not None]), 2)

    def test_group_file_text_and_graph_caps_fail_before_model_dispatch(self):
        cases = [("MAX_FILE_BYTES", 2, "FILE_LIMIT"),
                 ("MAX_TEXT_BYTES", 8, "TEXT_LIMIT"),
                 ("MAX_GRAPH_BYTES", 8, "GRAPH_LIMIT")]
        model = Model()
        for constant, limit, code in cases:
            with self.subTest(constant=constant), mock.patch.object(engine, constant, limit):
                with self.assertRaises(engine.EngineError) as error:
                    self.build([source("a"), source("b")], llm_enabled=True, converse=model)
                self.assertEqual(error.exception.code, code)
        self.assertEqual(model.calls, [])


class SnapshotTests(unittest.TestCase):
    def test_safe_snapshot_retains_utf8_crlf_and_normalizes_dot_prefix(self):
        text = "요구사항 REQ-123\r\n다음 줄\r\n"
        data = tar_bytes([("./docs/plan.md", text.encode()), ("binary.dat", b"\x00\xff")])
        self.assertEqual(engine.parse_snapshot(data), {"docs/plan.md": text})

    def test_large_pdf_original_is_skipped_but_markdown_sidecar_is_kept(self):
        pdf = b"%PDF-1.7\n" + b"0" * (2 * 1024 * 1024) + b"\n%%EOF"
        data = tar_bytes([("documents/plan.PDF", pdf), ("documents/plan.md", b"REQ-123\n")])
        self.assertEqual(engine.parse_snapshot(data), {"documents/plan.md": "REQ-123\n"})
        with mock.patch.object(engine, "MAX_EXPANDED_BYTES", 1024 * 1024):
            with self.assertRaises(engine.EngineError) as error:
                engine.parse_snapshot(data)
        self.assertEqual(error.exception.code, "SNAPSHOT_LIMIT")

    def test_path_traversal_absolute_windows_and_duplicate_paths_are_rejected(self):
        paths = ["../secret", "/secret", "a/../../secret", r"a\secret", "C:/secret", "a//secret"]
        for path in paths:
            with self.subTest(path=path), self.assertRaises(engine.EngineError) as error:
                engine.parse_snapshot(tar_bytes([(path, b"text")]))
            self.assertEqual(error.exception.code, "UNSAFE_SNAPSHOT")
        with self.assertRaises(engine.EngineError) as error:
            engine.parse_snapshot(tar_bytes([("./a", b"one"), ("a", b"two")]))
        self.assertEqual(error.exception.code, "UNSAFE_SNAPSHOT")

    def test_symlinks_hardlinks_devices_and_sparse_files_are_rejected(self):
        for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.CHRTYPE, tarfile.FIFOTYPE):
            with self.subTest(kind=kind), self.assertRaises(engine.EngineError) as error:
                engine.parse_snapshot(tar_bytes([("bad", b"", kind, "/etc/passwd")]))
            self.assertEqual(error.exception.code, "UNSAFE_SNAPSHOT")
        with mock.patch.object(tarfile.TarInfo, "issparse", return_value=True):
            with self.assertRaises(engine.EngineError) as error:
                engine.parse_snapshot(tar_bytes([("a", b"text")]))
        self.assertEqual(error.exception.code, "UNSAFE_SNAPSHOT")

    def test_compressed_expanded_file_text_and_entry_caps(self):
        data = tar_bytes([("a.txt", b"hello"), ("b.txt", b"world")])
        for constant, limit, code in (
            ("MAX_SNAPSHOT_BYTES", len(data) - 1, "SNAPSHOT_LIMIT"),
            ("MAX_EXPANDED_BYTES", 100, "SNAPSHOT_LIMIT"),
            ("MAX_FILE_BYTES", 4, "FILE_LIMIT"),
            ("MAX_TEXT_BYTES", 9, "TEXT_LIMIT"),
            ("MAX_FILES", 1, "SNAPSHOT_LIMIT"),
        ):
            with self.subTest(constant=constant), mock.patch.object(engine, constant, limit):
                with self.assertRaises(engine.EngineError) as error:
                    engine.parse_snapshot(data)
                self.assertEqual(error.exception.code, code)

    def test_corrupt_gzip_or_tar_is_rejected(self):
        good = tar_bytes([("a", b"hello")])
        for data in (b"not an archive", gzip.compress(b"not a tar"), good[:-5]):
            with self.subTest(size=len(data)), self.assertRaises(engine.EngineError) as error:
                engine.parse_snapshot(data)
            self.assertEqual(error.exception.code, "INVALID_SNAPSHOT")


if __name__ == "__main__":
    unittest.main()
