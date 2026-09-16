"""Permission/version checks with real DynamoDB serialization and no AWS."""

import copy
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock
from boto3.dynamodb.types import TypeSerializer

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lambdas/shared/python"))
import group_access as access


class GroupAccessTests(unittest.TestCase):
    def setUp(self):
        self.gid = "grp_" + "a" * 32
        self.meta = {"group_id": self.gid, "owner_sub": "owner", "revision": 1,
                     "active_revision": 1, "active_version": "version1", "status": "READY",
                     "sources": [{"source_id": "sourceA"}, {"source_id": "sourceB"}],
                     "active_source_versions": {"sourceA": "a1", "sourceB": "b1"},
                     "active_source_descriptions": {"sourceA": 0, "sourceB": 0}}
        self.rows = {
            ("platform", "GROUP#" + self.gid, "META"): self.meta,
            ("platform", "USER#viewer", "GROUP#" + self.gid): {"role": "viewer"},
            ("platform", "USER#viewer", "REPO#sourceA"): {"role": "member"},
            ("registry", "sourceA"): {"repo_id": "sourceA", "enabled": "1", "graph_scope": "private",
                                     "active_source_version": "a1"},
            ("registry", "sourceB"): {"repo_id": "sourceB", "enabled": "1", "graph_scope": "public",
                                     "active_source_version": "b1"},
        }
        self.ddb = Mock()
        def get(TableName, Key, ConsistentRead):
            self.assertTrue(ConsistentRead)
            key = (TableName, *(v["S"] for v in Key.values()))
            row = self.rows.get(key)
            return {"Item": {k: TypeSerializer().serialize(v) for k, v in row.items()}} if row else {}
        self.ddb.get_item.side_effect = get

    def read(self, **kwargs):
        return access.require_access(self.ddb, "platform", "registry", "viewer", self.gid, **kwargs)

    def denied(self, status, **kwargs):
        with self.assertRaises(access.GroupError) as err:
            self.read(**kwargs)
        self.assertEqual(status, err.exception.status)

    def test_group_and_all_source_grants_allow_exact_current_version(self):
        self.assertEqual(self.read(check_versions=True)["active_version"], "version1")

    def test_private_source_revocation_blocks_all_derived_data(self):
        del self.rows["platform", "USER#viewer", "REPO#sourceA"]
        self.denied(403, check_versions=True)

    def test_group_invite_does_not_grant_source_access(self):
        del self.rows["platform", "USER#viewer", "REPO#sourceA"]
        self.denied(403)

    def test_disabled_public_source_is_denied(self):
        self.rows["registry", "sourceB"]["enabled"] = "0"
        self.denied(403)

    def test_missing_scope_is_private(self):
        del self.rows["registry", "sourceB"]["graph_scope"]
        self.denied(403)

    def test_deleted_user_denied(self):
        self.rows["platform", "USER#viewer", "DELETED"] = {"deleted": True}
        self.denied(403)

    def test_deleted_group_denied(self):
        self.meta["status"] = "DELETED"
        self.denied(404)

    def test_viewer_cannot_modify(self):
        self.denied(403, write=True)

    def test_source_update_invalidates_graph_but_allows_metadata(self):
        self.rows["registry", "sourceA"]["active_source_version"] = "a2"
        self.assertEqual(self.read()["revision"], 1)
        self.denied(409, check_versions=True)

    def test_common_description_update_invalidates_relations(self):
        self.rows["registry", "sourceA"]["description_version"] = 1
        self.denied(409, check_versions=True)

    def test_group_revision_change_invalidates_graph(self):
        self.meta["revision"] = 2
        self.denied(409, check_versions=True)

    def test_duplicate_sources_denied(self):
        self.meta["sources"] = [{"source_id": "sourceA"}] * 2
        self.denied(409)

    def test_service_errors_fail_closed(self):
        self.ddb.get_item.side_effect = RuntimeError("unavailable")
        with self.assertRaises(RuntimeError):
            self.read()

    def test_read_does_not_mutate_stored_metadata(self):
        before = copy.deepcopy(self.meta)
        self.read(check_versions=True)
        self.assertEqual(self.meta, before)
