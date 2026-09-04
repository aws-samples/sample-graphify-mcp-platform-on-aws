#!/usr/bin/env python3
"""End-to-end smoke test for the platform path (management API + key data plane).

Flow: Cognito password auth -> /me -> join a repo -> issue a key ->
raw MCP JSON-RPC through https://.../v1/mcp/{serverId} with X-Graphify-Key ->
negative checks (bad key 401/403, GET 405, revoked key 403) -> /usage.

Usage:
  uv run python scripts/platform_smoke.py --email t@example.com --password '...'
"""

from __future__ import annotations

import argparse
import json
import sys
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


def http(method: str, url: str, headers: dict | None = None, body: bytes | None = None) -> tuple[int, dict, bytes]:
    req = urllib.request.Request(url, method=method, data=body, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def api(base: str, token: str, method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
    headers = {"Authorization": f"Bearer {token}"}
    body = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(payload).encode()
    status, _, raw = http(method, base + path, headers, body)
    try:
        return status, json.loads(raw)
    except json.JSONDecodeError:
        return status, {"_raw": raw.decode(errors="replace")[:300]}


def mcp(base: str, server_id: str, key: str, payload: dict) -> tuple[int, dict | None]:
    status, _, raw = http(
        "POST",
        f"{base}/mcp/{server_id}",
        {"Content-Type": "application/json", "Accept": "application/json, text/event-stream", "X-Graphify-Key": key},
        json.dumps(payload).encode(),
    )
    try:
        return status, json.loads(raw)
    except json.JSONDecodeError:
        return status, None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", required=True)
    ap.add_argument("--password", required=True)
    ap.add_argument("--region", default="ap-northeast-2")
    ap.add_argument("--stack", default=STACK_NAME)
    ap.add_argument("--repo-id", default="github__psf__requests__main")
    ap.add_argument("--keep-key", action="store_true", help="skip the revocation step and print the key")
    args = ap.parse_args()

    outputs = stack_outputs(args.region, args.stack)
    api_base = outputs["PlatformApiUrl"].rstrip("/")
    mcp_base = outputs["McpDataApiUrl"].rstrip("/")

    idp = boto3.client("cognito-idp", region_name=args.region)
    auth = idp.admin_initiate_auth(
        UserPoolId=outputs["UserPoolId"],
        ClientId=outputs["UserPoolClientId"],
        AuthFlow="ADMIN_USER_PASSWORD_AUTH",
        AuthParameters={"USERNAME": args.email, "PASSWORD": args.password},
    )["AuthenticationResult"]
    token = auth["AccessToken"]
    check("cognito password auth -> access token", bool(token))

    status, me = api(api_base, token, "GET", "/me")
    check("GET /me", status == 200 and "sub" in me, json.dumps(me)[:120])

    status, joined = api(api_base, token, "POST", "/repos", {"git_url": "https://github.com/psf/requests"})
    check("POST /repos (pooled join of existing public repo)", status in (200, 201) and joined.get("repo_id") == args.repo_id, json.dumps(joined)[:160])

    status, repos = api(api_base, token, "GET", "/repos")
    check("GET /repos shows the grant", status == 200 and any(r["repo_id"] == args.repo_id for r in repos.get("repos", [])))

    status, servers = api(api_base, token, "GET", "/servers")
    ids = [s["server_id"] for s in servers.get("servers", [])]
    check("GET /servers lists hub + repo", status == 200 and "all" in ids and args.repo_id in ids, str(ids))

    status, key_out = api(api_base, token, "POST", "/keys", {"name": "smoke", "scope": "all", "expires_days": 7})
    key = key_out.get("api_key", "")
    kid = key_out.get("kid", "")
    check("POST /keys issues gfy_ key", status == 201 and key.startswith("gfy_live_"), f"kid={kid}")

    # --- data plane ---
    # A fresh API GW usage-plan key takes up to ~1 minute to propagate; the
    # gateway answers 403 Forbidden until then.
    import time as _t

    status, r = 0, None
    for _ in range(10):
        status, r = mcp(mcp_base, args.repo_id, key, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        if status != 403:
            break
        _t.sleep(10)
    tools = [t["name"] for t in (r or {}).get("result", {}).get("tools", [])]
    check(f"MCP tools/list via key ({args.repo_id})", status == 200 and "query_graph" in tools, f"{len(tools)} tools")

    status, r = mcp(mcp_base, args.repo_id, key, {
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "graph_stats", "arguments": {}},
    })
    text = "\n".join(c.get("text", "") for c in (r or {}).get("result", {}).get("content", []))
    check("MCP graph_stats via key", status == 200 and "nodes" in text.lower(), text.splitlines()[0][:80] if text else "no text")

    status, r = mcp(mcp_base, "all", key, {
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "query_graph", "arguments": {"question": "http session retry", "token_budget": 500}},
    })
    text = "\n".join(c.get("text", "") for c in (r or {}).get("result", {}).get("content", []))
    check("MCP hub (all) query via key", status == 200 and len(text) > 50, f"{len(text)} chars")

    # --- code-search tools (per-repo servers only; hub excluded by design) ---
    status, r = mcp(mcp_base, args.repo_id, key, {"jsonrpc": "2.0", "id": 10, "method": "tools/list"})
    repo_tools = [t["name"] for t in (r or {}).get("result", {}).get("tools", [])]
    check("repo server lists code tools", "search_code" in repo_tools and "read_source" in repo_tools, str([t for t in repo_tools if t in ("search_code", "read_source")]))

    status, r = mcp(mcp_base, "all", key, {"jsonrpc": "2.0", "id": 11, "method": "tools/list"})
    hub_tools = [t["name"] for t in (r or {}).get("result", {}).get("tools", [])]
    check("hub does NOT list code tools", "search_code" not in hub_tools and "read_source" not in hub_tools)

    # The snapshot lands on the runtime one sidecar sync (<=180s) after the
    # repo's next build publishes src.tar.gz — retry across that window.
    import time as _t2

    text, first_hit = "", ""
    for _ in range(10):
        status, r = mcp(mcp_base, args.repo_id, key, {
            "jsonrpc": "2.0", "id": 12, "method": "tools/call",
            "params": {"name": "search_code", "arguments": {"pattern": "class Session", "glob": "*.py"}},
        })
        text = "\n".join(c.get("text", "") for c in (r or {}).get("result", {}).get("content", []))
        if status == 200 and ".py:" in text:
            first_hit = text.splitlines()[1].split(":")[0] if len(text.splitlines()) > 1 else ""
            break
        _t2.sleep(30)
    check("search_code finds 'class Session'", "sessions.py" in text, text.splitlines()[0][:80] if text else "no text")

    if first_hit:
        status, r = mcp(mcp_base, args.repo_id, key, {
            "jsonrpc": "2.0", "id": 13, "method": "tools/call",
            "params": {"name": "read_source", "arguments": {"file": first_hit, "start_line": 1, "end_line": 10}},
        })
        text = "\n".join(c.get("text", "") for c in (r or {}).get("result", {}).get("content", []))
        check("read_source reads the hit file", status == 200 and "lines 1-10" in text and "1|" in text, text.splitlines()[0][:80] if text else "")
        status, r = mcp(mcp_base, args.repo_id, key, {
            "jsonrpc": "2.0", "id": 14, "method": "tools/call",
            "params": {"name": "read_source", "arguments": {"file": "../../etc/passwd"}},
        })
        text = "\n".join(c.get("text", "") for c in (r or {}).get("result", {}).get("content", []))
        check("read_source blocks traversal", "escapes" in text or "no such file" in text, text[:60])
    else:
        check("read_source reads the hit file", False, "no search hit to read")

    # --- negatives ---
    status, _ = mcp(mcp_base, args.repo_id, "gfy_live_AAAAAAAAAAAA_" + "a" * 43 + "aaaaaa", {"jsonrpc": "2.0", "id": 4, "method": "tools/list"})
    check("bad key rejected", status in (401, 403), f"status={status}")

    status, _, raw = http("GET", f"{mcp_base}/mcp/{args.repo_id}")
    check("GET on /mcp is a spec-legal 405", status == 405, f"status={status} body={raw[:60]!r}")

    # The authorizer now rejects an unregistered serverId (registry lookup
    # miss -> 403) before the proxy's 404 — earlier + less of an existence
    # oracle. Either terminal status is correct.
    status, _ = mcp(mcp_base, "no_such_repo", key, {"jsonrpc": "2.0", "id": 5, "method": "tools/list"})
    check("unknown serverId rejected", status in (403, 404), f"status={status}")

    status, usage = api(api_base, token, "GET", "/usage")
    total = sum(v.get("total", 0) for v in usage.get("keys", {}).values())
    check("GET /usage counters advanced", status == 200 and total >= 3, f"total={total}")

    if args.keep_key:
        print(f"\nkept key: {key}\nmcp url : {mcp_base}/mcp/all")
    else:
        status, _ = api(api_base, token, "DELETE", f"/keys/{kid}")
        check("DELETE /keys revokes", status == 200)
        status, _ = mcp(mcp_base, args.repo_id, key, {"jsonrpc": "2.0", "id": 6, "method": "tools/list"})
        check("revoked key rejected immediately", status in (401, 403), f"status={status}")

    print("\n" + ("SMOKE TEST PASSED" if failures == 0 else f"SMOKE TEST FAILED ({failures})"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
