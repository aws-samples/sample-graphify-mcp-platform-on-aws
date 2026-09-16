import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("publisher", ROOT / "cdk/build_scripts/publish_source_version.py")
publisher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publisher)


class SourceVersionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.graph, self.snapshot = root / "graph.json", root / "src.tar.gz"
        self.graph.write_text('{"nodes": [], "links": []}')
        self.snapshot.write_bytes(b"synthetic snapshot")
        self.registry, self.s3 = Mock(), Mock()
        self.registry.get_item.return_value = {"Item": {"enabled": "1", "build_arn": "arn:test"}}
        self.registry.meta.client.exceptions.ConditionalCheckFailedException = type("Conditional", (Exception,), {})
        self.objects = {}
        def put(**kwargs):
            data = kwargs["Body"]
            self.objects[kwargs["Key"]] = data.read() if hasattr(data, "read") else data
        self.s3.put_object.side_effect = put

    def publish(self):
        return publisher.publish(self.s3, self.registry, "bucket", "source", "build", "arn:test",
                                 self.graph, self.snapshot)

    def test_manifest_follows_pair_and_registry_activation(self):
        result = self.publish()
        self.assertEqual("published", result["state"])
        keys = list(self.objects)
        self.assertTrue(keys[-1].endswith("/manifest.json"))
        manifest = json.loads(self.objects[keys[-1]])
        self.assertEqual(manifest["graph_sha256"], hashlib.sha256(self.graph.read_bytes()).hexdigest())
        self.assertEqual(manifest["snapshot_sha256"], hashlib.sha256(self.snapshot.read_bytes()).hexdigest())
        self.assertIn("build_arn = :arn", self.registry.update_item.call_args.kwargs["ConditionExpression"])

    def test_stale_claim_uploads_nothing(self):
        self.registry.get_item.return_value["Item"]["build_arn"] = "newer"
        self.assertEqual("stale", self.publish()["state"])
        self.s3.put_object.assert_not_called()
        self.registry.update_item.assert_not_called()

    def test_disabled_source_uploads_nothing(self):
        self.registry.get_item.return_value["Item"]["enabled"] = "0"
        self.assertEqual("stale", self.publish()["state"])
        self.s3.put_object.assert_not_called()

    def test_missing_snapshot_invalidates_previous_group_version(self):
        self.snapshot.unlink()
        self.assertEqual("unavailable", self.publish()["state"])
        self.assertIn("REMOVE active_source_version",
                      self.registry.update_item.call_args.kwargs["UpdateExpression"])
        self.s3.put_object.assert_not_called()

    def test_failed_upload_never_activates_version(self):
        self.s3.put_object.side_effect = RuntimeError("offline write failed")
        with self.assertRaises(RuntimeError):
            self.publish()
        self.registry.update_item.assert_not_called()

    def test_new_claim_wins_during_upload(self):
        self.registry.update_item.side_effect = self.registry.meta.client.exceptions.ConditionalCheckFailedException()
        self.assertEqual("stale", self.publish()["state"])
