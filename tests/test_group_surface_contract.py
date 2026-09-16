"""Offline regression checks at the source-group integration boundaries.

Run: .venv/bin/python -B -m unittest discover -s tests -p test_group_surface_contract.py

Execute the real platform functions without their SDK-constructing module
initialization, the real BUILD_SPEC skip block with shell builtins only, and
the actual Node authorization function in a VM exposing only a mocked proxy.
No AWS clients, model calls, network access, or workspace artifacts are needed.
"""

import ast
import copy
import importlib.util
import json
import re
import shutil
import socket
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import boto3


ROOT = Path(__file__).resolve().parents[1]
PLATFORM_PATH = ROOT / "lambdas/platform_api/handler.py"
STREAM_PATH = ROOT / "lambdas/playground_stream/index.mjs"
GID = "grp_" + "a" * 32
NOW = 1_800_000_000


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def platform_functions(*names, **namespace):
    """Compile the unchanged function ASTs; exclude module-level SDK setup."""
    tree = ast.parse(PLATFORM_PATH.read_text(), filename=str(PLATFORM_PATH))
    nodes = [
        node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in names
    ]
    found = {node.name for node in nodes}
    if found != set(names):
        raise AssertionError(f"Missing platform functions: {set(names) - found}")
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(PLATFORM_PATH), "exec"), namespace)
    return SimpleNamespace(**namespace)


class OfflineCase(unittest.TestCase):
    def setUp(self):
        for target, attribute in (
            (boto3, "client"), (boto3, "resource"),
            (boto3.session.Session, "client"), (boto3.session.Session, "resource"),
            (socket.socket, "connect"), (socket, "create_connection"),
        ):
            patcher = mock.patch.object(
                target, attribute, side_effect=AssertionError("Network and AWS clients are forbidden"),
            )
            patcher.start()
            self.addCleanup(patcher.stop)


class ManualVersionSurfaceTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.bash = shutil.which("bash")
        if not self.bash:
            self.skipTest("bash is required for the real BUILD_SPEC skip-condition check")
        buildspec = load_module("surface_buildspec", ROOT / "cdk/buildspec.py")
        commands = [
            command for phase in buildspec.BUILD_SPEC["phases"].values()
            for command in phase.get("commands", [])
            if "crawl content unchanged" in command
        ]
        self.assertEqual(len(commands), 1, "The URL skip block must be unambiguous")
        lines = commands[0].splitlines()
        starts = [
            index for index, line in enumerate(lines)
            if line.lstrip().startswith("if ") and "$PREV" in line and "$CUR" in line
        ]
        self.assertEqual(len(starts), 1)
        start = starts[0]
        end = next(index for index in range(start + 1, len(lines)) if lines[index].strip() == "fi")
        self.skip_block = "\n".join(lines[start:end + 1])
        self.assertIn("touch /tmp/work/SKIP", self.skip_block)
        self.source = {
            "repo_id": "url__existing.example.test",
            "git_url": "https://existing.example.test/docs",
            "provider": "url", "source_type": "url", "enabled": "1",
            "status": "READY", "runtime_arn": "offline-existing-runtime",
            # This pre-feature source has no active_source_version.
        }
        self.registry = mock.Mock(spec_set=["get_item", "update_item"])
        self.registry.get_item.side_effect = lambda **kwargs: {"Item": copy.deepcopy(self.source)}
        self.codebuild = mock.Mock(spec_set=["start_build"])
        self.codebuild.start_build.return_value = {
            "build": {"id": "offline-build", "arn": "offline-build-arn"},
        }
        self.require_grant = mock.Mock()
        self.runtime = mock.Mock(spec_set=["ensure_repo_runtime"])
        self.runtime.ensure_repo_runtime.side_effect = AssertionError("No runtime operations")
        self.platform = platform_functions(
            "ApiError", "_start_build", "rebuild_repo",
            _registry=self.registry, _codebuild=self.codebuild,
            _require_grant=self.require_grant,
            PROJECT_NAME="offline-source-builder", GRAPH_BUCKET="offline-artifacts",
            time=SimpleNamespace(time=lambda: NOW),
            runtimes=self.runtime, RUNTIME_ENV={},
            _resp=lambda status, body: (status, body),
        )

    def skip_decision(self, *, force=None, previous="same-input", current="same-input"):
        # PATH contains no executables. The only external command in the real
        # block, touch, is replaced by a function that records its exact target.
        script = textwrap.dedent("""\
            set -eu
            contract_skipped=0
            touch() {
                if [ "$#" -ne 1 ] || [ "$1" != "/tmp/work/SKIP" ]; then
                    return 91
                fi
                contract_skipped=1
            }
        """) + self.skip_block + '\nprintf "contract_skip=%s\\n" "$contract_skipped"\n'
        environment = {"PATH": "/nonexistent-contract-tools", "PREV": previous, "CUR": current}
        if force is not None:
            environment["FORCE_SOURCE_VERSION"] = force
        result = subprocess.run(
            [self.bash, "--noprofile", "--norc", "-c", script],
            env=environment, text=True, capture_output=True, timeout=5, check=True,
        )
        match = re.search(r"^contract_skip=([01])$", result.stdout, re.MULTILINE)
        self.assertIsNotNone(match, result.stdout)
        return match[1] == "1"

    def submitted_environment(self):
        return {
            entry["name"]: entry["value"]
            for entry in self.codebuild.start_build.call_args.kwargs["environmentVariablesOverride"]
        }

    def test_manual_url_rebuild_repairs_unchanged_source_without_immutable_version(self):
        original = copy.deepcopy(self.source)
        status, result = self.platform.rebuild_repo(
            {}, {"sub": "source-owner"}, {"repoId": self.source["repo_id"]},
        )
        self.assertEqual(status, 202, result)
        self.require_grant.assert_called_once_with("source-owner", self.source["repo_id"])
        environment = self.submitted_environment()
        self.assertEqual(environment["FORCE_SOURCE_VERSION"], "1")
        self.assertEqual(environment["SOURCE_TYPE"], "url")
        self.assertTrue(environment["TARGET_SHA"].startswith("manual-"))
        self.assertFalse(self.skip_decision(force=environment["FORCE_SOURCE_VERSION"]))
        self.assertEqual(self.source, original, "The force flag must not persist on the source")
        self.assertNotIn("force_source_version", json.dumps(self.registry.update_item.call_args.kwargs))
        self.runtime.ensure_repo_runtime.assert_not_called()

    def test_ordinary_build_does_not_inherit_a_previous_manual_force_flag(self):
        self.platform.rebuild_repo({}, {"sub": "owner"}, {"repoId": self.source["repo_id"]})
        self.platform._start_build(self.source, "ordinary-poll-build")
        environment = self.submitted_environment()
        self.assertEqual(environment["FORCE_SOURCE_VERSION"], "0")
        self.assertTrue(self.skip_decision(force=environment["FORCE_SOURCE_VERSION"]))

    def test_real_url_skip_condition_preserves_polling_and_changed_content_behavior(self):
        for force, previous, current, expected in (
            (None, "same", "same", True),
            ("0", "same", "same", True),
            ("1", "same", "same", False),
            (None, "old", "new", False),
            ("0", "old", "new", False),
            ("1", "old", "new", False),
        ):
            with self.subTest(force=force, previous=previous, current=current):
                self.assertEqual(
                    self.skip_decision(force=force, previous=previous, current=current), expected,
                )


NODE_AUTH_HARNESS = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const source = fs.readFileSync(input.source, 'utf8');
const start = source.indexOf('async function authorizeServer(');
const end = source.indexOf('\nconst MCP_STATUS_HINTS', start);
if (start < 0 || end < 0) throw new Error('Cannot locate actual authorizeServer');
const matcher = source.match(/const SERVER_ID_RE\s*=\s*(\/[^\n;]+\/[a-z]*)\s*;/);
if (!matcher) throw new Error('Cannot locate actual server ID validator');
const outputs = [];
for (const entry of input.cases) {
  const calls = [];
  const context = vm.createContext({
    SERVER_ID_RE: vm.runInNewContext(matcher[1]),
    ApiError: class ApiError extends Error {
      constructor(status, message) { super(message); this.status = status; }
    },
    proxyInvoke: async (...args) => { calls.push(args); return entry.response; },
    contractContinued: false,
  });
  let status = null;
  let message = '';
  try {
    await vm.runInContext(
      source.slice(start, end) +
      '\n(async () => { await authorizeServer("surface-owner", ' +
      JSON.stringify(input.group_id) +
      '); globalThis.contractContinued = true; })()',
      context, {timeout: 1000},
    );
  } catch (error) {
    status = error.status ?? null;
    message = error.message;
  }
  outputs.push({name: entry.name, continued: context.contractContinued, status, message, calls});
}
process.stdout.write(JSON.stringify(outputs));
"""


class NodeAuthorizationSurfaceTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.node = shutil.which("node")
        if not self.node:
            self.skipTest("Node is required for the actual authorizeServer VM regression")

    def authorize(self, cases):
        # The VM receives no AWS SDK, fetch, require, process, or timers.
        script = "(async () => {\n" + NODE_AUTH_HARNESS + "\n})().catch(error => { console.error(error); process.exitCode = 1; });"
        result = subprocess.run(
            [self.node, "-e", script],
            input=json.dumps({"source": str(STREAM_PATH), "group_id": GID, "cases": cases}),
            env={"PATH": str(Path(self.node).parent)},
            text=True, capture_output=True, timeout=10, check=True,
        )
        outputs = json.loads(result.stdout)
        self.assertEqual(len(outputs), len(cases))
        for output in outputs:
            self.assertEqual(output["calls"], [[
                GID, "surface-owner",
                {"jsonrpc": "2.0", "id": "group-access", "method": "ping"}, 15000,
            ]])
        return outputs

    @staticmethod
    def case(name, body, status=200):
        return {"name": name, "response": {
            "status": status, "text": json.dumps(body),
        }}

    def test_valid_ping_result_reaches_the_continuation(self):
        outputs = self.authorize([self.case("allowed", {
            "jsonrpc": "2.0", "id": "group-access", "result": {},
        })])
        self.assertTrue(outputs[0]["continued"])
        self.assertIsNone(outputs[0]["status"])

    def test_http_200_rpc_denials_never_reach_the_continuation(self):
        error = {"code": -32000, "message": "upstream-private-diagnostic", "data": {"status": 403}}
        cases = [
            self.case("permission error", {"jsonrpc": "2.0", "id": "group-access", "error": error}),
            self.case("error and result", {"jsonrpc": "2.0", "id": "group-access", "error": error, "result": {}}),
            self.case("tool error", {"jsonrpc": "2.0", "id": "group-access", "result": {"isError": True}}),
        ]
        for output in self.authorize(cases):
            with self.subTest(case=output["name"]):
                self.assertFalse(output["continued"])
                self.assertEqual(output["status"], 403)
                self.assertNotIn("upstream-private-diagnostic", output["message"])

    def test_malformed_ping_responses_never_reach_the_continuation(self):
        cases = [
            self.case("null", None),
            self.case("array", []),
            self.case("missing result", {"jsonrpc": "2.0", "id": "group-access"}),
            self.case("null result", {"jsonrpc": "2.0", "id": "group-access", "result": None}),
            self.case("string result", {"jsonrpc": "2.0", "id": "group-access", "result": "ok"}),
            self.case("wrong id", {"jsonrpc": "2.0", "id": "another-request", "result": {}}),
            self.case("wrong protocol", {"jsonrpc": "1.0", "id": "group-access", "result": {}}),
            {"name": "invalid JSON", "response": {"status": 200, "text": "not JSON"}},
        ]
        for output in self.authorize(cases):
            with self.subTest(case=output["name"]):
                self.assertFalse(output["continued"])
                self.assertEqual(output["status"], 502 if output["name"] == "invalid JSON" else 403)

    def test_http_denial_and_failed_proxy_invoke_never_reach_the_continuation(self):
        cases = [self.case(str(status), {}, status=status) for status in (403, 503, 0)]
        for output in self.authorize(cases):
            with self.subTest(case=output["name"]):
                self.assertFalse(output["continued"])
                self.assertEqual(output["status"], int(output["name"]) or 502)


class ServerListingSurfaceTests(OfflineCase):
    def test_only_data_ready_nonrecovery_groups_become_mcp_endpoints(self):
        groups = []
        states = [
            {"status": "READY", "data_ready": True},
            {"status": "PARTIAL", "data_ready": True},
            {"status": "READY", "data_ready": False, "stale": True},
            {"status": "READY", "data_ready": False, "access_recovery": True},
            {"status": "READY", "data_ready": True, "access_recovery": True},
            {"status": "READY"},  # Missing readiness must fail closed.
            {"status": "BUILDING", "data_ready": False},
            {"status": "READY", "data_ready": True, "stale": True},
        ]
        for index, state in enumerate(states):
            groups.append({
                "group_id": f"grp_{index:032x}", "name": f"Group {index}",
                "description": f"Group context {index}", **state,
            })
        original = copy.deepcopy(groups)
        list_groups = mock.Mock(return_value=groups)
        ddb = object()
        platform = platform_functions(
            "list_servers",
            _grants=lambda sub, prefix: [], _registry_items=lambda ids: {},
            groups_api=SimpleNamespace(list_for_user=list_groups), _ddbc=ddb,
            PLATFORM_TABLE="offline-platform", REGISTRY_TABLE="offline-registry",
            MCP_BASE_URL="https://offline.example.test/v1",
            _resp=lambda status, body: (status, body),
        )
        ident = {"sub": "owner"}
        status, result = platform.list_servers({}, ident, {})
        self.assertEqual(status, 200)
        list_groups.assert_called_once_with(
            ident, ddb=ddb, platform_table="offline-platform",
            registry_table="offline-registry", mcp_base="https://offline.example.test/v1",
        )
        endpoints = [server for server in result["servers"] if server["kind"] == "group"]
        self.assertEqual([server["server_id"] for server in endpoints],
                         [group["group_id"] for group in groups[:2]])
        self.assertEqual([server["build_status"] for server in endpoints], ["READY", "PARTIAL"])
        for server in endpoints:
            self.assertIs(server["data_ready"], True)
            self.assertIs(server["stale"], False)
            self.assertIs(server["connection_available"], True)
            self.assertRegex(server["server_name"], r"^graphify-group-[0-9a-f]{12}$")
            self.assertNotEqual(server["server_name"], server["name"])
            self.assertEqual(server["mcp_url"],
                             "https://offline.example.test/v1/mcp/" + server["server_id"])
        self.assertEqual(result["servers"][0]["server_id"], "all", "The hub must remain listed")
        self.assertEqual(groups, original)
        for group in groups[2:]:
            self.assertNotIn(group["group_id"], json.dumps(result))

    def test_group_only_sources_never_advertise_an_available_dedicated_connection(self):
        items = {
            "live": {"enabled": "1", "runtime_id": "live-runtime"},
            "group-only": {"enabled": "1", "dedicated_runtime": "0"},
            "group-only-old-runtime": {
                "enabled": "1", "dedicated_runtime": "0", "runtime_id": "old-runtime",
            },
            "disabled": {"enabled": "0", "runtime_id": "disabled-runtime"},
            "no-runtime": {"enabled": "1"},
        }
        platform = platform_functions(
            "list_servers", "_repo_view",
            _grants=lambda sub, prefix: [{"sk": f"REPO#{rid}"} for rid in items],
            _registry_items=lambda ids: items, _can_manage=lambda item, ident: True,
            groups_api=SimpleNamespace(list_for_user=lambda *args, **kwargs: []),
            runtimes=SimpleNamespace(runtime_status=lambda rid: "READY"),
            _ddbc=object(), PLATFORM_TABLE="offline-platform", REGISTRY_TABLE="offline-registry",
            MCP_BASE_URL="https://offline.example.test/v1",
            LLM_DEFAULT_MODEL="test-model", LLM_CORPUS_CAP_MB_DEFAULT=10,
            _resp=lambda status, body: (status, body),
        )
        status, result = platform.list_servers({}, {"sub": "owner"}, {})
        self.assertEqual(status, 200)
        sources = {s["server_id"]: s for s in result["servers"] if s["kind"] == "repo"}
        for rid, item in items.items():
            with self.subTest(source=rid):
                expected = rid == "live"
                self.assertIs(sources[rid]["connection_available"], expected)
                view = platform._repo_view({"repo_id": rid, **item})
                self.assertIs(view["connection_available"], expected)
                self.assertIs(view["dedicated_runtime"], not rid.startswith("group-only"))


class SourceIdentitySurfaceTests(OfflineCase):
    def setUp(self):
        super().setUp()
        shared = ROOT / "lambdas/shared/python"
        self.access = load_module("surface_group_access", shared / "group_access.py")
        with mock.patch.dict(sys.modules, {"group_access": self.access}):
            self.api = load_module("surface_groups_api", ROOT / "lambdas/platform_api/groups_api.py")
            self.query = load_module("surface_group_query", shared / "group_query.py")

    def snapshot(self, source_id):
        config = self.api._config({
            "name": "Cross-surface source identity",
            "sources": [{"source_id": source_id}],
        })
        meta = {
            **config, "group_id": GID, "revision": 1, "active_revision": 1,
            "active_version": "bld_" + "b" * 32, "status": "READY",
            "active_source_versions": {source_id: "source-version-1"},
        }
        return self.query._snapshot(meta, GID)

    def test_management_accepted_ids_through_200_characters_survive_query_snapshot(self):
        for length in (160, 161, 174, 200):
            with self.subTest(length=length):
                source_id = "github__org__" + "r" * (length - len("github__org__"))
                snapshot = self.snapshot(source_id)
                self.assertEqual(dict(snapshot[2]), {source_id: "source-version-1"})

    def test_internal_double_dots_are_legal_in_source_ids_on_both_surfaces(self):
        source_id = "github__org__repo..schema__main"
        self.assertEqual(dict(self.snapshot(source_id)[2]), {source_id: "source-version-1"})

    def test_query_rejects_oversized_and_path_like_source_components(self):
        for source_id in ("x" * 201, ".", "..", "../repo", "repo/child", r"repo\child"):
            with self.subTest(source_id=source_id):
                with self.assertRaises(self.access.GroupError) as error:
                    self.query._component(source_id, "source ID", 400)
                self.assertEqual(error.exception.status, 400)


if __name__ == "__main__":
    unittest.main()
