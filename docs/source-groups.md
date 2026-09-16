# Source groups

The console's **Source groups / 소스 그룹** tab combines frontend/backend
repositories and planning/QA documents into a private project graph. It reuses
existing source graphs and adds cross-source relations with two exact source
quotes. The source-level SVG shows these stored relations, not description
similarity drawn as if it were an observed connection.

## Use the console

1. Register sources with optional shared descriptions. A description on a
   public source is public context; only its owner may edit it.
2. Build each source once with this release to produce an immutable graph and
   source snapshot. Existing sources without a version show a rebuild-required
   error. A manual URL rebuild now repairs version artifacts even when the
   crawl content did not change.
3. Create a group, select up to eight sources, assign roles and describe each
   source's role within the project. Group descriptions do not change source
   visibility.
4. Start the group build. Inspect its relationships and click the evidence to
   read the exact file version. Only ready groups appear in MCP Servers and
   the Playground picker.
5. Optionally enable AI relationship analysis. The default is explicit
   references only; the model default is Claude Sonnet 5. Descriptions help
   select candidates but do not prove implementation or test coverage.

**My Sources / 내 소스** also lists groups separately from individual sources.
Expand a group to see its members and roles, then open its detail view, full
graph or connection guide. Groups still building or requiring access recovery
remain visible with their current status.

Owners can edit or delete directly from My Sources, the group-management list,
or the selected group's heading. Editors can edit; viewers see an explanation
of their read-only role. Each management action fetches current metadata and
checks the role again before opening the form or deletion confirmation.
Management does not require a successful graph read. Edits can change the
group name, description, source membership and group-specific roles/context.
Deleting a group preserves the original sources.

The full graph explorer includes a group picker, source-based colors and
filters, source names on focused nodes, and a node inspector with an origin
source badge and directly connected source counts. Neighbor rows also identify
their sources. Cross-source edges use source colors; same-source edges remain
gray, with inferred edges thinner and lighter. The connection summary counts
the full loaded graph, including connections hidden by the current filters.
Graph pages and source
reads use authenticated management APIs and one pinned group version. A source
change or lost access clears the displayed group data. Group-only sources can
read source text through a ready group that contains the exact source version;
they do not require their own MCP runtime.

The connection guide supports ordinary sources and groups with a selected
target throughout API-key creation, client setup and Playground verification.
It provides Claude Code commands, Cursor MCP JSON and VS Code `.vscode/mcp.json`.
Group client names derive from stable group IDs instead of display names.
Groups run on demand in Lambda. A group-only source points to its available
group connections, and an unavailable selection never silently widens an
API key's scope to all servers.

Explicit signals include requirement/test-case identifiers, qualified HTTP
method+route contracts, and file/symbol references. For example, matching
`REQ-100` proves a shared reference, not that one implementation correctly
satisfies the requirement. Optional Bedrock judgments remain `INFERRED`, with
validated quotes and an unreviewed state.

Korean particles attached to an identifier (`REQ-100을`, `TC-100으로`) are
recognized without matching parts of larger ASCII identifiers. Models return
short candidate IDs and evidence indices; the server attaches the original
verified quotes. Final text is parsed separately from non-text reasoning
blocks. Invalid model output remains `PARTIAL`; successfully validated pairs
are retained for a bounded explicit retry. An AI judgment and an explicit
reference may describe the same connection, so their counts are not a measure
of independent relationships or accuracy.

Group invitations create group grants only. Every reader must also retain
access to every source. An owner who loses source access receives minimal
recovery metadata and can remove inaccessible sources; derived graph content,
source quotes, and MCP endpoints remain blocked.

## Implementation

| Component | Responsibility |
|---|---|
| `lambdas/platform_api/groups_api.py` | Group CRUD, roles/descriptions, grants, revision CAS and idempotent build dispatch |
| `lambdas/shared/python/group_access.py` | Strongly consistent group/source authorization and current-version checks |
| `lambdas/group_worker/` | Asynchronous Lambda graph composition, evidence validation, optional bounded Bedrock calls, immutable publication, reconciliation and garbage collection |
| `lambdas/shared/python/group_query.py` | Group MCP, paginated graph reads and exact versioned source reads |
| `cdk/build_scripts/publish_source_version.py` | Same-build graph/snapshot publication and conditional registry version activation |
| `console/groups.js`, `groups.css` | Group editor, source relationship view, evidence reader and status/usage |
| `console/index.html`, `graph.js`, `graph.css` | My Sources group listing, scoped connection guide, full group graph and pinned source viewer |

Group queries run inside the existing proxy Lambda, reading S3 through its
existing gateway endpoint. There is no per-group Fargate service or new NAT
gateway. The management API uses the same query module; private group graphs
are paginated through authenticated APIs instead of presigned public bearer
URLs. Python helpers ship in a shared Lambda layer.

`query_graph` searches both node metadata and stored relationship signals and
quotes. A requirement identifier such as `REQ-100` can therefore find the
connected endpoints even when no heading or function has that identifier as
its name. Results include matching relations and adjacent graph links.

Source versions live at `source-versions/<source>/<version>/`; group graphs,
manifests and selected text copies at `groups/<group>/versions/<build>/`.
The manifest pins file checksums and source versions. Missing/corrupt versions
produce errors rather than falling back to mutable `latest` source text.

Every mutation uses the expected group revision. Build reservations and worker
leases prevent duplicate dispatch/execution; activation rechecks source
versions, grants and the build claim with a DynamoDB transaction. Ledger status
and confirmed usage are saved with completion. Deleted groups are tombstoned,
so late workers cannot reactivate them.

An existing built group is checked for changed source versions or descriptions
on the five-minute reconciliation schedule. One automatic attempt is allowed
per changed input signature; failures require explicit retry rather than
continuous model spending. Draft groups are not automatically built.
Discovery queries the sparse `GROUPS` partition of the existing `entity-index`;
it does not scan unrelated usage records. Index entries are never authority:
metadata, source versions and grants are reread from the base tables.

## Cost and limits

Group creation adds metadata, not always-on compute. Group builds spend Lambda
time and S3 requests/storage; opted-in AI analysis also spends Bedrock tokens.
Source rebuilds still have the platform's normal CodeBuild/extraction cost.
Unchanged source pairs reuse validated group-local decisions. The UI shows
observed tokens, model calls and reuse; calls with unavailable usage are counted
separately. Reserved tokens are limits, not billed token measurements.

| Limit | Current value |
|---|---:|
| Sources per group | 8 |
| Graph | 32 MiB, 50,000 nodes, 200,000 links |
| Text evidence | 2 MiB/file, 32 MiB/group |
| Manifest | 4 MiB |
| Compressed source snapshot | 200 MiB/source |
| Expanded snapshot | 256 MiB/source |
| Candidate relationships | 1,000 |
| Reserved model input/output | 200,000 / 40,000 tokens/build |
| Worker | 15 minutes; two concurrent Lambda executions |
| Source read | 400 lines |
| Serialized query response | 1 MiB; graph pages adapt their size |

Known binary originals such as PDF/Office/image files are skipped for text
evidence; their converted Markdown is used. Archives reject unsafe links and
path traversal. Limits and incomplete model output are explicit failures or
`PARTIAL` results, never represented as a complete analysis.

Scheduled garbage collection preserves active and in-flight versions and
removes obsolete artifacts older than one hour. Scans are paginated and
bounded per tick. S3 noncurrent versions under the two new prefixes expire
after one day; active objects have no blanket expiration. Source and group
deletion blocks access immediately, while physical storage reclamation is
asynchronous. Original source uploads are not deleted when a group is removed.

## Verification and deployment

```bash
PYTHONPATH=lambdas/shared/python .venv/bin/python -B -m unittest discover -s tests -p 'test_group*.py'
.venv/bin/python -B -m unittest discover -s tests -p 'test_source_versions.py'
node --test tests/test_group_graph_ui.cjs
```

For browser navigation, use an existing Playwright installation and Chrome:

```bash
GRAPHIFY_PLAYWRIGHT_MODULE=/path/to/existing/playwright node tests/test_console_group_navigation.cjs
```

The browser suite uses isolated API/session fixtures, checks group navigation
and client configuration, and captures desktop/mobile screenshots. It does
not create users, issue real keys or call AWS. Live API authorization and graph
publication must be checked against the deployed stack separately.

Deploy the CDK stack, shared layer, Lambda code, build scripts and console
together. Use the existing stack name. The optional
`-c runtime_image_uri=<ECR URI@sha256:digest>` retains a verified existing
runtime image during a management-only release; omit it when intentionally
rolling runtime code.

The original [design plan](source-groups-plan.ko.md) remains useful context.
This release uses on-demand Lambda group queries instead of a dedicated
runtime, and stores membership atomically in group metadata. Personal notes
are not copied into shared group context.
