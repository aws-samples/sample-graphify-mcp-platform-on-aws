"""Offline scheduled reconciliation with the real ACL and atomic DDB contracts.

Run: .venv/bin/python -B -m unittest discover -s tests -p test_group_reconcile.py
"""

import copy
from decimal import Decimal
import importlib.util
import json
import os
from pathlib import Path
import socket
import sys
import types
import unittest
from unittest import mock

import boto3

from test_groups_api import (
    MemoryDynamo, MemoryLambda, PLATFORM, REGISTRY, typed, decoded, aws_error,
)


ROOT = Path(__file__).resolve().parents[1]
GID, SUB, WORKER = "grp_" + "a" * 32, "owner-sub", "offline-worker"
NOW = 1_800_000_000
STATE = {"pk": "SYSTEM#GROUP_RECONCILE", "sk": "CURSOR"}
INDEX_KEYS = ("pk", "sk", "gsi1pk", "gsi1sk")


def index_key(gid):
    return {"pk": "GROUP#" + gid, "sk": "META", "gsi1pk": "GROUPS", "gsi1sk": gid}


def load(relative, name):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class IndexDynamo(MemoryDynamo):
    def __init__(self):
        super().__init__()
        self.queries = []
        self.after_query = None
        self.after_get = None
        self.before_update = None
        self.query_error = None
        self.pages = None
        self.commit_then_fail = False

    def scan(self, **request):
        raise AssertionError("Mixed PlatformTable scans are forbidden")

    def query(self, **request):
        self.queries.append(copy.deepcopy(request))
        assert request["IndexName"] == "entity-index"
        assert request["Limit"] == 100
        assert "ConsistentRead" not in request
        assert "FilterExpression" not in request
        assert decoded(request["ExpressionAttributeValues"]) == {":groups": "GROUPS"}
        assert request["ExpressionAttributeNames"]["#gpk"] == "gsi1pk"
        assert request["KeyConditionExpression"] == "#gpk = :groups"
        if self.query_error:
            raise self.query_error
        if self.pages is not None:
            page = copy.deepcopy(self.pages.pop(0))
        else:
            def order(row):
                return row["gsi1sk"], row["pk"], row["sk"]
            rows = sorted(
                (row for row in self.tables[PLATFORM].values()
                 if row.get("gsi1pk") == "GROUPS" and row.get("gsi1sk")),
                key=order,
            )
            if request.get("ExclusiveStartKey"):
                cursor = decoded(request["ExclusiveStartKey"])
                assert set(cursor) == set(INDEX_KEYS)
                rows = [row for row in rows if order(row) > order(cursor)]
            selected = rows[:request["Limit"]]
            page = {
                "Items": [typed({field: row[field] for field in INDEX_KEYS}) for row in selected],
            }
            if len(rows) > len(selected):
                page["LastEvaluatedKey"] = typed({field: selected[-1][field] for field in INDEX_KEYS})
        if self.after_query:
            callback, self.after_query = self.after_query, None
            callback()
        return page

    def get_item(self, **request):
        result = super().get_item(**request)
        if self.after_get:
            self.after_get(request)
        return result

    def update_item(self, **request):
        if self.before_update:
            self.before_update(request)
        return super().update_item(**request)

    def transact_write_items(self, **request):
        result = super().transact_write_items(**request)
        if self.commit_then_fail:
            self.commit_then_fail = False
            raise TimeoutError("Ambiguous committed transaction")
        return result


class ReconcileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.access = load("lambdas/shared/python/group_access.py", "reconcile_test_access")
        cls.engine = load("lambdas/group_worker/engine.py", "reconcile_test_engine")
        with mock.patch.dict(sys.modules, {"group_access": cls.access, "engine": cls.engine}):
            cls.reconcile = load("lambdas/group_worker/reconcile.py", "reconcile_under_test")
            cls.worker = load("lambdas/group_worker/handler.py", "reconcile_contract_worker")

    def setUp(self):
        for patcher in (
            mock.patch.object(socket, "create_connection", side_effect=AssertionError("No network")),
            mock.patch.object(boto3, "client", side_effect=AssertionError("No live clients")),
            mock.patch.object(boto3, "resource", side_effect=AssertionError("No live resources")),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.ddb, self.invoke = IndexDynamo(), MemoryLambda()
        self.seed_group(GID)

    def seed_group(self, gid, **changes):
        self.ddb.seed(PLATFORM, {
            "pk": "GROUP#" + gid, "sk": "META", "group_id": gid, "owner_sub": SUB,
            "gsi1pk": "GROUPS", "gsi1sk": gid,
            "name": "Group", "sources": [{"source_id": "source-a", "role": "backend", "description": "Order API"}],
            "description": "Group context", "llm_enabled": False, "model": "",
            "revision": 2, "status": "READY", "active_version": "previous-build",
            "active_revision": 1, "active_source_versions": {"source-a": "v1"},
            "active_source_descriptions": {"source-a": 1},
            "active_manifest_key": f"groups/{gid}/versions/previous-build/manifest.json",
            "active_graph_key": f"groups/{gid}/versions/previous-build/graph.json",
            **changes,
        })
        self.ddb.seed(PLATFORM, {"pk": "USER#" + SUB, "sk": "GROUP#" + gid, "role": "owner"})
        self.ddb.seed(PLATFORM, {"pk": "USER#" + SUB, "sk": "REPO#source-a", "role": "reader"})
        self.ddb.seed(REGISTRY, {
            "repo_id": "source-a", "enabled": "1", "graph_scope": "private",
            "active_source_version": "v2", "description_version": 2,
            "source_epoch": 1, "acl_epoch": 1,
        })

    def meta(self, gid=GID):
        return self.ddb.row(PLATFORM, pk="GROUP#" + gid, sk="META")

    def change_meta(self, gid=GID, **fields):
        self.ddb.seed(PLATFORM, {**self.meta(gid), **fields})

    def source(self, **changes):
        row = self.ddb.row(REGISTRY, repo_id="source-a")
        self.ddb.seed(REGISTRY, {**row, **changes})

    def cursor(self):
        return self.ddb.row(PLATFORM, **STATE) or {}

    def ledger(self, bid=None, gid=GID):
        return self.ddb.row(PLATFORM, pk="GROUP#" + gid, sk="BUILD#" + (bid or self.meta(gid)["build_id"]))

    def run_tick(self, *, now=NOW, remaining=lambda: 60000, worker=WORKER):
        result = self.reconcile.run(
            self.ddb, self.invoke, PLATFORM, REGISTRY, worker, now=now, remaining=remaining,
        )
        json.dumps(result)
        return result

    def active_fields(self):
        return {key: value for key, value in self.meta().items() if key.startswith("active_")}

    def seed_build(self, *, active=True, started=NOW - 961, worker_expiry=None, status="QUEUED"):
        meta = self.meta()
        self.change_meta(status="BUILDING", build_id="manual-build", build_started_at=started,
                         active_version=meta["active_version"] if active else "")
        if worker_expiry is not None:
            self.change_meta(worker_token="worker-token", worker_lease_expires_at=worker_expiry)
        self.ddb.seed(PLATFORM, {
            "pk": "GROUP#" + GID, "sk": "BUILD#manual-build", "group_id": GID,
            "build_id": "manual-build", "revision": meta["revision"], "requested_by": SUB,
            "created_at": started, "status": status, "usage": {"input_tokens": 20}, "stats": {},
        })

    def test_claim_and_payload_match_the_actual_worker_contract_with_decimals(self):
        prior = self.active_fields()
        self.change_meta(revision=Decimal(2), active_revision=Decimal(1))
        result = self.run_tick(now=Decimal(NOW))
        self.assertEqual(result["scheduled"], 1)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(len(self.invoke.calls), 1)
        call = self.invoke.calls[0]
        self.assertEqual((call["FunctionName"], call["InvocationType"]), (WORKER, "Event"))
        event = json.loads(call["Payload"])
        self.assertIs(type(event["revision"]), int)
        gid, bid, revision, sub = self.worker._event(event)
        ledger = self.ledger()
        self.assertTrue(self.worker._ledger_matches(ledger, gid, bid, revision, sub))
        self.assertTrue(self.worker._same_fields(
            ledger, self.meta(), ("sources", "description", "llm_enabled", "model"),
        ))
        self.assertEqual(ledger["source_versions"], {"source-a": "v2"})
        self.assertEqual(ledger["source_description_versions"], {"source-a": 2})
        self.assertTrue(ledger["dispatched"])
        self.assertEqual(ledger["dispatched_at"], NOW)
        self.assertEqual(self.active_fields(), prior)
        self.assertEqual(self.meta()["status"], "BUILDING")

    def test_unchanged_inputs_never_retry_for_status_alone(self):
        for status in ("READY", "PARTIAL", "FAILED", "STALE"):
            with self.subTest(status=status):
                self.change_meta(status=status, active_revision=2,
                                 active_source_versions={"source-a": "v2"},
                                 active_source_descriptions={"source-a": 2})
                self.assertEqual(self.run_tick()["scheduled"], 0)
        self.assertFalse(self.invoke.calls)

    def test_version_description_and_revision_changes_independently_trigger_one_claim(self):
        original = copy.deepcopy(self.ddb.tables)
        for field, stale in (
            ("active_revision", 1), ("active_source_versions", {"source-a": "v1"}),
            ("active_source_descriptions", {"source-a": 1}),
        ):
            self.ddb.tables = copy.deepcopy(original)
            self.change_meta(**{
                "active_revision": 2, "active_source_versions": {"source-a": "v2"},
                "active_source_descriptions": {"source-a": 2}, field: stale,
            })
            self.assertEqual(self.run_tick()["scheduled"], 1)

    def test_draft_and_initial_failed_groups_remain_manual_opt_in(self):
        for status in ("DRAFT", "FAILED", "STALE"):
            self.change_meta(status=status, active_version="")
            self.assertEqual(self.run_tick()["scheduled"], 0)
        self.change_meta(status="DRAFT", active_version="old")
        self.assertEqual(self.run_tick()["scheduled"], 0)
        self.assertFalse(self.invoke.calls)

    def test_recent_reservations_and_worker_leases_are_both_honored(self):
        for started, expiry in (
            (NOW - 959, None), (NOW - 960, None), (NOW + 100, None),
            (NOW - 2000, NOW + 100), (NOW - 2000, NOW),
        ):
            self.seed_build(started=started, worker_expiry=expiry)
            result = self.run_tick()
            self.assertEqual((result["scheduled"], result["timed_out"]), (0, 0))
            self.assertEqual(self.meta()["status"], "BUILDING")
        self.assertFalse(self.invoke.calls)

    def test_expired_initial_and_prior_active_builds_close_meta_and_ledger_atomically(self):
        for active in (False, True):
            self.seed_build(active=active, worker_expiry=NOW - 1)
            prior = self.active_fields()
            result = self.run_tick()
            self.assertEqual(result["timed_out"], 1)
            self.assertEqual(result["scheduled"], 0)
            self.assertEqual(self.meta()["status"], "FAILED")
            self.assertEqual(self.meta()["last_error"], "BUILD_TIMEOUT")
            self.assertEqual(self.meta()["build_started_at"], 0)
            self.assertNotIn("worker_token", self.meta())
            self.assertNotIn("worker_lease_expires_at", self.meta())
            self.assertEqual(self.ledger()["status"], "FAILED")
            self.assertEqual(self.ledger()["ended_at"], NOW)
            self.assertEqual(self.ledger()["usage"], {"input_tokens": 20})
            self.assertEqual(self.active_fields(), prior)
        self.assertFalse(self.invoke.calls)

    def test_index_staleness_cannot_timeout_a_new_initial_worker(self):
        self.seed_build(active=False)
        self.ddb.after_query = lambda: self.change_meta(build_started_at=NOW, build_id="new-build")
        result = self.run_tick()
        self.assertEqual(result["timed_out"], 0)
        self.assertEqual(self.meta()["status"], "BUILDING")
        self.assertEqual(self.meta()["build_id"], "new-build")

    def test_missing_meta_clock_uses_ledger_age_without_premature_timeout(self):
        self.seed_build(active=False, started=NOW)
        self.change_meta(build_started_at=0)
        self.assertEqual(self.run_tick()["timed_out"], 0)
        self.assertEqual(self.meta()["status"], "BUILDING")
        self.assertEqual(self.run_tick(now=NOW + 961)["timed_out"], 1)

    def test_unknown_build_age_does_not_invent_timeout_or_dispatch(self):
        self.seed_build(active=False, started=0)
        self.assertEqual(self.run_tick()["timed_out"], 0)
        self.assertEqual(self.meta()["status"], "BUILDING")
        self.assertFalse(self.invoke.calls)

    def test_worker_lease_renewal_during_timeout_cancels_both_updates(self):
        self.seed_build(active=False)
        self.ddb.before_transaction = lambda _ddb: self.change_meta(
            worker_token="new-owner", worker_lease_expires_at=NOW + 960,
        )
        result = self.run_tick()
        self.assertEqual(result["failed"], 1)
        self.assertEqual(self.meta()["status"], "BUILDING")
        self.assertEqual(self.ledger()["status"], "QUEUED")
        self.assertFalse(self.invoke.calls)

    def test_revoked_access_does_not_leave_an_expired_build_forever_building(self):
        self.seed_build(active=False)
        self.ddb.remove(PLATFORM, pk="USER#" + SUB, sk="REPO#source-a")
        self.assertEqual(self.run_tick()["timed_out"], 1)
        self.assertEqual(self.meta()["status"], "FAILED")
        self.assertFalse(self.invoke.calls)

    def test_no_rebuild_for_revoked_deleted_disabled_or_missing_inputs(self):
        original = copy.deepcopy(self.ddb.tables)
        mutations = (
            lambda: self.ddb.remove(PLATFORM, pk="USER#" + SUB, sk="GROUP#" + GID),
            lambda: self.ddb.remove(PLATFORM, pk="USER#" + SUB, sk="REPO#source-a"),
            lambda: self.ddb.seed(PLATFORM, {"pk": "USER#" + SUB, "sk": "DELETED"}),
            lambda: self.ddb.seed(PLATFORM, {"pk": "USER#" + SUB, "sk": "REPO#source-a", "status": "REVOKED"}),
            lambda: self.ddb.seed(PLATFORM, {"pk": "USER#" + SUB, "sk": "GROUP#" + GID, "role": "owner", "status": "REVOKED"}),
            lambda: self.source(enabled="0"), lambda: self.source(deleted_at=NOW),
            lambda: self.source(active_source_version=""), lambda: self.source(active_source_version="../escape"),
            lambda: self.change_meta(status="DELETED"), lambda: self.change_meta(deleted_at=NOW),
            lambda: self.change_meta(sources=[]),
        )
        for mutate in mutations:
            self.ddb.tables = copy.deepcopy(original)
            mutate()
            self.assertEqual(self.run_tick()["scheduled"], 0)
        self.assertFalse(self.invoke.calls)

    def test_source_acl_metadata_and_revision_races_cancel_claim_and_ledger_together(self):
        original = copy.deepcopy(self.ddb.tables)
        mutations = (
            lambda: self.ddb.remove(PLATFORM, pk="USER#" + SUB, sk="GROUP#" + GID),
            lambda: self.ddb.remove(PLATFORM, pk="USER#" + SUB, sk="REPO#source-a"),
            lambda: self.ddb.seed(PLATFORM, {"pk": "USER#" + SUB, "sk": "DELETED"}),
            lambda: self.source(enabled="0"), lambda: self.source(active_source_version="v3"),
            lambda: self.source(description_version=3), lambda: self.source(acl_epoch=2),
            lambda: self.change_meta(status="DELETED"), lambda: self.change_meta(revision=3),
            lambda: self.change_meta(description="Changed without revision"),
            lambda: self.change_meta(worker_token="late", worker_lease_expires_at=NOW + 960),
        )
        for mutate in mutations:
            self.ddb.tables = copy.deepcopy(original)
            self.ddb.before_transaction = lambda _ddb: mutate()
            result = self.run_tick()
            self.assertEqual(result["scheduled"], 0)
            self.assertNotIn("last_auto_signature", self.meta())
            self.assertFalse(any(key[1].startswith("BUILD#") for key in self.ddb.tables[PLATFORM]))
        self.assertFalse(self.invoke.calls)

    def test_public_source_can_schedule_without_private_grant_but_scope_race_cancels(self):
        self.source(graph_scope="public")
        self.ddb.remove(PLATFORM, pk="USER#" + SUB, sk="REPO#source-a")
        self.ddb.before_transaction = lambda _ddb: self.source(graph_scope="private")
        self.assertEqual(self.run_tick()["scheduled"], 0)
        self.source(graph_scope="public")
        self.assertEqual(self.run_tick()["scheduled"], 1)

    def test_claim_cannot_recreate_a_concurrently_deleted_meta(self):
        self.ddb.before_transaction = lambda _ddb: self.ddb.remove(
            PLATFORM, pk="GROUP#" + GID, sk="META",
        )
        self.assertEqual(self.run_tick()["scheduled"], 0)
        self.assertIsNone(self.meta())
        self.assertFalse(self.invoke.calls)
        self.assertFalse(any(key[1].startswith("BUILD#") for key in self.ddb.tables[PLATFORM]))

    def test_one_attempt_per_signature_survives_input_changes_and_rollback(self):
        self.assertEqual(self.run_tick()["scheduled"], 1)
        first_bid = self.meta()["build_id"]
        self.change_meta(status="FAILED", build_started_at=0)
        self.assertEqual(self.run_tick(now=NOW + 1000)["scheduled"], 0)
        self.source(active_source_version="v3")
        self.assertEqual(self.run_tick(now=NOW + 1001)["scheduled"], 1)
        self.change_meta(status="FAILED", build_started_at=0)
        self.source(active_source_version="v2")
        before = sum(name == "transact_write_items" for name, _call in self.ddb.calls)
        self.assertEqual(self.run_tick(now=NOW + 2000)["scheduled"], 0)
        self.assertEqual(sum(name == "transact_write_items" for name, _call in self.ddb.calls), before)
        self.assertEqual(len(self.invoke.calls), 2)
        self.assertIsNotNone(self.ledger(first_bid))

    def test_ambiguous_invoke_stays_queued_until_timeout_and_is_never_repeated(self):
        self.invoke.failure = TimeoutError("Do not expose private request details")
        result = self.run_tick()
        self.assertEqual((result["scheduled"], result["failed"]), (0, 1))
        self.assertEqual(self.meta()["status"], "BUILDING")
        self.assertEqual(self.ledger()["status"], "QUEUED")
        self.assertTrue(self.ledger()["dispatch_uncertain"])
        self.assertEqual(self.ledger()["last_error"], "DISPATCH_UNCONFIRMED")
        self.assertEqual(self.run_tick(now=NOW + 300)["scheduled"], 0)
        self.assertEqual(self.run_tick(now=NOW + 961)["timed_out"], 1)
        self.assertEqual(self.run_tick(now=NOW + 1200)["scheduled"], 0)
        self.assertEqual(self.meta()["status"], "FAILED")
        self.assertEqual(len(self.invoke.calls), 1)

    def test_non_202_invoke_is_not_counted_as_scheduled(self):
        self.invoke.status_code = 500
        result = self.run_tick()
        self.assertEqual((result["scheduled"], result["failed"]), (0, 1))
        self.assertTrue(self.ledger()["dispatch_uncertain"])
        self.assertEqual(self.ledger()["status"], "QUEUED")

    def test_ambiguous_committed_claim_never_redispatches(self):
        self.ddb.commit_then_fail = True
        result = self.run_tick()
        self.assertEqual(result["failed"], 1)
        self.assertEqual(self.meta()["status"], "BUILDING")
        self.assertIsNotNone(self.ledger())
        self.assertEqual(self.run_tick(now=NOW + 961)["timed_out"], 1)
        self.assertEqual(self.run_tick(now=NOW + 1200)["scheduled"], 0)
        self.assertFalse(self.invoke.calls)

    def test_dispatch_marker_failure_does_not_reinvoke_or_abort_accepted_worker(self):
        def fail_marker(request):
            key = decoded(request["Key"])
            if key["sk"].startswith("BUILD#"):
                raise TimeoutError("Marker unavailable")
        self.ddb.before_update = fail_marker
        self.assertEqual(self.run_tick()["scheduled"], 1)
        self.assertEqual(self.meta()["status"], "BUILDING")
        self.assertEqual(self.run_tick(now=NOW + 300)["scheduled"], 0)
        self.assertEqual(len(self.invoke.calls), 1)

    def test_usage_rows_do_not_delay_groups_or_consume_the_query_page(self):
        for number in range(2000):
            self.ddb.seed(PLATFORM, {
                "pk": f"USAGE#{number:04}", "sk": "ROW", "gsi1pk": "USAGE", "gsi1sk": str(number),
            })
        result = self.run_tick()
        self.assertEqual(result["processed"], 1)
        self.assertEqual(result["scheduled"], 1)
        self.assertEqual(len(self.ddb.queries), 1)
        self.assertEqual(self.cursor()["cursor"], {})

    def test_exactly_one_page_of_100_group_keys_is_read_per_tick(self):
        gids = ["grp_" + f"{number:032x}" for number in range(100)]
        for gid in gids:
            self.seed_group(gid, status="DRAFT", active_version="")
        result = self.run_tick()
        self.assertEqual((result["processed"], result["scheduled"]), (100, 0))
        self.assertEqual(len(self.ddb.queries), 1)
        self.assertEqual(self.cursor()["cursor"], index_key(gids[-1]))
        self.assertEqual(self.run_tick(now=NOW + 300)["scheduled"], 1)
        self.assertEqual(len(self.ddb.queries), 2)
        self.assertEqual(decoded(self.ddb.queries[-1]["ExclusiveStartKey"]), index_key(gids[-1]))

    def test_legacy_two_key_scan_cursor_is_reset_before_index_query(self):
        self.ddb.seed(PLATFORM, {**STATE, "cursor": {"pk": "USAGE#zzzz", "sk": "ROW"}})
        self.assertEqual(self.run_tick()["scheduled"], 1)
        self.assertNotIn("ExclusiveStartKey", self.ddb.queries[0])
        self.assertEqual(self.cursor()["cursor"], {})

    def test_eventually_missing_index_entry_never_claims_until_discovered(self):
        self.ddb.pages = [{"Items": []}]
        before = self.meta()
        result = self.run_tick()
        self.assertEqual(result["scheduled"], 0)
        self.assertEqual(result["processed"], 0)
        self.assertEqual(self.meta(), before)
        self.assertFalse(self.invoke.calls)
        self.ddb.pages = None
        self.assertEqual(self.run_tick(now=NOW + 300)["scheduled"], 1)

    def test_stale_index_entries_cannot_recreate_deleted_groups_or_bypass_current_acl(self):
        original = copy.deepcopy(self.ddb.tables)
        mutations = (
            lambda: self.ddb.remove(PLATFORM, pk="GROUP#" + GID, sk="META"),
            lambda: self.change_meta(status="DELETED"),
            lambda: self.ddb.remove(PLATFORM, pk="USER#" + SUB, sk="GROUP#" + GID),
            lambda: self.ddb.remove(PLATFORM, pk="USER#" + SUB, sk="REPO#source-a"),
            lambda: self.source(enabled="0"),
        )
        for mutate in mutations:
            self.ddb.tables = copy.deepcopy(original)
            self.ddb.pages = [{"Items": [typed(index_key(GID))]}]
            self.ddb.after_query = mutate
            result = self.run_tick()
            self.assertEqual((result["scheduled"], result["processed"]), (0, 1))
            self.assertFalse(any(key[1].startswith("BUILD#") for key in self.ddb.tables[PLATFORM]))
        self.assertFalse(self.invoke.calls)
        self.assertTrue(all(request.get("ConsistentRead") is True
                            for name, request in self.ddb.calls if name == "get_item"))

    def test_stale_index_metadata_is_not_used_to_activate_an_already_current_group(self):
        self.ddb.after_query = lambda: self.change_meta(
            active_revision=2, active_source_versions={"source-a": "v2"},
            active_source_descriptions={"source-a": 2},
        )
        result = self.run_tick()
        self.assertEqual((result["scheduled"], result["processed"]), (0, 1))
        self.assertEqual(self.meta()["status"], "READY")
        self.assertNotIn("build_id", self.meta())
        self.assertFalse(self.invoke.calls)

    def test_partial_pages_checkpoint_last_processed_group_and_do_not_starve_later_groups(self):
        second = "grp_" + "b" * 32
        third = "grp_" + "c" * 32
        self.seed_group(second)
        self.seed_group(third)
        result = self.run_tick(remaining=lambda: 14000 if self.invoke.calls else 60000)
        self.assertEqual(result["scheduled"], 1)
        self.assertEqual(self.cursor()["cursor"], index_key(GID))
        result = self.run_tick(now=NOW + 300)
        self.assertEqual(result["scheduled"], 2)
        self.assertEqual({json.loads(call["Payload"])["group_id"] for call in self.invoke.calls}, {GID, second, third})

    def test_budget_exhaustion_before_claim_preserves_unprocessed_group(self):
        meta_reads = 0
        def after_get(request):
            nonlocal meta_reads
            if decoded(request["Key"]).get("sk") == "META":
                meta_reads += 1
        self.ddb.after_get = after_get
        result = self.run_tick(remaining=lambda: 14000 if meta_reads >= 2 else 60000)
        self.assertEqual(result["scheduled"], 0)
        self.assertEqual(result["processed"], 0)
        self.assertEqual(self.cursor()["cursor"], {})
        self.assertEqual(self.run_tick(now=NOW + 300)["scheduled"], 1)

    def test_bad_group_does_not_poison_cursor_or_prevent_later_valid_groups(self):
        second = "grp_" + "b" * 32
        self.seed_group(second)
        self.change_meta(build_started_at="bad timestamp")
        result = self.run_tick()
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["scheduled"], 1)
        self.assertEqual(json.loads(self.invoke.calls[0]["Payload"])["group_id"], second)

    def test_active_run_lease_rejects_overlapping_delivery(self):
        self.ddb.seed(PLATFORM, {**STATE, "run_token": "other", "lease_expires_at": NOW + 960, "cursor": {}})
        self.assertTrue(self.run_tick()["busy"])
        self.assertFalse(self.ddb.queries)
        self.assertFalse(self.invoke.calls)
        self.assertEqual(self.run_tick(now=NOW + 961)["scheduled"], 1)

    def test_late_run_cannot_overwrite_new_cursor_owner(self):
        replacement = index_key("grp_" + "f" * 32)
        def steal():
            self.ddb.seed(PLATFORM, {**STATE, "run_token": "new-owner",
                                    "lease_expires_at": NOW + 2000, "cursor": replacement})
        self.ddb.after_query = steal
        result = self.run_tick()
        self.assertEqual(result["failed"], 1)
        self.assertEqual(self.cursor()["run_token"], "new-owner")
        self.assertEqual(self.cursor()["cursor"], replacement)

    def test_corrupt_cursor_recovers_and_query_failure_retains_previous_position(self):
        self.ddb.seed(PLATFORM, {**STATE, "cursor": {"wrong": "value"}, "lease_expires_at": "bad"})
        self.assertEqual(self.run_tick()["scheduled"], 1)
        self.assertNotIn("ExclusiveStartKey", self.ddb.queries[0])
        previous = index_key("grp_" + "0" * 32)
        self.ddb.seed(PLATFORM, {**STATE, "cursor": previous})
        self.ddb.query_error = TimeoutError("Temporary scan failure")
        self.assertEqual(self.run_tick(now=NOW + 300)["failed"], 1)
        self.assertEqual(self.cursor()["cursor"], previous)
        self.assertNotIn("run_token", self.cursor())

    def test_cursor_flush_failure_recovers_after_run_lease_without_duplicate_dispatch(self):
        def fail_flush(request):
            if request.get("ConditionExpression") == "run_token = :token":
                raise TimeoutError("Cursor write unavailable")
        self.ddb.before_update = fail_flush
        result = self.run_tick()
        self.assertEqual((result["scheduled"], result["failed"]), (1, 1))
        self.ddb.before_update = None
        self.assertTrue(self.run_tick(now=NOW + 300)["busy"])
        self.assertEqual(self.run_tick(now=NOW + 961)["timed_out"], 1)
        self.assertEqual(len(self.invoke.calls), 1)

    def test_no_work_without_worker_or_minimum_time_budget(self):
        self.assertEqual(self.run_tick(worker="")["processed"], 0)
        self.assertEqual(self.run_tick(remaining=lambda: 100)["processed"], 0)
        self.assertFalse(self.ddb.queries)
        self.assertFalse(self.invoke.calls)

    def entry_fixture(self, remaining=60000):
        context = mock.Mock()
        context.get_remaining_time_in_millis.return_value = remaining
        garbage = types.ModuleType("garbage")
        garbage.sweep = mock.Mock(return_value={"obsolete_objects_deleted": 7})
        clients = {
            "dynamodb": mock.Mock(name="ddb"), "lambda": mock.Mock(name="invoke"),
            "s3": mock.Mock(name="s3"),
        }
        summary = {"scheduled": 2, "skipped": 1, "failed": 0}
        env = {
            "PLATFORM_TABLE": PLATFORM, "REGISTRY_TABLE": REGISTRY,
            "AWS_LAMBDA_FUNCTION_NAME": WORKER, "GRAPH_BUCKET": "offline-artifacts",
        }
        return context, garbage, clients, summary, env

    def test_entry_sweeps_once_after_reconciliation_with_the_same_ddb_client(self):
        context, garbage, clients, summary, env = self.entry_fixture()
        order = []
        garbage.sweep.side_effect = lambda *args: order.append("gc") or {"obsolete_objects_deleted": 7}
        with (
            mock.patch.dict(os.environ, env),
            mock.patch.dict(sys.modules, {"garbage": garbage}),
            mock.patch.object(boto3, "client", side_effect=lambda name, **kw: clients[name]) as client,
            mock.patch.object(self.reconcile, "run", side_effect=lambda *args, **kw: order.append("run") or summary) as run,
        ):
            result = self.reconcile.reconcile({"reconcile": True}, context)
        self.assertEqual(order, ["run", "gc"])
        run.assert_called_once_with(
            clients["dynamodb"], clients["lambda"], PLATFORM, REGISTRY, WORKER,
            remaining=context.get_remaining_time_in_millis,
        )
        garbage.sweep.assert_called_once_with(
            clients["dynamodb"], clients["s3"], PLATFORM, REGISTRY, "offline-artifacts",
        )
        self.assertEqual(result["gc"], {"obsolete_objects_deleted": 7})
        self.assertEqual(result["scheduled"], 2)
        self.assertEqual([call.args[0] for call in client.call_args_list], ["dynamodb", "lambda", "s3"])
        for call in client.call_args_list:
            self.assertEqual(call.kwargs["config"].retries["total_max_attempts"], 1)

    def test_entry_skips_gc_at_or_below_thirty_seconds(self):
        for remaining in (30000, 29999, 0):
            context, garbage, clients, summary, env = self.entry_fixture(remaining)
            with (
                mock.patch.dict(os.environ, env),
                mock.patch.dict(sys.modules, {"garbage": garbage}),
                mock.patch.object(boto3, "client", side_effect=lambda name, **kw: clients[name]) as client,
                mock.patch.object(self.reconcile, "run", return_value=summary),
            ):
                result = self.reconcile.reconcile({"reconcile": True}, context)
            self.assertEqual(result, {"scheduled": 2, "skipped": 1, "failed": 0})
            garbage.sweep.assert_not_called()
            self.assertEqual([call.args[0] for call in client.call_args_list], ["dynamodb", "lambda"])

    def test_entry_uses_remaining_time_after_run_and_sweeps_above_the_boundary(self):
        for final_remaining in (30001, 30000):
            context, garbage, clients, summary, env = self.entry_fixture(900000)
            def finish_run(*args, **kwargs):
                context.get_remaining_time_in_millis.return_value = final_remaining
                return summary
            with (
                mock.patch.dict(os.environ, env),
                mock.patch.dict(sys.modules, {"garbage": garbage}),
                mock.patch.object(boto3, "client", side_effect=lambda name, **kw: clients[name]),
                mock.patch.object(self.reconcile, "run", side_effect=finish_run),
            ):
                result = self.reconcile.reconcile({"reconcile": True}, context)
            self.assertEqual(garbage.sweep.call_count, int(final_remaining > 30000))
            self.assertEqual("gc" in result, final_remaining > 30000)

    def test_entry_gc_failure_returns_only_exception_type_without_retrying(self):
        context, garbage, clients, summary, env = self.entry_fixture()
        garbage.sweep.side_effect = RuntimeError("s3://private-bucket/sensitive-key credential=secret")
        with (
            mock.patch.dict(os.environ, env),
            mock.patch.dict(sys.modules, {"garbage": garbage}),
            mock.patch.object(boto3, "client", side_effect=lambda name, **kw: clients[name]),
            mock.patch.object(self.reconcile, "run", return_value=summary),
        ):
            result = self.reconcile.reconcile({"reconcile": True}, context)
        self.assertEqual(result, {"scheduled": 2, "skipped": 1, "failed": 0, "gc_error": "RuntimeError"})
        garbage.sweep.assert_called_once()

    def test_entry_contains_s3_client_construction_failure(self):
        context, garbage, clients, summary, env = self.entry_fixture()
        def client(name, **kwargs):
            if name == "s3":
                raise OSError("private configuration details")
            return clients[name]
        with (
            mock.patch.dict(os.environ, env),
            mock.patch.dict(sys.modules, {"garbage": garbage}),
            mock.patch.object(boto3, "client", side_effect=client),
            mock.patch.object(self.reconcile, "run", return_value=summary),
        ):
            result = self.reconcile.reconcile({"reconcile": True}, context)
        self.assertEqual(result, {"scheduled": 2, "skipped": 1, "failed": 0, "gc_error": "OSError"})
        garbage.sweep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
