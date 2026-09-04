/* Graph explorer for the Graphify console (the 그래프 tab).
 *
 * Renders a source's knowledge graph with sigma.js v3 (WebGL) over a
 * graphology model. Data comes from GET /repos/{id}/graph (or
 * /catalog/{id}/graph for public previews): a presigned S3 URL to the build's
 * compact viz bundle — columnar nodes/edges with a layout PRECOMPUTED at build
 * time (cdk/build_scripts/make_viz.py), so nothing moves in the browser and
 * the picture is identical on every visit. Builds older than make_viz.py fall
 * back to the raw graph.json (size-capped) with a client-side circlepack.
 *
 * Views: a community meta-graph (default above ~3k nodes) that drills down
 * into node-level views (one community, an ego network, or everything), with
 * a breadcrumb back. Filters never move nodes — they rebuild the view graph
 * (sigma's `hidden` still costs a full buffer refill, per measurement), and
 * hover/selection/path highlights are reducers over a partial refresh.
 *
 * Security: every string in the bundle is attacker-influenced repo/doc
 * content (the hub bundle mixes tenants). Text reaches the DOM only through
 * el()/textContent; id-keyed indexes are Maps; parsed objects are copied
 * field-by-field with coercion; the presigned URL is held in a local and
 * never persisted or echoed. Depends on index.html globals: el, t, I18N,
 * LANG, api, flash, S, CFG, serverName, copyText, switchTab, rebuildRepo.
 */
(() => {
"use strict";

/* ---------------- i18n (merged into the console's I18N tables) ---------------- */
const GX_I18N = {
  ko: {
    "gx.load": "불러오기", "gx.search.ph": "항목/파일 이름 검색 ( / )",
    "gx.view.communities": "연관 그룹", "gx.view.full": "모든 항목",
    "gx.fit": "화면에 맞추기 (F)", "gx.labels": "이름 표시/숨김 (L)", "gx.png": "이미지 저장", "gx.fullscreen": "전체 화면 (Esc로 종료)",
    "gx.filters": "보기 설정", "gx.color": "색으로 구분", "gx.color.community": "연관 그룹", "gx.color.type": "항목 종류",
    "gx.color.dir": "최상위 폴더", "gx.color.repo": "레포",
    "gx.types": "항목 종류", "gx.relations": "연결 종류", "gx.repos": "레포", "gx.mindeg": "최소 연결 수",
    "gx.inferred": "추론된 연결도 표시", "gx.legend": "범례", "gx.legend.filter": "검색…",
    "gx.legend.more": "{n}개 더 보기", "gx.legend.other": "기타 ({n}개, 회색)",
    "gx.src.hub": "허브 (모든 public 소스를 하나로 합친 그래프)", "gx.src.mine": "내 소스", "gx.src.catalog": "카탈로그 (구독하지 않은 public 소스)",
    "gx.src.none": "볼 수 있는 소스가 없습니다. 소스 탭에서 등록하거나 카탈로그에서 구독하세요.",
    "gx.crumb.root": "소스", "gx.crumb.community": "연관 그룹", "gx.crumb.ego": "주변", "gx.crumb.path": "경로", "gx.crumb.all": "모든 항목",
    "gx.hop": "{n}단계 이웃",
    "gx.st.loading": "불러오는 중", "gx.st.parsing": "그래프 구성 중…", "gx.st.nodes": "항목", "gx.st.edges": "연결",
    "gx.st.communities": "연관 그룹", "gx.st.shown": "표시 중", "gx.st.built": "빌드", "gx.st.layout": "배치: 빌드 때 미리 계산",
    "gx.st.layout.client": "배치: 브라우저에서 계산 (원본 graph.json)",
    "gx.empty.title": "소스를 선택하고 불러오기를 누르세요",
    "gx.empty.body": "빌드가 끝난 소스의 지식 그래프를 폴더 → 연관 그룹 → 항목 순서로 내려가며 볼 수 있습니다. 항목을 클릭하면 파일, 연결 관계가 오른쪽에 표시됩니다.",
    "gx.pending.title": "아직 그래프가 없습니다", "gx.pending.body": "첫 빌드가 끝나면 볼 수 있습니다. 빌드 상태: {s}",
    "gx.failed.title": "마지막 빌드가 실패했습니다", "gx.failed.body": "성공한 빌드가 없어 그래프가 없습니다.",
    "gx.toobig.title": "시각화 데이터가 없는 큰 그래프입니다",
    "gx.toobig.body": "이 빌드({size})에는 시각화 데이터가 없고 원본 graph.json이 너무 커서 브라우저에서 열 수 없습니다. 재빌드하면 시각화 데이터가 만들어집니다.",
    "gx.rawnotice": "이 빌드에는 시각화 데이터가 없어 원본 graph.json({size})으로 그립니다. 재빌드하면 더 빠르고 정돈된 배치를 얻습니다.",
    "gx.rebuild": "재빌드", "gx.retry": "다시 시도",
    "gx.err.libs": "그래프 라이브러리를 불러오지 못했습니다 (CDN 차단 또는 무결성 검증 실패). 네트워크를 확인하고 새로고침하세요.",
    "gx.err.webgl": "이 브라우저에서 WebGL을 사용할 수 없어 그래프를 그릴 수 없습니다. 하드웨어 가속을 켜거나 다른 브라우저를 사용하세요.",
    "gx.err.fetch": "그래프 데이터를 내려받지 못했습니다", "gx.err.expired": "다운로드 링크가 만료되었습니다. 다시 불러오세요.",
    "gx.err.parse": "그래프 데이터를 해석할 수 없습니다",
    "gx.big.warn": "항목 {n}개를 한 번에 그립니다. 폴더나 연관 그룹 뷰에서 시작하는 편이 읽기 쉽습니다.",
    "gx.ins.empty": "항목을 클릭하면 파일, 연결, 이웃이 여기 표시됩니다. 원을 더블클릭하면 안으로 들어갑니다.",
    "gx.ins.shortcuts": "단축키: / 검색, F 화면 맞춤, L 이름 표시, +/- 확대, Esc 선택 해제/뒤로, Alt+← 상위 화면",
    "gx.ins.stats": "그래프 요약", "gx.ins.types": "항목 종류", "gx.ins.relations": "연결 종류", "gx.ins.top": "연결이 많은 항목",
    "gx.ins.file": "파일", "gx.ins.id": "ID", "gx.ins.degree": "연결 수", "gx.ins.in": "들어옴", "gx.ins.out": "나감",
    "gx.ins.community": "연관 그룹", "gx.ins.repo": "레포", "gx.ins.neighbors": "연결된 항목",
    "gx.ins.copy": "복사", "gx.ins.copied": "복사됨", "gx.ins.more": "{n}개 더 보기",
    "gx.ins.ego1": "연결된 항목만 보기", "gx.ins.ego2": "두 단계까지 보기", "gx.ins.pathA": "여기서 출발", "gx.ins.pathB": "여기까지 경로",
    "gx.ins.ctx": "설명용 텍스트 복사", "gx.ins.ask": "플레이그라운드에 질문", "gx.ins.drill": "안으로 들어가기",
    "gx.ins.auto": "자동으로 붙인 이름입니다 (빌드가 그룹 이름을 정하지 않음. 문서 AI 추출을 켜면 의미 있는 이름이 생깁니다)",
    "gx.ins.size": "항목 수", "gx.ins.dir": "주요 폴더", "gx.ins.hubs": "중심 항목", "gx.ins.linked": "연결된 연관 그룹",
    "gx.community.n": "연관 그룹 #{n}", "gx.src.pick": "그래프를 볼 소스",
    "gx.code.title": "소스 코드", "gx.code.view": "소스 보기", "gx.code.auto": "항목을 선택하면 자동으로 소스 보기", "gx.code.up": "▲ 위 40줄", "gx.code.down": "▼ 아래 40줄", "gx.code.loading": "소스 불러오는 중…",
    "gx.code.lines": "{a}–{b}줄 / 전체 {n}줄", "gx.code.copy": "코드 복사", "gx.code.copied": "코드를 복사했습니다",
    "gx.code.nofile": "이 항목에는 파일 정보가 없습니다.", "gx.code.err": "소스를 읽지 못했습니다",
    "gx.code.hubnote": "허브 그래프의 항목은 원본 레포 서버({repo})에서 읽습니다.",
    "gx.code.pageonly": "이 항목의 위치는 페이지({page})로만 기록되어 파일 처음부터 표시합니다. 소스를 재빌드하면 해당 페이지 위치로 이동합니다.",
    "gx.code.noloc": "이 항목에는 줄 위치 정보가 없어 파일 처음부터 표시합니다 (추출 시 위치가 기록되지 않은 항목).",
    "gx.code.approx": "정확한 위치가 기록되지 않아 항목 이름이 처음 나오는 줄로 이동했습니다 (근사치).",
    "gx.code.unavailable": "이 서버에는 소스 스냅샷이 없습니다 (빌드가 끝나면 생기고, 문서/URL 소스는 변환된 마크다운을 읽습니다).",
    "gx.ins.pathtitle": "가장 짧은 연결 경로", "gx.ins.pathnone": "두 항목을 잇는 경로가 없습니다 (8단계 이내).",
    "gx.ins.pathpick": "다른 항목을 선택하고 '여기까지 경로'를 누르면 경로가 표시됩니다.",
    "gx.ins.clearpath": "경로 지우기", "gx.ins.hops": "단계",
    "gx.rel.in": "← {r}", "gx.rel.out": "{r} →",
    "gx.ask.prompt": "이 소스에서 `{label}` ({file})의 역할과 관련 코드/문서를 설명해줘.",
    "gx.png.ok": "이미지를 저장했습니다", "gx.ctx.ok": "항목 설명을 마크다운으로 복사했습니다",
    "gx.hud.filtered": "필터로 숨김: 항목 {n}", "gx.back": "뒤로",
    "gx.view.groups": "폴더", "gx.view.groups.repo": "레포", "gx.crumb.group": "폴더", "gx.crumb.group.repo": "레포",
    "gx.simple": "간단히 보기", "gx.simple.tip": "설명(근거) 항목, '포함' 같은 구조 연결, AI가 추론한 연결을 숨기고 호출/임포트처럼 확실하고 의미 있는 연결만 남깁니다.", "gx.ins.groups.linked": "연결된 폴더", "gx.ins.communities": "연관 그룹",
    "gx.summary": "이 그래프는 항목 {n}개와 연결 {e}개로 이루어져 있고, 서로 밀접한 항목끼리 {c}개 연관 그룹으로 묶여 있습니다. 가장 큰 {gk}는 {top}이고, 연결이 가장 많은 항목은 {hub}({d}개)입니다.",
    "gx.summary.hint": "폴더 뷰의 원을 더블클릭하면 그 안의 연관 그룹이, 다시 더블클릭하면 실제 항목(함수, 클래스, 문서 섹션)이 보입니다. 화살표는 호출/참조 방향입니다.",
    "gx.gk.dir": "폴더", "gx.gk.repo": "레포",
    "gx.hud.scope.dir": "폴더", "gx.hud.scope.community": "연관 그룹",
    "gx.filtered.node": "이 항목은 현재 필터(또는 간단히 보기)로 숨겨져 있습니다. 필터를 해제하면 표시됩니다.",
    "gx.toobig.hub": "허브 병합 그래프({size})에는 아직 시각화 데이터가 없습니다. 어느 소스든 다음 빌드가 끝나면 함께 만들어집니다.",
    "gx.emptygraph.title": "그래프가 비어 있습니다", "gx.emptygraph.body": "이 빌드에는 항목이 없습니다 (public 소스가 없는 허브이거나, 추출된 심볼이 없는 소스).",
    "gx.inferred.short": "추론됨",
  },
  en: {
    "gx.load": "Load", "gx.search.ph": "Search nodes & files ( / )",
    "gx.view.communities": "Communities", "gx.view.full": "All nodes",
    "gx.fit": "Fit to view (F)", "gx.labels": "Toggle labels (L)", "gx.png": "PNG", "gx.fullscreen": "Fullscreen (Esc to exit)",
    "gx.filters": "Filters", "gx.color": "Color by", "gx.color.community": "Community", "gx.color.type": "Node type",
    "gx.color.dir": "Top-level folder", "gx.color.repo": "Repo",
    "gx.types": "Node types", "gx.relations": "Edge relations", "gx.repos": "Repos", "gx.mindeg": "Min degree",
    "gx.inferred": "Show INFERRED edges", "gx.legend": "Legend", "gx.legend.filter": "Search…",
    "gx.legend.more": "Show {n} more", "gx.legend.other": "Other ({n}, grey)",
    "gx.src.hub": "hub (every public source merged)", "gx.src.mine": "My sources", "gx.src.catalog": "Catalog (public, not subscribed)",
    "gx.src.none": "No viewable sources. Register one in the Sources tab or subscribe from the catalog.",
    "gx.crumb.root": "Source", "gx.crumb.community": "Community", "gx.crumb.ego": "Neighborhood", "gx.crumb.path": "Path", "gx.crumb.all": "All nodes",
    "gx.hop": "{n}-hop",
    "gx.st.loading": "Loading", "gx.st.parsing": "Building graph…", "gx.st.nodes": "nodes", "gx.st.edges": "edges",
    "gx.st.communities": "communities", "gx.st.shown": "shown", "gx.st.built": "built", "gx.st.layout": "layout: precomputed at build",
    "gx.st.layout.client": "layout: in-browser (raw graph.json)",
    "gx.empty.title": "Pick a source and press Load",
    "gx.empty.body": "Browse a built source's knowledge graph community by community, then click nodes to see files and relations.",
    "gx.pending.title": "No graph yet", "gx.pending.body": "Available once the first build finishes. Build status: {s}",
    "gx.failed.title": "The last build failed", "gx.failed.body": "No build has succeeded yet, so there is no graph.",
    "gx.toobig.title": "Large graph without a visualization bundle",
    "gx.toobig.body": "This build ({size}) has no visualization bundle and the raw graph.json is too large for the browser. Rebuild to generate the bundle.",
    "gx.rawnotice": "This build has no visualization bundle: rendering the raw graph.json ({size}). Rebuild for a faster, tidier precomputed layout.",
    "gx.rebuild": "Rebuild", "gx.retry": "Retry",
    "gx.err.libs": "The graph libraries failed to load (CDN blocked or integrity check failed). Check your network and reload.",
    "gx.err.webgl": "WebGL is unavailable in this browser, so the graph cannot be drawn. Enable hardware acceleration or use another browser.",
    "gx.err.fetch": "Could not download the graph data", "gx.err.expired": "The download link expired. Load again.",
    "gx.err.parse": "Could not parse the graph data",
    "gx.big.warn": "Drawing {n} nodes at once. The community view is easier to read.",
    "gx.ins.empty": "Click a node to see its file, relations and neighbors here. Double-click a community to open it.",
    "gx.ins.shortcuts": "Shortcuts: / search, F fit, L labels, +/- zoom, Esc deselect/back, Alt+← parent view",
    "gx.ins.stats": "Graph summary", "gx.ins.types": "Node types", "gx.ins.relations": "Edge relations", "gx.ins.top": "Most connected",
    "gx.ins.file": "File", "gx.ins.id": "Node id", "gx.ins.degree": "Degree", "gx.ins.in": "in", "gx.ins.out": "out",
    "gx.ins.community": "Community", "gx.ins.repo": "Repo", "gx.ins.neighbors": "Neighbors",
    "gx.ins.copy": "Copy", "gx.ins.copied": "Copied", "gx.ins.more": "Show {n} more",
    "gx.ins.ego1": "Neighbors only", "gx.ins.ego2": "2-hop", "gx.ins.pathA": "Path from", "gx.ins.pathB": "Path to",
    "gx.ins.ctx": "Copy as context", "gx.ins.ask": "Ask in Playground", "gx.ins.drill": "Open",
    "gx.ins.auto": "Auto-named (the build did not label communities. Enable AI extraction for names)",
    "gx.ins.size": "Nodes", "gx.ins.dir": "Main folder", "gx.ins.hubs": "Hub nodes", "gx.ins.linked": "Linked communities",
    "gx.community.n": "Community #{n}", "gx.src.pick": "Source to explore",
    "gx.code.title": "Source code", "gx.code.view": "View source", "gx.code.auto": "Load source automatically on select", "gx.code.up": "▲ 40 lines up", "gx.code.down": "▼ 40 lines down", "gx.code.loading": "Loading source…",
    "gx.code.lines": "lines {a}–{b} of {n}", "gx.code.copy": "Copy code", "gx.code.copied": "Code copied",
    "gx.code.nofile": "This node has no file information.", "gx.code.err": "Could not read the source",
    "gx.code.hubnote": "Hub nodes are read from their original repo server ({repo}).",
    "gx.code.pageonly": "This node only records a page reference ({page}), so the file is shown from the top. Rebuild the source to jump to that page.",
    "gx.code.noloc": "This node has no line information (the extractor recorded none), so the file is shown from the top.",
    "gx.code.approx": "No exact location was recorded; jumped to the first line that mentions the node's label (approximate).",
    "gx.code.unavailable": "This server has no source snapshot yet (it appears after a build; docs/URL sources serve the converted markdown).",
    "gx.ins.pathtitle": "Shortest path", "gx.ins.pathnone": "No path between the two nodes (within 8 hops).",
    "gx.ins.pathpick": "Select another node and press 'Path to' to finish the path.",
    "gx.ins.clearpath": "Clear path", "gx.ins.hops": "hops",
    "gx.rel.in": "← {r}", "gx.rel.out": "{r} →",
    "gx.ask.prompt": "Explain the role of `{label}` ({file}) in this source and the code/docs related to it.",
    "gx.png.ok": "PNG saved", "gx.ctx.ok": "Node context copied as markdown",
    "gx.hud.filtered": "hidden by filters: {n} nodes", "gx.back": "Back",
    "gx.view.groups": "Folders", "gx.view.groups.repo": "Repos", "gx.crumb.group": "Folder", "gx.crumb.group.repo": "Repo",
    "gx.simple": "Simplified", "gx.simple.tip": "Hides rationale nodes, structural (contains) edges and inferred edges, leaving confident semantic relations such as calls and imports.", "gx.ins.groups.linked": "Linked folders", "gx.ins.communities": "Communities",
    "gx.summary": "This graph has {n} nodes and {e} relations, grouped into {c} communities. The largest {gk} is {top}; the most connected node is {hub} ({d} links).",
    "gx.summary.hint": "In the folder view, double-click a bubble to see its communities, then double-click again for the actual nodes. Arrows show call/reference direction.",
    "gx.gk.dir": "folder", "gx.gk.repo": "repo",
    "gx.hud.scope.dir": "Folder", "gx.hud.scope.community": "Community",
    "gx.filtered.node": "This node is hidden by the current filters (or Simplified view). Clear them to show it.",
    "gx.toobig.hub": "The hub's merged graph ({size}) has no visualization bundle yet. It is generated together with the next build of any source.",
    "gx.inferred.short": "inferred",
    "gx.emptygraph.title": "The graph is empty", "gx.emptygraph.body": "This build has no nodes (a hub with no public sources, or a source with no extracted symbols).",
  },
};
// index.html declares `const I18N` at script top level: a global lexical
// binding, reachable by name but NOT as window.I18N.
if (typeof I18N !== "undefined" && I18N) {
  Object.assign(I18N.ko, GX_I18N.ko); Object.assign(I18N.en, GX_I18N.en);
  // boot()'s applyI18n() may already have run (this script is deferred behind
  // the CDN libs); re-apply so the tab's markup picks up the merged keys.
  if (typeof applyI18n === "function") { try { applyI18n(); } catch (e) { /* DOM not ready yet: boot() applies later */ } }
}
const tt = (k, vars) => {
  let s = t(k);
  for (const [a, b] of Object.entries(vars || {})) s = s.split(`{${a}}`).join(String(b));
  return s;
};
// Like tt(), but returns DOM with each substituted value wrapped in <b> — for
// the summary paragraph, where the numbers and names are what the eye needs.
const richText = (k, vars) => {
  const frag = document.createDocumentFragment();
  const parts = t(k).split(/(\{[a-z]+\})/g);
  for (const part of parts) {
    const m = /^\{([a-z]+)\}$/.exec(part);
    if (m && vars && m[1] in vars) frag.append(el("b", null, String(vars[m[1]])));
    else if (part) frag.append(part);
  }
  return frag;
};

/* ---------------- glossary (data values → plain Korean) ----------------
 * file_type / relation names come from graphify as English identifiers. The
 * Korean UI shows a plain-language gloss and keeps the raw term in tooltips;
 * unknown values fall through unchanged. English UI shows raw terms. */
const TYPE_KO = { code: "코드", document: "문서", rationale: "설명(근거)", concept: "개념", paper: "논문", "": "(없음)" };
const REL_KO = {
  calls: "호출", indirect_call: "간접 호출", contains: "포함", imports: "임포트", imports_from: "임포트(from)",
  references: "참조", inherits: "상속", implements: "구현", extends: "확장", method: "메서드", defines: "정의",
  uses: "사용", re_exports: "재내보내기", rationale_for: "근거 설명", instantiates: "인스턴스 생성",
  conceptually_related_to: "개념적 연관", shares_data_with: "데이터 공유", embeds: "임베드", listened_by: "리스너",
  case_of: "케이스", bound_to: "바인딩", mixes_in: "믹스인", uses_config: "설정 사용", references_constant: "상수 참조",
  uses_static_prop: "정적 속성 사용", dispatches_to: "디스패치", participate_in: "참여", form: "구성",
  semantically_similar_to: "의미 유사", related_to: "관련", depends_on: "의존", "": "(없음)",
};
const KIND_KO = { heading: "제목", page: "페이지", section: "절", "": "" };
const isKo = () => (typeof LANG === "undefined" ? "ko" : LANG) === "ko";
const glossType = (raw) => (isKo() && Object.prototype.hasOwnProperty.call(TYPE_KO, raw) ? TYPE_KO[raw] : (raw || "(none)"));
const glossRel = (raw) => (isKo() && Object.prototype.hasOwnProperty.call(REL_KO, raw) ? REL_KO[raw] : (raw || "related"));
const glossKind = (raw) => (isKo() && Object.prototype.hasOwnProperty.call(KIND_KO, raw) ? KIND_KO[raw] : raw);
const dirLabel = (name) => (name === "(root)" ? (isKo() ? "(최상위)" : "(root)") : name);

/* ---------------- palettes ---------------- */
// Qualitative ramp (readable on the console's near-white stage); rank-assigned
// within the current view, never `id % n` — 652 communities would alias.
const PALETTE = ["#4F46E5", "#0D9488", "#D97706", "#DB2777", "#2563EB", "#65A30D", "#7C3AED", "#EA580C",
  "#0891B2", "#C026D3", "#16A34A", "#B45309", "#4338CA", "#E11D48", "#0EA5E9", "#84CC16"];
const GREY = "#B6C0CE";
// Own-property lookup: file_type is untrusted text ("constructor" must not resolve).
const typeColor = (name) => (Object.prototype.hasOwnProperty.call(TYPE_COLOR, name) ? TYPE_COLOR[name] : undefined);
const TYPE_COLOR = { code: "#4F46E5", document: "#0D9488", rationale: "#D97706", concept: "#7C3AED", paper: "#DB2777" };
const REL_PRIORITY = ["calls", "indirect_call", "imports", "imports_from", "references", "inherits", "implements",
  "method", "defines", "uses", "re_exports", "rationale_for", "contains"];
const EDGE_COLOR = "rgba(15, 23, 42, 0.14)";
const EDGE_COLOR_INFERRED = "rgba(15, 23, 42, 0.07)";
const DIM_NODE = "rgba(148, 163, 184, 0.45)";
const HILITE = "#4F46E5";
const PATH_COLOR = "#DB2777";
const COMMUNITY_VIEW_THRESHOLD = 3000;   // node count above which the meta-graph is the entry view
const RAW_MAX_NODES_FOR_LAYOUT = 60000;
const MAX_BUNDLE_RAW_BYTES = 160 * 1024 * 1024;   // decoded JSON the tab is willing to parse

/* ---------------- small helpers ---------------- */
const $ = (id) => document.getElementById(id);
const reduceMotion = () => window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
const CTRL_RE = /[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\u200b-\u200f\u2028\u2029\u202a-\u202e\u2066-\u2069\ufeff]/g;
function clean(v, max = 200) {
  const s = String(v ?? "").replace(CTRL_RE, "");
  return s.length > max ? s.slice(0, max - 1) + "…" : s;
}
function midTrunc(s, max = 48) {
  s = String(s || "");
  if (s.length <= max) return s;
  const head = Math.ceil((max - 1) * 0.6), tail = max - 1 - head;
  return s.slice(0, head) + "…" + s.slice(s.length - tail);
}
function fmtBytes(n) {
  n = Number(n) || 0;
  return n >= 1048576 ? (n / 1048576).toFixed(1) + " MB" : n >= 1024 ? (n / 1024).toFixed(0) + " KB" : n + " B";
}
function fmtN(n) { return Number(n || 0).toLocaleString(); }
function topDir(path) {
  const p = String(path || "");
  const i = p.indexOf("/");
  return i < 0 ? "(root)" : p.slice(0, i);
}
function safeFilename(s) { return String(s || "graph").replace(/[^A-Za-z0-9._-]/g, "_").slice(0, 80); }

/* ---------------- model ----------------
 * M: columnar arrays indexed by node index; adjacency as CSR; communities.
 * Built from the viz bundle (fromBundle) or the raw graph.json (fromRaw). */
function buildAdjacency(n, es, et) {
  const off = new Int32Array(n + 1);
  for (let i = 0; i < es.length; i++) { off[es[i] + 1]++; off[et[i] + 1]++; }
  for (let i = 0; i < n; i++) off[i + 1] += off[i];
  const nb = new Int32Array(off[n]), eid = new Int32Array(off[n]), dir = new Int8Array(off[n]);
  const fill = off.slice(0, n);
  for (let i = 0; i < es.length; i++) {
    const s = es[i], tg = et[i];
    let p = fill[s]++; nb[p] = tg; eid[p] = i; dir[p] = 1;    // out
    p = fill[tg]++; nb[p] = s; eid[p] = i; dir[p] = 0;        // in
  }
  return { off, nb, eid, dir };
}
function intern(values) {
  const table = new Map(); const out = new Int32Array(values.length); const list = [];
  for (let i = 0; i < values.length; i++) {
    const v = values[i];
    let k = table.get(v);
    if (k === undefined) { k = list.length; table.set(v, k); list.push(v); }
    out[i] = k;
  }
  return { list, idx: out };
}
function finishModel(M) {
  const n = M.n;
  M.adj = buildAdjacency(n, M.es, M.et);
  M.maxDeg = 1;
  for (let i = 0; i < n; i++) if (M.deg[i] > M.maxDeg) M.maxDeg = M.deg[i];
  // top-level directory per node (color mode "dir")
  const dirOfFile = M.files.map((f) => topDir(f));
  const d = intern(Array.from({ length: n }, (_, i) => dirOfFile[M.f[i]] || "(root)"));
  M.dirs = d.list; M.d = d.idx;
  // community index + dominant repo (hub) + members
  M.commByCid = new Map();
  for (const c of M.communities) { c.members = []; M.commByCid.set(c.id, c); }
  for (let i = 0; i < n; i++) {
    const c = M.commByCid.get(M.c[i]);
    if (c) c.members.push(i);
  }
  for (const c of M.communities) {
    if (!c.members.length) continue;
    const cnt = new Map();
    for (const i of c.members) { const k = M.r ? M.r[i] : 0; cnt.set(k, (cnt.get(k) || 0) + 1); }
    c.repo = [...cnt.entries()].sort((a, b) => b[1] - a[1])[0][0];
    const dc = new Map();
    for (const i of c.members) dc.set(M.d[i], (dc.get(M.d[i]) || 0) + 1);
    c.dirIdx = [...dc.entries()].sort((a, b) => b[1] - a[1])[0][0];
    c.hubs = (c.hubs || []).filter((h) => Number.isInteger(h) && h >= 0 && h < n);
    if (!c.hubs.length) c.hubs = c.members.slice().sort((a, b) => M.deg[b] - M.deg[a]).slice(0, 3);
  }
  computeFootprints(M);
  // Top-level groups (repo on the hub, else top-level folder): the most
  // human-readable first level — stable across rebuilds, few, nameable.
  M.groupKind = M.r && M.repos.length > 1 ? "repo" : "dir";
  const gk = M.groupKind === "repo" ? M.r : M.d;
  const gnames = M.groupKind === "repo" ? M.repos : M.dirs;
  const gm = new Map();
  for (let i = 0; i < n; i++) { let a = gm.get(gk[i]); if (!a) { a = []; gm.set(gk[i], a); } a.push(i); }
  M.groups = [...gm.entries()].sort((a, b) => b[1].length - a[1].length).map(([k, members]) => ({
    k, members,
    // Hub repos read as their MCP server names (what users see everywhere else).
    label: M.groupKind === "repo" ? (typeof serverName === "function" ? serverName(gnames[k]) : gnames[k]) : (gnames[k] || "(root)"),
    sub: M.groupKind === "repo" ? gnames[k] : "",
  }));
  M.groupByKey = new Map(M.groups.map((g) => [g.k, g]));
  const gw = new Map();
  for (let e = 0; e < M.e; e++) {
    const a = gk[M.es[e]], b = gk[M.et[e]];
    if (a === b) continue;
    const key = a < b ? a * 1e6 + b : b * 1e6 + a;
    gw.set(key, (gw.get(key) || 0) + 1);
  }
  M.gedges = [...gw.entries()].map(([key, w]) => [Math.floor(key / 1e6), key % 1e6, w]).sort((a, b) => b[2] - a[2]);
  M.gk = gk;
  // search index (lower-cased label / file), built lazily on first search
  M.search = null;
  return M;
}
// Community footprint = centroid + radius covering its members in graph units,
// so the aggregate view's disc IS the area the members occupy after drill-down.
function computeFootprints(M) {
  if (!M.x) return;
  let minR = Infinity;
  for (const c of M.communities) {
    if (!c.members.length) continue;
    let sx = 0, sy = 0;
    for (const i of c.members) { sx += M.x[i]; sy += M.y[i]; }
    c.x = sx / c.members.length; c.y = sy / c.members.length;
    let r = 0;
    for (const i of c.members) { const d = Math.hypot(M.x[i] - c.x, M.y[i] - c.y); if (d > r) r = d; }
    c.radius = r;
    if (c.members.length > 1 && r > 0 && r < minR) minR = r;
  }
  const floor = Math.max(5, Number.isFinite(minR) ? minR * 0.6 : 8);
  for (const c of M.communities) if (!c.radius || c.radius < floor) c.radius = floor;
}
function fromBundle(b, srcId) {
  if (!b || typeof b !== "object" || !b.nodes || !b.edges) throw new Error("bad bundle");
  const N = b.nodes, E = b.edges;
  const n = Array.isArray(N.id) ? N.id.length : 0;
  const col = (arr, len, fallback) => Array.isArray(arr) && arr.length === len ? arr : Array.from({ length: len }, () => fallback);
  const asInt = (arr, len) => { const o = new Int32Array(len); const a = col(arr, len, 0); for (let i = 0; i < len; i++) o[i] = (a[i] | 0); return o; };
  const asF = (arr, len) => { const o = new Float64Array(len); const a = col(arr, len, 0); for (let i = 0; i < len; i++) o[i] = +a[i] || 0; return o; };
  const m = Number(Array.isArray(E.s) ? E.s.length : 0);
  const types = col(b.types, (b.types || []).length, "").map((s) => clean(s, 40));
  const kinds = col(b.kinds, (b.kinds || []).length, "").map((s) => clean(s, 40));
  const relations = col(b.relations, (b.relations || []).length, "").map((s) => clean(s, 40));
  const files = col(b.files, (b.files || []).length, "").map((s) => clean(s, 512));
  const repos = col(b.repos, (b.repos || []).length, "").map((s) => clean(s, 200));
  const M = {
    srcId, header: { generated_at: clean(b.generated_at, 40), built_at_commit: clean(b.built_at_commit, 40), layout: b.layout ? clean(b.layout, 30) : null },
    n, e: m,
    id: col(N.id, n, "").map((s) => clean(s, 512)),
    label: col(N.label, n, "").map((s) => clean(s, 200)),
    c: asInt(N.c, n), t: asInt(N.t, n), k: asInt(N.k, n), f: asInt(N.f, n),
    loc: col(N.loc, n, "").map((s) => clean(s, 40)),
    deg: asInt(N.deg, n),
    x: N.x ? asF(N.x, n) : null, y: N.y ? asF(N.y, n) : null,
    r: repos.length && N.r ? asInt(N.r, n) : null,
    es: asInt(E.s, m), et: asInt(E.t, m), er: asInt(E.r, m), einf: asInt(E.inf, m),
    types, kinds, relations, files, repos,
    communities: [], cedges: [],
    hyperedges: [],
    layoutSource: N.x ? "build" : null,
  };
  // bounds-check edges (a corrupt bundle must not crash the reducers)
  for (let i = 0; i < m; i++) {
    if (M.es[i] < 0 || M.es[i] >= n) M.es[i] = 0;
    if (M.et[i] < 0 || M.et[i] >= n) M.et[i] = 0;
    if (M.er[i] < 0 || M.er[i] >= relations.length) M.er[i] = 0;
  }
  for (let i = 0; i < n; i++) {
    if (M.t[i] < 0 || M.t[i] >= types.length) M.t[i] = 0;
    if (M.k[i] < 0 || M.k[i] >= kinds.length) M.k[i] = 0;
    if (M.f[i] < 0 || M.f[i] >= files.length) M.f[i] = 0;
    if (M.r && (M.r[i] < 0 || M.r[i] >= repos.length)) M.r[i] = 0;
  }
  for (const c of Array.isArray(b.communities) ? b.communities : []) {
    if (!c || typeof c !== "object") continue;
    M.communities.push({
      id: c.id | 0, label: clean(c.label, 120) || `Community ${c.id | 0}`, auto: !!c.auto,
      size: c.size | 0, dir: clean(c.dir, 160), dirShare: +c.dir_share || 0,
      hubs: Array.isArray(c.hubs) ? c.hubs.map((h) => h | 0) : [],
      type: c.type | 0, x: Number.isFinite(+c.x) ? +c.x : null, y: Number.isFinite(+c.y) ? +c.y : null,
    });
  }
  for (const ce of Array.isArray(b.cedges) ? b.cedges : []) {
    if (Array.isArray(ce) && ce.length >= 3) M.cedges.push([ce[0] | 0, ce[1] | 0, ce[2] | 0]);
  }
  for (const h of Array.isArray(b.hyperedges) ? b.hyperedges : []) {
    if (h && Array.isArray(h.nodes)) M.hyperedges.push({ label: clean(h.label, 120), nodes: h.nodes.map((x) => x | 0).filter((x) => x >= 0 && x < n) });
  }
  finishModel(M);
  // make_viz.py skips x/y above its node cap: lay out here rather than crash.
  if (!M.x) { M.layoutSource = "client"; layoutClient(M); computeFootprints(M); }
  return M;
}
// Raw graph.json (networkx node_link_data) → the same model. No positions in
// the file: circlepack by community (graphology-library) or a circular
// fallback. Communities are derived here the way make_viz.py derives them.
function fromRaw(g, srcId) {
  if (!g || !Array.isArray(g.nodes)) throw new Error("bad graph.json");
  const links = Array.isArray(g.links) ? g.links : Array.isArray(g.edges) ? g.edges : [];
  const rawNodes = g.nodes.filter((x) => x && typeof x === "object" && typeof x.id === "string");
  const n = rawNodes.length;
  const idIndex = new Map();
  rawNodes.forEach((x, i) => idIndex.set(x.id, i));
  const tI = intern(rawNodes.map((x) => clean(x.file_type, 40)));
  const kI = intern(rawNodes.map((x) => clean(x.node_kind, 40)));
  const fI = intern(rawNodes.map((x) => clean(x.source_file, 512)));
  const hasRepo = rawNodes.some((x) => typeof x.repo === "string");
  const rI = hasRepo ? intern(rawNodes.map((x) => clean(x.repo, 200))) : null;
  const es = [], et = [], erv = [], einf = [];
  for (const l of links) {
    if (!l || typeof l !== "object") continue;
    const s = idIndex.get(l.source), tg = idIndex.get(l.target);
    if (s === undefined || tg === undefined) continue;
    es.push(s); et.push(tg); erv.push(clean(l.relation, 40)); einf.push(l.confidence === "INFERRED" ? 1 : 0);
  }
  const rIn = intern(erv);
  const deg = new Int32Array(n);
  const seen = new Set();
  for (let i = 0; i < es.length; i++) {
    const a = Math.min(es[i], et[i]), b = Math.max(es[i], et[i]);
    const key = a * n + b;
    if (a === b || seen.has(key)) continue;
    seen.add(key); deg[a]++; deg[b]++;
  }
  const c = new Int32Array(n);
  const commName = new Map();
  rawNodes.forEach((x, i) => {
    const cid = Number.isInteger(x.community) ? x.community : -1;
    c[i] = cid;
    if (!commName.has(cid)) commName.set(cid, clean(x.community_name, 120));
  });
  const M = {
    srcId, header: { generated_at: "", built_at_commit: clean(g.built_at_commit, 40), layout: null },
    n, e: es.length,
    id: rawNodes.map((x) => clean(x.id, 512)),
    label: rawNodes.map((x) => clean(x.label || x.id, 200)),
    c, t: tI.idx, k: kI.idx, f: fI.idx,
    loc: rawNodes.map((x) => clean(x.source_location, 40)),
    deg, x: null, y: null, r: rI ? rI.idx : null,
    es: Int32Array.from(es), et: Int32Array.from(et), er: rIn.idx, einf: Int8Array.from(einf),
    types: tI.list, kinds: kI.list, relations: rIn.list, files: fI.list, repos: rI ? rI.list : [],
    communities: [], cedges: [], hyperedges: [],
    layoutSource: "client",
  };
  // communities: size, auto label, dominant dir, hubs
  const members = new Map();
  for (let i = 0; i < n; i++) { if (!members.has(c[i])) members.set(c[i], []); members.get(c[i]).push(i); }
  for (const [cid, mem] of [...members.entries()].sort((a, b) => b[1].length - a[1].length)) {
    const name = commName.get(cid) || "";
    const dirs = new Map();
    for (const i of mem) {
      const parts = M.files[M.f[i]].split("/");
      const key = parts.length > 1 ? parts.slice(0, -1).slice(0, 2).join("/") : parts[0];
      dirs.set(key, (dirs.get(key) || 0) + 1);
    }
    const [dir, dn] = [...dirs.entries()].sort((a, b) => b[1] - a[1])[0] || ["", 0];
    const tc = new Map();
    for (const i of mem) tc.set(M.t[i], (tc.get(M.t[i]) || 0) + 1);
    M.communities.push({
      id: cid, label: name || `Community ${cid}`, auto: !name || /^Community \d+$/.test(name), size: mem.length,
      dir, dirShare: dn / mem.length, hubs: mem.slice().sort((a, b) => deg[b] - deg[a]).slice(0, 3),
      type: [...tc.entries()].sort((a, b) => b[1] - a[1])[0][0], x: null, y: null,
    });
  }
  const cw = new Map();
  for (let i = 0; i < es.length; i++) {
    const a = c[es[i]], b = c[et[i]];
    if (a === b) continue;
    const key = a < b ? `${a}|${b}` : `${b}|${a}`;
    cw.set(key, (cw.get(key) || 0) + 1);
  }
  for (const [key, w] of cw) { const [a, b] = key.split("|").map(Number); M.cedges.push([a, b, w]); }
  for (const h of Array.isArray(g.hyperedges) ? g.hyperedges : []) {
    if (h && Array.isArray(h.nodes)) {
      const idxs = h.nodes.map((x) => idIndex.get(x)).filter((x) => x !== undefined);
      if (idxs.length >= 2) M.hyperedges.push({ label: clean(h.label || h.relation, 120), nodes: idxs });
    }
  }
  finishModel(M);
  layoutClient(M);
  return M;
}
// Client-side layout for the raw fallback: circlepack by community keeps the
// two-level look (communities as discs) and is deterministic and O(n log n).
function layoutClient(M) {
  const n = M.n;
  M.x = new Float64Array(n); M.y = new Float64Array(n);
  const GL = window.graphologyLibrary;
  if (GL && GL.layout && GL.layout.circlepack && n <= RAW_MAX_NODES_FOR_LAYOUT) {
    try {
      const g = new graphology.Graph();
      for (let i = 0; i < n; i++) g.addNode(String(i), { size: 1 + Math.sqrt(M.deg[i]), community: M.c[i] });
      GL.layout.circlepack.assign(g, { hierarchyAttributes: ["community"] });
      let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
      for (let i = 0; i < n; i++) {
        const a = g.getNodeAttributes(String(i));
        M.x[i] = a.x; M.y[i] = a.y;
        if (a.x < minX) minX = a.x; if (a.x > maxX) maxX = a.x; if (a.y < minY) minY = a.y; if (a.y > maxY) maxY = a.y;
      }
      const span = Math.max(maxX - minX, maxY - minY) || 1, cx = (minX + maxX) / 2, cy = (minY + maxY) / 2;
      for (let i = 0; i < n; i++) { M.x[i] = (M.x[i] - cx) * 2000 / span; M.y[i] = (M.y[i] - cy) * 2000 / span; }
    } catch (e) { circularFallback(M); }
  } else circularFallback(M);
  for (const c of M.communities) {
    if (!c.members.length) continue;
    let sx = 0, sy = 0;
    for (const i of c.members) { sx += M.x[i]; sy += M.y[i]; }
    c.x = sx / c.members.length; c.y = sy / c.members.length;
  }
}
function circularFallback(M) {
  // communities on a ring, members on a small ring around their community
  const C = M.communities.length || 1;
  M.communities.forEach((c, k) => {
    const ang = 2 * Math.PI * k / C, R = 900;
    c.x = R * Math.cos(ang); c.y = R * Math.sin(ang);
    const r = 8 + 6 * Math.sqrt(c.members.length);
    c.members.forEach((i, j) => {
      const a = 2 * Math.PI * j / c.members.length;
      M.x[i] = c.x + r * Math.cos(a); M.y[i] = c.y + r * Math.sin(a);
    });
  });
}

/* ---------------- state ---------------- */
const GX = {
  srcId: "", route: "", info: null, M: null,
  cache: new Map(),                       // srcId -> { etag, M } (session memory only; never the URL)
  view: { mode: "groups", scope: { kind: "all" } },   // mode: groups | communities | full; scope: all | dir{d} | community{cid} | ego{node,hops} | path{nodes}
  simple: false,
  history: [],                            // view stack for ← / breadcrumb back
  colorBy: "dir",
  filters: { types: new Set(), relations: new Set(), repos: new Set(), legend: new Set(), minDeg: 0, inferred: true },
  labelsOn: true,
  renderer: null, graph: null, viewNodes: [], viewEdges: [], isCommunityView: false,
  hovered: null, hoveredNbrs: null, selected: null, selectedNbrs: null,   // sigma node keys
  path: { a: null, b: null, nodes: null, edges: null },                 // node indices / sets of keys
  colorRank: new Map(), legendCats: [],
  hoverRaf: 0, loading: false, loadSeq: 0, shown: false, libsError: "", pollTimer: 0,
  code: { cache: new Map(), seq: 0, auto: true, range: null },
  lang: typeof LANG === "undefined" ? "ko" : LANG,
};
const K = (i) => String(i);                 // node index -> sigma key
const CK = (cid) => "c:" + cid;             // community id -> sigma key
const isCK = (key) => key.charCodeAt(0) === 99 && key.charCodeAt(1) === 58;

/* ---------------- colors ---------------- */
// Category value of a node under the current color mode.
function catOfNode(i) {
  const M = GX.M;
  switch (GX.colorBy) {
    case "type": return M.t[i];
    case "dir": return M.d[i];
    case "repo": return M.r ? M.r[i] : 0;
    default: return M.c[i];
  }
}
function catOfCommunity(c) {
  const M = GX.M;
  switch (GX.colorBy) {
    case "type": return c.type;
    case "dir": return c.dirIdx;
    case "repo": return c.repo;
    default: return c.id;
  }
}
function catLabel(cat) {
  const M = GX.M;
  switch (GX.colorBy) {
    case "type": return glossType(M.types[cat]);
    case "dir": return dirLabel(M.dirs[cat] || "(root)");
    case "repo": return M.repos[cat] || "";
    default: { const c = M.commByCid.get(cat); return c ? communityLabel(c) : tt("gx.community.n", { n: cat }); }
  }
}
// Categories ranked by node count; the top PALETTE.length get hues, the rest grey.
function computeColorRank() {
  const M = GX.M;
  const counts = new Map();
  for (let i = 0; i < M.n; i++) { const c = catOfNode(i); counts.set(c, (counts.get(c) || 0) + 1); }
  const cats = [...counts.entries()].sort((a, b) => b[1] - a[1] || a[0] - b[0]);
  GX.colorRank = new Map();
  GX.legendCats = cats.map(([cat, n], rank) => {
    let color;
    if (GX.colorBy === "type") color = typeColor(M.types[cat]) || PALETTE[rank % PALETTE.length];
    else color = rank < PALETTE.length ? PALETTE[rank] : GREY;
    GX.colorRank.set(cat, color);
    return { cat, n, color, label: catLabel(cat) };
  });
}
function colorOfCat(cat) { return GX.colorRank.get(cat) || GREY; }
function communityLabel(c) {
  if (!c.auto) return c.label;
  const M = GX.M;
  const hub = c.hubs && c.hubs.length ? M.label[c.hubs[0]] : "";
  return c.dir ? `${c.dir}${hub ? ", " + midTrunc(hub, 28) : ""}` : (hub || tt("gx.community.n", { n: c.id }));
}

/* ---------------- filters ---------------- */
function nodePasses(i) {
  const M = GX.M, F = GX.filters;
  if (F.types.has(M.t[i])) return false;
  if (GX.simple && M.types[M.t[i]] === "rationale") return false;
  if (M.r && F.repos.has(M.r[i])) return false;
  if (F.minDeg && M.deg[i] < F.minDeg) return false;
  if (F.legend.size && F.legend.has(catOfNode(i))) return false;
  return true;
}
function edgePasses(e) {
  const M = GX.M, F = GX.filters;
  if (F.relations.has(M.er[e])) return false;
  if (GX.simple) { const r = M.relations[M.er[e]]; if (r === "contains" || r === "rationale_for" || M.einf[e]) return false; }
  if (!F.inferred && M.einf[e]) return false;
  return true;
}
function filtersActive() {
  const F = GX.filters;
  return F.types.size || F.relations.size || F.repos.size || F.legend.size || F.minDeg || !F.inferred || GX.simple;
}

/* ---------------- view graphs ---------------- */
function nodeSize(i) {
  const M = GX.M;
  // Smaller graphs get bigger marks (a 360-node graph should not look like dust).
  const base = M.n < 800 ? 4 : M.n < 3000 ? 3.2 : 2.4;
  return base + (M.n < 3000 ? 9 : 7) * Math.sqrt(M.deg[i] / M.maxDeg);
}
function scopeNodeSet() {
  const M = GX.M, sc = GX.view.scope;
  if (sc.kind === "dir") { const g = M.groupByKey.get(sc.d); return new Set(g ? g.members : []); }
  if (sc.kind === "community") { const c = M.commByCid.get(sc.cid); return new Set(c ? c.members : []); }
  if (sc.kind === "ego") return egoSet(sc.node, sc.hops);
  if (sc.kind === "path") return new Set(sc.nodes);
  return null;  // all
}
function egoSet(node, hops) {
  const M = GX.M, out = new Set([node]);
  let frontier = [node];
  for (let h = 0; h < hops && frontier.length; h++) {
    const next = [];
    for (const u of frontier) {
      for (let p = M.adj.off[u]; p < M.adj.off[u + 1]; p++) {
        const v = M.adj.nb[p];
        if (!out.has(v)) { out.add(v); next.push(v); }
      }
      if (out.size > 6000) return out;   // starburst guard (a utils module can have thousands of edges)
    }
    frontier = next;
  }
  return out;
}
function buildNodeGraph() {
  const M = GX.M;
  const g = new graphology.MultiDirectedGraph({ allowSelfLoops: true });
  const set = scopeNodeSet();
  const nodes = [], edges = [];
  const inView = new Uint8Array(M.n);
  const iter = set ? [...set] : Array.from({ length: M.n }, (_, i) => i);
  for (const i of iter) {
    if (!nodePasses(i)) continue;
    inView[i] = 1;
    nodes.push({ key: K(i), attributes: {
      x: M.x[i], y: M.y[i], size: nodeSize(i), color: colorOfCat(catOfNode(i)),
      label: midTrunc(M.label[i], 48), type: "circle", idx: i,
    } });
  }
  const m = M.e;
  for (let e = 0; e < m; e++) {
    const s = M.es[e], tg = M.et[e];
    if (!inView[s] || !inView[tg] || !edgePasses(e)) continue;
    edges.push({ key: K(e), source: K(s), target: K(tg), attributes: {
      size: M.einf[e] ? 0.6 : 1.1, color: M.einf[e] ? EDGE_COLOR_INFERRED : EDGE_COLOR, type: "arrow", eidx: e,
    } });
  }
  g.import({ nodes, edges });
  GX.isCommunityView = false;
  return g;
}
function buildCommunityGraph() {
  const M = GX.M;
  const g = new graphology.MultiDirectedGraph({ allowSelfLoops: false });
  let maxSize = 1;
  for (const c of M.communities) if (c.members.length > maxSize) maxSize = c.members.length;
  const F = GX.filters;
  const scope = scopeNodeSet();
  const nodes = [], present = new Set();
  for (const c of M.communities) {
    if (!c.members.length) continue;
    // A community whose every member is filtered out (or outside the folder
    // scope) disappears; otherwise the disc shrinks to its visible share.
    const visible = c.members.reduce((acc, i) => acc + ((!scope || scope.has(i)) && nodePasses(i) ? 1 : 0), 0);
    if (!visible) continue;
    if (F.legend.size && GX.colorBy === "community" && F.legend.has(c.id)) continue;
    present.add(c.id);
    // Sizes are in GRAPH units here (itemSizesReference: "positions"): the disc
    // is the members' footprint, shrunk by the filtered-out share.
    const r = (c.radius || 8) * Math.sqrt(visible / c.members.length) * 0.92;
    nodes.push({ key: CK(c.id), attributes: {
      x: c.x ?? 0, y: c.y ?? 0, size: r,
      color: colorOfCat(catOfCommunity(c)), label: midTrunc(communityLabel(c), 40), type: "circle", cid: c.id, count: visible,
    } });
  }
  // Inter-community weights are tallied from the edges that actually pass the
  // current filters (the bundle's precomputed cedges describe the whole graph).
  const cw = metaEdgeWeights((i) => M.c[i], (i) => present.has(M.c[i]));
  let maxW = 1;
  for (const w of cw.values()) if (w > maxW) maxW = w;
  const edges = [];
  for (const [key, w] of cw) {
    const [a, b] = key.split("|").map(Number);
    edges.push({ key: "ce:" + key, source: CK(a), target: CK(b), attributes: {
      size: 1.5 + 9 * Math.sqrt(w / maxW), color: "rgba(15, 23, 42, 0.16)", type: "line", weight: w,
    } });
  }
  g.import({ nodes, edges });
  GX.isCommunityView = true;
  return g;
}

// Weighted meta-edges between categories of visible nodes over the edges that
// pass the filters. keyOf(i) -> category; inScope(i) -> node is drawn.
function metaEdgeWeights(keyOf, inScope) {
  const M = GX.M, out = new Map();
  for (let e = 0; e < M.e; e++) {
    const s = M.es[e], t2 = M.et[e];
    const a = keyOf(s), b = keyOf(t2);
    if (a === b || !inScope(s) || !inScope(t2) || !nodePasses(s) || !nodePasses(t2) || !edgePasses(e)) continue;
    const key = a < b ? `${a}|${b}` : `${b}|${a}`;
    out.set(key, (out.get(key) || 0) + 1);
  }
  return out;
}
// Top-level groups (folders / repos) as a small weighted meta-graph laid out
// client-side (a few dozen nodes): the "map of the project" entry view.
function buildGroupGraph() {
  const M = GX.M;
  const g = new graphology.MultiDirectedGraph({ allowSelfLoops: false });
  const rows = [];
  for (const grp of M.groups) {
    const visible = grp.members.reduce((acc, i) => acc + (nodePasses(i) ? 1 : 0), 0);
    if (visible) rows.push({ grp, visible });
  }
  const maxN = Math.max(1, ...rows.map((r) => r.visible));
  const n = rows.length;
  rows.forEach((r, k) => {
    const ang = 2 * Math.PI * k / Math.max(n, 1);
    g.addNode("g:" + r.grp.k, {
      x: Math.cos(ang) * 100, y: Math.sin(ang) * 100, size: 12 + 36 * Math.sqrt(r.visible / maxN),
      color: GX.colorBy === "community" ? PALETTE[k % PALETTE.length] : colorOfCat(dominantCat(r.grp.members)),
      label: midTrunc(dirLabel(r.grp.label), 36), type: "circle", gkey: r.grp.k, count: r.visible,
    });
  });
  const gw = metaEdgeWeights((i) => M.gk[i], () => true);
  let maxW = 1;
  for (const w of gw.values()) if (w > maxW) maxW = w;
  for (const [key, w] of gw) {
    const [ka, kb] = key.split("|").map(Number);
    const a = "g:" + ka, b = "g:" + kb;
    if (!g.hasNode(a) || !g.hasNode(b)) continue;
    g.addEdgeWithKey("ge:" + key, a, b, { size: 1.2 + 8 * Math.sqrt(w / maxW), color: `rgba(79, 70, 229, ${0.22 + 0.4 * Math.sqrt(w / maxW)})`, type: "line", weight: w, label: fmtN(w) });
  }
  // Deterministic force layout from the circular seed (tiny graph, sync).
  const GL = window.graphologyLibrary;
  if (n > 2 && g.size > 0 && GL && GL.layoutForceAtlas2) {
    try {
      GL.layoutForceAtlas2.assign(g, { iterations: 400, settings: { ...GL.layoutForceAtlas2.inferSettings(g), adjustSizes: true, gravity: 1, scalingRatio: 30, strongGravityMode: true } });
      if (GL.layoutNoverlap) GL.layoutNoverlap.assign(g, { maxIterations: 200, settings: { margin: 12, ratio: 1.2, speed: 3 } });
    } catch (e) { /* keep the circle */ }
  }
  GX.isCommunityView = false; GX.isGroupView = true;
  return g;
}
// Most common category (under the active colour mode) among a node set.
function dominantCat(members) {
  const c = new Map();
  for (const i of members) { const k = catOfNode(i); c.set(k, (c.get(k) || 0) + 1); }
  return [...c.entries()].sort((a, b) => b[1] - a[1])[0][0];
}
const isGK = (key) => key.charCodeAt(0) === 103 && key.charCodeAt(1) === 58;

/* ---------------- renderer ---------------- */
function libsReady() {
  if (!window.graphology || !window.Sigma) {
    const failed = (window.__gxScriptErrors || []).some((u) => /graphology|sigma/.test(u));
    GX.libsError = failed || !window.graphology ? "libs" : "webgl";
    return false;
  }
  return true;
}
function sigmaSettings(n, e) {
  return {
    allowInvalidContainer: true,
    // Community discs are footprints in graph units; node marks are screen px.
    itemSizesReference: GX.isCommunityView ? "positions" : "screen",
    zoomToSizeRatioFunction: GX.isCommunityView ? ((x) => x) : Math.sqrt,
    defaultEdgeType: "arrow",
    renderLabels: GX.labelsOn,
    // Edge labels only in the tiny folder meta-graph (relation counts between
    // folders); at any real edge count they are pure noise.
    renderEdgeLabels: GX.isGroupView && GX.labelsOn,
    edgeLabelFont: "'Plus Jakarta Sans', 'Apple SD Gothic Neo', Pretendard, -apple-system, sans-serif",
    edgeLabelSize: 11, edgeLabelWeight: "600", edgeLabelColor: { color: "#64748B" },
    enableEdgeEvents: false,
    hideEdgesOnMove: e > 5000,
    hideLabelsOnMove: n > 2000,
    labelFont: "'Plus Jakarta Sans', 'Apple SD Gothic Neo', Pretendard, -apple-system, sans-serif",
    labelWeight: "600", labelColor: { color: "#0F172A" },
    labelRenderedSizeThreshold: GX.isCommunityView ? 9 : n > 5000 ? 11 : n > 800 ? 8 : 5,
    labelGridCellSize: GX.isCommunityView ? 170 : n > 5000 ? 140 : 90,
    labelDensity: GX.isCommunityView ? 0.35 : n > 5000 ? 0.4 : 0.9,
    zIndex: false,
    minCameraRatio: 0.01, maxCameraRatio: 8,
    stagePadding: GX.isGroupView ? 90 : 40,
    labelSize: GX.isGroupView ? 13 : 12,
    nodeReducer, edgeReducer,
  };
}
function ensureRenderer(g) {
  const container = $("gx-canvas");
  // State first: setGraph() runs the reducers synchronously, and they must
  // see the graph they are being asked about.
  GX.graph = g;
  GX.viewNodes = g.nodes(); GX.viewEdges = g.edges();
  GX.hovered = null; GX.hoveredNbrs = null;
  if (GX.selected && !g.hasNode(GX.selected)) { GX.selected = null; GX.selectedNbrs = null; }
  else if (GX.selected) GX.selectedNbrs = new Set(g.neighbors(GX.selected));
  if (GX.renderer) {
    GX.renderer.setSettings(sigmaSettings(g.order, g.size));
    GX.renderer.setGraph(g);
  } else {
    GX.renderer = new Sigma(g, container, sigmaSettings(g.order, g.size));
    bindRendererEvents(GX.renderer);
  }
  GX.renderer.refresh();
}
function styleRefresh() {
  if (!GX.renderer) return;
  GX.renderer.refresh({ partialGraph: { nodes: GX.viewNodes, edges: GX.viewEdges }, skipIndexation: true });
}

/* ---------------- reducers (hover / selection / path) ---------------- */
function nodeReducer(key, data) {
  const focus = GX.hovered || GX.selected;
  const nbrs = GX.hovered ? GX.hoveredNbrs : GX.selectedNbrs;
  const pathOn = GX.path.nodes && !GX.isCommunityView && !GX.isGroupView;
  const onPath = pathOn && GX.path.nodes.has(key);
  if (pathOn) {
    if (onPath) return { ...data, color: PATH_COLOR, size: data.size + 2, forceLabel: true, highlighted: key === focus };
    if (!focus || (key !== focus && !(nbrs && nbrs.has(key)))) return { ...data, color: DIM_NODE, label: null };
  }
  if (!focus) return data;
  if (key === focus) return { ...data, highlighted: true, forceLabel: true, size: data.size + 2 };
  // Neighbors get their names forced only while the neighborhood is small
  // enough to read; a 70-neighbor hub would otherwise bury itself in labels.
  if (nbrs && nbrs.has(key)) return { ...data, forceLabel: nbrs.size <= 20 && (data.size >= 5 || !!GX.selected), highlighted: false };
  return { ...data, color: DIM_NODE, label: null };
}
function edgeReducer(key, data) {
  const focus = GX.hovered || GX.selected;
  if (GX.path.edges && !GX.isCommunityView && !GX.isGroupView) {
    if (GX.path.edges.has(key)) return { ...data, color: PATH_COLOR, size: 2.4 };
    if (!focus) return { ...data, color: "rgba(15, 23, 42, 0.04)" };
  }
  if (!focus) return data;
  const g = GX.graph;
  if (!g.hasEdge(key) || !g.hasNode(focus)) return data;
  if (g.hasExtremity(key, focus)) {
    return { ...data, color: g.source(key) === focus ? "rgba(79, 70, 229, 0.75)" : "rgba(124, 58, 237, 0.75)", size: Math.max(data.size, 1.6) };
  }
  return { ...data, hidden: true };
}
function setHover(key) {
  if (GX.hovered === key) return;
  GX.hovered = key;
  GX.hoveredNbrs = key ? new Set(GX.graph.neighbors(key)) : null;
  if (GX.hoverRaf) cancelAnimationFrame(GX.hoverRaf);
  GX.hoverRaf = requestAnimationFrame(() => { GX.hoverRaf = 0; styleRefresh(); });
}
function setSelected(key) {
  GX.selected = key;
  GX.selectedNbrs = key && GX.graph.hasNode(key) ? new Set(GX.graph.neighbors(key)) : null;
  styleRefresh();
  renderInspector();
}

/* ---------------- renderer events ---------------- */
function bindRendererEvents(r) {
  const stage = $("gx-stage");
  r.on("enterNode", ({ node }) => { setHover(node); stage.style.cursor = "pointer"; });
  r.on("leaveNode", () => { setHover(null); stage.style.cursor = ""; });
  r.on("clickNode", ({ node }) => { setSelected(node); });
  r.on("doubleClickNode", ({ node, event }) => {
    event.preventSigmaDefault();
    if (isGK(node)) drillGroup(GX.graph.getNodeAttribute(node, "gkey"));
    else if (isCK(node)) drillCommunity(GX.graph.getNodeAttribute(node, "cid"));
    else focusEgo(GX.graph.getNodeAttribute(node, "idx"), 1);
  });
  r.on("clickStage", () => { if (GX.selected) setSelected(null); });
  r.on("doubleClickStage", ({ event }) => { event.preventSigmaDefault(); fitView(); });
}

/* ---------------- camera ---------------- */
const anim = (ms) => ({ duration: reduceMotion() ? 0 : ms });
// Port of @sigma/utils fitViewportToNodes (ESM-only, absent from the UMD).
function cameraStateForNodes(sigma, nodes) {
  let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
  let fMinX = Infinity, fMaxX = -Infinity, fMinY = Infinity, fMaxY = -Infinity;
  const graph = sigma.getGraph();
  for (const node of nodes) {
    const data = sigma.getNodeDisplayData(node);
    if (!data) continue;
    const { x, y } = graph.getNodeAttributes(node);
    minX = Math.min(minX, x); maxX = Math.max(maxX, x); minY = Math.min(minY, y); maxY = Math.max(maxY, y);
    fMinX = Math.min(fMinX, data.x); fMaxX = Math.max(fMaxX, data.x); fMinY = Math.min(fMinY, data.y); fMaxY = Math.max(fMaxY, data.y);
  }
  if (!Number.isFinite(minX)) return null;
  const bb = sigma.getCustomBBox() || sigma.getBBox();
  const graphWidth = bb.x[1] - bb.x[0] || 1, graphHeight = bb.y[1] - bb.y[0] || 1;
  const groupWidth = maxX - minX || graphWidth, groupHeight = maxY - minY || graphHeight;
  const { width, height } = sigma.getDimensions();
  const correction = Sigma.utils.getCorrectionRatio({ width, height }, { width: graphWidth, height: graphHeight });
  const ratio = (groupHeight / groupWidth < height / width ? groupWidth : groupHeight) / Math.max(graphWidth, graphHeight) * correction;
  return { ...sigma.getCamera().getState(), angle: 0, x: (fMinX + fMaxX) / 2, y: (fMinY + fMaxY) / 2, ratio: ratio * 1.15 };
}
function fitView(nodes) {
  const r = GX.renderer; if (!r) return;
  if (!nodes || !nodes.length) { r.getCamera().animatedReset(anim(350)); return; }
  const st = cameraStateForNodes(r, nodes);
  if (st) r.getCamera().animate(st, anim(350));
}
function focusNodeCamera(key) {
  const r = GX.renderer; if (!r || !GX.graph.hasNode(key)) return;
  const data = r.getNodeDisplayData(key); if (!data) return;
  const cam = r.getCamera();
  cam.animate({ x: data.x, y: data.y, ratio: Math.min(cam.getState().ratio, 0.35) }, anim(400));
}

/* ---------------- view navigation ---------------- */
function applyView({ pushHistory = true, fit = true } = {}) {
  if (!GX.M) return;
  computeColorRank();
  GX.isGroupView = false;
  const g = GX.view.mode === "groups" ? buildGroupGraph() : GX.view.mode === "communities" ? buildCommunityGraph() : buildNodeGraph();
  ensureRenderer(g);
  if (fit) fitView();
  $("gx-view").querySelectorAll("button").forEach((b) => b.classList.toggle("active", b.dataset.view === GX.view.mode));
  renderChips(); renderCrumbs(); renderLegend(); renderStatus(); renderInspector();
  // Hub-scale "everything at once" warning: once per entry into that view.
  const bigAll = GX.view.mode === "full" && GX.view.scope.kind === "all" && GX.M.n > 20000;
  if (bigAll && !GX.warnedBig) flash("err", tt("gx.big.warn", { n: fmtN(GX.M.n) }));
  GX.warnedBig = bigAll;
  // The first-level button names what the level groups by (folders, or repos on the hub).
  const gb = $("gx-view").querySelector('[data-view="groups"]');
  if (gb) gb.textContent = t(GX.M.groupKind === "repo" ? "gx.view.groups.repo" : "gx.view.groups");
}
function setView(view, { keepSelection = false, keepPath = false, replace = false } = {}) {
  // A path view is never history: its highlight state cannot be restored
  // once cleared, so "back" would land on an empty path scope.
  if (!replace && GX.view.scope.kind !== "path") GX.history.push({ view: GX.view, selected: GX.selected });
  if (GX.history.length > 40) GX.history.shift();
  GX.view = view;
  if (!keepSelection) { GX.selected = null; GX.selectedNbrs = null; }
  // A path highlight belongs to its path view; any other navigation drops it
  // so aggregate views never inherit an all-dimmed canvas.
  if (!keepPath && view.scope.kind !== "path") clearPathState();
  applyView();
}
function goBack() {
  let prev = GX.history.pop();
  while (prev && prev.view.scope.kind === "path") prev = GX.history.pop();   // legacy entries
  if (!prev) { if (GX.selected) setSelected(null); return; }
  if (GX.view.scope.kind === "path") clearPathState();
  GX.view = prev.view; GX.selected = prev.selected; GX.selectedNbrs = null;
  applyView();
}
function drillCommunity(cid) {
  const c = GX.M.commByCid.get(cid); if (!c) return;
  // Remember the folder we came through so the breadcrumb keeps the trail.
  const via = GX.view.scope.kind === "dir" ? GX.view.scope.d : GX.view.scope.dir;
  setView({ mode: "full", scope: via != null ? { kind: "community", cid, dir: via } : { kind: "community", cid } });
}
// Folder → its communities (or straight to nodes when the folder is small).
function drillGroup(k) {
  const grp = GX.M.groupByKey.get(k); if (!grp) return;
  const comms = new Set(grp.members.map((i) => GX.M.c[i]));
  if (grp.members.length <= 600 || comms.size <= 2) setView({ mode: "full", scope: { kind: "dir", d: k } });
  else setView({ mode: "communities", scope: { kind: "dir", d: k } });
}
function focusEgo(idx, hops) {
  setView({ mode: "full", scope: { kind: "ego", node: idx, hops } }, { keepSelection: true });
  selectIfPresent(K(idx));
}
// Select a node key only if the current view graph has it (filters may have
// removed it); otherwise clear the selection and say why.
function selectIfPresent(key) {
  if (GX.graph.hasNode(key)) { GX.selected = key; GX.selectedNbrs = new Set(GX.graph.neighbors(key)); }
  else { GX.selected = null; GX.selectedNbrs = null; if (filtersActive()) flash("err", t("gx.filtered.node")); }
  styleRefresh(); renderInspector();
}
function rootMode(M) {
  if (M.groups.length > 1) return "groups";
  return M.n > COMMUNITY_VIEW_THRESHOLD ? "communities" : "full";
}
function showRoot() { setView({ mode: rootMode(GX.M), scope: { kind: "all" } }); }
// Ensure a node is on screen: switch to a node view containing it if needed.
function revealNode(idx) {
  const key = K(idx);
  if (!GX.isCommunityView && !GX.isGroupView && GX.graph.hasNode(key)) { setSelected(key); focusNodeCamera(key); return; }
  const c = GX.M.commByCid.get(GX.M.c[idx]);
  if (c && c.members.length > 1 && GX.M.n > COMMUNITY_VIEW_THRESHOLD) {
    setView({ mode: "full", scope: { kind: "community", cid: c.id } });
  } else if (!GX.graph.hasNode(key)) {
    setView({ mode: "full", scope: { kind: "all" } });
  }
  if (GX.graph.hasNode(key)) { setSelected(key); focusNodeCamera(key); }
  else if (nodePasses(idx)) { focusEgo(idx, 1); }
  else { setSelected(null); flash("err", t("gx.filtered.node")); }
}

/* ---------------- shortest path (undirected BFS over the full model) ---------------- */
function shortestPath(a, b, maxHops = 8) {
  const M = GX.M;
  if (a === b) return { nodes: [a], edges: [] };
  const prev = new Int32Array(M.n).fill(-2);
  const prevE = new Int32Array(M.n).fill(-1);
  prev[a] = -1;
  let frontier = [a];
  for (let h = 0; h < maxHops && frontier.length; h++) {
    const next = [];
    for (const u of frontier) {
      for (let p = M.adj.off[u]; p < M.adj.off[u + 1]; p++) {
        const v = M.adj.nb[p];
        if (prev[v] !== -2) continue;
        prev[v] = u; prevE[v] = M.adj.eid[p];
        if (v === b) {
          const nodes = [], edges = [];
          for (let x = b; x !== -1; x = prev[x]) { nodes.push(x); if (prevE[x] >= 0) edges.push(prevE[x]); }
          return { nodes: nodes.reverse(), edges: edges.reverse() };
        }
        next.push(v);
      }
    }
    frontier = next;
  }
  return null;
}
function setPathEndpoint(which, idx) {
  GX.path[which] = idx;
  if (GX.path.a != null && GX.path.b != null) {
    const res = shortestPath(GX.path.a, GX.path.b);
    GX.path.result = res;
    if (res) {
      GX.path.nodes = new Set(res.nodes.map(K)); GX.path.edges = new Set(res.edges.map(K));
      // show the path inside its 1-hop neighborhood so context stays visible
      const set = new Set();
      for (const i of res.nodes) for (const v of egoSet(i, 1)) set.add(v);
      // Re-anchoring an existing path REPLACES the path view instead of
      // stacking another one on the history.
      setView({ mode: "full", scope: { kind: "path", nodes: [...set] } }, { keepSelection: true, keepPath: true, replace: GX.view.scope.kind === "path" });
      selectIfPresent(K(idx));
      fitView([...GX.path.nodes].filter((k) => GX.graph.hasNode(k)));
    } else { GX.path.nodes = null; GX.path.edges = null; styleRefresh(); }
  }
  renderInspector();
}
function clearPath() {
  GX.path = { a: null, b: null, nodes: null, edges: null, result: null };
  if (GX.view.scope.kind === "path") {
    // Back to the last non-path view (never leave the user in an orphaned path scope).
    let prev = GX.history.pop();
    while (prev && prev.view.scope.kind === "path") prev = GX.history.pop();
    if (prev) { GX.view = prev.view; GX.selected = prev.selected; GX.selectedNbrs = null; applyView(); }
    else showRoot();
  } else { styleRefresh(); renderInspector(); }
}

/* ---------------- loading ---------------- */
function sourceRoute(srcId) {
  if (srcId === "all") return `/repos/all/graph`;
  const mine = (S.servers || []).some((s) => s.server_id === srcId) || (S.repos || []).some((r) => r.repo_id === srcId);
  return mine ? `/repos/${encodeURIComponent(srcId)}/graph` : `/catalog/${encodeURIComponent(srcId)}/graph`;
}
async function fetchWithProgress(url, total, onProgress) {
  // Bare fetch: no Authorization header (that is for the platform API; S3
  // validates the SigV4 query string), no credentials.
  const resp = await fetch(url, { method: "GET", mode: "cors", credentials: "omit" });
  if (resp.status === 403) throw new Error(t("gx.err.expired"));
  if (!resp.ok) throw new Error(`${t("gx.err.fetch")} (HTTP ${resp.status})`);
  if (!resp.body || !resp.body.getReader) return resp.text();
  const reader = resp.body.getReader();
  const chunks = []; let got = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    chunks.push(value); got += value.byteLength;
    onProgress(got, total);
  }
  const buf = new Uint8Array(got); let o = 0;
  for (const c of chunks) { buf.set(c, o); o += c.byteLength; }
  return new TextDecoder().decode(buf);
}
async function loadSource(srcId, { force = false } = {}) {
  if (!srcId) return;
  if (!libsReady()) { showOverlay("error", GX.libsError === "webgl" ? t("gx.err.webgl") : t("gx.err.libs")); return; }
  const seq = ++GX.loadSeq;
  GX.srcId = srcId; GX.loading = true;
  try { sessionStorage.setItem("gfy-gx-src", srcId); } catch {}
  try { history.replaceState(null, "", `#graph/${encodeURIComponent(srcId)}`); } catch {}
  $("gx-source").value = srcId;
  showOverlay("loading", `${t("gx.st.loading")}…`);
  try {
    const info = await api("GET", sourceRoute(srcId));
    if (seq !== GX.loadSeq) return;
    // Bearer URLs live in locals only; GX.info never holds them.
    const vizUrl = info.viz ? info.viz.url : null, graphUrl = info.graph ? info.graph.url : null;
    if (info.viz) info.viz.url = null;
    if (info.graph) info.graph.url = null;
    GX.info = info;
    if (info.state !== "ready") {
      const s = String(info.status || "").toUpperCase();
      const retry = el("button", { class: "ghost mini", onclick: () => loadSource(srcId, { force: true }) }, t("gx.retry"));
      if (info.state === "empty") showOverlay("card", t("gx.failed.title"), t("gx.failed.body") + (info.last_error ? `\n${clean(info.last_error, 300)}` : ""), retry);
      else {
        showOverlay("card", t("gx.pending.title"), tt("gx.pending.body", { s: s || "-" }), retry);
        // A build in flight resolves on its own: re-check while the tab is open.
        clearTimeout(GX.pollTimer);
        GX.pollTimer = setTimeout(() => { if (GX.shown && GX.srcId === srcId && !GX.M) loadSource(srcId); }, 20000);
      }
      return;
    }
    const etag = info.viz ? "v:" + info.viz.etag : info.graph ? "g:" + info.graph.etag : "";
    const cached = GX.cache.get(srcId);
    let M;
    if (cached && cached.etag === etag && !force) {
      M = cached.M;
    } else if (vizUrl) {
      if ((info.viz.raw_bytes || 0) > MAX_BUNDLE_RAW_BYTES) throw new Error(tt("gx.toobig.body", { size: fmtBytes(info.viz.raw_bytes) }));
      // The stream yields DECODED bytes (Content-Encoding: gzip is transparent),
      // so the denominator is the bundle's uncompressed size, not Content-Length.
      const text = await fetchWithProgress(vizUrl, info.viz.raw_bytes || 0, (got, total) => showProgress(got, total, info.viz.bytes));
      if (seq !== GX.loadSeq) return;
      showOverlay("loading", t("gx.st.parsing"));
      await new Promise((r) => setTimeout(r, 0));
      M = fromBundle(JSON.parse(text), srcId);
    } else if (graphUrl) {
      const text = await fetchWithProgress(graphUrl, info.graph.bytes, (got, total) => showProgress(got, total));
      if (seq !== GX.loadSeq) return;
      showOverlay("loading", t("gx.st.parsing"));
      await new Promise((r) => setTimeout(r, 0));
      M = fromRaw(JSON.parse(text), srcId);
      flash("ok", tt("gx.rawnotice", { size: fmtBytes(info.graph.bytes) }));
    } else {
      const size = info.graph ? fmtBytes(info.graph.bytes) : "?";
      showOverlay("card", t("gx.toobig.title"), tt(srcId === "all" ? "gx.toobig.hub" : "gx.toobig.body", { size }), rebuildButton(srcId));
      return;
    }
    if (seq !== GX.loadSeq) return;
    GX.cache.set(srcId, { etag, M });
    // A parsed model is tens of MB; keep the last few sources only.
    while (GX.cache.size > 3) GX.cache.delete(GX.cache.keys().next().value);
    GX.M = M;
    GX.history = []; GX.selected = null; GX.selectedNbrs = null; clearPathState(); GX.code.range = null;
    GX.filters = { types: new Set(), relations: new Set(), repos: new Set(), legend: new Set(), minDeg: 0, inferred: true };
    GX.colorBy = M.r && M.repos.length > 1 ? "repo" : (GX.colorBy === "repo" ? "dir" : GX.colorBy);
    $("gx-color").value = GX.colorBy;
    $("gx-color").querySelector('option[value="repo"]').hidden = !(M.r && M.repos.length > 1);
    $("gx-mindeg").value = "0"; $("gx-mindeg-val").textContent = "0"; $("gx-inferred").checked = true;
    $("gx-simple").checked = false; GX.simple = false; GX.warnedBig = false;
    $("gx-legend-filter").value = ""; legendLimit = 60;
    $("gx-mindeg").max = String(Math.min(20, Math.max(1, Math.round(Math.sqrt(M.maxDeg)))));
    renderChips();
    hideOverlay();
    GX.view = { mode: rootMode(M), scope: { kind: "all" } };
    applyView();
    if (!M.n) showOverlay("card", t("gx.emptygraph.title"), t("gx.emptygraph.body"));
  } catch (e) {
    if (seq !== GX.loadSeq) return;
    const msg = e && e.message ? e.message : String(e);
    showOverlay("error", /JSON|parse|bad bundle|bad graph/i.test(msg) ? t("gx.err.parse") : t("gx.err.fetch"), clean(msg, 200),
      el("button", { class: "mini", onclick: () => loadSource(srcId, { force: true }) }, t("gx.retry")));
  } finally { if (seq === GX.loadSeq) GX.loading = false; }
}
function clearPathState() { GX.path = { a: null, b: null, nodes: null, edges: null, result: null }; }
function rebuildButton(srcId) {
  const mine = (S.repos || []).some((r) => r.repo_id === srcId);
  if (!mine || typeof rebuildRepo !== "function") return null;
  return el("button", { class: "mini", onclick: () => rebuildRepo(srcId) }, t("gx.rebuild"));
}

/* ---------------- overlay / progress / hud / status ---------------- */
function showOverlay(kind, title, body, action) {
  const ov = $("gx-overlay"), card = $("gx-overlay-card");
  card.replaceChildren();
  if (kind === "loading") {
    card.append(el("div", { class: "gx-spinner" }), el("h4", null, title),
      el("div", { class: "gx-progress indeterminate", id: "gx-progress" }, el("div")), el("p", { id: "gx-progress-txt" }, ""));
  } else {
    card.append(el("h4", null, title));
    if (body) for (const line of String(body).split("\n")) card.append(el("p", null, line));
    if (action) card.append(el("div", { class: "btns" }, action));
    if (kind === "error") card.style.borderColor = "#FECACA"; else card.style.borderColor = "";
  }
  ov.hidden = false;
}
function hideOverlay() { $("gx-overlay").hidden = true; }
function showProgress(got, total, rawBytes) {  // rawBytes = compressed transfer size, shown as an aside
  const bar = $("gx-progress"), txt = $("gx-progress-txt");
  if (!bar) return;
  if (total) { bar.classList.remove("indeterminate"); bar.firstChild.style.width = `${Math.min(100, Math.round(got / total * 100))}%`; }
  txt.textContent = `${fmtBytes(got)}${total ? " / " + fmtBytes(total) : ""}${rawBytes ? ` (gzip ${fmtBytes(rawBytes)})` : ""}`;
}
function showHud() {
  const hud = $("gx-hud"); hud.replaceChildren();
  if (!GX.M) return;
  const g = GX.graph;
  if (GX.isGroupView) hud.append(el("span", null, `${t(GX.M.groupKind === "repo" ? "gx.view.groups.repo" : "gx.view.groups")} ${fmtN(g.order)}, ${t("gx.st.edges")} ${fmtN(g.size)}`));
  else if (GX.isCommunityView) hud.append(el("span", null, `${t("gx.st.communities")} ${fmtN(g.order)}, ${t("gx.st.edges")} ${fmtN(g.size)}`));
  else hud.append(el("span", null, `${t("gx.st.shown")} ${fmtN(g.order)} / ${fmtN(GX.M.n)}, ${t("gx.st.edges")} ${fmtN(g.size)}`));
  if (GX.simple) hud.append(el("span", { class: "warn" }, t("gx.simple")));
  renderScopeTitle();
  if (filtersActive()) {
    const scope = scopeNodeSet();
    let hidden = 0;
    if (scope) { for (const i of scope) if (!nodePasses(i)) hidden++; }
    else { for (let i = 0; i < GX.M.n; i++) if (!nodePasses(i)) hidden++; }
    hud.append(el("span", { class: "warn" }, tt("gx.hud.filtered", { n: fmtN(hidden) })));
  }
}
// Title strip inside the stage naming the drilled-down scope, so a node view
// always says which folder/community it is showing.
function renderScopeTitle() {
  const box = $("gx-title-hud"); if (!box) return;
  box.replaceChildren(); box.hidden = true;
  const M = GX.M, sc = GX.view.scope; if (!M) return;
  let title = "", sub = "";
  if (sc.kind === "community") {
    const c = M.commByCid.get(sc.cid); if (!c) return;
    title = communityLabel(c); sub = `${t("gx.hud.scope.community")}, ${fmtN(c.members.length)} ${t("gx.st.nodes")}${c.dir ? `, ${c.dir}/ ${Math.round(c.dirShare * 100)}%` : ""}`;
  } else if (sc.kind === "dir") {
    const grp = M.groupByKey.get(sc.d); if (!grp) return;
    title = dirLabel(grp.label); sub = `${t(M.groupKind === "repo" ? "gx.gk.repo" : "gx.hud.scope.dir")}, ${fmtN(grp.members.length)} ${t("gx.st.nodes")}, ${fmtN(new Set(grp.members.map((i) => M.c[i])).size)} ${t("gx.st.communities")}`;
  } else if (sc.kind === "ego") {
    title = M.label[sc.node]; sub = `${t("gx.crumb.ego")}, ${tt("gx.hop", { n: sc.hops })}`;
  } else return;
  box.append(el("strong", null, midTrunc(title, 60)), el("span", null, sub));
  box.hidden = false;
}
function renderStatus() {
  const st = $("gx-status"); st.replaceChildren();
  const M = GX.M; if (!M) return;
  const sep = () => el("span", { class: "sep" }, "/");
  st.append(el("span", null, `${t("gx.st.nodes")} ${fmtN(M.n)}`), sep(), el("span", null, `${t("gx.st.edges")} ${fmtN(M.e)}`), sep(),
    el("span", null, `${t("gx.st.communities")} ${fmtN(M.communities.length)}`), sep(),
    el("span", null, M.layoutSource === "build" ? t("gx.st.layout") : t("gx.st.layout.client")));
  const built = (GX.info && GX.info.last_built_at) || M.header.generated_at;
  if (built) st.append(sep(), el("span", null, `${t("gx.st.built")} ${String(built).slice(0, 16).replace("T", " ")}`));
  if (M.header.built_at_commit) st.append(sep(), el("span", { class: "mono" }, M.header.built_at_commit.slice(0, 10)));
  showHud();
}
function renderCrumbs() {
  const c = $("gx-crumbs"); c.replaceChildren();
  if (!GX.M) return;
  const crumb = (label, onclick) => onclick ? el("button", { onclick }, label) : el("span", { class: "cur" }, label);
  const sep = () => el("span", { class: "sep" }, "›");
  const rootLabel = `${serverName(GX.srcId)} (${fmtN(GX.M.n)})`;
  const sc = GX.view.scope;
  const atRoot = sc.kind === "all" && GX.view.mode === rootMode(GX.M);
  c.append(crumb(rootLabel, atRoot ? null : () => { GX.history = []; showRoot(); }));
  if (sc.kind === "all" && !atRoot) c.append(sep(), crumb(GX.view.mode === "communities" ? t("gx.crumb.community") : t("gx.crumb.all")));
  if (sc.kind === "dir") {
    const grp = GX.M.groupByKey.get(sc.d);
    const lbl = `${t(GX.M.groupKind === "repo" ? "gx.crumb.group.repo" : "gx.crumb.group")}: ${grp ? dirLabel(grp.label) : sc.d} (${fmtN(grp ? grp.members.length : 0)})`;
    c.append(sep(), GX.view.mode === "communities" ? crumb(lbl) : crumb(lbl, () => setView({ mode: "communities", scope: { kind: "dir", d: sc.d } })));
    if (GX.view.mode === "full") c.append(sep(), crumb(t("gx.crumb.all")));
  }
  if (sc.kind === "community") {
    if (sc.dir != null) {
      const grp = GX.M.groupByKey.get(sc.dir);
      c.append(sep(), crumb(`${t(GX.M.groupKind === "repo" ? "gx.crumb.group.repo" : "gx.crumb.group")}: ${grp ? dirLabel(grp.label) : sc.dir}`, () => setView({ mode: "communities", scope: { kind: "dir", d: sc.dir } })));
    }
    const cm = GX.M.commByCid.get(sc.cid); c.append(sep(), crumb(`${t("gx.crumb.community")}: ${cm ? communityLabel(cm) : sc.cid} (${fmtN(GX.graph.order)})`));
  }
  if (sc.kind === "ego") c.append(sep(), crumb(`${t("gx.crumb.ego")}: ${midTrunc(GX.M.label[sc.node], 40)}, ${tt("gx.hop", { n: sc.hops })} (${fmtN(GX.graph.order)})`));
  if (sc.kind === "path") c.append(sep(), crumb(`${t("gx.crumb.path")} (${fmtN(GX.graph.order)})`));
  if (GX.history.length) c.append(el("button", { onclick: goBack, title: "Esc", style: "margin-left:auto;" }, "← " + t("gx.back")));
}

/* ---------------- filter chips + legend ---------------- */
// Rebuilding the chip/legend DOM would drop keyboard focus: remember the
// focused control's key before a rebuild and put focus back afterwards.
function focusedKey(container) {
  const a = document.activeElement;
  return a && container.contains(a) ? a.dataset.key || (a.closest("[data-key]") || {}).dataset?.key || "" : "";
}
function restoreFocus(container, key) {
  if (!key) return;
  const n = container.querySelector(`[data-key="${key.replace(/"/g, '\\"')}"]`);
  if (n) (n.matches("input,button") ? n : n.querySelector("input") || n).focus();
}
function chip(label, count, active, onToggle, swatch, mono, key) {
  const b = el("button", { class: `gx-chip${active ? "" : " off"}${mono ? " mono" : ""}`, type: "button", "aria-pressed": String(active), onclick: onToggle, "data-key": key || label });
  if (swatch) b.append(el("span", { class: "sw", style: `background:${swatch}` }));
  b.append(document.createTextNode(label));
  if (count != null) b.append(el("span", { class: "n" }, fmtN(count)));
  return b;
}
function renderChips() {
  const M = GX.M, F = GX.filters;
  const left = $("gx-left"); const fk = focusedKey(left);
  const typesEl = $("gx-types"); typesEl.replaceChildren();
  const tCount = new Map(); for (let i = 0; i < M.n; i++) tCount.set(M.t[i], (tCount.get(M.t[i]) || 0) + 1);
  for (const [ti, n] of [...tCount.entries()].sort((a, b) => b[1] - a[1])) {
    const tchip = chip(glossType(M.types[ti]), n, !F.types.has(ti), () => { toggle(F.types, ti); applyView({ fit: false }); }, typeColor(M.types[ti]) || GREY, false, "t:" + ti);
    tchip.title = M.types[ti] || "";
    typesEl.append(tchip);
  }
  const relEl = $("gx-relations"); relEl.replaceChildren();
  const rCount = new Map(); for (let e = 0; e < M.e; e++) rCount.set(M.er[e], (rCount.get(M.er[e]) || 0) + 1);
  for (const [ri, n] of [...rCount.entries()].sort((a, b) => b[1] - a[1])) {
    const rchip = chip(glossRel(M.relations[ri]), n, !F.relations.has(ri), () => { toggle(F.relations, ri); applyView({ fit: false }); }, null, !isKo(), "r:" + ri);
    rchip.title = M.relations[ri] || "";
    relEl.append(rchip);
  }
  const repoField = $("gx-repos-field"), repoEl = $("gx-repos"); repoEl.replaceChildren();
  repoField.hidden = !(M.r && M.repos.length > 1);
  if (M.r) {
    const c = new Map(); for (let i = 0; i < M.n; i++) c.set(M.r[i], (c.get(M.r[i]) || 0) + 1);
    for (const [ri, n] of [...c.entries()].sort((a, b) => b[1] - a[1])) {
      repoEl.append(chip(midTrunc(serverName(M.repos[ri]) || M.repos[ri], 30), n, !F.repos.has(ri), () => { toggle(F.repos, ri); applyView({ fit: false }); }, null, true, "p:" + ri));
    }
  }
  restoreFocus(left, fk);
}
function toggle(set, v) { if (set.has(v)) set.delete(v); else set.add(v); }
let legendLimit = 60;
function renderLegend() {
  const M = GX.M, F = GX.filters, box = $("gx-legend"); const fk = focusedKey(box); box.replaceChildren();
  if (!M) return;
  const q = ($("gx-legend-filter").value || "").trim().toLowerCase();
  const cats = GX.legendCats.filter((c) => !q || c.label.toLowerCase().includes(q));
  $("gx-legend-count").textContent = fmtN(GX.legendCats.length);
  const shown = cats.slice(0, legendLimit);
  for (const c of shown) {
    const row = clickable(el("div", { class: `gx-lg${F.legend.has(c.cat) ? " off" : ""}${GX.view.scope.kind === "community" && GX.colorBy === "community" && GX.view.scope.cid === c.cat ? " active" : ""}`, "data-key": "l:" + c.cat }));
    const cb = el("input", { type: "checkbox" }); cb.checked = !F.legend.has(c.cat);
    cb.addEventListener("change", (e) => { e.stopPropagation(); toggle(F.legend, c.cat); applyView({ fit: false }); });
    row.append(cb, el("span", { class: "sw", style: `background:${c.color}` }));
    const lb = el("span", { class: "lb" }, c.label);
    if (GX.colorBy === "community") { const cm = M.commByCid.get(c.cat); if (cm && cm.auto) lb.append(" ", el("small", null, `#${c.cat}`)); }
    row.append(lb, el("span", { class: "n" }, fmtN(c.n)));
    row.title = c.label;
    row.onclick = (e) => {
      if (e.target === cb) return;
      if (GX.colorBy === "community") { drillCommunity(c.cat); }
      else if (e.altKey) { F.legend = new Set(GX.legendCats.map((x) => x.cat).filter((x) => x !== c.cat)); applyView({ fit: false }); }
      else selectCategory(c.cat);
    };
    box.append(row);
  }
  if (cats.length > shown.length) {
    box.append(el("div", { class: "gx-lg-more" }, el("button", { class: "ghost mini", onclick: () => { legendLimit += 200; renderLegend(); } }, tt("gx.legend.more", { n: fmtN(cats.length - shown.length) }))));
  }
  if (GX.legendCats.length > PALETTE.length && GX.colorBy !== "type") {
    box.append(el("div", { class: "gx-empty", style: "padding:6px;" }, tt("gx.legend.other", { n: fmtN(GX.legendCats.length - PALETTE.length) })));
  }
  restoreFocus(box, fk);
}
// Legend row click (non-community modes): spotlight that category via the
// hover mechanism — every other node dims until the pointer moves.
function selectCategory(cat) {
  const g = GX.graph; const keys = [];
  if (GX.isGroupView) {
    for (const grp of GX.M.groups) if (g.hasNode("g:" + grp.k) && dominantCat(grp.members) === cat) keys.push("g:" + grp.k);
  } else if (GX.isCommunityView) {
    for (const c of GX.M.communities) if (catOfCommunity(c) === cat && g.hasNode(CK(c.id))) keys.push(CK(c.id));
  } else {
    g.forEachNode((k, a) => { if (catOfNode(a.idx) === cat) keys.push(k); });
  }
  if (keys.length) fitView(keys);
}

/* ---------------- inspector ---------------- */
// Click-only rows become keyboard-operable (Enter/Space) without changing markup.
function clickable(node) {
  node.tabIndex = 0; node.setAttribute("role", "button");
  node.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); node.click(); } });
  return node;
}
function copyBtn(text) {
  return el("button", { class: "gx-copy", onclick: (e) => { copyText(text); const b = e.target; const o = b.textContent; b.textContent = t("gx.ins.copied"); setTimeout(() => { b.textContent = o; }, 1200); } }, t("gx.ins.copy"));
}
function nodeRow(i, meta) {
  const M = GX.M;
  const row = el("div", { class: "gx-nb", onclick: () => revealNode(i), title: `${M.label[i]}\n${M.files[M.f[i]]}${M.loc[i] ? ":" + M.loc[i] : ""}` },
    el("span", { class: "sw", style: `background:${colorOfCat(catOfNode(i))}` }),
    el("span", { class: "lb" }, midTrunc(M.label[i], 44)));
  if (meta) row.append(el("span", { class: "meta" }, meta));
  return clickable(row);
}
function renderInspector() {
  const box = $("gx-inspector"); box.replaceChildren();
  const M = GX.M;
  if (!M) { box.append(el("p", { class: "gx-empty" }, t("gx.ins.empty"))); return; }
  const key = GX.selected;
  if (!key || !GX.graph.hasNode(key)) { renderSummary(box); return; }
  if (isGK(key)) renderGroupInspector(box, GX.M.groupByKey.get(GX.graph.getNodeAttribute(key, "gkey")));
  else if (isCK(key)) renderCommunityInspector(box, GX.M.commByCid.get(GX.graph.getNodeAttribute(key, "cid")));
  else renderNodeInspector(box, GX.graph.getNodeAttribute(key, "idx"));
}
function bars(box, entries, total) {
  const wrap = el("div", { class: "gx-bars" });
  for (const [k, n] of entries) {
    wrap.append(el("div", { class: "gx-bar" }, el("span", { class: "k", title: k }, k),
      el("div", { class: "b" }, el("div", { style: `width:${Math.max(2, n / total * 100)}%` })), el("span", { class: "n" }, fmtN(n))));
  }
  box.append(wrap);
}
function renderGroupInspector(box, grp) {
  if (!grp) return;
  const M = GX.M;
  box.append(el("div", { class: "gx-title" }, dirLabel(grp.label)));
  if (grp.sub) box.append(el("div", { class: "gx-sub mono" }, grp.sub));
  const comms = new Set(grp.members.map((i) => M.c[i]));
  const kv = el("dl", { class: "gx-kv" });
  kv.append(el("dt", null, t("gx.ins.size")), el("dd", null, fmtN(grp.members.length)));
  kv.append(el("dt", null, t("gx.ins.communities")), el("dd", null, fmtN(comms.size)));
  box.append(kv);
  box.append(el("div", { class: "btns" }, el("button", { class: "mini", onclick: () => drillGroup(grp.k) }, t("gx.ins.drill"))));
  const tc = new Map(); for (const i of grp.members) { const k = glossType(M.types[M.t[i]]); tc.set(k, (tc.get(k) || 0) + 1); }
  box.append(el("h4", null, t("gx.ins.types"))); bars(box, [...tc.entries()].sort((a, b) => b[1] - a[1]), grp.members.length);
  box.append(el("h4", null, t("gx.ins.hubs")));
  for (const i of grp.members.slice().sort((a, b) => M.deg[b] - M.deg[a]).slice(0, 8)) box.append(nodeRow(i, `${M.deg[i]}`));
  const linked = M.gedges.filter((ge) => ge[0] === grp.k || ge[1] === grp.k).slice(0, 8);
  if (linked.length) {
    box.append(el("h4", null, t("gx.ins.groups.linked")));
    for (const ge of linked) {
      const other = M.groupByKey.get(ge[0] === grp.k ? ge[1] : ge[0]); if (!other) continue;
      box.append(el("div", { class: "gx-nb", onclick: () => { const k = "g:" + other.k; if (GX.graph.hasNode(k)) { setSelected(k); focusNodeCamera(k); } } },
        el("span", { class: "lb", style: "font-family:var(--font);" }, dirLabel(other.label)), el("span", { class: "meta" }, `${fmtN(ge[2])} ${t("gx.st.edges")}`)));
    }
  }
}
function renderSummary(box) {
  const M = GX.M, info = GX.info || {};
  // One-paragraph plain-language orientation before the numbers.
  const topGroup = M.groups[0];
  let hubIdx = 0;
  for (let i = 1; i < M.n; i++) if (M.deg[i] > M.deg[hubIdx]) hubIdx = i;
  if (M.n) {
    const card = el("div", { class: "gx-summary" });
    card.append(el("p", null, richText("gx.summary", {
      n: fmtN(M.n), e: fmtN(M.e), c: fmtN(M.communities.length), gk: t(M.groupKind === "repo" ? "gx.gk.repo" : "gx.gk.dir"),
      top: topGroup ? `${dirLabel(topGroup.label)} (${fmtN(topGroup.members.length)})` : "-", hub: midTrunc(M.label[hubIdx], 40), d: fmtN(M.deg[hubIdx]),
    })));
    card.append(el("p", { class: "hint-line" }, t("gx.summary.hint")));
    box.append(card);
  }
  box.append(el("h4", null, t("gx.ins.stats")));
  const grid = el("div", { class: "gx-stat-grid" });
  const stat = (n, l) => el("div", { class: "gx-stat" }, el("div", { class: "n" }, fmtN(n)), el("div", { class: "l" }, l));
  grid.append(stat(M.n, t("gx.st.nodes")), stat(M.e, t("gx.st.edges")), stat(M.communities.length, t("gx.st.communities")), stat(M.files.length, LANG === "ko" ? "파일" : "files"));
  box.append(grid);
  const tc = new Map(); for (let i = 0; i < M.n; i++) { const k = glossType(M.types[M.t[i]]); tc.set(k, (tc.get(k) || 0) + 1); }
  box.append(el("h4", null, t("gx.ins.types"))); bars(box, [...tc.entries()].sort((a, b) => b[1] - a[1]), M.n);
  const rc = new Map(); for (let e = 0; e < M.e; e++) { const k = glossRel(M.relations[M.er[e]]); rc.set(k, (rc.get(k) || 0) + 1); }
  box.append(el("h4", null, t("gx.ins.relations"))); bars(box, [...rc.entries()].sort((a, b) => b[1] - a[1]).slice(0, 8), M.e || 1);
  box.append(el("h4", null, t("gx.ins.top")));
  const top = Array.from({ length: M.n }, (_, i) => i).sort((a, b) => M.deg[b] - M.deg[a]).slice(0, 8);
  for (const i of top) box.append(nodeRow(i, `${M.deg[i]}`));
  box.append(el("p", { class: "gx-empty", style: "margin-top:12px;" }, t("gx.ins.empty")), el("p", { class: "gx-empty" }, t("gx.ins.shortcuts")));
}
function renderNodeInspector(box, i) {
  const M = GX.M;
  box.append(el("div", { class: "gx-title" }, M.label[i]));
  const pills = el("div", { class: "gx-pills" });
  pills.append(el("span", { class: "pill neutral", style: `background:${typeColor(M.types[M.t[i]]) || GREY}22; color:#0F172A;`, title: M.types[M.t[i]] || "" }, glossType(M.types[M.t[i]])));
  if (M.kinds[M.k[i]]) pills.append(el("span", { class: "pill neutral", title: M.kinds[M.k[i]] }, glossKind(M.kinds[M.k[i]])));
  const cm = M.commByCid.get(M.c[i]);
  if (cm) pills.append(el("span", { class: "pill private", style: "cursor:pointer;", title: t("gx.ins.community"), onclick: () => drillCommunity(cm.id) }, midTrunc(communityLabel(cm), 36)));
  box.append(pills);
  const kv = el("dl", { class: "gx-kv" });
  const file = M.files[M.f[i]];
  const fileRef = file ? `${file}${M.loc[i] ? ":" + M.loc[i] : ""}` : "-";
  kv.append(el("dt", null, t("gx.ins.file")), el("dd", { class: "mono" }, fileRef, file ? copyBtn(fileRef) : ""));
  kv.append(el("dt", null, t("gx.ins.id")), el("dd", { class: "mono" }, midTrunc(M.id[i], 60), copyBtn(M.id[i])));
  let ind = 0, outd = 0;
  for (let p = M.adj.off[i]; p < M.adj.off[i + 1]; p++) { if (M.adj.dir[p]) outd++; else ind++; }
  kv.append(el("dt", null, t("gx.ins.degree")), el("dd", null, `${fmtN(M.deg[i])} (${t("gx.ins.out")} ${fmtN(outd)}, ${t("gx.ins.in")} ${fmtN(ind)})`));
  if (M.r) kv.append(el("dt", null, t("gx.ins.repo")), el("dd", { class: "mono" }, M.repos[M.r[i]] || "-"));
  box.append(kv);
  const pathB = el("button", { class: "ghost mini", onclick: () => setPathEndpoint("b", i) }, t("gx.ins.pathB"));
  pathB.disabled = GX.path.a == null || GX.path.a === i;
  box.append(el("div", { class: "btns" },
    el("button", { class: "ghost mini", onclick: () => focusEgo(i, 1) }, t("gx.ins.ego1")),
    el("button", { class: "ghost mini", onclick: () => focusEgo(i, 2) }, t("gx.ins.ego2")),
    el("button", { class: "ghost mini", onclick: () => setPathEndpoint("a", i) }, t("gx.ins.pathA")),
    pathB,
    el("button", { class: "ghost mini", onclick: () => copyContext(i) }, t("gx.ins.ctx")),
    el("button", { class: "mini", onclick: () => askPlayground(i) }, t("gx.ins.ask"))));
  renderPathSection(box);
  renderSourceSection(box, i);
  // neighbors grouped by relation × direction, semantic priority order
  const groups = new Map();
  for (let p = M.adj.off[i]; p < M.adj.off[i + 1]; p++) {
    const e = M.adj.eid[p], rel = M.relations[M.er[e]] || "related", dir = M.adj.dir[p] ? "out" : "in";
    const gk = `${dir}|${rel}`;
    if (!groups.has(gk)) groups.set(gk, { rel, dir, nodes: [] });
    groups.get(gk).nodes.push({ v: M.adj.nb[p], inf: M.einf[e] });
  }
  const pri = (rel) => { const k = REL_PRIORITY.indexOf(rel); return k < 0 ? 50 : k; };
  const sorted = [...groups.values()].sort((a, b) => pri(a.rel) - pri(b.rel) || (a.dir === "out" ? -1 : 1));
  box.append(el("h4", null, t("gx.ins.neighbors"), el("span", { class: "gx-count" }, fmtN(M.adj.off[i + 1] - M.adj.off[i]))));
  for (const grp of sorted) {
    const det = el("details", { class: "gx-nb-group" }); det.open = grp.nodes.length <= 12;
    const sum = el("summary", { title: grp.rel }, el("span", { class: "rel" }, grp.dir === "out" ? tt("gx.rel.out", { r: glossRel(grp.rel) }) : tt("gx.rel.in", { r: glossRel(grp.rel) })),
      el("span", { class: "n" }, fmtN(grp.nodes.length)));
    det.append(sum);
    const list = el("div");
    const rows = grp.nodes.sort((a, b) => M.deg[b.v] - M.deg[a.v]);
    const cap = 25;
    for (const { v, inf } of rows.slice(0, cap)) list.append(nodeRow(v, inf ? t("gx.inferred.short") : ""));
    if (rows.length > cap) {
      list.append(el("button", { class: "ghost mini", style: "margin:4px 12px;", onclick: (ev) => {
        ev.target.remove(); for (const { v, inf } of rows.slice(cap)) list.append(nodeRow(v, inf ? t("gx.inferred.short") : ""));
      } }, tt("gx.ins.more", { n: fmtN(rows.length - cap) })));
    }
    det.append(list); box.append(det);
  }
}
function renderCommunityInspector(box, c) {
  if (!c) return;
  const M = GX.M;
  box.append(el("div", { class: "gx-title" }, communityLabel(c)));
  if (c.auto) box.append(el("div", { class: "gx-sub" }, `${tt("gx.community.n", { n: c.id })}: ${t("gx.ins.auto")}`));
  const kv = el("dl", { class: "gx-kv" });
  kv.append(el("dt", null, t("gx.ins.size")), el("dd", null, fmtN(c.members.length)));
  if (c.dir) kv.append(el("dt", null, t("gx.ins.dir")), el("dd", { class: "mono" }, `${c.dir}/ (${Math.round(c.dirShare * 100)}%)`));
  if (M.r) kv.append(el("dt", null, t("gx.ins.repo")), el("dd", { class: "mono" }, M.repos[c.repo] || "-"));
  box.append(kv);
  box.append(el("div", { class: "btns" }, el("button", { class: "mini", onclick: () => drillCommunity(c.id) }, t("gx.ins.drill"))));
  const tc = new Map(); for (const i of c.members) { const k = glossType(M.types[M.t[i]]); tc.set(k, (tc.get(k) || 0) + 1); }
  box.append(el("h4", null, t("gx.ins.types"))); bars(box, [...tc.entries()].sort((a, b) => b[1] - a[1]), c.members.length);
  box.append(el("h4", null, t("gx.ins.hubs")));
  for (const i of c.members.slice().sort((a, b) => M.deg[b] - M.deg[a]).slice(0, 8)) box.append(nodeRow(i, `${M.deg[i]}`));
  const linked = M.cedges.filter((ce) => ce[0] === c.id || ce[1] === c.id).sort((a, b) => b[2] - a[2]).slice(0, 8);
  if (linked.length) {
    box.append(el("h4", null, t("gx.ins.linked")));
    for (const ce of linked) {
      const other = M.commByCid.get(ce[0] === c.id ? ce[1] : ce[0]); if (!other) continue;
      box.append(el("div", { class: "gx-nb", onclick: () => { if (GX.isCommunityView && GX.graph.hasNode(CK(other.id))) { setSelected(CK(other.id)); focusNodeCamera(CK(other.id)); } else drillCommunity(other.id); } },
        el("span", { class: "sw", style: `background:${colorOfCat(catOfCommunity(other))}` }),
        el("span", { class: "lb", style: "font-family:var(--font);" }, midTrunc(communityLabel(other), 40)), el("span", { class: "meta" }, `${fmtN(ce[2])} ${t("gx.st.edges")}`)));
    }
  }
}
function renderPathSection(box) {
  const P = GX.path, M = GX.M;
  if (P.a == null && P.b == null) return;
  box.append(el("h4", null, t("gx.ins.pathtitle"), el("button", { class: "gx-copy", style: "margin-left:auto;", onclick: clearPath }, t("gx.ins.clearpath"))));
  if (P.a != null && P.b == null) { box.append(el("p", { class: "gx-empty" }, `A: ${midTrunc(M.label[P.a], 40)}. ${t("gx.ins.pathpick")}`)); return; }
  if (!P.result) { box.append(el("p", { class: "gx-empty" }, t("gx.ins.pathnone"))); return; }
  const list = el("div", { class: "gx-path-list" });
  P.result.nodes.forEach((v, k) => {
    const step = el("div", { class: "step", onclick: () => revealNode(v) }, el("span", { class: "sw", style: `display:inline-block;width:8px;height:8px;border-radius:50%;background:${PATH_COLOR}` }), el("span", null, midTrunc(M.label[v], 40)));
    list.append(step);
    if (k < P.result.edges.length) {
      const e = P.result.edges[k]; const fwd = M.es[e] === v;
      list.append(el("div", { class: "step rel", style: "padding-left:18px;", title: M.relations[M.er[e]] || "" }, `${fwd ? "↓" : "↑"} ${glossRel(M.relations[M.er[e]])}`));
    }
  });
  box.append(el("div", { class: "gx-sub", style: "margin-bottom:4px;" }, `${P.result.edges.length} ${t("gx.ins.hops")}`), list);
}
/* ---------------- source viewer ----------------
 * Per-repo MCP servers carry read_source(file, start_line, end_line), served
 * from the build's source snapshot. The console asks the platform API
 * (POST /repos/{id}/source), which applies the same access rule as the graph
 * routes and forwards the single read to the repo's task — no API key needed. */
const CODE_WINDOW = 40;
function sourceServerFor(i) {
  // Hub nodes belong to a repo; its dedicated server holds the snapshot.
  const M = GX.M;
  if (GX.srcId === "all") return M.r ? M.repos[M.r[i]] : "";
  return GX.srcId;
}
function parseLoc(loc) {
  const m = /^L(\d+)/.exec(String(loc || ""));
  return m ? Math.max(1, parseInt(m[1], 10)) : 0;
}
function parseReadSource(text) {
  // "<file> lines <a>-<b> of <n>:" then "<n>| <text>" rows; "error: …" otherwise.
  const head = /^(.*) lines (\d+)-(\d+) of (\d+):\n?/.exec(text);
  if (!head) return { error: text.trim() };
  const rows = [];
  for (const line of text.slice(head[0].length).split("\n")) {
    const m = /^\s*(\d+)\| ?(.*)$/.exec(line);
    if (m) rows.push({ n: parseInt(m[1], 10), text: m[2] });
  }
  return { file: head[1], start: parseInt(head[2], 10), end: parseInt(head[3], 10), total: parseInt(head[4], 10), rows };
}
function renderSourceSection(box, i) {
  const M = GX.M;
  const file = M.files[M.f[i]];
  const line = parseLoc(M.loc[i]);
  const server = sourceServerFor(i);
  const wrap = el("div", { class: "gx-code-wrap" });
  box.append(el("h4", null, t("gx.code.title"), el("span", { class: "gx-count mono" }, file ? midTrunc(file, 40) : "")), wrap);
  if (!file || !server) { wrap.append(el("p", { class: "gx-empty" }, t("gx.code.nofile"))); return; }
  const auto = el("label", { class: "gx-code-auto" });
  const cb = el("input", { type: "checkbox" }); cb.checked = GX.code.auto;
  cb.addEventListener("change", () => { GX.code.auto = cb.checked; try { localStorage.setItem("gfy-gx-autosrc", cb.checked ? "1" : "0"); } catch {} if (cb.checked) loadSourceInto(wrap.querySelector(".gx-code-body"), i, server, file, line); });
  auto.append(cb, " ", t("gx.code.auto"));
  const body = el("div", { class: "gx-code-body" });
  wrap.append(body, auto);
  if (GX.srcId === "all") wrap.append(el("p", { class: "gx-sub" }, tt("gx.code.hubnote", { repo: serverName(server) })));
  // PDF/Office sidecar nodes cite pages ("p.3"); bundles built with make_viz's
  // --src-dir carry "L<line> (p.3)", older ones (and the hub) only the page.
  const rawLoc = String(M.loc[i] || "");
  if (!line) wrap.append(el("p", { class: "gx-sub" }, /^p\.\d+/.test(rawLoc) ? tt("gx.code.pageonly", { page: rawLoc }) : t("gx.code.noloc")));
  else if (/\(~\)$/.test(rawLoc)) wrap.append(el("p", { class: "gx-sub" }, t("gx.code.approx")));
  // A re-render for the same node (console data refresh, language switch)
  // keeps the range the user paged to instead of snapping back.
  const kept = GX.code.range && GX.code.range.node === i ? { start: GX.code.range.start, end: GX.code.range.end } : null;
  if (GX.code.auto || kept) loadSourceInto(body, i, server, file, line, kept);
  else body.append(el("div", { class: "btns" }, el("button", { class: "ghost mini", onclick: () => loadSourceInto(body, i, server, file, line) }, t("gx.code.view"))));
}
async function loadSourceInto(body, i, server, file, line, range) {
  const start = range ? range.start : Math.max(1, line ? line - 8 : 1);
  const end = range ? range.end : start + CODE_WINDOW - 1;
  const seq = ++GX.code.seq;
  GX.code.range = { node: i, start, end };
  body.replaceChildren(el("p", { class: "gx-empty" }, t("gx.code.loading")));
  const cacheKey = `${server}|${file}|${start}|${end}`;
  try {
    let text = GX.code.cache.get(cacheKey);
    if (text == null) {
      const out = await api("POST", `/repos/${encodeURIComponent(server)}/source`, { file, start_line: start, end_line: end });
      text = String(out.text || "");
      GX.code.cache.set(cacheKey, text);
      while (GX.code.cache.size > 60) GX.code.cache.delete(GX.code.cache.keys().next().value);
    }
    if (seq !== GX.code.seq) return;
    const parsed = parseReadSource(text);
    body.replaceChildren();
    if (parsed.error) {
      const unavailable = /snapshot|unavailable/i.test(parsed.error);
      body.append(el("p", { class: "gx-empty" }, unavailable ? t("gx.code.unavailable") : `${t("gx.code.err")}: ${clean(parsed.error, 300)}`));
      return;
    }
    GX.code.range = { node: i, start: parsed.start, end: parsed.end };
    const pre = el("pre", { class: "gx-code" });
    for (const r of parsed.rows) {
      // NB: not "row" — index.html's global .row (form rows, flex-wrap) would apply.
      pre.append(el("div", { class: `gx-cl${r.n === line ? " hl" : ""}` }, el("span", { class: "ln" }, String(r.n)), el("span", { class: "tx" }, r.text || " ")));
    }
    const up = el("button", { class: "ghost mini", onclick: () => loadSourceInto(body, i, server, file, line, { start: Math.max(1, parsed.start - CODE_WINDOW), end: parsed.end }) }, t("gx.code.up"));
    const down = el("button", { class: "ghost mini", onclick: () => loadSourceInto(body, i, server, file, line, { start: parsed.start, end: Math.min(parsed.total, parsed.end + CODE_WINDOW) }) }, t("gx.code.down"));
    up.disabled = parsed.start <= 1; down.disabled = parsed.end >= parsed.total;
    const nav = el("div", { class: "gx-code-nav" },
      el("span", { class: "gx-sub" }, tt("gx.code.lines", { a: parsed.start, b: parsed.end, n: parsed.total })),
      el("span", { style: "flex:1" }), up, down,
      el("button", { class: "ghost mini", onclick: () => { copyText(parsed.rows.map((r) => r.text).join("\n")); flash("ok", t("gx.code.copied")); } }, t("gx.code.copy")));
    body.append(nav, pre);
    const hl = pre.querySelector(".hl"); if (hl && !range) hl.scrollIntoView({ block: "center" });
  } catch (e) {
    if (seq !== GX.code.seq) return;
    const msg = e && e.message ? e.message : String(e);
    body.replaceChildren(el("p", { class: "gx-empty" }, /no such repo|forbidden|403/i.test(msg) ? t("gx.code.noaccess") : `${t("gx.code.err")}: ${clean(msg, 200)}`),
      el("div", { class: "btns" }, el("button", { class: "ghost mini", onclick: () => loadSourceInto(body, i, server, file, line, range) }, t("gx.retry"))));
  }
}
function copyContext(i) {
  const M = GX.M, lines = [];
  const file = M.files[M.f[i]];
  lines.push(`### ${M.label[i]}`, `- source: ${file}${M.loc[i] ? ":" + M.loc[i] : ""}`, `- id: ${M.id[i]}`, `- type: ${M.types[M.t[i]]}${M.kinds[M.k[i]] ? " / " + M.kinds[M.k[i]] : ""}`);
  const cm = M.commByCid.get(M.c[i]); if (cm) lines.push(`- community: ${communityLabel(cm)}`);
  const groups = new Map();
  for (let p = M.adj.off[i]; p < M.adj.off[i + 1]; p++) {
    const rel = M.relations[M.er[M.adj.eid[p]]] || "related", dir = M.adj.dir[p] ? "→" : "←";
    const k = `${dir} ${rel}`; if (!groups.has(k)) groups.set(k, []);
    const v = M.adj.nb[p]; groups.get(k).push(`${M.label[v]} (${M.files[M.f[v]]}${M.loc[v] ? ":" + M.loc[v] : ""})`);
  }
  for (const [k, arr] of groups) { lines.push(`- ${k} (${arr.length}):`); for (const s of arr.slice(0, 30)) lines.push(`  - ${s}`); if (arr.length > 30) lines.push(`  - … +${arr.length - 30}`); }
  copyText(lines.join("\n")); flash("ok", t("gx.ctx.ok"));
}
function askPlayground(i) {
  const M = GX.M;
  const ps = $("play-server"); if (ps && [...ps.options].some((o) => o.value === GX.srcId)) ps.value = GX.srcId;
  const ci = $("chat-input"); if (ci) ci.value = tt("gx.ask.prompt", { label: M.label[i], file: M.files[M.f[i]] || "" });
  switchTab("play"); if (ci) ci.focus();
}

/* ---------------- search ---------------- */
let searchActive = -1, searchHits = [];
function buildSearchIndex() {
  const M = GX.M;
  M.search = { label: M.label.map((s) => s.toLowerCase()), file: M.files.map((s) => s.toLowerCase()) };
}
function runSearch(q) {
  const box = $("gx-search-results"); box.replaceChildren(); searchActive = -1; searchHits = [];
  const M = GX.M; q = (q || "").trim().toLowerCase();
  if (!M || q.length < 1) { box.hidden = true; return; }
  if (!M.search) buildSearchIndex();
  const hits = [];
  for (let i = 0; i < M.n; i++) {
    const l = M.search.label[i]; let score = -1;
    if (l === q) score = 0; else if (l.startsWith(q)) score = 1; else if (l.includes(q)) score = 2;
    else if (M.search.file[M.f[i]].includes(q)) score = 3;
    if (score >= 0) hits.push({ i, score });
  }
  hits.sort((a, b) => a.score - b.score || M.deg[b.i] - M.deg[a.i]);
  searchHits = hits.slice(0, 40).map((h) => h.i);
  for (const i of searchHits) {
    const row = clickable(el("div", { class: "gx-sr", onclick: () => pickSearch(i) }));
    row.append(
      el("span", { class: "sw", style: `background:${colorOfCat(catOfNode(i))}` }),
      el("span", { class: "lb" }, midTrunc(M.label[i], 60)),
      el("span", { class: "meta" }, `${midTrunc(M.files[M.f[i]], 34)}${M.loc[i] ? ":" + M.loc[i] : ""}, ${M.deg[i]}`));
    box.append(row);
  }
  if (hits.length > searchHits.length) box.append(el("div", { class: "gx-sr", style: "color:var(--muted); cursor:default;" }, `+${fmtN(hits.length - searchHits.length)}`));
  box.hidden = !searchHits.length;
}
function pickSearch(i) {
  $("gx-search-results").hidden = true; $("gx-search").value = "";
  revealNode(i);
}
function searchKey(e) {
  const box = $("gx-search-results");
  if (box.hidden) { if (e.key === "Escape") e.target.blur(); return; }
  const rows = box.querySelectorAll(".gx-sr");
  if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    e.preventDefault();
    searchActive = Math.max(0, Math.min(searchHits.length - 1, searchActive + (e.key === "ArrowDown" ? 1 : -1)));
    rows.forEach((r, k) => r.classList.toggle("active", k === searchActive));
    rows[searchActive] && rows[searchActive].scrollIntoView({ block: "nearest" });
  } else if (e.key === "Enter" && !e.isComposing) { e.preventDefault(); pickSearch(searchHits[Math.max(0, searchActive)]); }
  else if (e.key === "Escape") { box.hidden = true; e.target.blur(); }
}

/* ---------------- export / fullscreen / keyboard ---------------- */
function exportPng() {
  const r = GX.renderer; if (!r) return;
  r.once("afterRender", () => {
    const cs = r.getCanvases(); const { width, height } = r.getDimensions(); const dpr = window.devicePixelRatio || 1;
    const out = document.createElement("canvas"); out.width = Math.round(width * dpr); out.height = Math.round(height * dpr);
    const ctx = out.getContext("2d"); ctx.fillStyle = "#FBFCFE"; ctx.fillRect(0, 0, out.width, out.height);
    for (const id of ["edges", "edgeLabels", "nodes", "labels", "hovers", "hoverNodes"]) { const cv = cs[id]; if (cv) ctx.drawImage(cv, 0, 0, cv.width, cv.height, 0, 0, out.width, out.height); }
    // caption strip
    ctx.fillStyle = "rgba(15,23,42,0.75)"; ctx.font = `${12 * dpr}px sans-serif`;
    const noun = GX.isGroupView ? t(GX.M.groupKind === "repo" ? "gx.view.groups.repo" : "gx.view.groups") : GX.isCommunityView ? t("gx.st.communities") : t("gx.st.nodes");
    ctx.fillText(`${serverName(GX.srcId)}, ${noun} ${fmtN(GX.graph.order)}, graphify`, 12 * dpr, out.height - 12 * dpr);
    out.toBlob((blob) => {
      if (!blob) return;
      const a = document.createElement("a"); a.href = URL.createObjectURL(blob);
      a.download = `${safeFilename(GX.srcId)}-graph.png`; document.body.append(a); a.click();
      setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
      flash("ok", t("gx.png.ok"));
    }, "image/png");
  });
  r.refresh();
}
function toggleFullscreen(force) {
  const gx = $("gx");
  const on = force != null ? force : !gx.classList.contains("fullscreen");
  gx.classList.toggle("fullscreen", on);
  document.body.style.overflow = on ? "hidden" : "";
  requestAnimationFrame(() => { if (GX.renderer) { GX.renderer.resize(); GX.renderer.refresh(); } });
}
function onKey(e) {
  if (!GX.shown || $("page-graph").hidden) return;
  const tag = (e.target.tagName || "").toLowerCase();
  const typing = tag === "input" || tag === "select" || tag === "textarea";
  if (e.metaKey || e.ctrlKey) return;   // never shadow browser chords (⌘F, ⌘L, Ctrl+−…)
  if (e.key === "/" && !typing) { e.preventDefault(); $("gx-search").focus(); return; }
  if (typing) return;
  if (e.key === "Escape") {
    if ($("gx").classList.contains("fullscreen")) return toggleFullscreen(false);
    if (GX.selected) return setSelected(null);
    return goBack();
  }
  if (!GX.renderer) return;
  if (e.key === "f" || e.key === "F") fitView();
  else if (e.key === "l" || e.key === "L") toggleLabels();
  else if (e.key === "+" || e.key === "=") GX.renderer.getCamera().animatedZoom(anim(200));
  else if (e.key === "-" || e.key === "_") GX.renderer.getCamera().animatedUnzoom(anim(200));
  else if (e.key === "ArrowLeft" && e.altKey) goBack();
}
function toggleLabels() {
  GX.labelsOn = !GX.labelsOn;
  $("gx-labels").setAttribute("aria-pressed", String(GX.labelsOn));
  if (GX.renderer) { GX.renderer.setSetting("renderLabels", GX.labelsOn); GX.renderer.setSetting("renderEdgeLabels", GX.isGroupView && GX.labelsOn); GX.renderer.refresh(); }
}

/* ---------------- source picker + boot ---------------- */
function renderSourcePicker() {
  const sel = $("gx-source"); const cur = sel.value || GX.srcId;
  sel.replaceChildren();
  const servers = (S.servers || []).filter((s) => s.kind === "repo");
  const mine = new Set(servers.map((s) => s.server_id));
  sel.append(el("option", { value: "all", "data-sub": "all" }, t("gx.src.hub")));
  if (servers.length) {
    const og = el("optgroup", { label: t("gx.src.mine") });
    for (const s of servers.slice().sort((a, b) => serverName(a.server_id).localeCompare(serverName(b.server_id)))) {
      og.append(el("option", { value: s.server_id, "data-sub": s.server_id }, serverName(s.server_id)));
    }
    sel.append(og);
  }
  const cat = (S.catalog || []).filter((c) => !mine.has(c.repo_id) && String(c.status).toUpperCase() === "READY");
  if (cat.length) {
    const og = el("optgroup", { label: t("gx.src.catalog") });
    for (const c of cat) og.append(el("option", { value: c.repo_id, "data-sub": c.repo_id }, serverName(c.repo_id)));
    sel.append(og);
  }
  // The loaded source must stay selectable even if it left the list (e.g. a
  // catalog row that was just subscribed, or a source removed mid-session).
  if (cur && ![...sel.options].some((o) => o.value === cur)) sel.append(el("option", { value: cur }, cur));
  if (cur) sel.value = cur;
}
function wire() {
  $("gx-load").onclick = () => loadSource($("gx-source").value, { force: true });
  $("gx-source").addEventListener("change", () => loadSource($("gx-source").value));
  $("gx-view").querySelectorAll("button").forEach((b) => {
    b.onclick = () => {
      if (!GX.M) return;
      // Keep a folder scope when switching level; other scopes reset to all.
      const sc = GX.view.scope.kind === "dir" && b.dataset.view !== "groups" ? GX.view.scope : { kind: "all" };
      setView({ mode: b.dataset.view, scope: sc });
    };
  });
  $("gx-simple").addEventListener("change", (e) => { GX.simple = e.target.checked; applyView({ fit: false }); });
  $("gx-color").addEventListener("change", (e) => {
    GX.colorBy = e.target.value; GX.filters.legend = new Set(); legendLimit = 60;
    try { localStorage.setItem("gfy-gx-color", GX.colorBy); } catch {}
    applyView({ fit: false });
  });
  try { const c = localStorage.getItem("gfy-gx-color"); if (c && ["community", "type", "dir"].includes(c)) { GX.colorBy = c; $("gx-color").value = c; } } catch {}
  try { GX.code.auto = localStorage.getItem("gfy-gx-autosrc") !== "0"; } catch {}
  $("gx-mindeg").addEventListener("input", (e) => { GX.filters.minDeg = Number(e.target.value) || 0; $("gx-mindeg-val").textContent = e.target.value; });
  $("gx-mindeg").addEventListener("change", () => applyView({ fit: false }));
  $("gx-inferred").addEventListener("change", (e) => { GX.filters.inferred = e.target.checked; applyView({ fit: false }); });
  $("gx-legend-filter").addEventListener("input", renderLegend);
  $("gx-search").addEventListener("input", (e) => runSearch(e.target.value));
  $("gx-search").addEventListener("keydown", searchKey);
  $("gx-search").addEventListener("focus", (e) => { if (e.target.value) runSearch(e.target.value); });
  document.addEventListener("click", (e) => { if (!e.target.closest(".gx-tb-search")) $("gx-search-results").hidden = true; });
  $("gx-fit").onclick = () => fitView();
  $("gx-zoom-in").onclick = () => GX.renderer && GX.renderer.getCamera().animatedZoom(anim(200));
  $("gx-zoom-out").onclick = () => GX.renderer && GX.renderer.getCamera().animatedUnzoom(anim(200));
  $("gx-labels").onclick = toggleLabels;
  $("gx-png").onclick = exportPng;
  $("gx-full").onclick = () => toggleFullscreen();
  document.addEventListener("keydown", onKey);
  window.addEventListener("resize", () => { if (GX.renderer && GX.shown) GX.renderer.resize(); });
  renderInspector();
}
let wired = false;
const GraphExplorer = {
  onShow() {
    GX.shown = true;
    if (!wired) { wired = true; wire(); }
    renderSourcePicker();
    if (!libsReady()) { showOverlay("error", GX.libsError === "webgl" ? t("gx.err.webgl") : t("gx.err.libs")); return; }
    if (GX.renderer) requestAnimationFrame(() => { GX.renderer.resize(); GX.renderer.refresh(); });
    if (!GX.M && !GX.loading) {
      showOverlay("card", t("gx.empty.title"), t("gx.empty.body"));
      // Deferred so an open(serverId) issued in the same tick (openGraph from
      // a source row) wins instead of racing a second download.
      setTimeout(() => {
        if (GX.M || GX.loading) return;
        let want = "";
        const m = /^#graph\/([^/]+)/.exec(location.hash || "");
        if (m) { try { want = decodeURIComponent(m[1]); } catch {} }
        if (!want) { try { want = sessionStorage.getItem("gfy-gx-src") || ""; } catch {} }
        const sel = $("gx-source");
        if (want && [...sel.options].some((o) => o.value === want)) loadSource(want);
      }, 0);
    }
  },
  onHide() { GX.shown = false; if ($("gx").classList.contains("fullscreen")) toggleFullscreen(false); },
  onData() {
    if (!wired) return;
    renderSourcePicker();
    // render() runs on every console data refresh (12 s build polling, tab
    // actions) — only a LANGUAGE change warrants re-labelling the whole view;
    // otherwise the inspector (and a paged source window) must stay put.
    const lang = typeof LANG === "undefined" ? "ko" : LANG;
    if (GX.M && GX.renderer && lang !== GX.lang) applyView({ fit: false });
    GX.lang = lang;
  },
  open(serverId) {
    if (!wired) { wired = true; wire(); }
    renderSourcePicker();
    const sel = $("gx-source");
    if (![...sel.options].some((o) => o.value === serverId)) sel.append(el("option", { value: serverId }, serverId));
    sel.value = serverId;
    if (GX.srcId !== serverId || !GX.M) loadSource(serverId);
  },
};
// Debug/test hook (state only; presigned URLs are dropped after use).
GraphExplorer._state = () => GX;
GraphExplorer._act = { setView, drillCommunity, drillGroup, focusEgo, revealNode, setSelected, setPathEndpoint, clearPath, goBack, fitView, applyView, K, CK };
window.GraphExplorer = GraphExplorer;
})();
