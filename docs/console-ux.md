# Console UX

The console prioritizes finding existing sources and safely managing them.
The existing indigo identity is retained: white `#FFFFFF` surfaces, slate
`#F8FAFC` background, ink `#0F172A`, muted text `#475569`, indigo `#4F46E5`
actions and red `#DC2626` destructive controls. The existing Plus Jakarta Sans
and Korean system font stack remain; identifiers alone use monospace.

Lists lead each management page. Registration, key issuance and invitations
open task modals from explicit entry points. Records align identity, status and actions in
wide columns. Supporting metadata and management actions use their own rows
instead of competing in narrow columns. On mobile, records stack with
visible field labels; editing and deletion remain visible.

```
Page title                        Add / Refresh
Search                    Filter / Sort
Record identity       Status        View actions
Description and metadata            Management actions
```

This is an operational console, so decoration and hover motion are secondary
to legibility, stable controls and visible focus. Search filters presentation
only, never authorization or the scopes available for API keys/Playground.
Read errors have retry actions and do not masquerade as empty lists.
Delayed panel responses must never switch the source a management action
applies to. Source deletion and subscription removal use distinct wording.

Verification covers admin/member permissions, list search/pagination,
registration entry, source management panels, key scopes, error recovery,
keyboard navigation and responsive layouts. Browser fixtures never send
invitations, reset passwords or delete real users/sources.

## Implemented behavior

- Source, catalog, key, user, file and source-member records use three
  columns with supporting metadata below the primary identifier. Desktop
  lists scroll within a labeled region; mobile records stack.
- Search/filter/sort controls preserve the full underlying access list.
  Pagination and changed filters reset the list scroll position.
- Source registration is opened explicitly and preserves unfinished input
  when closed. Key and user pages contain lists and modal entry actions.
- Accessible native dialogs replace browser confirmation/prompt windows.
  Rename/crawl inputs support inline validation and asynchronous error/retry.
  The one-time key dialog clears secret text when closed.
- Private source owners see source deletion; public subscribers see
  unsubscribe; invited private readers see leave-source. Deletion confirmation
  uses freshly read source metadata. The API remains the authorization authority.
- List loading, empty, filtered-empty and failed states are distinct. Admin
  refreshes cannot be overwritten by earlier full-console refreshes.
- Source panels pin requests and writes to the selected source. Session
  invalidation clears private state and tokens, cancels polling/stream display,
  and prevents stale token/API/tool responses from repopulating it.
- Direct MCP calls preserve the selected server, tool and entered JSON across
  lazy tool-list loading. Expired keys are displayed separately from active keys.

## Task modals

`ConsoleDialogs.mountPanel` hosts an existing content node in the native
dialog top layer. It keeps the node's IDs, values and event handlers, and
uses the same visual system as confirmation and prompt dialogs.

| Task | Modal content |
| --- | --- |
| Register a source | Source type, description, access and extraction settings |
| Complete registration | Upload instructions or webhook settings, cleared on close |
| Issue an API key | Name, exact server scope and expiry |
| Display the new key | One-time value and client configuration, cleared on close |
| Invite a user | Email and role, restricted to administrators |
| Manage a source | AI settings, members, files or build diagnostics |
| Connect a client | Scoped client setup and verification instructions |
| Manage a group | Create/edit form, group members or common source description |

Only one task modal is open at a time. A confirmation may open above it;
closing that confirmation returns focus to the task. Background controls are
inert, background scrolling is locked, the heading/close control stays visible,
and the body scrolls independently. Forms respond to the modal's actual width.

Escape and explicit Close use the same lifecycle. A backdrop click does not
discard work. Active registration, issuance, invitation, upload and save
operations veto ordinary closing; session reset always closes and invalidates
private state. Errors appear inside the modal and preserve values for retry.
Registration drafts survive cancellation; group editor cancellation discards
the unsaved edit. Group saves retain revision and idempotency checks.

Successful actions update their lists and close or advance to a result modal.
The graph explorer, Playground and read-only group detail remain workspaces,
so their main interactions are still available on their dedicated pages.

## Button placement and text boundaries

Form fields align at the top, including a selector beside a multiline JSON
editor. Primary form actions occupy a separate full-width footer and align
right at their natural width, with at least 12 CSS pixels of clearance from
the inputs. Chat composition stays inline on wide screens and stacks on
small screens. Cancel precedes Save in group editors.

Refresh and create actions belong to the page action toolbar, rather than
inside a heading. Zero heading margins remain intentional where parent
`gap`/padding supplies the spacing; a global replacement of zero margins
would double those gaps.

Dropdown options separate their primary label and identifier into rows.
Graph summaries, neighbor metadata and source paths wrap rather than lose
meaningful text. Graph canvas labels move left at the right boundary and
above/below a node when neither side has room. Only labels wider than the
whole viewport are ellipsized; original labels and source identifiers remain
unchanged in the model and inspector. Label geometry uses CSS viewport pixels,
including high-DPI rendering.

For source-group graphs, focused node labels include the source name. The
inspector shows an origin-source badge, source badges on neighboring nodes,
and a summary of directly connected sources. The summary uses the full loaded
graph, including connections hidden by a filter; distinct neighbors and
parallel edges are counted separately. Self-loops have explicit counting
explanations. Source badge colors remain stable when filters or color modes
change. Cross-source edges use the starting source's color, while internal
edges remain gray. Inferred edges are thinner and lighter, including during
selection and path highlighting. A legend explains these distinctions.
Automatic source loading centers the cited line only inside the code viewport;
it does not scroll the page or inspector away from the source summary.

## Source groups and code surfaces

Source groups use the same full-width record-table layout as sources, keys
and users: a page heading with refresh/create actions, search/status/sort
controls, a result count, and pagination. Group details follow the list at
full width. Filtering affects display only; it never narrows the canonical
groups, available API-key scopes, or a selected group's details. Access
recovery is shown as a warning state instead of a green ready state.

`codeViewer(value, options)` renders endpoints and client configuration with
the shared dark `.snippet` surface and a dedicated copy-action footer. The
footer is separated from the code by 12 CSS pixels and aligned right.
The pre/code text and copied value remain literal, including quotes,
whitespace and line endings. Wrapping does not rewrite the value.
MCP server cards, connection guides and source-group endpoints use the same
component rather than styling long endpoints as inline code.

Connection-guide steps use 28px separation, with 12px spacing between their
headings, explanatory text, controls and action areas.

## Local verification

Use an existing Playwright installation; the scripts do not install packages.

```bash
export GRAPHIFY_PLAYWRIGHT_MODULE=/path/to/existing/playwright
node tests/test_console_ux.cjs
node tests/test_console_group_navigation.cjs
node tests/test_console_group_management.cjs
node tests/test_console_dialogs.cjs
node tests/test_console_panels.cjs
node tests/test_console_modal_flows.cjs
node tests/test_console_group_modals.cjs
node tests/test_console_group_consistency.cjs
node tests/test_console_spacing.cjs
node --test tests/test_group_graph_ui.cjs
node --test tests/test_graph_label_layout.cjs
node --test tests/test_graph_source_affinity.cjs
```

The browser suites use synthetic identities and intercepted APIs. Deployment
verification separately compares CloudFront assets with tested file hashes,
checks the public sign-in gate, and renders the existing demo from operator-
verified API responses. It does not claim a live Cognito login test or execute
destructive administration against real users.
