# Document sources — operator notes

Operational quick reference for the `files` / `url` source types. Concepts and
registration commands live in the README section "Document sources"; this page
records what to check when something looks off.

## files source

- Change detection = sha256 over the sorted S3 listing (`rel \t etag \t size`)
  of `uploads/<repo_id>/`, suffixed with the LLM knobs:
  `<sha>|doc=2|img=<0|1>|model=<id or empty>|llm=<0|1>`. The poller computes it every
  `poll_interval_seconds` and rebuilds when it differs from `last_built_sha`, so a
  settings change is a change too. The build recomputes the hash from its own
  listing, appends the same suffix and publishes it as
  `repos/<repo_id>/latest/source_hash`; the completion Lambda copies that into
  `last_built_sha`. An LLM fallback marks the hash `llmfallback-<sha>|…` so the
  next tick retries.
- `doc=2` versions the document-conversion pipeline. Deploying the updated
  poller/buildspec rebuilds an unchanged files source once; image-enabled
  sources can incur new page-transcription calls during that migration.
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

## PDF body recovery when image extraction is enabled

With **both** `LLM_EXTRACT=1` and `LLM_IMAGES=1`, `convert_docs.py` checks
each PDF page before adding outline titles or other generated metadata.
A page with fewer than 50 native alphanumeric characters is sent as a
single-page PDF to the configured Bedrock model. The request enables
document citations, which activates Claude's visual PDF path in Converse.
This uses the existing `pypdf` and `boto3` dependencies; no separate rasterizer
or OCR package is installed.

The model is asked to transcribe the visible body, including Korean text,
tables, amounts, units and steps. The transcription is materialized in the
usual `.pdf.d/*.md` files, alongside original `p.N` markers and a
`text_method: bedrock_pdf_ocr` provenance comment. It therefore reaches
`search_code` and `read_source` through the normal source snapshot. It is a
model transcription, not an independently verified copy of every fact.
Dense tables and visually ambiguous text still require original-page checks.

This body-recovery path is **separate from the 300 embedded-image selection
budget**. Embedded images can still provide graph descriptions and links,
but their short labels are not treated as a substitute for page body text.
Native-text-rich pages keep the native path. A sparse-page detector does not
prove that text-rich pages contain every table cell or visual relationship.
Standalone image files retain the existing graph-image behavior.

### Bounds, caching and failure behavior

| Build environment variable | Default | Meaning |
|---|---|---|
| `DOCUMENT_OCR_MAX_NEW_PAGES` | `300` | Maximum cache-miss pages per build; cache hits do not consume it |
| `DOCUMENT_OCR_MAX_TOKENS` | `16384` | Maximum output tokens per page |
| `DOCUMENT_OCR_WORKERS` | `4` | Concurrent page requests within one PDF; allowed range 1–8 |
| `DOCUMENT_OCR_CACHE_DIR` | `/tmp/work/document-ocr-cache` | Local cache outside the source tree |
| `DOCUMENT_OCR_AUDIT_PATH` | `/tmp/work/document-ocr-audit.jsonl` | Attempt-level raw response/error/usage audit outside the source tree |
| `DOCUMENT_CONVERSION_REPORT` | `/tmp/work/document-conversion-report.json` | Local page/file conversion report |

The cache key binds source-file SHA-256, original page number, single-page
PDF SHA-256, model, transcription prompt/version and output limit. Successful
entries are saved locally and, when `GRAPH_BUCKET`/`REPO_ID` are set, under
`repos/<repo_id>/document-ocr-cache/v1/`. A failed later page does not discard
earlier paid successes. A new build can resume from those entries. Cached
usage is reported separately from new invocation usage; missing response
usage must not be interpreted as zero cost.

The CodeBuild project's default timeout is 60 minutes. Sources created through
the platform API with `llm_extract=1` receive a 120-minute
`build_timeout_minutes` default when no explicit override is supplied. The
poller passes that per-source value as `timeoutInMinutesOverride`; operator
scripts and existing sources can have different values. Check the source's
actual setting rather than treating the project default as its effective limit.

The 165-question validation candidate took 38.7 minutes with a 180-minute
benchmark override, including about 27.7 minutes of document conversion
(native parsing plus OCR) and 9.1 minutes of semantic extraction. It reused
761 semantic entries and re-extracted 109, so this was not a cold-build timing.
For capacity planning only, linearly scaling the semantic portion from 109 to
870 files and adding the observed conversion time gives roughly 100 minutes.
That is an unmeasured planning estimate, not a benchmark or completion guarantee:
file lengths, image work, retries, concurrency and additional OCR pages can
invalidate the scaling. Monitor large migrations against their per-source
timeout and preserve the page caches and periodic semantic-cache checkpoints.

When a build reaches the new-page budget, already successful page caches remain
available to the next build. With unchanged inputs, successful page requests,
and no other limiting failures, N > 0 distinct cache-miss pages and a budget B > 0
require ceil(N/B) builds, of which at most ceil(N/B)-1 fail for that budget.
This is not guaranteed convergence: unreadable pages, repeated invalid outputs,
oversized sidecars, the PDF page cap, or deadlines can keep a source failing.

Only complete, valid responses are accepted. Model output truncation,
entirely unreadable pages, invalid JSON, expired page work, corrupt cache entries
and exhausted new-page budgets are explicit failures. Pending OCR work is
cancelled at the document deadline; already dispatched remote work cannot
be withdrawn. SDK timeouts bound the remaining local wait.

If an otherwise readable page contains `[UNREADABLE]` or `[ILLEGIBLE]`
spans, those markers are preserved verbatim. The page is reported as
`transcribed_uncertain` and the conversion report as `partial`; readable
content can be published without replacing unknown values with guesses.
This partial status is distinct from a failed/truncated API response.
The v2 transcription prompt requests `transcribed` for partly readable pages
with marked gaps, and reserves `unreadable` for visible text that cannot be
read at all. A page with no visible text, including an empty ruled table, is
`blank`. The prompt version change invalidates v1 OCR cache entries.
Reasoning/signature metadata is never included in the source transcription.

Failed conversion does not publish a replacement graph or fingerprint.
For files sources, snapshot creation and upload must also succeed before
the latest graph is published; the old best-effort snapshot behavior remains
only for git/URL sources. S3 graph and snapshot objects are still separate
publications, not an atomic multi-object transaction.

### Evidence and unsupported inputs

- Per-build reports and raw OCR audits are uploaded to
  `history/<repo_id>/builds/<CodeBuild UUID>/`, including conversion failures
  when the process was able to write diagnostics.
- Successful files builds publish
  `repos/<repo_id>/latest/document-conversion-report.json`. Inspect its
  document/page status counts, OCR cache counts, known token usage and
  errors. These are processing metrics, not semantic recall or QA accuracy.
- Reports, caches and raw model audits are never source documents and must
  remain outside `CONVERT_DOCS_SRC`.
- Existing user-supplied sidecars are preserved and marked
  `existing_unverified`; their presence is not proof of complete conversion.
- Legacy `.doc`/`.xls` and PowerPoint formats are listed as unsupported by
  this converter. Native-only sparse pages and skipped formats produce a
  partial report rather than an assertion that all content was extracted.
- PDFs requiring an opening password, when no password is supplied, are
  explicitly `unsupported` with `reason_code=pdf_password_required`. The
  original upload is preserved, protected pages are not read or sent to OCR,
  and no converted sidecar is invented. The corpus report remains `partial`;
  `pdf_documents_unknown_page_count` counts all PDF records without a known
  page count, including password-required inputs, pre-existing unverified
  sidecars, and failures before page counting.
  PDFs readable with an empty opening password still use normal conversion.
  Unexpected parsing/decryption failures remain errors that block publication.
- PDFs above the 2,000-page cap and oversized converted parts now fail
  explicitly instead of silently publishing truncated text.

Before the `doc=2` migration, inventory enabled files sources, inspect original
PDF page counts and converted DOCX/XLSX sidecar sizes (10 MiB limit), and retain
their graph, source snapshot, report, fingerprint object versions and registry
state. Oversized inputs need splitting or another deliberate remediation; a
retry alone will not make them fit. A failed rebuild retains the prior graph.

The configured model must support visual PDF document processing through
Converse. The live development check used
`global.anthropic.claude-sonnet-5`; it rejects `temperature`, so the page
request omits that parameter.
