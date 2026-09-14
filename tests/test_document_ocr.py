"""Offline contract tests: no AWS calls and no additional test dependencies."""

import base64
import copy
import hashlib
import io
import json
import os
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from cdk.build_scripts import document_ocr as ocr


PDF = b"%PDF-1.7\nsingle-page-test-fixture\n%%EOF"
SOURCE_SHA = hashlib.sha256(b"original document").hexdigest()
BODY = "# 퇴직연금\n\n| 세금 | 수수료 |\n| --- | --- |\n| 15.4% | 1,200원 |\n\n가. 단위: 백만원"
USAGE = {"inputTokens": 2512, "outputTokens": 1025, "totalTokens": 3537, "cacheReadInputTokens": 0}


def response(text=BODY, status="transcribed", stop="end_turn", content=None, usage=None):
    return {
        "output": {"message": {"role": "assistant", "content": content if content is not None else [
            {"text": json.dumps({"status": status, "text": text}, ensure_ascii=False)}
        ]}},
        "stopReason": stop,
        "usage": copy.deepcopy(USAGE if usage is None else usage),
        "metrics": {"latencyMs": 13000},
        "ResponseMetadata": {"RequestId": "offline-request", "HTTPStatusCode": 200},
    }


class SDKError(Exception):
    def __init__(self, code, message="offline SDK failure", usage=None):
        super().__init__(message)
        self.response = {"Error": {"Code": code, "Message": message}}
        if usage is not None:
            self.response["usage"] = usage


class FakeBedrock:
    def __init__(self, outcomes=None, handler=None):
        self.outcomes = list(outcomes or [])
        self.handler = handler
        self.calls = []
        self.lock = threading.Lock()

    def converse(self, **request):
        with self.lock:
            self.calls.append(copy.deepcopy(request))
            outcome = self.outcomes.pop(0) if self.outcomes else None
        if self.handler:
            return self.handler(request)
        if isinstance(outcome, Exception):
            raise outcome
        return copy.deepcopy(outcome if outcome is not None else response())


class FakeS3:
    def __init__(self):
        self.objects = {}
        self.gets = []
        self.puts = []
        self.bodies = []
        self.get_error = None
        self.put_error = None
        self.lock = threading.Lock()

    def get_object(self, **request):
        with self.lock:
            self.gets.append(request)
            if self.get_error:
                raise self.get_error
            key = (request["Bucket"], request["Key"])
            if key not in self.objects:
                raise SDKError("NoSuchKey")
            body = io.BytesIO(self.objects[key])
            self.bodies.append(body)
            return {"Body": body}

    def put_object(self, **request):
        with self.lock:
            self.puts.append(request)
            if self.put_error:
                raise self.put_error
            self.objects[request["Bucket"], request["Key"]] = request["Body"]
            return {}


class DocumentOCRTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bedrock = FakeBedrock()

    def make_ocr(self, name="cache", **kwargs):
        options = {
            "model_id": ocr.DEFAULT_MODEL_ID,
            "cache_dir": self.root / name,
            "audit_path": self.root / "audit.jsonl",
            "client": self.bedrock,
        }
        options.update(kwargs)
        return ocr.DocumentOCR(**options)

    def audit(self):
        return [json.loads(line) for line in (self.root / "audit.jsonl").read_text().splitlines()]

    def assert_page_error(self, engine, *, code=None, pdf=PDF, sha=SOURCE_SHA, page=1, **kwargs):
        with self.assertRaises(ocr.OCRPageError) as caught:
            engine.process_page(pdf, sha, page, **kwargs)
        if code:
            self.assertEqual(caught.exception.code, code)
        self.assertEqual(caught.exception.page_number, page)
        return caught.exception

    def test_visual_request_and_verbatim_korean_table(self):
        engine = self.make_ocr()
        result = engine.process_page(PDF, SOURCE_SHA, 1)
        request = self.bedrock.calls[0]
        self.assertEqual(request["modelId"], ocr.DEFAULT_MODEL_ID)
        self.assertEqual(request["inferenceConfig"], {"maxTokens": 16384})
        self.assertEqual(request["messages"], [{"role": "user", "content": [
            {"document": {"format": "pdf", "name": "page", "source": {"bytes": PDF},
                          "citations": {"enabled": True}}},
            {"text": ocr.PAGE_INSTRUCTION},
        ]}])
        self.assertEqual(request["system"], [{"text": ocr.SYSTEM_PROMPT}])
        self.assertNotIn("toolConfig", request)
        self.assertEqual(result["text"], BODY)
        self.assertEqual(result["status"], "transcribed")
        self.assertFalse(result["cache_hit"])
        self.assertEqual(result["usage"], USAGE)
        self.assertEqual(engine.snapshot()["new_pages"], 1)

    def test_plain_and_citation_text_fragments_are_joined_in_order(self):
        payload = json.dumps({"status": "transcribed", "text": BODY}, ensure_ascii=False)
        self.bedrock.outcomes = [response(content=[
            {"text": payload[:17]},
            {"citationsContent": {"content": [{"text": payload[17:45]}, {"text": payload[45:73]}],
                                  "citations": [{"sourceContent": [{"text": "citation metadata is not output"}]}]}},
            {"text": payload[73:]},
        ])]
        self.assertEqual(self.make_ocr().process_page(PDF, SOURCE_SHA, 1)["text"], BODY)

    def test_whole_response_json_fence_from_real_poc_shape(self):
        payload = json.dumps({"status": "transcribed", "text": BODY}, ensure_ascii=False, indent=2)
        for newline in ("\n", "\r\n"):
            with self.subTest(newline=newline):
                self.bedrock.outcomes = [response(content=[{"text": f" \n```json{newline}{payload}{newline}```\n "}])]
                engine = self.make_ocr(name=f"fence-{len(newline)}")
                self.assertEqual(engine.process_page(PDF, SOURCE_SHA, 1)["text"], BODY)

    def test_fence_can_span_citation_blocks(self):
        payload = json.dumps({"status": "transcribed", "text": BODY}, ensure_ascii=False)
        self.bedrock.outcomes = [response(content=[
            {"text": "```json\n"},
            {"citationsContent": {"content": [{"text": payload}], "citations": []}},
            {"text": "\n```"},
        ])]
        self.assertEqual(self.make_ocr().process_page(PDF, SOURCE_SHA, 1)["text"], BODY)

    def test_only_end_turn_is_accepted_and_rejected_usage_is_audited(self):
        for reason in ("max_tokens", "stop_sequence", "tool_use", "guardrail_intervened", "content_filtered", None):
            with self.subTest(reason=reason):
                raw = response(stop=reason)
                self.bedrock.outcomes = [raw]
                before = len(self.bedrock.calls)
                engine = self.make_ocr(name=str(reason))
                self.assert_page_error(engine, code="invalid_stop_reason")
                self.assertEqual(len(self.bedrock.calls) - before, 1)
                self.assertEqual(list(engine.cache_dir.iterdir()), [])
                self.assertEqual(engine.snapshot()["usage"], USAGE)
                self.assertEqual(engine.snapshot()["errors"], 1)
                self.assertEqual(self.audit()[-1]["response"], raw)
                self.assertEqual(self.audit()[-1]["usage"], USAGE)

    def test_invalid_json_empty_unreadable_and_surrounding_prose_rejected(self):
        good = '{"status":"transcribed","text":"내용"}'
        bad_payloads = [
            ("not JSON", "invalid_json"),
            (f"Here is the result:\n{good}", "invalid_json"),
            (f"Here is the result:\n```json\n{good}\n```", "invalid_json"),
            (f"```json\n{good}\n```\nDone.", "invalid_json"),
            (f"```python\n{good}\n```", "invalid_json"),
            (good + good, "invalid_json"),
            ('{"status":"transcribed","text":""}', "empty_transcription"),
            ('{"status":"transcribed","text":"  \\n"}', "empty_transcription"),
            ('{"status":"blank","text":"not blank"}', "invalid_blank"),
            ('{"status":"unreadable","text":"[UNREADABLE]"}', "unreadable"),
            ('{"status":"transcribed","text":"[illegible: glyph]"}', "unreadable"),
            ('{"status":"unknown","text":"content"}', "invalid_status"),
            ('{"status":"blank","text":null}', "invalid_json"),
            ('{"status":true,"text":"content"}', "invalid_json"),
            ('{"status":"blank","text":"","extra":0}', "invalid_json"),
            ('{"status":"blank","status":"transcribed","text":"content"}', "invalid_json"),
            ('{"status":"blank","text":NaN}', "invalid_json"),
            ('{"status":"transcribed","text":"bad\\u0000text"}', "invalid_text"),
            ('[]', "invalid_json"),
        ]
        for index, (payload, code) in enumerate(bad_payloads):
            with self.subTest(payload=payload):
                self.bedrock.outcomes = [response(content=[{"text": payload}])]
                engine = self.make_ocr(name=f"invalid-{index}")
                self.assert_page_error(engine, code=code)
                self.assertEqual(engine.snapshot()["api_attempts"], 1)
                self.assertEqual(list(engine.cache_dir.iterdir()), [])

    def test_reasoning_metadata_is_not_transcribed(self):
        payload = json.dumps({"status": "transcribed", "text": BODY})
        self.bedrock.outcomes = [response(content=[
            {"reasoningContent": {"reasoningText": {"text": "NOT SOURCE TEXT", "signature": "opaque"}}},
            {"text": payload},
        ])]
        result = self.make_ocr().process_page(PDF, SOURCE_SHA, 1)
        self.assertEqual(result["text"], BODY)
        self.assertNotIn("NOT SOURCE TEXT", result["text"])

    def test_partial_uncertainty_is_preserved_and_counted_in_cache(self):
        text = "매매 시간 09:00~15:30. 화면 아이콘 [UNREADABLE]; 단위 [illegible: glyph]."
        self.bedrock.outcomes = [response(text=text)]
        engine = self.make_ocr()
        first = engine.process_page(PDF, SOURCE_SHA, 1)
        cached = engine.process_page(PDF, SOURCE_SHA, 1)
        self.assertEqual(first["text"], text)
        self.assertEqual(first["uncertain_spans"], 2)
        self.assertEqual(cached["uncertain_spans"], 2)
        self.assertTrue(cached["cache_hit"])
        self.assertEqual(len(self.bedrock.calls), 1)

    def test_tool_calls_and_malformed_content_rejected(self):
        bad_content = [
            [{"toolUse": {"toolUseId": "x", "name": "unrequested", "input": {}}}],
            [{"text": '{"status":"blank","text":""}', "toolUse": {}}],
            [{"toolResult": {}}],
            [{"citationsContent": {"content": [{"text": 3}]}}],
            [{"citationsContent": {"content": "wrong"}}],
            [{"image": {}}],
            [{"text": None}],
            [None],
            [],
        ]
        for index, content in enumerate(bad_content):
            with self.subTest(content=content):
                self.bedrock.outcomes = [response(content=content)]
                engine = self.make_ocr(name=f"content-{index}")
                self.assert_page_error(engine)
                self.assertEqual(engine.snapshot()["api_attempts"], 1)
                self.assertEqual(list(engine.cache_dir.iterdir()), [])

    def test_blank_is_a_cacheable_success(self):
        self.bedrock.outcomes = [response(text="", status="blank")]
        engine = self.make_ocr()
        first = engine.process_page(PDF, SOURCE_SHA, 1)
        second = engine.process_page(PDF, SOURCE_SHA, 1)
        self.assertEqual((first["status"], first["text"]), ("blank", ""))
        self.assertTrue(second["cache_hit"])
        self.assertEqual(engine.snapshot()["blank_pages"], 2)
        self.assertEqual(len(self.bedrock.calls), 1)

    def test_local_cache_hit_costs_zero_and_retains_original_raw_usage(self):
        raw_usage = {**USAGE, "cacheWriteInputTokens": 7,
                     "cacheDetails": [{"inputTokens": 7, "ttl": "1h"}]}
        raw = response(usage=raw_usage)
        self.bedrock.outcomes = [raw]
        first = self.make_ocr().process_page(PDF, SOURCE_SHA, 1)
        fresh = self.make_ocr(max_new_pages=0)
        hit = fresh.process_page(PDF, SOURCE_SHA, 1)
        self.assertTrue(hit["cache_hit"])
        self.assertEqual(hit["cache_source"], "local")
        self.assertEqual(hit["cache_key"], first["cache_key"])
        self.assertTrue(all(value == 0 for value in hit["usage"].values()))
        self.assertEqual(hit["original_usage"], first["usage"])
        self.assertEqual(hit["raw_usage"], raw_usage)
        self.assertEqual(self.audit()[0]["response"], raw)
        self.assertEqual(self.audit()[0]["usage"], raw_usage)
        self.assertEqual(len(self.audit()), 1)
        self.assertEqual(fresh.snapshot()["new_pages"], 0)
        self.assertEqual(fresh.snapshot()["usage"], ocr._zero_usage())
        self.assertEqual(len(self.bedrock.calls), 1)

    def test_every_request_binding_changes_the_key(self):
        keys = [self.make_ocr().process_page(PDF, SOURCE_SHA, 1)["cache_key"]]
        keys.append(self.make_ocr().process_page(PDF, hashlib.sha256(b"other").hexdigest(), 1)["cache_key"])
        keys.append(self.make_ocr().process_page(PDF, SOURCE_SHA, 2)["cache_key"])
        keys.append(self.make_ocr().process_page(PDF + b"\n", SOURCE_SHA, 1)["cache_key"])
        keys.append(self.make_ocr(model_id="other-model").process_page(PDF, SOURCE_SHA, 1)["cache_key"])
        keys.append(self.make_ocr(max_tokens=2048).process_page(PDF, SOURCE_SHA, 1)["cache_key"])
        with mock.patch.object(ocr, "PROMPT_SHA256", "f" * 64):
            keys.append(self.make_ocr().process_page(PDF, SOURCE_SHA, 1)["cache_key"])
        with mock.patch.object(ocr, "PROMPT_VERSION", "next-prompt"):
            keys.append(self.make_ocr().process_page(PDF, SOURCE_SHA, 1)["cache_key"])
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(len(self.bedrock.calls), len(keys))

    def test_cache_corruption_types_hash_version_and_rebound_payload_fail_closed(self):
        engine = self.make_ocr()
        result = engine.process_page(PDF, SOURCE_SHA, 1)
        path = engine.cache_dir / f"{result['cache_key']}.json"
        original = json.loads(path.read_text())

        def changed(field, value, *, resign=True):
            entry = copy.deepcopy(original)
            entry[field] = value
            if resign:
                entry["payload_sha256"] = ocr._digest({k: v for k, v in entry.items() if k != "payload_sha256"})
            return json.dumps(entry)

        corruptions = [
            "{truncated", "null",
            changed("result", {"status": "transcribed", "text": "tampered"}, resign=False),
            changed("bindings", {**original["bindings"], "source_sha256": "0" * 64}),
            changed("bindings", {**original["bindings"], "page_number": True}),
            changed("bindings", {**original["bindings"], "model_id": "other-model"}),
            changed("cache_version", 2), changed("cache_version", True),
            changed("cache_key", "0" * 64),
            changed("usage", {**original["usage"], "inputTokens": True}),
            changed("usage", {**original["usage"], "outputTokens": -1}),
            changed("usage", []), changed("raw_usage", []),
            changed("raw_usage", {"inputTokens": "invalid", "outputTokens": 0, "totalTokens": 0}),
            changed("result", {"status": "transcribed", "text": ""}),
            changed("result", {"status": "unreadable", "text": "partial"}),
        ]
        for data in corruptions:
            with self.subTest(data=data[:80]):
                path.write_text(data)
                self.assert_page_error(engine, code="invalid_cache")
        self.assertEqual(len(self.bedrock.calls), 1)
        self.assertEqual(engine.snapshot()["errors"], len(corruptions))

    def test_remote_miss_put_and_fresh_build_hit(self):
        s3 = FakeS3()
        options = {"cache_bucket": "graph-bucket", "cache_prefix": "repos/repo/document-ocr-cache/v1/",
                   "s3_client": s3}
        first = self.make_ocr(**options).process_page(PDF, SOURCE_SHA, 1)
        expected_key = f"repos/repo/document-ocr-cache/v1/{first['cache_key']}.json"
        self.assertEqual(s3.gets, [{"Bucket": "graph-bucket", "Key": expected_key}])
        self.assertEqual(s3.puts[0]["Key"], expected_key)
        self.assertEqual(s3.puts[0]["ContentType"], "application/json")
        fresh = self.make_ocr(name="fresh-build", max_new_pages=0, **options)
        hit = fresh.process_page(PDF, SOURCE_SHA, 1)
        self.assertEqual(hit["cache_source"], "remote")
        self.assertEqual(hit["text"], BODY)
        self.assertEqual(hit["original_usage"], USAGE)
        self.assertTrue(all(value == 0 for value in hit["usage"].values()))
        self.assertEqual(fresh.snapshot()["remote_cache_hits"], 1)
        self.assertEqual(fresh.snapshot()["new_pages"], 0)
        self.assertTrue(all(body.closed for body in s3.bodies))
        self.assertEqual(fresh.process_page(PDF, SOURCE_SHA, 1)["cache_source"], "local")
        self.assertEqual(len(s3.gets), 2)
        self.assertEqual(len(self.bedrock.calls), 1)

    def test_only_remote_not_found_errors_are_misses(self):
        for index, code in enumerate(("AccessDenied", "NoSuchBucket", "ExpiredToken", "InternalError")):
            with self.subTest(code=code):
                s3 = FakeS3()
                s3.get_error = SDKError(code)
                engine = self.make_ocr(name=f"denied-{index}", s3_client=s3,
                                       cache_bucket="bucket", cache_prefix="prefix")
                self.assert_page_error(engine, code="remote_cache_read_error")
                self.assertEqual(engine.snapshot()["new_pages"], 0)
        self.assertEqual(self.bedrock.calls, [])
        for index, code in enumerate(("NoSuchKey", "404", "NotFound")):
            with self.subTest(code=code):
                s3 = FakeS3()
                s3.get_error = SDKError(code)
                engine = self.make_ocr(name=f"missing-{index}", s3_client=s3,
                                       cache_bucket="bucket", cache_prefix="prefix")
                self.assertFalse(engine.process_page(PDF, SOURCE_SHA, 1)["cache_hit"])

    def test_missing_credentials_are_not_a_remote_cache_miss(self):
        class NoCredentialsError(Exception):
            pass

        s3 = FakeS3()
        s3.get_error = NoCredentialsError("no credentials")
        engine = self.make_ocr(s3_client=s3, cache_bucket="bucket", cache_prefix="prefix")
        self.assert_page_error(engine, code="remote_cache_read_error")
        self.assertEqual(self.bedrock.calls, [])

    def test_corrupt_remote_cache_fails_without_regeneration(self):
        s3 = FakeS3()
        options = {"s3_client": s3, "cache_bucket": "bucket", "cache_prefix": "prefix"}
        self.make_ocr(**options).process_page(PDF, SOURCE_SHA, 1)
        object_key = next(iter(s3.objects))
        s3.objects[object_key] = b'{"truncated":'
        fresh = self.make_ocr(name="fresh", **options)
        self.assert_page_error(fresh, code="invalid_cache")
        self.assertEqual(len(self.bedrock.calls), 1)
        self.assertEqual(list(fresh.cache_dir.iterdir()), [])
        self.assertTrue(s3.bodies[-1].closed)

    def test_local_read_errors_are_not_misses(self):
        engine = self.make_ocr()
        with mock.patch.object(Path, "read_bytes", side_effect=PermissionError("denied")):
            self.assert_page_error(engine, code="cache_read_error")
        self.assertEqual(self.bedrock.calls, [])

    def test_remote_write_failure_is_explicit_and_does_not_cache_partial_success(self):
        s3 = FakeS3()
        s3.put_error = SDKError("AccessDenied")
        engine = self.make_ocr(s3_client=s3, cache_bucket="bucket", cache_prefix="prefix")
        self.assert_page_error(engine, code="remote_cache_write_error")
        self.assertEqual(list(engine.cache_dir.iterdir()), [])
        self.assertEqual(s3.objects, {})
        self.assertEqual(len(self.audit()), 1)
        self.assertEqual(engine.snapshot()["usage"], USAGE)

    def test_remote_success_survives_local_failure_and_next_build_reuses_it(self):
        s3 = FakeS3()
        options = {"s3_client": s3, "cache_bucket": "bucket", "cache_prefix": "prefix"}
        engine = self.make_ocr(**options)
        with mock.patch.object(ocr.os, "replace", side_effect=OSError("disk error")):
            self.assert_page_error(engine, code="cache_write_error")
        self.assertEqual(list(engine.cache_dir.iterdir()), [])
        self.assertEqual(len(s3.objects), 1)
        fresh = self.make_ocr(name="fresh", max_new_pages=0, **options)
        self.assertEqual(fresh.process_page(PDF, SOURCE_SHA, 1)["cache_source"], "remote")
        self.assertEqual(len(self.bedrock.calls), 1)

    def test_retries_are_bounded_and_all_raw_error_usage_is_counted(self):
        failed_usage = {"inputTokens": 10, "outputTokens": 2, "totalTokens": 12}
        self.bedrock.outcomes = [
            SDKError("ThrottlingException", usage=failed_usage),
            SDKError("ModelNotReadyException"),
            response(),
        ]
        engine = self.make_ocr(max_new_pages=1)
        with mock.patch.object(ocr.time, "sleep") as sleep:
            result = engine.process_page(PDF, SOURCE_SHA, 1)
        self.assertEqual(sleep.call_args_list, [mock.call(0.25), mock.call(0.5)])
        self.assertEqual(result["usage"], {**USAGE, "inputTokens": 2522, "outputTokens": 1027, "totalTokens": 3549})
        self.assertEqual(result["raw_usage"], USAGE)
        stats = engine.snapshot()
        self.assertEqual((stats["new_pages"], stats["api_attempts"], stats["retries"], stats["attempt_errors"]),
                         (1, 3, 2, 2))
        self.assertEqual(stats["errors"], 0)
        self.assertEqual(stats["usage"], result["usage"])
        audit = self.audit()
        self.assertEqual([row["attempt"] for row in audit], [1, 2, 3])
        self.assertEqual(audit[0]["error"]["response"]["usage"], failed_usage)
        self.assertEqual(audit[0]["usage"], failed_usage)
        self.assertIsNone(audit[-1]["error"])
        cached = engine.process_page(PDF, SOURCE_SHA, 1)
        self.assertEqual(cached["original_usage"], result["usage"])
        self.assertTrue(all(value == 0 for value in cached["usage"].values()))

    def test_repeated_transient_errors_stop_at_three_attempts(self):
        self.bedrock.outcomes = [SDKError("ServiceUnavailableException") for _ in range(4)]
        engine = self.make_ocr()
        with mock.patch.object(ocr.time, "sleep"):
            self.assert_page_error(engine, code="api_error")
        self.assertEqual(len(self.bedrock.calls), 3)
        self.assertEqual(len(self.audit()), 3)
        self.assertEqual(engine.snapshot()["new_pages"], 1)
        self.assertEqual(engine.snapshot()["errors"], 1)
        self.assertEqual(list(engine.cache_dir.iterdir()), [])

    def test_nontransient_errors_are_not_retried(self):
        for index, code in enumerate(("AccessDeniedException", "ValidationException", "ResourceNotFoundException",
                                      "ModelErrorException")):
            with self.subTest(code=code):
                self.bedrock.outcomes = [SDKError(code)]
                engine = self.make_ocr(name=f"failure-{index}")
                with mock.patch.object(ocr.time, "sleep") as sleep:
                    self.assert_page_error(engine, code="api_error")
                sleep.assert_not_called()
                self.assertEqual(engine.snapshot()["api_attempts"], 1)

    def test_network_timeout_is_retried(self):
        class ReadTimeoutError(Exception):
            pass

        self.bedrock.outcomes = [ReadTimeoutError("timeout"), response()]
        with mock.patch.object(ocr.time, "sleep"):
            result = self.make_ocr().process_page(PDF, SOURCE_SHA, 1)
        self.assertEqual(result["text"], BODY)
        self.assertEqual(len(self.bedrock.calls), 2)

    def test_budget_is_checked_before_api_and_cache_hits_are_free(self):
        engine = self.make_ocr(max_new_pages=1)
        engine.process_page(PDF, SOURCE_SHA, 1)
        self.assertTrue(engine.process_page(PDF, SOURCE_SHA, 1)["cache_hit"])
        with self.assertRaises(ocr.OCRBudgetExceeded) as caught:
            engine.process_page(PDF, SOURCE_SHA, 2)
        self.assertEqual(caught.exception.code, "budget_exceeded")
        self.assertEqual(caught.exception.page_number, 2)
        self.assertEqual(len(self.bedrock.calls), 1)
        self.assertEqual(engine.snapshot()["new_pages"], 1)
        empty_budget = self.make_ocr(name="zero", max_new_pages=0)
        with self.assertRaises(ocr.OCRBudgetExceeded):
            empty_budget.process_page(PDF, SOURCE_SHA, 1)
        self.assertEqual(empty_budget.snapshot()["api_attempts"], 0)

    def test_four_distinct_pages_can_generate_concurrently_and_audit_is_valid_jsonl(self):
        barrier = threading.Barrier(4)

        def handler(_):
            barrier.wait(timeout=5)
            return response()

        self.bedrock.handler = handler
        engine = self.make_ocr(max_new_pages=4)
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda page: engine.process_page(PDF, SOURCE_SHA, page), range(1, 5)))
        self.assertEqual(len({result["cache_key"] for result in results}), 4)
        stats = engine.snapshot()
        self.assertEqual((stats["new_pages"], stats["api_attempts"], stats["errors"]), (4, 4, 0))
        self.assertEqual(stats["usage"], {key: value * 4 for key, value in USAGE.items()})
        self.assertEqual(len(self.audit()), 4)
        self.assertEqual(len(list(engine.cache_dir.glob("*.json"))), 4)

    def test_same_page_concurrency_deduplicates_paid_generation(self):
        start = threading.Barrier(4)
        engine = self.make_ocr(max_new_pages=1)

        def call(index):
            if index < 4:
                start.wait(timeout=5)
            return engine.process_page(PDF, SOURCE_SHA, 1)

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(call, range(16)))
        self.assertEqual(sum(result["cache_hit"] for result in results), 15)
        self.assertEqual(len(self.bedrock.calls), 1)
        self.assertEqual(engine.snapshot()["new_pages"], 1)
        self.assertEqual(engine.snapshot()["cache_hits"], 15)
        self.assertEqual(len(self.audit()), 1)

    def test_concurrent_budget_cannot_be_oversubscribed(self):
        engine = self.make_ocr(max_new_pages=3)

        def call(page):
            try:
                return engine.process_page(PDF, SOURCE_SHA, page)
            except ocr.OCRBudgetExceeded:
                return None

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(call, range(1, 25)))
        self.assertEqual(sum(result is not None for result in results), 3)
        self.assertEqual(len(self.bedrock.calls), 3)
        self.assertEqual(engine.snapshot()["new_pages"], 3)
        self.assertEqual(engine.snapshot()["errors"], 21)

    def test_snapshot_is_a_copy(self):
        engine = self.make_ocr()
        engine.process_page(PDF, SOURCE_SHA, 1)
        snapshot = engine.snapshot()
        snapshot["new_pages"] = -1
        snapshot["usage"]["inputTokens"] = -1
        self.assertEqual(engine.snapshot()["new_pages"], 1)
        self.assertEqual(engine.snapshot()["usage"]["inputTokens"], USAGE["inputTokens"])

    def test_document_cap_and_invalid_input_fail_before_any_call(self):
        engine = self.make_ocr()
        self.assert_page_error(engine, pdf=b"x" * (ocr.MAX_DOCUMENT_BYTES + 1), code="document_too_large")
        for value in (b"", None, "PDF", bytearray(PDF)):
            self.assert_page_error(engine, pdf=value, code="invalid_pdf")
        for value in (0, -1, True, "1"):
            self.assert_page_error(engine, page=value, code="invalid_page")
        for value in ("not-a-sha", "", None, "g" * 64):
            self.assert_page_error(engine, sha=value, code="invalid_source_sha256")
        self.assertEqual(self.bedrock.calls, [])
        self.assertEqual(engine.snapshot()["new_pages"], 0)
        # The exact conservative byte limit is accepted.
        result = engine.process_page(b"x" * ocr.MAX_DOCUMENT_BYTES, SOURCE_SHA, 1)
        self.assertEqual(result["status"], "transcribed")

    def test_audit_redacts_pdf_binary_base64_and_credentials_but_keeps_raw_response_fields(self):
        secret = "offline-secret-do-not-log"
        message = f"{PDF!r} {PDF.decode()} {base64.b64encode(PDF).decode()} {secret}"
        error = SDKError("ValidationException", message)
        error.response["binary"] = PDF
        error.response["nested"] = {"Authorization": "Bearer private", "AWS_SECRET_ACCESS_KEY": secret}
        self.bedrock.outcomes = [error]
        with mock.patch.dict(os.environ, {"AWS_SECRET_ACCESS_KEY": secret}):
            self.assert_page_error(self.make_ocr(), code="api_error")
        raw = (self.root / "audit.jsonl").read_text()
        self.assertNotIn(base64.b64encode(PDF).decode(), raw)
        self.assertNotIn("%PDF", raw)
        self.assertNotIn(secret, raw)
        self.assertNotIn("private", raw)
        event = self.audit()[0]
        self.assertEqual(event["error"]["response"]["Error"]["Code"], "ValidationException")
        self.assertEqual(event["error"]["response"]["binary"], "[REDACTED BINARY]")
        self.assertNotIn("request", event)

    def test_audit_failure_is_fatal_without_success_cache(self):
        engine = self.make_ocr()
        engine.audit_path.mkdir()
        self.assert_page_error(engine, code="audit_error")
        self.assertEqual(list(engine.cache_dir.iterdir()), [])
        self.assertEqual(engine.snapshot()["usage"], USAGE)

    def test_invalid_usage_is_not_cached_and_raw_usage_is_audited(self):
        usage = {"inputTokens": True, "outputTokens": 1, "totalTokens": 1}
        self.bedrock.outcomes = [response(usage=usage)]
        engine = self.make_ocr()
        self.assert_page_error(engine, code="invalid_usage")
        self.assertEqual(self.audit()[0]["usage"], usage)
        self.assertEqual(list(engine.cache_dir.iterdir()), [])

    def test_audit_cannot_be_inside_source_tree(self):
        source = self.root / "src"
        with mock.patch.dict(os.environ, {"CONVERT_DOCS_SRC": str(source)}):
            with self.assertRaisesRegex(ValueError, "outside"):
                self.make_ocr(audit_path=source / "audit.jsonl")

    def test_from_env_defaults_and_repo_cache_binding(self):
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(ocr.DocumentOCR, "__init__", return_value=None) as init:
            ocr.DocumentOCR.from_env()
        init.assert_called_once_with(
            model_id=ocr.DEFAULT_MODEL_ID, cache_dir=ocr.DEFAULT_CACHE_DIR,
            cache_bucket=None, cache_prefix=None, max_new_pages=300, max_tokens=16384,
            audit_path=ocr.DEFAULT_AUDIT_PATH,
        )
        env = {
            "LLM_MODEL": "test-model", "DOCUMENT_OCR_CACHE_DIR": str(self.root / "env-cache"),
            "DOCUMENT_OCR_AUDIT_PATH": str(self.root / "env-audit.jsonl"),
            "DOCUMENT_OCR_MAX_NEW_PAGES": "7", "DOCUMENT_OCR_MAX_TOKENS": "2000",
            "GRAPH_BUCKET": "graph", "REPO_ID": "repo-123",
            "AWS_REGION": "us-east-1", "AWS_DEFAULT_REGION": "eu-west-1",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            engine = ocr.DocumentOCR.from_env()
        self.assertEqual(engine.model_id, "test-model")
        self.assertEqual(engine.region, "us-east-1")
        self.assertEqual(engine.cache_prefix, "repos/repo-123/document-ocr-cache/v1/")
        self.assertEqual((engine.max_new_pages, engine.max_tokens), (7, 2000))
        self.assertEqual(engine.cache_dir, (self.root / "env-cache").resolve())
        self.assertEqual(engine.audit_path, (self.root / "env-audit.jsonl").resolve())

    def test_region_fallback_and_invalid_configuration(self):
        with mock.patch.dict(os.environ, {"AWS_DEFAULT_REGION": "eu-west-1"}, clear=True):
            self.assertEqual(self.make_ocr().region, "eu-west-1")
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(self.make_ocr().region, "ap-northeast-2")
        for options in ({"max_new_pages": -1}, {"max_new_pages": True}, {"max_tokens": 0},
                        {"model_id": ""}, {"cache_bucket": "missing-prefix"}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.make_ocr(**options)
        for env in ({"GRAPH_BUCKET": "bucket"}, {"REPO_ID": "repo"},
                    {"GRAPH_BUCKET": "bucket", "REPO_ID": "../other"}):
            with mock.patch.dict(os.environ, env, clear=True), self.assertRaises(ValueError):
                ocr.DocumentOCR.from_env()

    def test_lazy_sdk_clients_are_thread_local_with_hidden_retries_disabled(self):
        clients = []
        creation_lock = threading.Lock()

        def create_client(service, config):
            client = SimpleNamespace(service=service, config=config)
            with creation_lock:
                clients.append(client)
            return client

        session = mock.Mock(side_effect=lambda **_: SimpleNamespace(client=create_client))
        config = mock.Mock(side_effect=lambda **kwargs: kwargs)
        modules = {
            "boto3": SimpleNamespace(session=SimpleNamespace(Session=session)),
            "botocore.config": SimpleNamespace(Config=config),
        }
        engine = self.make_ocr(client=None)
        barrier = threading.Barrier(4)

        def call(_):
            barrier.wait(timeout=5)
            first = engine._sdk_client("bedrock-runtime")
            self.assertIs(first, engine._sdk_client("bedrock-runtime"))
            s3 = engine._sdk_client("s3")
            self.assertIs(s3, engine._sdk_client("s3"))
            return first

        with mock.patch.dict("sys.modules", modules):
            with ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(call, range(4)))
        self.assertEqual(len({id(result) for result in results}), 4)
        self.assertEqual(len(clients), 8)
        for client in clients:
            self.assertEqual(client.config["retries"]["total_max_attempts"], 1)
        for call_args in session.call_args_list:
            self.assertEqual(call_args.kwargs["region_name"], engine.region)

    def test_expired_deadline_or_cancellation_prevents_cache_and_model_calls(self):
        cancelled = threading.Event()
        cancelled.set()
        for name, kwargs, code in (
            ("expired", {"deadline": 99}, "deadline_exceeded"),
            ("cancelled", {"cancel_event": cancelled}, "cancelled"),
        ):
            with self.subTest(name=name), mock.patch.object(ocr.time, "monotonic", return_value=100):
                s3 = FakeS3()
                engine = self.make_ocr(name=name, s3_client=s3, cache_bucket="bucket", cache_prefix="prefix")
                with mock.patch.object(engine, "_read_cache") as read:
                    self.assert_page_error(engine, code=code, **kwargs)
                read.assert_not_called()
                self.assertEqual(s3.gets, [])
                self.assertEqual(s3.puts, [])
                stats = engine.snapshot()
                self.assertEqual((stats["new_pages"], stats["api_attempts"], stats["usage_unavailable_attempts"]),
                                 (0, 0, 0))
                self.assertEqual(stats["timeout_errors" if name == "expired" else "cancelled_errors"], 1)
                self.assertEqual(self.audit()[-1]["error"]["code"], code)
        self.assertEqual(self.bedrock.calls, [])

    def test_deadline_gate_applies_even_to_an_existing_local_hit(self):
        engine = self.make_ocr()
        engine.process_page(PDF, SOURCE_SHA, 1)
        with mock.patch.object(ocr.time, "monotonic", return_value=100), \
                mock.patch.object(Path, "read_bytes") as read:
            self.assert_page_error(engine, deadline=99, code="deadline_exceeded")
        read.assert_not_called()
        self.assertEqual(engine.snapshot()["cache_hits"], 0)

    def test_expiry_during_local_cache_read_prevents_generation(self):
        now = [100.0]
        engine = self.make_ocr()

        def miss():
            now[0] = 102
            raise FileNotFoundError

        with mock.patch.object(ocr.time, "monotonic", side_effect=lambda: now[0]), \
                mock.patch.object(Path, "read_bytes", side_effect=miss):
            self.assert_page_error(engine, deadline=101, code="deadline_exceeded")
        self.assertEqual(self.bedrock.calls, [])
        self.assertEqual(engine.snapshot()["new_pages"], 0)

    def test_cancellation_before_retry_preserves_known_usage_and_stops_new_calls(self):
        cancelled = threading.Event()
        failed_usage = {"inputTokens": 9, "outputTokens": 2, "totalTokens": 11}

        def handler(_):
            cancelled.set()
            raise SDKError("ThrottlingException", usage=failed_usage)

        self.bedrock.handler = handler
        engine = self.make_ocr()
        with mock.patch.object(cancelled, "wait") as wait, mock.patch.object(ocr.time, "sleep") as sleep:
            self.assert_page_error(engine, cancel_event=cancelled, code="cancelled")
        wait.assert_not_called()
        sleep.assert_not_called()
        stats = engine.snapshot()
        self.assertEqual((stats["api_attempts"], stats["retries"], stats["usage_unavailable_attempts"]), (1, 0, 0))
        self.assertEqual(stats["usage"], failed_usage)
        self.assertEqual(stats["cancelled_errors"], 1)
        self.assertEqual(self.audit()[0]["usage"], failed_usage)
        self.assertEqual(self.audit()[0]["error"]["response"]["Error"]["Code"], "ThrottlingException")
        self.assertEqual(self.audit()[-1]["error"]["code"], "cancelled")

    def test_cancellation_interrupts_retry_wait(self):
        cancelled = threading.Event()
        self.bedrock.outcomes = [SDKError("ServiceUnavailableException"), response()]
        engine = self.make_ocr()
        with mock.patch.object(cancelled, "wait", side_effect=lambda _: cancelled.set()) as wait:
            self.assert_page_error(engine, cancel_event=cancelled, code="cancelled")
        wait.assert_called_once_with(0.25)
        self.assertEqual(len(self.bedrock.calls), 1)
        self.assertEqual(engine.snapshot()["usage_unavailable_attempts"], 1)

    def test_retry_backoff_is_clipped_to_remaining_deadline(self):
        now = [100.0]
        self.bedrock.outcomes = [SDKError("ThrottlingException"), response()]
        engine = self.make_ocr()

        def sleep(delay):
            now[0] += delay

        with mock.patch.object(ocr.time, "monotonic", side_effect=lambda: now[0]), \
                mock.patch.object(ocr.time, "sleep", side_effect=sleep) as sleeper:
            self.assert_page_error(engine, deadline=100.04, code="deadline_exceeded")
        self.assertAlmostEqual(sleeper.call_args.args[0], 0.04)
        self.assertEqual(len(self.bedrock.calls), 1)
        self.assertEqual(engine.snapshot()["usage_unavailable_attempts"], 1)

    def test_queued_ninety_ms_calls_never_start_after_forty_ms_deadline(self):
        # Deterministic monotonic clock: one running call consumes 90ms; the
        # other three queued jobs then observe the already-expired 40ms budget.
        now = [100.0]

        def handler(_):
            now[0] += 0.09
            return response()

        self.bedrock.handler = handler
        engine = self.make_ocr()

        def call(page):
            return self.assert_page_error(engine, page=page, deadline=100.04, code="deadline_exceeded")

        with mock.patch.object(ocr.time, "monotonic", side_effect=lambda: now[0]):
            with ThreadPoolExecutor(max_workers=1) as pool:
                errors = list(pool.map(call, range(1, 5)))
        self.assertEqual(len(errors), 4)
        self.assertEqual(len(self.bedrock.calls), 1)
        stats = engine.snapshot()
        self.assertEqual((stats["new_pages"], stats["api_attempts"], stats["timeout_errors"]), (1, 1, 4))
        self.assertEqual(stats["usage"], USAGE)
        self.assertEqual(stats["usage_unavailable_attempts"], 0)
        self.assertEqual(list(engine.cache_dir.iterdir()), [])
        attempts = [row for row in self.audit() if row["event"] == "attempt"]
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["response"], response())

    def test_inflight_injected_client_finishes_but_queued_call_is_cancelled(self):
        cancelled, entered, release = threading.Event(), threading.Event(), threading.Event()

        def handler(_):
            entered.set()
            self.assertTrue(release.wait(timeout=5))
            return response()

        self.bedrock.handler = handler
        self.bedrock.close = mock.Mock()
        engine = self.make_ocr()

        def call(page):
            return self.assert_page_error(engine, page=page, cancel_event=cancelled, code="cancelled")

        with ThreadPoolExecutor(max_workers=1) as pool:
            try:
                running = pool.submit(call, 1)
                self.assertTrue(entered.wait(timeout=5))
                queued = pool.submit(call, 2)
                cancelled.set()
                self.assertFalse(running.done())
            finally:
                release.set()
            running.result(timeout=5)
            queued.result(timeout=5)
        self.assertEqual(len(self.bedrock.calls), 1)
        self.assertEqual(engine.snapshot()["usage"], USAGE)
        self.assertEqual(engine.snapshot()["cancelled_errors"], 2)
        self.assertEqual(engine.snapshot()["usage_unavailable_attempts"], 0)
        self.bedrock.close.assert_not_called()

    def test_page_lock_wait_obeys_deadline(self):
        engine = self.make_ocr()
        first = engine.process_page(PDF, SOURCE_SHA, 1)
        lock = engine._page_locks[first["cache_key"]]
        lock.acquire()
        with ThreadPoolExecutor(max_workers=1) as pool:
            try:
                future = pool.submit(self.assert_page_error, engine, deadline=time.monotonic() + 0.04,
                                     code="deadline_exceeded")
                future.result(timeout=2)
            finally:
                lock.release()
        self.assertEqual(len(self.bedrock.calls), 1)
        self.assertEqual(engine.snapshot()["cache_hits"], 0)

    def test_sdk_config_bounds_each_operation_and_deadline_clients_are_closed(self):
        now = [100.0]
        s3 = FakeS3()
        clients = []
        config = mock.Mock(side_effect=lambda **kwargs: kwargs)

        def create_client(service, config):
            def get(**request):
                now[0] += 2
                return s3.get_object(**request)

            def converse(**request):
                now[0] += 3
                return self.bedrock.converse(**request)

            client = SimpleNamespace(service=service, config=config, created_at=now[0],
                                     get_object=get, put_object=s3.put_object, converse=converse,
                                     close=mock.Mock())
            clients.append(client)
            return client

        session = mock.Mock(side_effect=lambda **_: SimpleNamespace(client=create_client))
        modules = {
            "boto3": SimpleNamespace(session=SimpleNamespace(Session=session)),
            "botocore.config": SimpleNamespace(Config=config),
        }
        engine = self.make_ocr(client=None, cache_bucket="bucket", cache_prefix="prefix")
        with mock.patch.dict("sys.modules", modules), \
                mock.patch.object(ocr.time, "monotonic", side_effect=lambda: now[0]):
            result = engine.process_page(PDF, SOURCE_SHA, 1, deadline=110)
        self.assertEqual(result["text"], BODY)
        self.assertEqual([client.service for client in clients], ["s3", "bedrock-runtime", "s3"])
        self.assertEqual([client.created_at for client in clients], [100, 102, 105])
        for client in clients:
            self.assertGreater(client.config["connect_timeout"], 0)
            self.assertGreater(client.config["read_timeout"], 0)
            self.assertLessEqual(client.config["connect_timeout"] + client.config["read_timeout"],
                                 110 - client.created_at)
            self.assertEqual(client.config["retries"]["total_max_attempts"], 1)
            client.close.assert_called_once_with()
        self.assertEqual(vars(engine._thread_clients), {})

    def test_expiry_during_client_creation_closes_client_without_api_attempt(self):
        now = [100.0]
        client = SimpleNamespace(close=mock.Mock(), converse=mock.Mock())

        def create_client(*args, **kwargs):
            now[0] = 102
            return client

        modules = {
            "boto3": SimpleNamespace(session=SimpleNamespace(
                Session=lambda **_: SimpleNamespace(client=create_client))),
            "botocore.config": SimpleNamespace(Config=lambda **kwargs: kwargs),
        }
        engine = self.make_ocr(client=None)
        with mock.patch.dict("sys.modules", modules), \
                mock.patch.object(ocr.time, "monotonic", side_effect=lambda: now[0]):
            self.assert_page_error(engine, deadline=101, code="deadline_exceeded")
        client.close.assert_called_once_with()
        client.converse.assert_not_called()
        self.assertEqual(engine.snapshot()["api_attempts"], 0)
        self.assertEqual(engine.snapshot()["usage_unavailable_attempts"], 0)
        self.assertFalse(self.audit()[0]["api_attempted"])

    def test_deadline_retry_clients_are_rebounded_and_closed_on_sdk_errors(self):
        now = [100.0]
        clients = []
        self.bedrock.outcomes = [SDKError("ThrottlingException"), response()]

        def create_client(service, config):
            client = SimpleNamespace(config=config, created_at=now[0], close=mock.Mock(),
                                     converse=self.bedrock.converse)
            clients.append(client)
            return client

        def sleep(delay):
            now[0] += delay

        modules = {
            "boto3": SimpleNamespace(session=SimpleNamespace(
                Session=lambda **_: SimpleNamespace(client=create_client))),
            "botocore.config": SimpleNamespace(Config=lambda **kwargs: kwargs),
        }
        engine = self.make_ocr(client=None)
        with mock.patch.dict("sys.modules", modules), \
                mock.patch.object(ocr.time, "monotonic", side_effect=lambda: now[0]), \
                mock.patch.object(ocr.time, "sleep", side_effect=sleep):
            result = engine.process_page(PDF, SOURCE_SHA, 1, deadline=101)
        self.assertEqual(len(clients), 2)
        budgets = []
        for client in clients:
            budget = client.config["connect_timeout"] + client.config["read_timeout"]
            budgets.append(budget)
            self.assertLessEqual(budget, 101 - client.created_at)
            client.close.assert_called_once_with()
        self.assertLess(budgets[1], budgets[0])
        self.assertEqual(result["usage_unavailable_attempts"], 1)
        self.assertEqual(vars(engine._thread_clients), {})

    def test_socket_timeout_after_deadline_records_unknown_usage_without_retry(self):
        class ReadTimeoutError(Exception):
            pass

        now = [100.0]

        def handler(_):
            now[0] = 102
            raise ReadTimeoutError("socket timed out")

        self.bedrock.handler = handler
        engine = self.make_ocr()
        with mock.patch.object(ocr.time, "monotonic", side_effect=lambda: now[0]):
            self.assert_page_error(engine, deadline=101, code="deadline_exceeded")
        stats = engine.snapshot()
        self.assertEqual((stats["api_attempts"], stats["retries"], stats["usage_unavailable_attempts"]), (1, 0, 1))
        self.assertEqual(stats["usage"], ocr._zero_usage())
        self.assertEqual(self.audit()[0]["error"]["type"], "ReadTimeoutError")
        self.assertEqual(self.audit()[-1]["error"]["code"], "deadline_exceeded")

    def test_s3_stream_read_uses_remaining_budget_and_does_not_start_generation_on_expiry(self):
        now = [100.0]
        s3 = FakeS3()
        options = {"s3_client": s3, "cache_bucket": "bucket", "cache_prefix": "prefix"}
        self.make_ocr(**options).process_page(PDF, SOURCE_SHA, 1)
        data = next(iter(s3.objects.values()))
        body = io.BytesIO(data)
        body.set_socket_timeout = mock.Mock()
        original_read = body.read

        def read():
            now[0] = 104
            return original_read()

        body.read = read

        def get(**_):
            now[0] = 102
            return {"Body": body}

        s3.get_object = get
        engine = self.make_ocr(name="fresh", **options)
        with mock.patch.object(ocr.time, "monotonic", side_effect=lambda: now[0]):
            self.assert_page_error(engine, deadline=103, code="deadline_exceeded")
        body.set_socket_timeout.assert_called_once_with(1)
        self.assertTrue(body.closed)
        self.assertEqual(engine.snapshot()["api_attempts"], 0)
        self.assertEqual(engine.snapshot()["usage_unavailable_attempts"], 0)
        self.assertEqual(list(engine.cache_dir.iterdir()), [])
        self.assertEqual(len(self.bedrock.calls), 1)

    def test_successful_remote_put_that_crosses_deadline_keeps_usage_and_survives(self):
        now = [100.0]
        s3 = FakeS3()
        original_put = s3.put_object

        def put(**request):
            result = original_put(**request)
            now[0] = 102
            return result

        s3.put_object = put
        options = {"s3_client": s3, "cache_bucket": "bucket", "cache_prefix": "prefix"}
        engine = self.make_ocr(**options)
        with mock.patch.object(ocr.time, "monotonic", side_effect=lambda: now[0]):
            self.assert_page_error(engine, deadline=101, code="deadline_exceeded")
        self.assertEqual(engine.snapshot()["usage"], USAGE)
        self.assertEqual(engine.snapshot()["usage_unavailable_attempts"], 0)
        self.assertEqual(list(engine.cache_dir.iterdir()), [])
        hit = self.make_ocr(name="fresh", max_new_pages=0, **options).process_page(PDF, SOURCE_SHA, 1)
        self.assertEqual(hit["cache_source"], "remote")
        self.assertEqual(len(self.bedrock.calls), 1)

    def test_cancellation_after_response_prevents_cache_put_and_keeps_usage(self):
        cancelled = threading.Event()
        s3 = FakeS3()

        def handler(_):
            cancelled.set()
            return response()

        self.bedrock.handler = handler
        engine = self.make_ocr(s3_client=s3, cache_bucket="bucket", cache_prefix="prefix")
        self.assert_page_error(engine, cancel_event=cancelled, code="cancelled")
        self.assertEqual(s3.puts, [])
        self.assertEqual(engine.snapshot()["usage"], USAGE)
        self.assertEqual(self.audit()[0]["response"], response())

    def test_unavailable_usage_counts_real_calls_and_survives_success_cache(self):
        self.bedrock.outcomes = [SDKError("ThrottlingException"), response()]
        engine = self.make_ocr()
        with mock.patch.object(ocr.time, "sleep"):
            first = engine.process_page(PDF, SOURCE_SHA, 1)
        self.assertEqual(first["usage"], USAGE)
        self.assertEqual(first["usage_unavailable_attempts"], 1)
        self.assertEqual(first["original_usage_unavailable_attempts"], 1)
        self.assertEqual(engine.snapshot()["usage_unavailable_attempts"], 1)
        self.assertFalse(self.audit()[0]["usage_available"])
        self.assertTrue(self.audit()[1]["usage_available"])
        fresh = self.make_ocr(max_new_pages=0)
        hit = fresh.process_page(PDF, SOURCE_SHA, 1)
        self.assertEqual(hit["usage_unavailable_attempts"], 0)
        self.assertEqual(hit["original_usage_unavailable_attempts"], 1)
        self.assertEqual(fresh.snapshot()["usage_unavailable_attempts"], 0)

    def test_api_failures_without_usage_do_not_imply_zero_cost(self):
        self.bedrock.outcomes = [SDKError("ValidationException")]
        engine = self.make_ocr()
        self.assert_page_error(engine, code="api_error")
        self.assertEqual(engine.snapshot()["usage"], ocr._zero_usage())
        self.assertEqual(engine.snapshot()["usage_unavailable_attempts"], 1)
        self.assertTrue(self.audit()[0]["api_attempted"])
        self.assertFalse(self.audit()[0]["usage_available"])

    def test_client_setup_failure_is_not_counted_as_unknown_api_usage(self):
        engine = self.make_ocr(client=None)
        with mock.patch.object(engine, "_sdk_client", side_effect=SDKError("NoCredentialsError")):
            self.assert_page_error(engine, code="api_error")
        self.assertEqual(engine.snapshot()["api_attempts"], 0)
        self.assertEqual(engine.snapshot()["usage_unavailable_attempts"], 0)
        self.assertFalse(self.audit()[0]["api_attempted"])

    def test_partial_usage_preserves_known_counters_and_flags_unknown_usage(self):
        self.bedrock.outcomes = [SDKError("ValidationException", usage={"inputTokens": 42})]
        engine = self.make_ocr()
        self.assert_page_error(engine, code="api_error")
        self.assertEqual(engine.snapshot()["usage"]["inputTokens"], 42)
        self.assertEqual(engine.snapshot()["usage_unavailable_attempts"], 1)

    def test_old_cache_usage_uncertainty_is_not_silently_zeroed(self):
        engine = self.make_ocr()
        result = engine.process_page(PDF, SOURCE_SHA, 1)
        path = engine.cache_dir / f"{result['cache_key']}.json"
        entry = json.loads(path.read_text())
        del entry["usage_unavailable_attempts"]
        entry["payload_sha256"] = ocr._digest({k: v for k, v in entry.items() if k != "payload_sha256"})
        path.write_text(json.dumps(entry))
        hit = self.make_ocr(max_new_pages=0).process_page(PDF, SOURCE_SHA, 1)
        self.assertTrue(hit["cache_hit"])
        self.assertEqual(hit["usage_unavailable_attempts"], 0)
        self.assertIsNone(hit["original_usage_unavailable_attempts"])
        self.assertEqual(len(self.bedrock.calls), 1)

    def test_invalid_unavailable_usage_count_in_cache_fails_closed(self):
        engine = self.make_ocr()
        result = engine.process_page(PDF, SOURCE_SHA, 1)
        path = engine.cache_dir / f"{result['cache_key']}.json"
        original = json.loads(path.read_text())
        for value in (None, -1, True, "1"):
            with self.subTest(value=value):
                entry = {**original, "usage_unavailable_attempts": value}
                entry["payload_sha256"] = ocr._digest({k: v for k, v in entry.items() if k != "payload_sha256"})
                path.write_text(json.dumps(entry))
                self.assert_page_error(engine, code="invalid_cache")
        self.assertEqual(len(self.bedrock.calls), 1)

    def test_parent_cache_guard_and_prompt_snapshot_fields_are_preserved(self):
        source = self.root / "src"
        with mock.patch.dict(os.environ, {"CONVERT_DOCS_SRC": str(source)}):
            with self.assertRaisesRegex(ValueError, "outside"):
                self.make_ocr(cache_dir=source / "cache")
        snapshot = self.make_ocr().snapshot()
        self.assertEqual(snapshot["prompt_version"], ocr.PROMPT_VERSION)
        self.assertEqual(snapshot["prompt_sha256"], ocr.PROMPT_SHA256)
        self.assertEqual(snapshot["max_tokens"], 16384)


if __name__ == "__main__":
    unittest.main()
