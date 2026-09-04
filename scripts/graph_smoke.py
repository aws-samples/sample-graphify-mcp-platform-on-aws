#!/usr/bin/env python3
"""End-to-end smoke test for the console graph explorer's data path.

Flow: Cognito password auth -> GET /repos/all/graph (hub) -> GET /repos/{id}/graph
-> bare fetch of the presigned viz.json (gzip-encoded) -> bundle schema checks
-> negatives (unknown id 404, private-or-unknown via /catalog 404, bad id 400).

Usage:
  uv run python scripts/graph_smoke.py --email t@example.com --password '...' [--repo-id github__psf__requests__main]
"""

from __future__ import annotations

import argparse
import gzip
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


def http(method: str, url: str, headers: dict | None = None) -> tuple[int, dict, bytes]:
    req = urllib.request.Request(url, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, {k.lower(): v for k, v in exc.headers.items()}, exc.read()


def api(base: str, token: str, path: str) -> tuple[int, dict]:
    status, _, raw = http("GET", base + path, {"Authorization": f"Bearer {token}"})
    try:
        return status, json.loads(raw)
    except json.JSONDecodeError:
        return status, {"_raw": raw.decode(errors="replace")[:300]}


def check_bundle(name: str, raw: bytes, headers: dict) -> None:
    # urllib does NOT auto-decode Content-Encoding; the browser does.
    body = gzip.decompress(raw) if headers.get("content-encoding") == "gzip" else raw
    b = json.loads(body)
    n = len(b["nodes"]["id"])
    m = len(b["edges"]["s"])
    cols = {k: len(v) for k, v in b["nodes"].items()}
    check(f"{name}: bundle v{b.get('v')} parses ({n} nodes / {m} edges, {len(b.get('communities', []))} communities)", n > 0)
    check(f"{name}: node columns aligned", all(v == n for v in cols.values()), str(cols))
    check(f"{name}: edge columns aligned", all(len(b["edges"][k]) == m for k in ("s", "t", "r", "inf")))
    check(f"{name}: edges in bounds", all(0 <= s < n and 0 <= t < n for s, t in zip(b["edges"]["s"], b["edges"]["t"])))
    check(f"{name}: positions present", "x" in b["nodes"] and "y" in b["nodes"], f"layout={b.get('layout')}")
    check(f"{name}: dictionaries", all(isinstance(b.get(k), list) for k in ("types", "kinds", "relations", "files", "repos")))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", required=True)
    ap.add_argument("--password", required=True)
    ap.add_argument("--region", default="ap-northeast-2")
    ap.add_argument("--stack", default=STACK_NAME)
    ap.add_argument("--repo-id", default="github__psf__requests__main")
    args = ap.parse_args()

    outputs = stack_outputs(args.region, args.stack)
    api_base = outputs["PlatformApiUrl"].rstrip("/")

    idp = boto3.client("cognito-idp", region_name=args.region)
    auth = idp.admin_initiate_auth(
        UserPoolId=outputs["UserPoolId"], ClientId=outputs["UserPoolClientId"],
        AuthFlow="ADMIN_USER_PASSWORD_AUTH", AuthParameters={"USERNAME": args.email, "PASSWORD": args.password},
    )["AuthenticationResult"]
    token = auth["AccessToken"]
    check("cognito password auth -> access token", bool(token))

    # /repos/{id}/graph is grant-gated: join the (public, pooled) repo first —
    # idempotent, same call platform_smoke.py makes.
    if args.repo_id == "github__psf__requests__main":
        req = urllib.request.Request(api_base + "/repos", method="POST", data=json.dumps({"git_url": "https://github.com/psf/requests"}).encode(),
                                     headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                check("POST /repos (join requests)", resp.status in (200, 201))
        except urllib.error.HTTPError as exc:
            check("POST /repos (join requests)", False, exc.read().decode(errors="replace")[:160])

    for label, path in (("hub", "/repos/all/graph"), ("repo", f"/repos/{args.repo_id}/graph")):
        status, out = api(api_base, token, path)
        check(f"GET {path}", status == 200 and out.get("state") in ("ready", "pending", "empty"), json.dumps({k: v for k, v in out.items() if k not in ("viz", "graph")})[:200])
        if status != 200:
            continue
        check(f"{label}: no bearer URL unless ready", out["state"] == "ready" or not (out.get("viz") or out.get("graph")))
        if out["state"] != "ready":
            print(f"      {label} not ready (state={out['state']}, status={out.get('status')}) — skipping download")
            continue
        viz, graph = out.get("viz"), out.get("graph")
        check(f"{label}: viz or graph present", bool(viz or graph))
        if viz:
            check(f"{label}: viz meta fields", all(k in viz for k in ("url", "bytes", "raw_bytes", "etag", "stats", "layout")), json.dumps(viz.get("stats"))[:160])
            check(f"{label}: graph fallback URL withheld when viz exists", not (graph and graph.get("url")))
            status, headers, raw = http("GET", viz["url"])
            check(f"{label}: presigned viz.json GET", status == 200, f"{status} {headers.get('content-type')} enc={headers.get('content-encoding')} len={headers.get('content-length')}")
            if status == 200:
                check(f"{label}: response headers pinned", headers.get("content-type", "").startswith("application/json") and "no-store" in headers.get("cache-control", ""), f"{headers.get('cache-control')}")
                check_bundle(label, raw, headers)
        elif graph:
            print(f"      {label}: no viz bundle yet (graph.json {graph.get('bytes')} B) — rebuild to generate one")
            if graph.get("url"):
                status, headers, raw = http("GET", graph["url"])
                check(f"{label}: presigned graph.json GET", status == 200 and json.loads(raw).get("nodes") is not None)

    status, out = api(api_base, token, "/repos/does__not__exist/graph")
    check("unknown repo -> 404", status == 404, json.dumps(out)[:120])
    status, out = api(api_base, token, "/catalog/does__not__exist/graph")
    check("catalog unknown -> 404", status == 404, json.dumps(out)[:120])
    status, out = api(api_base, token, f"/catalog/{args.repo_id}/graph")
    check("catalog public repo -> 200", status == 200 and out.get("state"), json.dumps({k: v for k, v in out.items() if k not in ("viz", "graph")})[:160])
    # Private files silos carry a __u<sub8> suffix; a foreign one must 404 on both routes.
    status, out = api(api_base, token, "/repos/files__nope__u00000000/graph")
    check("foreign private id via /repos -> 404", status == 404)
    status, out = api(api_base, token, "/catalog/files__nope__u00000000/graph")
    check("foreign private id via /catalog -> 404", status == 404)

    print(f"\n{'ALL PASS' if not failures else str(failures) + ' FAILURE(S)'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
