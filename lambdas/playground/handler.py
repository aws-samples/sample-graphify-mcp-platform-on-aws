"""Playground backend: Bedrock Claude (Anthropic SDK) + MCP bridge.

Two JWT-protected routes on the management HTTP API:

  POST /playground/mcp   — forwards ONE MCP JSON-RPC request (tools/list,
                           tools/call, initialize, ping) to an MCP server the
                           signed-in user may reach. The console uses this
                           both to render the tool panel and as a direct
                           tool-invocation tester.

  POST /playground/chat  — ONE Claude-on-Bedrock model call (AnthropicBedrock
                           from the anthropic SDK) with the MCP tools exposed
                           as Anthropic tools. If Claude requests tool use,
                           this Lambda executes the calls against the SAME
                           server and returns ready-made tool_result blocks;
                           the browser appends them and re-POSTs, driving the
                           agentic loop client-side so no single request
                           outlives API Gateway's 30s cap.

Access is the console identity, not an API key: the caller's Cognito `sub`
must hold a grant on the server (owner, subscriber or member — the same rows
that populate the MCP Servers tab); the hub ("all") serves only the merged
public graph and is open to every signed-in user. MCP traffic then goes to
the in-VPC proxy Lambda, invoked directly with a synthesized authorizer
context (kid "playground") — the same path the console's source viewer uses.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "vendor"))

import boto3  # noqa: E402  (Lambda-provided)
from anthropic import AnthropicBedrock  # noqa: E402  (vendored)

REGION = os.environ["AWS_REGION"]
MCP_PROXY_FN = os.environ["MCP_PROXY_FN"]
REGISTRY_TABLE = os.environ["REGISTRY_TABLE"]
ALLOWED_MODELS = [m.strip() for m in os.environ["ALLOWED_MODELS"].split(",") if m.strip()]
DEFAULT_MODEL = os.environ.get("DEFAULT_MODEL", ALLOWED_MODELS[0] if ALLOWED_MODELS else "")
PLATFORM_TABLE = os.environ["PLATFORM_TABLE"]
# Per-user, per-day Bedrock token ceiling (input+output). Bounds what any one
# console account can spend; the invite-only pool bounds who has an account.
DAILY_TOKEN_BUDGET = int(os.environ.get("DAILY_TOKEN_BUDGET", "20000000"))

_ddb = boto3.client("dynamodb", region_name=REGION)
_lambda_clients: dict[int, object] = {}


def _lambda(timeout: float):
    """Lambda client whose read timeout matches the caller's remaining budget
    (the proxy itself caps the upstream call at ~26s)."""
    from botocore.config import Config

    key = max(2, int(timeout) + 1)
    if key not in _lambda_clients:
        _lambda_clients[key] = boto3.client(
            "lambda", region_name=REGION,
            config=Config(read_timeout=key, connect_timeout=3, retries={"max_attempts": 0}),
        )
    return _lambda_clients[key]

# One request = ONE model attempt. max_retries=0 because a retry after a slow
# first attempt cannot fit the 28s Lambda budget anyway — it would only turn a
# structured 502 into an opaque gateway timeout and bill a second generation.
_anthropic = AnthropicBedrock(aws_region=REGION, max_retries=0, timeout=24.0)

SERVER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,127}$")
TOOL_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")

MCP_METHODS = {"initialize", "ping", "tools/list", "tools/call"}
MAX_TOOLS = 48
MAX_MESSAGES = 60
MAX_TOKENS_CAP = 4096
MAX_SYSTEM_CHARS = 4000
MAX_TOOL_RESULT_CHARS = 16_000  # keeps a huge graph dump from blowing the context
# Must comfortably hold a full 8-round tool loop: 8 rounds x ~4 calls x 16k
# chars of results plus overhead — the model's context window is the real cap.
MAX_BODY_CHARS = 1_000_000


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _resp(status: int, body) -> dict:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, ensure_ascii=False),
    }


def _claims(event: dict) -> dict:
    claims = ((event.get("requestContext") or {}).get("authorizer") or {}).get("jwt", {}).get("claims", {})
    if not claims.get("sub"):
        raise ApiError(401, "no identity in request context")
    return claims


def _body(event: dict) -> dict:
    raw = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        import base64 as _b64

        raw = _b64.b64decode(raw).decode()
    if len(raw) > MAX_BODY_CHARS:
        raise ApiError(413, f"request body too large (> {MAX_BODY_CHARS} chars)")
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError
        return parsed
    except ValueError:
        raise ApiError(400, "request body must be a JSON object") from None


def _require_server(body: dict, sub: str) -> str:
    """Validate server_id and check the signed-in user may reach it."""
    server_id = str(body.get("server_id", "")).strip()
    if not SERVER_ID_RE.fullmatch(server_id):
        raise ApiError(400, "server_id is invalid")
    if server_id == "all":
        return server_id  # hub: merged PUBLIC graph, open to every console user
    reg = _ddb.get_item(TableName=REGISTRY_TABLE, Key={"repo_id": {"S": server_id}}).get("Item")
    if not reg or reg.get("enabled", {}).get("S") != "1":
        raise ApiError(404, f"unknown MCP server '{server_id}' (not registered or disabled)")
    grant = _ddb.get_item(
        TableName=PLATFORM_TABLE,
        Key={"pk": {"S": f"USER#{sub}"}, "sk": {"S": f"REPO#{server_id}"}},
    ).get("Item")
    if not grant:
        raise ApiError(403, f"you have no access to '{server_id}' — subscribe to it in the catalog or ask its owner to add you")
    return server_id


def _mcp_call(server_id: str, sub: str, payload: dict, timeout: float) -> tuple[int, dict | None, str]:
    """Run one JSON-RPC message through the in-VPC MCP proxy Lambda (direct
    invoke with a synthesized authorizer context). Returns
    (http_status, parsed_json_or_None, raw_text_snippet)."""
    proxy_event = {
        "pathParameters": {"serverId": server_id},
        "requestContext": {"authorizer": {"kid": "playground", "ownerSub": sub, "scopeServerIds": server_id}},
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(payload),
        "isBase64Encoded": False,
    }
    try:
        res = _lambda(timeout).invoke(FunctionName=MCP_PROXY_FN, Payload=json.dumps(proxy_event).encode())
        out = json.loads(res["Payload"].read() or b"{}")
    except Exception as exc:  # timeout, throttled invoke
        return 0, None, f"{type(exc).__name__}: {exc}"
    if not isinstance(out, dict) or "statusCode" not in out:
        return 0, None, "bad proxy response"
    status = int(out.get("statusCode") or 500)
    text = out.get("body") or ""
    try:
        return status, json.loads(text), text[:2000]
    except json.JSONDecodeError:
        return status, None, text[:2000]


def _mcp_status_hint(status: int) -> str:
    return {
        0: "MCP proxy unreachable",
        403: "forbidden (403) — this server is not scoped for the playground session",
        404: "server not found (404)",
        429: "throttled or quota exceeded (429)",
        502: "MCP server unavailable (502) — its task may be starting",
        504: "MCP server timed out (504)",
    }.get(status, f"MCP proxy returned HTTP {status}")


# ---------------------------------------------------------------------------
# POST /playground/mcp — single JSON-RPC passthrough (tool panel + tester)
# ---------------------------------------------------------------------------

def playground_mcp(event, sub: str) -> dict:
    body = _body(event)
    server_id = _require_server(body, sub)
    payload = body.get("payload")
    if not isinstance(payload, dict) or payload.get("method") not in MCP_METHODS:
        raise ApiError(400, f"payload.method must be one of {sorted(MCP_METHODS)}")
    payload.setdefault("jsonrpc", "2.0")
    payload.setdefault("id", 1)
    status, parsed, raw = _mcp_call(server_id, sub, payload, timeout=20)
    if status != 200:
        return _resp(200, {"ok": False, "status": status, "hint": _mcp_status_hint(status), "raw": raw})
    return _resp(200, {"ok": True, "status": 200, "body": parsed if parsed is not None else raw})


# ---------------------------------------------------------------------------
# POST /playground/chat — one model call (+ tool execution) per request
# ---------------------------------------------------------------------------

def _anthropic_tools(raw_tools) -> list[dict]:
    """MCP tools/list entries -> Anthropic tool specs."""
    if not isinstance(raw_tools, list):
        return []
    out, seen = [], set()
    for t in raw_tools[:MAX_TOOLS]:
        if not isinstance(t, dict):
            continue
        name = str(t.get("name", ""))
        if not TOOL_NAME_RE.fullmatch(name) or name in seen:
            continue
        seen.add(name)
        schema = t.get("inputSchema") or t.get("input_schema") or {}
        if not isinstance(schema, dict) or schema.get("type") != "object":
            schema = {"type": "object"}
        out.append({
            "name": name,
            "description": str(t.get("description", ""))[:1500],
            "input_schema": schema,
        })
    return out


def _sanitize_messages(raw) -> list[dict]:
    if not isinstance(raw, list) or not raw:
        raise ApiError(400, "messages must be a non-empty list")
    if len(raw) > MAX_MESSAGES:
        raise ApiError(400, f"conversation too long (> {MAX_MESSAGES} messages) — start a new one")
    msgs = []
    for m in raw:
        if not isinstance(m, dict) or m.get("role") not in ("user", "assistant"):
            raise ApiError(400, "each message needs role user|assistant")
        content = m.get("content")
        if not isinstance(content, (str, list)):
            raise ApiError(400, "message content must be a string or a block list")
        msgs.append({"role": m["role"], "content": content})
    return msgs


def _tool_result_text(parsed: dict | None, raw: str) -> tuple[str, bool]:
    """Extract text from an MCP tools/call response. Returns (text, is_error)."""
    if parsed is None:
        return raw or "(empty response)", True
    if "error" in parsed:
        err = parsed["error"]
        return f"MCP error {err.get('code')}: {err.get('message')}", True
    result = parsed.get("result") or {}
    parts = [c.get("text", "") for c in result.get("content", []) if isinstance(c, dict) and c.get("type") == "text"]
    text = "\n".join(p for p in parts if p) or json.dumps(result, ensure_ascii=False)[:4000]
    if len(text) > MAX_TOOL_RESULT_CHARS:
        text = text[:MAX_TOOL_RESULT_CHARS] + f"\n…[truncated at {MAX_TOOL_RESULT_CHARS} chars]"
    return text, bool(result.get("isError"))


def _budget_check(sub: str) -> tuple[str, int]:
    """Raise 429 when the caller's daily playground token budget is spent."""
    day = time.strftime("%Y-%m-%d", time.gmtime())
    key = {"pk": {"S": f"USAGE#PLAYGROUND#{sub}"}, "sk": {"S": f"D#{day}"}}
    item = _ddb.get_item(TableName=PLATFORM_TABLE, Key=key).get("Item") or {}
    used = int(item.get("tokens", {}).get("N", "0"))
    if used >= DAILY_TOKEN_BUDGET:
        raise ApiError(429, f"playground daily token budget exhausted ({DAILY_TOKEN_BUDGET} tokens/day) — resets at 00:00 UTC")
    return day, used


def _budget_record(sub: str, day: str, tokens: int) -> None:
    try:
        _ddb.update_item(
            TableName=PLATFORM_TABLE,
            Key={"pk": {"S": f"USAGE#PLAYGROUND#{sub}"}, "sk": {"S": f"D#{day}"}},
            UpdateExpression="ADD tokens :n, req :one SET #ttl = :ttl",
            ExpressionAttributeNames={"#ttl": "ttl"},
            ExpressionAttributeValues={
                ":n": {"N": str(tokens)},
                ":one": {"N": "1"},
                ":ttl": {"N": str(int(time.time()) + 90 * 86400)},
            },
        )
    except Exception as exc:  # metering must never fail the user's turn
        print(f"budget_record failed: {type(exc).__name__}: {exc}")


def playground_chat(event, ctx, sub: str) -> dict:
    body = _body(event)
    server_id = _require_server(body, sub)
    day, _used = _budget_check(sub)

    model = str(body.get("model") or DEFAULT_MODEL)
    if model not in ALLOWED_MODELS:
        raise ApiError(400, f"model must be one of {ALLOWED_MODELS}")
    max_tokens = min(int(body.get("max_tokens") or 1024), MAX_TOKENS_CAP)
    system = str(body.get("system", ""))[:MAX_SYSTEM_CHARS]
    messages = _sanitize_messages(body.get("messages"))
    tools = _anthropic_tools(body.get("tools"))

    kwargs = {"model": model, "max_tokens": max_tokens, "messages": messages}
    if system:
        kwargs["system"] = system
    if tools:
        kwargs["tools"] = tools
        # final=true: the client is wrapping up (loop budget hit) — keep the
        # tool definitions so the tool_use/tool_result history stays coherent,
        # but forbid new calls so this turn must answer in text.
        if body.get("final") is True:
            kwargs["tool_choice"] = {"type": "none"}

    try:
        message = _anthropic.messages.create(**kwargs)
    except Exception as exc:
        # Surface Bedrock-side failures (validation, throttling, access) as a
        # structured 502 instead of an opaque 500.
        raise ApiError(502, f"Bedrock call failed: {type(exc).__name__}: {exc}") from None

    _budget_record(sub, day, message.usage.input_tokens + message.usage.output_tokens)

    assistant_content = [block.model_dump(exclude_none=True) for block in message.content]
    out = {
        "model": model,
        "stop_reason": message.stop_reason,
        "assistant": assistant_content,
        "usage": {
            "input_tokens": message.usage.input_tokens,
            "output_tokens": message.usage.output_tokens,
        },
    }

    if message.stop_reason == "tool_use":
        results = []
        for block in assistant_content:
            if block.get("type") != "tool_use":
                continue
            # Leave >=4s of Lambda budget so the response itself still ships.
            remaining = ctx.get_remaining_time_in_millis() / 1000 - 4 if ctx else 15
            if remaining < 2:
                results.append({
                    "type": "tool_result", "tool_use_id": block["id"],
                    "content": "tool execution skipped: request time budget exhausted — answer from the results you already have",
                    "is_error": True,
                })
                # Tell the client so its loop wraps up instead of burning
                # further rounds re-requesting tools that will be skipped.
                out["budget_exhausted"] = True
                continue
            status, parsed, raw = _mcp_call(
                server_id, sub,
                {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                 "params": {"name": block["name"], "arguments": block.get("input") or {}}},
                timeout=min(20, remaining),
            )
            if status != 200:
                text, is_err = f"{_mcp_status_hint(status)}: {raw[:500]}", True
            else:
                text, is_err = _tool_result_text(parsed, raw)
            result_block = {"type": "tool_result", "tool_use_id": block["id"], "content": text}
            if is_err:
                result_block["is_error"] = True
            results.append(result_block)
        out["tool_results"] = results

    return _resp(200, out)


def handler(event: dict, ctx) -> dict:
    method = (event.get("requestContext") or {}).get("http", {}).get("method", "")
    path = event.get("rawPath", "") or "/"
    try:
        claims = _claims(event)  # JWT authorizer already validated the token; this asserts identity presence
        if method == "POST" and path == "/playground/chat":
            return playground_chat(event, ctx, claims["sub"])
        if method == "POST" and path == "/playground/mcp":
            return playground_mcp(event, claims["sub"])
        raise ApiError(404, f"no route: {method} {path}")
    except ApiError as exc:
        return _resp(exc.status, {"error": str(exc)})
    except Exception as exc:  # noqa: BLE001
        print(f"UNHANDLED {method} {path}: {type(exc).__name__}: {exc}")
        return _resp(500, {"error": f"internal error: {type(exc).__name__}"})
