/* Independent source-level graph. Descriptions never create edges.
 * Untrusted strings use textContent; graph pages and excerpts pin one version.
 * Abort signals and generation guards prevent stale responses from rendering.
 */
(() => {
"use strict";
const MODEL = "global.anthropic.claude-sonnet-5";
const ROLES = ["frontend", "backend", "planning", "qa", "shared", "other"];
const TEXT = {
  tab: ["소스 그룹", "Source groups"],
  intro: ["프론트엔드, 백엔드, 기획, QA 소스를 묶어 원문 근거가 있는 교차 관계를 탐색합니다.", "Connect frontend, backend, planning, and QA sources through evidence-backed cross-source relations."],
  new: ["그룹 만들기", "Create group"], refresh: ["새로고침", "Refresh"],
  empty: ["아직 소스 그룹이 없습니다.", "No source groups yet."],
  choose: ["그룹을 선택하거나 새로 만드세요.", "Select a group or create one."],
  search: ["검색", "Search"],
  searchGroups: ["그룹 이름, ID, 설명 또는 구성 소스 ID 검색", "Search group names, IDs, descriptions, or source IDs"],
  groupInfo: ["그룹 정보", "Group information"], groupId: ["그룹 ID", "Group ID"],
  listStatus: ["분석 상태", "Build status"], allStatuses: ["모든 상태", "All statuses"],
  sort: ["정렬", "Sort"], nameSort: ["이름순", "Name"],
  newestSort: ["최신 생성순", "Newest"], statusSort: ["상태순", "Status"],
  listCount: ["전체 {total}개 중 {shown}개", "{shown} of {total} groups"],
  noGroupMatches: ["조건에 맞는 소스 그룹이 없습니다.", "No source groups match these filters."],
  clearFilters: ["필터 초기화", "Clear filters"],
  groupSourceCount: ["소스 {n}개", "{n} sources"],
  noGroupSources: ["이 그룹에 구성된 소스가 없습니다.", "This group has no sources."],
  recoveryStatus: ["접근 권한 복구 필요", "Access recovery needed"],
  viewActions: ["보기 및 연결", "View and connect"], manageActions: ["그룹 관리", "Manage group"],
  actions: ["동작", "Actions"],
  buildSettings: ["관계 분석 설정", "Relation build settings"],
  versionDetails: ["그룹 버전 정보", "Group versions"],
  loading: ["불러오는 중…", "Loading…"], saving: ["저장 중…", "Saving…"],
  retry: ["다시 시도", "Try again"], cancel: ["취소", "Cancel"], close: ["닫기", "Close"],
  save: ["저장", "Save"], edit: ["그룹 수정", "Edit group"],
  manageHint: ["그룹 이름, 설명과 구성 소스를 수정하거나 그룹을 삭제합니다. 그룹을 삭제해도 원본 소스는 유지됩니다.", "Edit the group name, description and sources, or delete the group. Deleting a group preserves its original sources."],
  viewerHelp: ["읽기 전용 그룹입니다. 수정은 소유자나 편집자, 삭제는 소유자만 할 수 있습니다.", "This group is read-only. Owners and editors can edit it; only its owner can delete it."],
  editorHelp: ["그룹을 수정할 수 있습니다. 삭제는 소유자만 할 수 있습니다.", "You can edit this group. Only its owner can delete it."],
  actionDenied: ["현재 권한으로 이 그룹을 수정하거나 삭제할 수 없습니다. 최신 권한을 확인했습니다.", "Your current role does not permit this action. The latest group permissions have been checked."],
  actionBusy: ["진행 중인 작업을 마친 뒤 다시 시도하세요.", "Finish the current action before trying again."],
  saved: ["그룹을 저장했습니다. 관계 분석을 실행하면 새 설정을 반영합니다.", "Group saved. Build relations to apply the new settings."],
  name: ["그룹 이름", "Group name"], description: ["그룹 설명 (최대 1,000자)", "Group description (up to 1,000 characters)"],
  nameRequired: ["그룹 이름을 1-120자로 입력하세요.", "Enter a group name between 1 and 120 characters."],
  descriptionHint: ["프로젝트의 목적과 소스들이 어떻게 협력하는지 적으세요. 설명 자체는 관계의 근거로 사용하지 않습니다.", "Describe the project and how its sources work together. Descriptions alone are not evidence of a relation."],
  sourceDescription: ["소스 공통 설명 (선택, 최대 500자)", "Common source description (optional, up to 500 characters)"],
  sourceDescriptionHint: ["소스의 목적과 내용을 적으세요. 공개 소스의 설명도 공개됩니다. 그룹 안에서의 역할과 설명은 별도로 설정합니다.", "Describe the source's purpose and contents. Public sources also have public descriptions. Set group-specific roles and descriptions separately."],
  editDescription: ["설명 편집", "Edit description"],
  descriptionSaved: ["소스 설명을 저장했습니다.", "Source description saved."],
  descriptionLimit: ["소스 설명은 최대 500자입니다.", "Source descriptions allow up to 500 characters."],
  sources: ["구성 소스", "Sources"], selectSources: ["소스 선택 (1-8개)", "Select sources (1-8)"],
  sourceCount: ["{n}/8개 선택", "{n}/8 selected"],
  sourceLimit: ["소스를 1-8개 선택하세요.", "Select between 1 and 8 sources."],
  noSources: ["선택할 수 있는 소스가 없습니다. 소스 탭에서 등록하거나 구독하세요.", "No sources are available. Register or subscribe to a source in the Sources tab."],
  searchSources: ["소스 이름, ID, 설명 검색", "Search source names, IDs, and descriptions"],
  noMatches: ["검색 결과가 없습니다.", "No matching sources."],
  role: ["그룹 안에서의 역할", "Role in this group"],
  groupSourceDescription: ["그룹 안에서의 설명 (최대 500자)", "Description in this group (up to 500 characters)"],
  commonDescription: ["공통 설명", "Common description"],
  missingSource: ["현재 선택 가능한 소스 목록에 없습니다. 접근 권한과 소스 상태를 확인하세요.", "This source is not currently selectable. Check its permissions and status."],
  recovery: ["일부 소스에 접근할 수 없어 그룹 설정 복구만 가능합니다. 문제가 있는 소스를 제거하거나 원본 권한을 복구하세요. 남기는 소스의 기존 역할과 설명은 유지됩니다.", "Some sources are inaccessible. You can recover group settings by removing affected sources or restoring source access. Existing roles and descriptions are preserved for retained sources."],
  recoverySources: ["유지할 소스 선택 (모두 해제 가능)", "Select sources to keep (all may be removed)"],
  sourceVersion: ["소스 버전", "Source version"],
  noVersion: ["불변 소스 버전이 필요합니다. 먼저 소스를 재빌드하세요.", "An immutable source version is required. Rebuild this source first."],
  goSources: ["소스 탭 열기", "Open Sources"],
  acl: ["그룹 초대는 원본 소스 접근 권한을 부여하지 않습니다. 모든 구성 소스에 대한 읽기 권한이 별도로 필요합니다.", "A group invitation does not grant access to original sources. Members must separately have read access to every source."],
  aclEdit: ["소스를 추가하면 해당 소스 권한이 없는 기존 멤버는 그룹에 접근할 수 없게 됩니다.", "Adding a source prevents existing members without access to that source from accessing the group."],
  ai: ["AI 관계 판정 (선택)", "AI relation assessment (optional)"],
  aiEnable: ["Bedrock 모델로 관계 후보를 판정합니다", "Use a Bedrock model to assess relation candidates"],
  aiCost: ["기본은 꺼짐입니다. 켜면 관계 분석 시 양쪽 근거와 설명이 모델로 전송되며 토큰 비용이 발생합니다. 기존 문서 AI 추출 설정과는 별개입니다.", "Off by default. Relation builds send evidence and descriptions to the model and incur token charges when enabled. This is separate from document AI extraction."],
  aiOff: ["AI 판정 꺼짐 (명시적 관계 분석)", "AI assessment off (explicit relation analysis)"],
  aiOn: ["AI 판정 켜짐", "AI assessment on"],
  model: ["모델", "Model"], cache: ["변경되지 않은 소스 쌍의 분석 결과는 캐시에서 재사용합니다.", "Analysis of unchanged source pairs is reused from cache."],
  rebuild: ["관계 분석 실행", "Build relations"],
  queued: ["관계 분석을 요청했습니다.", "Relation build requested."],
  immutableConflict: ["그룹 버전이 바뀌었거나 구성 소스의 불변 버전이 없습니다. 새로고침하고, 버전이 없는 소스를 먼저 재빌드한 뒤 다시 실행하세요.", "The group revision changed or a source has no immutable version. Refresh, rebuild sources missing a version, then try again."],
  conflict: ["다른 변경이 먼저 저장되었습니다. 최신 설정을 불러온 뒤 다시 편집하세요.", "Another change was saved first. Load the latest settings before editing again."],
  conflictReload: ["최신 설정 불러오기 (편집 내용 초기화)", "Load latest settings (discard edits)"],
  buildId: ["빌드 ID", "Build ID"], revision: ["설정 revision", "Settings revision"],
  version: ["그래프 버전", "Graph version"], activeRevision: ["그래프 revision", "Graph revision"],
  pending: ["아직 게시된 관계 그래프가 없습니다. 소스를 빌드한 뒤 관계 분석을 실행하세요.", "No relation graph has been published. Build your sources, then build relations."],
  stale: ["설정 또는 원본 소스가 변경되었습니다. 관계 분석을 다시 실행하세요.", "Settings or an original source changed. Build relations again."],
  changed: ["현재 설정과 그래프의 revision이 달라 결과를 지웠습니다. 관계 분석을 다시 실행하세요.", "The graph does not match the current settings revision and was cleared. Build relations again."],
  partial: ["일부 후보만 분석된 결과입니다. 사용량과 오류를 확인하세요.", "Only part of the candidate set was analyzed. Check usage and errors."],
  failed: ["최근 관계 분석이 실패했습니다.", "The latest relation build failed."],
  pollStopped: ["자동 상태 확인을 마쳤습니다. 새로고침으로 진행 상황을 확인하세요.", "Automatic status checks have ended. Refresh to check progress."],
  building: ["관계 분석 중입니다. 이 탭이 보이는 동안에만 상태를 제한된 횟수로 확인합니다.", "Relations are building. A limited number of status checks run while this tab is visible."],
  lastError: ["최근 오류", "Last error"],
  errorCode: ["오류 코드", "Error code"], partialReasons: ["부분 완료 사유", "Partial result reasons"],
  sourceRebuildHelp: ["구성 소스의 불변 버전이 없습니다. 소스 탭에서 해당 원본 소스를 재빌드한 뒤 그룹 관계 분석을 다시 실행하세요.", "A source has no immutable version. Rebuild the original source in the Sources tab, then build group relations again."],
  corpusLimitHelp: ["소스가 파일, 텍스트, 그래프 한도를 초과했습니다. 소스를 더 작은 단위로 나누거나 포함 범위를 줄인 뒤 원본 소스와 그룹을 다시 빌드하세요.", "A source exceeds a file, text, or graph limit. Split the corpus into smaller sources or reduce its included scope, then rebuild the sources and group."],
  changedInputHelp: ["분석 중 소스 또는 그룹 설정이 변경되었습니다. 최신 설정을 새로고침한 뒤 그룹 관계 분석을 다시 실행하세요.", "Sources or group settings changed during analysis. Refresh the latest settings, then build group relations again."],
  delete: ["그룹 삭제", "Delete group"],
  deleteConfirm: ["이 그룹을 삭제할까요? 원본 소스와 원본 소스의 권한은 유지됩니다.", "Delete this group? Original sources and their source permissions are retained."],
  deleted: ["그룹을 삭제했습니다.", "Group deleted."],
  viewer: ["읽기 전용", "Read only"], owner: ["소유자", "Owner"], editor: ["편집자", "Editor"],
  mcp: ["그룹 MCP URL", "Group MCP URL"], copy: ["URL 복사", "Copy URL"],
  fullGraph: ["전체 그래프 보기", "View full graph"], guide: ["연동 가이드", "Connection guide"],
  graph: ["소스 간 관계", "Cross-source relations"],
  graphHint: ["선은 관계 API가 반환한 실제 교차 소스 관계입니다. 소스 배치와 역할, 설명은 관계를 만들지 않습니다. 선이나 목록의 관계를 선택해 양쪽 근거를 확인하세요.", "Edges represent actual cross-source relations returned by the API. Layout, roles, and descriptions do not create relations. Select an edge or a listed relation to inspect both evidence records."],
  graphAlt: ["소스 간 관계 그래프. 아래 관계 목록에서도 동일한 근거를 열 수 있습니다.", "Cross-source relation graph. The relation list below opens the same evidence."],
  graphLoad: ["그래프 불러오기", "Load graph"],
  graphNotLoaded: ["관계 그래프가 준비되어 있습니다. 그래프 불러오기를 눌러 확인하세요.", "The relation graph is ready. Select Load graph to view it."],
  noRelations: ["현재 버전에서 확인된 교차 소스 관계가 없습니다.", "This version contains no cross-source relations."],
  loaded: ["교차 관계 {n}개 불러옴", "{n} cross-source relations loaded"],
  filtered: ["{n}개 관계 표시", "Showing {n} relations"],
  more: ["관계 더 불러오기", "Load more relations"], showMore: ["목록 더 보기", "Show more"],
  graphLimit: ["화면의 관계 한도에 도달했습니다. 세부 분석은 그룹 MCP를 사용하세요.", "The console relation limit has been reached. Use the group MCP for further analysis."],
  sourceFilter: ["관계에 포함된 소스", "Source involved in the relation"],
  allSources: ["모든 소스", "All sources"],
  noFilteredRelations: ["이 소스의 관계가 현재 불러온 목록에 없습니다.", "No relations for this source are present in the loaded results."],
  evidence: ["관계 근거", "Relation evidence"],
  evidenceHint: ["관계를 선택하면 양쪽 원문의 위치와 정확한 인용을 표시합니다.", "Select a relation to see both source locations and exact quotations."],
  relation: ["관계", "Relation"], evidenceKind: ["근거 종류", "Evidence kind"],
  confidence: ["추론 점수", "Inference score"],
  confidenceHint: ["점수는 실측 정확도가 아닙니다. 원문 근거를 확인하세요.", "The score is not measured accuracy. Verify the source evidence."],
  method: ["추출 방법", "Method"], review: ["검토 상태", "Review status"],
  from: ["출발 소스", "From source"], to: ["도착 소스", "To source"],
  missingEvidence: ["이 관계에는 양쪽 소스를 식별할 수 있는 근거 두 개가 없어 원문을 열 수 없습니다.", "This relation does not contain two evidence records identifying both sources, so source reading is unavailable."],
  noQuote: ["인용문이 제공되지 않았습니다.", "No quotation was provided."],
  file: ["파일", "File"], lines: ["{a}-{b}줄", "Lines {a}-{b}"],
  locationMissing: ["정확한 파일과 줄 위치가 제공되지 않아 원문을 열 수 없습니다.", "Exact file and line locations are unavailable, so this source cannot be opened."],
  sourceRead: ["이 버전의 원문 보기", "Read source at this version"],
  sourceReadTitle: ["버전 고정 원문", "Source at the pinned version"],
  sourceReadError: ["해당 버전의 원문을 읽지 못했습니다.", "Unable to read source at this version."],
  versionError: ["버전이 변경되었거나 보존된 원문이 없습니다. 그래프를 새로 불러오세요. 최신 원문으로 대체하지 않습니다.", "The version changed or its source snapshot is unavailable. Reload the graph. The viewer does not substitute the latest source."],
  invalid: ["응답 형식 또는 그래프 버전이 올바르지 않습니다. 새로고침하세요.", "The response format or graph version is invalid. Refresh to try again."],
  denied: ["그룹 또는 구성 소스 접근 권한이 없어 저장된 그래프와 근거를 지웠습니다.", "Access to the group or a source is unavailable. Cached graph and evidence data have been cleared."],
  error: ["요청을 처리하지 못했습니다.", "The request could not be completed."],
  members: ["그룹 멤버", "Group members"], loadMembers: ["멤버 불러오기", "Load members"],
  manageMembers: ["멤버 관리", "Manage members"],
  invite: ["초대", "Invite"], email: ["이메일", "Email"], memberRole: ["그룹 권한", "Group permission"],
  remove: ["제거", "Remove"],
  removeConfirm: ["이 멤버의 그룹 접근 권한을 제거할까요? 원본 소스 권한은 유지됩니다.", "Remove this member's group access? Their original source permissions are retained."],
  noMembers: ["표시할 그룹 멤버가 없습니다.", "There are no group members to display."],
  invited: ["그룹 초대를 저장했습니다.", "Group invitation saved."],
  removed: ["그룹 멤버를 제거했습니다.", "Group member removed."],
  usage: ["분석 사용량", "Analysis usage"],
  usageHint: ["서버가 보고한 토큰, 모델 호출, 캐시 재사용 횟수입니다. 금액을 추정하지 않습니다.", "Token, model-call, and cache-reuse counts reported by the server. No currency estimate is calculated."],
  noUsage: ["아직 보고된 사용량이 없습니다.", "No usage has been reported yet."],
  unknownUsage: ["사용량을 확인할 수 없는 호출이 {n}건 있습니다. 표시된 토큰 수에는 확인된 사용량만 포함됩니다.", "{n} calls have unconfirmed usage. Displayed tokens include confirmed usage only."],
  inputTokens: ["입력 토큰", "Input tokens"], outputTokens: ["출력 토큰", "Output tokens"],
  calls: ["모델 호출", "Model calls"], cachedPairs: ["재사용된 소스 쌍", "Reused source pairs"],
  analyzedPairs: ["분석된 소스 쌍", "Analyzed source pairs"], rawUsage: ["상세 사용량", "Detailed usage"],
  role_frontend: ["프론트엔드", "Frontend"], role_backend: ["백엔드", "Backend"],
  role_planning: ["기획", "Planning"], role_qa: ["QA", "QA"], role_shared: ["공통", "Shared"], role_other: ["기타", "Other"],
  status_DRAFT: ["초안", "Draft"], status_STALE: ["갱신 필요", "Stale"],
  status_BUILDING: ["분석 중", "Building"], status_READY: ["준비됨", "Ready"],
  status_PARTIAL: ["부분 완료", "Partial"], status_FAILED: ["실패", "Failed"], status_DELETED: ["삭제됨", "Deleted"],
};
for (const [key, words] of Object.entries(TEXT)) {
  for (const [i, lang] of ["ko", "en"].entries()) I18N[lang][`sg.${key}`] = words[i];
}
const $ = (id) => document.getElementById(id);
const tr = (key, vars = {}) => t(`sg.${key}`).replace(/\{(\w+)\}/g, (m, k) => String(vars[k] ?? m));
const text = (v) => typeof v === "string" ? v : v == null ? "" : String(v);
const sid = (r) => text(r.source_id || r.repo_id);
const path = (id) => `/groups/${encodeURIComponent(id)}`;
const displayName = (r) => text(r.name || r.server_name || sid(r));
const button = (label, handler, cls = "ghost mini") => el("button", { type: "button", class: cls, onclick: handler, "data-i18n": `sg.${label}` }, tr(label));
const translated = (tag, label, attrs = {}) => el(tag, { ...attrs, "data-i18n": `sg.${label}` }, tr(label));
const translatedHint = (label) => translated("p", label, { class: "hint" });
async function confirmAction(message) {
  if (typeof uiConfirm === "function") return await uiConfirm(message);
  return await Promise.resolve(confirm(message));
}
const hint = (value) => el("p", { class: "hint" }, value);
const errorNode = (value) => el("p", { class: "sg-message sg-error", role: "alert" }, value);
const roleOf = (g) => text(g?.role || g?.member_role || g?.group_role);
const owner = (g) => g?.owned === true || roleOf(g) === "owner";
const editable = (g) => g?.can_edit === true || g?.manageable === true || owner(g) || roleOf(g) === "editor";
const G = {
  initialized: false, shown: false, lang: "", groups: [], id: "", meta: null,
  graph: null, selected: null, members: null, requests: new Map(), serial: 0,
  timer: null, polls: 0, draft: null, busy: false, listLoaded: false,
  filter: "", visibleRelations: 40, sourceText: null,
  manageSerial: 0,
};
// Presentation state never replaces the authoritative group or source arrays.
const listView = { query: "", status: "", sort: "name", page: 1, size: 25 };
// Mounted panels live outside sg-detail. Keep their identities separate from
// view data so disposing an old panel cannot clear its replacement.
let editorPanel = null, membersPanel = null, descriptionPanel = null;
let memberSession = null, descriptionSession = null;
const sourceName = (id) => displayName((S.repos || []).find((r) => sid(r) === id)
  || (G.meta?.sources || []).find((r) => sid(r) === id) || { source_id: id });
function cancel(tag) {
  G.requests.get(tag)?.controller.abort();
  G.requests.delete(tag);
}
function cancelAll(except = []) {
  for (const tag of G.requests.keys()) if (!except.includes(tag)) cancel(tag);
  clearTimeout(G.timer); G.timer = null;
}
function begin(tag) {
  cancel(tag);
  const req = { controller: new AbortController(), serial: ++G.serial, id: G.id, auth: S.authEpoch };
  G.requests.set(tag, req);
  return req;
}
function current(tag, req) {
  return G.requests.get(tag) === req && !req.controller.signal.aborted && req.auth === S.authEpoch;
}
async function request(tag, req, method, url, body) {
  const out = await api(method, url, body, { signal: req.controller.signal });
  if (!current(tag, req)) throw new DOMException("Stale request", "AbortError");
  return out;
}
const aborted = (e) => e?.name === "AbortError";
const denied = (e) => [401, 403, 404, 410].includes(Number(e?.status));
function message(e, action = "") {
  if (denied(e)) return tr("denied");
  if (Number(e?.status) === 409) return tr(action === "rebuild" ? "immutableConflict" : action === "source" ? "versionError" : "conflict");
  return `${tr(action === "source" ? "sourceReadError" : "error")} ${text(e?.message)}`;
}
function clearGraph() {
  cancel("graph"); cancel("source");
  G.graph = null; G.selected = null; G.sourceText = null; G.filter = ""; G.visibleRelations = 40;
  for (const id of ["sg-graph", "sg-evidence", "sg-source"]) if ($(id)) $(id).replaceChildren();
  if ($("sg-source")) $("sg-source").hidden = true;
}
function clearDetail() {
  closeEditor("reset"); closeMembers("reset");
  cancelAll(["list", "description"]); clearGraph();
  G.meta = null; G.members = null; G.draft = null; G.busy = false;
  if ($("sg-detail")) $("sg-detail").replaceChildren();
}
function clearMembers() {
  closeMembers("reset");
  cancel("members"); G.members = null;
}
function clearSourceGroups() {
  if (typeof updateSourceGroups === "function") updateSourceGroups([]);
}
function applyAccessState(meta, origin) {
  if (!owner(meta) || meta.access_recovery) clearMembers();
  if (G.draft?.id === meta.group_id
      && (!editable(meta) || (!!meta.access_recovery !== !!G.draft.g?.access_recovery))) {
    closeEditor("reset");
    $("sg-notice")?.replaceChildren(errorNode(tr(meta.access_recovery ? "recovery" : "actionDenied")));
  }
  if (meta.access_recovery) {
    // A response started before this access decision cannot restore old data.
    if (origin !== "detail") cancel("detail");
    if (origin !== "list") { cancel("list"); clearSourceGroups(); }
    clearGraph();
  }
}
function revoke(e, refreshedList = false) {
  const id = G.id;
  cancelAll(["description"]); G.manageSerial++;
  clearDetail(); G.groups = G.groups.filter((g) => g.group_id !== id); G.id = "";
  renderList(); $("sg-detail").replaceChildren(errorNode(message(e)));
  if (!refreshedList) { clearSourceGroups(); refreshList(); }
}
function handleError(e, target, action = "") {
  if (aborted(e)) return;
  if (denied(e)) { revoke(e); return; }
  if (target?.isConnected) target.replaceChildren(errorNode(message(e, action)));
}
const stateOf = (g) => g.access_recovery ? "RECOVERY"
  : g.stale && g.status === "READY" ? "STALE" : text(g.status || "DRAFT").toUpperCase();
function status(g) {
  const value = stateOf(g), label = value === "RECOVERY" ? "recoveryStatus" : `status_${value}`;
  return el("span", { class: `sg-status sg-status-${Object.hasOwn(TEXT, label) ? value.toLowerCase() : "draft"}` },
    Object.hasOwn(TEXT, label) ? tr(label) : value);
}
function safeReason(value) {
  const code = typeof value === "string" ? value : value?.code;
  return typeof code === "string" && /^[A-Z][A-Z0-9_]{0,63}$/.test(code) ? code : "";
}
function reasonHelp(code) {
  if (["SOURCE_VERSION_MISSING", "SOURCE_SNAPSHOT_MISSING", "SNAPSHOT_MISSING"].includes(code)) return "sourceRebuildHelp";
  if (["FILE_LIMIT", "TEXT_LIMIT", "GRAPH_LIMIT", "NODE_LIMIT", "EDGE_LIMIT"].includes(code)) return "corpusLimitHelp";
  if (["STALE_INPUT", "BUILD_INPUT_CHANGED", "SOURCE_CHANGED", "SOURCE_VERSION_CHANGED"].includes(code)) return "changedInputHelp";
  return "";
}
const dataReady = (g) => !!g?.data_ready && !g.stale && !g.access_recovery;
function dataActions(g) {
  // Recheck current access on click, including buttons from an earlier render.
  const ready = () => dataReady(G.meta?.group_id === g.group_id ? G.meta
    : G.groups.find((row) => row.group_id === g.group_id));
  const graph = button("fullGraph", () => {
    if (ready() && typeof openGraph === "function") openGraph(g.group_id);
  });
  const guide = button("guide", () => {
    if (ready() && typeof openConnectionGuide === "function") openConnectionGuide(g.group_id);
  });
  graph.disabled = !dataReady(g) || typeof openGraph !== "function";
  guide.disabled = !dataReady(g) || typeof openConnectionGuide !== "function";
  return [graph, guide];
}
function mcpLine(g) {
  const url = `${text(CFG?.mcpBase).replace(/\/$/, "")}/mcp/${encodeURIComponent(g.group_id)}`;
  return el("div", { class: "sg-mcp" }, codeViewer(url, { label: tr("mcp"), copyLabel: tr("copy") }));
}
function init() {
  if (G.initialized) return;
  G.initialized = true; G.lang = LANG;
  renderShell();
  document.addEventListener("visibilitychange", () => {
    clearTimeout(G.timer);
    if (!document.hidden && G.shown) schedulePoll();
  });
  let resizeFrame = 0;
  window.addEventListener("resize", () => {
    if (resizeFrame || !G.shown || !G.graph || G.draft) return;
    resizeFrame = requestAnimationFrame(() => {
      resizeFrame = 0;
      if (!G.shown || !G.graph || G.draft) return;
      const box = $("sg-graph"), svg = box?.querySelector(".sg-svg");
      if (svg && svg.classList.contains("sg-svg-narrow") !== (box.clientWidth < 520)) renderGraph();
    });
  });
  // Handles fast config loads that finish before this deferred script runs.
  document.querySelectorAll('[data-i18n^="sg."]').forEach((node) => { node.textContent = t(node.dataset.i18n); });
}
function renderShell() {
  const focused = document.activeElement;
  const focusId = ["sg-search", "sg-status-filter", "sg-status-filter-combo", "sg-sort", "sg-sort-combo"]
    .includes(focused?.id) ? focused.id : "";
  // Keep a list error and its retry action until a successful refresh replaces it.
  const list = $("sg-list") || el("div", { id: "sg-list" });
  const change = (key, value) => {
    listView[key] = value; listView.page = 1; renderList();
    const wrap = $("groups-table")?.parentElement; if (wrap) wrap.scrollTop = 0;
  };
  const search = el("input", { id: "sg-search", type: "search", maxlength: "200", autocomplete: "off",
    placeholder: tr("searchGroups"), "data-i18n-ph": "sg.searchGroups",
    oninput: (e) => change("query", e.currentTarget.value) });
  search.value = listView.query;
  const filter = el("select", { id: "sg-status-filter", onchange: (e) => change("status", e.currentTarget.value) },
    translated("option", "allStatuses", { value: "" }),
    ...["DRAFT", "READY", "BUILDING", "STALE", "PARTIAL", "FAILED", "RECOVERY"].map((value) =>
      translated("option", value === "RECOVERY" ? "recoveryStatus" : `status_${value}`, { value })));
  filter.value = listView.status;
  const sort = el("select", { id: "sg-sort", onchange: (e) => change("sort", e.currentTarget.value) },
    ...[["name", "nameSort"], ["newest", "newestSort"], ["status", "statusSort"]].map(([value, label]) =>
      translated("option", label, { value })));
  sort.value = listView.sort;
  const control = (label, input, className = "") => el("div", { class: className },
    translated("label", label, { for: input.id }), input);
  $("sg-root").replaceChildren(
    el("section", { class: "sg-list-section", "aria-labelledby": "sg-list-heading" },
      el("div", { class: "sg-intro page-heading" },
        el("div", null, el("h2", { id: "sg-list-heading" }, el("span", { class: "dot", "aria-hidden": "true" }),
          translated("span", "tab")), hint(tr("intro"))),
        el("div", { class: "btns page-actions" }, button("refresh", refresh), button("new", () => editGroup(null), "mini"))),
      el("div", { id: "sg-notice", "aria-live": "polite" }),
      el("div", { id: "sg-toolbar", class: "list-toolbar" },
        control("search", search, "list-search"), control("listStatus", filter), control("sort", sort),
        el("div", { class: "sg-filter-actions" }, button("clearFilters", clearGroupFilters))),
      el("p", { id: "sg-count", class: "list-count", "aria-live": "polite" }),
      list, el("div", { id: "pager-groups", class: "pager", hidden: "" })),
    el("div", { id: "sg-detail", "aria-live": "polite" }, hint(tr("choose"))));
  enhanceSelect(filter); enhanceSelect(sort);
  if (focusId) ($(`${focusId}-combo`) || $(focusId))?.focus({ preventScroll: true });
}
function clearGroupFilters() {
  Object.assign(listView, { query: "", status: "", sort: "name", page: 1 });
  for (const [id, value] of [["sg-search", ""], ["sg-status-filter", ""], ["sg-sort", "name"]]) {
    if ($(id)) $(id).value = value;
  }
  renderList();
  const wrap = $("groups-table")?.parentElement; if (wrap) wrap.scrollTop = 0;
  $("sg-search")?.focus({ preventScroll: true });
}
function renderGroupPager(total) {
  const bar = $("pager-groups");
  const pages = Math.max(1, Math.ceil(total / listView.size));
  listView.page = Math.max(1, Math.min(listView.page, pages));
  const start = (listView.page - 1) * listView.size;
  bar.replaceChildren(); bar.hidden = total <= 10;
  if (!bar.hidden) {
    const navigate = (page) => {
      listView.page = page; renderList();
      const wrap = $("groups-table")?.parentElement; if (wrap) wrap.scrollTop = 0;
    };
    const size = el("select", { id: "sg-page-size", class: "pager-size", "aria-label": t("pg.size"),
      onchange: (e) => { listView.size = Number(e.currentTarget.value); navigate(1); } },
    ...[10, 25, 50, 100].map((n) => el("option", { value: String(n) }, `${n} / ${t("pg.perpage")}`)));
    size.value = String(listView.size);
    const prev = el("button", { id: "sg-page-prev", type: "button", class: "ghost mini", "aria-label": t("pg.prev"),
      onclick: () => navigate(listView.page - 1) }, "‹");
    const next = el("button", { id: "sg-page-next", type: "button", class: "ghost mini", "aria-label": t("pg.next"),
      onclick: () => navigate(listView.page + 1) }, "›");
    prev.disabled = listView.page <= 1; next.disabled = listView.page >= pages;
    bar.append(el("span", { class: "hint pager-info" }, t("pg.range")
      .replace("{a}", String(start + 1)).replace("{b}", String(Math.min(start + listView.size, total))).replace("{n}", String(total))),
    size, prev, el("span", { class: "pager-page" }, `${listView.page} / ${pages}`), next);
  }
  return start;
}
function renderList() {
  const list = $("sg-list");
  const focusId = document.activeElement?.id;
  const query = listView.query.trim().toLocaleLowerCase();
  const rows = G.groups.filter((g) => (!listView.status || stateOf(g) === listView.status)
    && (!query || [g.name, g.group_id, g.description, ...(Array.isArray(g.sources) ? g.sources.map(sid) : [])]
      .map(text).join(" ").toLocaleLowerCase().includes(query)));
  const compareName = (a, b) => text(a.name || a.group_id).localeCompare(text(b.name || b.group_id),
    LANG === "ko" ? "ko" : "en", { numeric: true, sensitivity: "base" }) || text(a.group_id).localeCompare(text(b.group_id));
  const created = (g) => typeof g.created_at === "number" ? g.created_at : (Date.parse(text(g.created_at)) || 0) / 1000;
  rows.sort((a, b) => (listView.sort === "newest" ? created(b) - created(a)
    : listView.sort === "status" ? stateOf(a).localeCompare(stateOf(b)) : 0) || compareName(a, b));
  $("sg-count").textContent = G.listLoaded ? tr("listCount", { total: G.groups.length, shown: rows.length }) : "";
  const start = renderGroupPager(rows.length);
  if (!G.listLoaded && list.querySelector('[role="alert"]')) return;
  const body = el("tbody");
  const cell = (label, ...children) => el("td", { "data-label": tr(label) }, ...children);
  list.replaceChildren(el("div", { class: "table-wrap", tabindex: "0", role: "region", "aria-labelledby": "sg-list-heading" },
    el("table", { id: "groups-table", class: "record-table", "aria-labelledby": "sg-list-heading" },
      el("thead", null, el("tr", null, ...["groupInfo", "listStatus", "actions"].map((label) =>
        translated("th", label, { scope: "col" })))), body)));
  for (const g of rows.slice(start, start + listView.size)) {
    const openButton = el("button", { id: `sg-open-${g.group_id}`, type: "button", class: "sg-list-open record-name",
      onclick: () => {
        open(g.group_id);
        if (G.id === g.group_id && !G.draft) $("sg-detail")?.scrollIntoView({ block: "start" });
      }, "aria-controls": "sg-detail",
      "aria-current": String(g.group_id === G.id) }, el("strong", null, text(g.name || g.group_id)));
    const manage = managementActions(g);
    body.append(el("tr", { class: `sg-list-card${g.group_id === G.id ? " sg-current" : ""}`, "data-group-id": g.group_id },
      cell("groupInfo", openButton, g.description ? el("p", { class: "record-description" }, text(g.description)) : "",
        el("span", { class: "record-id sg-id" }, text(g.group_id)),
        el("div", { class: "record-meta" }, Array.isArray(g.sources)
          ? el("span", null, tr("groupSourceCount", { n: g.sources.length })) : "")),
      cell("listStatus", el("div", { class: "record-state" }, status(g),
        el("span", { class: "hint" }, tr(owner(g) ? "owner" : editable(g) ? "editor" : "viewer")))),
      cell("actions", el("div", { class: "record-actions" },
        !g.access_recovery ? el("div", null, translated("span", "viewActions", { class: "action-label" }),
          el("div", { class: "btns" }, ...dataActions(g))) : "",
        manage.children.length ? el("div", null, translated("span", "manageActions", { class: "action-label" }), manage) : "",
        !editable(g) ? hint(tr("viewerHelp")) : !owner(g) ? hint(tr("editorHelp")) : ""))));
  }
  if (!rows.length) {
    const filtering = !!query || !!listView.status;
    body.append(el("tr", null, el("td", { colspan: "3", class: "list-empty" },
      hint(tr(!G.listLoaded ? "loading" : !G.groups.length ? "empty" : "noGroupMatches")),
      G.listLoaded ? el("div", { class: "btns" }, filtering ? button("clearFilters", clearGroupFilters)
        : button("new", () => editGroup(null), "mini")) : "")));
  }
  if (focusId && (focusId.startsWith("sg-open-") || ["sg-page-size", "sg-page-prev", "sg-page-next"].includes(focusId))) {
    const target = $(focusId);
    (target?.disabled ? $("sg-page-size") : target)?.focus({ preventScroll: true });
  }
}
function managementActions(g) {
  const actions = el("div", { class: "btns sg-management-actions", role: "group",
    "data-group-id": g.group_id,
    "aria-label": `${tr("edit")}: ${text(g.name || g.group_id)}` });
  if (editable(g)) {
    const edit = button("edit", () => manage(g.group_id, "edit"));
    edit.disabled = G.busy || !!G.draft;
    actions.append(edit);
  }
  if (owner(g)) {
    if (!g.access_recovery) {
      const members = button("manageMembers", () => manage(g.group_id, "members"));
      members.disabled = G.busy || !!G.draft;
      actions.append(members);
    }
    const remove = button("delete", () => manage(g.group_id, "delete"), "danger mini");
    remove.disabled = G.busy || !!G.draft;
    actions.append(remove);
  }
  return actions;
}
async function refreshList() {
  if (!G.shown || !CFG) return;
  const req = begin("list");
  try {
    const out = await request("list", req, "GET", "/groups");
    if (!current("list", req)) return;
    if (!Array.isArray(out.groups)) throw new Error(tr("invalid"));
    G.groups = out.groups.filter((g) => g && typeof g.group_id === "string" && g.status !== "DELETED");
    G.listLoaded = true; renderList();
    // Publish only this authoritative list, without merging cached detail data.
    if (current("list", req) && typeof updateSourceGroups === "function") updateSourceGroups(G.groups);
    if (G.id && !G.groups.some((g) => g.group_id === G.id)) revoke({ status: 403 }, true);
    const selected = G.groups.find((g) => g.group_id === G.id);
    if (selected) applyAccessState(selected, "list");
    // Recovery cancels an older detail read even before it produced metadata.
    // Render the authoritative redacted list entry instead of leaving Loading.
    if (selected && !G.draft && (selected.access_recovery || (G.meta && (selected.data_ready === false
        || selected.data_ready !== G.meta.data_ready || selected.stale !== G.meta.stale
        || roleOf(selected) !== roleOf(G.meta)
        || selected.revision !== G.meta.revision || selected.active_version !== G.meta.active_version)))) {
      cancel("detail"); clearGraph(); G.meta = selected;
      applyAccessState(selected, "list"); renderList(); renderDetail();
    }
  } catch (e) {
    if (!current("list", req) || aborted(e)) return;
    G.groups = []; G.listLoaded = false; renderList();
    if (denied(e)) { clearDetail(); clearSourceGroups(); }
    $("sg-list").replaceChildren(errorNode(message(e)), button("retry", refreshList));
  } finally { if (current("list", req)) G.requests.delete("list"); }
}
function refresh() {
  if (G.draft || G.busy) return;
  $("sg-notice").replaceChildren();
  refreshList();
  if (G.id) open(G.id);
}
function normalMeta(out, id) {
  const meta = out?.group;
  if (!meta || meta.group_id !== id || !Number.isInteger(meta.revision) || meta.revision < 1
      || !Array.isArray(meta.sources) || meta.sources.length > 8
      || meta.sources.some((s) => !s || !sid(s)) || new Set(meta.sources.map(sid)).size !== meta.sources.length) {
    throw new Error(tr("invalid"));
  }
  return meta;
}
async function open(id, { includeGraph = true } = {}) {
  init();
  if (!closeGroupPanels("navigation")) return false;
  const intent = ++G.manageSerial;
  clearDetail(); G.id = text(id); G.polls = 0;
  renderList(); $("sg-detail").replaceChildren(hint(tr("loading")));
  await loadDetail(false, { includeGraph });
  return G.shown && G.manageSerial === intent && G.id === text(id) && !!G.meta;
}
async function loadDetail(poll, { includeGraph = true } = {}) {
  if (!G.shown || !G.id) return;
  const req = begin("detail");
  try {
    const out = await request("detail", req, "GET", path(req.id));
    if (!current("detail", req)) return;
    const previous = G.meta, meta = normalMeta(out, req.id);
    if (meta.status === "DELETED") { revoke({ status: 410 }); return; }
    G.meta = meta;
    applyAccessState(meta, "detail");
    const row = G.groups.findIndex((g) => g.group_id === meta.group_id);
    if (row >= 0) G.groups[row] = meta;
    renderList();
    if (!poll || !previous) renderDetail();
    else {
      if (previous.revision !== meta.revision || previous.active_version !== meta.active_version || meta.data_ready === false) clearGraph();
      renderMeta();
    }
    if ((meta.access_recovery && !previous?.access_recovery)
        || (poll && previous?.status === "BUILDING" && meta.status !== "BUILDING")) refreshList();
    if (includeGraph && !G.draft && !G.graph && meta.active_version && meta.active_revision === meta.revision && meta.data_ready !== false) await loadGraph();
    else if (!G.graph && !G.draft) renderGraph();
    schedulePoll();
  } catch (e) {
    if (!current("detail", req) || aborted(e)) return;
    clearGraph(); handleError(e, $("sg-detail"));
    if (!denied(e)) $("sg-detail").append(button("retry", () => open(G.id)));
  } finally { if (current("detail", req)) G.requests.delete("detail"); }
}
function schedulePoll() {
  clearTimeout(G.timer); G.timer = null;
  if (!G.shown || document.hidden || G.draft || membersPanel?.isOpen || G.busy || G.meta?.access_recovery || G.meta?.status !== "BUILDING") return;
  if (G.polls >= 12) { if ($("sg-poll")) $("sg-poll").textContent = tr("pollStopped"); return; }
  G.timer = setTimeout(() => { G.polls++; loadDetail(true); }, 10000);
}
function renderDetail() {
  $("sg-detail").replaceChildren(
    el("section", { id: "sg-meta" }),
    el("section", { id: "sg-graph-section" }, el("h2", null, tr("graph")), hint(tr("graphHint")),
      el("div", { id: "sg-graph" })),
    el("section", { id: "sg-evidence" }, el("h2", null, tr("evidence")), hint(tr("evidenceHint"))),
    el("section", { id: "sg-source", hidden: "" }),
    el("section", { id: "sg-usage" }));
  renderMeta(); renderGraph(); renderUsage();
}
function info(label, value) {
  return el("div", { class: "sg-fact" }, el("dt", null, tr(label)), el("dd", null, text(value) || "-"));
}
function renderMeta() {
  const g = G.meta;
  if (!g || !$("sg-meta")) return;
  const actions = el("div", { class: "btns sg-data-actions" }, ...dataActions(g), button("refresh", () => open(g.group_id)));
  if (editable(g)) {
    const build = button("rebuild", rebuild, "mini");
    build.disabled = g.status === "BUILDING" || G.busy || g.access_recovery === true || !g.sources.length;
    actions.append(build);
  }
  const manage = managementActions(g);
  manage.classList.add("page-actions");
  $("sg-meta").replaceChildren(
    el("div", { class: "sg-detail-heading page-heading" }, el("h2", null, text(g.name || g.group_id)), status(g),
      el("span", { class: "hint" }, tr(owner(g) ? "owner" : editable(g) ? "editor" : "viewer")),
      manage),
    !editable(g) ? hint(tr("viewerHelp")) : !owner(g) ? hint(tr("editorHelp")) : "",
    g.description ? el("p", { class: "sg-description" }, text(g.description)) : "",
    el("div", { class: "sg-id record-id", "aria-label": tr("groupId") }, g.group_id), actions,
    el("div", { id: "sg-action-message", "aria-live": "polite" }),
    el("div", { id: "sg-poll", class: "hint" }),
    g.access_recovery ? el("p", { class: "sg-message" }, tr("recovery")) : mcpLine(g),
    el("div", { class: "sg-detail-block" },
      el("div", { class: "sg-sources-heading" }, el("h3", null, tr("sources")),
        hint(tr("groupSourceCount", { n: g.sources.length }))),
      g.sources.length ? el("div", { class: "sg-source-grid" }, ...g.sources.map((s) => {
        if (g.access_recovery) return el("div", { class: "sg-source-card sg-id" }, sid(s));
        const repo = (S.repos || []).find((r) => sid(r) === sid(s));
        const version = g.active_source_versions?.[sid(s)] || s.source_version || repo?.active_source_version;
        return el("div", { class: "sg-source-card" },
          el("div", { class: "sg-source-heading" }, el("strong", { class: "record-name" }, displayName(repo || s)),
            el("span", { class: "sg-role" }, ROLES.includes(s.role) ? tr(`role_${s.role}`) : text(s.role))),
          el("div", { class: "sg-id record-id" }, sid(s)),
          s.description ? el("p", { class: "sg-description" }, text(s.description)) : "",
          repo?.description ? hint(`${tr("commonDescription")}: ${text(repo.description)}`) : "",
          version ? el("dl", { class: "sg-source-facts" }, info("sourceVersion", version)) : hint(tr("noVersion")));
      })) : hint(tr("noGroupSources"))),
    el("div", { class: "sg-detail-block" }, el("h3", null, tr("buildSettings")),
      hint(tr(g.llm_enabled === true ? "aiOn" : "aiOff")),
      g.llm_enabled === true ? hint(`${tr("model")}: ${text(g.model || MODEL)}`) : "", hint(tr("cache"))),
    el("div", { class: "sg-detail-block sg-technical" }, el("h3", null, tr("versionDetails")),
      el("dl", { class: "sg-facts" }, info("revision", g.revision), info("version", g.active_version),
        info("activeRevision", g.active_revision), ...(g.build_id ? [info("buildId", g.build_id)] : []))));
  if (g.status === "BUILDING") $("sg-poll").textContent = tr(G.polls >= 12 ? "pollStopped" : "building");
  if (g.status === "STALE" || g.stale) $("sg-action-message").append(hint(tr("stale")));
  if (g.status === "PARTIAL") {
    const partial = el("p", { class: "sg-message" }, tr("partial"));
    const reasons = Array.isArray(g.partial_reasons) ? [...new Set(g.partial_reasons.map(safeReason).filter(Boolean))].slice(0, 16) : [];
    if (reasons.length) partial.append(" ", el("code", null, reasons.join(", ")));
    $("sg-action-message").append(partial);
  }
  const code = safeReason(g.last_error_code);
  if (g.status === "FAILED") {
    const failure = errorNode(tr("failed"));
    if (code) failure.append(" ", el("code", null, code));
    $("sg-action-message").append(failure);
  } else if (code) $("sg-action-message").append(hint(`${tr("errorCode")}: ${code}`));
  if (reasonHelp(code)) {
    $("sg-action-message").append(hint(tr(reasonHelp(code))));
    if (reasonHelp(code) === "sourceRebuildHelp") $("sg-action-message").append(button("goSources", () => switchTab("repos")));
  }
  if (g.last_error && g.status !== "FAILED") {
    $("sg-action-message").append(hint(`${tr("lastError")}: ${tr("failed")}`));
  }
  renderUsage();
}

/* Common descriptions and per-group descriptions have separate controls. */
function field(label, control, help) {
  return el("div", { class: "sg-field" }, el("label", { for: control.id, "data-i18n": `sg.${label}` }, tr(label)), control,
    help ? el("p", { class: "hint", "data-i18n": `sg.${help}` }, tr(help)) : "");
}
function input(id, value, max, multiline = false) {
  const control = el(multiline ? "textarea" : "input", { id, maxlength: String(max), ...(multiline ? { rows: "3" } : { type: "text" }) });
  control.value = text(value); return control;
}
function closeEditor(reason = "close") { return editorPanel?.close(reason) ?? true; }
function closeMembers(reason = "close") { return membersPanel?.close(reason) ?? true; }
function closeGroupPanels(reason) {
  return closeEditor(reason) && closeMembers(reason) && closeDescription(reason);
}
function groupReturnFocus(id, action, trigger) {
  return () => {
    if (trigger?.isConnected && !trigger.closest("dialog")) return trigger;
    const actions = [...document.querySelectorAll("#sg-root .sg-management-actions")]
      .find((node) => node.dataset.groupId === id);
    return actions?.querySelector(`[data-i18n="sg.${action}"]`)
      || $("sg-root")?.querySelector('[data-i18n="sg.new"]');
  };
}
async function manage(id, action) {
  if (!["edit", "delete", "members"].includes(action)) return;
  init();
  if (G.busy || G.draft) { $("sg-notice").replaceChildren(hint(tr("actionBusy"))); return; }
  const trigger = document.activeElement;
  // Management must remain available without a built or readable graph.
  // Use current metadata/role/revision rather than the cached list entry.
  const pending = open(id, { includeGraph: false }), intent = G.manageSerial;
  if (!await pending || intent !== G.manageSerial || !G.shown || G.id !== id || !G.meta) return;
  const g = G.meta;
  if (action === "edit" ? !editable(g) : !owner(g) || (action === "members" && g.access_recovery)) {
    $("sg-notice").replaceChildren(errorNode(tr("actionDenied")));
    return;
  }
  if (action === "edit") editGroup(g, trigger);
  else if (action === "members") showMembers(trigger);
  else await deleteGroup();
  if (action === "delete" && G.id === id) $("sg-detail")?.scrollIntoView({ block: "start", behavior: "smooth" });
}
function editGroup(g, trigger = document.activeElement) {
  init();
  if (!G.shown || G.busy || (g && (G.meta !== g || !editable(g))) || !closeGroupPanels("replace")) return;
  cancelAll(["description"]); G.manageSerial++;
  const selected = new Map((g?.sources || []).map((s) => [sid(s), g?.access_recovery
    ? { source_id: sid(s) } : { source_id: sid(s), role: s.role || "other", description: s.description || "" }]));
  const d = { id: g?.group_id || "", revision: g?.revision, selected, key: crypto.randomUUID(), g,
    saving: false, groupEpoch: S.groupEpoch };
  const content = el("section", { id: "sg-editor", class: "sg-panel", hidden: "" });
  const heading = translated("h2", g ? "edit" : "new", { id: "sg-editor-title" });
  content.append(heading);
  document.body.append(content);
  G.draft = d;
  renderEditor();
  const finish = (reason) => {
    if (editorPanel !== panel) return;
    editorPanel = null; G.draft = null; G.manageSerial++;
    cancel("mutation"); G.busy = false;
    panel.dispose(); content.remove();
    if (G.shown && reason !== "reset" && reason !== "success") {
      renderList(); renderMeta(); renderGraph(); schedulePoll();
      if (!G.meta) $("sg-detail").replaceChildren(hint(tr("choose")));
      if (!G.listLoaded) refreshList();
    }
  };
  const panel = ConsoleDialogs.mountPanel(content, {
    heading, titleId: heading.id, size: "xl", initialFocus: () => $("sg-name"),
    returnFocus: groupReturnFocus(d.id, d.id ? "edit" : "new", trigger),
    beforeClose: () => !d.saving, onClose: finish,
    closeLabel: () => tr("close"), busyMessage: () => tr("saving"),
  });
  editorPanel = panel;
  if (!panel.open()) { finish("replace"); return; }
  renderList(); renderMeta();
}
function renderEditor() {
  const d = G.draft; if (!d) return;
  const g = d.g;
  const name = input("sg-name", g?.name, 120); name.required = true;
  const description = input("sg-description", g?.description, 1000, true);
  const ai = el("input", { id: "sg-ai", type: "checkbox" }); ai.checked = g?.llm_enabled === true;
  const model = input("sg-model", g?.model || MODEL, 200); model.readOnly = true;
  const form = el("form", { id: "sg-form" },
    field("name", name),
    field("description", description, "descriptionHint"),
    el("fieldset", { class: "sg-fieldset" }, translated("legend", g?.access_recovery ? "recoverySources" : "selectSources"),
      el("input", { id: "sg-source-search", type: "search", placeholder: tr("searchSources"), "aria-label": tr("searchSources") }),
      el("div", { id: "sg-source-count", class: "hint", "aria-live": "polite" }),
      el("div", { id: "sg-source-picker", class: "sg-source-picker" })),
    translatedHint(g?.access_recovery ? "recovery" : "acl"), translatedHint("aclEdit"),
    el("fieldset", { class: "sg-fieldset sg-ai" }, translated("legend", "ai"),
      el("label", { class: "sg-check", for: "sg-ai" }, ai, translated("span", "aiEnable")),
      translatedHint("aiCost"), field("model", model), translatedHint("cache")),
    el("div", { id: "sg-editor-message", "aria-live": "polite" }),
    el("div", { class: "btns sg-form-actions" }, button("cancel", () => {
      if (G.draft === d) closeEditor("cancel");
    }), translated("button", g ? "save" : "new", { id: "sg-save", type: "submit", class: "mini" })));
  model.disabled = !ai.checked;
  ai.addEventListener("change", () => { model.disabled = !ai.checked; });
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    if (G.draft === d && editorPanel?.isOpen) saveGroup();
  });
  $("sg-editor").append(form);
  $("sg-source-search").addEventListener("input", filterSources);
  renderSourcePicker();
}
function renderSourcePicker() {
  const d = G.draft; if (!d) return;
  const picker = $("sg-source-picker"); picker.replaceChildren();
  if (d.g?.access_recovery) {
    d.g.sources.forEach((s, i) => {
      const id = sid(s), check = el("input", { type: "checkbox", id: `sg-keep-${i}` });
      check.checked = d.selected.has(id);
      check.addEventListener("change", () => {
        if (check.checked) d.selected.set(id, { source_id: id }); else d.selected.delete(id);
        updateSourceCount();
      });
      picker.append(el("div", { class: "sg-pick-row", "data-sg-search": id.toLowerCase() },
        el("label", { class: "sg-check", for: check.id }, check, el("span", { class: "sg-id" }, id))));
    });
    picker.append(translated("p", "noMatches", { id: "sg-no-matches", class: "hint", hidden: "" }));
    updateSourceCount(); return;
  }
  const rows = [...(S.repos || [])].filter((r) => r.enabled !== false && r.status !== "DELETED");
  const missing = [...d.selected.keys()].filter((id) => !rows.some((r) => sid(r) === id));
  rows.push(...missing.map((id) => ({ source_id: id, unavailable: true })));
  if (!rows.length) picker.append(translatedHint("noSources"), button("goSources", () => switchTab("repos")));
  rows.forEach((repo, i) => {
    const id = sid(repo), entry = d.selected.get(id);
    const check = el("input", { type: "checkbox", id: `sg-pick-${i}`, "data-sg-pick": id }); check.checked = !!entry;
    const role = el("select", { id: `sg-role-${i}` }, ...ROLES.map((r) => translated("option", `role_${r}`, { value: r })));
    if (entry?.role && !ROLES.includes(entry.role)) role.append(el("option", { value: entry.role }, text(entry.role)));
    role.value = entry?.role || "other";
    const description = input(`sg-source-desc-${i}`, entry?.description || "", 500, true);
    const extras = el("div", { class: "sg-source-fields" }, field("role", role), field("groupSourceDescription", description));
    extras.hidden = !entry;
    check.addEventListener("change", () => {
      if (check.checked) {
        if (d.selected.size >= 8 || repo.unavailable) { check.checked = false; return; }
        d.selected.set(id, { source_id: id, role: role.value, description: description.value });
      } else d.selected.delete(id);
      extras.hidden = !check.checked; updateSourceCount();
    });
    role.addEventListener("change", () => { if (d.selected.has(id)) d.selected.get(id).role = role.value; });
    description.addEventListener("input", () => { if (d.selected.has(id)) d.selected.get(id).description = description.value; });
    picker.append(el("div", { class: "sg-pick-row", "data-sg-search": `${displayName(repo)} ${id} ${text(repo.description)}`.toLowerCase() },
      el("label", { class: "sg-check", for: check.id }, check, el("strong", null, displayName(repo))),
      el("div", { class: "sg-id" }, id), repo.description ? el("p", { class: "hint" },
        translated("span", "commonDescription"), `: ${text(repo.description)}`) : "",
      repo.unavailable ? translated("p", "missingSource", { class: "sg-message sg-error", role: "alert" })
        : !repo.active_source_version ? translatedHint("noVersion") : "",
      extras));
  });
  picker.append(translated("p", "noMatches", { id: "sg-no-matches", class: "hint", hidden: "" }));
  updateSourceCount();
}
function updateSourceCount() {
  if (!G.draft) return;
  $("sg-source-count").textContent = tr("sourceCount", { n: G.draft.selected.size });
  document.querySelectorAll("[data-sg-pick]").forEach((check) => {
    check.disabled = G.draft.saving || (!check.checked && (G.draft.selected.size >= 8
      || !(S.repos || []).some((r) => sid(r) === check.dataset.sgPick && r.enabled !== false && r.status !== "DELETED")));
  });
}
function filterSources() {
  const query = $("sg-source-search").value.trim().toLowerCase(); let count = 0;
  document.querySelectorAll("[data-sg-search]").forEach((row) => {
    row.hidden = !row.dataset.sgSearch.includes(query); if (!row.hidden) count++;
  });
  $("sg-no-matches").hidden = count > 0;
}
async function saveGroup() {
  const d = G.draft, form = $("sg-form");
  if (!d || !editorPanel?.isOpen || !G.shown || G.busy || !form?.reportValidity()) return;
  if (d.id && (G.id !== d.id || !editable(G.meta)
      || !!G.meta.access_recovery !== !!d.g?.access_recovery)) {
    closeEditor("reset"); renderList(); renderMeta();
    $("sg-notice").replaceChildren(errorNode(tr("actionDenied")));
    return;
  }
  const body = {
    name: $("sg-name").value.trim(), description: $("sg-description").value.trim(),
    sources: [...d.selected.values()].map((s) => d.g?.access_recovery ? { source_id: s.source_id }
      : { source_id: s.source_id, role: s.role, description: s.description.trim() }),
    llm_enabled: $("sg-ai").checked, model: $("sg-model").value || MODEL,
    ...(d.id ? { expected_revision: d.revision } : { idempotency_key: d.key }),
  };
  if (!d.id) {
    const { idempotency_key, ...configuration } = body;
    const fingerprint = JSON.stringify(configuration);
    if (d.lastPayload && fingerprint !== d.lastPayload) d.key = crypto.randomUUID();
    d.lastPayload = fingerprint;
    body.idempotency_key = d.key;
  }
  if ((!body.sources.length && !d.g?.access_recovery) || body.sources.length > 8) {
    $("sg-editor-message").replaceChildren(errorNode(tr("sourceLimit"))); return;
  }
  if (!body.name || body.name.length > 120 || body.description.length > 1000 || body.sources.some((s) => (s.description || "").length > 500)) {
    $("sg-editor-message").replaceChildren(errorNode(tr(!body.name || body.name.length > 120 ? "nameRequired" : "invalid")));
    form.reportValidity(); return;
  }
  cancel("list"); const req = begin("mutation"); G.busy = true; d.saving = true;
  form.setAttribute("aria-busy", "true");
  form.querySelectorAll("input,textarea,select,button").forEach((node) => { node.disabled = true; });
  $("sg-save").dataset.i18n = "sg.saving";
  $("sg-save").textContent = tr("saving");
  try {
    const out = await request("mutation", req, "POST", d.id ? path(d.id) : "/groups", body);
    const g = normalMeta(out, d.id || out?.group?.group_id);
    G.groups = G.groups.filter((row) => row.group_id !== g.group_id);
    G.groups.unshift(g); G.listLoaded = true;
    G.id = g.group_id; G.meta = g; G.members = null; G.polls = 0;
    closeEditor("success"); clearGraph();
    applyAccessState(g, "mutation");
    renderList(); renderDetail();
    if (typeof updateSourceGroups === "function") updateSourceGroups(G.groups);
    $("sg-notice").replaceChildren(hint(tr("saved")));
    loadDetail(false);
    refreshList();
    refreshAll().catch(() => {});
  } catch (e) {
    if (!current("mutation", req) || aborted(e)) return;
    handleError(e, $("sg-editor-message"));
    if (!denied(e) && Number(e.status) === 409) {
      $("sg-editor-message").append(button("conflictReload", () => {
        if (G.draft === d && !d.saving && closeEditor("close")) open(d.id);
      }));
    }
  } finally {
    if (current("mutation", req)) {
      G.requests.delete("mutation"); G.busy = false; d.saving = false;
      if (G.draft === d && editorPanel?.isOpen && form.isConnected) {
        ConsoleDialogs.notify("ok", "");
        form.setAttribute("aria-busy", "false");
        form.querySelectorAll("input,textarea,select,button").forEach((node) => { node.disabled = false; });
        $("sg-model").disabled = !$("sg-ai").checked;
        $("sg-save").dataset.i18n = `sg.${d.id ? "save" : "new"}`;
        $("sg-save").textContent = tr(d.id ? "save" : "new"); updateSourceCount();
      }
    }
  }
}
async function rebuild() {
  const g = G.meta;
  if (!g || !editable(g) || G.busy || G.draft || membersPanel?.isOpen || g.status === "BUILDING" || g.access_recovery || !g.sources.length) return;
  const req = begin("mutation"); G.busy = true; clearTimeout(G.timer); renderMeta();
  // Retry the same revision with the same key after a lost network response.
  if (!G.buildIntent || G.buildIntent.id !== g.group_id || G.buildIntent.revision !== g.revision) {
    G.buildIntent = { id: g.group_id, revision: g.revision, key: crypto.randomUUID() };
  }
  try {
    const out = await request("mutation", req, "POST", `${path(g.group_id)}/rebuild`,
      { expected_revision: g.revision, idempotency_key: G.buildIntent.key });
    if (out.group_id !== g.group_id || !out.build_id) throw new Error(tr("invalid"));
    G.buildIntent = null;
    await open(g.group_id);
    if (G.id === g.group_id) $("sg-notice").replaceChildren(hint(`${tr("queued")} ${text(out.build_id)}`));
    refreshList();
  } catch (e) {
    if (!current("mutation", req) || aborted(e)) return;
    handleError(e, $("sg-action-message"), "rebuild");
    if (!denied(e) && Number(e.status) === 409) {
      G.buildIntent = null; clearGraph(); renderGraph();
      $("sg-action-message").append(button("goSources", () => switchTab("repos")), button("refresh", () => open(g.group_id)));
    }
  } finally {
    if (current("mutation", req)) {
      G.requests.delete("mutation"); G.busy = false;
      // Preserve the action error and the data actions' readiness checks.
      const build = $("sg-meta")?.querySelector('[data-i18n="sg.rebuild"]');
      if (build) build.disabled = G.meta?.status === "BUILDING" || !!G.meta?.access_recovery || !G.meta?.sources?.length;
      document.querySelectorAll("#sg-root .sg-management-actions button").forEach((node) => { node.disabled = !!G.draft; });
      renderList();
      schedulePoll();
    }
  }
}
async function deleteGroup() {
  const g = G.meta;
  if (!g || !owner(g) || G.busy || G.draft || membersPanel?.isOpen || !G.shown) return;
  const intent = G.manageSerial, revision = g.revision;
  if (!await confirmAction(`${g.name}\n\n${tr("deleteConfirm")}`)) return;
  // An async modal may outlive navigation, logout, or refreshed permissions.
  if (!G.shown || G.id !== g.group_id || G.manageSerial !== intent || G.meta !== g
      || !owner(G.meta) || G.meta.revision !== revision || G.busy || G.draft || membersPanel?.isOpen) return;
  const req = begin("mutation"); G.busy = true;
  try {
    await request("mutation", req, "DELETE", path(g.group_id), { expected_revision: g.revision });
    cancel("list");
    clearDetail(); G.id = ""; G.groups = G.groups.filter((row) => row.group_id !== g.group_id);
    if (typeof updateSourceGroups === "function") updateSourceGroups(G.groups);
    renderList(); $("sg-detail").replaceChildren(hint(tr("choose")));
    $("sg-notice").replaceChildren(hint(tr("deleted"))); refreshList(); refreshAll().catch(() => {});
  } catch (e) {
    if (current("mutation", req)) handleError(e, $("sg-action-message"));
  } finally { if (current("mutation", req)) { G.requests.delete("mutation"); G.busy = false; } }
}

/* Member changes have their own CAS revision; only owners can list members. */
function currentMembers(session) {
  return !!session && memberSession === session && membersPanel?.isOpen
    && G.shown && G.id === session.id && owner(G.meta) && !G.meta.access_recovery;
}
function showMembers(trigger = document.activeElement) {
  if (!G.shown || !owner(G.meta) || G.meta.access_recovery || G.busy || !closeGroupPanels("replace")) return;
  const session = { id: G.id, saving: false, needsDetail: false, groupEpoch: S.groupEpoch };
  const heading = translated("h2", "members", { id: "sg-members-title", tabindex: "-1" });
  const content = el("section", { id: "sg-members-section", class: "sg-panel", hidden: "" },
    heading, translatedHint("acl"), el("div", { id: "sg-members" }));
  document.body.append(content);
  const finish = (reason) => {
    if (membersPanel !== panel) return;
    membersPanel = null; memberSession = null; G.members = null; G.manageSerial++;
    cancel("members"); cancel("mutation"); G.busy = false;
    panel.dispose(); content.remove();
    if (G.shown && reason !== "reset") { renderList(); renderMeta(); schedulePoll(); }
  };
  const panel = ConsoleDialogs.mountPanel(content, {
    heading, titleId: heading.id, size: "lg",
    initialFocus: () => $("sg-member-email") || heading,
    returnFocus: groupReturnFocus(session.id, "manageMembers", trigger),
    beforeClose: () => !session.saving, onClose: finish,
    closeLabel: () => tr("close"), busyMessage: () => tr("saving"),
  });
  membersPanel = panel; memberSession = session;
  if (!panel.open()) { finish("replace"); return; }
  clearTimeout(G.timer); G.timer = null;
  loadMembers(session);
}
async function loadMembers(session = memberSession) {
  if (!currentMembers(session) || G.busy) return false;
  const req = begin("members");
  G.members = null; $("sg-members").replaceChildren(hint(tr("loading")));
  try {
    // A successful write requires fresh group metadata as well as the next
    // membership CAS revision. A failed refresh retries this read, not the write.
    if (session.needsDetail) {
      const meta = normalMeta(await request("members", req, "GET", path(req.id)), req.id);
      if (!currentMembers(session)) return false;
      if (meta.status === "DELETED") { revoke({ status: 410 }); return false; }
      G.meta = meta;
      const index = G.groups.findIndex((g) => g.group_id === req.id);
      if (index >= 0) G.groups[index] = meta;
      applyAccessState(meta, "members");
      renderList(); renderDetail();
      if (!currentMembers(session)) return false;
      session.needsDetail = false;
      if (typeof updateSourceGroups === "function") updateSourceGroups(G.groups);
    }
    if (!currentMembers(session) || !current("members", req)) return false;
    const out = await request("members", req, "GET", `${path(req.id)}/members`);
    if (!currentMembers(session)) return false;
    if (out.group_id !== req.id || !Array.isArray(out.members) || !Number.isInteger(out.revision)) throw new Error(tr("invalid"));
    G.members = out; renderMembers(session);
    return true;
  } catch (e) {
    if (!current("members", req) || !currentMembers(session) || aborted(e)) return false;
    handleError(e, $("sg-members"));
    if (!denied(e)) $("sg-members")?.append(button("retry", () => loadMembers(session)));
    return false;
  }
  finally { if (current("members", req)) G.requests.delete("members"); }
}
function renderMembers(session = memberSession) {
  if (!currentMembers(session) || !G.members || !$("sg-members")) return;
  const data = G.members;
  const email = el("input", { id: "sg-member-email", type: "email", required: "", maxlength: "254", autocomplete: "email" });
  const role = el("select", { id: "sg-member-role" }, ...["viewer", "editor"].map((r) => translated("option", r, { value: r })));
  const form = el("form", { class: "sg-member-form", onsubmit: (e) => {
    e.preventDefault();
    if (currentMembers(session) && G.members === data && form.reportValidity()) {
      changeMember(session, "POST", "", { email: email.value.trim(), role: role.value });
    }
  } }, field("email", email), field("memberRole", role), translated("button", "invite", { type: "submit", class: "mini" }));
  const rows = el("div", { class: "sg-member-list" });
  for (const m of data.members) {
    const removal = button("remove", async () => {
      const g = G.meta, intent = G.manageSerial, revision = data.revision;
      if (!currentMembers(session) || G.busy || G.draft
          || G.members !== data || m.role === "owner" || m.you === true || !m.sub) return;
      if (!await confirmAction(`${text(m.email || m.sub)}\n\n${tr("removeConfirm")}`)) return;
      if (!currentMembers(session) || G.meta !== g
          || G.manageSerial !== intent || G.members !== data || data.revision !== revision || G.busy || G.draft) return;
      await changeMember(session, "DELETE", text(m.sub));
    }, "danger mini");
    removal.disabled = m.role === "owner" || m.you === true || !m.sub;
    rows.append(el("div", { class: "sg-member" }, el("span", null, text(m.email || m.sub)),
      ["owner", "editor", "viewer"].includes(m.role) ? translated("span", m.role, { class: "sg-role" })
        : el("span", { class: "sg-role" }, text(m.role)), removal));
  }
  $("sg-members").replaceChildren(form, el("div", { id: "sg-members-message", "aria-live": "polite" }),
    rows, data.members.length ? "" : translatedHint("noMembers"));
}
async function changeMember(session, method, sub, body = {}) {
  if (!currentMembers(session) || !G.members || G.busy) return;
  cancel("detail"); cancel("list");
  const id = session.id, revision = G.members.revision, req = begin("mutation");
  G.busy = true; session.saving = true;
  const content = $("sg-members"), result = $("sg-members-message");
  const controls = [...content.querySelectorAll("input,select,button")].map((node) => [node, node.disabled]);
  controls.forEach(([node]) => { node.disabled = true; });
  content.setAttribute("aria-busy", "true"); result.replaceChildren(translatedHint("saving"));
  try {
    const out = await request("mutation", req, method, `${path(id)}/members${sub ? `/${encodeURIComponent(sub)}` : ""}`,
      { ...body, expected_revision: revision });
    if (out.group_id !== id || !Number.isInteger(out.revision)) throw new Error(tr("invalid"));
    if (!currentMembers(session)) return;
    // Keep the mounted form independent of any detail-area re-render.
    G.members = null; session.needsDetail = true; clearGraph(); renderGraph();
    G.requests.delete("mutation"); G.busy = false; session.saving = false;
    ConsoleDialogs.notify("ok", "");
    content.setAttribute("aria-busy", "false");
    if (await loadMembers(session) && currentMembers(session)) {
      $("sg-members-message").replaceChildren(hint(tr(method === "POST" ? "invited" : "removed")));
    }
    if (currentMembers(session)) refreshList();
  } catch (e) {
    if (!current("mutation", req) || !currentMembers(session) || aborted(e)) return;
    handleError(e, result);
    if (!denied(e) && Number(e.status) === 409) {
      session.needsDetail = true;
      clearGraph(); renderGraph(); result.append(button("refresh", () => loadMembers(session)));
    }
  } finally {
    if (current("mutation", req)) {
      G.requests.delete("mutation"); G.busy = false; session.saving = false;
      if (currentMembers(session)) {
        ConsoleDialogs.notify("ok", "");
        content.setAttribute("aria-busy", "false");
        controls.forEach(([node, disabled]) => { if (node.isConnected) node.disabled = disabled; });
      }
    }
  }
}
function closeDescription(reason = "close") { return descriptionPanel?.close(reason) ?? true; }
function editSourceDescription(repo) {
  const latest = (S.repos || []).find((row) => sid(row) === sid(repo));
  if (!latest?.manageable || !closeGroupPanels("replace")) return;
  repo = latest;
  const trigger = document.activeElement, session = { id: sid(repo), saving: false };
  const description = input("sg-common-description", repo.description, 500, true);
  const save = el("button", { type: "submit", class: "mini", "data-i18n": "sg.save" }, tr("save"));
  const result = el("div", { "aria-live": "polite" });
  const heading = translated("h2", "editDescription", { id: "sg-description-title" });
  const form = el("form", null,
    el("p", { class: "sg-id" }, sid(repo)), field("sourceDescription", description, "sourceDescriptionHint"), result,
    el("div", { class: "btns sg-form-actions" }, button("cancel", () => {
      if (descriptionSession === session) closeDescription("cancel");
    }), save));
  const content = el("section", { id: "sg-description-editor", class: "sg-panel sg-dialog", hidden: "" }, heading, form);
  document.body.append(content);
  const finish = () => {
    if (descriptionPanel !== panel) return;
    descriptionPanel = null; descriptionSession = null;
    cancel("description"); panel.dispose(); content.remove();
  };
  const panel = ConsoleDialogs.mountPanel(content, {
    heading, titleId: heading.id, size: "md", initialFocus: () => description,
    returnFocus: () => trigger?.isConnected ? trigger : $("repos-search"),
    beforeClose: () => !session.saving, onClose: finish,
    closeLabel: () => tr("close"), busyMessage: () => tr("saving"),
  });
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (descriptionSession !== session || !panel.isOpen || !form.reportValidity() || session.saving) return;
    if (!(S.repos || []).some((row) => sid(row) === session.id && row.manageable)) {
      closeDescription("reset"); flash("err", tr("actionDenied")); return;
    }
    const value = description.value.trim();
    if (value.length > 500) { result.replaceChildren(errorNode(tr("descriptionLimit"))); return; }
    const req = begin("description"); session.saving = true; save.disabled = true; description.disabled = true;
    save.dataset.i18n = "sg.saving"; save.textContent = tr("saving");
    form.setAttribute("aria-busy", "true");
    try {
      await request("description", req, "POST", `/repos/${encodeURIComponent(sid(repo))}/description`, { description: value });
      if (descriptionSession !== session || !panel.isOpen) return;
      repo.description = value;
      const currentRepo = (S.repos || []).find((row) => sid(row) === session.id);
      if (currentRepo) currentRepo.description = value;
      // Replace the old row before restoring focus. Its detached opener falls
      // back to repos-search, which also survives the subsequent refreshAll.
      renderRepos(); closeDescription("success");
      if (G.meta?.sources.some((s) => sid(s) === sid(repo))) {
        G.meta.stale = true; G.meta.data_ready = false; clearGraph(); renderMeta(); renderGraph();
      }
      flash("ok", tr("descriptionSaved")); refreshAll().catch(() => {});
    } catch (err) {
      if (current("description", req) && !aborted(err)) {
        if (denied(err)) { closeDescription("reset"); flash("err", message(err)); }
        else result.replaceChildren(errorNode(message(err)));
      }
    } finally {
      if (current("description", req)) {
        G.requests.delete("description"); session.saving = false;
        ConsoleDialogs.notify("ok", "");
        save.disabled = false; description.disabled = false;
        save.dataset.i18n = "sg.save"; save.textContent = tr("save");
        form.setAttribute("aria-busy", "false");
      }
    }
  });
  descriptionPanel = panel; descriptionSession = session;
  if (!panel.open()) finish();
}

/* Read only relation chunks: this source-level view needs no node downloads. */
const MAX_RELATIONS = 2000;
function endpoint(relation, side) {
  const value = relation?.[side];
  return value && typeof value === "object" ? value : {};
}
function normalizeRelation(r) {
  if (!r || typeof r !== "object") return null;
  if (r.from && r.to) return { ...r, edge_id: r.edge_id || r.id };
  // The worker's directed relation contract orders evidence as source, target.
  // Keep opaque node IDs intact; never infer a source from an ID prefix/path.
  if (typeof r.source !== "string" || typeof r.target !== "string"
      || !Array.isArray(r.evidence) || r.evidence.length !== 2 || r.evidence.some((e) => !e?.source_id)) return null;
  return { ...r, edge_id: r.id || r.edge_id,
    from: { source_id: r.evidence[0].source_id, source_version: r.evidence[0].source_version, node_id: r.source },
    to: { source_id: r.evidence[1].source_id, source_version: r.evidence[1].source_version, node_id: r.target } };
}
function crossRelation(relation, ids) {
  const from = endpoint(relation, "from"), to = endpoint(relation, "to");
  return relation && typeof relation.edge_id === "string" && relation.edge_id
    && typeof relation.relation === "string" && ids.has(from.source_id) && ids.has(to.source_id)
    && from.source_id !== to.source_id;
}
function graphSources(value) {
  if (Array.isArray(value)) return value.map((s) => typeof s === "string" ? { source_id: s } : s);
  return [];
}
async function loadGraph(more = false) {
  const meta = G.meta;
  if (!meta || !G.shown || G.draft || G.busy || G.requests.has("graph")) return;
  if (meta.data_ready === false || meta.active_revision !== meta.revision || !meta.active_version) {
    clearGraph(); renderGraph(); return;
  }
  const existing = more ? G.graph : null;
  if (more && (!existing || existing.next == null || existing.relations.length >= MAX_RELATIONS)) return;
  if (!more) clearGraph();
  const req = begin("graph"), offset = existing?.next ?? 0;
  const query = new URLSearchParams({ kind: "relations", offset: String(offset), limit: "500" });
  if (existing) query.set("group_version", existing.version);
  if (existing) { renderGraph(); }
  else $("sg-graph").replaceChildren(hint(tr("loading")));
  try {
    const out = await request("graph", req, "GET", `${path(req.id)}/graph?${query}`);
    const sources = graphSources(out.sources), ids = new Set(sources.map(sid));
    if (out.group_id !== req.id || typeof out.version !== "string" || !out.version
        || out.revision !== meta.revision || !Array.isArray(out.relations)
        || out.relations.length > 500 || sources.length > 8 || sources.length !== ids.size
        || ids.size !== meta.sources.length || meta.sources.some((s) => !ids.has(sid(s)))
        || (existing && out.version !== existing.version) || out.version !== meta.active_version
        || (out.next_offset != null && (!Number.isInteger(out.next_offset) || out.next_offset <= offset))) {
      throw new Error(tr("invalid"));
    }
    const byId = new Map((existing?.relations || []).map((r) => [r.edge_id, r]));
    for (const value of out.relations) {
      const relation = normalizeRelation(value);
      if (crossRelation(relation, ids)) byId.set(relation.edge_id, relation);
    }
    G.graph = { version: out.version, revision: out.revision, sources,
      relations: [...byId.values()].slice(0, MAX_RELATIONS), next: out.next_offset ?? null,
      stats: out.stats || {}, usage: out.usage, status: out.status, partial: out.partial === true,
      partialReasons: Array.isArray(out.partial_reasons) ? [...new Set(out.partial_reasons.map(safeReason).filter(Boolean))].slice(0, 16) : [],
      pages: (existing?.pages || 0) + 1 };
    renderGraph(); renderUsage();
  } catch (e) {
    if (!current("graph", req) || aborted(e)) return;
    clearGraph();
    handleError(e, $("sg-graph"));
    if (!denied(e)) $("sg-graph")?.append(button("retry", () => open(req.id)));
  } finally {
    if (current("graph", req)) {
      G.requests.delete("graph");
      if (G.graph) renderGraph();
    }
  }
}
function filteredRelations() {
  return (G.graph?.relations || []).filter((r) => !G.filter || r.from.source_id === G.filter || r.to.source_id === G.filter);
}
function relationLabel(r) {
  return `${sourceName(r.from.source_id)} → ${sourceName(r.to.source_id)} (${r.relation})`;
}
function renderGraph() {
  const box = $("sg-graph");
  if (!box) return;
  const restoreFilterFocus = document.activeElement?.id === "sg-relation-filter";
  const restoreRelationFocus = document.activeElement?.classList.contains("sg-relation")
    ? document.activeElement.dataset.relationId : null;
  const graph = G.graph, meta = G.meta;
  box.replaceChildren();
  if (!graph) {
    const readable = dataReady(meta) && meta.active_version && meta.active_revision === meta.revision;
    box.append(hint(tr(meta?.access_recovery ? "recovery" : (meta?.stale && meta?.active_version) || meta?.status === "STALE" ? "stale"
      : meta?.active_version && meta?.active_revision !== meta?.revision ? "changed" : readable ? "graphNotLoaded" : "pending")));
    if (readable) {
      box.append(button("graphLoad", () => loadGraph()));
    }
    if ($("sg-evidence")) $("sg-evidence").replaceChildren(el("h2", null, tr("evidence")), hint(tr("evidenceHint")));
    return;
  }
  const relations = filteredRelations();
  const filter = el("select", { id: "sg-relation-filter" },
    el("option", { value: "" }, tr("allSources")),
    ...graph.sources.map((s) => el("option", { value: sid(s) }, sourceName(sid(s)))));
  filter.value = G.filter;
  filter.addEventListener("change", () => {
    G.filter = filter.value; G.visibleRelations = 40; renderGraph();
  });
  box.append(el("div", { class: "sg-graph-toolbar" }, field("sourceFilter", filter),
    hint(`${tr("version")}: ${graph.version}, ${tr("loaded", { n: graph.relations.length })}`)));
  box.append(drawGraph(graph.sources, relations));
  if (graph.status === "PARTIAL" || graph.partial) box.append(el("p", { class: "sg-message" }, tr("partial")));
  if (graph.partialReasons?.length) {
    box.append(el("div", { class: "sg-partial-reasons" }, el("h3", null, tr("partialReasons")),
      el("ul", null, ...graph.partialReasons.map((code) => el("li", null,
        el("code", null, code), reasonHelp(code) ? `: ${tr(reasonHelp(code))}` : "")))));
  }
  if (!relations.length) box.append(hint(tr(G.filter ? "noFilteredRelations" : "noRelations")));
  const list = el("div", { class: "sg-relations", "aria-label": tr("graph") });
  for (const r of relations.slice(0, G.visibleRelations)) {
    list.append(el("button", { type: "button", class: `sg-relation${G.selected?.edge_id === r.edge_id ? " sg-selected" : ""}`,
      onclick: () => selectRelation(r), "data-relation-id": r.edge_id, "aria-controls": "sg-evidence",
      "aria-pressed": String(G.selected?.edge_id === r.edge_id) },
      el("span", null, relationLabel(r)),
      el("small", null, `${text(r.evidence_kind)}${r.method ? ` (${text(r.method)})` : ""}`)));
  }
  box.append(hint(tr("filtered", { n: Math.min(relations.length, G.visibleRelations) })), list);
  const controls = el("div", { class: "btns" });
  if (relations.length > G.visibleRelations) controls.append(button("showMore", () => { G.visibleRelations += 40; renderGraph(); }));
  if (graph.next != null && graph.relations.length < MAX_RELATIONS && graph.pages < 40) {
    const more = button(G.requests.has("graph") ? "loading" : "more", () => loadGraph(true));
    more.disabled = G.requests.has("graph"); controls.append(more);
  } else if (graph.next != null) controls.append(hint(tr("graphLimit")));
  box.append(controls);
  if (restoreFilterFocus) $("sg-relation-filter").focus({ preventScroll: true });
  if (restoreRelationFocus) [...list.querySelectorAll("button")]
    .find((node) => node.dataset.relationId === restoreRelationFocus)?.focus({ preventScroll: true });
}
function svgNode(tag, attrs = {}, label) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, String(value));
  if (label != null) node.textContent = text(label);
  return node;
}
function shortLabel(value, max = 22) {
  let result = "", width = 0;
  for (const char of value) {
    width += char.codePointAt(0) > 255 ? 2 : 1;
    if (width > max) return result + "…";
    result += char;
  }
  return result;
}
function drawGraph(sources, relations) {
  const narrow = $("sg-graph").clientWidth < 520;
  const height = narrow ? Math.max(160, sources.length * 100 + 24) : 480;
  const svg = svgNode("svg", { viewBox: `0 0 ${narrow ? 360 : 800} ${height}`,
    class: `sg-svg${narrow ? " sg-svg-narrow" : ""}`, role: "img", "aria-label": tr("graphAlt") });
  const defs = svgNode("defs");
  const marker = svgNode("marker", { id: "sg-arrow", viewBox: "0 0 10 10", refX: 9, refY: 5,
    markerWidth: 6, markerHeight: 6, orient: "auto-start-reverse" });
  marker.append(svgNode("path", { d: "M 0 0 L 10 5 L 0 10 z", fill: "#64748b" })); defs.append(marker); svg.append(defs);
  const points = new Map(sources.map((s, i) => {
    if (narrow) return [sid(s), { x: 124, y: 62 + i * 100 }];
    const angle = -Math.PI / 2 + 2 * Math.PI * i / Math.max(1, sources.length);
    return [sid(s), { x: 400 + (sources.length === 1 ? 0 : 280 * Math.cos(angle)), y: 240 + (sources.length === 1 ? 0 : 162 * Math.sin(angle)) }];
  }));
  // One path per actual relation preserves direction and parallel edges.
  const pairs = new Map();
  for (const r of relations) {
    const key = JSON.stringify([r.from.source_id, r.to.source_id]);
    if (!pairs.has(key)) pairs.set(key, []);
    pairs.get(key).push(r);
  }
  for (const rows of pairs.values()) rows.forEach((r, i) => {
    const a = points.get(r.from.source_id), b = points.get(r.to.source_id);
    const dx = b.x - a.x, dy = b.y - a.y, len = Math.hypot(dx, dy) || 1;
    const ux = dx / len, uy = dy / len;
    const start = Math.min(90 / Math.max(Math.abs(ux), 0.001), 31 / Math.max(Math.abs(uy), 0.001));
    const bend = (i - (rows.length - 1) / 2) * Math.min(6, 70 / Math.max(rows.length, 1)) + 12;
    // In narrow panels, readable anchors stack vertically and actual relations
    // arc beside them. Scaling the desktop ring made source labels too small.
    const curve = narrow
      ? `M ${a.x + 91} ${a.y} Q ${Math.min(344, 276 + (a.y > b.y ? 28 : 0) + i * 4)} ${(a.y + b.y) / 2} ${b.x + 98} ${b.y}`
      : `M ${a.x + ux * start} ${a.y + uy * start} Q ${(a.x + b.x) / 2 - uy * bend} ${(a.y + b.y) / 2 + ux * bend} ${b.x - ux * (start + 7)} ${b.y - uy * (start + 7)}`;
    const line = svgNode("path", {
      d: curve,
      class: `sg-edge${G.selected?.edge_id === r.edge_id ? " sg-edge-selected" : ""}`, "marker-end": "url(#sg-arrow)",
      "data-relation-id": r.edge_id,
    });
    line.append(svgNode("title", {}, relationLabel(r)));
    line.addEventListener("click", () => selectRelation(r));
    svg.append(line);
  });
  for (const source of sources) {
    const id = sid(source), point = points.get(id), member = G.meta.sources.find((s) => sid(s) === id);
    const node = svgNode("g", { class: `sg-anchor${G.filter === id ? " sg-anchor-selected" : ""}`,
      transform: `translate(${point.x},${point.y})` });
    node.append(svgNode("title", {}, `${sourceName(id)}\n${id}`),
      svgNode("rect", { x: -90, y: -31, width: 180, height: 62, rx: 12 }),
      svgNode("text", { y: -3, "text-anchor": "middle", class: "sg-anchor-name" }, shortLabel(sourceName(id))),
      svgNode("text", { y: 18, "text-anchor": "middle", class: "sg-anchor-role" }, ROLES.includes(member?.role) ? tr(`role_${member.role}`) : text(member?.role)));
    node.addEventListener("click", () => { G.filter = G.filter === id ? "" : id; G.visibleRelations = 40; renderGraph(); });
    svg.append(node);
  }
  return svg;
}
function selectRelation(r) {
  cancel("source"); G.sourceText = null; G.selected = r;
  $("sg-source").replaceChildren(); $("sg-source").hidden = true;
  renderGraph(); renderEvidence();
  $("sg-evidence").scrollIntoView({ block: "nearest", behavior: "smooth" });
}
function evidencePair(r) {
  if (!Array.isArray(r.evidence) || r.evidence.length !== 2) return null;
  const from = r.evidence.filter((e) => e && e.source_id === r.from.source_id);
  const to = r.evidence.filter((e) => e && e.source_id === r.to.source_id);
  return from.length === 1 && to.length === 1 ? [from[0], to[0]] : null;
}
function locationOf(e) {
  const range = e.range || {};
  return { file: text(e.file), start: e.line_start ?? e.start_line ?? range.start_line ?? range.start,
    end: e.line_end ?? e.end_line ?? range.end_line ?? range.end };
}
function sourceArgs(e, endpointValue) {
  const loc = locationOf(e);
  const graphSource = G.graph?.sources.find((s) => sid(s) === e.source_id);
  const pinnedSource = text(graphSource?.source_version || graphSource?.version || G.meta?.active_source_versions?.[e.source_id]);
  const version = text(e.source_version || e.version || endpointValue.source_version || pinnedSource);
  if (!loc.file || !Number.isInteger(loc.start) || !Number.isInteger(loc.end) || loc.start < 1 || loc.end < loc.start
      || loc.end - loc.start >= 400 || !version || (pinnedSource && version !== pinnedSource)
      || (endpointValue.source_version && version !== endpointValue.source_version)) return null;
  return { group_version: G.graph.version, source_id: e.source_id, file: loc.file, start_line: loc.start, end_line: loc.end };
}
function renderEvidence() {
  const r = G.selected, box = $("sg-evidence");
  if (!r || !G.graph || !box) return;
  const pair = evidencePair(r);
  box.replaceChildren(el("h2", null, tr("evidence")), el("p", { class: "sg-relation-title" }, relationLabel(r)),
    el("div", { class: "sg-id" }, r.edge_id),
    el("dl", { class: "sg-facts" }, info("version", G.graph.version),
      info("evidenceKind", r.evidence_kind), info("method", r.method), info("model", r.model_id),
      info("review", r.review_status), ...(typeof r.confidence_score === "number" ? [info("confidence", r.confidence_score)] : [])),
    hint(tr("confidenceHint")));
  if (!pair) { box.append(errorNode(tr("missingEvidence"))); return; }
  const grid = el("div", { class: "sg-evidence-grid" });
  pair.forEach((e, i) => {
    const loc = locationOf(e), ep = i === 0 ? r.from : r.to, args = sourceArgs(e, ep);
    const card = el("article", { class: "sg-evidence-card" },
      el("h3", null, `${tr(i === 0 ? "from" : "to")}: ${sourceName(e.source_id)}`),
      el("div", { class: "sg-id" }, text(ep.node_id)),
      el("dl", { class: "sg-facts" }, info("file", loc.file),
        info("sourceVersion", e.source_version || e.version || G.meta.active_source_versions?.[e.source_id])),
      Number.isInteger(loc.start) && Number.isInteger(loc.end) ? hint(tr("lines", { a: loc.start, b: loc.end })) : "",
      el("pre", { class: "sg-quote", tabindex: "0", role: "region",
        "aria-label": `${tr("evidence")}: ${sourceName(e.source_id)}` }, typeof e.quote === "string" ? e.quote : tr("noQuote")));
    if (args) card.append(button("sourceRead", () => readSource(args), "mini"));
    else card.append(hint(tr("locationMissing")));
    grid.append(card);
  });
  box.append(grid);
}
function sourceResult(out) {
  const result = out?.result || out;
  if (result?.isError || result?.error) throw new Error(text(result.error || result.content?.find((b) => b.type === "text")?.text || tr("sourceReadError")));
  if (typeof result?.text === "string") return result.text;
  if (typeof result?.content === "string") return result.content;
  if (typeof result?.structuredContent?.text === "string") return result.structuredContent.text;
  if (Array.isArray(result?.content)) {
    const blocks = result.content.filter((block) => block.type === "text" && typeof block.text === "string");
    if (blocks.length) return blocks.map((block) => block.text).join("\n");
  }
  throw new Error(tr("invalid"));
}
async function readSource(args) {
  if (!G.graph || args.group_version !== G.graph.version || !G.shown) return;
  const req = begin("source"); G.sourceText = null;
  const target = $("sg-source"); target.hidden = false;
  target.replaceChildren(el("h2", null, tr("sourceReadTitle")), hint(tr("loading")), button("close", closeSource));
  try {
    const out = await request("source", req, "POST", `${path(req.id)}/source`, args);
    const provenance = out?.result?.structuredContent || out?.structuredContent || out?.result || out;
    if (G.graph?.version !== args.group_version
        || (provenance.group_version && provenance.group_version !== args.group_version)
        || (provenance.source_id && provenance.source_id !== args.source_id)
        || (provenance.file && provenance.file !== args.file)
        || (provenance.source_version && G.meta.active_source_versions?.[args.source_id]
          && provenance.source_version !== G.meta.active_source_versions[args.source_id])) throw new Error(tr("invalid"));
    const value = sourceResult(out);
    if (/^error\s*:/i.test(value.trim())) throw new Error(value);
    G.sourceText = { args, value }; renderSourceText();
  } catch (e) {
    if (!current("source", req) || aborted(e)) return;
    if (denied(e)) { revoke(e); return; }
    if (Number(e.status) === 409) {
      clearGraph(); renderGraph();
      $("sg-graph").append(errorNode(tr("versionError")), button("refresh", () => open(req.id)));
    } else target.replaceChildren(errorNode(message(e, "source")),
      el("div", { class: "btns" }, button("retry", () => readSource(args)), button("close", closeSource)));
  } finally { if (current("source", req)) G.requests.delete("source"); }
}
function closeSource() {
  cancel("source"); G.sourceText = null;
  if ($("sg-source")) { $("sg-source").replaceChildren(); $("sg-source").hidden = true; }
}
function renderSourceText() {
  if (!G.sourceText || !$("sg-source")) return;
  const { args, value } = G.sourceText;
  $("sg-source").hidden = false;
  $("sg-source").replaceChildren(el("h2", null, tr("sourceReadTitle")),
    el("dl", { class: "sg-facts" }, info("version", args.group_version), info("file", args.file)),
    hint(`${sourceName(args.source_id)}, ${tr("lines", { a: args.start_line, b: args.end_line })}`),
    el("pre", { class: "sg-pre", tabindex: "0" }, value),
    button("close", closeSource));
}
function renderUsage() {
  const box = $("sg-usage"); if (!box || !G.meta) return;
  const usage = G.graph?.usage || G.meta.usage;
  box.replaceChildren(el("h2", null, tr("usage")), hint(tr("usageHint")));
  if (!usage || typeof usage !== "object" || !Object.keys(usage).length) { box.append(hint(tr("noUsage"))); return; }
  if (typeof usage.unknown_usage_attempts === "number" && Number.isFinite(usage.unknown_usage_attempts) && usage.unknown_usage_attempts > 0) {
    box.append(el("p", { class: "sg-message", role: "status" }, tr("unknownUsage", { n: usage.unknown_usage_attempts })));
  }
  const metrics = [
    ["inputTokens", ["input_tokens"]], ["outputTokens", ["output_tokens"]],
    ["calls", ["model_calls", "llm_calls", "calls"]], ["cachedPairs", ["cached_pairs", "cache_hits", "reused_pairs"]],
    ["analyzedPairs", ["analyzed_pairs", "candidate_pairs"]],
  ];
  const facts = el("dl", { class: "sg-facts" });
  for (const [label, keys] of metrics) {
    const value = keys.map((key) => usage[key]).find((v) => typeof v === "number" && Number.isFinite(v) && v >= 0);
    facts.append(info(label, value == null ? "-" : value.toLocaleString(LANG === "ko" ? "ko-KR" : "en-US")));
  }
  const counts = Object.fromEntries(Object.entries(usage)
    .filter(([key, value]) => !/usd|cost|price/i.test(key) && typeof value === "number" && Number.isFinite(value)));
  box.append(facts, el("details", null, el("summary", null, tr("rawUsage")), el("pre", { class: "sg-pre" }, JSON.stringify(counts, null, 2))));
}
function onData() {
  if (!G.initialized) return;
  if (descriptionSession && !(S.repos || []).some((r) => sid(r) === descriptionSession.id && r.manageable)) {
    closeDescription("reset"); flash("err", tr("actionDenied"));
  }
  const session = G.draft?.id ? G.draft : memberSession;
  if (session && session.groupEpoch !== S.groupEpoch && !S.groupsError) {
    session.groupEpoch = S.groupEpoch;
    const selected = (S.groups || []).find((g) => g.group_id === session.id);
    if (!selected) revoke({ status: 403 }, true);
    else if (!editable(selected) || (memberSession && !owner(selected)) || selected.access_recovery
        || roleOf(selected) !== roleOf(G.meta)
        || !!selected.access_recovery !== !!G.meta?.access_recovery) {
      G.groups = [...S.groups]; G.meta = selected;
      clearGraph(); applyAccessState(selected, "list");
      renderList(); renderDetail();
    }
  }
  // Existing source polling never fans out into group or model API polling.
  if (G.graph && G.meta) {
    const changed = (S.repos || []).some((r) => Object.hasOwn(G.meta.active_source_versions || {}, sid(r))
      && r.active_source_version && r.active_source_version !== G.meta.active_source_versions[sid(r)]);
    if (changed) { G.meta.stale = true; G.meta.data_ready = false; clearGraph(); renderMeta(); renderGraph(); }
  }
  if (G.lang !== LANG) {
    G.lang = LANG;
    // Translate mounted controls in place. Rebuilding a form loses its draft,
    // focus, CAS/idempotency intent and the disabled state of a pending save.
    for (const panel of [editorPanel, membersPanel, descriptionPanel]) {
      if (!panel?.isOpen) continue;
      panel.element.lang = LANG;
      panel.element.querySelectorAll("[data-i18n]").forEach((node) => { node.textContent = t(node.dataset.i18n); });
      panel.element.querySelectorAll("[data-panel-close]").forEach((node) => { node.textContent = tr("close"); });
    }
    if (editorPanel?.isOpen) {
      $("sg-source-search").placeholder = tr("searchSources");
      $("sg-source-search").setAttribute("aria-label", tr("searchSources"));
      updateSourceCount();
    }
    renderShell(); renderList();
    if (G.meta) { renderDetail(); renderEvidence(); renderSourceText(); }
    else if (G.requests.has("detail")) $("sg-detail").replaceChildren(hint(tr("loading")));
    schedulePoll();
  }
}
function reset() {
  G.shown = false;
  closeGroupPanels("reset"); cancelAll(); clearGraph();
  G.id = ""; G.groups = []; G.meta = null; G.members = null; G.draft = null;
  G.listLoaded = false; G.busy = false; G.buildIntent = null; G.polls = 0; G.manageSerial++;
  Object.assign(listView, { query: "", status: "", sort: "name", page: 1, size: 25 });
  clearSourceGroups();
  if (G.initialized) { renderShell(); $("sg-list").replaceChildren(); }
  if ($("reg-description")) $("reg-description").value = "";
}
window.SourceGroups = {
  init, open, manage, editSourceDescription, reset, onData,
  onShow() {
    init(); if (G.shown) return;
    G.shown = true;
    refreshList();
    if (G.id) open(G.id);
  },
  onHide() {
    const wasShown = G.shown; G.shown = false;
    closeGroupPanels("reset");
    if (!wasShown) return;
    cancelAll(); clearGraph(); G.members = null; G.busy = false; G.manageSerial++;
  },
};
init();
})();
