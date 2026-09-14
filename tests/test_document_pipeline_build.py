"""Offline checks for the files-source conversion/build contract.

Run with: python -m unittest discover -s tests -p test_document_pipeline_build.py
AWS imports and shell commands are stubbed; no credentials or services are used.
"""

import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import time
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load_module(name, relative_path):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BUILD_SPEC = load_module("document_buildspec", "cdk/buildspec.py").BUILD_SPEC


def command_containing(phase, needle):
    matches = [c for c in BUILD_SPEC["phases"][phase]["commands"] if needle in c]
    if len(matches) != 1:
        raise AssertionError(f"expected one {phase} command containing {needle!r}")
    return matches[0]


CONVERT = command_containing("build", "python /tmp/convert_docs.py")
FINGERPRINT = command_containing("post_build", "SRC_HASH_FILE=")
REPORT = command_containing("post_build", "latest/document-conversion-report.json")
FILES_SNAPSHOT = command_containing("post_build", "src snapshot exceeds 200MB cap")
SNAPSHOT = command_containing("post_build", "src snapshot failed (non-fatal)")
LATEST_GRAPH = command_containing(
    "post_build", '[ -f /tmp/work/SKIP ] || aws s3 cp "$GRAPHIFY_OUT/graph.json"')
PUBLISH_GATE = BUILD_SPEC["phases"]["post_build"]["commands"][0]


class DocumentPipelineBuildTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.work = self.root / "work"
        (self.work / "src").mkdir(parents=True)
        (self.work / "graphify-out").mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.log = self.root / "commands.jsonl"
        # Real bash executes the generated commands. These stubs only replace
        # external boundaries, including GNU stat on a macOS test runner.
        stub = f"#!{sys.executable}\n" + textwrap.dedent("""
            import json
            import os
            from pathlib import Path
            import sys

            tool = Path(sys.argv[0]).name
            args = sys.argv[1:]
            work = Path(os.environ["TEST_WORK"])
            event = {"tool": tool, "args": args}
            if tool == "aws" and args[:2] == ["s3", "cp"]:
                source = Path(args[2])
                if source.is_file() and source.name in (
                    "source_fingerprint", "document-conversion-report.json",
                    "document-ocr-audit.jsonl",
                ):
                    event["body"] = source.read_text()
            with open(os.environ["TEST_LOG"], "a") as log:
                log.write(json.dumps(event) + "\\n")
            if tool == "python" and Path(args[0]).name == "convert_docs.py":
                rc = int(os.environ.get("CONVERTER_RC", "0"))
                (work / "document-conversion-report.json").write_text(
                    json.dumps({"complete": rc == 0}))
                Path(os.environ["DOCUMENT_OCR_AUDIT_PATH"]).write_text(
                    json.dumps({"page": 1, "complete": rc == 0}) + "\\n")
                cache = Path(os.environ["DOCUMENT_OCR_CACHE_DIR"])
                cache.mkdir(exist_ok=True)
                (cache / "page.json").write_text('{"text":"cached page"}')
                (work / "src" / "document.pdf.md").write_text(
                    "# Converted page\\n\\nSource content.")
                sys.exit(rc)
            if tool == "stat":
                if os.environ.get("FAIL_SNAPSHOT_STAT"):
                    sys.exit(48)
                print(os.environ.get("SNAPSHOT_SIZE") or Path(args[-1]).stat().st_size)
            if tool == "tar":
                if os.environ.get("FAIL_SNAPSHOT_CREATE"):
                    sys.exit(47)
                os.execv(os.environ["REAL_TAR"], ["tar", *args])
            if tool == "aws" and args[:2] == ["s3", "cp"]:
                if args[2].endswith("/document_ocr.py") and os.environ.get("FAIL_HELPER"):
                    sys.exit(45)
                if "/history/" in args[3] and os.environ.get("FAIL_HISTORY"):
                    sys.exit(46)
                if args[3].endswith("/latest/src.tar.gz") and os.environ.get("FAIL_SNAPSHOT_UPLOAD"):
                    sys.exit(49)
        """)
        for name in ("aws", "python", "graphify", "stat", "tar"):
            path = self.bin / name
            path.write_text(stub)
            path.chmod(0o755)
        self.env = {
            **os.environ,
            **{k: self.relocate(v) for k, v in BUILD_SPEC["env"]["variables"].items()},
            "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
            "TEST_WORK": str(self.work),
            "TEST_LOG": str(self.log),
            "REAL_TAR": shutil.which("tar"),
            # Match the Linux build archive when testing with macOS bsdtar.
            "COPYFILE_DISABLE": "1",
            "GRAPH_BUCKET": "offline-bucket",
            "REPO_ID": "documents",
            "CODEBUILD_BUILD_ID": "project:build-123",
            "SOURCE_TYPE": "files",
            "LLM_EXTRACT": "0",
            "LLM_IMAGES": "0",
            "LLM_MODEL": "",
            "PRUNE_PATHS": "",
            "CONVERTER_RC": "0",
            "FAIL_HELPER": "",
            "FAIL_HISTORY": "",
            "FAIL_SNAPSHOT_CREATE": "",
            "FAIL_SNAPSHOT_STAT": "",
            "FAIL_SNAPSHOT_UPLOAD": "",
            "SNAPSHOT_SIZE": "",
        }
        # Patch boto3 before importing modules with module-level clients.
        # This works even when boto3 is not installed on the test machine.
        self.boto3 = mock.MagicMock()
        with mock.patch.dict(sys.modules, {"boto3": self.boto3}), mock.patch.dict(
            os.environ,
            {"TABLE_NAME": "offline-table", "PROJECT_NAME": "offline-project",
             "GRAPH_BUCKET": "offline-bucket", "REPO_ID": "documents"},
        ):
            self.poller = load_module("document_poller", "lambdas/poller/handler.py")
            self.fetch = load_module("document_fetch", "cdk/build_scripts/fetch_uploads.py")
        self.poller.GITHUB_TOKEN_SECRET_ARN = ""
        self.poller.time = mock.Mock(wraps=time)
        self.poller.time.time.return_value = 123
        self.poller.claim_build = mock.Mock(return_value=True)
        self.poller.start_build = mock.Mock(
            return_value={"id": "project:build-123", "arn": "offline-build"})

    def relocate(self, value):
        return value.replace("/tmp/", str(self.root) + "/")

    def run_shell(self, commands, **env):
        if isinstance(commands, str):
            commands = [commands]
        return subprocess.run(
            ["bash", "-e", "-c", self.relocate("\n".join(commands))],
            env={**self.env, **env}, cwd=self.root,
            capture_output=True, text=True, timeout=10,
        )

    def events(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def poll(self, item):
        self.poller.table.query.return_value = {"Items": [item]}
        self.poller.claim_build.reset_mock()
        self.poller.start_build.reset_mock()
        with contextlib.redirect_stdout(io.StringIO()):
            return self.poller.handler({}, None)["results"][0]

    def test_buildspec_json_cap_and_bash_syntax(self):
        # Count the escaped representation as well as UTF-8 bytes.
        for indent in (None, 2, 4):
            encoded = json.dumps(BUILD_SPEC, indent=indent).encode()
            self.assertLess(len(encoded), 25_600)
        for name, phase in BUILD_SPEC["phases"].items():
            commands = phase["commands"]
            for command in [*commands, "\n".join(commands)]:
                with self.subTest(phase=name, command=command[:80]):
                    result = subprocess.run(
                        ["bash", "-n"], input=command, capture_output=True,
                        text=True, timeout=5,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)

    def test_manifest_hash_matches_fetch_uploads_without_changing_raw_recipe(self):
        # Unsorted, paginated keys include folder markers, defaults, unsafe
        # paths and decomposed Unicode (hash raw names, normalize downloads).
        pages = [
            {"Contents": [
                {"Key": "uploads/documents/z.pdf", "ETag": '"etag-z"', "Size": "20"},
                {"Key": "uploads/documents/folder/"},
                {"Key": "uploads/documents/../outside.pdf", "ETag": '"bad"', "Size": 4},
            ]},
            {},
            {"Contents": [
                {"Key": "uploads/documents/a.md", "ETag": '"etag-a"', "Size": 3},
                {"Key": "uploads/documents/e\u0301.pdf"},
                {"Key": "uploads/documents/"},
            ]},
        ]
        client = self.poller.s3
        client.get_paginator.return_value.paginate.return_value = pages
        self.boto3.client.return_value = client
        self.fetch.SRC = self.work / "src"
        self.fetch.HASH_OUT = self.work / "content_hash"
        client.download_file.side_effect = (
            lambda bucket, key, dest: Path(dest).write_text("offline document"))
        expected = hashlib.sha256(
            "../outside.pdf\tbad\t4\na.md\tetag-a\t3\ne\u0301.pdf\t\t0\nz.pdf\tetag-z\t20".encode()
        ).hexdigest()
        self.assertEqual(self.poller.files_manifest_hash("documents"), expected)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.fetch.main(), 0)
        self.assertEqual(self.fetch.HASH_OUT.read_text(), expected)
        self.assertTrue((self.fetch.SRC / "é.pdf").exists())
        self.assertFalse((self.work / "outside.pdf").exists())
        client.get_paginator.return_value.paginate.assert_called_with(
            Bucket="offline-bucket", Prefix="uploads/documents/")

    def test_empty_uploads_never_start_a_build(self):
        self.poller.s3.get_paginator.return_value.paginate.return_value = [
            {}, {"Contents": [{"Key": "uploads/documents/"}]}]
        self.assertIsNone(self.poller.files_manifest_hash("documents"))
        result = self.poll({"repo_id": "documents", "source_type": "files"})
        self.assertEqual(result["action"], "no-change")
        self.poller.start_build.assert_not_called()

    def test_files_fingerprint_agreement_and_one_time_migration(self):
        raw_hash = "a" * 64
        (self.work / "content_hash").write_text(raw_hash)
        self.poller.files_manifest_hash = mock.Mock(return_value=raw_hash)
        for images, model, llm in [
            ("0", "", "0"), ("1", "", "0"), ("0", "example-model", "1"),
            ("1", "global.anthropic.claude-sonnet-5", "1"),
        ]:
            with self.subTest(images=images, model=model, llm=llm):
                suffix = f"|img={images}|model={model}|llm={llm}"
                expected = raw_hash + "|doc=2" + suffix
                result = self.run_shell(
                    FINGERPRINT, LLM_IMAGES=images, LLM_MODEL=model, LLM_EXTRACT=llm)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual((self.work / "source_fingerprint").read_text(), expected)
                self.assertEqual(self.events()[-1]["body"], expected)
                item = {
                    "repo_id": "documents", "source_type": "files",
                    "llm_images": images, "llm_model": model, "llm_extract": llm,
                    "last_built_sha": raw_hash + suffix,
                }
                result = self.poll(item)
                self.assertEqual(result["action"], "build-started")
                self.assertEqual(self.poller.start_build.call_args.args[1], expected)
                self.assertEqual(self.poller.claim_build.call_args.args[1], expected)
                result = self.poll({**item, "last_built_sha": expected})
                self.assertEqual(result["action"], "no-change")
                self.poller.start_build.assert_not_called()
        # Registry rows predating the settings also use the same defaults.
        result = self.poll({"repo_id": "documents", "source_type": "files",
                            "last_built_sha": raw_hash})
        self.assertEqual(result["sha"], raw_hash + "|doc=2|img=0|model=|llm=0")

    def test_converter_failure_preserves_rc_and_uploads_history_before_exit(self):
        for upload_failure in ("", "1"):
            with self.subTest(upload_failure=upload_failure):
                self.log.unlink(missing_ok=True)
                result = self.run_shell(
                    BUILD_SPEC["phases"]["build"]["commands"],
                    CONVERTER_RC="7", FAIL_HISTORY=upload_failure,
                    LLM_EXTRACT="1", LLM_IMAGES="1",
                )
                self.assertEqual(result.returncode, 7, result.stderr)
                events = self.events()
                scripts = [e["args"][2] for e in events if e["tool"] == "aws"][:2]
                self.assertEqual(scripts, [
                    "s3://offline-bucket/assets/build_scripts/convert_docs.py",
                    "s3://offline-bucket/assets/build_scripts/document_ocr.py",
                ])
                uploads = [e for e in events if e["tool"] == "aws" and "/history/" in e["args"][3]]
                self.assertEqual([e["args"][3] for e in uploads], [
                    "s3://offline-bucket/history/documents/builds/build-123/document-conversion-report.json",
                    "s3://offline-bucket/history/documents/builds/build-123/document-ocr-audit.jsonl",
                ])
                self.assertEqual(json.loads(uploads[0]["body"]), {"complete": False})
                self.assertEqual(json.loads(uploads[1]["body"]), {"page": 1, "complete": False})
                self.assertEqual([e["tool"] for e in events], ["aws", "aws", "python", "aws", "aws"])
                self.assertEqual((self.work / "document-conversion.rc").read_text(), "7")
                # CodeBuild invokes this phase separately even after failure.
                # Existing output or a SKIP marker must not permit publication.
                (self.work / "graphify-out" / "graph.json").write_text('{"nodes":[{}]}')
                (self.work / "SKIP").touch()
                self.log.unlink()
                post = self.run_shell(BUILD_SPEC["phases"]["post_build"]["commands"])
                self.assertEqual(post.returncode, 7, post.stderr)
                self.assertEqual(self.events(), [])

    def test_missing_result_or_helper_fetch_failure_blocks_publication(self):
        result = self.run_shell(CONVERT, FAIL_HELPER="1")
        self.assertEqual(result.returncode, 45, result.stderr)
        self.assertFalse((self.work / "document-conversion.rc").exists())
        self.assertNotIn("python", [e["tool"] for e in self.events()])
        self.log.unlink()
        result = self.run_shell(BUILD_SPEC["phases"]["post_build"]["commands"])
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.events(), [])

    def test_success_publishes_report_and_snapshot_without_diagnostics(self):
        result = self.run_shell(CONVERT, FAIL_HISTORY="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        (self.work / "content_hash").write_text("raw-hash")
        post_commands = BUILD_SPEC["phases"]["post_build"]["commands"]
        publication = [PUBLISH_GATE, FILES_SNAPSHOT, LATEST_GRAPH, SNAPSHOT, REPORT, FINGERPRINT]
        indexes = [post_commands.index(command) for command in publication]
        self.assertEqual(indexes, sorted(indexes))
        # The 200MB boundary is accepted; the real archive remains small.
        result = self.run_shell(publication, SNAPSHOT_SIZE="209715200")
        self.assertEqual(result.returncode, 0, result.stderr)
        latest = [e for e in self.events() if e["tool"] == "aws" and "/latest/" in e["args"][3]]
        self.assertEqual([e["args"][3].rsplit("/", 1)[-1] for e in latest],
                         ["src.tar.gz", "graph.json", "document-conversion-report.json", "source_hash"])
        self.assertEqual(json.loads(latest[2]["body"]), {"complete": True})
        # The later best-effort lane must not create/upload a second snapshot.
        self.assertEqual(sum(e["tool"] == "tar" for e in self.events()), 1)
        with tarfile.open(self.work / "src.tar.gz") as archive:
            self.assertEqual([m.name for m in archive.getmembers() if m.isfile()],
                             ["./document.pdf.md"])
        # The semantic pass and snapshot share src/; diagnostics and helper
        # state live outside it, so neither can become semantic input.
        self.assertEqual([p.name for p in (self.work / "src").iterdir()],
                         ["document.pdf.md"])
        self.assertEqual(BUILD_SPEC["env"]["variables"]["DOCUMENT_OCR_CACHE_DIR"],
                         "/tmp/work/document-ocr-cache")
        self.assertEqual(BUILD_SPEC["env"]["variables"]["DOCUMENT_OCR_AUDIT_PATH"],
                         "/tmp/work/document-ocr-audit.jsonl")
        self.assertNotIn("document-ocr-cache.tar", json.dumps(BUILD_SPEC))

    def test_files_snapshot_failures_block_graph_and_fingerprint_publication(self):
        result = self.run_shell(CONVERT)
        self.assertEqual(result.returncode, 0, result.stderr)
        (self.work / "content_hash").write_text("raw-hash")
        (self.work / "graphify-out" / "graph.json").write_text('{"nodes":[{}]}')
        for failure, expected_rc in [
            ({"FAIL_SNAPSHOT_CREATE": "1"}, 47),
            ({"FAIL_SNAPSHOT_STAT": "1"}, 48),
            ({"SNAPSHOT_SIZE": "209715201"}, 1),
            ({"FAIL_SNAPSHOT_UPLOAD": "1"}, 49),
        ]:
            with self.subTest(failure=failure):
                self.log.unlink(missing_ok=True)
                # A stale archive cannot mask failure to create the new one.
                (self.work / "src.tar.gz").write_text("stale archive")
                result = self.run_shell(
                    BUILD_SPEC["phases"]["post_build"]["commands"], **failure)
                self.assertEqual(result.returncode, expected_rc, result.stderr)
                uploads = [e["args"][3] for e in self.events()
                           if e["tool"] == "aws" and e["args"][:2] == ["s3", "cp"]]
                expected_uploads = (
                    ["s3://offline-bucket/repos/documents/latest/src.tar.gz"]
                    if "FAIL_SNAPSHOT_UPLOAD" in failure else [])
                self.assertEqual(uploads, expected_uploads)
                self.assertFalse((self.work / "source_fingerprint").exists())

    def test_git_and_url_snapshots_remain_best_effort(self):
        for source_type in ("git", "url"):
            for failure in [
                {"FAIL_SNAPSHOT_CREATE": "1"},
                {"SNAPSHOT_SIZE": "209715201"},
                {"FAIL_SNAPSHOT_UPLOAD": "1"},
            ]:
                with self.subTest(source_type=source_type, failure=failure):
                    self.log.unlink(missing_ok=True)
                    result = self.run_shell(
                        [FILES_SNAPSHOT, SNAPSHOT], SOURCE_TYPE=source_type, **failure)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(sum(e["tool"] == "tar" for e in self.events()), 1)

    def test_git_and_url_paths_are_unchanged(self):
        self.poller.files_manifest_hash = mock.Mock()
        self.poller.resolve_head = mock.Mock(return_value="git-commit")
        for source_type in ("git", "url"):
            with self.subTest(source_type=source_type):
                item = {"repo_id": "documents", "git_url": "https://example.test/repo"}
                if source_type == "url":
                    item["source_type"] = "url"
                result = self.poll(item)
                expected = "git-commit" if source_type == "git" else "crawl-123"
                self.assertEqual(result["sha"], expected)
                self.assertNotIn("|doc=", result["sha"])
                # The new converter gate must not affect other source types.
                (self.work / "document-conversion.rc").write_text("7")
                result = self.run_shell([CONVERT, PUBLISH_GATE, REPORT], SOURCE_TYPE=source_type)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.events(), [])
        self.poller.files_manifest_hash.assert_not_called()
        self.poller.resolve_head.assert_called_once()
        (self.work / "content_hash").write_text("crawl-hash")
        result = self.run_shell(
            [command_containing("pre_build", "python /tmp/docs_crawler.py"), FINGERPRINT],
            SOURCE_TYPE="url", LLM_EXTRACT="1", LLM_IMAGES="1",
            LLM_MODEL="example-model", PRUNE_PATHS="archive",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            (self.work / "source_fingerprint").read_text(),
            f"crawl-hash|graphifyy={BUILD_SPEC['env']['variables']['GRAPHIFY_VERSION']}"
            "|prune=archive|viz=1|model=example-model|llm=1",
        )


if __name__ == "__main__":
    unittest.main()
