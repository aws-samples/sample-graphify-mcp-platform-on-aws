"""Offline groups API contracts with atomic, stateful DynamoDB storage.

Run: .venv/bin/python -B -m unittest discover -s tests -p test_groups_api.py
"""

import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import socket
import sys
import types
import unittest
from unittest import mock

import boto3
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import ClientError


ROOT = Path(__file__).resolve().parents[1]
PLATFORM = "offline-platform"
REGISTRY = "offline-registry"
BUCKET = "offline-artifacts"
OWNER = "owner-sub"
TARGET = "target-sub"
NOW = 1_800_000_000
_SERIALIZER = TypeSerializer()
_DESERIALIZER = TypeDeserializer()
_MISSING = object()


def typed(item):
    return {key: _SERIALIZER.serialize(value) for key, value in item.items()}


def decoded(item):
    return {key: _DESERIALIZER.deserialize(value) for key, value in item.items()}


def aws_error(code, operation):
    return ClientError({"Error": {"Code": code, "Message": "Offline injected failure"}}, operation)


class Expression:
    """Evaluate the DynamoDB conditions used here; unknown syntax fails loudly."""

    TOKEN = re.compile(r"\s*(<=|>=|<>|[=<>(),]|[#:]?[A-Za-z0-9_.-]+)")

    def __init__(self, expression, item, names=None, values=None):
        self.tokens = self.TOKEN.findall(expression)
        if "".join(self.tokens) != re.sub(r"\s+", "", expression):
            raise AssertionError(f"Unsupported DynamoDB expression: {expression!r}")
        self.index = 0
        self.item = item
        self.names = names or {}
        self.values = values or {}

    def take(self, expected=None):
        token = self.tokens[self.index]
        self.index += 1
        if expected is not None and token.upper() != expected.upper():
            raise AssertionError(f"Expected {expected!r}, got {token!r}")
        return token

    def peek(self, value):
        return self.index < len(self.tokens) and self.tokens[self.index].upper() == value.upper()

    def operand(self):
        token = self.take()
        if token.startswith(":"):
            return self.values[token]
        return self.item.get(self.names.get(token, token), _MISSING)

    def atom(self):
        if self.peek("("):
            self.take("(")
            result = self.or_expression()
            self.take(")")
            return result
        if self.peek("NOT"):
            self.take("NOT")
            return not self.atom()
        if any(self.peek(name) for name in ("attribute_exists", "attribute_not_exists", "begins_with")):
            function = self.take().lower()
            self.take("(")
            left = self.operand()
            if function == "begins_with":
                self.take(",")
                right = self.operand()
                result = left is not _MISSING and left.startswith(right)
            else:
                result = (left is not _MISSING) == (function == "attribute_exists")
            self.take(")")
            return result
        left, operation, right = self.operand(), self.take(), self.operand()
        if left is _MISSING or right is _MISSING:
            return False
        if operation == "=":
            return left == right
        if operation == "<>":
            return left != right
        if operation == "<":
            return left < right
        if operation == "<=":
            return left <= right
        if operation == ">":
            return left > right
        if operation == ">=":
            return left >= right
        raise AssertionError(f"Unsupported comparison: {operation!r}")

    def and_expression(self):
        result = self.atom()
        while self.peek("AND"):
            self.take("AND")
            right = self.atom()
            result = result and right
        return result

    def or_expression(self):
        result = self.and_expression()
        while self.peek("OR"):
            self.take("OR")
            right = self.and_expression()
            result = result or right
        return result

    def evaluate(self):
        result = self.or_expression()
        if self.index != len(self.tokens):
            raise AssertionError(f"Unconsumed DynamoDB tokens: {self.tokens[self.index:]}")
        return result


class MemoryDynamo:
    """Low-level AttributeValue API; transactions check before committing."""

    def __init__(self):
        self.tables = {PLATFORM: {}, REGISTRY: {}}
        self.calls = []
        self.before_transaction = None
        self.page_size = 100

    @staticmethod
    def key(table, item):
        fields = ("repo_id",) if table == REGISTRY else ("pk", "sk")
        return tuple(item[field] for field in fields)

    def seed(self, table, item):
        self.tables[table][self.key(table, item)] = copy.deepcopy(item)

    def row(self, table, **key):
        return copy.deepcopy(self.tables[table].get(self.key(table, key)))

    def remove(self, table, **key):
        self.tables[table].pop(self.key(table, key), None)

    @staticmethod
    def matches(expression, item, request):
        return Expression(
            expression, item, request.get("ExpressionAttributeNames"),
            decoded(request.get("ExpressionAttributeValues", {})),
        ).evaluate()

    def get_item(self, **request):
        self.calls.append(("get_item", copy.deepcopy(request)))
        table = request["TableName"]
        item = self.tables[table].get(self.key(table, decoded(request["Key"])))
        return {"Item": typed(copy.deepcopy(item))} if item is not None else {}

    def query(self, **request):
        self.calls.append(("query", copy.deepcopy(request)))
        table = request["TableName"]
        rows = [
            item for item in self.tables[table].values()
            if self.matches(request["KeyConditionExpression"], item, request)
        ]
        sort_key = "gsi1sk" if request.get("IndexName") else "sk"
        rows.sort(key=lambda row: (row.get(sort_key, ""), self.key(table, row)))
        if request.get("ScanIndexForward") is False:
            rows.reverse()
        if request.get("ExclusiveStartKey"):
            start = self.key(table, decoded(request["ExclusiveStartKey"]))
            position = next(i for i, row in enumerate(rows) if self.key(table, row) == start)
            rows = rows[position + 1:]
        size = min(request.get("Limit", self.page_size), self.page_size)
        page = rows[:size]
        result = {"Items": [typed(copy.deepcopy(row)) for row in page], "Count": len(page)}
        if len(rows) > size:
            fields = ("repo_id",) if table == REGISTRY else ("pk", "sk")
            result["LastEvaluatedKey"] = typed({field: page[-1][field] for field in fields})
        return result

    @staticmethod
    def comma_parts(expression):
        return re.split(r",\s*(?![^()]*\))", expression)

    @classmethod
    def updated(cls, item, request):
        item = copy.deepcopy(item)
        names = request.get("ExpressionAttributeNames", {})
        values = decoded(request.get("ExpressionAttributeValues", {}))

        def value(expression):
            expression = expression.strip()
            default = re.fullmatch(r"if_not_exists\(([^,]+),\s*([^)]+)\)", expression)
            if default:
                field, fallback = default.groups()
                return item.get(names.get(field, field), value(fallback))
            addition = re.fullmatch(r"(.+?)\s+([+-])\s+(.+)", expression)
            if addition:
                left, operator, right = addition.groups()
                return value(left) + value(right) * (1 if operator == "+" else -1)
            if expression.startswith(":"):
                return values[expression]
            return item[names.get(expression, expression)]

        clauses = re.split(r"\b(SET|REMOVE|ADD|DELETE)\b", request["UpdateExpression"])[1:]
        if not clauses:
            raise AssertionError("Unsupported empty UpdateExpression")
        for operation, expression in zip(clauses[::2], clauses[1::2]):
            for part in cls.comma_parts(expression.strip()):
                if operation == "SET":
                    field, rhs = part.split("=", 1)
                    field = field.strip()
                    item[names.get(field, field)] = copy.deepcopy(value(rhs))
                elif operation == "REMOVE":
                    item.pop(names.get(part, part), None)
                elif operation == "ADD":
                    field, rhs = part.split()
                    field = names.get(field, field)
                    item[field] = item.get(field, 0) + value(rhs)
                else:
                    raise AssertionError(f"Unsupported update operation: {operation!r}")
        return item

    def transact_write_items(self, **request):
        self.calls.append(("transact_write_items", copy.deepcopy(request)))
        if self.before_transaction:
            callback, self.before_transaction = self.before_transaction, None
            callback(self)
        writes = []
        seen = set()
        for transaction in request["TransactItems"]:
            operation, parameters = next(iter(transaction.items()))
            table = parameters["TableName"]
            key_item = decoded(parameters["Item"] if operation == "Put" else parameters["Key"])
            key = self.key(table, key_item)
            if (table, key) in seen:
                raise AssertionError("A DynamoDB transaction cannot target an item twice")
            seen.add((table, key))
            current = self.tables[table].get(key, {})
            condition = parameters.get("ConditionExpression")
            if condition and not self.matches(condition, current, parameters):
                raise aws_error("TransactionCanceledException", "TransactWriteItems")
            if operation == "ConditionCheck":
                continue
            if operation == "Put":
                item = key_item
            elif operation == "Delete":
                item = None
            elif operation == "Update":
                item = self.updated(current or key_item, parameters)
            else:
                raise AssertionError(f"Unsupported transaction operation: {operation!r}")
            writes.append((table, key, item))
        for table, key, item in writes:
            if item is None:
                self.tables[table].pop(key, None)
            else:
                self.tables[table][key] = copy.deepcopy(item)
        return {}

    def update_item(self, **request):
        self.calls.append(("update_item", copy.deepcopy(request)))
        table = request["TableName"]
        key_item = decoded(request["Key"])
        key = self.key(table, key_item)
        current = self.tables[table].get(key, {})
        condition = request.get("ConditionExpression")
        if condition and not self.matches(condition, current, request):
            raise aws_error("ConditionalCheckFailedException", "UpdateItem")
        item = self.updated(current or key_item, request)
        self.tables[table][key] = item
        return {"Attributes": typed(copy.deepcopy(item))} if request.get("ReturnValues") else {}


class MemoryS3:
    def __init__(self):
        self.objects = set()
        self.heads = []

    def head_object(self, **request):
        self.heads.append(copy.deepcopy(request))
        if (request["Bucket"], request["Key"]) not in self.objects:
            raise aws_error("404", "HeadObject")
        return {"ContentLength": 123, "ETag": '"offline"'}


class MemoryLambda:
    def __init__(self):
        self.calls = []
        self.failure = None
        self.on_invoke = None
        self.status_code = 202

    def invoke(self, **request):
        self.calls.append(copy.deepcopy(request))
        if self.on_invoke:
            self.on_invoke(request)
        if self.failure:
            raise self.failure
        return {"StatusCode": self.status_code}


class MemoryCognito:
    def __init__(self):
        self.users = []
        self.calls = []

    def list_users(self, **request):
        self.calls.append(copy.deepcopy(request))
        return {"Users": copy.deepcopy(self.users)}


def cognito_user(sub=TARGET, email="target@example.test", *, enabled=True, verified="true"):
    return {
        "Username": sub,
        "Enabled": enabled,
        "Attributes": [
            {"Name": "sub", "Value": sub},
            {"Name": "email", "Value": email},
            {"Name": "email_verified", "Value": verified},
        ],
    }


def fallback_group_access():
    """Small authorization boundary for an unfinished shared-module checkout."""
    module = types.ModuleType("group_access")

    class GroupError(Exception):
        def __init__(self, status, message):
            super().__init__(message)
            self.status = status

    def get_item(ddb, table, key):
        response = ddb.get_item(TableName=table, Key=typed(key), ConsistentRead=True)
        return decoded(response["Item"]) if response.get("Item") else None

    def source_access(ddb, platform_table, registry_table, sub, source_id):
        source = get_item(ddb, registry_table, {"repo_id": source_id})
        if not source or source.get("enabled") != "1":
            raise GroupError(403, "source unavailable")
        if source.get("graph_scope") != "public" and not get_item(
            ddb, platform_table, {"pk": f"USER#{sub}", "sk": f"REPO#{source_id}"}
        ):
            raise GroupError(403, "source access required")
        return source

    def is_group_id(value):
        return isinstance(value, str) and re.fullmatch(r"grp_[0-9a-f]{32}", value) is not None

    def require_access(ddb, platform_table, registry_table, sub, gid, *,
                       write=False, check_versions=False):
        if not sub or not is_group_id(gid):
            raise GroupError(404, "group not found")
        if get_item(ddb, platform_table, {"pk": f"USER#{sub}", "sk": "DELETED"}):
            raise GroupError(403, "account deleted")
        grant = get_item(ddb, platform_table, {"pk": f"USER#{sub}", "sk": f"GROUP#{gid}"})
        if not grant or grant.get("role") not in ("owner", "editor", "viewer"):
            raise GroupError(403, "membership required")
        meta = get_item(ddb, platform_table, {"pk": f"GROUP#{gid}", "sk": "META"})
        if not meta or meta.get("status") == "DELETED":
            raise GroupError(404, "group not found")
        if write and grant["role"] not in ("owner", "editor"):
            raise GroupError(403, "write access required")
        versions = {}
        for source in meta.get("sources", []):
            sid = source["source_id"]
            source_row = source_access(ddb, platform_table, registry_table, sub, sid)
            versions[sid] = source_row.get("active_source_version", "")
        if check_versions and (
            meta.get("status") not in ("READY", "PARTIAL") or not meta.get("active_version")
            or meta.get("active_revision") != meta.get("revision")
            or not versions or not all(versions.values())
            or versions != meta.get("active_source_versions")
        ):
            raise GroupError(409, "group graph is not current")
        return meta

    module.GroupError = GroupError
    module.get_item = get_item
    module.is_group_id = is_group_id
    module.source_access = source_access
    module.require_access = require_access
    return module


def load_api():
    def load(name, path):
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    with mock.patch.object(boto3, "client", side_effect=AssertionError("AWS client forbidden")), \
            mock.patch.object(boto3, "resource", side_effect=AssertionError("AWS resource forbidden")), \
            mock.patch.object(boto3.session.Session, "client", side_effect=AssertionError("AWS forbidden")), \
            mock.patch.object(boto3.session.Session, "resource", side_effect=AssertionError("AWS forbidden")), \
            mock.patch.object(socket.socket, "connect", side_effect=AssertionError("Network forbidden")):
        shared_path = ROOT / "lambdas/shared/python/group_access.py"
        shared = load("group_access", shared_path) if shared_path.is_file() else fallback_group_access()
        with mock.patch.dict(sys.modules, {"group_access": shared}):
            return load("offline_groups_api", ROOT / "lambdas/platform_api/groups_api.py")


class DynamoAtomicityTests(unittest.TestCase):
    def test_later_failed_condition_rolls_back_earlier_writes(self):
        ddb = MemoryDynamo()
        original = {"pk": "GROUP#g", "sk": "META", "revision": 2}
        ddb.seed(PLATFORM, original)
        with self.assertRaises(ClientError):
            ddb.transact_write_items(TransactItems=[
                {"Put": {"TableName": PLATFORM, "Item": typed({"pk": "USER#u", "sk": "GROUP#g"})}},
                {"Put": {
                    "TableName": PLATFORM, "Item": typed({**original, "revision": 3}),
                    "ConditionExpression": "revision = :expected",
                    "ExpressionAttributeValues": typed({":expected": 1}),
                }},
            ])
        self.assertIsNone(ddb.row(PLATFORM, pk="USER#u", sk="GROUP#g"))
        self.assertEqual(ddb.row(PLATFORM, pk="GROUP#g", sk="META"), original)

    def test_condition_does_not_treat_a_missing_field_as_not_equal(self):
        self.assertFalse(Expression("#status <> :deleted", {}, {"#status": "status"},
                                    {":deleted": "DELETED"}).evaluate())
        self.assertTrue(Expression("attribute_not_exists(pk) OR (revision = :r AND #s <> :d)",
                                   {"pk": "g", "revision": 2, "status": "READY"},
                                   {"#s": "status"}, {":r": 2, ":d": "DELETED"}).evaluate())


class GroupsApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.api = load_api()

    def setUp(self):
        for target, attribute in (
            (boto3, "client"), (boto3, "resource"),
            (boto3.session.Session, "client"), (boto3.session.Session, "resource"),
            (socket.socket, "connect"), (socket, "create_connection"),
        ):
            patcher = mock.patch.object(
                target, attribute, side_effect=AssertionError("This test must remain offline"),
            )
            patcher.start()
            self.addCleanup(patcher.stop)
        self.ddb = MemoryDynamo()
        self.s3 = MemoryS3()
        self.worker = MemoryLambda()
        self.cognito = MemoryCognito()
        self.cognito.users = [cognito_user()]
        self.now = NOW
        clock = mock.patch.object(self.api.time, "time", side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)
        self.ident = {"sub": OWNER, "username": "owner@example.test", "groups": ["member"]}
        self.dependencies = {
            "platform": mock.Mock(spec=(), name="unused-platform-resource"),
            "registry": mock.Mock(spec=(), name="unused-registry-resource"),
            "ddb": self.ddb, "s3": self.s3, "lambda_client": self.worker,
            "worker_fn": "offline-group-worker", "platform_table": PLATFORM,
            "registry_table": REGISTRY, "bucket": BUCKET,
            "mcp_base": "https://offline.example.test", "cognito": self.cognito,
            "user_pool_id": "offline-user-pool",
        }
        self.seed_source("source-a")
        self.seed_source("source-b")

    def tearDown(self):
        for operation, request in self.ddb.calls:
            if operation in ("get_item", "query") and not request.get("IndexName"):
                self.assertIs(request.get("ConsistentRead"), True, request)

    def seed_source(self, sid, *, scope="public", enabled="1", version="version-1"):
        self.ddb.seed(REGISTRY, {
            "repo_id": sid, "enabled": enabled, "graph_scope": scope,
            "active_source_version": version, "description": "Shared source description",
            "description_version": 3, "created_by_sub": OWNER,
        })
        if version:
            prefix = f"source-versions/{sid}/{version}/"
            self.s3.objects.update({
                (BUCKET, prefix + "manifest.json"),
                (BUCKET, prefix + "graph.json"),
                (BUCKET, prefix + "src.tar.gz"),
            })

    def grant_source(self, sid, sub=OWNER):
        self.ddb.seed(PLATFORM, {
            "pk": f"USER#{sub}", "sk": f"REPO#{sid}",
            "personal_description": "Private subscriber context",
        })

    def grant_group(self, gid, sub=TARGET, role="viewer"):
        self.ddb.seed(PLATFORM, {
            "pk": f"USER#{sub}", "sk": f"GROUP#{gid}", "role": role,
            "gsi1pk": f"GROUP#{gid}", "gsi1sk": f"USER#{sub}", "status": "ACTIVE",
        })

    def request(self, method, path="/groups", body=None, *, sub=OWNER):
        event = {"body": json.dumps(body) if body is not None else ""}
        ident = {**self.ident, "sub": sub}
        result = self.api.handle(event, ident, method, path, **self.dependencies)
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 2)
        self.assertIsInstance(result[0], int)
        self.assertIsInstance(result[1], dict)
        return result

    def creation_body(self, **overrides):
        return {
            "name": "Offline source group",
            "description": "Frontend and API contracts",
            "sources": [{"source_id": "source-a", "role": "frontend"}],
            "idempotency_key": "create-token-0001",
            **overrides,
        }

    def create(self, **overrides):
        status, result = self.request("POST", body=self.creation_body(**overrides))
        self.assertEqual(status, 201, result)
        return result["group"]

    def meta(self, gid):
        return self.ddb.row(PLATFORM, pk=f"GROUP#{gid}", sk="META")

    def ledger(self, gid, build_id):
        return self.ddb.row(PLATFORM, pk=f"GROUP#{gid}", sk=f"BUILD#{build_id}")

    def expect_unchanged_error(self, method, path, body, status, *, sub=OWNER):
        before = copy.deepcopy(self.ddb.tables)
        dispatches = len(self.worker.calls)
        actual, result = self.request(method, path, body, sub=sub)
        self.assertEqual(actual, status, result)
        self.assertEqual(self.ddb.tables, before)
        self.assertEqual(len(self.worker.calls), dispatches)
        return result

    def test_creation_requires_a_bounded_safe_idempotency_key(self):
        for token in (None, "", "short", "a" * 129, "../create-token", "create token", "token\n1234"):
            with self.subTest(token=token):
                self.expect_unchanged_error(
                    "POST", "/groups", self.creation_body(idempotency_key=token), 400,
                )
        self.assertEqual(self.s3.heads, [])

    def test_creation_refuses_disabled_private_or_implicit_public_sources(self):
        for changes in (
            {"enabled": "0"}, {"enabled": True}, {"graph_scope": "private"},
            {"graph_scope": ""}, {"graph_scope": None},
        ):
            with self.subTest(changes=changes):
                source = self.ddb.row(REGISTRY, repo_id="source-a")
                self.ddb.seed(REGISTRY, {**source, "enabled": "1", "graph_scope": "public", **changes})
                self.expect_unchanged_error("POST", "/groups", self.creation_body(), 403)
        source = self.ddb.row(REGISTRY, repo_id="source-a")
        source.pop("graph_scope")
        self.ddb.seed(REGISTRY, source)
        self.expect_unchanged_error("POST", "/groups", self.creation_body(), 403)

    def test_source_id_validation_precedes_any_artifact_request(self):
        for sid in ("../secret", "source/a", "", "source\nid"):
            with self.subTest(source_id=sid):
                self.expect_unchanged_error(
                    "POST", "/groups", self.creation_body(sources=[{"source_id": sid}]), 400,
                )
        self.assertEqual(self.s3.heads, [])

    def test_source_limit_and_duplicate_ids_are_enforced(self):
        for number in range(9):
            self.seed_source(f"source-{number}")
        for sources in (
            [{"source_id": f"source-{number}"} for number in range(9)],
            [{"source_id": "source-a"}, {"source_id": "source-a", "role": "backend"}],
        ):
            with self.subTest(sources=sources):
                self.expect_unchanged_error(
                    "POST", "/groups", self.creation_body(sources=sources), 400,
                )

    def test_create_get_and_list_preserve_context_without_copying_personal_descriptions(self):
        self.seed_source("source-a", scope="private")
        self.grant_source("source-a")
        source = {"source_id": "source-a", "role": "frontend", "description": "Group-only context"}
        registry_before = copy.deepcopy(self.ddb.tables[REGISTRY])
        original_grant = self.ddb.row(PLATFORM, pk=f"USER#{OWNER}", sk="REPO#source-a")
        group = self.create(sources=[source])
        gid = group["group_id"]
        self.assertEqual(group["revision"], 1)
        self.assertEqual(group["sources"], [source])
        self.assertEqual(group["kind"], "group")
        self.assertEqual(group["role"], "owner")
        self.assertFalse(group["data_ready"])
        self.assertTrue(group["stale"])
        self.assertEqual(group["mcp_url"], f"https://offline.example.test/mcp/{gid}")
        self.assertEqual(self.meta(gid)["sources"], [source])
        grant = self.ddb.row(PLATFORM, pk=f"USER#{OWNER}", sk=f"GROUP#{gid}")
        mirror = self.ddb.row(PLATFORM, pk=f"GROUP#{gid}", sk=f"MEMBER#{OWNER}")
        self.assertEqual(grant["role"], "owner")
        self.assertEqual(mirror["role"], "owner")
        self.assertEqual(mirror["sub"], OWNER)
        for path in (f"/groups/{gid}", "/groups"):
            status, result = self.request("GET", path)
            self.assertEqual(status, 200, result)
            self.assertNotIn("Private subscriber context", json.dumps(result))
            self.assertNotIn("Shared source description", json.dumps(result))
        listed = self.api.list_for_user(
            self.ident, ddb=self.ddb, platform_table=PLATFORM, registry_table=REGISTRY,
            mcp_base=self.dependencies["mcp_base"], unused_dependency=object(),
        )
        self.assertEqual(listed, [group])
        self.assertEqual(self.ddb.tables[REGISTRY], registry_before)
        self.assertEqual(self.ddb.row(PLATFORM, pk=f"USER#{OWNER}", sk="REPO#source-a"), original_grant)
        self.assertEqual(self.worker.calls, [])

    def test_create_replay_is_deterministic_and_rejects_changed_payload(self):
        group = self.create()
        gid = group["group_id"]
        digest_input = json.dumps(
            ["create", OWNER, "create-token-0001"], ensure_ascii=False, separators=(",", ":"),
        ).encode()
        self.assertEqual(gid, "grp_" + hashlib.sha256(digest_input).hexdigest()[:32])
        before = copy.deepcopy(self.ddb.tables)
        status, replay = self.request("POST", body=self.creation_body())
        self.assertEqual(status, 200, replay)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["group"], group)
        self.assertEqual(self.ddb.tables, before)
        self.expect_unchanged_error("POST", "/groups", self.creation_body(name="Different group"), 409)
        status, another = self.request("POST", body=self.creation_body(), sub=TARGET)
        self.assertEqual(status, 201, another)
        self.assertNotEqual(another["group"]["group_id"], gid)

    def test_simultaneous_create_with_same_token_returns_committed_group_as_replay(self):
        committed = []
        self.ddb.before_transaction = lambda _: committed.append(self.create())
        status, result = self.request("POST", body=self.creation_body())
        self.assertEqual(status, 200, result)
        self.assertTrue(result["replayed"])
        self.assertEqual(result["group"], committed[0])
        self.assertEqual(len(self.ddb.tables[PLATFORM]), 3)
        self.assertEqual(self.worker.calls, [])

    def test_twenty_group_limit_allows_replay_and_reuses_deleted_capacity(self):
        groups = [self.create(idempotency_key=f"capacity-token-{index:04d}")
                  for index in range(20)]
        blocked = self.creation_body(idempotency_key="capacity-overflow")
        self.expect_unchanged_error("POST", "/groups", blocked, 409)
        status, replay = self.request("POST", body=self.creation_body(
            idempotency_key="capacity-token-0000",
        ))
        self.assertEqual(status, 200, replay)
        self.assertEqual(replay["group"]["group_id"], groups[0]["group_id"])
        self.assertTrue(replay["replayed"])
        status, result = self.request("DELETE", f"/groups/{groups[0]['group_id']}",
                                      {"expected_revision": 1})
        self.assertEqual(status, 200, result)
        status, created = self.request("POST", body=blocked)
        self.assertEqual(status, 201, created)
        self.assertNotEqual(created["group"]["group_id"], groups[0]["group_id"])

    def test_invitation_limit_counts_target_grants_without_partial_membership(self):
        gid = self.create()["group_id"]
        target_groups = []
        for index in range(20):
            status, result = self.request("POST", body=self.creation_body(
                idempotency_key=f"target-capacity-{index:04d}",
            ), sub=TARGET)
            self.assertEqual(status, 201, result)
            target_groups.append(result["group"]["group_id"])
        invitation = {"expected_revision": 1, "email": "target@example.test", "role": "viewer"}
        self.expect_unchanged_error("POST", f"/groups/{gid}/members", invitation, 409)
        self.assertIsNone(self.ddb.row(PLATFORM, pk=f"USER#{TARGET}", sk=f"GROUP#{gid}"))
        status, result = self.request("DELETE", f"/groups/{target_groups[0]}",
                                      {"expected_revision": 1}, sub=TARGET)
        self.assertEqual(status, 200, result)
        status, result = self.request("POST", f"/groups/{gid}/members", invitation)
        self.assertEqual(status, 201, result)
        status, result = self.request("POST", f"/groups/{gid}/members", {
            **invitation, "expected_revision": 2, "role": "editor",
        })
        self.assertEqual(status, 201, result)
        self.assertEqual(result["member"]["role"], "editor")

    def test_source_count_description_and_role_boundaries(self):
        sources = []
        for number in range(8):
            sid = f"source-{number}"
            self.seed_source(sid)
            sources.append({"source_id": sid, "role": "custom integration role", "description": "한" * 500})
        group = self.create(description="한" * 1000, sources=sources, idempotency_key="x" * 128)
        self.assertEqual(len(group["sources"]), 8)
        self.assertEqual(group["description"], "한" * 1000)
        self.assertEqual(group["sources"], sources)
        for changes in (
            {"description": "x" * 1001},
            {"sources": [{"source_id": "source-a", "description": "x" * 501}]},
            {"sources": [{"source_id": "source-a", "role": "x" * 65}]},
            {"sources": [{"source_id": "source-a", "role": "bad\x00role"}]},
        ):
            with self.subTest(changes=changes):
                self.expect_unchanged_error(
                    "POST", "/groups", self.creation_body(idempotency_key="invalid-config-01", **changes), 400,
                )

    def test_edit_uses_revision_and_marks_previous_graph_stale(self):
        group = self.create()
        gid = group["group_id"]
        meta = self.meta(gid)
        self.ddb.seed(PLATFORM, {
            **meta, "status": "READY", "active_version": "previous-graph",
            "active_revision": 1, "active_source_versions": {"source-a": "version-1"},
        })
        updated_sources = [{"source_id": "source-b", "role": "backend", "description": "Service contract"}]
        status, result = self.request("POST", f"/groups/{gid}", {
            "expected_revision": 1, "name": "Edited name", "sources": updated_sources,
        })
        self.assertEqual(status, 200, result)
        updated = result["group"]
        self.assertEqual(updated["revision"], 2)
        self.assertEqual(updated["name"], "Edited name")
        self.assertEqual(updated["sources"], updated_sources)
        self.assertEqual(updated["status"], "STALE")
        self.assertFalse(updated["data_ready"])
        self.assertEqual(self.meta(gid)["active_version"], "previous-graph")
        self.expect_unchanged_error(
            "POST", f"/groups/{gid}", {"expected_revision": 1, "name": "Lost update"}, 409,
        )

    def test_all_mutations_require_positive_integer_revision_and_reject_stale_revision(self):
        gid = self.create()["group_id"]
        self.grant_group(gid)
        operations = (
            ("POST", f"/groups/{gid}", {"name": "Edited"}),
            ("DELETE", f"/groups/{gid}", {}),
            ("POST", f"/groups/{gid}/members", {"email": "target@example.test"}),
            ("DELETE", f"/groups/{gid}/members/{TARGET}", {}),
            ("POST", f"/groups/{gid}/rebuild", {"idempotency_key": "build-token-01"}),
        )
        for method, path, body in operations:
            for revision in (None, True, "1", 0, -1, 1.5, 2):
                with self.subTest(method=method, path=path, revision=revision):
                    self.expect_unchanged_error(
                        method, path, {**body, "expected_revision": revision}, 409 if revision == 2 else 400,
                    )
        self.assertEqual(self.cognito.calls, [])
        self.assertEqual(self.s3.heads, [])

    def test_transaction_rechecks_revision_and_never_partially_invites(self):
        gid = self.create()["group_id"]

        def concurrent_edit(ddb):
            ddb.seed(PLATFORM, {**self.meta(gid), "revision": 2, "name": "Concurrent winner"})

        self.ddb.before_transaction = concurrent_edit
        status, result = self.request("POST", f"/groups/{gid}/members", {
            "expected_revision": 1, "email": "target@example.test", "role": "editor",
        })
        self.assertEqual(status, 409, result)
        self.assertEqual(self.meta(gid)["name"], "Concurrent winner")
        self.assertEqual(self.meta(gid)["revision"], 2)
        self.assertIsNone(self.ddb.row(PLATFORM, pk=f"USER#{TARGET}", sk=f"GROUP#{gid}"))
        self.assertIsNone(self.ddb.row(PLATFORM, pk=f"GROUP#{gid}", sk=f"MEMBER#{TARGET}"))

    def test_transaction_rechecks_source_access_on_creation(self):
        before_platform = copy.deepcopy(self.ddb.tables[PLATFORM])

        def revoke_source(ddb):
            ddb.seed(REGISTRY, {**ddb.row(REGISTRY, repo_id="source-a"), "graph_scope": "private"})

        self.ddb.before_transaction = revoke_source
        status, result = self.request("POST", body=self.creation_body())
        self.assertEqual(status, 409, result)
        self.assertEqual(self.ddb.tables[PLATFORM], before_platform)
        self.assertEqual(self.ddb.row(REGISTRY, repo_id="source-a")["graph_scope"], "private")

    def test_delete_tombstones_group_without_removing_source_or_source_grants(self):
        self.seed_source("source-a", scope="private")
        self.grant_source("source-a")
        gid = self.create()["group_id"]
        self.grant_group(gid)
        registry_before = copy.deepcopy(self.ddb.tables[REGISTRY])
        source_grant = self.ddb.row(PLATFORM, pk=f"USER#{OWNER}", sk="REPO#source-a")
        status, result = self.request("DELETE", f"/groups/{gid}", {"expected_revision": 1})
        self.assertEqual(status, 200, result)
        self.assertTrue(result["deleted"])
        self.assertEqual(self.meta(gid)["status"], "DELETED")
        self.assertEqual(self.meta(gid)["revision"], 2)
        self.assertEqual(self.ddb.tables[REGISTRY], registry_before)
        self.assertEqual(self.ddb.row(PLATFORM, pk=f"USER#{OWNER}", sk="REPO#source-a"), source_grant)
        self.expect_unchanged_error("GET", f"/groups/{gid}", None, 404)
        status, result = self.request("GET")
        self.assertEqual(status, 200, result)
        self.assertEqual(result["groups"], [])

    def test_listing_paginates_and_omits_nonowner_groups_after_source_revocation(self):
        self.ddb.page_size = 1
        first = self.create(name="Zulu", idempotency_key="create-list-01")
        second = self.create(name="alpha", idempotency_key="create-list-02")
        self.seed_source("source-b", scope="private")
        self.grant_source("source-b")
        denied = self.create(
            name="Revoked source", sources=[{"source_id": "source-b"}], idempotency_key="create-list-03",
        )
        for group in (first, second, denied):
            self.grant_group(group["group_id"])
        self.grant_source("source-b", TARGET)
        self.ddb.remove(PLATFORM, pk=f"USER#{TARGET}", sk="REPO#source-b")
        self.ddb.seed(PLATFORM, {"pk": f"USER#{TARGET}", "sk": "GROUP#invalid-pointer", "role": "viewer"})
        self.grant_group("grp_" + "f" * 32)
        self.grant_source("unrelated-source", TARGET)
        groups = self.api.list_for_user(
            {**self.ident, "sub": TARGET}, ddb=self.ddb, platform_table=PLATFORM, registry_table=REGISTRY,
        )
        self.assertEqual([group["group_id"] for group in groups], [second["group_id"], first["group_id"]])
        self.assertNotIn(denied["group_id"], json.dumps(groups))
        self.assertTrue(any(request.get("ExclusiveStartKey") for name, request in self.ddb.calls if name == "query"))
        self.expect_unchanged_error("GET", f"/groups/{denied['group_id']}", None, 403, sub=TARGET)

    def test_owner_get_and_list_expose_only_recovery_metadata_until_revoked_source_is_removed(self):
        self.seed_source("source-a", scope="private")
        self.grant_source("source-a")
        original = self.create(sources=[
            {"source_id": "source-a", "role": "frontend", "description": "Revoked context"},
            {"source_id": "source-b", "role": "backend", "description": "Backend context"},
        ])
        gid = original["group_id"]
        self.ddb.seed(PLATFORM, {
            **self.meta(gid), "status": "READY", "active_revision": 1,
            "active_version": "old-group-artifact",
            "active_source_versions": {"source-a": "version-1", "source-b": "version-1"},
            "active_source_descriptions": {"source-a": 3, "source-b": 3},
            "build_id": "old-build-id", "build_started_at": NOW,
            "usage": {"total_tokens": 123}, "last_error": "Raw provider diagnostic",
            "worker_token": "old-worker-token", "worker_lease_expires_at": NOW + 900,
        })
        self.ddb.remove(PLATFORM, pk=f"USER#{OWNER}", sk="REPO#source-a")

        def assert_recovery(view, revision, description):
            self.assertEqual(view["group_id"], gid)
            self.assertEqual(view["owner_sub"], OWNER)
            self.assertEqual(view["role"], "owner")
            self.assertEqual(view["name"], original["name"])
            self.assertEqual(view["description"], description)
            self.assertEqual(view["revision"], revision)
            self.assertEqual(view["sources"], [{"source_id": "source-a"}, {"source_id": "source-b"}])
            self.assertIs(view["access_recovery"], True)
            self.assertIs(view["data_ready"], False)
            self.assertIs(view["stale"], True)
            self.assertLessEqual(set(view), {
                "group_id", "owner_sub", "name", "description", "revision", "llm_enabled", "model",
                "status", "created_at", "updated_at", "kind", "server_id", "role", "sources",
                "data_ready", "stale", "access_recovery",
            })

        def assert_recovery_reads(revision, description):
            before = copy.deepcopy(self.ddb.tables)
            status, detail = self.request("GET", f"/groups/{gid}")
            self.assertEqual(status, 200, detail)
            assert_recovery(detail["group"], revision, description)
            status, listing = self.request("GET", "/groups")
            self.assertEqual(status, 200, listing)
            self.assertEqual(listing["groups"], [detail["group"]])
            groups = self.api.list_for_user(self.ident, **self.dependencies)
            self.assertEqual(groups, [detail["group"]])
            self.assertEqual(self.ddb.tables, before)

        assert_recovery_reads(1, original["description"])
        status, recovered = self.request("POST", f"/groups/{gid}", {
            "expected_revision": 1, "description": "Repair in progress",
        })
        self.assertEqual(status, 200, recovered)
        assert_recovery(recovered["group"], 2, "Repair in progress")
        assert_recovery_reads(2, "Repair in progress")
        status, repaired = self.request("POST", f"/groups/{gid}", {
            "expected_revision": 2, "sources": [{"source_id": "source-b"}],
        })
        self.assertEqual(status, 200, repaired)
        self.assertEqual(repaired["group"]["revision"], 3)
        surviving_sources = [{
            "source_id": "source-b", "role": "backend", "description": "Backend context",
        }]
        self.assertEqual(repaired["group"]["sources"], surviving_sources)
        self.assertEqual(self.meta(gid)["sources"], surviving_sources)
        status, result = self.request("GET", f"/groups/{gid}")
        self.assertEqual(status, 200, result)
        self.assertEqual(result["group"]["sources"], surviving_sources)
        self.assertFalse(result["group"].get("access_recovery", False))
        self.assertEqual(result["group"]["mcp_url"], f"https://offline.example.test/mcp/{gid}")

    def test_revoked_source_viewers_and_editors_are_denied_and_omitted_from_lists(self):
        self.seed_source("source-a", scope="private")
        self.grant_source("source-a")
        gid = self.create()["group_id"]
        for role in ("viewer", "editor"):
            with self.subTest(role=role):
                self.grant_group(gid, role=role)
                self.grant_source("source-a", TARGET)
                status, detail = self.request("GET", f"/groups/{gid}", sub=TARGET)
                self.assertEqual(status, 200, detail)
                self.ddb.remove(PLATFORM, pk=f"USER#{TARGET}", sk="REPO#source-a")
                self.expect_unchanged_error("GET", f"/groups/{gid}", None, 403, sub=TARGET)
                status, listing = self.request("GET", "/groups", sub=TARGET)
                self.assertEqual(status, 200, listing)
                self.assertEqual(listing["groups"], [])
                groups = self.api.list_for_user({**self.ident, "sub": TARGET}, **self.dependencies)
                self.assertEqual(groups, [])

    def test_owner_without_group_grant_cannot_read_or_list_recovery_metadata(self):
        self.seed_source("source-a", scope="private")
        self.grant_source("source-a")
        gid = self.create()["group_id"]
        self.ddb.remove(PLATFORM, pk=f"USER#{OWNER}", sk="REPO#source-a")
        self.ddb.remove(PLATFORM, pk=f"USER#{OWNER}", sk=f"GROUP#{gid}")
        self.assertEqual(self.meta(gid)["owner_sub"], OWNER)
        self.assertIsNotNone(self.ddb.row(PLATFORM, pk=f"GROUP#{gid}", sk=f"MEMBER#{OWNER}"))
        self.expect_unchanged_error("GET", f"/groups/{gid}", None, 403)
        status, listing = self.request("GET", "/groups")
        self.assertEqual(status, 200, listing)
        self.assertEqual(listing["groups"], [])
        self.assertEqual(self.api.list_for_user(self.ident, **self.dependencies), [])

    def test_viewer_editor_and_owner_permissions_remain_distinct(self):
        gid = self.create()["group_id"]
        self.grant_group(gid, role="viewer")
        status, result = self.request("GET", f"/groups/{gid}", sub=TARGET)
        self.assertEqual(status, 200, result)
        for method, path, body in (
            ("POST", f"/groups/{gid}", {"expected_revision": 1, "name": "Viewer edit"}),
            ("POST", f"/groups/{gid}/rebuild", {"expected_revision": 1, "idempotency_key": "build-token-01"}),
            ("GET", f"/groups/{gid}/members", None),
        ):
            self.expect_unchanged_error(method, path, body, 403, sub=TARGET)
        self.grant_group(gid, role="editor")
        for method, path, body in (
            ("GET", f"/groups/{gid}/members", None),
            ("POST", f"/groups/{gid}/members", {"expected_revision": 1, "email": "target@example.test"}),
            ("DELETE", f"/groups/{gid}", {"expected_revision": 1}),
        ):
            self.expect_unchanged_error(method, path, body, 403, sub=TARGET)
        status, result = self.request("POST", f"/groups/{gid}", {
            "expected_revision": 1, "name": "Editor change",
        }, sub=TARGET)
        self.assertEqual(status, 200, result)
        self.assertEqual(self.meta(gid)["name"], "Editor change")

    def test_deleted_account_cannot_use_owner_recovery(self):
        gid = self.create()["group_id"]
        self.ddb.seed(PLATFORM, {"pk": f"USER#{OWNER}", "sk": "DELETED"})
        for method, body in (("GET", None), ("POST", {"expected_revision": 1}), ("DELETE", {"expected_revision": 1})):
            self.expect_unchanged_error(method, f"/groups/{gid}", body, 403)

    def test_invite_and_remove_commit_grant_and_member_mirror_without_touching_sources(self):
        self.seed_source("source-a", scope="private")
        self.grant_source("source-a")
        gid = self.create()["group_id"]
        invitation = {"expected_revision": 1, "email": "target@example.test", "role": "editor"}
        self.expect_unchanged_error("POST", f"/groups/{gid}/members", invitation, 403)
        self.grant_source("source-a", TARGET)
        before_registry = copy.deepcopy(self.ddb.tables[REGISTRY])
        source_grants = {
            key: copy.deepcopy(row) for key, row in self.ddb.tables[PLATFORM].items()
            if key[1].startswith("REPO#")
        }
        status, result = self.request("POST", f"/groups/{gid}/members", invitation)
        self.assertEqual(status, 201, result)
        self.assertEqual(result["revision"], 2)
        self.assertEqual(self.meta(gid)["revision"], 2)
        grant = self.ddb.row(PLATFORM, pk=f"USER#{TARGET}", sk=f"GROUP#{gid}")
        mirror = self.ddb.row(PLATFORM, pk=f"GROUP#{gid}", sk=f"MEMBER#{TARGET}")
        self.assertEqual(grant["role"], "editor")
        self.assertEqual(grant["invited_email"], "target@example.test")
        self.assertEqual(mirror["role"], "editor")
        self.assertEqual(mirror["sub"], TARGET)
        self.assertEqual(self.cognito.calls[-1], {
            "UserPoolId": "offline-user-pool", "Filter": 'email = "target@example.test"', "Limit": 2,
        })
        status, result = self.request("POST", f"/groups/{gid}/members", {
            **invitation, "expected_revision": 2, "role": "viewer",
        })
        self.assertEqual(status, 201, result)
        self.assertEqual(result["revision"], 3)
        self.assertEqual(self.ddb.row(PLATFORM, pk=f"USER#{TARGET}", sk=f"GROUP#{gid}")["role"], "viewer")
        self.assertEqual(self.ddb.row(PLATFORM, pk=f"GROUP#{gid}", sk=f"MEMBER#{TARGET}")["role"], "viewer")
        status, result = self.request("DELETE", f"/groups/{gid}/members/{TARGET}", {"expected_revision": 3})
        self.assertEqual(status, 200, result)
        self.assertEqual(result["revision"], 4)
        self.assertIsNone(self.ddb.row(PLATFORM, pk=f"USER#{TARGET}", sk=f"GROUP#{gid}"))
        self.assertIsNone(self.ddb.row(PLATFORM, pk=f"GROUP#{gid}", sk=f"MEMBER#{TARGET}"))
        self.assertEqual(self.ddb.tables[REGISTRY], before_registry)
        self.assertEqual({
            key: row for key, row in self.ddb.tables[PLATFORM].items()
            if key[1].startswith("REPO#")
        }, source_grants)

    def test_invitation_requires_exact_unique_enabled_verified_cognito_user_with_sub(self):
        gid = self.create()["group_id"]
        missing_sub = cognito_user()
        missing_sub["Attributes"] = [attr for attr in missing_sub["Attributes"] if attr["Name"] != "sub"]
        missing_enabled = cognito_user()
        del missing_enabled["Enabled"]
        for users in (
            [], [cognito_user(), cognito_user(sub="other-sub")],
            [cognito_user(enabled=False)], [missing_enabled],
            [cognito_user(verified="false")], [cognito_user(verified="")],
            [cognito_user(email="other@example.test")], [missing_sub], [cognito_user(sub="")],
        ):
            with self.subTest(users=users):
                self.cognito.users = users
                self.expect_unchanged_error("POST", f"/groups/{gid}/members", {
                    "expected_revision": 1, "email": "target@example.test",
                }, 404)

    def test_invitation_rejects_unsafe_email_roles_and_owner_changes(self):
        gid = self.create()["group_id"]
        for fields in (
            {"email": 'target"@example.test'}, {"email": "target\\@example.test"},
            {"email": "target\n@example.test"}, {"email": "not-an-email"},
            {"email": "target@example.test", "role": "owner"},
            {"email": "target@example.test", "role": "administrator"},
        ):
            with self.subTest(fields=fields):
                self.expect_unchanged_error(
                    "POST", f"/groups/{gid}/members", {"expected_revision": 1, **fields}, 400,
                )
        self.assertEqual(self.cognito.calls, [])
        self.cognito.users = [cognito_user(sub=OWNER)]
        self.expect_unchanged_error("POST", f"/groups/{gid}/members", {
            "expected_revision": 1, "email": "target@example.test",
        }, 400)
        self.expect_unchanged_error(
            "DELETE", f"/groups/{gid}/members/{OWNER}", {"expected_revision": 1}, 400,
        )

    def test_deleted_invitee_is_not_granted_group_access(self):
        gid = self.create()["group_id"]
        self.ddb.seed(PLATFORM, {"pk": f"USER#{TARGET}", "sk": "DELETED"})
        self.expect_unchanged_error("POST", f"/groups/{gid}/members", {
            "expected_revision": 1, "email": "target@example.test",
        }, 403)

    def test_target_source_revocation_during_invite_rolls_back_both_membership_rows(self):
        self.seed_source("source-a", scope="private")
        self.grant_source("source-a")
        self.grant_source("source-a", TARGET)
        gid = self.create()["group_id"]
        original_meta = self.meta(gid)

        def revoke(ddb):
            ddb.remove(PLATFORM, pk=f"USER#{TARGET}", sk="REPO#source-a")

        self.ddb.before_transaction = revoke
        status, result = self.request("POST", f"/groups/{gid}/members", {
            "expected_revision": 1, "email": "target@example.test",
        })
        self.assertEqual(status, 409, result)
        self.assertEqual(self.meta(gid), original_meta)
        self.assertIsNone(self.ddb.row(PLATFORM, pk=f"USER#{TARGET}", sk=f"GROUP#{gid}"))
        self.assertIsNone(self.ddb.row(PLATFORM, pk=f"GROUP#{gid}", sk=f"MEMBER#{TARGET}"))

    def test_member_listing_uses_strong_grants_instead_of_mirror_roles(self):
        gid = self.create()["group_id"]
        status, result = self.request("POST", f"/groups/{gid}/members", {
            "expected_revision": 1, "email": "target@example.test", "role": "viewer",
        })
        self.assertEqual(status, 201, result)
        self.ddb.seed(PLATFORM, {
            "pk": f"GROUP#{gid}", "sk": f"MEMBER#{TARGET}", "sub": TARGET, "role": "owner",
        })
        self.ddb.seed(PLATFORM, {
            "pk": f"GROUP#{gid}", "sk": "MEMBER#removed-sub", "sub": "removed-sub", "role": "editor",
        })
        self.ddb.page_size = 1
        status, result = self.request("GET", f"/groups/{gid}/members")
        self.assertEqual(status, 200, result)
        self.assertEqual({member["sub"]: member["role"] for member in result["members"]}, {
            OWNER: "owner", TARGET: "viewer",
        })
        for member in result["members"]:
            self.assertLessEqual(set(member), {"sub", "role", "you", "email"})
        self.assertEqual(result["revision"], 2)

    def rebuild(self, gid, *, revision=1, token="build-token-01"):
        return self.request("POST", f"/groups/{gid}/rebuild", {
            "expected_revision": revision, "idempotency_key": token,
        })

    def test_rebuild_requires_safe_idempotency_key_before_reserving_or_heading_artifacts(self):
        gid = self.create()["group_id"]
        for token in (None, "", "short", "x" * 129, "../build-token", "token\n1234"):
            with self.subTest(token=token):
                self.expect_unchanged_error("POST", f"/groups/{gid}/rebuild", {
                    "expected_revision": 1, "idempotency_key": token,
                }, 400)
        self.assertEqual(self.s3.heads, [])

    def test_rebuild_pins_versions_and_configuration_then_dispatches_once(self):
        self.seed_source("source-b", version="version-2")
        sources = [
            {"source_id": "source-a", "role": "frontend", "description": "HTTP caller"},
            {"source_id": "source-b", "role": "backend", "description": "HTTP service"},
        ]
        gid = self.create(sources=sources)["group_id"]
        during_dispatch = []

        def observe(request):
            event = json.loads(request["Payload"])
            during_dispatch.append({
                "ledger": self.ledger(gid, event["build_id"]), "meta": self.meta(gid),
            })

        self.worker.on_invoke = observe
        status, result = self.rebuild(gid)
        self.assertEqual(status, 202, result)
        self.assertFalse(result["replayed"])
        bid = result["build_id"]
        self.assertEqual(result["revision"], 1)
        ledger = self.ledger(gid, bid)
        self.assertEqual(ledger["source_versions"], {"source-a": "version-1", "source-b": "version-2"})
        self.assertEqual(ledger["source_description_versions"], {"source-a": 3, "source-b": 3})
        self.assertEqual(ledger["sources"], sources)
        self.assertEqual(ledger["requested_by"], OWNER)
        self.assertEqual(ledger["description"], "Frontend and API contracts")
        self.assertEqual(during_dispatch[0]["ledger"]["status"], "QUEUED")
        self.assertFalse(during_dispatch[0]["ledger"].get("dispatched", False))
        self.assertEqual(during_dispatch[0]["meta"]["status"], "BUILDING")
        self.assertEqual(during_dispatch[0]["meta"]["build_id"], bid)
        self.assertIs(ledger["dispatched"], True)
        self.assertEqual(len(self.worker.calls), 1)
        invocation = self.worker.calls[0]
        self.assertEqual(invocation["FunctionName"], "offline-group-worker")
        self.assertEqual(invocation["InvocationType"], "Event")
        self.assertEqual(json.loads(invocation["Payload"]), {
            "group_id": gid, "build_id": bid, "revision": 1, "requested_by": OWNER,
        })
        self.assertEqual(
            {(request["Bucket"], request["Key"]) for request in self.s3.heads},
            {(BUCKET, f"source-versions/{sid}/{version}/{artifact}")
             for sid, version in (("source-a", "version-1"), ("source-b", "version-2"))
             for artifact in ("manifest.json", "graph.json", "src.tar.gz")},
        )
        before = copy.deepcopy(self.ddb.tables)
        heads = copy.deepcopy(self.s3.heads)
        status, replay = self.rebuild(gid)
        self.assertEqual(status, 202, replay)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["build_id"], bid)
        self.assertEqual(len(self.worker.calls), 1)
        self.assertEqual(self.ddb.tables, before)
        self.assertEqual(self.s3.heads, heads)

    def test_accepted_invoke_with_failed_dispatch_journal_returns_202_and_never_reinvokes(self):
        gid = self.create()["group_id"]
        with mock.patch.object(
            self.ddb, "update_item", side_effect=aws_error("InternalServerError", "UpdateItem"),
        ) as journal:
            status, result = self.rebuild(gid)
        self.assertEqual(status, 202, result)
        self.assertFalse(result["replayed"])
        journal.assert_called_once()
        bid = result["build_id"]
        ledger = self.ledger(gid, bid)
        self.assertEqual(ledger["status"], "QUEUED")
        self.assertFalse(ledger.get("dispatched", False))
        self.assertFalse(ledger.get("dispatch_failed", False))
        self.assertEqual(self.meta(gid)["status"], "BUILDING")
        self.assertEqual(len(self.worker.calls), 1)
        before = copy.deepcopy(self.ddb.tables)
        status, replay = self.rebuild(gid)
        self.assertEqual(status, 202, replay)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["build_id"], bid)
        self.assertEqual(replay["status"], "QUEUED")
        self.assertEqual(len(self.worker.calls), 1)
        self.assertEqual(self.ddb.tables, before)

    def test_dispatch_journal_preserves_worker_completion_during_invoke_and_replay_returns_ready(self):
        gid = self.create()["group_id"]
        completed = []

        def finish_during_invoke(request):
            event = json.loads(request["Payload"])
            ledger = {
                **self.ledger(gid, event["build_id"]),
                "status": "READY", "completed_at": NOW, "usage": {"relations": 2},
            }
            self.ddb.seed(PLATFORM, ledger)
            completed.append(ledger)

        self.worker.on_invoke = finish_during_invoke
        status, result = self.rebuild(gid)
        self.assertEqual(status, 202, result)
        bid = result["build_id"]
        self.assertEqual(self.ledger(gid, bid), {
            **completed[0], "dispatched": True, "dispatched_at": NOW,
        })
        self.assertEqual(len(self.worker.calls), 1)
        before = copy.deepcopy(self.ddb.tables)
        status, replay = self.rebuild(gid)
        self.assertEqual(status, 202, replay)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["build_id"], bid)
        self.assertEqual(replay["status"], "READY")
        self.assertEqual(len(self.worker.calls), 1)
        self.assertEqual(self.ddb.tables, before)

    def test_missing_or_unsafe_source_version_never_creates_a_build(self):
        gid = self.create()["group_id"]
        for version in (None, "", "../latest", "version/escape", "version\n1"):
            with self.subTest(version=version):
                source = self.ddb.row(REGISTRY, repo_id="source-a")
                self.ddb.seed(REGISTRY, {**source, "active_source_version": version})
                self.expect_unchanged_error("POST", f"/groups/{gid}/rebuild", {
                    "expected_revision": 1, "idempotency_key": "build-token-01",
                }, 409)
        source = self.ddb.row(REGISTRY, repo_id="source-a")
        source.pop("active_source_version")
        self.ddb.seed(REGISTRY, source)
        self.expect_unchanged_error("POST", f"/groups/{gid}/rebuild", {
            "expected_revision": 1, "idempotency_key": "build-token-01",
        }, 409)
        self.assertEqual(self.s3.heads, [])

    def test_each_pinned_artifact_is_required_before_reserving_a_build(self):
        gid = self.create()["group_id"]
        for artifact in ("manifest.json", "graph.json", "src.tar.gz"):
            with self.subTest(artifact=artifact):
                missing = (BUCKET, f"source-versions/source-a/version-1/{artifact}")
                self.s3.objects.remove(missing)
                try:
                    self.expect_unchanged_error("POST", f"/groups/{gid}/rebuild", {
                        "expected_revision": 1, "idempotency_key": "build-token-01",
                    }, 409)
                finally:
                    self.s3.objects.add(missing)

    def test_artifact_service_failure_does_not_reserve_a_build_or_leak_provider_error(self):
        gid = self.create()["group_id"]
        with mock.patch.object(self.s3, "head_object", side_effect=aws_error("AccessDenied", "HeadObject")):
            result = self.expect_unchanged_error("POST", f"/groups/{gid}/rebuild", {
                "expected_revision": 1, "idempotency_key": "build-token-01",
            }, 503)
        self.assertNotIn("AccessDenied", json.dumps(result))

    def test_source_version_or_description_change_during_reservation_rolls_back_build(self):
        gid = self.create()["group_id"]
        for field, replacement in (("active_source_version", "version-2"), ("description_version", 4)):
            with self.subTest(field=field):
                original_meta = self.meta(gid)

                def change_source(ddb):
                    ddb.seed(REGISTRY, {**ddb.row(REGISTRY, repo_id="source-a"), field: replacement})

                self.ddb.before_transaction = change_source
                status, result = self.rebuild(gid)
                self.assertEqual(status, 409, result)
                self.assertEqual(self.ddb.row(REGISTRY, repo_id="source-a")[field], replacement)
                self.assertEqual(self.meta(gid), original_meta)
                self.assertFalse(any(key[1].startswith("BUILD#") for key in self.ddb.tables[PLATFORM]))
                self.assertEqual(self.worker.calls, [])
                self.seed_source("source-a")

    def test_dispatch_failure_retains_failed_ledger_and_replays_without_second_invoke(self):
        gid = self.create()["group_id"]
        self.worker.failure = aws_error("ServiceUnavailableException", "Invoke")
        status, failure = self.rebuild(gid)
        self.assertEqual(status, 503, failure)
        bid = failure["build_id"]
        ledger = self.ledger(gid, bid)
        self.assertEqual(ledger["status"], "FAILED")
        self.assertIs(ledger["dispatch_failed"], True)
        self.assertFalse(ledger.get("dispatched", False))
        self.assertEqual(self.meta(gid)["status"], "FAILED")
        self.assertEqual(self.meta(gid)["build_started_at"], NOW)
        self.assertNotIn("ServiceUnavailableException", json.dumps(failure))
        self.assertEqual(len(self.worker.calls), 1)
        before = copy.deepcopy(self.ddb.tables)
        status, replay = self.rebuild(gid)
        self.assertEqual(status, 503, replay)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["build_id"], bid)
        self.assertEqual(self.ddb.tables, before)
        self.assertEqual(len(self.worker.calls), 1)
        self.now += 899
        self.expect_unchanged_error("POST", f"/groups/{gid}/rebuild", {
            "expected_revision": 1, "idempotency_key": "different-build-token",
        }, 409)
        self.now += 1
        self.worker.failure = None
        status, next_build = self.rebuild(gid, token="different-build-token")
        self.assertEqual(status, 202, next_build)
        self.assertNotEqual(next_build["build_id"], bid)
        self.assertEqual(len(self.worker.calls), 2)
        self.assertEqual(self.ledger(gid, bid), ledger)

    def test_unaccepted_async_invoke_is_retained_as_failed_without_reinvocation(self):
        gid = self.create()["group_id"]
        self.worker.status_code = 200
        status, result = self.rebuild(gid)
        self.assertEqual(status, 503, result)
        ledger = self.ledger(gid, result["build_id"])
        self.assertEqual(ledger["status"], "FAILED")
        self.assertTrue(ledger["dispatch_failed"])
        self.worker.status_code = 202
        status, replay = self.rebuild(gid)
        self.assertEqual(status, 503, replay)
        self.assertTrue(replay["replayed"])
        self.assertEqual(len(self.worker.calls), 1)

    def test_queued_ledger_still_blocks_reinvocation_if_failure_recording_also_fails(self):
        gid = self.create()["group_id"]
        self.worker.failure = TimeoutError("Invoke acknowledgement lost")
        with mock.patch.object(self.ddb, "update_item", side_effect=aws_error("InternalServerError", "UpdateItem")):
            status, failure = self.rebuild(gid)
        self.assertEqual(status, 503, failure)
        self.assertEqual(self.ledger(gid, failure["build_id"])["status"], "QUEUED")
        self.worker.failure = None
        status, replay = self.rebuild(gid)
        self.assertEqual(status, 202, replay)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["build_id"], failure["build_id"])
        self.assertEqual(len(self.worker.calls), 1)
        self.expect_unchanged_error("POST", f"/groups/{gid}/rebuild", {
            "expected_revision": 1, "idempotency_key": "second-build-token",
        }, 409)

    def test_build_lease_survives_configuration_edit_and_is_independent_of_status(self):
        gid = self.create()["group_id"]
        status, first = self.rebuild(gid)
        self.assertEqual(status, 202, first)
        status, edit = self.request("POST", f"/groups/{gid}", {
            "expected_revision": 1, "description": "Changed during running build",
        })
        self.assertEqual(status, 200, edit)
        self.assertEqual(self.meta(gid)["revision"], 2)
        self.assertEqual(self.meta(gid)["build_started_at"], NOW)
        self.now = NOW + 899
        for state in ("DRAFT", "STALE", "READY", "FAILED", "BUILDING"):
            with self.subTest(status=state):
                self.ddb.seed(PLATFORM, {**self.meta(gid), "status": state})
                self.expect_unchanged_error("POST", f"/groups/{gid}/rebuild", {
                    "expected_revision": 2, "idempotency_key": "second-build-token",
                }, 409)
        self.now = NOW + 900
        status, second = self.rebuild(gid, revision=2, token="second-build-token")
        self.assertEqual(status, 202, second)
        self.assertNotEqual(second["build_id"], first["build_id"])
        self.assertEqual(second["revision"], 2)
        self.assertEqual(len(self.worker.calls), 2)

    def test_expired_build_clears_old_worker_lease_without_changing_prior_ledger(self):
        gid = self.create()["group_id"]
        status, first = self.rebuild(gid)
        self.assertEqual(status, 202, first)
        self.ddb.seed(PLATFORM, {
            **self.meta(gid), "worker_token": "old-execution-token",
            "worker_lease_expires_at": NOW + 1200,
        })
        self.ddb.seed(PLATFORM, {
            **self.ledger(gid, first["build_id"]), "worker_token": "old-execution-token",
            "worker_lease_expires_at": NOW + 1200,
        })
        prior_ledger = self.ledger(gid, first["build_id"])
        self.now = NOW + 900
        status, second = self.rebuild(gid, token="replacement-build-token")
        self.assertEqual(status, 202, second)
        self.assertNotEqual(second["build_id"], first["build_id"])
        updated = self.meta(gid)
        self.assertEqual(updated["build_id"], second["build_id"])
        self.assertNotIn("worker_token", updated)
        self.assertNotIn("worker_lease_expires_at", updated)
        self.assertEqual(self.ledger(gid, first["build_id"]), prior_ledger)
        self.assertEqual(len(self.worker.calls), 2)

    def test_old_build_replay_does_not_dispatch_after_edit_or_lease_expiry(self):
        gid = self.create()["group_id"]
        status, first = self.rebuild(gid)
        self.assertEqual(status, 202, first)
        status, result = self.request("POST", f"/groups/{gid}", {
            "expected_revision": 1, "name": "New revision",
        })
        self.assertEqual(status, 200, result)
        self.expect_unchanged_error("POST", f"/groups/{gid}/rebuild", {
            "expected_revision": 2, "idempotency_key": "build-token-01",
        }, 409)
        self.now = NOW + 901
        status, second = self.rebuild(gid, revision=2, token="second-build-token")
        self.assertEqual(status, 202, second)
        before = copy.deepcopy(self.ddb.tables)
        status, replay = self.rebuild(gid)
        self.assertEqual(status, 202, replay)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["build_id"], first["build_id"])
        self.assertEqual(replay["revision"], 1)
        self.assertEqual(len(self.worker.calls), 2)
        self.assertEqual(self.ddb.tables, before)
        self.assertEqual(self.meta(gid)["build_id"], second["build_id"])

    def test_concurrent_build_reservation_is_blocked_inside_transaction(self):
        gid = self.create()["group_id"]

        def reserve(ddb):
            ddb.seed(PLATFORM, {
                **self.meta(gid), "build_id": "concurrent-worker", "build_started_at": NOW,
                "status": "BUILDING",
            })

        self.ddb.before_transaction = reserve
        status, result = self.rebuild(gid)
        self.assertEqual(status, 409, result)
        self.assertEqual(self.meta(gid)["build_id"], "concurrent-worker")
        self.assertEqual(self.worker.calls, [])
        self.assertFalse(any(key[1].startswith("BUILD#") for key in self.ddb.tables[PLATFORM]))

    def test_simultaneous_same_token_rebuild_replays_winner_without_invoking_twice(self):
        gid = self.create()["group_id"]
        committed = []
        self.ddb.before_transaction = lambda _: committed.append(self.rebuild(gid))
        status, replay = self.rebuild(gid)
        self.assertEqual(committed[0][0], 202, committed)
        self.assertEqual(status, 202, replay)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["build_id"], committed[0][1]["build_id"])
        self.assertEqual(len(self.worker.calls), 1)
        self.assertEqual(sum(key[1].startswith("BUILD#") for key in self.ddb.tables[PLATFORM]), 1)

    def test_metadata_stays_visible_but_not_data_ready_after_source_or_description_changes(self):
        gid = self.create()["group_id"]
        self.ddb.seed(PLATFORM, {
            **self.meta(gid), "status": "READY", "active_revision": 1,
            "active_version": "group-version-1", "active_source_versions": {"source-a": "version-1"},
            "active_source_descriptions": {"source-a": 3},
        })
        status, result = self.request("GET", f"/groups/{gid}")
        self.assertEqual(status, 200, result)
        self.assertTrue(result["group"]["data_ready"])
        for changes in ({"active_source_version": "version-2"}, {"description_version": 4}):
            self.seed_source("source-a")
            self.ddb.seed(REGISTRY, {**self.ddb.row(REGISTRY, repo_id="source-a"), **changes})
            for path in (f"/groups/{gid}", "/groups"):
                with self.subTest(changes=changes, path=path):
                    status, result = self.request("GET", path)
                    self.assertEqual(status, 200, result)
                    groups = result["groups"] if path == "/groups" else [result["group"]]
                    self.assertEqual(len(groups), 1)
                    self.assertEqual(groups[0]["group_id"], gid)
                    self.assertFalse(groups[0]["data_ready"])
                    self.assertTrue(groups[0]["stale"])
        self.assertEqual(self.meta(gid)["active_version"], "group-version-1")


if __name__ == "__main__":
    unittest.main()
