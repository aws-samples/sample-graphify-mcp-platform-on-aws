#!/usr/bin/env python3
"""End-to-end smoke test for the playground (Bedrock Claude + MCP bridge).

Flow: Cognito password auth -> /playground/mcp tools/list -> /playground/chat
plain turn -> chat turn that must exercise MCP tool use (client-driven loop,
like the console) -> negatives. MCP access is the signed-in identity (no API
key): the hub is open to every user, other servers need a grant.

Usage:
  uv run python scripts/playground_smoke.py --email t@example.com --password '...'
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

import boto3

from common import STACK_NAME, stack_outputs

PASS, FAIL = "\033[32mPASS\033[0m", "\033[31mFAIL\033[0m"
failures = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global failures
    print(f"[{PASS if ok else FAIL}] {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        failures += 1


def api(base: str, token: str, method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
    headers = {"Authorization": f"Bearer {token}"}
    body = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(payload).encode()
    req = urllib.request.Request(base + path, method=method, data=body, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read() or b"{}")
        except json.JSONDecodeError:
            return exc.code, {}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", required=True)
    ap.add_argument("--password", required=True)
    ap.add_argument("--region", default="ap-northeast-2")
    ap.add_argument("--stack", default=STACK_NAME)
    ap.add_argument("--server-id", default="all")
    args = ap.parse_args()

    outputs = stack_outputs(args.region, args.stack)
    api_base = outputs["PlatformApiUrl"].rstrip("/")

    idp = boto3.client("cognito-idp", region_name=args.region)
    auth = idp.admin_initiate_auth(
        UserPoolId=outputs["UserPoolId"],
        ClientId=outputs["UserPoolClientId"],
        AuthFlow="ADMIN_USER_PASSWORD_AUTH",
        AuthParameters={"USERNAME": args.email, "PASSWORD": args.password},
    )["AuthenticationResult"]
    token = auth["AccessToken"]
    id_token = auth["IdToken"]  # streaming path verifies the ID token (X-Graphify-Id)
    check("cognito auth", bool(token))

    # tools/list via the playground bridge (retry: a freshly deployed proxy
    # or a restarting task can 502 for a moment)
    tools, mcp_out = [], {}
    for _ in range(6):
        status, mcp_out = api(api_base, token, "POST", "/playground/mcp", {
            "server_id": args.server_id,
            "payload": {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        })
        if status == 200 and mcp_out.get("ok"):
            tools = mcp_out.get("body", {}).get("result", {}).get("tools", [])
            break
        time.sleep(10)
    check("playground/mcp tools/list", bool(tools), f"{len(tools)} tools (last={json.dumps(mcp_out)[:120]})")

    # direct tool call through the bridge
    status, out = api(api_base, token, "POST", "/playground/mcp", {
        "server_id": args.server_id,
        "payload": {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                    "params": {"name": "graph_stats", "arguments": {}}},
    })
    text = "\n".join(c.get("text", "") for c in out.get("body", {}).get("result", {}).get("content", []))
    check("playground/mcp direct graph_stats", status == 200 and out.get("ok") and "node" in text.lower(), text[:80])

    # plain chat turn (no tools)
    status, out = api(api_base, token, "POST", "/playground/chat", {
        "server_id": args.server_id,
        "messages": [{"role": "user", "content": "Reply with exactly: READY"}],
    })
    text = "".join(b.get("text", "") for b in out.get("assistant", []) if b.get("type") == "text")
    check("playground/chat plain turn", status == 200 and out.get("stop_reason") == "end_turn" and "READY" in text,
          f"stop={out.get('stop_reason')} text={text[:40]!r}")

    # tool-use loop, exactly as the console drives it
    messages = [{"role": "user", "content": "Use the available MCP tools to find out how HTTP retry logic works in the indexed code. You MUST call at least one tool. Keep the final answer under 80 words."}]
    used_tools, final_text, rounds = [], "", 0
    for rounds in range(1, 9):
        status, out = api(api_base, token, "POST", "/playground/chat", {
            "server_id": args.server_id,
            "messages": messages, "tools": tools,
            "system": "You are a code-exploration assistant. Ground answers in tool results.",
        })
        if status != 200:
            break
        messages.append({"role": "assistant", "content": out["assistant"]})
        used_tools += [b["name"] for b in out["assistant"] if b.get("type") == "tool_use"]
        final_text = "".join(b.get("text", "") for b in out["assistant"] if b.get("type") == "text") or final_text
        if out.get("stop_reason") != "tool_use" or not out.get("tool_results"):
            break
        messages.append({"role": "user", "content": out["tool_results"]})
    check("playground/chat MCP tool-use loop", status == 200 and used_tools and len(final_text) > 30,
          f"rounds={rounds} tools={used_tools} answer={final_text[:70]!r}")

    # streaming endpoint (CloudFront /pgstream -> OAC -> IAM Function URL, SSE)
    stream_url = outputs.get("PlaygroundStreamUrl", "").rstrip("/")
    if stream_url:
        deltas, final, err = 0, None, ""
        # A newly added CloudFront behavior takes a few minutes to propagate;
        # until then /pgstream can 403 at the edge. Retry.
        import hashlib as _hl

        for _ in range(18):
            body = json.dumps({"server_id": args.server_id,
                               "messages": [{"role": "user", "content": "Reply with exactly: STREAMING OK"}]}).encode()
            req = urllib.request.Request(
                stream_url, method="POST", data=body,
                headers={"X-Graphify-Id": id_token, "Content-Type": "application/json",
                         "x-amz-content-sha256": _hl.sha256(body).hexdigest()})
            deltas, final, err = 0, None, ""
            try:
                with urllib.request.urlopen(req, timeout=120) as resp:
                    body = resp.read().decode(errors="replace")
                for line in body.split("\n"):
                    if line.startswith("data: "):
                        ev = json.loads(line[6:])
                        if ev.get("type") == "delta":
                            deltas += 1
                        elif ev.get("type") == "final":
                            final = ev
                break
            except urllib.error.HTTPError as exc:
                err = f"HTTP {exc.code}: {exc.read().decode(errors='replace')[:150]}"
                if exc.code != 403:
                    break
                time.sleep(10)
        text = "".join(b.get("text", "") for b in (final or {}).get("assistant", []) if b.get("type") == "text")
        check("playground streaming (SSE deltas + final)", deltas >= 1 and "STREAMING OK" in text,
              f"deltas={deltas} text={text[:40]!r} {err}")

        nbody = b"{}"
        req = urllib.request.Request(stream_url, method="POST", data=nbody,
                                     headers={"Content-Type": "application/json",
                                              "x-amz-content-sha256": _hl.sha256(nbody).hexdigest()})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                st = resp.status
        except urllib.error.HTTPError as exc:
            st = exc.code
        check("stream no id token -> 401", st == 401, f"status={st}")

    # negatives
    status, out = api(api_base, token, "POST", "/playground/chat", {
        "server_id": args.server_id, "model": "anthropic.evil-model",
        "messages": [{"role": "user", "content": "hi"}],
    })
    check("unknown model rejected", status == 400, f"status={status}")
    status, out = api(api_base, token, "POST", "/playground/chat", {
        "server_id": "github__nobody__no-such-repo__main",
        "messages": [{"role": "user", "content": "hi"}],
    })
    check("unknown server rejected (404)", status == 404, f"status={status}")
    status, out = api(api_base, token, "POST", "/playground/mcp", {
        "server_id": "bad id!", "payload": {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    })
    check("invalid server_id rejected (400)", status == 400, f"status={status}")
    req = urllib.request.Request(api_base + "/playground/chat", method="POST", data=b"{}",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            status = resp.status
    except urllib.error.HTTPError as exc:
        status = exc.code
    check("no JWT -> 401", status == 401, f"status={status}")

    print("\n" + ("PLAYGROUND SMOKE PASSED" if failures == 0 else f"PLAYGROUND SMOKE FAILED ({failures})"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
