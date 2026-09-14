"""Offline PDF conversion regressions using synthetic text and fake PDF pages.

Run with: python -m unittest discover -s tests -p test_convert_docs.py
"""

import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from cdk.build_scripts import convert_docs as converter


class FakePage:
    def __init__(self, text, *, visit=True):
        self.text = text
        self.visit = visit

    def extract_text(self, visitor_text=None):
        if self.visit and visitor_text is not None:
            visitor_text(
                self.text, [1, 0, 0, 1, 0, 0], [1, 0, 0, 1, 0, 0],
                {"/BaseFont": "Regular"}, 12,
            )
        return self.text


class StubOCR:
    model_id = "offline-transcription-model"

    def __init__(self, outcomes=None):
        self.outcomes = outcomes or {}
        self.calls = []
        self.options = []
        self.lock = threading.Lock()

    def process_page(self, data, source_sha256, page_number, *, deadline=None, cancel_event=None):
        with self.lock:
            self.calls.append((data, source_sha256, page_number))
            self.options.append({"deadline": deadline, "cancel_event": cancel_event})
        outcome = self.outcomes.get(page_number, {
            "status": "transcribed", "text": f"Transcribed synthetic page {page_number}."
        })
        if isinstance(outcome, Exception):
            raise outcome
        return {
            "cache_hit": False, "cache_key": f"offline-cache-{page_number}",
            "model_id": self.model_id, **outcome,
        }

    def snapshot(self):
        with self.lock:
            return {"new_pages": len(self.calls), "api_attempts": len(self.calls)}


class ConvertDocsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.src = self.root / "src"
        self.src.mkdir()
        self.pdf = self.src / "sample.pdf"
        self.pdf.write_bytes(b"%PDF-1.7\nsynthetic offline fixture\n%%EOF")
        original_sha = hashlib.sha256(self.pdf.read_bytes()).hexdigest()
        self.addCleanup(
            lambda: self.assertEqual(hashlib.sha256(self.pdf.read_bytes()).hexdigest(), original_sha)
        )
        self.dest = self.src / "sample.pdf.d"
        self.scratch = self.src / "sample.pdf.d.tmp"
        self.report = {}
        self.pages = []
        self.reader = SimpleNamespace(pages=self.pages, outline=[])
        self.pdf_reader = mock.Mock(return_value=self.reader)
        self.report_path = self.root / "reports" / "document-conversion-report.json"
        self.ocr = StubOCR()
        self.ocr.cache_dir = self.root / "cache"
        self.ocr.audit_path = self.root / "audit.jsonl"
        self.ocr_factory = mock.Mock(return_value=self.ocr)
        self.enterContext(mock.patch.object(converter, "SRC", self.src))
        self.enterContext(mock.patch.dict("sys.modules", {
            "pypdf": SimpleNamespace(PdfReader=self.pdf_reader),
            "boto3": None, "botocore": None, "botocore.config": None,
            "document_ocr": SimpleNamespace(DocumentOCR=SimpleNamespace(from_env=self.ocr_factory)),
        }))
        self.single_page = self.enterContext(mock.patch.object(
            converter, "_single_page_pdf",
            side_effect=lambda reader, index: f"offline-page-{index + 1}".encode(),
        ))
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))
        self.enterContext(contextlib.redirect_stderr(io.StringIO()))

    def convert(self, *texts, ocr=None, visit=True):
        self.pages[:] = [FakePage(text, visit=visit) for text in texts]
        return converter.convert_pdf(self.pdf, image_budget=0, ocr=ocr, report=self.report)

    def markdown(self):
        return "\n".join(path.read_text() for path in sorted(self.dest.glob("*.md")))

    def run_main(self, **environment):
        env = {
            "LLM_EXTRACT": "1", "LLM_IMAGES": "1",
            "CONVERT_DOCS_SRC": str(self.src),
            "DOCUMENT_CONVERSION_REPORT": str(self.report_path),
            **environment,
        }
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(converter.signal, "signal"), \
                mock.patch.object(converter.signal, "alarm"):
            return converter.main()

    def read_report(self):
        report = json.loads(self.report_path.read_text())
        self.assertFalse(self.report_path.is_relative_to(self.src))
        self.assertFalse(self.report_path.with_name(self.report_path.name + ".tmp").exists())
        return report

    def test_only_pages_below_fifty_native_alphanumeric_characters_request_ocr(self):
        ocr = StubOCR()
        # Whitespace and punctuation must not raise a sparse page above the threshold.
        self.assertTrue(self.convert("가" * 48 + "7" + " !?. " * 30, "나" * 49 + "8", ocr=ocr))
        self.assertEqual([call[2] for call in ocr.calls], [1])
        self.assertEqual(
            [page["native_alphanumeric_characters"] for page in self.report["pages"]], [49, 50]
        )

    def test_password_required_pdf_is_explicitly_unsupported_without_reading_pages_or_ocr(self):
        reader = mock.Mock(is_encrypted=True)
        reader.decrypt.return_value = 0
        type(reader).pages = mock.PropertyMock(side_effect=AssertionError("protected pages accessed"))
        self.pdf_reader.return_value = reader
        self.assertFalse(self.convert(ocr=self.ocr))
        reader.decrypt.assert_called_once_with("")
        self.assertEqual(self.report["status"], "unsupported")
        self.assertEqual(self.report["reason_code"], "pdf_password_required")
        self.assertNotIn("page_count", self.report)
        self.assertEqual(self.report["pages"], [])
        self.assertEqual(self.ocr.calls, [])
        self.assertFalse(self.dest.exists())
        self.assertFalse(self.scratch.exists())

    def test_empty_opening_password_pdf_still_converts_normally(self):
        self.reader.is_encrypted = True
        self.reader.decrypt = mock.Mock(return_value=2)
        self.assertTrue(self.convert("Readable native document body. " * 5, ocr=self.ocr))
        self.reader.decrypt.assert_called_once_with("")
        self.assertEqual(self.report["status"], "converted")
        self.assertEqual(self.report["page_count"], 1)
        self.assertIn("Readable native document body.", self.markdown())

    def test_unexpected_decryption_error_remains_a_failure(self):
        self.reader.is_encrypted = True
        self.reader.decrypt = mock.Mock(side_effect=RuntimeError("unexpected cipher failure"))
        with self.assertRaisesRegex(RuntimeError, "unexpected cipher failure"):
            self.convert(ocr=self.ocr)
        self.assertEqual(self.report["status"], "failed")
        self.assertFalse(self.dest.exists())

    def test_password_required_and_readable_pdf_batch_reports_partial_without_dropping_readable_file(self):
        protected = self.src / "protected.pdf"
        protected.write_bytes(b"%PDF-1.7\nprotected fixture")
        locked = mock.Mock(is_encrypted=True)
        locked.decrypt.return_value = 0
        type(locked).pages = mock.PropertyMock(side_effect=ValueError("password required"))
        self.pages[:] = [FakePage("Readable native document body. " * 5)]
        self.pdf_reader.side_effect = lambda stream: (
            locked if b"protected fixture" in stream.getvalue() else self.reader)
        self.assertEqual(self.run_main(LLM_IMAGES="0"), 0)
        report = self.read_report()
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["totals"]["converted_documents"], 1)
        self.assertEqual(report["totals"]["failed_documents"], 0)
        self.assertEqual(report["totals"]["document_statuses"]["unsupported"], 1)
        self.assertEqual(report["totals"]["pdf_documents_unknown_page_count"], 1)
        self.assertTrue(protected.exists())
        self.assertTrue(self.dest.exists())

    def test_zero_image_budget_still_transcribes_every_sparse_page(self):
        ocr = StubOCR()
        self.assertTrue(self.convert(*[""] * 7, ocr=ocr))
        expected_sha = hashlib.sha256(self.pdf.read_bytes()).hexdigest()
        self.assertCountEqual(
            ocr.calls, [(f"offline-page-{page}".encode(), expected_sha, page) for page in range(1, 8)]
        )

    def test_native_rich_pages_do_not_serialize_or_request_ocr(self):
        ocr = StubOCR()
        text = "A synthetic native paragraph with enough readable characters for extraction."
        self.assertTrue(self.convert(text, text, ocr=ocr))
        self.assertEqual(ocr.calls, [])
        self.single_page.assert_not_called()
        self.assertIn(text, self.markdown())
        self.assertTrue(all(page["status"] == "native" for page in self.report["pages"]))

    def test_mixed_pages_keep_original_page_numbers_and_source_attribution(self):
        ocr = StubOCR({2: {"status": "blank", "text": ""}})
        self.assertTrue(self.convert("Native paragraph. " * 6, "", "", ocr=ocr))
        markdown = self.markdown()
        self.assertIn('converted_from_file: "sample.pdf"', markdown)
        self.assertIn("##### sample — p.1", markdown)
        self.assertIn("##### sample — p.3", markdown)
        self.assertLess(markdown.index("##### sample — p.1"), markdown.index("##### sample — p.3"))
        self.assertIn("Transcribed synthetic page 3.", markdown)
        self.assertEqual([page["page"] for page in self.report["pages"]], [1, 2, 3])

    def test_transcribed_markdown_preserves_headings_tables_numbers_and_uncertainty(self):
        body = (
            "# Synthetic inventory\n\n## Measurements\n\n"
            "| Item | Mass (kg) |\n| --- | ---: |\n| Widget | 1,234.50 |\n\n"
            "1. Retain 0.025% tolerance.\n2. Read [unreadable] from the original.\n"
            "3. The label remains [ILLEGIBLE]; retain the readable measurement."
        )
        self.assertTrue(self.convert("", ocr=StubOCR({1: {"status": "transcribed", "text": body}})))
        markdown = self.markdown()
        self.assertIn("# Synthetic inventory", markdown)
        self.assertRegex(markdown, r"(?m)^#{1,4} Measurements$")
        self.assertIn("| Item | Mass (kg) |\n| --- | ---: |\n| Widget | 1,234.50 |", markdown)
        self.assertIn("1. Retain 0.025% tolerance.", markdown)
        self.assertIn("2. Read [unreadable] from the original.", markdown)
        self.assertIn("3. The label remains [ILLEGIBLE]; retain the readable measurement.", markdown)
        self.assertEqual(self.report["pages"][0]["status"], "transcribed_uncertain")
        self.assertEqual(self.report["pages"][0]["uncertain_spans"], 2)
        self.assertIn("uncertain_spans: 2;", markdown)

    def test_model_and_cache_provenance_follow_the_transcribed_page(self):
        ocr = StubOCR({1: {
            "status": "transcribed", "text": "Synthetic cached transcription.",
            "cache_hit": True, "cache_key": "cached-page-key", "model_id": "cached-model",
        }})
        self.assertTrue(self.convert("", ocr=ocr))
        self.assertIn("model: cached-model", self.markdown())
        self.assertIn("original_page: 1", self.markdown())
        page = self.report["pages"][0]
        self.assertEqual(page["method"], "bedrock_pdf_ocr")
        self.assertEqual(page["model_id"], "cached-model")
        self.assertTrue(page["cache_hit"])
        self.assertEqual(page["cache_key"], "cached-page-key")

    def test_heading_only_transcription_keeps_its_original_page_marker(self):
        ocr = StubOCR({1: {"status": "transcribed", "text": "# Synthetic cover title"}})
        self.assertTrue(self.convert("", ocr=ocr))
        self.assertIn("##### sample — p.1", self.markdown())

    def test_initial_long_ocr_heading_preserves_full_text_beyond_display_title_limit(self):
        heading = "Synthetic heading with deliberately long descriptive text " * 3 + "INITIAL_SENTINEL"
        self.assertGreater(len(heading), converter.TITLE_MAX)
        ocr = StubOCR({1: {"status": "transcribed", "text": f"# {heading}\nSynthetic body."}})
        self.assertTrue(self.convert("", ocr=ocr))
        markdown = self.markdown()
        self.assertIn(heading, markdown)
        self.assertIn("INITIAL_SENTINEL", markdown)
        self.assertIn("##### sample — p.1", markdown)

    def test_midpage_long_ocr_heading_preserves_full_text_when_it_opens_a_part(self):
        heading = "Synthetic heading with deliberately long descriptive text " * 3 + "MIDPAGE_SENTINEL"
        prefix = "Synthetic paragraph before the section boundary.\n" * 200
        self.assertGreater(len(prefix), converter.PART_MIN)
        ocr = StubOCR({1: {"status": "transcribed", "text": f"{prefix}\n# {heading}\nSynthetic body."}})
        self.assertTrue(self.convert("", ocr=ocr))
        parts = sorted(self.dest.glob("*.md"))
        self.assertEqual(len(parts), 2)
        self.assertIn(heading, parts[1].read_text())
        self.assertIn("MIDPAGE_SENTINEL", parts[1].read_text())
        self.assertIn("##### sample — p.1", parts[1].read_text())

    def test_expired_deadline_fails_without_starting_any_ocr_jobs(self):
        self.pages[:] = [FakePage("") for _ in range(4)]
        self.scratch.mkdir()
        (self.scratch / "stale.md").write_text("Stale incomplete conversion")
        with self.assertRaises((TimeoutError, RuntimeError)):
            converter.convert_pdf(
                self.pdf, ocr=self.ocr, report=self.report, ocr_workers=1,
                deadline=time.monotonic() - 1,
            )
        self.assertEqual(self.ocr.calls, [])
        self.assertEqual(self.report["status"], "failed")
        self.assertTrue(self.report["pages"])
        self.assertTrue(all(
            page["status"] == "failed" and page["error"]["type"] == "TimeoutError"
            for page in self.report["pages"]
        ))
        self.assertFalse(self.dest.exists())
        self.assertFalse(self.scratch.exists())

    def test_short_deadline_cancels_waiting_jobs_and_fails_without_final_sidecar(self):
        self.pages[:] = [FakePage("") for _ in range(4)]
        started = []
        finished = threading.Event()
        requested_deadline = time.monotonic() + 1.0

        def block_until_cancelled(data, source_sha256, page_number, *, deadline=None, cancel_event=None):
            started.append((page_number, deadline, cancel_event))
            try:
                if page_number == 1 and cancel_event is not None:
                    # Keep the sole worker occupied until the document deadline cancels it.
                    # The bound also makes a broken cancellation implementation terminate.
                    cancel_event.wait(timeout=2.0)
                raise TimeoutError("synthetic cancelled page")
            finally:
                finished.set()

        with mock.patch.object(self.ocr, "process_page", side_effect=block_until_cancelled):
            with self.assertRaises(TimeoutError):
                converter.convert_pdf(
                    self.pdf, ocr=self.ocr, report=self.report, ocr_workers=1,
                    deadline=requested_deadline,
                )
            self.assertTrue(finished.wait(timeout=2.0), "active OCR worker was not released")
        self.assertEqual([item[0] for item in started], [1])
        self.assertEqual(started[0][1], requested_deadline)
        self.assertIsNotNone(started[0][2])
        self.assertTrue(started[0][2].is_set())
        self.assertEqual(self.report["status"], "failed")
        self.assertFalse(self.dest.exists())
        self.assertFalse(self.scratch.exists())

    def test_confirmed_blank_document_is_distinguished_from_unextracted_native_text(self):
        self.assertFalse(self.convert("", ocr=StubOCR({1: {"status": "blank", "text": ""}})))
        self.assertEqual(self.report["status"], "blank")
        self.assertEqual(self.report["pages"][0]["status"], "blank")
        self.assertFalse(self.dest.exists())
        self.assertFalse(self.scratch.exists())
        native_report = {}
        self.assertFalse(converter.convert_pdf(self.pdf, report=native_report))
        self.assertEqual(native_report["status"], "no_extractable_text")
        self.assertEqual(native_report["pages"][0]["status"], "limited_native")

    def test_ocr_failure_rolls_back_scratch_and_never_publishes_partial_sidecar(self):
        self.scratch.mkdir()
        (self.scratch / "partial.md").write_text("Stale incomplete conversion")
        ocr = StubOCR({2: RuntimeError("synthetic OCR unavailable")})
        with self.assertRaisesRegex(RuntimeError, "2"):
            self.convert("", "", ocr=ocr)
        self.assertEqual(self.report["status"], "failed")
        self.assertEqual(self.report["pages"][1]["status"], "failed")
        self.assertFalse(self.dest.exists())
        self.assertFalse(self.scratch.exists())

    def test_existing_user_sidecar_is_preserved_without_reading_pdf_or_requesting_ocr(self):
        self.dest.mkdir()
        authored = self.dest / "user-notes.md"
        authored.write_bytes(b"# User-authored notes\nPreserve exactly.\n")
        original = authored.read_bytes()
        ocr = StubOCR()
        self.assertFalse(self.convert("", ocr=ocr))
        self.assertEqual(authored.read_bytes(), original)
        self.assertEqual(list(self.dest.iterdir()), [authored])
        self.pdf_reader.assert_not_called()
        self.assertEqual(ocr.calls, [])
        self.assertEqual(self.report["status"], "existing_unverified")

    def test_page_cap_refuses_conversion_instead_of_publishing_truncated_output(self):
        ocr = StubOCR()
        with mock.patch.object(converter, "PDF_PAGE_CAP", 2):
            with self.assertRaisesRegex(ValueError, "page count.*exceeds"):
                self.convert("", "", "", ocr=ocr)
        self.assertEqual(self.report["status"], "failed")
        self.assertEqual(ocr.calls, [])
        self.assertFalse(self.dest.exists())
        self.assertFalse(self.scratch.exists())

    def test_native_only_plain_extraction_works_with_aws_dependencies_unavailable(self):
        text = "Synthetic native text remains usable without OCR or installed AWS SDKs."
        self.assertTrue(self.convert(text, visit=False))
        self.assertIn(text, self.markdown())
        self.assertNotIn("bedrock_pdf_ocr", self.markdown())
        self.assertFalse(self.report["pages"][0]["ocr_requested"])
        self.single_page.assert_not_called()

    def test_main_writes_complete_report_and_ocr_counters_for_converted_document(self):
        self.pages[:] = [FakePage("")]
        self.assertEqual(self.run_main(), 0)
        self.ocr_factory.assert_called_once_with()
        report = self.read_report()
        self.assertEqual(report["status"], "complete")
        self.assertEqual(report["totals"]["converted_documents"], 1)
        self.assertEqual(report["totals"]["ocr_requested_pages"], 1)
        self.assertEqual(report["totals"]["page_statuses"], {"transcribed": 1})
        self.assertEqual(report["ocr"], self.ocr.snapshot())
        self.assertEqual(
            report["documents"][0]["source_sha256"], hashlib.sha256(self.pdf.read_bytes()).hexdigest()
        )
        self.assertTrue(self.dest.is_dir())

    def test_main_reports_partial_and_exits_zero_when_ocr_contains_uncertain_spans(self):
        self.pages[:] = [FakePage(""), FakePage("")]
        text = "Readable synthetic measurement: 12.5 kg; label [UNREADABLE], unit [ILLEGIBLE]."
        self.ocr.outcomes = {1: {"status": "transcribed", "text": text}}
        self.assertEqual(self.run_main(), 0)
        report = self.read_report()
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["totals"]["converted_documents"], 1)
        self.assertEqual(report["totals"]["failed_documents"], 0)
        self.assertEqual(report["totals"]["uncertain_pages"], 1)
        self.assertEqual(report["totals"]["page_statuses"], {
            "transcribed_uncertain": 1, "transcribed": 1,
        })
        pages = report["documents"][0]["pages"]
        self.assertEqual([page["uncertain_spans"] for page in pages], [2, 0])
        self.assertIn(text, self.markdown())
        self.assertIn("original_page: 1; uncertain_spans: 2;", self.markdown())
        self.assertIn("original_page: 2; uncertain_spans: 0;", self.markdown())

    def test_main_does_not_instantiate_ocr_unless_both_toggles_are_enabled(self):
        self.pages[:] = [FakePage("")]
        for extract, images in (("0", "0"), ("0", "1"), ("1", "0")):
            with self.subTest(LLM_EXTRACT=extract, LLM_IMAGES=images):
                self.assertEqual(self.run_main(LLM_EXTRACT=extract, LLM_IMAGES=images), 0)
                self.ocr_factory.assert_not_called()
                report = self.read_report()
                self.assertFalse(report["ocr_enabled"])
                self.assertIsNone(report["ocr"])
                self.assertEqual(report["status"], "partial")
                self.assertEqual(report["documents"][0]["status"], "no_extractable_text")
                self.assertEqual(report["totals"]["native_limited_pages"], 1)
                self.single_page.assert_not_called()

    def test_main_records_complete_native_conversion_without_ocr(self):
        self.pages[:] = [FakePage("Synthetic readable paragraph. " * 4, visit=False)]
        self.assertEqual(self.run_main(LLM_IMAGES="0"), 0)
        report = self.read_report()
        self.assertEqual(report["status"], "complete")
        self.assertEqual(report["totals"]["page_statuses"], {"native": 1})
        self.ocr_factory.assert_not_called()
        self.assertIn("Synthetic readable paragraph.", self.markdown())

    def test_main_records_blank_when_ocr_rejects_native_punctuation_as_content(self):
        self.pages[:] = [FakePage(" ... !!! --- \n", visit=False), FakePage("")]
        self.ocr.outcomes = {page: {"status": "blank", "text": ""} for page in (1, 2)}
        self.assertEqual(self.run_main(), 0)
        report = self.read_report()
        self.assertEqual(report["status"], "complete")
        self.assertEqual(report["documents"][0]["status"], "blank")
        self.assertEqual(report["totals"]["page_statuses"], {"blank": 2})
        self.assertEqual(report["totals"]["converted_documents"], 0)
        self.assertFalse(self.dest.exists())
        self.assertFalse(self.scratch.exists())

    def test_main_preserves_existing_sidecar_and_reports_unverified_partial_conversion(self):
        self.dest.mkdir()
        authored = self.dest / "old-sidecar.md"
        authored.write_bytes(b"# Existing sidecar\nOriginal user annotations.\n")
        expected_sha = hashlib.sha256(authored.read_bytes()).hexdigest()
        self.pages[:] = [FakePage("")]
        self.assertEqual(self.run_main(), 0)
        report = self.read_report()
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["documents"][0]["status"], "existing_unverified")
        self.assertEqual(hashlib.sha256(authored.read_bytes()).hexdigest(), expected_sha)
        self.assertEqual(list(self.dest.iterdir()), [authored])
        self.assertEqual(self.ocr.calls, [])
        self.single_page.assert_not_called()

    def test_main_reports_unsupported_originals_without_changing_their_bytes(self):
        legacy = self.src / "synthetic.doc"
        legacy.write_bytes(b"synthetic unsupported legacy document")
        expected_sha = hashlib.sha256(legacy.read_bytes()).hexdigest()
        self.pages[:] = [FakePage("Native paragraph. " * 6)]
        self.assertEqual(self.run_main(LLM_IMAGES="0"), 0)
        report = self.read_report()
        self.assertEqual(report["status"], "partial")
        records = {record["source_file"]: record for record in report["documents"]}
        self.assertEqual(records["synthetic.doc"]["status"], "unsupported")
        self.assertEqual(hashlib.sha256(legacy.read_bytes()).hexdigest(), expected_sha)
        self.assertFalse(legacy.with_name(legacy.name + ".md").exists())

    def test_main_ocr_budget_or_api_failure_returns_one_without_publishing_sidecar(self):
        class OCRBudgetExceeded(RuntimeError):
            pass

        for error in (OCRBudgetExceeded("synthetic page budget exhausted"),
                      RuntimeError("synthetic OCR API unavailable")):
            with self.subTest(error=type(error).__name__):
                self.pages[:] = [FakePage(""), FakePage("")]
                self.ocr.outcomes = {2: error}
                self.scratch.mkdir()
                (self.scratch / "partial.md").write_text("Stale incomplete result")
                self.assertEqual(self.run_main(), 1)
                report = self.read_report()
                self.assertEqual(report["status"], "failed")
                self.assertEqual(report["totals"]["failed_documents"], 1)
                self.assertEqual(report["totals"]["converted_documents"], 0)
                self.assertEqual(report["totals"]["page_statuses"], {"transcribed": 1, "failed": 1})
                failed_page = report["documents"][0]["pages"][1]
                self.assertEqual(failed_page["error"]["type"], type(error).__name__)
                self.assertEqual(failed_page["error"]["message"], str(error))
                self.assertFalse(self.dest.exists())
                self.assertFalse(self.scratch.exists())

    def test_main_continues_reporting_other_documents_after_pdf_reader_failure(self):
        good_pdf = self.src / "z-readable.pdf"
        good_pdf.write_bytes(b"another synthetic PDF fixture")
        expected_sha = hashlib.sha256(good_pdf.read_bytes()).hexdigest()
        self.pages[:] = [FakePage("Native paragraph. " * 6)]

        def read_pdf(source):
            contents = source.getvalue() if isinstance(source, io.BytesIO) else Path(source).read_bytes()
            if contents == self.pdf.read_bytes():
                raise ValueError("synthetic corrupt PDF")
            return self.reader

        self.pdf_reader.side_effect = read_pdf
        self.assertEqual(self.run_main(LLM_IMAGES="0"), 1)
        report = self.read_report()
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["totals"]["document_statuses"], {"failed": 1, "converted": 1})
        self.assertEqual(report["totals"]["converted_documents"], 1)
        self.assertEqual(report["totals"]["failed_documents"], 1)
        self.assertTrue(good_pdf.with_name(good_pdf.name + ".d").is_dir())
        self.assertEqual(hashlib.sha256(good_pdf.read_bytes()).hexdigest(), expected_sha)
        self.assertFalse(self.dest.exists())

    def test_main_writes_failure_report_when_ocr_initialization_fails(self):
        self.pages[:] = [FakePage("")]
        self.ocr_factory.side_effect = ValueError("synthetic invalid OCR configuration")
        self.assertEqual(self.run_main(), 1)
        report = self.read_report()
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["error"]["message"], "synthetic invalid OCR configuration")
        self.assertEqual(report["documents"], [])
        self.assertEqual(report["totals"]["failed_documents"], 1)
        self.assertFalse(self.dest.exists())
        self.pdf_reader.assert_not_called()

    def test_main_rejects_report_paths_inside_source_including_symlink_aliases(self):
        alias = self.root / "source-alias"
        alias.symlink_to(self.src, target_is_directory=True)
        for directory in (self.src, alias):
            with self.subTest(directory=directory.name):
                target = directory / "conversion-report.json"
                self.assertEqual(self.run_main(DOCUMENT_CONVERSION_REPORT=str(target)), 1)
                self.assertFalse(target.exists())
                self.assertFalse(self.dest.exists())
                self.ocr_factory.assert_not_called()
                self.pdf_reader.assert_not_called()

    def test_main_rejects_ocr_cache_or_audit_inside_source_and_writes_external_report(self):
        for field, name in (("cache_dir", "cache"), ("audit_path", "audit.jsonl")):
            with self.subTest(field=field), mock.patch.object(self.ocr, field, self.src / name):
                self.assertEqual(self.run_main(), 1)
                report = self.read_report()
                self.assertEqual(report["status"], "failed")
                self.assertIn("outside", report["error"]["message"])
                self.assertEqual(report["documents"], [])
                self.assertEqual(self.ocr.calls, [])
                self.assertFalse(self.dest.exists())
                self.assertFalse((self.src / name).exists())

    def test_main_writes_failure_report_for_invalid_worker_configuration(self):
        for workers in ("0", "9", "invalid"):
            with self.subTest(workers=workers):
                self.assertEqual(self.run_main(DOCUMENT_OCR_WORKERS=workers), 1)
                report = self.read_report()
                self.assertEqual(report["status"], "failed")
                self.assertEqual(report["error"]["type"], "ValueError")
                self.ocr_factory.assert_not_called()
                self.assertFalse(self.dest.exists())

    def test_main_reports_complete_for_empty_source_without_initializing_ocr(self):
        empty_src = self.root / "empty-src"
        empty_src.mkdir()
        with mock.patch.object(converter, "SRC", empty_src):
            self.assertEqual(self.run_main(CONVERT_DOCS_SRC=str(empty_src)), 0)
        report = self.read_report()
        self.assertEqual(report["status"], "complete")
        self.assertEqual(report["documents"], [])
        self.assertEqual(report["totals"]["converted_documents"], 0)
        self.assertEqual(report["totals"]["failed_documents"], 0)
        self.ocr_factory.assert_not_called()

    def test_main_reports_real_ocr_factory_cache_guard_before_creating_source_artifacts(self):
        from cdk.build_scripts.document_ocr import DocumentOCR

        self.ocr_factory.side_effect = DocumentOCR.from_env
        alias = self.root / "source-alias"
        alias.symlink_to(self.src, target_is_directory=True)
        cache = alias / "ocr-cache"
        self.assertEqual(self.run_main(
            DOCUMENT_OCR_CACHE_DIR=str(cache),
            DOCUMENT_OCR_AUDIT_PATH=str(self.root / "audit.jsonl"),
        ), 1)
        report = self.read_report()
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["error"]["type"], "ValueError")
        self.assertIn("outside", report["error"]["message"])
        self.assertFalse(cache.exists())
        self.assertEqual(list(self.src.iterdir()), [self.pdf])
        self.pdf_reader.assert_not_called()

    def test_main_reports_failure_when_conversion_is_interrupted(self):
        self.pdf_reader.side_effect = KeyboardInterrupt("synthetic interruption")
        try:
            result = self.run_main(LLM_IMAGES="0")
        except KeyboardInterrupt:
            # Propagating cancellation is fine; claiming a complete conversion is not.
            pass
        else:
            self.assertEqual(result, 1)
        report = self.read_report()
        self.assertEqual(report["status"], "failed")
        self.assertGreater(report["totals"]["failed_documents"], 0)
        self.assertEqual(report["documents"][0]["status"], "failed")
        self.assertFalse(self.dest.exists())
        self.assertFalse(self.scratch.exists())

    def test_main_restores_previous_alarm_handler_after_success_or_failure(self):
        previous_handler = mock.Mock(name="previous_alarm_handler")
        for error in (None, ValueError("synthetic unreadable PDF")):
            with self.subTest(error=error):
                self.pdf_reader.side_effect = error
                self.pages[:] = [FakePage("")]
                env = {"LLM_EXTRACT": "0", "LLM_IMAGES": "0",
                       "DOCUMENT_CONVERSION_REPORT": str(self.report_path)}
                with mock.patch.dict(os.environ, env, clear=True), \
                        mock.patch.object(converter.signal, "getsignal", return_value=previous_handler), \
                        mock.patch.object(converter.signal, "signal", return_value=previous_handler) as set_handler, \
                        mock.patch.object(converter.signal, "alarm") as alarm:
                    self.assertEqual(converter.main(), 1 if error else 0)
                self.assertGreaterEqual(set_handler.call_count, 2)
                self.assertEqual(set_handler.call_args, mock.call(converter.signal.SIGALRM, previous_handler))
                self.assertEqual(alarm.call_args, mock.call(0))


if __name__ == "__main__":
    unittest.main()
