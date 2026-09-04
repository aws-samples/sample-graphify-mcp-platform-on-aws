# Engineering reference — aws-graphify-mcp-platform

> Deep-dive companion to the top-level [README](../README.md): design decisions, verified behaviors, operational gotchas and every CLI flag. Read this before changing the build/query planes.

Deploy [graphify](https://github.com/Graphify-Labs/graphify) as a remote **MCP server on ECS Fargate**, with a fully AWS-native pipeline that clones any git repo, builds its code knowledge graph, and keeps it up to date automatically.

```
git URL ──registration──> DynamoDB registry
                              │
EventBridge rate(5 min) ─> Poller Lambda ── GitHub commits API (ETag/SHA) ──┐
                              │  head SHA changed?                          │
                              ▼                                             │
                    CodeBuild (ARM64, NO_SOURCE)                            │
                      git fetch --depth 1 <SHA>                             │
                      graphify extract . --code-only   (incremental)        │
                              │                                             │
                              ▼                                             │
                    S3  repos/<repo_id>/latest/graphify-out/graph.json      │
                              │                        history/<sha>/…      │
                              ▼                                             │
                    ECS Fargate services (linux/arm64, always-warm,         │
                      one task per repo + a hub; Cloud Map DNS, VPC-only)   │
                      entrypoint.py: S3 sync thread + os.replace()          │
                      graphify serve: graph resident, hot-reload on change  │
                              ▲                                             │
                    MCP clients (Claude Code / Cursor …)                    │
                      via API GW /v1/mcp/{serverId} + X-Graphify-Key ───────┘
```

Key properties (all empirically verified during design):

- **graphify unmodified.** Its own MCP server (`graphify.serve`) speaks the required contract (`0.0.0.0:8000`, `POST /mcp`, stateless streamable-HTTP) via CLI flags.
- **Graph updates are a file swap, not a redeploy.** graphify re-stats `graph.json` on every tool call (cache keyed on `(mtime_ns, size)`), so the entrypoint's S3-sync thread + `os.replace()` propagates new graphs into *live* sessions.
- **Always-warm, memory-sized per repo.** The graph loads ONCE at task start and stays resident, so warm calls are sub-second even for very large graphs (the previous fixed-memory microVM runtime OOM-crash-looped on big repos, re-paying the cold load per call). Task size is per-repo (`service_cpu`/`service_memory`, default 0.5 vCPU/2GB).
- **One dedicated MCP service per repo, plus a hub.** Registration creates a per-repo Fargate service (searches that repo only; created dynamically with boto3, reusing the stack's image/roles/cluster). The CDK-managed **hub service** serves the **merged all-repos graph** (`repos/__all__`, refreshed by every build via `graphify merge-graphs`, node ids tagged per repo), so one query searches every repo.
- **Nothing world-accessible.** Tasks sit in public subnets (outbound-only, no NAT) behind a security group that admits ONLY the data-plane proxy Lambda; the sole public entry is API Gateway + the key authorizer.
- **Polling, not webhooks**, so it works for public repos you don't own (requirement: test with public GitHub repos). Webhook-owned repos can simply use a shorter poll or a phase-2 webhook Lambda.
- **Any smart-HTTP git host**, not just GitHub: GitLab (SaaS/self-managed), Bitbucket, Gitea/Forgejo, GitHub Enterprise, plain `git http-backend` — change detection falls back to the git smart-HTTP ref advertisement (`info/refs`), and clone auth maps the PAT to each provider's Basic-auth username. See "Beyond GitHub" below.
- **Cost model is always-on**: each repo service is a small ARM Fargate task billed 24/7 (~$17/mo at the 0.5 vCPU/2GB default in ap-northeast-2); the build pipeline stays ≈ $0.2/month.

## Layout

| Path | What |
|---|---|
| `cdk/` | CDK app (Python): S3, DynamoDB(+GSI), CodeBuild, Lambdas, EventBridge, VPC + ECS Fargate query plane |
| `cdk/buildspec.py` | Inline buildspec: pinned-SHA fetch → incremental `graphify extract --code-only` → S3 publish |
| `runtime/entrypoint.py` | Query-plane entrypoint: S3 graph sync (ETag + atomic `os.replace`) + `graphify.serve.serve_http` |
| `runtime/Dockerfile` | Query-plane image (linux/arm64; built/pushed by `cdk deploy` via DockerImageAsset) |
| `lambdas/poller/` | Scheduled change detection (GitHub `commits` API w/ `application/vnd.github.sha` + `If-None-Match`; git smart-HTTP fallback for non-GitHub) |
| `lambdas/completion/` | CodeBuild state-change → DynamoDB state transitions |
| `scripts/register_repo.py` | Register a repo (resolves real default branch — never assumes `main`) + first build + dedicated service |
| `scripts/sync_runtimes.py` | Create/roll-forward every repo's dedicated Fargate service (idempotent; run after `cdk deploy` changes the entrypoint/image) |
| `scripts/deregister_repo.py` | Delete a repo's service, disable polling; `--purge` also removes graphs + registry item |
| `scripts/smoke_test.py` | Raw JSON-RPC through the API-key data plane: tools/list + tool calls (needs `GRAPHIFY_API_KEY`) |
| `scripts/print_mcp_config.py` | Emits the `.mcp.json` block (HTTP data-plane entries, `X-Graphify-Key`) |
| `lambdas/authorizer/` | MCP data-plane REQUEST authorizer: `X-Graphify-Key` → DynamoDB key lookup → scoped IAM policy + usage-plan identifier |
| `lambdas/mcp_proxy/` | MCP data-plane proxy: API Gateway → in-VPC HTTP to the repo's Fargate service (Cloud Map DNS) + per-key usage counters |
| `lambdas/platform_api/` | Management API (Cognito JWT): repos, MCP servers, API keys, usage, invites |
| `console/` | Platform console SPA (S3+CloudFront, Cognito managed login + PKCE, ko/en) |
| `console/graph.js`, `console/graph.css` | Graph explorer tab (sigma.js v3 WebGL + graphology): folder → community → node drill-down, filters, search, path finding, PNG export |
| `cdk/build_scripts/make_viz.py` | Build-plane bundle for the explorer: `graph.json` → columnar gzip `viz.json` + `viz-meta.json` with a precomputed two-level (community) layout via python-igraph |
| `scripts/create_platform_user.py` | Bootstrap/reset a console user (first admin) |
| `scripts/platform_smoke.py` | Platform E2E: Cognito auth → join repo → issue key → MCP via key → negatives → usage |
| `lambdas/playground/` | Console playground backend (buffered): Claude on Bedrock (Anthropic SDK) + MCP data-plane bridge |
| `lambdas/playground_stream/` | Streaming playground backend: Function URL (SSE) → Claude on Bedrock, in-Lambda Cognito JWT verify |
| `scripts/playground_smoke.py` | Playground E2E: tools/list via bridge → plain chat → Claude MCP tool-use loop → negatives |
| `scripts/graph_smoke.py` | Graph explorer E2E: `/repos/{id}/graph` + `/catalog/{id}/graph` → presigned `viz.json` fetch (gzip) → bundle schema checks → tenancy negatives |

## Deploy

Prerequisites: `uv`, Node 20+ (for the CDK CLI via `npx`), AWS credentials with admin-ish rights, CDK bootstrapped in the target region (`ap-northeast-2` by default).

Docker must be running locally: `cdk deploy` builds and pushes the query-plane image (`runtime/Dockerfile`, linux/arm64).

```bash
uv sync
npx -y aws-cdk@2.1139.0 deploy                      # stack: GraphifyMcpPlatform (override: -c stack_name=… / GRAPHIFY_STACK_NAME)
```

Optional context flags: `-c runtime_name=my_graphify` (naming stem, pattern `[a-zA-Z][a-zA-Z0-9_]{0,47}` — **no hyphens**), `-c github_token_secret_arn=<arn>` (PAT for polling; strongly recommended beyond light testing — unauthenticated GitHub API quota is 60/h per *shared AWS egress IP*), `-c build_compute=small|medium|large` (graph-build container: ARM `small`=2vCPU/4GB, `medium`=4/8, `large`=8/16GB; **default `large`** — graphify's graph assembly OOMs 4GB on big repos like LiteLLM. Use `small` for a small-repo personal deploy to stay in the CodeBuild free tier), `-c hub_cpu=1024 -c hub_memory=4096` (hub task size).

> `webapp/` (the localhost setup console) predates the Fargate migration — its package/smoke jobs target the removed pre-Fargate flow and need a refresh before use. Use the CLI above.

## Register a repo & test

```bash
# Public repo, default branch auto-resolved, first build starts immediately
uv run python scripts/register_repo.py --url https://github.com/psf/requests

# Wait for the build (1–2 min for a repo this size), then (key from the console):
GRAPHIFY_API_KEY=gfy_live_... uv run python scripts/smoke_test.py --repo-id github__psf__requests__main --node Session

# Connect Claude Code / Cursor — emits "graphify-all" (hub: one query searches
# every repo via the merged graph) plus one entry per repo's dedicated service
uv run python scripts/print_mcp_config.py         # paste into .mcp.json (fill in your key)
```

Private repos: create a Secrets Manager secret named `graphify/...` whose value is **exactly** `{"token": "<fine-grained PAT>"}` (the JSON shape is validated at registration) and pass `--auth-secret graphify/...`. The token is resolved *inside* CodeBuild (`SECRETS_MANAGER` env type) and injected as an `http.extraHeader` — it never transits CloudTrail. Re-registering an existing repo requires `--force`.

## Platform mode — API keys + multi-user console

The stack also deploys a **self-serve platform** on top of the same build/query planes, so MCP clients need an **API key only — no AWS credentials, no SigV4 proxy**:

```
MCP client ── X-Graphify-Key ──> API Gateway REST /v1/mcp/{serverId}
                                    │ Lambda REQUEST authorizer (TTL 0: revocation is immediate)
                                    │   gfy_live_<kid>_<secret><crc> → SHA-256 lookup → scoped IAM policy
                                    │   + per-key usage plan (20 rps / 40 burst / 500k req/mo)
                                    ▼
                                 proxy Lambda (in-VPC) ── HTTP :8000/mcp via Cloud Map DNS ──> per-repo / hub Fargate task
                                    └─ per-key·per-server usage counters (DynamoDB)

Console (S3+CloudFront SPA, ko/en) ── Cognito managed login (invite-only, PKCE) ──> HTTP API (JWT) ──> platform Lambda
```

Bootstrap the first admin, then everything else happens in the console (`ConsoleUrl` stack output):

```bash
uv run python scripts/create_platform_user.py --email you@example.com --admin
uv run python scripts/platform_smoke.py --email you@example.com --password '<printed>'   # full E2E
```

Console flows: register repos (public URL or PAT), watch build/runtime status, copy per-server MCP URLs, issue/revoke API keys (shown once; **active ~1 min after issuance** — API GW usage-plan propagation), per-key usage charts, invite users (admin), and a **playground** (below). Client config is one line:

```bash
claude mcp add --transport http graphify \
  https://<api-id>.execute-api.ap-northeast-2.amazonaws.com/v1/mcp/all \
  --header "X-Graphify-Key: gfy_live_..."
```

Tenancy model:

- **Public repos are pooled**: one build, one graph, one service shared platform-wide; a second registration joins instantly (`subscriber_count` ref-counting; teardown at 0; re-registering a torn-down repo revives it).
- **Private repos (PAT) are siloed**: `repo_id` gets a `__u<sub8>` owner suffix, `graph_scope=private` keeps the graph **out of the merged hub** (the buildspec membership filter is fail-closed on `graph_scope = public`), and only the owner's grants/keys can reach its server.
- **serverId** in the MCP URL is the `repo_id`, or `all` for the hub (merged public graph). Keys are scoped `ALL` or to explicit server ids; the authorizer gates access and the proxy re-checks scope in code.
- The API-key data plane is the ONLY access path — the Fargate tasks are reachable solely from the proxy Lambda's security group.

## Playground — test MCP with Claude on Bedrock

The console's **Playground** tab lets any signed-in user test an MCP server end-to-end without leaving the browser:

```
Console ── Cognito JWT ──> POST /playground/chat ── AnthropicBedrock (Claude on Bedrock) ─┐
                                    │  stop_reason=tool_use → execute tool calls          │
                                    ▼                                                     │
                 grant check (USER#sub / REPO#serverId) → invoke McpProxyFn directly     <┘
                 (synthesized authorizer context kid="playground", scoped to serverId)
```

- Pick a server — **no API key**. The server picker mirrors `GET /servers` (the hub plus every source the user owns, subscribes to or was invited to), and both playground Lambdas enforce the same rule server-side: `all` is open to every signed-in user; any other `server_id` must be an enabled registry row on which the caller's `sub` holds a `USER#<sub>/REPO#<id>` grant (403 otherwise, 404 for unknown/disabled). MCP traffic then goes to the in-VPC proxy Lambda by direct invoke with a synthesized authorizer context (`kid: "playground"`, `scopeServerIds` pinned to that server) — the same path the graph explorer's source viewer uses — so the public data plane, API keys and usage plans are not involved. (The old design pasted a `gfy_` key and went through API Gateway; it was dropped so users can only test servers they are entitled to, without minting a key first.)
- **Load tools** renders the server's `tools/list`; Claude then calls them in an agentic loop. The model picker lists the Claude models **≥ Sonnet 4.6** that run under the account's default Bedrock data-retention mode (Sonnet 4.6, Opus 4.6/4.7/4.8 — `global.*` cross-region inference profiles). The Claude 5 family (Fable/Sonnet/Opus 5) is omitted because it requires the `provider_data_share` retention mode; add those ids once that is enabled. The loop is **client-driven** — one model call per HTTP request — so no request outlives the platform limits; the browser re-POSTs while `stop_reason == "tool_use"` (max 8 rounds, then a wrap-up round with `tool_choice: none` forces an answer).
- **Streaming**: output streams token-by-token as Server-Sent Events (text, thinking, tool_use, tool_result frames). Because HTTP API integrations are buffered, streaming runs on a dedicated **Lambda Function URL** (`RESPONSE_STREAM`); it carries no API Gateway authorizer, so the Lambda verifies the Cognito access token itself (`aws-jwt-verify`). Assistant replies render as **Markdown** (marked + DOMPurify, SRI-pinned). A **Stop** button aborts an in-flight turn.
- A **direct tool call** panel invokes a single tool with JSON args (prefilled from the tool's schema) for protocol-level testing.

## Graph explorer — the console's 그래프 tab

Every built source (and the hub) can be browsed visually in the console. Nothing is laid out in the browser: each build runs `make_viz.py` after `cluster-only` and publishes, next to `graph.json`,

- `viz.json` — a compact columnar bundle (dictionaries for types/relations/files/repos, per-node columns incl. precomputed `x`/`y`, integer-indexed edges, community summaries + inter-community edge weights). Uploaded with `Content-Encoding: gzip`; ~30× smaller than `graph.json` (14.7 MB → 0.47 MB for a 12k-node repo).
- `viz-meta.json` — a ~1 KB header (stats, layout kind, sizes) the platform API inlines.

The layout is **two-level**: Fruchterman-Reingold (exact, `grid="nogrid"` — igraph's grid approximation collapses these graphs) on the community meta-graph places each community, a local FR lays out its members inside a disc sized by √members, and a grid-hashed relaxation removes disc overlaps. Communities are therefore visually distinct clusters and the aggregate view's disc is exactly the footprint its members occupy after drill-down. 12k nodes ≈ 1 s, 20k ≈ 2 s; the step is best-effort (`|| echo`) and never fails a build.

```
console ── Cognito JWT ──> GET /repos/{id}/graph   (grant-gated; 'all' = hub)
                           GET /catalog/{id}/graph (public preview, same predicate as the catalog)
                              └─> { state: ready|pending|empty, viz:{url,bytes,stats…}, graph:{url|null,bytes} }
                                    url = presigned S3 GET (300 s, exact key, ResponseContentType/Disposition/CacheControl pinned)
console ── bare fetch(url) ──> S3 (CORS GET from the console origin; simple request, no preflight)
```

Views: **폴더/레포** (top-level folders — repos on the hub — as a small weighted meta-graph, edge labels = relation counts) → **연관 그룹** (Leiden communities as discs = footprints; double-click to open) → **모든 항목** (one community, an ego network, a path neighbourhood, or everything). The Korean UI uses plain terms (항목 = node, 연결 = edge, 연관 그룹 = community) and glosses relation/type values (호출 = calls, 포함 = contains, 설명(근거) = rationale …); English keeps the raw terms. Color by folder / node type / community (top-16 hues, rest grey) / repo; filters (node type, relation, repo, min degree, INFERRED, and a **간단히 보기** preset that hides `rationale` nodes plus `contains`/`rationale_for`/INFERRED edges); legend-as-filter; search with keyboard navigation; a node inspector with neighbors grouped by relation × direction, an inline **source viewer** (`POST /repos/{id}/source`: the platform Lambda applies the graph routes' access rule, then invokes the in-VPC proxy Lambda directly with a synthesized authorizer context to run the repo task's `read_source` — no API key needed; ±40 lines around the node's line with paging, highlighted; hub nodes read from their origin repo server; 3000 reads/user/day), copy-as-context (markdown) and "ask in Playground"; shortest path (undirected BFS, ≤ 8 hops); PNG export; fullscreen; `/` `F` `L` `+` `-` `Esc` `Alt+←` shortcuts; ko/en.

Operational notes:

- Document nodes rarely carry a line: the Markdown quick-scan gives headings one (`L844`), but LLM extraction on PDF/Office sidecars cites pages (`p.3`, `p.21-24`) and leaves concept/entity nodes with a file and no location. `make_viz.py --src-dir /tmp/work/src` (per-repo bundles only) resolves them in `resolve_doc_locs()`: a page citation → the sidecar's `## <stem> — p.N` heading line, stored as `L<line> (p.N)`; otherwise the first line mentioning the node's label (retrying without a trailing parenthetical), stored as `L<line> (~)`. On the 18-PDF Korean finance corpus this covers 514 page citations + 182 label matches of 817 nodes; the ~120 leftovers (paraphrased labels) and hub-bundle nodes open at line 1 with an explicit hint (`gx.code.noloc` / `gx.code.pageonly`), and `(~)` matches show an "approximate" hint (`gx.code.approx`).
- Builds older than `make_viz.py` have no bundle: the tab falls back to the raw `graph.json` (client-side circlepack layout) up to 32 MB, else asks for a rebuild. Trigger bundles for existing repos with `uv run python scripts/update_repo_runtimes.py --rebuild` (or 재빌드 in the console). The hub bundle refreshes on every build's merge step.
- Access mirrors the data plane: hub → any member; public source → any member (catalog preview); private → grant holders. Torn-down rows 404 even though the S3 objects outlive them. Presigns are metered per user (500/day) and route-throttled.
- Verify after a deploy: `uv run python scripts/graph_smoke.py --email <e> --password <p>` (34 checks). The build scripts under `cdk/build_scripts/` reach CodeBuild through the stack's `BuildScriptsDeployment` (S3 `assets/build_scripts/`), so a `make_viz.py` change needs a `cdk deploy`; `igraph` is pinned in `cdk/buildspec.py` (`IGRAPH_VERSION`).
- Libraries load from cdnjs/jsDelivr with SRI (`graphology` 0.26.0, `sigma.js` 3.0.3, `graphology-library` 0.8.0 — the last is not on cdnjs). A blocked script or a browser without WebGL renders an error card, not a blank tab. If a CSP is ever added to the CloudFront response-headers policy it needs `script-src` for both CDNs and `worker-src blob:` (graphology-library's layout workers).

## Code search — `search_code` / `read_source`

Per-repo MCP servers (never the hub) carry two platform tools on top of graphify's graph tools:

- `search_code(pattern, glob?, regex?, ignore_case?, max_results?)` — full-text search over the repo's source (literal by default; 1MB/file scan cap, 5s budget, 100-result cap).
- `read_source(file, start_line?, end_line?)` — read a file range (≤400 numbered lines), grounded by graph nodes' `source_file`/`source_location`.

How it works: each build also publishes `repos/<id>/latest/src.tar.gz` (checkout minus `.git`, ≤200MB); the task's sync thread pulls it next to the graph (safe extraction via `tarfile filter="data"` + size caps, atomic symlink flip) and an ASGI wrapper in `runtime/entrypoint.py` adds the tools at the MCP JSON-RPC layer — graphify itself is untouched. The hub (`all`) serves the merged graph only and never gets a snapshot. After changing the entrypoint (a `cdk deploy` publishes the new image), roll existing per-repo services forward with:

```bash
uv run python scripts/update_repo_runtimes.py --rebuild   # services -> new image, builds -> publish snapshots
```
- Backends: `lambdas/playground/` (buffered, vendored `anthropic` SDK, ARM64) and `lambdas/playground_stream/` (Node.js, `@anthropic-ai/bedrock-sdk`, reserved concurrency so a burst can't drain the account pool the data plane shares); both scope `bedrock:InvokeModel` to Anthropic models/profiles, enforce a per-user daily token budget, and cap tokens/messages/tools/tool-result size.

```bash
uv run python scripts/playground_smoke.py --email you@example.com --password '...'   # playground E2E
```

## Beyond GitHub — any git-based repository

Registration auto-detects the provider from the host (`--provider github|gitlab|bitbucket|generic` to override):

```bash
uv run python scripts/register_repo.py --url https://gitlab.com/gitlab-org/release-cli          # GitLab (verified E2E)
uv run python scripts/register_repo.py --url https://git.mycorp.com/team/service --provider generic
uv run python scripts/register_repo.py --url https://gitlab.mycorp.com/team/app --auth-secret graphify/pat/corp
```

How each concern generalizes:

| Concern | GitHub | Everything else |
|---|---|---|
| Change detection | commits API (`application/vnd.github.sha` + conditional request; smart-HTTP fallback on 403/429) | git smart-HTTP `info/refs?service=git-upload-pack` — one unauthenticated-quota-free GET, works on any git server, PAT rides as Basic auth |
| Default-branch resolution | repos API, `info/refs` fallback | `symref=HEAD:refs/heads/<branch>` from the ref advertisement |
| Clone auth username | `x-access-token:<PAT>` | `gitlab → oauth2`, `bitbucket → x-token-auth`, `generic → git` (override with a `GIT_AUTH_USER` build env if your server wants another) |
| Pinned-SHA fetch | `fetch --depth 1 <sha>` (`allow-reachable-sha1-in-want`) | same when advertised (GitLab/Gitea do); otherwise automatic fallback: fetch the branch ref, check the SHA out, deepen to 100 if the branch advanced mid-build |

Requirements and limits:

- The server must be **reachable over HTTPS from AWS** (poller Lambda + CodeBuild both egress via the public internet) and speak **git smart HTTP**. SSH-only remotes are not supported — use an HTTPS PAT (every mainstream host issues one).
- **GitHub Enterprise Server** registers as `generic` (the GitHub REST path only understands github.com); everything works via smart HTTP.
- **Private-network git** (self-hosted GitLab in a VPC / on-prem behind DX·VPN): attach the poller Lambda and the CodeBuild project to that VPC (CDK: `vpc` + `subnet_selection` on both, plus a NAT gateway or routed connectivity, S3/DynamoDB/Secrets Manager gateway·interface endpoints). The Fargate tasks stay in the public subnets — they only read S3, never the git server. Not wired by default to keep idle cost at ~$0; the change is confined to `cdk/graphify_stack.py`.
- **CodeCommit** is intentionally unsupported (closed to new AWS customers since 2024).

## Document sources — file folders & docs-site URLs

Not everything worth graphing lives in git. Two more `source_type`s reuse the
whole pipeline (same CodeBuild project, poller scheduling, completion Lambda,
Fargate service, hub merge) — a doc source is just another `graph.json` in S3.
Markdown is extracted with graphify's no-LLM quick-scan (page + heading nodes,
cross-file link edges), so builds stay LLM-free; code files mixed into a
folder are AST-extracted as usual.

**Static file folder** (`source_type=files`) — an S3 upload prefix you sync any
folder of supported files (code and/or `.md`/`.mdx`/`.qmd` docs) into:

```bash
# register + sync a local folder + first build in one go
uv run python scripts/register_files_repo.py --name myproject --path ~/src/myproject
# later updates: just re-sync — the poller detects the change and rebuilds
aws s3 sync ~/src/myproject s3://<GraphBucket>/uploads/files__myproject/ --delete
```

Change detection: the poller hashes the S3 listing (key + ETag + size) under
`uploads/<repo_id>/` and rebuilds when it differs from the last built hash —
`last_built_sha` holds that content hash instead of a commit — suffixed with
the LLM knobs as `<sha>|img=<0|1>|model=<id or empty>|llm=<0|1>` (the build
publishes the same string as `source_hash`), so changing `llm_images`,
`llm_model` or `llm_extract` counts as a change and the poller rebuilds on its
next tick even when the toggle arrived while a build was in flight. PDF/Word/Excel
documents are converted to markdown at build time
(`cdk/build_scripts/convert_docs.py`: a PDF becomes `<name>.pdf.d/NNN-<title>.md`
parts split at section headings — PDF outline, bold/larger-than-body font lines,
제N장/절/조 and dotted numbering — with a `##### <stem> — p.N` marker per page
(`make_viz.py` accepts any heading level for the marker). Parts are sized in
characters to stay under graphify's 20,000-char slicing cap: close at the next
`##` once ≥ 8k chars, at a `###` past 14k, at any heading past 18k, at a page
boundary past 20k. Written into `<name>.pdf.d.tmp` and renamed when complete.
docx/xlsx use graphify's own converters into a single sidecar), so a
documents-only corpus still yields a real graph. Sidecar frontmatter names the
original as `converted_from_file` — never `source_file`, which the LLM would echo
and graphify would reject as out-of-scope attribution (458 items dropped once);
image-only scanned PDFs have no extractable text and are skipped. Console users:
`POST /repos {"source_type":"files","name":"..."}` creates a **private,
per-user silo** (never hub-merged) and returns the upload prefix + sync
command.

**Docs-site URL** (`source_type=url`) — a public https docs site crawled into
markdown on the build plane:

```bash
uv run python scripts/register_url_repo.py --url https://hatch.pypa.io/1.18/ --max-pages 100
```

The crawler is sitemap-first (robots.txt `Sitemap:` directives, then
`<prefix>/sitemap.xml`, then `/sitemap.xml`), falls back to BFS
link-following, and is clamped to the registered host + path prefix and
`--max-pages` (≤500). robots.txt disallows are honored; private/link-local
addresses are refused (SSRF guard); in-scope links are rewritten to relative
`.md` paths so the graph gets real cross-page edges. Re-crawl runs every
`--poll-interval` (default 6 h) as a build; when the crawled content hash
matches the previously published `source_hash`, the build skips extract +
publish entirely. Console: `POST /repos {"source_type":"url","url":"..."}`
(public, pooled — a second registration joins).

**LLM extraction** (`llm_extract=1`, url|files): the buildspec swaps the
quick-scan for `graphify extract --backend bedrock` while the converted Markdown
corpus is under `LLM_CORPUS_CAP_MB` (registry `llm_corpus_cap_mb`, default 64,
max 512; `/llm` body `corpus_cap_mb`; bigger corpora deterministically fall
back). The LLM branch runs `GRAPHIFY_MAX_WORKERS=6` and a background loop
checkpoints `$GRAPHIFY_OUT/cache` to the S3 llmcache key every 10 min, so a
CodeBuild timeout loses at most 10 minutes of Bedrock work. `convert_docs.py`
selects PDF images from XObject metadata without decoding (decoding all
embedded images stalled a 168-PDF corpus for over an hour) and aborts any single
document after 600 s (`PDF_TIME_BUDGET_S`, logged as FAILED, build continues). Three registry
knobs ride every StartBuild path as `LLM_EXTRACT` / `LLM_IMAGES` / `LLM_MODEL`
(poller, platform API, `update_repo_runtimes.py`) and are edited together via
`POST /repos/{id}/llm {enabled?, images?, model?}` (omitted keys keep their
value; a rebuild starts when the graph would change):

- `llm_model` — one of `LLM_MODELS` in `lambdas/platform_api/handler.py`
  (advertised to the console via `/me`). Default `global.anthropic.claude-sonnet-5`
  is stored as *absent*. graphify's semantic cache is keyed by content + prompt,
  not model, so the S3 archive is namespaced: `llmcache.tar.gz` for the default,
  `llmcache-<slug>.tar.gz` otherwise. For url sources the model is part of the
  skip fingerprint (`…|model=<id>|llm=1`) so a model change re-extracts an
  unchanged crawl.
- `llm_images` — files only (the crawler strips `<img>`). Keeps png/jpg/gif/webp
  in the corpus so graphify's vision path sends them to Bedrock (one image node
  each, edges to the docs it references; ~1.6k tokens/image, 5 MB per image, 20
  per request). `convert_docs.py` also extracts figures embedded in PDFs
  (`<name>.pdf.d/img/pNNNN-KK.png`, referenced from the page's part): unique by
  sha1 across the corpus, ≥ 20 KB and ≥ 200 px, largest area first, a hard
  300 total shared by page count (a 4-per-PDF floor that is trimmed back when
  it would breach 300), downscaled to 1,280 px; only when LLM_EXTRACT=1 too.
  Above 600 image files the build deletes them and extracts text only, logging
  `LLM images skipped`. Rasters are excluded from `src.tar.gz`. graphify packs
  images AFTER all text units, so image nodes mostly land in image-only chunks:
  expect image→doc edges only for images co-chunked with text.
- Enabling `llm_extract` (registration or `/llm`) sets `build_timeout_minutes`
  to 120 when the row has none — the 60-min project default is too short for a
  cold LLM build.
- Output budget: `GRAPHIFY_MAX_OUTPUT_TOKENS=64000` + `GRAPHIFY_API_TIMEOUT=1500`
  in the buildspec env, and `graphify extract … --token-budget 30000` (input
  chunks half of graphify's 60k default). graphify bisects a chunk whenever the
  JSON is truncated and keeps only a partial result at depth 3; at the 16k
  default a 590-page guide lost most of its nodes (13 truncations → 409 nodes),
  and 24 Korean FSS PDFs still truncated 13× at 32k/60k. Bedrock accepts
  maxTokens 64k on every allow-listed model (128k on all but Haiku 4.5).

Both source types run a clean-room `--force` extract every build (no
incremental baseline): `built_at_commit` and the shrink guard are git
concepts, and a docs corpus legitimately shrinks when files are deleted.

## Push-triggered builds (owned repos)

Polling is the default because it needs no repo permissions. For repos you **own** (webhook installation requires repo admin — that is the ownership gate), register with `--trigger webhook` to build within seconds of a push:

```bash
uv run python scripts/register_repo.py --url https://github.com/you/yourrepo --trigger webhook
# prints the Payload URL + the command that reveals the HMAC secret
```

Then add the webhook in GitHub (Settings → Webhooks → Add webhook): the printed Payload URL, content type `application/json`, the secret, push events only. The setup console shows the same values inline when you pick the webhook trigger.

How it works: GitHub push → **API Gateway HTTP API** → Lambda → `X-Hub-Signature-256` HMAC verified over the raw body (constant-time, before any parsing) → same claim/StartBuild path as the poller. API Gateway (not a Lambda Function URL) is deliberate: a NONE-auth Function URL puts `Principal: *` on the Lambda resource policy, which security scanners flag — and Amazon-internal tooling auto-blocks — as a world-accessible Lambda. With API Gateway the Lambda policy is scoped to `apigateway.amazonaws.com` + this API's ARN, and the HMAC remains the auth gate. Properties:

- Only `trigger=webhook` registrations are acted on; signed pushes for anything else are acknowledged and ignored.
- A push that arrives while a build is in flight is deferred to the poller (`next_poll_at = now + 120`) instead of double-building.
- Webhook repos keep a **6 h safety poll** (GitHub delivery is at-least-once, not guaranteed); `--poll-interval` overrides it.
- GitHub push payloads only for now; non-GitHub hosts use polling.
- Non-2xx responses show up in the repo's webhook "Recent Deliveries" panel for debugging.

## Update flow (requirement #2)

1. Poller compares the repo head SHA against `last_built_sha` (GitHub: conditional request, body *is* the SHA; non-GitHub: `info/refs` ref advertisement).
2. On change it claims the build via conditional `UpdateItem` (double-build guard) and runs CodeBuild at the **pinned SHA**.
3. CodeBuild restores `graph.json`+`manifest.json` (arms graphify's incremental path), extracts, verifies `built_at_commit == SHA` (retries `--force` if the shrink guard blocked a legitimate shrink), runs `cluster-only --no-label` for `GRAPH_REPORT.md`/community labels, publishes to `repos/<repo_id>/latest/` + `history/<repo_id>/<sha>/` (the latter expires after 30 days).
4. The task's sync thread notices the new S3 ETag, downloads to a temp file, `os.replace()`s it — graphify hot-reloads on the next tool call. Live sessions see the new graph within `SYNC_INTERVAL_SECONDS` (180 s) + one reload on the first call after a swap.

## Design decisions & gotchas (read before modifying)

- **Never set `GRAPHIFY_API_KEY`** in the task env: graphify's api-key middleware also matches `Authorization: Bearer` and would 401 requests. Network isolation (task SG admits only the proxy Lambda) + the data plane's key authorizer are the auth boundary.
- **Never set `GRAPHIFY_OUT`** in the task env: an absolute value makes `Path(project_path) / GRAPHIFY_OUT` collapse onto one path for *every* repo.
- `fetch_uploads.py` materializes S3 keys under **NFC** filenames. Keys synced from macOS are NFD (decomposed Hangul); the LLM emits `source_file` paths in NFC, and graphify's out-of-scope filter drops every node whose path does not byte-match a dispatched file — the first 168-PDF build lost all 10,574 items and fell back to the quick-scan. The manifest hash still uses the raw keys, so change detection is unaffected; `read_source` keeps its NFC/NFD fallback for older snapshots.
- Data-plane gateway responses (`cdk/graphify_stack.py`): GET/DELETE on `/v1/mcp/{serverId}` are MOCK 405s, `MISSING_AUTHENTICATION_TOKEN` (undefined route/method inside the stage) is 404, and `UNAUTHORIZED`/`ACCESS_DENIED` bodies carry a `hint`. Reason: on any 401 the MCP SDK runs OAuth discovery (`/.well-known/*`, `POST /register`); API Gateway's default 403 for those made Claude Code cache the server as "needs authentication" (`~/.claude/mcp-needs-auth-cache.json`) until cleared. Root-level paths outside the stage still return API Gateway's own 403 — not customizable.
- `--stateless` is load-bearing, not advisory: stateful streamable-HTTP would demand session affinity the proxy doesn't provide.
- The service name IS the Cloud Map DNS label, derived from `repo_id` by an identical function in `lambdas/mcp_proxy/handler.py`, `lambdas/platform_api/runtimes.py`, and `scripts/common.py` — keep the three in sync or that repo silently 502s.
- Per-repo task sizing lives on the registry row (`service_cpu`/`service_memory`, default 512/2048); bump memory for big graphs (LiteLLM runs 1024/4096) and combine with `prune_paths` to shrink the graph itself.
- Per-repo build sizing lives there too: `build_timeout_minutes` (project default 60; CodeBuild allows up to 480) and `build_compute` (`BUILD_GENERAL1_SMALL|MEDIUM|LARGE`). The poller, platform API and `scripts/update_repo_runtimes.py --rebuild` pass them as `timeoutInMinutesOverride`/`computeTypeOverride`; there is no console field yet — set them on the DynamoDB row. A running build cannot be extended: raise the value, stop the build, and let the poller reclaim it. LLM doc corpora need ≥ 90 (24 PDFs ≈ 52 min cold); the full linux tree needed 240.
- `list_prs` / `get_pr_impact` / `triage_prs` need the `gh` CLI + a checkout and will return tool-level errors in this deployment; the seven graph tools are the product.
- Graph size budget: graphify caps `graph.json` at 512 MiB (`GRAPHIFY_MAX_GRAPH_BYTES`); RSS amplifies ~7× file size — size the task memory accordingly (django ≈ 62 MB → 427 MB RSS).
- Build statuses: `REGISTERED` → `BUILDING` → `READY` | `FAILED` | `TOO_LARGE` (completion Lambda: graph.json above `GRAPH_SERVE_CAP_BYTES`, default 512 MiB — the task would refuse it; `last_built_sha` is still recorded so the poller only rebuilds on a source change). The completion Lambda also records `has_snapshot` (whether `src.tar.gz` was published); the console renders a `소스 없음` badge when it is false.
- The same 512 MiB cap applies to `graphify merge-graphs`, and one oversize input aborts the whole hub merge (a 1.6 GB linux graph took the hub bundle down). The buildspec now skips any staged graph above the cap with a `hub merge: skipping <repo>` log line; that repo keeps its dedicated server but is absent from `all`.
- Community IDs are not stable across rebuilds (clustering is non-deterministic); don't key anything on them.
- `shortest_path` is directed by default; retry with `undirected=true` per its error hint.
- Pinned versions (`graphifyy==0.9.51`, `mcp==2.1.1`, `starlette==1.6.0`) were verified together; upstream PR #2099 would *rename* `project_path` → `graph`, so don't float the graphify version.

## Costs (ap-northeast-2)

| Item | Idle | Active |
|---|---|---|
| Pipeline (S3+DDB+Lambda+EventBridge) | ≈ $0.05/mo | — |
| CodeBuild ARM small ($0.00385/min) | $0 | ~1 min/build; 100 free min/mo |
| Fargate query plane (always-on, ARM) | ~$17/mo per repo @0.5 vCPU/2GB; hub ~$34/mo @1 vCPU/4GB | same (billed 24/7) |

## Teardown

```bash
npx -y aws-cdk@2.1139.0 destroy
```
