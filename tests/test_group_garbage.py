import hashlib
import importlib.util
from datetime import datetime, timezone
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lambdas/shared/python"))
spec = importlib.util.spec_from_file_location("group_garbage", ROOT / "lambdas/group_worker/garbage.py")
gc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gc)


class GroupGarbageTests(unittest.TestCase):
    def test_only_old_unpinned_artifacts_are_deleted(self):
        now = 200000
        old, recent = datetime.fromtimestamp(now - 4000, timezone.utc), datetime.fromtimestamp(now, timezone.utc)
        ddb, s3 = Mock(), Mock()
        source_build = hashlib.sha256(b"build-arn").hexdigest()[:32]
        keys = {
            "groups/": [
                ("groups/g/versions/active/graph.json", old),
                ("groups/g/versions/inflight/files/s/file.txt", old),
                ("groups/g/versions/obsolete/graph.json", old),
                ("groups/g/versions/new/graph.json", recent)],
            "source-versions/": [
                ("source-versions/s/current/manifest.json", old),
                (f"source-versions/s/{source_build}/graph.json", old),
                ("source-versions/s/obsolete/graph.json", old)],
        }
        s3.list_objects_v2.side_effect = lambda **kw: {"Contents": [
            {"Key": key, "LastModified": date} for key, date in keys[kw["Prefix"]]]}
        s3.delete_objects.return_value = {}
        def get(_ddb, table, key):
            if key.get("pk") == "GROUP#g":
                return {"active_version": "active", "build_id": "inflight", "status": "READY"}
            if key.get("repo_id") == "s":
                return {"enabled": "1", "active_source_version": "current", "build_arn": "build-arn"}
            return {}
        with patch.object(gc, "get_item", side_effect=get):
            result = gc.sweep(ddb, s3, "p", "r", "bucket", now=now)
        self.assertEqual(result["obsolete_objects_deleted"], 2)
        removed = [o["Key"] for call in s3.delete_objects.call_args_list for o in call.kwargs["Delete"]["Objects"]]
        self.assertEqual(removed, ["groups/g/versions/obsolete/graph.json",
                                   "source-versions/s/obsolete/graph.json"])

    def test_delete_errors_keep_scan_cursor_retryable(self):
        ddb, s3 = Mock(), Mock()
        s3.list_objects_v2.return_value = {"Contents": [{
            "Key": "groups/g/versions/old/graph.json",
            "LastModified": datetime.fromtimestamp(0, timezone.utc)}]}
        s3.delete_objects.return_value = {"Errors": [{"Code": "AccessDenied"}]}
        with patch.object(gc, "get_item", return_value={}):
            gc.sweep(ddb, s3, "p", "r", "bucket", now=10000)
        # The groups cursor did not advance; the second namespace has no
        # valid source-version-shaped keys in this intentionally shared page.
        keys = [call.kwargs["Item"]["sk"]["S"] for call in ddb.put_item.call_args_list]
        self.assertNotIn("groups/", keys)
