"""Offline build diagnostics contracts; all AWS boundaries are strict mocks.

Run: .venv/bin/python -B -m unittest discover -s tests -p test_build_diagnostics.py
"""

import copy
from datetime import datetime, timedelta, timezone
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import textwrap
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
PROJECT = "offline-graph-build"
REPO = "offline-repository"
BUILD_ID = PROJECT + ":11111111-2222-3333-4444-555555555555"
BUILD_ARN = "arn:aws:codebuild:us-east-1:123456789012:build/" + BUILD_ID
LOG_GROUP = "/aws/codebuild/" + PROJECT
STREAM = "11111111-2222-3333-4444-555555555555"
START = datetime(2026, 9, 14, 1, 0, tzinfo=timezone.utc)


def load_diagnostics():
    spec = importlib.util.spec_from_file_location(
        "offline_build_diagnostics",
        ROOT / "lambdas/platform_api/build_diagnostics.py",
    )
    module = importlib.util.module_from_spec(spec)
    # Importing this pure helper must never construct a live SDK client.
    boto = mock.Mock()
    boto.client.side_effect = AssertionError("Live AWS clients are forbidden")
    boto.resource.side_effect = AssertionError("Live AWS resources are forbidden")
    with mock.patch.dict(sys.modules, {"boto3": boto}):
        spec.loader.exec_module(module)
    return module


class AwsFailure(Exception):
    """Botocore-shaped exception without requiring the SDK."""

    def __init__(self, code, message="Synthetic offline service failure"):
        super().__init__(message)
        self.response = {"Error": {"Code": code, "Message": message}}


def build_fixture(status="FAILED"):
    return {
        "id": BUILD_ID,
        "arn": BUILD_ARN,
        "projectName": PROJECT,
        "buildStatus": status,
        "currentPhase": "COMPLETED" if status != "IN_PROGRESS" else "BUILD",
        "startTime": START,
        "endTime": START + timedelta(seconds=42),
        "environment": {
            "environmentVariables": [
                {"name": "REPO_ID", "value": REPO, "type": "PLAINTEXT"},
            ],
        },
        "phases": [
            {"phaseType": "SUBMITTED", "phaseStatus": "SUCCEEDED", "durationInSeconds": 0},
            {"phaseType": "PROVISIONING", "phaseStatus": "SUCCEEDED", "durationInSeconds": 12},
            {
                "phaseType": "BUILD", "phaseStatus": status, "durationInSeconds": 30,
                "contexts": [
                    {"statusCode": "COMMAND_EXECUTION_ERROR",
                     "message": "Error while executing command: graphify build. exit status 1"},
                ],
            },
        ],
        "logs": {"groupName": LOG_GROUP, "streamName": STREAM, "cloudWatchLogsArn":
                 "arn:aws:logs:us-east-1:123456789012:log-group:" + LOG_GROUP},
    }


class DiagnosticsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.diagnostics = load_diagnostics()

    def setUp(self):
        blocker = mock.patch.object(
            socket, "create_connection", side_effect=AssertionError("No network in offline tests"),
        )
        blocker.start()
        self.addCleanup(blocker.stop)
        self.item = {
            "repo_id": REPO, "source_type": "files", "status": "FAILED",
            "build_id": BUILD_ID, "build_arn": BUILD_ARN, "last_error": "Build failed",
        }
        self.build = build_fixture()
        self.codebuild = mock.Mock(spec_set=["batch_get_builds"])
        self.codebuild.batch_get_builds.return_value = {"builds": [self.build]}
        self.logs = mock.Mock(spec_set=["get_log_events"])
        self.logs.get_log_events.return_value = {
            "events": [{"timestamp": 1, "message": "Conversion failed: document is malformed"}],
            "nextBackwardToken": "older-page", "nextForwardToken": "newer-page",
        }

    def describe(self, **kwargs):
        return self.diagnostics.describe_build(
            self.item, self.codebuild, self.logs, PROJECT, **kwargs,
        )

    def assert_error(self, status, **kwargs):
        with self.assertRaises(self.diagnostics.DiagnosticsError) as raised:
            self.describe(**kwargs)
        self.assertEqual(raised.exception.status, status)
        self.logs.get_log_events.assert_not_called()

    def test_failed_build_preserves_each_phase_and_command_failure_context(self):
        result = self.describe()
        phases = result["build"]["phases"]
        self.assertEqual([phase["name"] for phase in phases],
                         ["SUBMITTED", "PROVISIONING", "BUILD"])
        self.assertEqual(phases[-1]["messages"], [{
            "code": "COMMAND_EXECUTION_ERROR",
            "message": "Error while executing command: graphify build. exit status 1",
        }])
        self.assertEqual(phases[-1]["duration_seconds"], 30)

    def test_build_result_contains_json_serializable_timing_and_identity(self):
        result = self.describe()
        self.assertEqual(result["repo_id"], REPO)
        self.assertEqual(result["source_status"], "FAILED")
        self.assertEqual(result["build"]["id"], BUILD_ID)
        self.assertEqual(result["build"]["status"], "FAILED")
        self.assertEqual(result["build"]["current_phase"], "COMPLETED")
        self.assertEqual(result["build"]["duration_seconds"], 42)
        self.assertTrue(result["build"]["started_at"])
        self.assertTrue(result["build"]["ended_at"])
        json.dumps(result)

    def test_submitted_and_provisioning_failures_keep_their_own_context(self):
        for phase in ("SUBMITTED", "PROVISIONING"):
            with self.subTest(phase=phase):
                self.build["phases"] = [{
                    "phaseType": phase, "phaseStatus": "FAILED", "durationInSeconds": 1,
                    "contexts": [{"statusCode": "CLIENT_ERROR", "message": "Unable to provision build"}],
                }]
                result = self.describe()
                self.assertEqual(result["build"]["phases"][0]["messages"],
                                 [{"code": "CLIENT_ERROR", "message": "Unable to provision build"}])

    def test_no_build_returns_redacted_registry_error_without_aws_calls(self):
        self.item.pop("build_id")
        self.item.pop("build_arn")
        self.item["last_error"] = "Artifact failed Authorization: Bearer synthetic-secret-value"
        result = self.describe()
        self.assertIsNone(result["build"])
        self.assertEqual(result["logs"]["state"], "not_started")
        self.assertIn("Artifact failed", result["last_error"])
        self.assertNotIn("synthetic-secret-value", result["last_error"])
        self.codebuild.batch_get_builds.assert_not_called()
        self.logs.get_log_events.assert_not_called()

    def test_build_arn_is_accepted_when_registry_has_no_build_id(self):
        self.item.pop("build_id")
        result = self.describe()
        self.assertEqual(result["build"]["id"], BUILD_ID)
        self.codebuild.batch_get_builds.assert_called_once()

    def test_inflight_build_ignores_stale_registry_error(self):
        self.build["buildStatus"] = "IN_PROGRESS"
        self.build["currentPhase"] = "BUILD"
        self.build.pop("endTime")
        self.item["last_error"] = "Stale failure from previous build"
        result = self.describe()
        self.assertFalse(result["last_error"])
        self.assertFalse(result["can_rebuild"])

    def test_successful_build_retains_registry_artifact_failure(self):
        self.build["buildStatus"] = "SUCCEEDED"
        self.item["last_error"] = "Artifact publication failed: graph.json is missing"
        result = self.describe()
        self.assertEqual(result["last_error"], self.item["last_error"])
        self.assertTrue(result["can_rebuild"])

    def test_terminal_build_statuses_allow_rebuild(self):
        for status in ("FAILED", "FAULT", "STOPPED", "TIMED_OUT", "SUCCEEDED"):
            with self.subTest(status=status):
                self.build["buildStatus"] = status
                self.assertTrue(self.describe()["can_rebuild"])

    def test_log_request_reads_only_latest_hundred_events_from_selected_stream(self):
        result = self.describe()
        self.logs.get_log_events.assert_called_once_with(
            logGroupName=LOG_GROUP, logStreamName=STREAM, limit=100, startFromHead=False,
        )
        self.assertEqual(result["logs"]["state"], "available")
        self.assertEqual(result["logs"]["events"],
                         [{"timestamp": 1, "message": "Conversion failed: document is malformed"}])
        self.assertEqual(result["logs"]["next_token"], "older-page")
        self.codebuild.batch_get_builds.assert_called_once()

    def test_cursor_uses_backward_token_bound_to_requested_current_build(self):
        result = self.describe(next_token="previous-page", requested_build_id=BUILD_ID)
        self.assertEqual(self.logs.get_log_events.call_args.kwargs["nextToken"], "previous-page")
        self.assertEqual(result["logs"]["next_token"], "older-page")

    def test_unchanged_backward_token_ends_pagination(self):
        self.logs.get_log_events.return_value["nextBackwardToken"] = "same-page"
        result = self.describe(next_token="same-page", requested_build_id=BUILD_ID)
        self.assertIsNone(result["logs"]["next_token"])

    def test_empty_page_preserves_advancing_cursor_without_fetching_another_page(self):
        self.logs.get_log_events.return_value["events"] = []
        result = self.describe(next_token="previous-page", requested_build_id=BUILD_ID)
        self.assertEqual(result["logs"]["events"], [])
        self.assertEqual(result["logs"]["next_token"], "older-page")
        self.logs.get_log_events.assert_called_once()

    def test_empty_page_with_nonadvancing_token_ends_pagination(self):
        self.logs.get_log_events.return_value.update(events=[], nextBackwardToken="previous-page")
        result = self.describe(next_token="previous-page", requested_build_id=BUILD_ID)
        self.assertIsNone(result["logs"]["next_token"])
        self.logs.get_log_events.assert_called_once()

    def test_response_without_backward_token_ends_pagination(self):
        self.logs.get_log_events.return_value.pop("nextBackwardToken")
        self.assertIsNone(self.describe()["logs"]["next_token"])

    def test_requested_previous_build_is_rejected_before_logs(self):
        self.assert_error(409, requested_build_id=PROJECT + ":previous-build")

    def test_cursor_without_build_binding_is_rejected(self):
        self.assert_error(400, next_token="unbound-cursor")

    def test_oversized_cursor_is_rejected(self):
        self.assert_error(400, next_token="x" * 2049, requested_build_id=BUILD_ID)

    def test_control_characters_in_cursor_are_rejected(self):
        for control in ("\x00", "\t", "\n", "\r", "\x1f", "\x7f"):
            with self.subTest(control=repr(control)):
                self.assert_error(400, next_token="cursor" + control, requested_build_id=BUILD_ID)

    def test_maximum_length_cursor_is_accepted(self):
        self.describe(next_token="x" * 2048, requested_build_id=BUILD_ID)
        self.assertEqual(len(self.logs.get_log_events.call_args.kwargs["nextToken"]), 2048)

    def test_registry_build_from_another_project_is_denied(self):
        self.item["build_id"] = "foreign-project:11111111-2222-3333-4444-555555555555"
        self.assert_error(403)

    def test_malformed_registry_build_id_is_denied(self):
        self.item["build_id"] = "not-a-project-build-id"
        self.assert_error(403)

    def test_empty_registry_repository_identity_is_denied(self):
        self.item["repo_id"] = ""
        self.assert_error(403)

    def test_returned_build_from_another_project_is_denied(self):
        self.build["projectName"] = "foreign-project"
        self.assert_error(403)

    def test_returned_different_build_id_is_denied(self):
        self.build["id"] = PROJECT + ":different-build"
        self.assert_error(403)

    def test_returned_build_bound_to_another_repository_is_denied(self):
        self.build["environment"]["environmentVariables"][0]["value"] = "other-repository"
        self.assert_error(403)

    def test_returned_build_without_repository_binding_is_denied(self):
        self.build["environment"]["environmentVariables"] = []
        self.assert_error(403)

    def test_returned_build_without_environment_is_denied(self):
        self.build.pop("environment")
        self.assert_error(403)

    def test_ambiguous_repository_bindings_are_denied(self):
        self.build["environment"]["environmentVariables"].append(
            {"name": "REPO_ID", "value": "another-repository", "type": "PLAINTEXT"},
        )
        self.assert_error(403)

    def test_unapproved_log_group_never_receives_a_log_request(self):
        self.build["logs"]["groupName"] = "/aws/codebuild/another-project"
        self.assert_error(403)

    def test_inflight_build_without_log_stream_reports_pending(self):
        self.build["buildStatus"] = "IN_PROGRESS"
        self.build["logs"].pop("streamName")
        result = self.describe()
        self.assertEqual(result["logs"]["state"], "pending")
        self.logs.get_log_events.assert_not_called()

    def test_terminal_build_without_log_stream_reports_missing(self):
        self.build["logs"].pop("streamName")
        self.assertEqual(self.describe()["logs"]["state"], "missing")
        self.logs.get_log_events.assert_not_called()

    def test_inflight_stream_not_yet_created_reports_pending(self):
        self.build["buildStatus"] = "IN_PROGRESS"
        self.logs.get_log_events.side_effect = AwsFailure("ResourceNotFoundException")
        self.assertEqual(self.describe()["logs"]["state"], "pending")
        self.logs.get_log_events.assert_called_once()

    def test_empty_log_page_is_available(self):
        self.logs.get_log_events.return_value["events"] = []
        self.assertEqual(self.describe()["logs"]["state"], "available")

    def test_expired_cloudwatch_stream_reports_missing_and_retains_phase_context(self):
        self.logs.get_log_events.side_effect = AwsFailure("ResourceNotFoundException")
        result = self.describe()
        self.assertEqual(result["logs"]["state"], "missing")
        self.assertTrue(result["build"]["phases"][-1]["messages"])
        self.logs.get_log_events.assert_called_once()

    def test_cloudwatch_access_failure_is_controlled_and_retains_phase_context(self):
        self.logs.get_log_events.side_effect = AwsFailure("AccessDeniedException")
        result = self.describe()
        self.assertEqual(result["logs"]["state"], "unavailable")
        self.assertTrue(result["build"]["phases"][-1]["messages"])
        self.logs.get_log_events.assert_called_once()

    def test_arbitrary_log_client_failure_returns_finite_fallback(self):
        self.logs.get_log_events.side_effect = RuntimeError("offline transport failure")
        result = self.describe()
        self.assertEqual(result["logs"]["state"], "unavailable")
        self.assertTrue(result["build"]["phases"][-1]["messages"])
        self.logs.get_log_events.assert_called_once()

    def test_invalid_cloudwatch_cursor_is_a_controlled_bad_request(self):
        self.logs.get_log_events.side_effect = AwsFailure(
            "InvalidParameterException", "The specified nextToken is invalid",
        )
        with self.assertRaises(self.diagnostics.DiagnosticsError) as raised:
            self.describe(next_token="expired-token", requested_build_id=BUILD_ID)
        self.assertEqual(raised.exception.status, 400)
        self.logs.get_log_events.assert_called_once()

    def test_arbitrary_codebuild_failure_returns_finite_fallback(self):
        self.codebuild.batch_get_builds.side_effect = RuntimeError("offline transport failure")
        result = self.describe()
        self.assertIsNone(result["build"])
        self.assertEqual(result["logs"]["state"], "unavailable")
        self.codebuild.batch_get_builds.assert_called_once()
        self.logs.get_log_events.assert_not_called()

    def test_codebuild_service_failure_returns_safe_error_code(self):
        self.codebuild.batch_get_builds.side_effect = AwsFailure("AccessDeniedException")
        result = self.describe()
        self.assertEqual(result["logs"]["error_code"], "AccessDeniedException")
        self.assertEqual(result["last_error"], self.item["last_error"])
        self.codebuild.batch_get_builds.assert_called_once()
        self.logs.get_log_events.assert_not_called()

    def test_invalid_parameter_without_cursor_is_unavailable_instead_of_bad_request(self):
        self.logs.get_log_events.side_effect = AwsFailure("InvalidParameterException")
        self.assertEqual(self.describe()["logs"]["state"], "unavailable")
        self.logs.get_log_events.assert_called_once()

    def test_service_exception_text_is_not_exposed_to_caller(self):
        self.logs.get_log_events.side_effect = AwsFailure(
            "AccessDeniedException", "Authorization: Bearer offline-exception-secret",
        )
        self.assertNotIn("offline-exception-secret", json.dumps(self.describe()))

    def test_missing_codebuild_record_does_not_scan_history(self):
        self.codebuild.batch_get_builds.return_value = {"builds": [], "buildsNotFound": [BUILD_ID]}
        result = self.describe()
        self.assertIsNone(result["build"])
        self.assertEqual(result["logs"]["state"], "missing")
        self.codebuild.batch_get_builds.assert_called_once()
        self.logs.get_log_events.assert_not_called()

    def test_phase_context_and_log_secrets_are_redacted(self):
        secret = "synthetic-phase-secret"
        self.build["phases"][-1]["contexts"][0]["message"] = "Authorization: Bearer " + secret
        self.logs.get_log_events.return_value["events"][0]["message"] = "api_key=synthetic-log-secret"
        rendered = json.dumps(self.describe())
        self.assertNotIn(secret, rendered)
        self.assertNotIn("synthetic-log-secret", rendered)

    def test_environment_variables_never_appear_in_diagnostics(self):
        self.build["environment"]["environmentVariables"].append(
            {"name": "CUSTOM_CONFIGURATION", "value": "never-return-environment-value"},
        )
        self.assertNotIn("never-return-environment-value", json.dumps(self.describe()))

    def test_oversized_log_page_is_bounded_to_one_hundred_events(self):
        self.logs.get_log_events.return_value["events"] = [
            {"timestamp": index, "message": str(index)} for index in range(150)
        ]
        self.assertEqual(len(self.describe()["logs"]["events"]), 100)
        self.logs.get_log_events.assert_called_once()

    def test_long_log_message_is_redacted_before_three_thousand_character_cutoff(self):
        self.logs.get_log_events.return_value["events"][0]["message"] = (
            "x" * 2992 + " AKIAIOSFODNN7EXAMPLE " + "y" * 2000
        )
        result = self.describe()["logs"]
        self.assertTrue(result["truncated"])
        self.assertNotIn("AKIAIOS", result["events"][0]["message"])
        self.assertLessEqual(len(result["events"][0]["message"]), 3000 + len("\n[…truncated]"))

    def test_short_log_message_is_not_marked_truncated(self):
        self.assertFalse(self.describe()["logs"]["truncated"])

    def test_failed_phase_context_produces_hint_when_logs_are_unavailable(self):
        examples = (
            ("git_auth", "fatal: Authentication failed for https://example.com/repo"),
            ("bedrock_access", "AccessDeniedException: not authorized to invoke Bedrock model"),
            ("throttled", "ThrottlingException: too many requests"),
            ("timeout", "Command timed out"),
            ("memory", "Command terminated with exit status 137"),
            ("document_conversion", "Document conversion incomplete: encrypted PDF"),
            ("source_download", "Failed to download source: NoSuchKey"),
            ("artifact_publish", "Failed to publish graph artifact"),
        )
        self.logs.get_log_events.side_effect = AwsFailure("ResourceNotFoundException")
        for expected_hint, message in examples:
            with self.subTest(hint=expected_hint):
                self.build["phases"][-1]["contexts"][0]["message"] = message
                self.assertIn(expected_hint, self.describe()["hints"])

    def test_failure_log_text_produces_hint_without_phase_context(self):
        self.build["phases"][-1]["contexts"] = []
        self.logs.get_log_events.return_value["events"][0]["message"] = "Out of memory"
        self.assertIn("memory", self.describe()["hints"])

    def test_successful_phase_context_does_not_generate_failure_hint(self):
        self.build["phases"][-1]["phaseStatus"] = "SUCCEEDED"
        self.build["phases"][-1]["contexts"][0]["message"] = "Out of memory"
        self.assertNotIn("memory", self.describe()["hints"])

    def test_successful_build_with_ready_registry_has_no_failure_hints(self):
        self.item.update(status="READY", last_error="")
        self.build["buildStatus"] = "SUCCEEDED"
        self.logs.get_log_events.return_value["events"][0]["message"] = "Out of memory"
        self.assertEqual(self.describe()["hints"], [])

    def test_inflight_build_has_no_failure_hints_even_with_error_log_text(self):
        self.build["buildStatus"] = "IN_PROGRESS"
        self.logs.get_log_events.return_value["events"][0]["message"] = "Out of memory"
        self.assertEqual(self.describe()["hints"], [])

    def test_describe_does_not_mutate_registry_or_build_fixtures(self):
        before_item, before_build = copy.deepcopy(self.item), copy.deepcopy(self.build)
        self.describe()
        self.assertEqual(self.item, before_item)
        self.assertEqual(self.build, before_build)


class RedactionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.diagnostics = load_diagnostics()
        spec = importlib.util.spec_from_file_location(
            "offline_diagnostics_keys", ROOT / "lambdas/platform_api/keys.py",
        )
        cls.keys = importlib.util.module_from_spec(spec)
        with mock.patch.dict(sys.modules, {"boto3": mock.Mock(spec_set=["client"])}), \
                mock.patch.dict(os.environ, {"AWS_REGION": "us-east-1"}):
            spec.loader.exec_module(cls.keys)

    def test_plain_failure_message_remains_readable(self):
        text = "Command failed: malformed PDF on page 7"
        self.assertEqual(self.diagnostics.redact(text), text)

    def test_common_credentials_are_removed(self):
        cases = (
            ("Authorization: Bearer offline-secret-bearer", "offline-secret-bearer"),
            ("Authorization: Basic dXNlcjpvZmZsaW5l", "dXNlcjpvZmZsaW5l"),
            ("api_key=offline-secret-api-key", "offline-secret-api-key"),
            ("X-Api-Key: offline-secret-header-key", "offline-secret-header-key"),
            ("AWS_SECRET_ACCESS_KEY=offline-secret-aws-key", "offline-secret-aws-key"),
            ("token=offline-secret-token", "offline-secret-token"),
            ("password=offline-secret-password", "offline-secret-password"),
            ("https://user:offline-secret-password@example.com/repo", "offline-secret-password"),
            ("key AKIAIOSFODNN7EXAMPLE was rejected", "AKIAIOSFODNN7EXAMPLE"),
            ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJvZmZsaW5lIn0.c3ludGhldGljc2lnbmF0dXJl",
             "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJvZmZsaW5lIn0.c3ludGhldGljc2lnbmF0dXJl"),
        )
        for text, secret in cases:
            with self.subTest(text=text):
                self.assertNotIn(secret, self.diagnostics.redact(text))

    def test_presigned_url_credentials_and_signature_are_removed(self):
        text = (
            "Download https://offline-bucket.s3.amazonaws.com/file.pdf?"
            "X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Credential=offline-credential"
            "&X-Amz-Security-Token=offline-session-secret&X-Amz-Signature=offline-signature"
        )
        result = self.diagnostics.redact(text)
        for secret in ("offline-credential", "offline-session-secret", "offline-signature"):
            self.assertNotIn(secret, result)

    def test_url_userinfo_with_apostrophe_is_fully_redacted(self):
        self.assertEqual(
            self.diagnostics.redact("https://user:pa'ss@example.com/repo"),
            "https://[REDACTED]@example.com/repo",
        )

    def test_adjacent_urls_in_compact_json_each_redact_credentials(self):
        text = json.dumps(
            ["https://public.example/file", "https://user:pa'ss@private.example/file"],
            separators=(",", ":"),
        )
        self.assertEqual(
            self.diagnostics.redact(text),
            '["https://public.example/file","https://[REDACTED]@private.example/file"]',
        )

    def test_quoted_json_and_shell_secrets_with_spaces_are_removed(self):
        for text in (
            '{"api_key": "offline secret with spaces"}',
            "password='offline secret with spaces'",
        ):
            with self.subTest(text=text):
                self.assertNotIn("offline secret", self.diagnostics.redact(text))

    def assert_graphify_key_is_fully_redacted(self, mode):
        # Real minting/checksum code, deterministic offline entropy. These bytes
        # produce a 43-character base64url secret beginning "-_"; a regex that
        # only consumes alphanumerics/underscores leaks the secret and checksum.
        with mock.patch.object(self.keys.secrets, "choice", side_effect="0123456789AB"), \
                mock.patch.object(self.keys.secrets, "token_bytes",
                                  return_value=b"\xfb\xff" + bytes(range(30))):
            key = self.keys.mint_key(mode)["plaintext"]
        self.assertTrue(key.startswith(f"gfy_{mode}_0123456789AB_-_"))
        text = f"Rejected key [{key}] during request"
        self.assertEqual(self.diagnostics.redact(text),
                         "Rejected key [[REDACTED]] during request")

    def test_graphify_live_key_with_leading_hyphen_secret_is_fully_redacted(self):
        self.assert_graphify_key_is_fully_redacted("live")

    def test_graphify_test_key_with_leading_hyphen_secret_is_fully_redacted(self):
        self.assert_graphify_key_is_fully_redacted("test")

    def test_json_password_with_escaped_quote_hides_entire_secret(self):
        text = json.dumps({"password": 'first"remaining-secret', "status": "failed"})
        self.assertEqual(self.diagnostics.redact(text),
                         '{"password": [REDACTED], "status": "failed"}')

    def test_shell_password_with_escaped_quote_hides_entire_secret(self):
        text = r'graphify --password "first\"remaining-secret" --verbose'
        self.assertEqual(self.diagnostics.redact(text),
                         "graphify --password [REDACTED] --verbose")

    def test_unterminated_json_password_hides_secret_through_end_of_input(self):
        for ending in ("", "\\"):
            with self.subTest(trailing_backslash=bool(ending)):
                text = r'{"password": "first\"remaining-secret' + ending
                self.assertEqual(self.diagnostics.redact(text), '{"password": [REDACTED]')

    def test_unterminated_shell_password_hides_secret_through_end_of_input(self):
        for ending in ("", "\\"):
            with self.subTest(trailing_backslash=bool(ending)):
                text = r'graphify --password "first\"remaining-secret' + ending
                self.assertEqual(self.diagnostics.redact(text), "graphify --password [REDACTED]")

    def test_private_key_body_is_removed(self):
        # Generate a PEM-shaped redaction fixture without embedding key material.
        kind = "PRIVATE KEY"
        text = "\n".join((f"-----BEGIN {kind}-----", "offline-private-key", f"-----END {kind}-----"))
        self.assertNotIn("offline-private-key", self.diagnostics.redact(text))

    def test_terminal_escape_sequences_do_not_hide_credentials(self):
        text = "\x1b[31mAuthorization:\x1b[0m Bearer offline-colored-secret"
        result = self.diagnostics.redact(text)
        self.assertNotIn("offline-colored-secret", result)
        self.assertNotIn("\x1b", result)

    def test_credentials_are_redacted_before_output_truncation(self):
        # Cutting this access key first would destroy the full-key regex match.
        text = "failure: " + "x" * 30 + " AKIAIOSFODNN7EXAMPLE rejected"
        result = self.diagnostics.redact(text, limit=50)
        self.assertNotIn("AKIAIOS", result)
        self.assertLessEqual(len(result), 50 + len("\n[…truncated]"))


class BoundedDiagnosticsRegressionTests(unittest.TestCase):
    def run_offline_probe(self, source):
        # Regex backtracking can hold the GIL: threads/elapsed-time assertions
        # cannot safely stop it. run() kills and reaps the child on timeout.
        prelude = (
            "import json, socket, sys\n"
            f"sys.path.insert(0, {str(ROOT / 'tests')!r})\n"
            "from test_build_diagnostics import "
            "load_diagnostics, build_fixture, PROJECT, REPO, BUILD_ID, mock\n"
            "mock.patch.object(socket, 'create_connection', "
            "side_effect=AssertionError('No network in offline probe')).start()\n"
            "diagnostics = load_diagnostics()\n"
        )
        try:
            completed = subprocess.run(
                [sys.executable, "-B", "-c", prelude + textwrap.dedent(source)],
                cwd=ROOT, capture_output=True, text=True, timeout=5, check=False,
            )
        except subprocess.TimeoutExpired:
            self.fail("Offline diagnostics probe exceeded its five-second deadline")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout)

    def test_repeated_bedrock_words_across_hundred_events_finish_within_five_seconds(self):
        result = self.run_offline_probe("""
            item = {"repo_id": REPO, "source_type": "files",
                    "status": "FAILED", "build_id": BUILD_ID}
            codebuild = mock.Mock(spec_set=["batch_get_builds"])
            codebuild.batch_get_builds.return_value = {"builds": [build_fixture()]}
            logs = mock.Mock(spec_set=["get_log_events"])
            logs.get_log_events.return_value = {
                "events": [{"timestamp": i, "message": "bedrock " * 375}
                           for i in range(100)]
            }
            result = diagnostics.describe_build(item, codebuild, logs, PROJECT)
            print(json.dumps({"event_count": len(result["logs"]["events"]),
                              "hints": result["hints"]}))
        """)
        self.assertEqual(result, {"event_count": 100, "hints": ["check_logs"]})

    def test_repeated_queryless_url_schemes_are_redacted_within_five_seconds(self):
        result = self.run_offline_probe("""
            text = "https://" * 20000 + "offline.example/path"
            redacted = diagnostics.redact(text, limit=3000)
            print(json.dumps({"preserved_prefix": redacted.startswith("https://https://"),
                              "output_length": len(redacted)}))
        """)
        self.assertTrue(result["preserved_prefix"])
        self.assertLessEqual(result["output_length"], 3000 + len("\n[…truncated]"))

    def test_unterminated_repeated_osc_sequences_finish_within_five_seconds(self):
        result = self.run_offline_probe("""
            text = "x\\x1b]" * 65000
            redacted = diagnostics.redact(text)
            print(json.dumps({"redacted": redacted}))
        """)
        self.assertEqual(result, {"redacted": "x"})


class HandlerGrantBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.diagnostics = load_diagnostics()
        fake_boto = mock.MagicMock()
        modules = {
            "boto3": fake_boto,
            "botocore.config": mock.MagicMock(),
            "gitreg": mock.MagicMock(),
            "keys": mock.MagicMock(),
            "groups_api": mock.MagicMock(),
            "group_access": mock.MagicMock(),
            "group_query": mock.MagicMock(),
            "runtimes": mock.MagicMock(),
            "build_diagnostics": self.diagnostics,
        }
        environment = {name: "offline-" + name.lower() for name in (
            "AWS_REGION", "PLATFORM_TABLE", "REGISTRY_TABLE", "PROJECT_NAME", "GRAPH_BUCKET",
            "MCP_BASE_URL", "USAGE_PLAN_ID", "USER_POOL_ID", "ECS_CLUSTER", "TASK_IMAGE",
            "TASK_ROLE_ARN", "TASK_EXEC_ROLE_ARN", "TASK_SUBNETS", "TASK_SECURITY_GROUP",
            "CLOUDMAP_NAMESPACE_ID", "SERVICE_LOG_GROUP",
        )}
        spec = importlib.util.spec_from_file_location(
            "offline_diagnostics_handler", ROOT / "lambdas/platform_api/handler.py",
        )
        self.handler = importlib.util.module_from_spec(spec)
        with mock.patch.dict(sys.modules, modules), mock.patch.dict(os.environ, environment):
            spec.loader.exec_module(self.handler)
        self.handler.PROJECT_NAME = PROJECT
        self.handler._platform = mock.Mock(spec_set=["get_item"])
        self.handler._platform.get_item.return_value = {
            "Item": {"pk": "USER#offline-user", "sk": "REPO#" + REPO},
        }
        self.handler._registry = mock.Mock(spec_set=["get_item"])
        self.handler._registry.get_item.return_value = {"Item": {
            "repo_id": REPO, "enabled": "1", "source_type": "files",
            "status": "FAILED", "build_id": BUILD_ID,
        }}
        self.handler._build_info = mock.Mock(spec_set=["batch_get_builds"])
        self.handler._build_info.batch_get_builds.return_value = {"builds": [build_fixture()]}
        self.handler._build_logs = mock.Mock(spec_set=["get_log_events"])
        self.handler._build_logs.get_log_events.return_value = {"events": []}

    def invoke(self, query=None):
        return self.handler.get_repo_build(
            {"queryStringParameters": query}, {"sub": "offline-user"}, {"repoId": REPO},
        )

    def test_missing_source_grant_denies_before_registry_or_build_reads(self):
        self.handler._platform.get_item.return_value = {}
        with self.assertRaises(self.handler.ApiError) as raised:
            self.invoke()
        self.assertEqual(raised.exception.status, 404)
        self.handler._registry.get_item.assert_not_called()
        self.handler._build_info.batch_get_builds.assert_not_called()
        self.handler._build_logs.get_log_events.assert_not_called()

    def test_authorized_source_reads_latest_registry_with_strong_consistency(self):
        response = self.invoke()
        self.assertEqual(response["statusCode"], 200)
        self.assertEqual(json.loads(response["body"])["build"]["id"], BUILD_ID)
        self.handler._platform.get_item.assert_called_once_with(
            Key={"pk": "USER#offline-user", "sk": "REPO#" + REPO},
        )
        self.handler._registry.get_item.assert_called_once_with(
            Key={"repo_id": REPO}, ConsistentRead=True,
        )

    def test_disabled_source_is_denied_before_build_reads(self):
        self.handler._registry.get_item.return_value["Item"]["enabled"] = "0"
        with self.assertRaises(self.handler.ApiError) as raised:
            self.invoke()
        self.assertEqual(raised.exception.status, 404)
        self.handler._build_info.batch_get_builds.assert_not_called()
        self.handler._build_logs.get_log_events.assert_not_called()

    def test_missing_registry_source_is_denied_before_build_reads(self):
        self.handler._registry.get_item.return_value = {}
        with self.assertRaises(self.handler.ApiError) as raised:
            self.invoke()
        self.assertEqual(raised.exception.status, 404)
        self.handler._build_info.batch_get_builds.assert_not_called()
        self.handler._build_logs.get_log_events.assert_not_called()

    def test_caller_cannot_override_log_group_or_stream(self):
        for parameter in ("log_group", "log_stream"):
            with self.subTest(parameter=parameter):
                with self.assertRaises(self.handler.ApiError) as raised:
                    self.invoke({parameter: "caller-selected"})
                self.assertEqual(raised.exception.status, 400)
                self.handler._build_info.batch_get_builds.assert_not_called()
                self.handler._build_logs.get_log_events.assert_not_called()

    def test_cursor_for_previous_build_returns_conflict_through_handler(self):
        with self.assertRaises(self.handler.ApiError) as raised:
            self.invoke({"build_id": PROJECT + ":previous-build", "next_token": "previous-page"})
        self.assertEqual(raised.exception.status, 409)
        self.handler._build_logs.get_log_events.assert_not_called()

    def test_authorized_handler_passes_bound_cursor_to_selected_build(self):
        self.invoke({"build_id": BUILD_ID, "next_token": "previous-page"})
        self.handler._build_logs.get_log_events.assert_called_once_with(
            logGroupName=LOG_GROUP, logStreamName=STREAM, limit=100,
            startFromHead=False, nextToken="previous-page",
        )


if __name__ == "__main__":
    unittest.main()
