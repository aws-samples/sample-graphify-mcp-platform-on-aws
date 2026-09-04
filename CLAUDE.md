# CLAUDE.md — sample-graphify-mcp-platform-on-aws

Deploys [graphify](https://github.com/Graphify-Labs/graphify) as a remote MCP server on ECS Fargate, with an S3/CodeBuild graph-build pipeline, change-detection polling, and a self-serve API-key platform (Cognito console + playground). `README.md` is the public front page (bilingual with `README.ko.md`); `docs/reference.md` is the authoritative engineering reference — its Layout table maps every directory, and its deploy/register/platform sections are kept current. Read it before structural changes, and keep both READMEs in sync when user-facing behavior changes.

## Commands

```bash
uv sync                                   # Python deps (uv-managed, Python >= 3.12)
npx -y aws-cdk@2.1139.0 deploy            # deploy (Docker must be running); set GRAPHIFY_STACK_NAME if your stack has a custom name
uv run python scripts/sync_runtimes.py    # roll every per-repo Fargate service after entrypoint/image changes
uv run python scripts/smoke_test.py --repo-id <id> --node <name>   # data-plane E2E (needs GRAPHIFY_API_KEY)
uv run python scripts/platform_smoke.py --email <e> --password <p> # platform E2E
uv run python scripts/playground_smoke.py                          # playground E2E
uv run python scripts/graph_smoke.py --email <e> --password <p>    # graph explorer E2E (API + presigned bundle)
uv run python scripts/update_repo_runtimes.py --rebuild --repo-id <id>   # rebuild one repo (e.g. to generate its viz bundle)
npx -y aws-cdk@2.1139.0 synth -c nag=true -o /tmp/cdk.out.nag                    # cdk-nag AwsSolutions report
```

There is no test suite; the `scripts/*_smoke.py` E2E scripts against a deployed stack are the verification path.

## Hard constraints — do not "fix" these

- **Stack name.** The code default is `GraphifyMcpPlatform`, overridable with `-c stack_name=...` or the `GRAPHIFY_STACK_NAME` env var (read by `cdk/app.py` and `scripts/common.py`). Always deploy and run scripts with the SAME name an existing deployment used, otherwise CDK creates a second stack whose fixed-name resources (CodeBuild project, log group, Cognito domain) collide. Never rename logical IDs: that replaces live resources.
- **The stack is stateful once deployed** (registry, graphs, Cognito users). Prefer additive changes; destructive CDK diffs need explicit sign-off.
- `runtime_name` context values match `[a-zA-Z][a-zA-Z0-9_]{0,47}` — **no hyphens** (repo/service naming derives from it).
- **Deploy assets, not junk:** `lambdas/playground/vendor/` (vendored `anthropic` SDK for Bedrock, `pip install -t vendor`) and `lambdas/playground_stream/node_modules/` (restore with `npm ci`) ship inside plain `Code.from_asset()` bundles. Don't delete vendor/; node_modules is gitignored but required locally at deploy time.
- **`.mcp.json` may hold a live `gfy_live_...` API key** — gitignored, never commit; regenerate with `scripts/print_mcp_config.py`.
- `webapp/` is the pre-Fargate localhost console and is **stale** (README says so). Don't extend it; the S3+CloudFront SPA in `console/` is the real console.

## Architecture cheat sheet

- Build plane: EventBridge rate(5 min) → poller Lambda (GitHub commits API w/ ETag; git smart-HTTP fallback for non-GitHub; S3 listing hash for file folders; crawl schedule for docs sites) → CodeBuild (ARM64, NO_SOURCE, inline buildspec in `cdk/buildspec.py`) → S3 `repos/<repo_id>/latest/graphify-out/graph.json`. Document sources run `cdk/build_scripts/convert_docs.py` (PDF → section-aligned Markdown parts, optional embedded-image extraction) and either the no-LLM quick-scan (`docs_extract_driver.py`) or, with `llm_extract`, a Bedrock Claude semantic pass.
- Query plane: always-warm Fargate task per repo + a hub serving the merged all-repos graph; `runtime/entrypoint.py` syncs from S3 with atomic `os.replace()` — graphs hot-reload into live sessions, no redeploy.
- Data plane: API GW REST `/v1/mcp/{serverId}` → REQUEST authorizer (`X-Graphify-Key`, TTL 0) → in-VPC proxy Lambda → Cloud Map DNS → task `:8000/mcp`. Fargate tasks admit only the proxy Lambda's SG. GET/DELETE answer 405; undefined routes 404; 401/403 bodies carry a `hint`.
- Registration of a new repo creates its Fargate service dynamically with boto3 (from `lambdas/platform_api/runtimes.py` / `scripts/register_repo.py`), reusing the stack's image/roles/cluster — per-repo services are NOT in the CDK template.
- `graphifyy` (the PyPI package) is pinned at 0.9.51 in both `cdk/buildspec.py` and `runtime/Dockerfile`; bump both together and re-run `sync_runtimes.py`. `docs_extract_driver.py` and `runtime/entrypoint.py` touch graphify internals that were verified at that version.
- Per-source LLM knobs (`llm_extract`, `llm_images`, `llm_model`, `llm_corpus_cap_mb`) ride EVERY StartBuild path (poller, platform API, `scripts/update_repo_runtimes.py`) as `LLM_*` env — a path that omits them silently rebuilds an LLM source as a quick-scan graph. Files-source `last_built_sha` is `<listing sha>|img=|model=|llm=`; the poller appends the same suffix.
- Graph explorer (console 그래프 tab): `cdk/build_scripts/make_viz.py` runs in every build (best-effort) and publishes `viz.json` (gzip, precomputed two-level igraph layout) + `viz-meta.json` beside `graph.json`; `GET /repos/{id}/graph` / `GET /catalog/{id}/graph` return short presigned S3 GETs. `console/graph.js` renders with sigma.js v3 + graphology from CDN (SRI-pinned). Keep igraph FR calls on `grid="nogrid"` — the grid approximation collapses these graphs.

## Conventions

- Python only (plus one Node lambda, `playground_stream`); no linter/formatter is configured — match existing style.
- Docs live in `docs/` (`architecture.*.html` diagrams; `reference.md` engineering reference; `document-sources-ops.md` covers files/url source ops).
