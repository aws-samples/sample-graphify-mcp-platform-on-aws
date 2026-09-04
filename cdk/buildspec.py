"""Inline buildspec for the graph-build CodeBuild project (NO_SOURCE).

One project serves every registered repo: REPO_ID / GIT_URL / TARGET_SHA /
GRAPH_BUCKET (and optionally GIT_TOKEN, type SECRETS_MANAGER) are injected per
build via StartBuild environmentVariablesOverride.

Source types (SOURCE_TYPE env, default "git" so pre-existing call sites are
unaffected):
  - git:   clone at the pinned SHA, incremental extract, verify
           built_at_commit == TARGET_SHA. Unchanged behavior.
  - files: download s3://$GRAPH_BUCKET/uploads/$REPO_ID/ (fetch_uploads.py),
           full --force extract with markdown routed through the no-LLM
           quick-scan (docs_extract_driver.py).
  - url:   crawl SOURCE_URL (docs_crawler.py: sitemap-first, scope-clamped,
           CRAWL_MAX_PAGES cap) into markdown, then extract like files.

Non-git sources have no commit hash, so the build itself computes a corpus
content hash (/tmp/work/content_hash) and publishes it to
repos/$REPO_ID/latest/source_hash AFTER the graph; the completion Lambda
reads that object into last_built_sha, and the poller compares against it
(files: its own S3 listing hash; url: every due tick starts a crawl-build and
THIS build skips everything — /tmp/work/SKIP — when the crawled hash equals
the previously published one). Non-git extraction is always a clean-room
--force run (no baseline restore): the shrink guard and built_at_commit
checks are git-only concepts, and a docs corpus legitimately shrinks when
files are deleted.

Design notes (all empirically verified against graphifyy 0.9.51):
- The SHA is fetched directly (GitHub advertises allow-reachable-sha1-in-want),
  so builds are idempotent and immune to the branch advancing mid-build.
- Restoring the previous graph.json + manifest.json arms graphify's
  incremental path (only changed files re-extracted). cache/ is NOT restored:
  on the --code-only path manifest.json alone buys the whole speedup.
- --code-only keeps the build LLM-free (tree-sitter AST only).
- If the shrink guard (or anything else) kept built_at_commit from advancing,
  retry once as a full forced rebuild, then hard-assert in post_build.
- GRAPHIFY_MAX_WORKERS is pinned because os.cpu_count() inside a container
  can report the host's CPUs, over-spawning workers on a 2-vCPU tier.
"""

GRAPHIFY_VERSION = "0.9.51"
MARKDOWNIFY_VERSION = "1.2.3"
DEFUSEDXML_VERSION = "0.7.1"
# Console graph-explorer bundle (make_viz.py): layout precomputation. Pinned
# like graphifyy; aarch64 manylinux wheels exist for the CodeBuild image.
IGRAPH_VERSION = "1.0.0"

# cdk/build_scripts/*.py are published to the graphs bucket by the stack's
# BuildScriptsDeployment (the project is NO_SOURCE, and inlining them as
# heredocs blew CodeBuild's 25,600-char buildspec cap). The build fetches
# them from this prefix at run time.
BUILD_SCRIPTS_PREFIX = "assets/build_scripts"


def _fetch_script(filename: str, dest: str) -> str:
    return f'aws s3 cp "s3://$GRAPH_BUCKET/{BUILD_SCRIPTS_PREFIX}/{filename}" {dest} --only-show-errors'


def _publish_viz(graph_path: str, out_dir: str, repo_id_expr: str, s3_prefix: str, src_dir: str = "") -> str:
    """Console graph-explorer bundle for one graph.json (make_viz.py), published
    beside it. viz.json is uploaded gzip-encoded (Content-Encoding: gzip) so the
    browser's fetch() decodes it transparently; viz-meta.json is the ~1 KB
    header the platform API inlines. Wrapped by the caller in `( … ) || echo`:
    the explorer is a read-side nicety and must never fail a build."""
    return "\n".join(
        [
            # Wall-clock box: the layout is quadratic in community count, and an
            # overrun must degrade to the non-fatal branch, not eat the build.
            f'timeout -k 30 600 python /tmp/make_viz.py --graph "{graph_path}" --out-dir "{out_dir}" --repo-id "{repo_id_expr}"'
            + (f' --src-dir "{src_dir}"' if src_dir else ""),
            f'aws s3 cp "{out_dir}/viz.json.gz" "{s3_prefix}/viz.json" --content-encoding gzip --content-type application/json --only-show-errors',
            f'aws s3 cp "{out_dir}/viz-meta.json" "{s3_prefix}/viz-meta.json" --content-type application/json --only-show-errors',
        ]
    )


BUILD_SPEC = {
    "version": "0.2",
    "env": {
        # PIPESTATUS in the LLM-extract block is a bash builtin.
        "shell": "bash",
        "variables": {
            "GRAPHIFY_OUT": "/tmp/work/graphify-out",
            "GRAPHIFY_MAX_WORKERS": "2",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            # Build scripts print progress per document; CodeBuild only sees it
            # live when Python does not block-buffer the pipe.
            "PYTHONUNBUFFERED": "1",
            "GRAPHIFY_VERSION": GRAPHIFY_VERSION,
            # LLM extraction: graphify caps Bedrock output at 16k tokens by
            # default and bisects a chunk whenever the JSON is truncated —
            # every split re-bills the input and a depth-3 miss keeps only a
            # partial result (a 590-page guide lost most of its nodes this
            # way). 64k is accepted by every allow-listed Claude model (Haiku
            # 4.5's ceiling); dense Korean regulation text still truncated 13x
            # at 32k. Pairs with `--token-budget 30000` on the extract call
            # (half-size input chunks), so output rarely nears the cap. The
            # botocore read timeout is raised for the longer generation.
            "GRAPHIFY_MAX_OUTPUT_TOKENS": "64000",
            "GRAPHIFY_API_TIMEOUT": "1500",
        }
    },
    "phases": {
        "install": {
            "runtime-versions": {"python": "3.12"},
            "commands": [
                f'pip install --quiet "graphifyy=={GRAPHIFY_VERSION}"',
                # Graph-explorer bundle layout (make_viz.py). Best-effort like
                # the bundle itself: an install failure only skips the viz.
                f'pip install --quiet "igraph=={IGRAPH_VERSION}" || echo "igraph install failed (viz bundle will be skipped)"',
                # Non-git sources: boto3 for the S3/crawl helpers (not
                # guaranteed in the CodeBuild python runtime), markdownify for
                # HTML->markdown (an optional graphifyy extra, so the base
                # install falls back to a lossy tag strip without it).
                "\n".join(
                    [
                        'if [ "${SOURCE_TYPE:-git}" != "git" ]; then pip install --quiet boto3; fi',
                        f'if [ "${{SOURCE_TYPE:-git}}" = "url" ]; then pip install --quiet "markdownify=={MARKDOWNIFY_VERSION}" "defusedxml=={DEFUSEDXML_VERSION}"; fi',
                        # files corpora carry office/PDF documents that the
                        # quick-scan can't read — convert_docs.py turns them
                        # into markdown sidecars first (pypdf + graphify's own
                        # docx/xlsx converters, which need python-docx/openpyxl).
                        'if [ "${SOURCE_TYPE:-git}" = "files" ]; then pip install --quiet pypdf pillow cryptography python-docx openpyxl; fi',
                    ]
                ),
            ],
        },
        "pre_build": {
            "commands": [
                'mkdir -p /tmp/work/src "$GRAPHIFY_OUT"',
                # Restore the incremental baseline (best-effort). git only:
                # non-git sources always build clean-room (see module docstring).
                '[ "${SOURCE_TYPE:-git}" != "git" ] || aws s3 cp "s3://$GRAPH_BUCKET/repos/$REPO_ID/latest/graphify-out/graph.json" "$GRAPHIFY_OUT/graph.json" --only-show-errors || true',
                '[ "${SOURCE_TYPE:-git}" != "git" ] || aws s3 cp "s3://$GRAPH_BUCKET/repos/$REPO_ID/latest/graphify-out/manifest.json" "$GRAPHIFY_OUT/manifest.json" --only-show-errors || true',
                "\n".join(
                    [
                        'if [ "${SOURCE_TYPE:-git}" = "git" ]; then',
                        "set -e",
                        "git init -q /tmp/work/src",
                        'git -C /tmp/work/src remote add origin "$GIT_URL"',
                        # PAT rides as Basic auth; the username the server
                        # expects differs per provider.
                        'case "${PROVIDER:-github}" in',
                        "  github) AUTH_USER=x-access-token;;",
                        "  gitlab) AUTH_USER=oauth2;;",
                        "  bitbucket) AUTH_USER=x-token-auth;;",
                        '  *) AUTH_USER="${GIT_AUTH_USER:-git}";;',
                        "esac",
                        'if [ -n "${GIT_TOKEN:-}" ]; then',
                        '  B64=$(printf \'%s:%s\' "$AUTH_USER" "$GIT_TOKEN" | base64 | tr -d \'\\n\')',
                        '  git -C /tmp/work/src config http.extraHeader "Authorization: Basic ${B64}"',
                        "fi",
                        "cd /tmp/work/src",
                        # Preferred: fetch the pinned SHA directly (GitHub/GitLab/
                        # Gitea advertise allow-reachable-sha1-in-want). Servers
                        # without it fall back to fetching the branch ref and
                        # checking the SHA out, deepening once if the branch
                        # advanced between poll and build.
                        'if git -c protocol.version=2 fetch -q --depth 1 origin "$TARGET_SHA" 2>/dev/null; then',
                        "  git checkout -q FETCH_HEAD",
                        "else",
                        '  echo "bare-SHA fetch unavailable; falling back to ref fetch of ${GIT_REF:-HEAD}"',
                        '  git -c protocol.version=2 fetch -q --depth 1 origin "${GIT_REF:-HEAD}"',
                        '  git checkout -q "$TARGET_SHA" 2>/dev/null || { git -c protocol.version=2 fetch -q --depth 100 origin "${GIT_REF:-HEAD}"; git checkout -q "$TARGET_SHA"; }',
                        "fi",
                        "fi",
                    ]
                ),
                # files source: mirror the uploaded corpus + manifest hash.
                "\n".join(
                    [
                        'if [ "${SOURCE_TYPE:-git}" = "files" ]; then',
                        "set -e",
                        _fetch_script("fetch_uploads.py", "/tmp/fetch_uploads.py"),
                        "python /tmp/fetch_uploads.py",
                        "fi",
                    ]
                ),
                # url source: crawl into markdown, then compare the corpus
                # hash against the last PUBLISHED one — unchanged content
                # skips the rest of the build (the poller starts a crawl-build
                # every interval; this comparison is the change detection).
                "\n".join(
                    [
                        'if [ "${SOURCE_TYPE:-git}" = "url" ]; then',
                        "set -e",
                        _fetch_script("docs_crawler.py", "/tmp/docs_crawler.py"),
                        "python /tmp/docs_crawler.py",
                        # The skip fingerprint covers the crawl content AND the
                        # other build inputs (graphify version, prune config) —
                        # otherwise a prune_paths change or a graphify bump on
                        # an unchanged site would be skipped forever.
                        # llm rides in the fingerprint so toggling LLM_EXTRACT
                        # re-extracts an unchanged crawl instead of self-SKIPping.
                        # viz=<n> is the explorer-bundle format version: bumping
                        # it re-derives every url source's bundle once instead
                        # of leaving unchanged sites SKIPping forever without one.
                        # It sits BEFORE llm= because the LLM-fallback marker below
                        # rewrites the fingerprint with an end-anchored `|llm=1$`.
                        'printf \'%s|graphifyy=%s|prune=%s|viz=1|model=%s|llm=%s\' "$(cat /tmp/work/content_hash)" "$GRAPHIFY_VERSION" "${PRUNE_PATHS:-}" "${LLM_MODEL:-}" "${LLM_EXTRACT:-0}" > /tmp/work/source_fingerprint',
                        'PREV=$(aws s3 cp "s3://$GRAPH_BUCKET/repos/$REPO_ID/latest/source_hash" - 2>/dev/null || echo none)',
                        "CUR=$(cat /tmp/work/source_fingerprint)",
                        'if [ "$PREV" = "$CUR" ]; then',
                        '  echo "[build] crawl content unchanged ($CUR) — skipping graph rebuild"',
                        "  touch /tmp/work/SKIP",
                        "fi",
                        "fi",
                    ]
                ),
                # Prune non-core directories BEFORE extraction. A very
                # large repo (e.g. LiteLLM: 56% of its 159k graph nodes
                # were tests/) produces a graph.json too big to hold in
                # the query task's memory. PRUNE_PATHS (space-separated
                # repo-relative dirs, from the registry row) shrinks the
                # graph AND the code-search snapshot to fit. Empty for
                # normal repos, so they are unaffected. Applies to every
                # source type (part of the build contract). MUST stay one
                # joined command: CodeBuild runs each list entry as its own
                # shell input, so a split `for`/`done` is a syntax error.
                "\n".join(
                    [
                        'for d in ${PRUNE_PATHS:-}; do',
                        '  case "$d" in /*|*..*) echo "skip unsafe prune path: $d"; continue;; esac',
                        '  rm -rf "/tmp/work/src/$d" && echo "pruned $d"',
                        "done",
                    ]
                ),
            ],
        },
        "build": {
            "commands": [
                # source_file paths are relative to the invocation CWD, and they
                # are every citation the AI client sees — always cd into the repo.
                "\n".join(
                    [
                        'if [ "${SOURCE_TYPE:-git}" = "git" ]; then',
                        "cd /tmp/work/src && graphify extract . --code-only --timing || echo 'extract exited non-zero; checking output'",
                        "fi",
                    ]
                ),
                "\n".join(
                    [
                        'if [ "${SOURCE_TYPE:-git}" = "git" ]; then',
                        "set -e",
                        "BUILT=$(python -c \"import json;print(json.load(open('$GRAPHIFY_OUT/graph.json')).get('built_at_commit',''))\" 2>/dev/null || echo none)",
                        'if [ "$BUILT" != "$TARGET_SHA" ]; then',
                        '  echo "built_at_commit=$BUILT != $TARGET_SHA -> forced full rebuild"',
                        "  cd /tmp/work/src && GRAPHIFY_FORCE=1 graphify extract . --code-only --force --timing",
                        "fi",
                        "fi",
                    ]
                ),
                # Non-git: full forced extract with markdown on the no-LLM
                # quick-scan lane. built_at_commit does not exist without git,
                # so the git-only verify/retry above never applies here.
                # files corpora first get office/PDF documents converted to
                # markdown sidecars (a PDF-only corpus is otherwise all
                # "papers" -> zero nodes -> failed build).
                "\n".join(
                    [
                        'if [ "${SOURCE_TYPE:-git}" = "files" ]; then',
                        "set -e",
                        _fetch_script("convert_docs.py", "/tmp/convert_docs.py"),
                        "python /tmp/convert_docs.py",
                        "fi",
                    ]
                ),
                "\n".join(
                    [
                        'if [ "${SOURCE_TYPE:-git}" != "git" ] && [ ! -f /tmp/work/SKIP ]; then',
                        "set -e",
                        _fetch_script("docs_extract_driver.py", "/tmp/docs_extract_driver.py"),
                        "cd /tmp/work/src",
                        'if [ "${LLM_EXTRACT:-0}" = "1" ]; then',
                        # Bedrock Sonnet 5 semantic pass over the DOCUMENT
                        # corpus. Sidecar .md files are the single doc source:
                        # originals (and images — the vision path would send
                        # them to Bedrock too) would double the token spend,
                        # so drop them; they stay in S3 uploads/, and
                        # read_source refuses binaries anyway.
                        '  find . -type f \\( -iname "*.pdf" -o -iname "*.docx" -o -iname "*.xlsx" -o -iname "*.bmp" -o -iname "*.tiff" \\) -delete',
                        # Raster images (png/jpg/gif/webp): graphify's vision
                        # path sends each one to Bedrock as an image block
                        # (~1.6k tokens apiece, 5 MB per-image ceiling, 20 per
                        # request) and mints a node linked to the pages that
                        # reference it. Off by default (LLM_IMAGES=0 deletes
                        # them — the historical behavior). On, a corpus above
                        # the image cap (600, uploaded + the ≤300 convert_docs
                        # pulls out of PDFs) is still extracted text-only so one
                        # screenshot-heavy upload cannot run away with the
                        # bill; the decision depends only on content, so the
                        # published hash stays stable (no retry loop).
                        '  IMG_COUNT=$(find . -type f \\( -iname "*.png" -o -iname "*.jpg" -o -iname "*.jpeg" -o -iname "*.gif" -o -iname "*.webp" \\) | wc -l | tr -d " ")',
                        '  if [ "${LLM_IMAGES:-0}" = "1" ] && [ "$IMG_COUNT" -gt 600 ]; then echo "LLM images skipped: $IMG_COUNT images > 600 cap; extracting text only"; fi',
                        '  if [ "${LLM_IMAGES:-0}" != "1" ] || [ "$IMG_COUNT" -gt 600 ]; then find . -type f \\( -iname "*.png" -o -iname "*.jpg" -o -iname "*.jpeg" -o -iname "*.gif" -o -iname "*.webp" \\) -delete; elif [ "$IMG_COUNT" -gt 0 ]; then echo "LLM images: $IMG_COUNT raster image(s) go through the vision path"; fi',
                        # Pre-flight corpus cap: a doc set too big to extract
                        # inside the 60-min build (and a runaway token bill)
                        # deterministically uses the quick-scan instead. The
                        # decision depends only on content, so the llm=1
                        # fingerprint stays — same content, same outcome, no
                        # every-tick retry loop.
                        "  DOC_BYTES=$(find . -type f -name '*.md' -print0 | xargs -0 stat -c%s 2>/dev/null | awk '{s+=$1} END {print s+0}')",
                        # Cap is per source (registry llm_corpus_cap_mb, default 64 MB
                        # ≈ 16M input tokens); the old fixed 4 MB silently downgraded
                        # a 168-PDF corpus to the quick-scan.
                        '  CAP_BYTES=$(( ${LLM_CORPUS_CAP_MB:-64} * 1048576 ))',
                        '  echo "LLM corpus: ${DOC_BYTES}B of markdown (cap ${LLM_CORPUS_CAP_MB:-64}MB)"',
                        '  if [ "${DOC_BYTES:-0}" -gt "$CAP_BYTES" ]; then',
                        '    echo "LLM extract skipped: markdown corpus ${DOC_BYTES}B > ${LLM_CORPUS_CAP_MB:-64}MB cap; using the quick-scan"',
                        "    python /tmp/docs_extract_driver.py . --code-only --force --timing",
                        "  else",
                        # Sonnet 5 rejects the temperature parameter
                        # (deprecated) -> "none" omits it.
                        '    export GRAPHIFY_BEDROCK_MODEL="${LLM_MODEL:-global.anthropic.claude-sonnet-5}"',
                        '    echo "LLM model: $GRAPHIFY_BEDROCK_MODEL (images=${LLM_IMAGES:-0})"',
                        # graphify's semantic cache is keyed by file content +
                        # prompt, NOT by model — one shared tarball would replay
                        # Sonnet output under an Opus build. Namespace the S3
                        # archive per model; the default keeps the legacy key so
                        # existing caches stay warm.
                        '    LLMCACHE_KEY="llmcache.tar.gz"; if [ -n "${LLM_MODEL:-}" ] && [ "$LLM_MODEL" != "global.anthropic.claude-sonnet-5" ]; then LLMCACHE_KEY="llmcache-$(printf %s "$LLM_MODEL" | tr -c "a-zA-Z0-9" _).tar.gz"; fi',
                        "    export GRAPHIFY_LLM_TEMPERATURE=none",
                        # Warm the semantic cache (content+prompt keyed) so a
                        # re-crawl/rebuild only pays LLM tokens for CHANGED
                        # documents. No --force here: --force skips cache
                        # reads, and the clean-room workspace has no baseline
                        # manifest so the scan is full either way.
                        '    (aws s3 cp "s3://$GRAPH_BUCKET/repos/$REPO_ID/$LLMCACHE_KEY" /tmp/llmcache.tar.gz --only-show-errors && mkdir -p "$GRAPHIFY_OUT" && tar -xzf /tmp/llmcache.tar.gz -C "$GRAPHIFY_OUT") || echo "no llm cache yet (first run)"',
                        # I/O-bound Bedrock calls: more workers than the AST default
                        # of 2 (graphify retries throttles with backoff).
                        "    export GRAPHIFY_MAX_WORKERS=6",
                        # A build that CodeBuild kills at its timeout never reaches
                        # the cache upload below — checkpoint the semantic cache to
                        # S3 every 10 minutes so the retry only pays for what is
                        # left (a partially-written entry is just a cache miss).
                        '    ( while sleep 600; do (tar -czf /tmp/llmcache.sync.tar.gz -C "$GRAPHIFY_OUT" cache 2>/dev/null && aws s3 cp /tmp/llmcache.sync.tar.gz "s3://$GRAPH_BUCKET/repos/$REPO_ID/$LLMCACHE_KEY" --only-show-errors && echo "llm cache checkpoint saved") || true; done ) &',
                        "    SYNC_PID=$!",
                        "    set +e",
                        # --token-budget halves graphify's 60k-token input chunks:
                        # a chunk's JSON output scales with its input, and dense
                        # regulation text overran even a 32k output cap (13
                        # truncations → recursive bisects → partial results).
                        "    graphify extract . --backend bedrock --token-budget 30000 --timing 2>&1 | tee /tmp/llmextract.log",
                        '    LLM_RC=${PIPESTATUS[0]}',
                        "    set -e",
                        '    kill "$SYNC_PID" 2>/dev/null || true',
                        # Save the cache on EVERY outcome: a failure at chunk
                        # 900/1000 must not re-bill the 900 finished chunks on
                        # the retry.
                        '    (tar -czf /tmp/llmcache.tar.gz -C "$GRAPHIFY_OUT" cache && aws s3 cp /tmp/llmcache.tar.gz "s3://$GRAPH_BUCKET/repos/$REPO_ID/$LLMCACHE_KEY" --only-show-errors) || echo "llm cache save failed (non-fatal)"',
                        '    if [ "$LLM_RC" != "0" ]; then',
                        # LLM failure must NOT fail the build (this platform
                        # retries failed builds forever): fall back to the
                        # deterministic quick-scan so the graph still ships.
                        '      echo "LLM extract failed (rc=$LLM_RC); falling back to the no-LLM quick-scan"',
                        "      python /tmp/docs_extract_driver.py . --code-only --force --timing",
                        "    fi",
                        # A fallback OR a partial semantic failure must not be
                        # frozen behind an llm=1 hash: mark the published hash
                        # so the next poll tick re-attempts the LLM path (the
                        # warm cache makes the retry pay only for what failed).
                        '    if [ "$LLM_RC" != "0" ] || grep -q "semantic chunk(s) failed" /tmp/llmextract.log; then',
                        '      if [ "${SOURCE_TYPE:-git}" = "url" ]; then',
                        '        sed -i "s/|llm=1$/|llm=fallback/" /tmp/work/source_fingerprint',
                        "      else",
                        '        printf \'llmfallback-%s\' "$(cat /tmp/work/content_hash)" > /tmp/work/content_hash.new && mv /tmp/work/content_hash.new /tmp/work/content_hash',
                        "      fi",
                        '      echo "published hash marked for LLM retry on the next tick"',
                        "    fi",
                        "  fi",
                        "else",
                        "  python /tmp/docs_extract_driver.py . --code-only --force --timing",
                        "fi",
                        "fi",
                    ]
                ),
            ],
        },
        "post_build": {
            "commands": [
                '[ -f /tmp/work/SKIP ] || test -s "$GRAPHIFY_OUT/graph.json"',
                '[ "${SOURCE_TYPE:-git}" != "git" ] || python -c "import json;d=json.load(open(\'$GRAPHIFY_OUT/graph.json\'));assert d.get(\'built_at_commit\')==\'$TARGET_SHA\',\'built_at_commit mismatch\';print(\'nodes:\',len(d.get(\'nodes\',[])),\'edges:\',len(d.get(\'links\',[])))"',
                # Non-git equivalent of the assertion: the graph must exist and
                # be non-empty (an empty corpus must fail, not publish nothing).
                "\n".join(
                    [
                        'if [ "${SOURCE_TYPE:-git}" != "git" ] && [ ! -f /tmp/work/SKIP ]; then',
                        "python -c \"import json;d=json.load(open('$GRAPHIFY_OUT/graph.json'));n=len(d.get('nodes',[]));assert n>0,'empty graph';print('nodes:',n,'edges:',len(d.get('links',[])))\"",
                        "fi",
                    ]
                ),
                # GRAPH_REPORT.md + .graphify_labels.json (community names +
                # the graphify://report MCP resource). Best-effort. LLM builds
                # also NAME the communities via Sonnet 5 (a handful of calls);
                # everything else keeps --no-label.
                "\n".join(
                    [
                        "if [ ! -f /tmp/work/SKIP ]; then",
                        'if [ "${LLM_EXTRACT:-0}" = "1" ] && [ "${SOURCE_TYPE:-git}" != "git" ]; then',
                        '(cd /tmp/work/src && GRAPHIFY_BEDROCK_MODEL="${LLM_MODEL:-global.anthropic.claude-sonnet-5}" GRAPHIFY_LLM_TEMPERATURE=none graphify cluster-only . --backend bedrock) || echo \'cluster-label failed (non-fatal)\'',
                        "else",
                        "(cd /tmp/work/src && graphify cluster-only . --no-label) || echo 'cluster-only failed (non-fatal)'",
                        "fi",
                        "fi",
                    ]
                ),
                '[ -f /tmp/work/SKIP ] || aws s3 cp "$GRAPHIFY_OUT/graph.json" "s3://$GRAPH_BUCKET/repos/$REPO_ID/latest/graphify-out/graph.json" --only-show-errors',
                '[ -f /tmp/work/SKIP ] || aws s3 cp "$GRAPHIFY_OUT/manifest.json" "s3://$GRAPH_BUCKET/repos/$REPO_ID/latest/graphify-out/manifest.json" --only-show-errors || true',
                '[ -f /tmp/work/SKIP ] || aws s3 cp "$GRAPHIFY_OUT/GRAPH_REPORT.md" "s3://$GRAPH_BUCKET/repos/$REPO_ID/latest/graphify-out/GRAPH_REPORT.md" --only-show-errors || true',
                '[ -f /tmp/work/SKIP ] || aws s3 cp "$GRAPHIFY_OUT/.graphify_labels.json" "s3://$GRAPH_BUCKET/repos/$REPO_ID/latest/graphify-out/.graphify_labels.json" --only-show-errors || true',
                # Console graph-explorer bundle (compact + precomputed layout):
                # after cluster-only so LLM community labels are picked up, and
                # AFTER graph.json so the bundle is never newer than the graph
                # it describes (the API treats a much older bundle as stale).
                # viz-meta.json is uploaded last as the commit marker.
                # Best-effort: never fails the build.
                "\n".join(
                    [
                        "if [ ! -f /tmp/work/SKIP ]; then",
                        "(",
                        "set -e",
                        _fetch_script("make_viz.py", "/tmp/make_viz.py"),
                        _publish_viz("$GRAPHIFY_OUT/graph.json", "/tmp/work/viz", "$REPO_ID",
                                     "s3://$GRAPH_BUCKET/repos/$REPO_ID/latest/graphify-out", src_dir="/tmp/work/src"),
                        # On failure drop the PREVIOUS build's bundle too: a stale
                        # picture under a fresh graph.json is worse than the
                        # console's raw-graph fallback.
                        ") || { echo 'viz bundle failed (non-fatal)'; aws s3 rm \"s3://$GRAPH_BUCKET/repos/$REPO_ID/latest/graphify-out/viz.json\" --only-show-errors || true; aws s3 rm \"s3://$GRAPH_BUCKET/repos/$REPO_ID/latest/graphify-out/viz-meta.json\" --only-show-errors || true; }",
                        "fi",
                    ]
                ),
                # Top-level history/ prefix so the bucket lifecycle rules apply.
                # Non-git builds have no commit — the content hash is the
                # version id.
                "\n".join(
                    [
                        "if [ ! -f /tmp/work/SKIP ]; then",
                        "set -e",
                        'HIST_ID="$TARGET_SHA"',
                        'if [ "${SOURCE_TYPE:-git}" != "git" ]; then HIST_ID=$(cat /tmp/work/content_hash); fi',
                        'aws s3 cp "$GRAPHIFY_OUT/graph.json" "s3://$GRAPH_BUCKET/history/$REPO_ID/$HIST_ID/graph.json" --only-show-errors',
                        "fi",
                    ]
                ),
                # Source snapshot for the runtime's code-search tools
                # (search_code / read_source). Capped so a huge monorepo can't
                # blow the runtime's /tmp; best-effort — without a snapshot the
                # tools report "unavailable" instead of failing the build.
                # The hub (__all__) never gets one: snapshots exist only under
                # per-repo prefixes, and the hub runtime doesn't sync them.
                "\n".join(
                    [
                        "if [ ! -f /tmp/work/SKIP ]; then",
                        "(",
                        "set -e",
                        "cd /tmp/work/src",
                        # Raster images never serve search_code/read_source (binaries are
                        # refused) — keep them out so an image-heavy LLM corpus does
                        # not push the snapshot over the cap.
                        "tar --exclude-vcs --exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.gif' --exclude='*.webp' -czf /tmp/work/src.tar.gz .",
                        'SZ=$(stat -c%s /tmp/work/src.tar.gz)',
                        'if [ "$SZ" -le 209715200 ]; then',
                        '  aws s3 cp /tmp/work/src.tar.gz "s3://$GRAPH_BUCKET/repos/$REPO_ID/latest/src.tar.gz" --only-show-errors',
                        '  echo "src snapshot uploaded (${SZ} bytes)"',
                        "else",
                        '  echo "src snapshot skipped (${SZ} bytes > 200MB cap)"',
                        "fi",
                        ") || echo 'src snapshot failed (non-fatal)'",
                        "fi",
                    ]
                ),
                # Publish the content hash LAST so it only ever describes a
                # fully published graph; the completion Lambda reads it into
                # last_built_sha for non-git sources. files: the pure manifest
                # hash (MUST match the poller's files_manifest_hash). url: the
                # skip fingerprint (content|graphifyy=..|prune=..) — the poller
                # never string-compares it, only this build's skip check does.
                "\n".join(
                    [
                        'if [ "${SOURCE_TYPE:-git}" != "git" ] && [ ! -f /tmp/work/SKIP ]; then',
                        "set -e",
                        # files: the published hash carries the LLM knobs so a
                        # settings change (images/model/on-off) is a "change" to
                        # the poller — the field order matches url's `|llm=` last
                        # so the fallback marker stays end-anchored. The poller's
                        # files branch appends the SAME suffix to its listing hash.
                        'if [ "${SOURCE_TYPE:-git}" = "files" ]; then printf \'%s|img=%s|model=%s|llm=%s\' "$(cat /tmp/work/content_hash)" "${LLM_IMAGES:-0}" "${LLM_MODEL:-}" "${LLM_EXTRACT:-0}" > /tmp/work/source_fingerprint; fi',
                        "SRC_HASH_FILE=/tmp/work/content_hash",
                        'if [ "${SOURCE_TYPE:-git}" != "git" ]; then SRC_HASH_FILE=/tmp/work/source_fingerprint; fi',
                        'aws s3 cp "$SRC_HASH_FILE" "s3://$GRAPH_BUCKET/repos/$REPO_ID/latest/source_hash" --only-show-errors',
                        "fi",
                    ]
                ),
                # Refresh the merged all-repos graph the hub runtime serves as
                # its default (repos/__all__/...). merge-graphs prefixes node
                # ids with a per-repo tag, so one query spans every repo.
                # Best-effort: concurrent builds race last-writer-wins, and
                # each writer merges the freshest set it can see.
                # NOT gated on /tmp/work/SKIP: the merge's MEMBERSHIP comes
                # from the registry, and a graph_scope flip must reach the hub
                # even when this build's own content was unchanged (a url
                # source's every rebuild would otherwise SKIP forever and a
                # now-private graph would linger in __all__ indefinitely).
                "\n".join(
                    [
                        "(",
                        "set -e",
                        "MERGE_DIR=/tmp/work/merge; mkdir -p \"$MERGE_DIR\"",
                        # Membership comes from the REGISTRY (enabled repos), not
                        # S3 listing — a deregistered repo drops out of __all__
                        # on the next build even when its graphs are retained.
                        # graph_scope filter is FAIL-CLOSED: only rows explicitly
                        # marked public join the shared hub graph. A private
                        # (PAT-cloned) repo must never leak into other tenants'
                        # merged view, so a row missing the attribute is excluded.
                        'for rid in $(aws dynamodb query --table-name "$REGISTRY_TABLE" --index-name due-index --key-condition-expression "enabled = :e" --filter-expression "graph_scope = :pub" --expression-attribute-values \'{":e":{"S":"1"},":pub":{"S":"public"}}\' --query \'Items[].repo_id.S\' --output text); do',
                        '  [ "$rid" = "None" ] && continue',
                        # Stage in the <repo>/graphify-out/graph.json layout so
                        # merge-graphs derives the repo TAG from the directory
                        # name (flat files would all tag as "tmp_work-N").
                        '  mkdir -p "$MERGE_DIR/$rid/graphify-out"',
                        '  aws s3 cp "s3://$GRAPH_BUCKET/repos/$rid/latest/graphify-out/graph.json" "$MERGE_DIR/$rid/graphify-out/graph.json" --only-show-errors || rm -rf "$MERGE_DIR/$rid"',
                        # merge-graphs refuses any input above graphify's 512 MiB
                        # graph cap and aborts the WHOLE merge (one huge repo
                        # took the hub down). Skip oversize graphs instead — the
                        # repo keeps its dedicated server; only the hub omits it.
                        '  if [ -f "$MERGE_DIR/$rid/graphify-out/graph.json" ] && [ "$(stat -c %s "$MERGE_DIR/$rid/graphify-out/graph.json")" -gt 536870912 ]; then echo "hub merge: skipping $rid (graph.json exceeds the 512 MiB merge cap)"; rm -rf "$MERGE_DIR/$rid"; fi',
                        "done",
                        'GRAPHS=$(ls "$MERGE_DIR"/*/graphify-out/graph.json 2>/dev/null | wc -l)',
                        'if [ "$GRAPHS" -ge 2 ]; then',
                        '  graphify merge-graphs "$MERGE_DIR"/*/graphify-out/graph.json --out /tmp/work/all_graph.json',
                        'elif [ "$GRAPHS" -eq 1 ]; then',
                        # Single repo: prefix it the same way merge-graphs would,
                        # so node-id shape never changes when a second repo lands.
                        '  python - "$MERGE_DIR" /tmp/work/all_graph.json <<\'PY\'',
                        "import json, pathlib, sys",
                        "from networkx.readwrite import json_graph as jg",
                        "from graphify.build import prefix_graph_for_global",
                        "src = next(pathlib.Path(sys.argv[1]).glob('*/graphify-out/graph.json'))",
                        "G = jg.node_link_graph(json.load(open(src)))",
                        "tagged = prefix_graph_for_global(G, src.parent.parent.name)",
                        "json.dump(jg.node_link_data(tagged), open(sys.argv[2], 'w'))",
                        "PY",
                        'elif [ "$GRAPHS" -eq 0 ]; then',
                        # Zero public repos left (e.g. the last one flipped
                        # private): the hub must serve an EMPTY graph, not the
                        # stale pre-flip merge.
                        '  printf \'%s\' \'{"directed": false, "multigraph": false, "graph": {}, "nodes": [], "links": []}\' > /tmp/work/all_graph.json',
                        "fi",
                        'test -s /tmp/work/all_graph.json',
                        'aws s3 cp /tmp/work/all_graph.json "s3://$GRAPH_BUCKET/repos/__all__/latest/graphify-out/graph.json" --only-show-errors',
                        'echo "merged $GRAPHS repo graph(s) into repos/__all__"',
                        # Hub explorer bundle (merged nodes carry repo/local_id;
                        # make_viz.py emits a repos dictionary for them).
                        "(",
                        "set -e",
                        '[ -f /tmp/make_viz.py ] || ' + _fetch_script("make_viz.py", "/tmp/make_viz.py"),
                        _publish_viz("/tmp/work/all_graph.json", "/tmp/work/viz_all", "all",
                                     "s3://$GRAPH_BUCKET/repos/__all__/latest/graphify-out"),
                        ") || { echo 'hub viz bundle failed (non-fatal)'; aws s3 rm \"s3://$GRAPH_BUCKET/repos/__all__/latest/graphify-out/viz.json\" --only-show-errors || true; aws s3 rm \"s3://$GRAPH_BUCKET/repos/__all__/latest/graphify-out/viz-meta.json\" --only-show-errors || true; }",
                        ") || echo 'all-repos merge failed (non-fatal)'",
                    ]
                ),
            ]
        },
    },
}
