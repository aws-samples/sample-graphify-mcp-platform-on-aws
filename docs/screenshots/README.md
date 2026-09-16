# README screenshot provenance

The `guide-*.en.png` and `guide-*.ko.png` images illustrate the console using a sanitized demo replay. Ready states, source metadata, timestamps, and graph counts describe that demo. They are not evidence of a live deployment, OCR accuracy, model quality, throughput, or benchmark results.

Captured on **2026-09-17, Asia/Seoul**, from base repository code at **`c9b106afea4e18379ed18d9028a126e04b6ccbbb`** plus a local copy-only correction to the three file-registration hint strings in `console/index.html`, using installed Chrome **153.0.8010.47**. The correction describes selected visibility and the two switches required for PDF body OCR. Every browser context used a **1440 CSS pixel desktop viewport**, device scale factor 1, and the console's existing fonts and styles. Image dimensions vary because modal windows and relevant page regions are cropped.

The captured `console/index.html` SHA-256 is:

```text
2f09afba880ef7ae3de26eef5ef6f712eaaf825181c6ac662257c2271bd04389
```

The [capture helper](../../scripts/readme/capture.cjs) serves the current `console/` HTML, CSS, and JavaScript bytes unchanged. The graph uses the actual Sigma WebGL renderer, and forms use the current native dialog components. The helper does not edit application source files. PNG image data is recompressed losslessly with Node's built-in zlib.

| View | English image | Korean image | Captured state |
| --- | --- | --- | --- |
| Sources | [guide-sources.en.png](guide-sources.en.png) | [guide-sources.ko.png](guide-sources.ko.png) | Ready demo group and the first two complete individual-source rows. The list reports four sources. Names sort independently in each language. |
| Register | [guide-register.en.png](guide-register.en.png) | [guide-register.ko.png](guide-register.ko.png) | File-folder registration dialog with document AI extraction and PDF body OCR plus image analysis enabled. Example server name: `pension-documents-demo`. The form is never submitted. |
| Source groups | [guide-groups.en.png](guide-groups.en.png) | [guide-groups.ko.png](guide-groups.ko.png) | Cross-source relationship diagram for the selected ready demo group. Cropped to the four source anchors and the beginning of the relation list. |
| Graph | [guide-graph.en.png](guide-graph.en.png) | [guide-graph.ko.png](guide-graph.ko.png) | Full graph, scope `all`, zero-based node 4 (`change_fund()`) selected, with the actual Simplified control enabled. The 1150 by 886 crop retains the canvas, origin badge, and all four connected-source cards. |
| Connect | [guide-connect.en.png](guide-connect.en.png) | [guide-connect.ko.png](guide-connect.ko.png) | Group connection guide with `https://mcp.example.com/v1/mcp/<group_id>` and the console's literal `<YOUR_KEY>` placeholder. No key is issued. |
| Build diagnostics | [guide-build.en.png](guide-build.en.png) | [guide-build.ko.png](guide-build.ko.png) | **Explicitly synthetic failure** for corrupt `demo-corrupt.pdf`, with `PdfReadError: EOF marker not found`. The error, phase message, build ID, and logs identify the fixture. No build was executed; displayed duration is illustrative. |
| Playground | [guide-playground.en.png](guide-playground.en.png) | [guide-playground.ko.png](guide-playground.ko.png) | Sonnet 4.6 selected, with seven group tools loaded through a local `tools/list` replay. The direct editor selects `query_graph` with `{"query":"REQ-102"}`. Run and Send are never pressed; no tool result or AI answer is shown. |

The [public demo fixture](../../scripts/readme/demo-fixture.json) contains four sources, 75 nodes, 126 edges, and 77 stored cross-source relations. It is derived from an existing four-source demo replay. Source names are the valid platform slugs `pension-api`, `pension-web`, `product-plan`, and `qa-scenarios` in both languages. Source descriptions and the group display name are localized, while source code, file names, requirement IDs, and relation evidence retain the demo's original language. The source excerpt is fictional pension sample code.

For both graph images, the helper checks the actual `#gx-simple` control. This display filter hides rationale nodes, structural edges such as `contains`, and inferred edges. The full node scope remains `all`; the canvas renders 65 of the 75 loaded nodes and 69 edges. No CSS hiding or fixture edits implement this filter. The connected-source cards summarize direct connections in the **full loaded graph**, including items hidden by the current view or filters. The helper checks that their four summaries and the loaded provenance remain unchanged after enabling Simplified.

The corrupt-PDF parsing failure follows the publication-blocking error behavior documented in [document source operations](../document-sources-ops.md). A PDF that requires an opening password has separate unsupported/partial handling. The synthetic build and log timestamps are September 16, 2026, at 10:00 AM KST through 10:00:12 AM KST. The helper verifies that they precede the actual Last fetched timestamp.

The Playground fixture is checked against the current `playground_models` list in [the CDK stack](../../cdk/graphify_stack.py): Sonnet 4.6, Opus 4.6, Opus 4.7, and Opus 4.8, with Sonnet 4.6 first and selected by default. Document extraction has its separate Sonnet 5 selection. The final connection and Playground pairs were recaptured after aligning the example `/v1` endpoint and Playground configuration.

The fixture uses `demo@example.com`, `example.com` subdomains, demo source and version IDs, and an all-zero example group ID ending in `1`. Real identities, account IDs, deployment endpoints, raw live configuration, credentials, and private-key material are excluded. Authored fixture content and captions contain no middle dots or em dashes. The final published fixture SHA-256 is:

```text
b19a3bc0ec3db8642f0c40d71d9b6d78994cfa681a87e71688e52adf840db51a
```

All browser requests are intercepted. Application data comes from the local fixture. The only accepted POST requests replay a version-pinned source read or group `tools/list`; all mutations, model requests, and tool executions are rejected. Source reads honor the requested line range and return matching excerpt checksums, so the graph's stale-response checks remain active. No real sign-in, AWS API call, or Bedrock invocation is used.

The helper can download the console's existing public CDN scripts and font files without credentials. JavaScript downloads must match the source HTML's SHA-384 integrity pins. These assets are cached outside the repository and served back through interception. It adds no package dependencies.

To reproduce, run from the repository root with Node, installed Chrome, and an existing Playwright package:

```sh
GRAPHIFY_PLAYWRIGHT_MODULE=/absolute/path/to/existing/node_modules/playwright \
GRAPHIFY_README_LOG_DIR="${TMPDIR:-/tmp}/graphify-readme-capture" \
node scripts/readme/capture.cjs
```

Optional environment variables select a subset:

```sh
GRAPHIFY_README_LANGS=en,ko \
GRAPHIFY_README_SHOTS=graph,playground \
GRAPHIFY_PLAYWRIGHT_MODULE=/absolute/path/to/existing/node_modules/playwright \
node scripts/readme/capture.cjs
```

The helper writes `capture-report.json`, visible-page text, and downloaded public assets into `GRAPHIFY_README_LOG_DIR`, which defaults to an OS temporary directory. The report records capture time, code SHA, console asset hashes, browser version, image dimensions and hashes, source reads, tool listings, and rejected requests. Regenerating after code changes captures the newer console and requires a fresh visual review.

This is a **multi-run validated set**. Eight delivered images come from the successful main capture run (`sources`, `register`, `groups`, and `graph`, both languages), two from the corrupt-PDF build recapture, and four from the endpoint/Playground configuration recapture. The external `final-capture-manifest.json` matches each of the fourteen current PNG hashes to its successful per-shot record and records that run's fixture hash. `final-verification.json` reports the consolidated checks. The four-image integration run alone does not validate all fourteen images.

Verification for the consolidated set passed: fourteen PNGs decoded and hash-matched, both languages, no horizontal page overflow, native modal dialogs, 75 loaded graph nodes, finite camera coordinates, a drawn source-qualified `change_fund()` label, and four visible connected-source cards. Simplified retains the selected node, the full node scope, and the full loaded source summaries while excluding rationale, structural, and inferred content from the rendered graph. Both graph crops were additionally inspected at 1000px display width. Each contributing run reported zero unexpected requests, JavaScript errors, model calls, tool executions, or mutations.

The registration screenshot demonstrates settings only; it does not demonstrate an OCR result. Font rasterization and fetch timestamps can vary between machines and captures.
