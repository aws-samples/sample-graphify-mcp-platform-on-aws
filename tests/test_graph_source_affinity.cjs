"use strict";

// Offline VM hooks into the real graph explorer; no browser, network or AWS.
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");

const graphPath = path.resolve(__dirname, "../console/graph.js");
const code = readFileSync(graphPath, "utf8");
const anchor = "window.GraphExplorer = GraphExplorer;";
assert.equal(code.split(anchor).length, 2);
const script = new vm.Script(code.replace(anchor, `
window.affinityTest = {
  GX, fromRaw, nodeSourceId, sourceColor, computeColorRank, connectedSources,
  nodeRow, renderNodeInspector, renderConnectedSources, renderLegend,
  buildNodeGraph, nodeReducer, edgeReducer, fitNodeLabel, drawBoundedNodeLabel,
  sigmaSettings, copyContext, sourceTargetsFor, PALETTE, GREY, EDGE_COLOR,
  EDGE_COLOR_INFERRED, PATH_COLOR
}; ${anchor}`), { filename: graphPath });
const plain = (value) => JSON.parse(JSON.stringify(value));

class Element {
  constructor(tag, attrs, ...children) {
    this.tagName = tag;
    this.children = [];
    Object.assign(this, attrs || {});
    this.append(...children);
  }
  append(...children) {
    for (const child of children) {
      if (child == null) continue;
      this.children.push(child);
      if (child instanceof Element) child.parent = this;
    }
  }
  replaceChildren(...children) { this.children = []; this.append(...children); }
  remove() { this.parent.children = this.parent.children.filter(child => child !== this); }
  setAttribute(name, value) { this[name] = String(value); }
  addEventListener() {}
  get textContent() { return this.children.map(child => child instanceof Element ? child.textContent : child).join(""); }
  set textContent(text) { this.children = [text]; }
}
function elements(root, predicate) {
  return [root, ...root.children.flatMap(child => child instanceof Element ? elements(child, () => true) : [])].filter(predicate);
}
const withClass = (root, name) => elements(root, item => String(item.class || "").split(" ").includes(name));

class Graph {
  import({ nodes, edges }) {
    this.nodeMap = new Map(nodes.map(node => [node.key, node.attributes]));
    this.edgeMap = new Map(edges.map(edge => [edge.key, edge]));
  }
  hasNode(key) { return this.nodeMap.has(key); }
  hasEdge(key) { return this.edgeMap.has(key); }
  hasExtremity(key, node) { const e = this.edgeMap.get(key); return e.source === node || e.target === node; }
  source(key) { return this.edgeMap.get(key).source; }
  getNodeAttributes(key) { return this.nodeMap.get(key); }
  getEdgeAttributes(key) { return this.edgeMap.get(key).attributes; }
}
function harness(lang = "en") {
  const dom = new Map(), I18N = { en: {}, ko: {} }, copied = [];
  const forbidden = () => { throw new Error("No network calls are allowed in this test"); };
  const context = vm.createContext({
    window: {}, graphology: { MultiDirectedGraph: Graph }, LANG: lang, I18N,
    S: { repos: [{ repo_id: "api", server_name: "Orders API" }], groups: [], servers: [] },
    t: key => I18N[lang][key] || key, serverName: id => id,
    document: {
      activeElement: null,
      getElementById(id) {
        if (!dom.has(id)) dom.set(id, new Element("div", { id, value: "" }));
        return dom.get(id);
      },
    },
    el: (tag, attrs, ...children) => new Element(tag, attrs, ...children),
    copyText: text => copied.push(text), flash() {}, console,
    api: forbidden, fetch: forbidden, URL, URLSearchParams, TextDecoder, TextEncoder,
    AbortController, DOMException, Blob, setTimeout, clearTimeout,
  });
  script.runInContext(context);
  const hooks = context.window.affinityTest;
  hooks.GX.code.auto = false;
  hooks.GX.isGroupView = false;
  hooks.GX.view = { mode: "full", scope: { kind: "all" } };
  return { ...hooks, dom, context, I18N, copied };
}

function node(id, source, extra = {}) {
  return {
    id, label: "submit", source_id: source, source_version: `${source}-v1`,
    source_file: "src/shared.ts", source_location: "L2", file_type: "code", node_kind: "function",
    ...extra,
  };
}
function edge(source, target, extra = {}) {
  return { source, target, relation: "calls", evidence_kind: "EXTRACTED", ...extra };
}
function fixture() {
  return {
    nodes: [
      node("api:0", "api"), node("api:1", "api"), node("ui:0", "ui"),
      node("ui:1", "ui"), node("docs:0", "docs"), node("isolated:0", "isolated"),
    ],
    links: [
      edge("api:0", "api:1"),
      edge("api:0", "ui:0"),
      edge("api:0", "ui:0", { evidence_kind: "INFERRED" }),
      edge("ui:0", "api:0"),
      edge("api:0", "ui:1", { confidence: "INFERRED" }),
      edge("api:0", "api:0"),
      edge("api:0", "api:0", { evidence_kind: "INFERRED" }),
      edge("ui:1", "docs:0"), // two hops away, never a direct source of api:0
    ],
  };
}
function groupModel(h, raw = fixture(), overrides = {}) {
  const ids = [...new Set(raw.nodes.map(row => row.source_id))];
  const M = h.fromRaw(raw, "grp_orders", {
    groupId: "grp_orders", groupVersion: "group-v1",
    groupSources: ids.map(source_id => ({ source_id, name: source_id === "ui" ? "Store UI" : source_id })),
    sourceVersions: new Map(ids.map(id => [id, `${id}-v1`])), ...overrides,
  });
  h.GX.M = M;
  h.GX.srcId = M.srcId;
  h.GX.colorBy = "repo";
  h.computeColorRank();
  return M;
}
function drawContext() {
  const calls = [];
  return {
    calls, canvas: { clientWidth: 320, clientHeight: 200 },
    measureText: text => ({ width: Array.from(text).length * 6, actualBoundingBoxAscent: 9, actualBoundingBoxDescent: 3 }),
    save() {}, restore() {}, beginPath() {}, arc() {}, fill() {},
    fillRect(...args) { calls.push(["box", ...args]); },
    fillText(...args) { calls.push(["text", ...args]); }, strokeText() {},
  };
}

test("group affiliation uses origin IDs, not duplicate labels, files or friendly names", () => {
  const h = harness(), M = groupModel(h);
  assert.equal(M.label[0], M.label[2]);
  assert.equal(M.files[M.f[0]], M.files[M.f[2]]);
  assert.equal(h.nodeSourceId(0), "api");
  assert.equal(h.nodeSourceId(2), "ui");
  const a = h.nodeRow(0), b = h.nodeRow(2);
  assert.match(a.textContent, /Orders API/);
  assert.match(b.textContent, /Store UI/);
  assert.match(a["aria-label"], /api/);
  assert.match(b["aria-label"], /ui/);
  assert.equal(a.role, "button");
  assert.equal(a.tabIndex, 0);
  assert.notEqual(withClass(a, "gx-source-swatch")[0].style, withClass(b, "gx-source-swatch")[0].style);
  // Even identical source display names retain distinct raw IDs for access.
  h.context.S.repos.push({ repo_id: "ui", server_name: "Orders API" });
  assert.notEqual(h.nodeRow(0)["aria-label"], h.nodeRow(2)["aria-label"]);
});

test("source badges match repo colors and stay stable across filtering and color modes", () => {
  const h = harness(), M = groupModel(h);
  const colors = M.repos.map(id => h.sourceColor(id));
  for (let i = 0; i < M.n; i++) assert.equal(h.sourceColor(h.nodeSourceId(i)), h.GX.colorRank.get(M.r[i]));
  h.GX.filters.repos.add(0);
  h.GX.filters.minDeg = 99;
  h.GX.colorBy = "type";
  h.computeColorRank();
  assert.deepEqual(M.repos.map(id => h.sourceColor(id)), colors);
  h.GX.colorBy = "community";
  h.computeColorRank();
  assert.deepEqual(M.repos.map(id => h.sourceColor(id)), colors);
});

test("connected sources count unique direct neighbors and real parallel edges, excluding transitive sources", () => {
  const h = harness();
  groupModel(h);
  assert.deepEqual(plain(h.connectedSources(0)), [
    { sourceId: "ui", edges: 4, incoming: 1, outgoing: 3, inferred: 2, selfLoops: 0, neighborCount: 2 },
    { sourceId: "api", edges: 3, incoming: 2, outgoing: 3, inferred: 1, selfLoops: 2, neighborCount: 1 },
  ]);
  h.GX.originSource = "api";
  h.GX.filters.repos.add(1);
  h.GX.filters.inferred = false;
  h.GX.filters.relations.add(0);
  h.GX.simple = true;
  h.GX.view.scope = { kind: "ego", node: 5, hops: 1 };
  assert.equal(h.connectedSources(0)[0].edges, 4, "counts do not use visible filters or the selected scope");
  assert.equal(h.connectedSources(0)[0].neighborCount, 2);
  assert.deepEqual(plain(h.connectedSources(5)), []);
});

test("inspector promotes origin and explains whole-graph, direct-edge and self-loop count semantics", () => {
  const h = harness(), raw = fixture(), box = new Element("div");
  raw.links.push(edge("api:0", "docs:0"), edge("isolated:0", "api:0"));
  const M = groupModel(h, raw);
  h.renderNodeInspector(box, 0);
  assert.equal(box.children[0].textContent, "submit");
  assert.equal(box.children[1].class, "gx-origin-card");
  assert.match(box.children[1].textContent, /Origin sourceOrders API/);
  const pillsIndex = box.children.findIndex(child => child.class === "gx-pills");
  const summaryIndex = box.children.findIndex(child => child.class === "gx-source-connections");
  const metadataIndex = box.children.findIndex(child => child.class === "gx-kv");
  assert.equal(box.children[pillsIndex + 1].textContent, "Connected sources4");
  assert.ok(summaryIndex > pillsIndex && summaryIndex < metadataIndex, "source cards precede files, IDs and version metadata");
  assert.equal(withClass(box.children[summaryIndex], "gx-source-connection").length, 4);
  assert.ok(box.textContent.includes("Source IDapi"));
  assert.ok(box.textContent.includes("group-v1"));
  assert.ok(box.textContent.includes(M.provenance[0].source_version));
  assert.match(box.textContent, /full loaded group graph.*hidden by the current view or filters/);
  assert.match(box.textContent, /2 unique neighbors, 4 edges/);
  assert.match(box.textContent, /out 3, in 1, inferred 2/);
  assert.match(box.textContent, /Each self-loop counts as one edge and once in each direction/);
  const rows = withClass(box, "gx-nb-source");
  assert.ok(rows.length > 0 && rows.every(row => withClass(row, "gx-source-badge").length === 1));
  assert.ok(rows.every(row => row.title.includes("Origin source:")));
});

test("isolated and self-loop-only nodes have accurate summaries", () => {
  const h = harness();
  groupModel(h, { nodes: [node("api:0", "api"), node("ui:0", "ui")], links: [edge("api:0", "api:0")] });
  assert.equal(h.connectedSources(0)[0].neighborCount, 0);
  assert.equal(h.connectedSources(0)[0].edges, 1);
  const box = new Element("div");
  h.renderConnectedSources(box, 1);
  assert.equal(withClass(box, "gx-source-connection").length, 0);
  assert.match(box.textContent, /No direct connections/);
});

test("many-source summaries start at eight rows and the palette overflow keeps the existing gray fallback", () => {
  const h = harness(), ids = Array.from({ length: 20 }, (_, i) => `source${i}`);
  const raw = { nodes: ids.map(id => node(id, id)), links: ids.slice(1).map(id => edge(ids[0], id)) };
  const M = groupModel(h, raw), box = new Element("div");
  h.renderConnectedSources(box, 0);
  assert.equal(withClass(box, "gx-source-connection").length, 8);
  const more = elements(box, item => item.tagName === "button")[0];
  assert.equal(more.textContent, "Show 11 more");
  more.onclick({ currentTarget: more });
  assert.equal(withClass(box, "gx-source-connection").length, 19);
  assert.equal(elements(box, item => item.tagName === "button").length, 0);
  assert.equal(h.sourceColor(ids.at(-1)), h.GREY);
  assert.equal(h.sourceColor(ids.at(-1)), h.GX.colorRank.get(M.r.at(-1)));
});

test("only the active focus label exposes origin, with unchanged neighbor/path labels and label budgets", () => {
  const h = harness(), M = groupModel(h), g = h.buildNodeGraph();
  const attrs = g.getNodeAttributes("0");
  assert.equal(h.nodeReducer("0", attrs), attrs, "idle nodes keep their original canvas label");
  h.GX.selected = "0";
  h.GX.selectedNbrs = new Set(["1", "2", "3"]);
  const selected = h.nodeReducer("0", attrs);
  assert.equal(selected.label, "Orders API: submit");
  assert.equal(selected.forceLabel, true);
  assert.equal(h.nodeReducer("2", g.getNodeAttributes("2")).label, g.getNodeAttributes("2").label);
  assert.equal(h.nodeReducer("2", g.getNodeAttributes("2")).forceLabel, true);
  assert.equal(h.nodeReducer("4", g.getNodeAttributes("4")).label, null);
  h.GX.selectedNbrs = new Set(Array.from({ length: 70 }, (_, i) => String(i + 1)));
  assert.equal(h.nodeReducer("2", g.getNodeAttributes("2")).forceLabel, false, "large neighborhoods do not force all labels");
  assert.equal(h.nodeReducer("2", g.getNodeAttributes("2")).label, g.getNodeAttributes("2").label);
  h.GX.hovered = "2"; h.GX.hoveredNbrs = new Set(["0"]);
  const hovered = h.nodeReducer("2", g.getNodeAttributes("2"));
  assert.equal(hovered.label, "Store UI: submit");
  h.GX.path.nodes = new Set(["0", "2"]);
  assert.equal(h.nodeReducer("0", attrs).color, h.PATH_COLOR);
  assert.equal(h.nodeReducer("0", attrs).label, attrs.label, "hover takes priority over selection, including on paths");
  assert.equal(h.nodeReducer("2", g.getNodeAttributes("2")).label, "Store UI: submit");
  h.GX.hovered = null; h.GX.hoveredNbrs = null;
  assert.equal(h.nodeReducer("0", attrs).label, "Orders API: submit", "selection regains the prefix after hover ends");
  assert.equal(h.nodeReducer("2", g.getNodeAttributes("2")).label, g.getNodeAttributes("2").label);
  h.GX.selected = null; h.GX.selectedNbrs = null;
  for (const key of h.GX.path.nodes) {
    const reduced = h.nodeReducer(key, g.getNodeAttributes(key));
    assert.equal(reduced.label, g.getNodeAttributes(key).label, "unfocused paths use ordinary display labels");
    assert.equal(reduced.forceLabel, true, "existing path label behavior is preserved");
  }
  const settings = h.sigmaSettings(6000, 9000);
  assert.equal(settings.labelDensity, 0.4);
  assert.equal(settings.labelGridCellSize, 140);
  const ctx = drawContext();
  settings.defaultDrawNodeHover(ctx, { ...hovered, x: 315, y: 199 }, settings);
  const text = ctx.calls.find(call => call[0] === "text");
  assert.equal(text[1], "Store UI: submit");
  const [, , x, y] = text;
  assert.ok(x >= 8 && x + ctx.measureText(text[1]).width <= 312 && y <= 192);
  assert.equal(attrs.label, "submit");
  assert.equal(M.label[0], "submit");
  assert.equal(h.nodeReducer("c:0", { label: "Community", cid: 0, size: 10 }).label, null);
});

test("long or HTML-like source names stay text, wrap in badges, and clip only in canvas presentation", () => {
  const h = harness(), name = '<img src=x onerror="boom">긴소스이름'.repeat(20);
  const M = groupModel(h, { nodes: [node("custom:0", "custom", { label: "longNode".repeat(20) })], links: [] }, {
    groupSources: [{ source_id: "custom", name }],
  });
  const row = h.nodeRow(0), badge = withClass(row, "gx-source-badge")[0];
  assert.equal(withClass(badge, "gx-source-name")[0].textContent, name);
  assert.equal(elements(badge, item => item.tagName === "img").length, 0);
  assert.ok(row["aria-label"].includes(name));
  h.GX.selected = "0";
  const data = h.nodeReducer("0", { idx: 0, label: M.label[0], x: 80, y: 60, size: 10 });
  const fit = h.fitNodeLabel(drawContext(), data, h.sigmaSettings(1, 0), { width: 180, height: 120 });
  assert.ok(fit.truncated && fit.width <= 164 && fit.x >= 8);
  assert.equal(M.groupSources[0].name, name);
  assert.equal(M.label[0], "longNode".repeat(20));
  const css = readFileSync(path.resolve(__dirname, "../console/graph.css"), "utf8");
  assert.match(css, /\.gx-source-name\s*\{[^}]*overflow-wrap:\s*anywhere/);
  assert.match(css, /\.gx-source-badge\s*\{[^}]*max-width:\s*100%[^}]*white-space:\s*normal/s);
});

test("cross-source arrows reuse origin colors, preserve inference, and do not invent topology", () => {
  const h = harness(), raw = fixture(), M = groupModel(h, raw), g = h.buildNodeGraph();
  h.GX.graph = g;
  assert.equal(g.nodeMap.size, M.n);
  assert.equal(g.edgeMap.size, raw.links.length);
  assert.equal(g.getEdgeAttributes("0").color, h.EDGE_COLOR);
  assert.equal(g.getEdgeAttributes("1").color, h.sourceColor("api") + "6B");
  assert.equal(g.getEdgeAttributes("2").color, h.sourceColor("api") + "2E");
  assert.equal(g.getEdgeAttributes("3").color, h.sourceColor("ui") + "6B");
  assert.ok(g.getEdgeAttributes("2").size < g.getEdgeAttributes("1").size);
  assert.equal(g.getEdgeAttributes("5").color, h.EDGE_COLOR, "self-loops stay within-source");
  assert.equal(g.getEdgeAttributes("6").color, h.EDGE_COLOR_INFERRED);
  for (const [key, value] of g.edgeMap) {
    assert.equal(M.id[Number(value.source)], raw.links[Number(key)].source);
    assert.equal(M.id[Number(value.target)], raw.links[Number(key)].target);
  }
  h.GX.filters.inferred = false;
  const filtered = h.buildNodeGraph();
  assert.equal(filtered.edgeMap.size, 5);
  assert.ok(!filtered.hasEdge("2") && !filtered.hasEdge("4") && !filtered.hasEdge("6"));
  h.GX.filters.inferred = true;
  h.GX.originSource = "api";
  const sourceOnly = h.buildNodeGraph();
  assert.equal(sourceOnly.nodeMap.size, 2);
  assert.equal(sourceOnly.edgeMap.size, 3);
});

test("selection and path highlights take priority and keep group inferred edges lighter and thinner", () => {
  const h = harness();
  groupModel(h);
  h.GX.graph = h.buildNodeGraph();
  const extracted = h.GX.graph.getEdgeAttributes("1"), inferred = h.GX.graph.getEdgeAttributes("2");
  assert.equal(h.edgeReducer("1", extracted), extracted);
  h.GX.selected = "0";
  assert.equal(h.edgeReducer("1", extracted).color, "rgba(79, 70, 229, 0.75)");
  assert.equal(h.edgeReducer("3", h.GX.graph.getEdgeAttributes("3")).color, "rgba(124, 58, 237, 0.75)");
  assert.equal(h.edgeReducer("2", inferred).color, "rgba(79, 70, 229, 0.38)");
  assert.ok(h.edgeReducer("2", inferred).size < h.edgeReducer("1", extracted).size);
  assert.equal(h.edgeReducer("7", h.GX.graph.getEdgeAttributes("7")).hidden, true);
  h.GX.path.edges = new Set(["1", "2"]);
  assert.equal(h.edgeReducer("1", extracted).color, h.PATH_COLOR);
  assert.equal(h.edgeReducer("2", inferred).color, h.PATH_COLOR + "99");
  assert.ok(h.edgeReducer("2", inferred).size < h.edgeReducer("1", extracted).size);
  assert.equal(extracted.color, h.sourceColor("api") + "6B");
});

test("the edge legend is localized and only appears for node-level group graphs", () => {
  for (const lang of ["ko", "en"]) {
    const h = harness(lang);
    groupModel(h);
    h.renderLegend();
    const legend = h.dom.get("gx-legend");
    assert.equal(withClass(legend, "gx-edge-legend").length, 1);
    assert.ok(legend.textContent.includes(h.I18N[lang]["gx.group.edges.cross"]));
    assert.ok(legend.textContent.includes(h.I18N[lang]["gx.group.edges.same"]));
    assert.ok(legend.textContent.includes(h.I18N[lang]["gx.group.edges.hint"]));
    const box = new Element("div");
    h.renderConnectedSources(box, 0);
    assert.ok(box.textContent.includes(h.I18N[lang]["gx.group.connected.scope"]));
    for (const [key, value] of Object.entries(h.I18N[lang])) {
      if (/gx\.group\.(sourceid|connected|edges)/.test(key)) assert.doesNotMatch(value, /[·—]/);
    }
    h.GX.isGroupView = true; h.renderLegend();
    assert.equal(withClass(legend, "gx-edge-legend").length, 0);
    h.GX.isGroupView = false; h.GX.isCommunityView = true; h.renderLegend();
    assert.equal(withClass(legend, "gx-edge-legend").length, 0);
  }
});

test("ordinary repositories and hub graphs retain their existing labels, edges, inference and source routes", () => {
  for (const srcId of ["single", "all"]) {
    const h = harness(), raw = fixture();
    if (srcId === "all") raw.nodes.forEach(row => { row.repo = row.source_id; });
    const M = h.fromRaw(raw, srcId);
    h.GX.M = M; h.GX.srcId = srcId; h.GX.colorBy = srcId === "all" ? "repo" : "dir";
    h.computeColorRank();
    h.GX.graph = h.buildNodeGraph();
    h.GX.selected = "0";
    assert.equal(h.nodeReducer("0", h.GX.graph.getNodeAttributes("0")).label, "submit");
    assert.equal(h.GX.graph.getEdgeAttributes("1").color, h.EDGE_COLOR);
    assert.equal(h.GX.graph.getEdgeAttributes("2").color, h.EDGE_COLOR, "evidence_kind handling is scoped to group graphs");
    assert.equal(h.GX.graph.getEdgeAttributes("4").color, h.EDGE_COLOR_INFERRED, "ordinary confidence handling is retained");
    assert.equal(h.edgeReducer("4", h.GX.graph.getEdgeAttributes("4")).color, "rgba(79, 70, 229, 0.75)");
    assert.equal(h.edgeReducer("4", h.GX.graph.getEdgeAttributes("4")).size, 1.6);
    assert.equal(withClass(h.nodeRow(0), "gx-source-badge").length, 0);
    assert.equal(h.nodeRow(0)["aria-label"], "submit");
    assert.deepEqual(plain(h.connectedSources(0)), []);
    assert.equal(h.nodeSourceId(0), srcId === "all" ? "api" : "single");
    assert.equal(h.sourceTargetsFor(M, 0)[0].server, srcId === "all" ? "api" : "single");
    h.renderLegend();
    assert.equal(withClass(h.dom.get("gx-legend"), "gx-edge-legend").length, 0);
    const box = new Element("div");
    h.renderNodeInspector(box, 0);
    assert.equal(withClass(box, "gx-origin-card").length, 0);
    assert.equal(withClass(box, "gx-source-connections").length, 0);
  }
});

function snapshot(value) {
  return JSON.stringify(value, (_key, item) => {
    if (ArrayBuffer.isView(item)) return Array.from(item);
    if (Object.prototype.toString.call(item) === "[object Map]") return { map: [...item] };
    if (Object.prototype.toString.call(item) === "[object Set]") return { set: [...item] };
    return item;
  });
}
test("affiliation rendering leaves model, immutable provenance, source paths and copied context untouched", () => {
  const h = harness(), raw = fixture(), M = groupModel(h, raw);
  const before = snapshot(M), rawBefore = snapshot(raw), routeBefore = snapshot(h.sourceTargetsFor(M, 0));
  Object.freeze(M);
  Object.freeze(M.label);
  M.provenance.forEach(Object.freeze);
  h.copyContext(0);
  const originalContext = h.copied.at(-1);
  h.GX.graph = h.buildNodeGraph();
  h.GX.selected = "0";
  h.GX.selectedNbrs = new Set(["1", "2", "3"]);
  h.nodeReducer("0", h.GX.graph.getNodeAttributes("0"));
  h.edgeReducer("1", h.GX.graph.getEdgeAttributes("1"));
  h.renderNodeInspector(new Element("div"), 0);
  h.renderLegend();
  h.copyContext(0);
  assert.equal(h.copied.at(-1), originalContext);
  assert.match(originalContext, /source_id: api/);
  assert.match(originalContext, /src\/shared.ts:L2/);
  assert.equal(snapshot(h.sourceTargetsFor(M, 0)), routeBefore);
  assert.equal(snapshot(M), before);
  assert.equal(snapshot(raw), rawBefore);
});
