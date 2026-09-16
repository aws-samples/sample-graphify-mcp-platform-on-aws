"use strict";

// Run from any directory: node --test tests/test_group_graph_ui.cjs
// Exercise the real IIFE without browser libraries, a DOM, AWS, or dependencies.
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");

const graphPath = path.resolve(__dirname, "../console/graph.js");
const graphSource = readFileSync(graphPath, "utf8");
const exportAnchor = "window.GraphExplorer = GraphExplorer;";
assert.equal(graphSource.split(exportAnchor).length, 2, "expected one final GraphExplorer assignment");
const graphScript = new vm.Script(graphSource.replace(exportAnchor, `
window.__testHooks = {
  GX, fromRaw, loadSource,
  setWiredForTest: (value) => { wired = value; },
  loadGroupGraph: typeof loadGroupGraph === "function" ? loadGroupGraph : undefined,
  sourceTargetsFor: typeof sourceTargetsFor === "function" ? sourceTargetsFor : undefined,
  readSourceResult: typeof readSourceResult === "function" ? readSourceResult : undefined,
};
${exportAnchor}`), { filename: graphPath });

const GID = "grp_0123456789abcdef0123456789abcdef";
const OTHER_GID = "grp_fedcba9876543210fedcba9876543210";
const VERSION = "group-build-3";
const SOURCES = [
  { source_id: "backend", role: "backend", description: "Order API" },
  { source_id: "frontend", role: "frontend", description: "Order UI" },
];
const SOURCE_VERSIONS = { backend: "backend-v2", frontend: "frontend-v7" };
const SEQ = 7;
const clone = (value) => JSON.parse(JSON.stringify(value));

function groupMeta(overrides = {}) {
  return {
    group_id: GID, name: "Orders", status: "READY", data_ready: true,
    revision: 3, active_revision: 3, active_version: VERSION,
    sources: clone(SOURCES), active_source_versions: { ...SOURCE_VERSIONS },
    ...overrides,
  };
}

function modelMeta(overrides = {}) {
  return {
    groupId: GID, groupVersion: VERSION, groupSources: clone(SOURCES),
    sourceVersions: new Map(Object.entries(SOURCE_VERSIONS)), ...overrides,
  };
}

function groupOnlyRepo(overrides = {}) {
  return {
    repo_id: "backend", dedicated_runtime: false, active_source_version: "backend-v2", ...overrides,
  };
}

function node(source = "backend", id = `${source}:submit`, overrides = {}) {
  return {
    id, label: "submit", original_id: "submit", source_id: source,
    source_version: SOURCE_VERSIONS[source], source_file: "src/orders.ts",
    source_location: "L2", file_type: "code", node_kind: "function",
    ...overrides,
  };
}

function graph() {
  return {
    nodes: [node(), node("frontend")],
    links: [{
      id: "backend-to-frontend", source: "backend:submit", target: "frontend:submit",
      relation: "shared_contract", evidence_kind: "EXTRACTED", method: "deterministic",
      evidence: [{ source_id: "backend", source_version: "backend-v2",
        file: "src/orders.ts", line_start: 2, line_end: 2, quote: 'return "서울";' }],
    }],
  };
}

function page(kind, values, next = null, overrides = {}) {
  return {
    group_id: GID, version: VERSION, group_version: VERSION, revision: 3,
    status: "READY", sources: clone(SOURCES), stats: {}, usage: {},
    [kind]: values, next_offset: next, ...overrides,
  };
}

class HarnessError extends Error {}

function smallDom() {
  const elements = new Map();
  const el = (tag, attributes, ...children) => {
    const classes = new Set(String(attributes?.class || "").split(/\s+/).filter(Boolean));
    const element = {
      tagName: tag, style: {}, hidden: false, value: "", textContent: "", children,
      append(...added) { this.children.push(...added); },
      replaceChildren(...replacement) { this.children = replacement; this.textContent = ""; },
      setAttribute(name, value) { this[name] = String(value); },
      removeAttribute(name) { delete this[name]; },
      blur() { this.blurred = true; },
      classList: { contains: (name) => classes.has(name), add: (name) => classes.add(name), remove: (name) => classes.delete(name) },
      ...attributes,
    };
    if (element.id) elements.set(element.id, element);
    return element;
  };
  const getElementById = (id) => elements.get(id) || el("div", { id });
  return { el, getElementById };
}

function harness({ api, state = {}, maxCalls = 600, withDom = false, withLibraries = withDom } = {}) {
  const calls = [];
  const S = { repos: [], servers: [], catalog: [], groups: [groupMeta()], ...state };
  const dom = withDom ? smallDom() : null;
  const timers = new Map();
  const session = new Map(), location = { hash: "" }, body = { style: { overflow: "" } };
  let nextTimer = 0;
  const context = vm.createContext({
    window: withLibraries ? { graphology: {}, Sigma: class {} } : {},
    S, I18N: { en: {}, ko: {} }, LANG: "en",
    t: (key) => key, serverName: (id) => id, console,
    URL, URLSearchParams, TextEncoder, TextDecoder, Blob, AbortController, DOMException,
    setTimeout: withDom ? (callback, delay) => {
      const id = ++nextTimer;
      timers.set(id, { callback, delay });
      return id;
    } : setTimeout,
    clearTimeout: withDom ? (id) => timers.delete(id) : clearTimeout,
    cancelAnimationFrame() {},
    el: dom?.el,
    location, history: { replaceState(_state, _title, hash) { location.hash = hash; } },
    sessionStorage: { setItem: (key, value) => session.set(key, value), getItem: (key) => session.get(key) || null, removeItem: (key) => session.delete(key) },
    // The loader optionally updates a progress element. No DOM is required.
    document: new Proxy({ getElementById: dom?.getElementById || (() => null), body }, {
      get(target, key) {
        if (key === "getElementById" || key === "body") return target[key];
        throw new HarnessError(`unexpected DOM access: ${String(key)}`);
      },
    }),
    fetch() { throw new HarnessError("GROUP operations must use the authenticated api helper"); },
    async api(method, route, body, options = {}) {
      const call = { method, route, url: new URL(route, "https://console.invalid"), body, options };
      calls.push(call);
      if (!api || calls.length > maxCalls) throw new HarnessError(`unexpected API call: ${method} ${route}`);
      return api(call, calls.length);
    },
  });
  graphScript.runInContext(context, { timeout: 2000 });
  const hooks = context.window.__testHooks;
  hooks.GX.loadSeq = SEQ;
  hooks.GX.code.seq = SEQ;
  return {
    ...hooks, S, calls, dom, timers, session, location, body, GraphExplorer: context.window.GraphExplorer,
    hook(name) {
      assert.equal(typeof hooks[name], "function", `${name} must be available at the final VM injection hook`);
      return hooks[name];
    },
  };
}

function groupModel(h, raw = graph(), meta = modelMeta()) {
  const M = h.fromRaw(raw, GID, meta);
  h.GX.M = M;
  h.GX.srcId = GID;
  return M;
}

function repoModel(h, repoId = "backend", raw = { nodes: [node()], links: [] }) {
  const M = h.fromRaw(raw, repoId);
  h.GX.M = M;
  h.GX.srcId = repoId;
  return M;
}

function graphApi(raw = graph(), transform = (out) => out) {
  return (call, count) => {
    const kind = call.url.searchParams.get("kind") || "nodes";
    if (!(kind in raw)) throw new HarnessError(`unexpected graph kind: ${kind}`);
    return transform(page(kind, raw[kind]), call, count);
  };
}

function assertGraphCalls(h, signal) {
  for (const [i, call] of h.calls.entries()) {
    assert.equal(call.method, "GET");
    assert.equal(call.url.pathname, `/groups/${GID}/graph`);
    assert.equal(call.url.searchParams.get("limit"), "500");
    assert.ok(["nodes", "links"].includes(call.url.searchParams.get("kind")));
    assert.equal(call.options.signal, signal, "every authenticated request must carry the caller's signal");
    if (i) assert.equal(call.url.searchParams.get("group_version"), VERSION, "all later pages must pin the first snapshot");
  }
}

// A bad mock, missing browser global, or assertion inside a stub must never
// accidentally satisfy a test expecting a production validation failure.
async function rejectsContract(operation) {
  await assert.rejects(operation, (error) => {
    assert.ok(error && typeof error.message === "string", "expected an error");
    assert.ok(!(error instanceof HarnessError), error.message);
    assert.ok(!(error instanceof assert.AssertionError), error.message);
    assert.ok(!["ReferenceError", "TypeError"].includes(error.name), error.stack);
    return true;
  });
}

test("VM injection exposes the planned helpers without booting the DOM", () => {
  const h = harness();
  for (const name of ["fromRaw", "loadGroupGraph", "sourceTargetsFor", "readSourceResult"]) h.hook(name);
});

test("fromRaw preserves GROUP IDs, labels, files, origins and link evidence exactly", () => {
  const h = harness();
  const id = `backend:${"x".repeat(550)}\u200b:한글`;
  const label = `  ${"설명".repeat(130)}\u200b<script>alert(1)</script>  `;
  const file = `src/${"nested/".repeat(80)}orders.ts`;
  const raw = graph();
  raw.nodes[0] = node("backend", id, { label, source_file: file, repo: "misleading-repo" });
  raw.nodes[1].label = "";
  raw.links[0].source = id;
  const before = clone(raw);
  const M = groupModel(h, raw);
  assert.deepEqual(Array.from(M.id), [id, "frontend:submit"]);
  assert.deepEqual(Array.from(M.label), [label, ""]);
  assert.equal(M.files[M.f[0]], file);
  assert.deepEqual(Array.from(M.repos), ["backend", "frontend"]);
  assert.deepEqual(Array.from(M.r, (index) => M.repos[index]), ["backend", "frontend"]);
  assert.deepEqual(clone(M.provenance), raw.nodes);
  assert.deepEqual(clone(M.linkProvenance), raw.links);
  assert.equal(M.groupId, GID);
  assert.equal(M.groupVersion, VERSION);
  assert.deepEqual(clone(M.groupSources), SOURCES);
  assert.equal(M.sourceVersions.get("frontend"), "frontend-v7");
  assert.deepEqual([M.n, M.e, M.es[0], M.et[0]], [2, 1, 0, 1]);
  assert.deepEqual(raw, before, "adapting a graph must not rewrite the input");
});

test("fromRaw keeps prototype-like GROUP IDs distinct and connects exact endpoints", () => {
  const h = harness();
  const ids = ["__proto__", "constructor", "toString", "a".repeat(520) + "1", "a".repeat(520) + "2"];
  const raw = {
    nodes: ids.map((id) => node("backend", id, { label: id })),
    links: ids.slice(1).map((id, index) => ({ source: ids[index], target: id, relation: "calls" })),
  };
  const M = groupModel(h, raw);
  assert.deepEqual(Array.from(M.id), ids);
  assert.deepEqual(Array.from(M.es), [0, 1, 2, 3]);
  assert.deepEqual(Array.from(M.et), [1, 2, 3, 4]);
});

test("fromRaw retains ordinary repository cleaning and label fallback", () => {
  const h = harness();
  const id = "x".repeat(550);
  const M = repoModel(h, "regular", {
    nodes: [node("backend", id, { label: "" }), node("frontend", "b", { label: "a\u200bb" })], links: [],
  });
  assert.equal(M.id[0].length, 512);
  assert.equal(M.label[0].length, 200);
  assert.equal(M.label[1], "ab");
  assert.equal(M.groupId, undefined);
});

test("loadGroupGraph reads both page streams, follows actual offsets and pins their snapshot", async () => {
  const signal = new AbortController().signal;
  const raw = graph();
  const h = harness({
    api(call) {
      const kind = call.url.searchParams.get("kind") || "nodes";
      const offset = Number(call.url.searchParams.get("offset") || 0);
      if (kind === "nodes" && offset === 0) return page(kind, [raw.nodes[0]], 1);
      if (kind === "nodes" && offset === 1) return page(kind, [raw.nodes[1]]);
      if (kind === "links" && offset === 0) return page(kind, raw.links, 1);
      if (kind === "links" && offset === 1) return page(kind, [{ ...raw.links[0], id: "second-link" }]);
      throw new HarnessError(`unexpected page ${kind}:${offset}`);
    },
  });
  const { M, info } = await h.hook("loadGroupGraph")(GID, groupMeta(), SEQ, signal);
  assert.deepEqual(h.calls.map((c) => [c.url.searchParams.get("kind"), Number(c.url.searchParams.get("offset") || 0)]),
    [["nodes", 0], ["nodes", 1], ["links", 0], ["links", 1]]);
  assertGraphCalls(h, signal);
  assert.deepEqual([M.n, M.e], [2, 2]);
  assert.equal(M.groupVersion, VERSION);
  assert.equal(M.sourceVersions.get("backend"), "backend-v2");
  assert.equal(M.sourceVersions.get("frontend"), "frontend-v7");
  assert.ok(info && typeof info === "object");
});

test("loadGroupGraph accepts an empty GROUP snapshot", async () => {
  const h = harness({ api: graphApi({ nodes: [], links: [] }) });
  const { M } = await h.hook("loadGroupGraph")(GID, groupMeta(), SEQ, new AbortController().signal);
  assert.deepEqual([M.n, M.e], [0, 0]);
  assert.equal(h.calls.length, 2);
});

test("loadGroupGraph rejects incomplete pagination and mismatched revision or totals", async (t) => {
  for (const [name, change] of [
    ["missing terminal cursor", (out) => { delete out.next_offset; return out; }],
    ["different revision", (out) => ({ ...out, revision: 4 })],
    ["missing nodes despite declared total", (out) => ({ ...out, stats: { nodes: 3 } })],
    ["missing links despite declared total", (out) => ({ ...out, stats: { links: 2 } })],
  ]) {
    await t.test(name, async () => {
      const h = harness({ api: graphApi(graph(), change) });
      await rejectsContract(() => h.hook("loadGroupGraph")(GID, groupMeta(), SEQ, new AbortController().signal));
    });
  }
});

test("loadGroupGraph accepts edge_id and preserves link provenance without rewriting IDs", async () => {
  const raw = graph();
  raw.links[0].edge_id = raw.links[0].id;
  delete raw.links[0].id;
  const h = harness({ api: graphApi(raw) });
  const { M } = await h.hook("loadGroupGraph")(GID, groupMeta(), SEQ, new AbortController().signal);
  assert.equal(M.e, 1);
  assert.deepEqual(clone(M.linkProvenance), raw.links);
  assert.equal(M.linkProvenance[0].id, undefined);
});

test("loadGroupGraph rejects mismatched group/version on first and subsequent pages", async (t) => {
  for (const at of [1, 2]) {
    for (const [field, value] of [
      ["group_id", OTHER_GID], ["version", "different-build"], ["group_version", "different-build"],
      ["version", ""], ["group_version", ""],
    ]) {
      await t.test(`${field}=${JSON.stringify(value)} on page ${at}`, async () => {
        const h = harness({
          api: graphApi(graph(), (out, _call, count) => count === at ? { ...out, [field]: value } : out),
        });
        await rejectsContract(() => h.hook("loadGroupGraph")(GID, groupMeta(), SEQ, new AbortController().signal));
        assert.equal(h.calls.length, at, "stop at the invalid page");
      });
    }
  }
  await t.test("both version fields agree on a new build after a valid first page", async () => {
    const signal = new AbortController().signal;
    const h = harness({
      api: graphApi(graph(), (out, _call, count) => count === 2
        ? { ...out, version: "rebuilt", group_version: "rebuilt" } : out),
    });
    await rejectsContract(() => h.hook("loadGroupGraph")(GID, groupMeta(), SEQ, signal));
    assert.equal(h.calls.length, 2);
    assertGraphCalls(h, signal);
  });
});

test("loadGroupGraph rejects invalid page shapes, source membership and graph identities", async (t) => {
  const cases = [
    ["missing nodes array", (out) => { delete out.nodes; }],
    ["non-array nodes", (out) => { out.nodes = {}; }],
    ["more than 500 nodes in one page", (out) => {
      out.nodes = Array.from({ length: 501 }, (_, i) => node("backend", `node-${i}`));
    }],
    ["wrong source membership", (out) => { out.sources = [SOURCES[0]]; }],
    ["duplicate source membership", (out) => { out.sources = [SOURCES[0], SOURCES[0]]; }],
    ["unlisted node origin", (out) => { out.nodes[0].source_id = "foreign-source"; }],
    ["mismatched node version", (out) => { out.nodes[0].source_version = "stale"; }],
    ["duplicate node ID", (out) => { out.nodes.push(clone(out.nodes[0])); }],
  ];
  for (const [name, change] of cases) {
    await t.test(name, async () => {
      const h = harness({ api: graphApi(graph(), (out) => { change(out); return out; }) });
      await rejectsContract(() => h.hook("loadGroupGraph")(GID, groupMeta(), SEQ, new AbortController().signal));
      assert.equal(h.calls.length, 1);
    });
  }
  for (const [name, change] of [
    ["foreign endpoint", (out) => { out.links[0].target = "missing-node"; }],
    ["duplicate link ID", (out) => { out.links.push(clone(out.links[0])); }],
    ["conflicting id and edge_id", (out) => { out.links[0].edge_id = "different-id"; }],
    ["source membership changes between node and link pages", (out) => { out.sources = [SOURCES[0]]; }],
  ]) {
    await t.test(name, async () => {
      const h = harness({
        api: graphApi(graph(), (out, _call, count) => { if (count === 2) change(out); return out; }),
      });
      await rejectsContract(() => h.hook("loadGroupGraph")(GID, groupMeta(), SEQ, new AbortController().signal));
      assert.equal(h.calls.length, 2);
    });
  }
});

test("loadGroupGraph rejects nonadvancing, fractional and nonnumeric cursors", async (t) => {
  for (const cursor of [0, -1, 0.5, "1", false]) {
    await t.test(`cursor ${JSON.stringify(cursor)}`, async () => {
      const h = harness({ api: () => page("nodes", [node()], cursor), maxCalls: 3 });
      await rejectsContract(() => h.hook("loadGroupGraph")(GID, groupMeta(), SEQ, new AbortController().signal));
      assert.equal(h.calls.length, 1);
    });
  }
  await t.test("repeated cursor after one valid page", async () => {
    const h = harness({
      api: (_call, count) => page("nodes", [node("backend", `node-${count}`)], 1), maxCalls: 3,
    });
    await rejectsContract(() => h.hook("loadGroupGraph")(GID, groupMeta(), SEQ, new AbortController().signal));
    assert.equal(h.calls.length, 2);
  });
});

test("loadGroupGraph stops before requests and after awaited responses when cancelled", async (t) => {
  for (const during of [false, true]) {
    for (const mode of ["sequence", "abort"]) {
      await t.test(`${mode} ${during ? "during request" : "before request"}`, async () => {
        const controller = new AbortController();
        let h;
        const cancel = () => mode === "sequence" ? h.GX.loadSeq++ : controller.abort();
        h = harness({ api: graphApi(graph(), (out) => { cancel(); return out; }) });
        if (!during) cancel();
        await rejectsContract(() => h.hook("loadGroupGraph")(GID, groupMeta(), SEQ, controller.signal));
        assert.equal(h.calls.length, during ? 1 : 0);
      });
    }
  }
});

test("loadGroupGraph enforces cumulative node and link limits across legal-sized pages", async (t) => {
  for (const [kind, limit] of [["nodes", 50000], ["links", 200000]]) {
    await t.test(`more than ${limit} ${kind}`, async () => {
      let served = 0;
      const h = harness({
        api(call) {
          const requested = call.url.searchParams.get("kind") || "nodes";
          if (requested !== kind) return page(requested, graph()[requested]);
          const offset = Number(call.url.searchParams.get("offset") || 0);
          const size = Math.min(500, limit + 1 - offset);
          if (size <= 0) throw new HarnessError("requested beyond the terminal page");
          const values = Array.from({ length: size }, (_, i) => kind === "nodes"
            ? node("backend", `n${offset + i}`, { label: "" })
            : { id: `e${offset + i}`, source: "backend:submit", target: "frontend:submit", relation: "calls" });
          served += values.length;
          return page(kind, values, offset + size <= limit ? offset + size : null);
        },
      });
      await rejectsContract(() => h.hook("loadGroupGraph")(GID, groupMeta(), SEQ, new AbortController().signal));
      // At the cap, a continuation cursor already proves the graph is too big.
      assert.ok(served >= limit && served <= limit + 1, "reject at the cumulative cap, not an earlier valid page");
    });
  }
});

test("loadGroupGraph accepts terminal pages at exactly 50k nodes and 200k links", async () => {
  const h = harness({
    api(call) {
      const kind = call.url.searchParams.get("kind");
      const offset = Number(call.url.searchParams.get("offset") || 0);
      const limit = kind === "nodes" ? 50000 : 200000;
      const rows = Array.from({ length: 500 }, (_, i) => kind === "nodes"
        ? node("backend", `n${offset + i}`, { label: "" })
        : { id: `e${offset + i}`, source: "n0", target: "n1", relation: "calls" });
      return page(kind, rows, offset + rows.length < limit ? offset + rows.length : null);
    },
  });
  const signal = new AbortController().signal;
  const { M } = await h.hook("loadGroupGraph")(GID, groupMeta(), SEQ, signal);
  assert.deepEqual([M.n, M.e], [50000, 200000]);
  assert.equal(h.calls.length, 500);
  assertGraphCalls(h, signal);
});

test("loadGroupGraph caps cumulative decoded UTF-8 JSON at 32 MiB", async () => {
  let bytes = 0;
  let characters = 0;
  const limit = 32 * 1024 * 1024;
  const h = harness({
    api(_call, count) {
      // Each page is below the API's 1 MiB bound; multibyte content detects
      // counting UTF-16 string length instead of decoded UTF-8 bytes.
      const out = page("nodes", [node("backend", `large-${count}`, { label: "한".repeat(240000) })], count);
      const json = JSON.stringify(out);
      assert.ok(Buffer.byteLength(json, "utf8") < 1024 * 1024);
      bytes += Buffer.byteLength(json, "utf8");
      characters += json.length;
      return out;
    },
    maxCalls: 100,
  });
  await rejectsContract(() => h.hook("loadGroupGraph")(GID, groupMeta(), SEQ, new AbortController().signal));
  assert.ok(bytes > limit && bytes < limit + 1024 * 1024, "reject the first page crossing the byte budget");
  assert.ok(characters < limit, "fixture must distinguish bytes from string length");
});

test("sourceTargetsFor selects a GROUP node's exact origin and snapshot", () => {
  const h = harness();
  const M = groupModel(h);
  assert.deepEqual(clone(h.hook("sourceTargetsFor")(M, 1)), [{
    groupId: GID, groupVersion: VERSION, sourceId: "frontend", sourceVersion: "frontend-v7",
  }]);
});

test("sourceTargetsFor keeps normal repository and hub source routes", () => {
  const h = harness({ state: { repos: [{ repo_id: "backend" }, { repo_id: "frontend" }] } });
  const M = repoModel(h);
  assert.deepEqual(clone(h.hook("sourceTargetsFor")(M, 0)), [{ server: "backend" }]);
  const hub = repoModel(h, "all", {
    nodes: [node("backend", "b", { repo: "backend" }), node("frontend", "f", { repo: "frontend" })],
    links: [],
  });
  assert.deepEqual(clone(h.hook("sourceTargetsFor")(hub, 1)), [{ server: "frontend" }]);
});

test("sourceTargetsFor routes group-only sources through matching ready groups", () => {
  const ready = groupMeta();
  const partial = groupMeta({ group_id: OTHER_GID, status: "PARTIAL", active_version: "partial-v1" });
  const h = harness({ state: {
    repos: [groupOnlyRepo()], groups: [ready, partial],
  } });
  const M = repoModel(h);
  assert.deepEqual(clone(h.hook("sourceTargetsFor")(M, 0)), [
    { groupId: GID, groupVersion: VERSION, sourceId: "backend", sourceVersion: "backend-v2" },
    { groupId: OTHER_GID, groupVersion: "partial-v1", sourceId: "backend", sourceVersion: "backend-v2" },
  ]);
});

test("sourceTargetsFor recognizes group-only runtime flags on repositories or servers", async (t) => {
  for (const flag of [false, 0, "0"]) {
    for (const where of ["repo", "server"]) {
      await t.test(`${where}: ${JSON.stringify(flag)}`, () => {
        const h = harness({ state: {
          repos: [groupOnlyRepo({ dedicated_runtime: where === "repo" ? flag : true })],
          servers: where === "server" ? [{ server_id: "backend", dedicated_runtime: flag }] : [],
        } });
        const M = repoModel(h);
        assert.deepEqual(clone(h.hook("sourceTargetsFor")(M, 0)), [{
          groupId: GID, groupVersion: VERSION, sourceId: "backend", sourceVersion: "backend-v2",
        }]);
      });
    }
  }
});

test("sourceTargetsFor returns no route for group-only sources without a usable group", async (t) => {
  const cases = [
    ["absent", []],
    ["different source", [groupMeta({ sources: [SOURCES[1]] })]],
    ["building", [groupMeta({ status: "BUILDING" })]],
    ["stale", [groupMeta({ stale: true })]],
    ["access recovery", [groupMeta({ access_recovery: true })]],
    ["data unavailable", [groupMeta({ data_ready: false })]],
    ["revision changed", [groupMeta({ revision: 4 })]],
    ["unversioned", [groupMeta({ active_version: "" })]],
    ["source version missing", [groupMeta({ active_source_versions: {} })]],
    ["source version changed", [groupMeta({ active_source_versions: { backend: "old-v1" } })]],
  ];
  for (const [name, groups] of cases) {
    await t.test(name, () => {
      const h = harness({ state: { repos: [groupOnlyRepo()], groups } });
      const M = repoModel(h);
      assert.deepEqual(clone(h.hook("sourceTargetsFor")(M, 0)), []);
      assert.equal(h.calls.length, 0);
    });
  }
});

function sourceResponse(overrides = {}) {
  return {
    group_id: GID, version: VERSION, group_version: VERSION,
    source_id: "frontend", source_version: "frontend-v7", file: "src/orders.ts",
    start_line: 2, end_line: 3, line_count: 8, text: 'return "서울";\r\n}\r\n',
    ...overrides,
  };
}

function assertGroupSourceCall(call, signal, source = "frontend") {
  assert.equal(call.method, "POST");
  assert.equal(call.url.pathname, `/groups/${GID}/source`);
  assert.equal(call.options.signal, signal);
  assert.equal(call.body.group_version, VERSION);
  assert.equal(call.body.source_id, source);
  assert.equal(call.body.file, "src/orders.ts");
  assert.equal(call.body.start_line, 2);
  assert.equal(call.body.end_line, 3);
}

test("readSourceResult follows snapshot line boundaries without changing source text", async () => {
  const text = "A — B · C\u2028**Markdown**\u0085";
  const h = harness({ api: () => sourceResponse({ text }) });
  const M = groupModel(h);
  const result = await h.hook("readSourceResult")(M, 1, "src/orders.ts", 2, 3, SEQ, new AbortController().signal);
  assert.equal(result.text, text);
  assert.deepEqual(clone(result.parsed.rows), [{ n: 2, text: "A — B · C" }, { n: 3, text: "**Markdown**" }]);
});

test("readSourceResult posts the node origin and renders structured GROUP text", async () => {
  const signal = new AbortController().signal;
  const h = harness({ api: () => sourceResponse() });
  const M = groupModel(h);
  const result = await h.hook("readSourceResult")(M, 1, "src/orders.ts", 2, 3, SEQ, signal);
  assert.equal(h.calls.length, 1);
  assertGroupSourceCall(h.calls[0], signal);
  assert.equal(result.text, sourceResponse().text, "preserve source bytes, including CRLF");
  assert.deepEqual(clone(result.parsed.rows), [{ n: 2, text: 'return "서울";' }, { n: 3, text: "}" }]);
  assert.equal(result.parsed.total, 8);
});

test("readSourceResult rejects mismatched and malformed GROUP response fields", async (t) => {
  const cases = [
    ["group_id", OTHER_GID], ["version", "wrong-build"], ["group_version", "wrong-build"],
    ["source_id", "backend"], ["source_version", "wrong-source-version"], ["file", "README.md"],
    ["start_line", 1], ["start_line", "2"], ["end_line", 4], ["end_line", 1],
    ["line_count", 2], ["line_count", "8"], ["line_count", -1], ["text", null], ["text", 123],
    ["end_line", 2.5], ["line_count", 8.5], ["text", "one line only"],
    ["text", "too\nmany\nlines\n"],
  ];
  for (const [field, value] of cases) {
    await t.test(`${field}=${JSON.stringify(value)}`, async () => {
      const h = harness({ api: () => sourceResponse({ [field]: value }) });
      const M = groupModel(h);
      await rejectsContract(() => h.hook("readSourceResult")(M, 1, "src/orders.ts", 2, 3, SEQ, new AbortController().signal));
      assert.equal(h.calls.length, 1, "invalid GROUP responses must not cause repository fallback");
      assert.equal(h.calls[0].url.pathname, `/groups/${GID}/source`);
    });
  }
  for (const field of ["group_id", "version", "group_version", "source_id", "source_version", "file",
    "start_line", "end_line", "line_count", "text"]) {
    await t.test(`missing ${field}`, async () => {
      const out = sourceResponse();
      delete out[field];
      const h = harness({ api: () => out });
      const M = groupModel(h);
      await rejectsContract(() => h.hook("readSourceResult")(M, 1, "src/orders.ts", 2, 3, SEQ, new AbortController().signal));
      assert.equal(h.calls.length, 1);
    });
  }
});

test("readSourceResult accepts EOF-clamped windows and empty files", async (t) => {
  await t.test("EOF", async () => {
    const h = harness({ api: () => sourceResponse({ end_line: 2, line_count: 2, text: "last line" }) });
    const M = groupModel(h);
    const result = await h.hook("readSourceResult")(M, 1, "src/orders.ts", 2, 3, SEQ, new AbortController().signal);
    assert.deepEqual(clone(result.parsed.rows), [{ n: 2, text: "last line" }]);
    assert.equal(result.parsed.total, 2);
  });
  await t.test("empty file", async () => {
    const h = harness({ api: () => sourceResponse({ start_line: 0, end_line: 0, line_count: 0, text: "" }) });
    const M = groupModel(h);
    const result = await h.hook("readSourceResult")(M, 1, "src/orders.ts", 1, 40, SEQ, new AbortController().signal);
    assert.deepEqual(clone(result.parsed.rows), []);
    assert.equal(result.parsed.total, 0);
  });
});

test("readSourceResult rejects stale source requests before and after awaiting API responses", async (t) => {
  for (const during of [false, true]) {
    for (const mode of ["sequence", "abort", "model"]) {
      await t.test(`${mode} ${during ? "during request" : "before request"}`, async () => {
        const controller = new AbortController();
        let h;
        const cancel = () => {
          if (mode === "sequence") h.GX.code.seq++;
          else if (mode === "abort") controller.abort();
          else h.GX.M = null;
        };
        h = harness({ api() { cancel(); return sourceResponse(); } });
        const M = groupModel(h);
        if (!during) cancel();
        await rejectsContract(() => h.hook("readSourceResult")(M, 1, "src/orders.ts", 2, 3, SEQ, controller.signal));
        assert.equal(h.calls.length, during ? 1 : 0);
      });
    }
  }
});

test("readSourceResult revalidates GROUP access on repeated reads instead of returning cached text", async () => {
  const signal = new AbortController().signal;
  const h = harness({ api(_call, count) {
    if (count === 1) return sourceResponse();
    throw Object.assign(new Error("access revoked"), { status: 403 });
  } });
  const M = groupModel(h);
  const read = () => h.hook("readSourceResult")(M, 1, "src/orders.ts", 2, 3, SEQ, signal);
  assert.equal((await read()).text, sourceResponse().text);
  await rejectsContract(read);
  assert.equal(h.calls.length, 2);
  for (const call of h.calls) assertGroupSourceCall(call, signal);
});

test("readSourceResult tries another ready group containing the same immutable source", async () => {
  const signal = new AbortController().signal;
  const h = harness({
    state: { repos: [groupOnlyRepo()], groups: [groupMeta(), groupMeta({ group_id: OTHER_GID })] },
    api(_call, count) {
      if (count === 1) throw Object.assign(new Error("group no longer accessible"), { status: 403 });
      return sourceResponse({ group_id: OTHER_GID, source_id: "backend", source_version: "backend-v2" });
    },
  });
  const M = repoModel(h);
  const result = await h.hook("readSourceResult")(M, 0, "src/orders.ts", 2, 3, SEQ, signal);
  assert.equal(result.target.groupId, OTHER_GID);
  assert.equal(result.text, sourceResponse().text);
  assert.deepEqual(h.calls.map((c) => c.url.pathname), [`/groups/${GID}/source`, `/groups/${OTHER_GID}/source`]);
  for (const call of h.calls) {
    assert.equal(call.body.source_id, "backend");
    assert.equal(call.body.group_version, VERSION);
    assert.equal(call.options.signal, signal);
  }
});

test("readSourceResult rejects a group-only source whose active version changes during the request", async () => {
  let h;
  h = harness({
    state: { repos: [groupOnlyRepo()] },
    api() {
      h.S.repos[0].active_source_version = "backend-rebuilt";
      return sourceResponse({ source_id: "backend", source_version: "backend-v2" });
    },
  });
  const M = repoModel(h);
  await rejectsContract(() => h.hook("readSourceResult")(M, 0, "src/orders.ts", 2, 3, SEQ, new AbortController().signal));
  assert.equal(h.calls.length, 1);
  assert.equal(h.calls[0].url.pathname, `/groups/${GID}/source`);
});

test("readSourceResult never falls back to repository MCP for group-only sources", async (t) => {
  for (const status of [403, 404, 409, 500]) {
    await t.test(`GROUP HTTP ${status}`, async () => {
      const h = harness({
        state: { repos: [groupOnlyRepo()] },
        api() { throw Object.assign(new Error(`HTTP ${status}`), { status }); },
      });
      const M = repoModel(h);
      await rejectsContract(() => h.hook("readSourceResult")(M, 0, "src/orders.ts", 2, 3, SEQ, new AbortController().signal));
      assert.equal(h.calls.length, 1);
      assert.equal(h.calls[0].url.pathname, `/groups/${GID}/source`);
    });
  }
  await t.test("no ready group means no API call", async () => {
    const h = harness({ state: { repos: [groupOnlyRepo()], groups: [] } });
    const M = repoModel(h);
    await rejectsContract(() => h.hook("readSourceResult")(M, 0, "src/orders.ts", 2, 3, SEQ, new AbortController().signal));
    assert.equal(h.calls.length, 0);
  });
});

test("readSourceResult preserves ordinary repository source reads", async () => {
  const signal = new AbortController().signal;
  const h = harness({
    state: { repos: [{ repo_id: "regular/repo" }] },
    api: () => ({ text: "src/orders.ts lines 2-3 of 8:\n2| return 1;\n3| }\n" }),
  });
  const M = repoModel(h, "regular/repo");
  const result = await h.hook("readSourceResult")(M, 0, "src/orders.ts", 2, 3, SEQ, signal);
  assert.equal(h.calls.length, 1);
  assert.equal(h.calls[0].route, "/repos/regular%2Frepo/source");
  assert.equal(h.calls[0].method, "POST");
  assert.equal(h.calls[0].options.signal, signal);
  assert.deepEqual(clone(h.calls[0].body), { file: "src/orders.ts", start_line: 2, end_line: 3 });
  assert.deepEqual(clone(result.parsed.rows), [{ n: 2, text: "return 1;" }, { n: 3, text: "}" }]);
});

test("loadSource clears prior graph, source text and interaction state on GROUP 403/404/409", async (t) => {
  for (const status of [403, 404, 409]) {
    for (const failAt of [1, 2]) {
      await t.test(`HTTP ${status} from ${failAt === 1 ? "metadata" : "graph"}`, async () => {
        const h = harness({
          withDom: true,
          api(_call, count) {
            if (count === failAt) throw Object.assign(new Error(`HTTP ${status}`), { status });
            return { group: groupMeta() };
          },
        });
        const M = groupModel(h);
        const oldLoad = new AbortController();
        const oldRead = new AbortController();
        let killed = 0;
        h.GX.loadController = oldLoad;
        h.GX.code.controller = oldRead;
        h.GX.renderer = { kill() { killed++; } };
        h.GX.graph = { privateGraph: true };
        h.GX.info = { privateMetadata: true };
        h.GX.cache.set(GID, { M });
        h.GX.code.cache.set("old-source", "private source text");
        h.GX.code.range = { node: 0, start: 1, end: 40 };
        h.GX.code.target = { groupId: GID };
        h.GX.selected = "0"; h.GX.selectedNbrs = new Set(["1"]);
        h.GX.hovered = "1"; h.GX.hoveredNbrs = new Set(["0"]);
        h.GX.path = { a: 0, b: 1, nodes: ["0", "1"], edges: ["0"] };
        h.GX.history = [{ privateHistory: true }];
        h.GX.viewNodes = ["0", "1"]; h.GX.viewEdges = ["0"];
        h.GX.colorRank.set("privateCategory", 1); h.GX.legendCats = ["privateCategory"];
        for (const id of ["gx-canvas", "gx-inspector", "gx-legend", "gx-search-results"]) {
          h.dom.getElementById(id).append("private visible content");
        }
        h.dom.getElementById("gx-search").value = "private search";

        await h.hook("loadSource")(GID, { force: true });

        assert.equal(h.calls.length, failAt);
        assert.equal(h.calls[0].method, "GET");
        assert.equal(h.calls[0].route, `/groups/${GID}`);
        assert.ok(h.calls.every((call) => call.options.signal === h.GX.loadController.signal));
        if (failAt === 2) assert.equal(h.calls[1].url.pathname, `/groups/${GID}/graph`);
        assert.ok(oldLoad.signal.aborted && oldRead.signal.aborted);
        assert.ok(h.GX.loadController.signal.aborted);
        assert.equal(killed, 1);
        for (const field of ["M", "info", "graph", "renderer", "selected", "selectedNbrs", "hovered", "hoveredNbrs"]) {
          assert.equal(h.GX[field], null, `${field} must be cleared`);
        }
        assert.equal(h.GX.loading, false);
        assert.equal(h.GX.cache.has(GID), false);
        assert.equal(h.GX.code.cache.size, 0);
        assert.equal(h.GX.code.range, null);
        assert.equal(h.GX.code.target, null);
        assert.deepEqual(clone(h.GX.path), { a: null, b: null, nodes: null, edges: null, result: null });
        for (const field of ["history", "viewNodes", "viewEdges", "legendCats"]) assert.equal(h.GX[field].length, 0);
        assert.equal(h.GX.colorRank.size, 0);
        for (const id of ["gx-canvas", "gx-inspector", "gx-legend", "gx-search-results"]) {
          assert.equal(h.dom.getElementById(id).children.length, 0, `${id} must not retain prior content`);
        }
        assert.equal(h.dom.getElementById("gx-search").value, "");
        assert.equal(h.dom.getElementById("gx-overlay").hidden, false);
        const title = h.dom.getElementById("gx-overlay-card").children[0];
        assert.equal(title.children[0], status === 409 ? "gx.group.stale" : "gx.group.denied");
      });
    }
  }
});

test("loadSource shows GROUP pending state without requiring rendering libraries", async () => {
  const meta = groupMeta({ status: "BUILDING", data_ready: false, active_version: "" });
  const h = harness({ withDom: true, withLibraries: false, api: () => ({ group: meta }) });
  groupModel(h);
  h.GX.code.cache.set("previous", "private source text");
  h.dom.getElementById("gx-inspector").append("private visible content");
  h.dom.getElementById("gx-canvas").append("previous graph");

  await h.hook("loadSource")(GID);

  assert.equal(h.calls.length, 1, "only metadata is needed to show pending state");
  assert.equal(h.calls[0].route, `/groups/${GID}`);
  assert.equal(h.GX.M, null);
  assert.equal(h.GX.renderer, null);
  assert.equal(h.GX.loading, false);
  assert.equal(h.GX.info.status, "BUILDING");
  assert.equal(h.GX.code.cache.size, 0);
  assert.equal(h.dom.getElementById("gx-inspector").children.length, 0);
  assert.equal(h.dom.getElementById("gx-canvas").children.length, 0);
  assert.equal(h.dom.getElementById("gx-overlay").hidden, false);
  const card = h.dom.getElementById("gx-overlay-card");
  assert.equal(card.children[0].children[0], "gx.group.pending");
  assert.equal(h.GX.libsError, "", "pending state must not be replaced by a library error");
  assert.equal(h.timers.size, 1);
  assert.equal(h.timers.get(h.GX.pollTimer).delay, 20000);
});

test("loadSource clears prior content and reports missing libraries for a ready GROUP", async () => {
  const h = harness({ withDom: true, withLibraries: false, api: () => ({ group: groupMeta() }) });
  groupModel(h);
  h.GX.code.cache.set("previous", "private source text");
  h.dom.getElementById("gx-inspector").append("private visible content");
  h.dom.getElementById("gx-canvas").append("previous graph");

  await h.hook("loadSource")(GID);

  assert.equal(h.calls.length, 1, "do not fetch graph pages if rendering libraries are unavailable");
  assert.equal(h.calls[0].route, `/groups/${GID}`);
  assert.equal(h.GX.M, null);
  assert.equal(h.GX.info, null);
  assert.equal(h.GX.loading, false);
  assert.equal(h.GX.code.cache.size, 0);
  assert.equal(h.dom.getElementById("gx-inspector").children.length, 0);
  assert.equal(h.dom.getElementById("gx-canvas").children.length, 0);
  assert.equal(h.dom.getElementById("gx-overlay").hidden, false);
  assert.equal(h.GX.libsError, "libs");
  assert.ok(h.dom.getElementById("gx-overlay-card").children.some((element) => element.children.includes("gx.err.libs")));
  assert.equal(h.timers.size, 0);
});

test("loadSource clears revoked content for access-recovery metadata before checking libraries", async () => {
  const h = harness({
    withDom: true, withLibraries: false, api: () => ({ group: groupMeta({ access_recovery: true }) }),
  });
  const M = groupModel(h);
  h.GX.cache.set(GID, { M });
  h.GX.code.cache.set("previous", "private source text");
  h.dom.getElementById("gx-inspector").append("private visible content");

  await h.hook("loadSource")(GID);

  assert.equal(h.calls.length, 1);
  assert.equal(h.GX.M, null);
  assert.equal(h.GX.info, null);
  assert.equal(h.GX.loading, false);
  assert.equal(h.GX.cache.has(GID), false);
  assert.equal(h.GX.code.cache.size, 0);
  assert.equal(h.dom.getElementById("gx-inspector").children.length, 0);
  assert.equal(h.dom.getElementById("gx-overlay").hidden, false);
  assert.equal(h.dom.getElementById("gx-overlay-card").children[0].children[0], "gx.group.denied");
  assert.equal(h.GX.libsError, "");
  assert.equal(h.timers.size, 0);
});

test("loadSource rejects a flat metadata response instead of accepting a non-API fixture shape", async () => {
  const h = harness({ withDom: true, api: () => groupMeta() });
  await h.hook("loadSource")(GID);
  assert.equal(h.calls.length, 1);
  assert.equal(h.GX.M, null);
  assert.equal(h.dom.getElementById("gx-overlay-card").children[0].children[0], "gx.group.stale");
});

test("loadSource never publishes a delayed group response after newer access or version metadata", async (t) => {
  for (const change of ["removed", "recovery", "version", "pending"]) {
    for (const at of ["metadata", "last graph page"]) {
      await t.test(`${change} during ${at}`, async () => {
        const h = harness({
          withDom: true, state: { groupEpoch: 1 },
          api(call) {
            const isMetadata = call.url.pathname === `/groups/${GID}`;
            const kind = call.url.searchParams.get("kind");
            const result = isMetadata ? { group: groupMeta() } : page(kind, graph()[kind]);
            if ((at === "metadata" && isMetadata) || (at === "last graph page" && kind === "links")) {
              h.S.groups = change === "removed" ? [] : [groupMeta(
                change === "recovery" ? { access_recovery: true }
                  : change === "version" ? { active_version: "new-group-version", revision: 4 }
                  : { data_ready: false, status: "BUILDING" },
              )];
              h.S.groupEpoch++;
            }
            return result;
          },
        });
        await h.hook("loadSource")(GID);
        assert.equal(h.calls.length, at === "metadata" ? 1 : 3);
        assert.equal(h.GX.M, null);
        assert.equal(h.GX.renderer, null);
        assert.equal(h.GX.loading, false);
        assert.ok(h.GX.loadController.signal.aborted);
        assert.equal(h.dom.getElementById("gx-overlay-card").children[0].children[0],
          change === "removed" || change === "recovery" ? "gx.group.denied" : "gx.group.stale");
      });
    }
  }
});

test("GraphExplorer.reset clears ordinary and GROUP caches, visible content and remembered selection", async (t) => {
  for (const group of [false, true]) {
    await t.test(group ? "GROUP" : "ordinary repo", () => {
      const h = harness({ withDom: true });
      const M = group ? groupModel(h) : repoModel(h);
      const graphRead = new AbortController(), sourceRead = new AbortController();
      let killed = 0, polled = false;
      h.GX.loadController = graphRead; h.GX.code.controller = sourceRead;
      h.GX.shown = true; h.GX.loading = true;
      h.GX.renderer = { kill() { killed++; } };
      h.GX.cache.set("ordinary-private-repo", { M });
      h.GX.cache.set(GID, { M });
      h.GX.code.cache.set("private-source", "old account source text");
      h.GX.code.range = { node: 0, start: 2, end: 3 };
      h.GX.code.target = { groupId: GID, sourceId: "private-source" };
      h.GX.originSource = "private-source";
      h.GX.history = [{ selected: "private-node" }];
      h.GX.view = { mode: "full", scope: { kind: "path", nodes: [0, 1] } };
      h.GX.filters.repos.add(1);
      h.GX.pollTimer = 99; h.timers.set(99, { callback: () => { polled = true; } });
      h.session.set("gfy-gx-src", M.srcId);
      h.location.hash = `#graph/${M.srcId}`;
      const privatePanels = ["gx-source", "gx-source-list", "gx-overlay-card", "gx-canvas", "gx-inspector", "gx-legend", "gx-search-results"];
      for (const id of privatePanels) h.dom.getElementById(id).append("old account private content");
      h.dom.getElementById("gx-source").value = M.srcId;
      h.dom.getElementById("gx-source-combo").value = "typed private name";
      h.dom.getElementById("gx-source-combo").placeholder = "private selected source";
      h.dom.getElementById("gx-legend-filter").value = "private search";
      h.dom.getElementById("gx").classList.add("fullscreen");
      h.body.style.overflow = "hidden";
      h.dom.getElementById("gx-search").setAttribute("aria-activedescendant", "old-private-node");

      h.S.groups = []; h.S.repos = []; h.S.servers = [];
      h.GraphExplorer.reset();
      h.GraphExplorer.reset(); // auth expiry and explicit logout may both call it
      h.hook("setWiredForTest")(true);
      h.GraphExplorer.onData(); // showGate renders once more after clearing S

      assert.ok(graphRead.signal.aborted && sourceRead.signal.aborted);
      assert.ok(h.GX.loadSeq > SEQ && h.GX.code.seq > SEQ);
      assert.equal(killed, 1);
      assert.equal(h.GX.loadController, null); assert.equal(h.GX.code.controller, null);
      assert.equal(h.GX.loading, false); assert.equal(h.GX.shown, false);
      assert.equal(h.GX.resetPending, true);
      assert.equal(h.GX.M, null); assert.equal(h.GX.graph, null); assert.equal(h.GX.info, null);
      assert.equal(h.GX.cache.size, 0); assert.equal(h.GX.code.cache.size, 0);
      assert.equal(h.GX.code.range, null); assert.equal(h.GX.code.target, null);
      assert.equal(h.GX.srcId, ""); assert.equal(h.GX.originSource, "");
      assert.equal(h.GX.history.length, 0); assert.equal(h.GX.filters.repos.size, 0);
      assert.deepEqual(clone(h.GX.view), { mode: "groups", scope: { kind: "all" } });
      assert.equal(h.session.has("gfy-gx-src"), false); assert.equal(h.location.hash, "#graph");
      assert.equal(h.GX.pollTimer, 0); assert.equal(h.timers.size, 0); assert.equal(polled, false);
      for (const id of privatePanels) assert.equal(h.dom.getElementById(id).children.length, 0, id);
      assert.equal(h.dom.getElementById("gx-source").value, "");
      assert.equal(h.dom.getElementById("gx-source-combo").value, "");
      assert.equal(h.dom.getElementById("gx-source-combo").placeholder, "");
      assert.ok(h.dom.getElementById("gx-source-combo").blurred);
      assert.equal(h.dom.getElementById("gx-source-list").hidden, true);
      assert.equal(h.dom.getElementById("gx-legend-filter").value, "");
      assert.equal(h.dom.getElementById("gx-search")["aria-activedescendant"], undefined);
      assert.equal(h.dom.getElementById("gx").classList.contains("fullscreen"), false);
      assert.equal(h.body.style.overflow, "");
      assert.equal(h.calls.length, 0);
    });
  }
});

test("GraphExplorer.reset ignores late graph metadata and final GROUP pages", async (t) => {
  for (const at of ["repo metadata", "group metadata", "group last page"]) {
    await t.test(at, async () => {
      let release, started;
      const deferred = new Promise(resolve => { release = resolve; });
      const held = new Promise(resolve => { started = resolve; });
      const h = harness({
        withDom: true, state: { repos: [{ repo_id: "backend" }] },
        api(call) {
          if (at !== "group last page" || call.url.searchParams.get("kind") === "links") {
            started(); return deferred;
          }
          return call.url.pathname.endsWith("/graph")
            ? page("nodes", graph().nodes) : { group: groupMeta() };
        },
      });
      const pending = h.loadSource(at === "repo metadata" ? "backend" : GID);
      await held;
      const oldSignal = h.GX.loadController.signal;
      h.GraphExplorer.reset();
      const sequence = h.GX.loadSeq;
      release(at === "repo metadata" ? { state: "ready", viz: { url: "https://private.invalid/data", etag: "private" } }
        : at === "group metadata" ? { group: groupMeta() } : page("links", graph().links));
      await pending;
      assert.ok(oldSignal.aborted);
      assert.equal(h.GX.loadSeq, sequence);
      assert.equal(h.GX.M, null); assert.equal(h.GX.info, null); assert.equal(h.GX.srcId, "");
      assert.equal(h.GX.cache.size, 0);
      assert.equal(h.dom.getElementById("gx-overlay-card").children.length, 0);
      assert.equal(h.calls.length, at === "group last page" ? 3 : 1);
    });
  }
});

test("GraphExplorer.reset prevents late source reads from restoring text or ordinary repo caches", async (t) => {
  for (const group of [false, true]) {
    await t.test(group ? "GROUP source read" : "ordinary repo source read", async () => {
      let release;
      const h = harness({ withDom: true, api: () => new Promise(resolve => { release = resolve; }) });
      const M = group ? groupModel(h) : repoModel(h);
      const controller = new AbortController(); h.GX.code.controller = controller;
      const pending = h.hook("readSourceResult")(M, group ? 1 : 0, "src/orders.ts", 2, 3, SEQ, controller.signal);
      const rejected = assert.rejects(pending, error => error.name === "AbortError");
      h.GraphExplorer.reset();
      release(group ? sourceResponse() : { text: "src/orders.ts lines 2-3 of 8:\n2| private source\n3| private source" });
      await rejected;
      assert.ok(controller.signal.aborted);
      assert.equal(h.GX.M, null); assert.equal(h.GX.code.cache.size, 0); assert.equal(h.GX.cache.size, 0);
      assert.equal(h.dom.getElementById("gx-inspector").children.length, 0);
      assert.equal(h.calls.length, 1);
    });
  }
});

test("GraphExplorer.reset is safe before mount and does not replace unrelated navigation", () => {
  const h = harness();
  h.location.hash = "#catalog";
  h.GraphExplorer.reset();
  assert.equal(h.location.hash, "#catalog");
  assert.equal(h.GX.M, null);
  assert.equal(h.GX.cache.size, 0);
});
