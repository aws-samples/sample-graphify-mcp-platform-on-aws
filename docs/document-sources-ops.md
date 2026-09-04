# Document sources — operator notes

Operational quick reference for the `files` / `url` source types. Concepts and
registration commands live in the README section "Document sources"; this page
records what to check when something looks off.

## files source

- Change detection = sha256 over the sorted S3 listing (`rel \t etag \t size`)
  of `uploads/<repo_id>/`, suffixed with the LLM knobs:
  `<sha>|img=<0|1>|model=<id or empty>|llm=<0|1>`. The poller computes it every
  `poll_interval_seconds` and rebuilds when it differs from `last_built_sha`, so a
  settings change is a change too. The build recomputes the hash from its own
  listing, appends the same suffix and publishes it as
  `repos/<repo_id>/latest/source_hash`; the completion Lambda copies that into
  `last_built_sha`. An LLM fallback marks the hash `llmfallback-<sha>|…` so the
  next tick retries.
- An EMPTY upload prefix never builds — sync at least one supported file.
- Korean/accented filenames synced from macOS arrive in NFD; the build
  materializes them as NFC (`fetch_uploads.py`) because LLM-emitted paths are
  NFC and graphify drops nodes whose `source_file` does not match a dispatched
  file byte-for-byte. Hash recipes still use the raw keys.
- Recipe lives in two places that must stay identical:
  `lambdas/poller/handler.py:files_manifest_hash` and
  `cdk/build_scripts/fetch_uploads.py`.
- **Never sync secret-bearing files.** The upload corpus becomes the graph AND
  the `search_code`/`read_source` snapshot; on a public (CLI-registered) repo
  every platform key can read it. `.mcp.json` (holds an API key), `.env`,
  credentials files must be excluded from the sync.

## url source

- The poller starts a crawl-build EVERY due tick; change detection happens in
  the build by comparing the crawl fingerprint
  (`<content sha256>|graphifyy=<ver>|prune=<paths>|viz=1|model=<id or empty>|llm=<0|1>`) against the published
  `source_hash`. Match → the build logs
  `crawl content unchanged — skipping graph rebuild` and publishes nothing.
- The fingerprint includes the graphify version and prune config on purpose:
  bumping either forces a re-extract even when the site is unchanged.
- Crawl scope = same host + path prefix of the registered URL. A dotted last
  segment is only treated as a file for real page extensions (`.html` etc.) —
  `/1.18` scopes to `/1.18/`, not the whole host.
- Discovery order: robots.txt `Sitemap:` → `<prefix>sitemap.xml` →
  `/sitemap.xml` → BFS link-following. robots.txt disallows are honored.

## Debugging

- Build logs: the CodeBuild project is `graphify_mcp_graph_build`; non-git
  branches log `[fetch_uploads]` / `[crawler]` lines.
- `repos/<repo_id>/latest/source_hash` is the last PUBLISHED content hash;
  compare it with `last_built_sha` on the registry row when a rebuild loop is
  suspected.
