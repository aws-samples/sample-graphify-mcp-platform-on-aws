#!/usr/bin/env node
'use strict';

/**
 * Run: node tests/test_console_group_navigation.cjs [case-name-substring]
 * If Playwright is cached outside normal Node module resolution, set
 * GRAPHIFY_PLAYWRIGHT_MODULE to that existing package's absolute path.
 *
 * Uses cached Playwright and installed Chrome, without installs. Serves the
 * real HTML, styles, and groups.js byte-for-byte. Only GraphExplorer is stubbed.
 * All browser requests are intercepted; API/session fixtures are synthetic.
 * Existing SRI-pinned CDN scripts are cached in OUT after digest verification.
 * Their downloads send no credentials. No AWS calls or real credentials.
 * Each case gets a new browser context with isolated cookies and storage.
 */
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const { STATUS_CODES } = require('node:http');
const { spawnSync } = require('node:child_process');
const { chromium } = require(process.env.GRAPHIFY_PLAYWRIGHT_MODULE || 'playwright');

const ROOT = path.resolve(__dirname, '..');
const OUT = '/tmp/graphify-group-ui-navigation';
const ORIGIN = 'https://console.navigation.test';
const MCP_BASE = 'https://mcp.navigation.test';
const MODEL = 'global.anthropic.claude-sonnet-5';
const READY = 'grp_0123456789abcdef0123456789abcdef';
const DRAFT = 'grp_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb';
const STALE = 'grp_cccccccccccccccccccccccccccccccc';
const RECOVERY = 'grp_dddddddddddddddddddddddddddddddd';
const PARTIAL = 'grp_eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee';
const NORMAL = 'github.com__offline__normal-repo__main';
const GROUP_ONLY = 'files__group-only';
const REVOKED = 'files__revoked-cache';
const ISSUED_KEY = 'OFFLINE_ISSUED_VALUE_NOT_A_CREDENTIAL';
const USER_NAME = '사용자·이름—보존';
const SOURCE_NAME = '원본·이름—보존';
const REVOKED_NAME = '취소된·원본—캐시';
const QUOTE = '  인용·원문—1–2\n<svg onload="window.__navigationInjected=1">고객 "문자열"</svg>  ';
const DESCRIPTION = '  고객·설명—그대로\n둘째 줄 (범위 1–2)  ';
const SOURCE_FILE = 'docs/원문·문서—1.md';
const CLIENT_NAME = 'graphify-group-0123456789ab';
const endpoint = id => `${MCP_BASE}/mcp/${encodeURIComponent(id)}`;
const clone = value => structuredClone(value);
const digest = bytes => crypto.createHash('sha256').update(bytes).digest('hex');
const results = [], cases = [], snapshots = new Map(), thirdParty = new Map();
const testCase = (name, run) => cases.push({ name, run });
const graphStub = `
window.__navigationGraph = [];
window.GraphExplorer = {
  init() {}, reset() {}, onData() {}, onShow() {}, onHide() {},
  open(id) { window.__navigationGraph.push(id); }
};`;

function group(id, extra = {}) {
  return {
    group_id: id, server_id: id, kind: 'group', name: `Group ${id.slice(4, 8)}`,
    description: DESCRIPTION, role: 'owner', revision: 1, status: 'READY',
    data_ready: true, stale: false, llm_enabled: false, model: MODEL,
    active_revision: 1, active_version: `${id}-v1`,
    sources: [
      { source_id: NORMAL, role: 'frontend', description: DESCRIPTION },
      { source_id: GROUP_ONLY, role: 'backend', description: DESCRIPTION },
    ],
    active_source_versions: { [NORMAL]: 'normal-v1', [GROUP_ONLY]: 'only-v1' },
    mcp_url: endpoint(id), ...extra,
  };
}

function fixture() {
  const repos = [
    {
      repo_id: NORMAL, source_id: NORMAL, server_name: SOURCE_NAME,
      source_type: 'git', git_url: 'https://example.invalid/normal.git', ref: 'main',
      status: 'READY', enabled: true, manageable: false, graph_scope: 'private',
      description: DESCRIPTION, active_source_version: 'normal-v1',
      last_built_sha: 'fixture-sha', created_at: '2026-09-15T00:00:00Z',
      runtime_status: 'READY', dedicated_runtime: true, connection_available: true,
    },
    {
      repo_id: GROUP_ONLY, source_id: GROUP_ONLY, server_name: '그룹 전용 소스',
      source_type: 'files', status: 'READY', enabled: true, graph_scope: 'private',
      description: DESCRIPTION, active_source_version: 'only-v1', last_built_sha: 'fixture-sha',
      runtime_status: 'NONE', dedicated_runtime: false, connection_available: false,
    },
  ];
  const groups = [
    group(READY, { name: '준비된 한글 그룹' }),
    group(DRAFT, {
      name: '초안 그룹', status: 'DRAFT', data_ready: false, stale: true,
      active_version: null, active_revision: null,
    }),
    group(STALE, { name: '갱신할 그룹', data_ready: false, stale: true }),
    {
      group_id: RECOVERY, server_id: RECOVERY, kind: 'group', name: '복구 그룹',
      description: DESCRIPTION, revision: 1, status: 'READY', role: 'owner',
      sources: [{ source_id: REVOKED }], access_recovery: true, data_ready: false, stale: true,
    },
    group(PARTIAL, { name: '부분 완료 그룹', status: 'PARTIAL' }),
  ];
  return {
    repos, groups, groupFailures: [], groupHolds: [], requests: [], unexpected: [], external: [],
    servers: [
      { server_id: 'all', kind: 'hub', runtime_status: 'READY', mcp_url: endpoint('all') },
      ...repos.map(repo => ({
        ...repo, kind: 'repo', server_id: repo.repo_id, build_status: repo.status,
        mcp_url: endpoint(repo.repo_id),
      })),
      ...groups.filter(g => g.data_ready).map(g => ({ ...clone(g), runtime_status: 'READY' })),
    ],
  };
}

async function prepareAssets() {
  fs.mkdirSync(path.join(OUT, 'cdn'), { recursive: true });
  for (const file of ['index.html', 'groups.js', 'groups.css', 'graph.css']) {
    snapshots.set(file, fs.readFileSync(path.join(ROOT, 'console', file)));
  }
  for (const file of ['console.css', 'dialogs.js', 'dialogs.css']) {
    if (fs.existsSync(path.join(ROOT, 'console', file))) {
      snapshots.set(file, fs.readFileSync(path.join(ROOT, 'console', file)));
    }
  }
  const scripts = [...snapshots.get('index.html').toString('utf8').matchAll(/<script\b([^>]+)>/g)].flatMap(([, attrs]) => {
    const url = attrs.match(/\bsrc="(https:[^"]+)"/)?.[1];
    const integrity = attrs.match(/\bintegrity="([^"]+)"/)?.[1];
    return url ? [{ url, integrity }] : [];
  });
  await Promise.all(scripts.map(async ({ url, integrity }) => {
    assert.ok(['cdnjs.cloudflare.com', 'cdn.jsdelivr.net'].includes(new URL(url).hostname), `Unapproved CDN: ${url}`);
    assert.match(integrity || '', /^sha384-[A-Za-z0-9+/=]+$/, `Missing pinned integrity: ${url}`);
    const cache = path.join(OUT, 'cdn', `${digest(url).slice(0, 16)}.js`);
    let bytes;
    if (fs.existsSync(cache)) bytes = fs.readFileSync(cache);
    else {
      const response = await fetch(url, { credentials: 'omit', redirect: 'error', signal: AbortSignal.timeout(20000) });
      assert.equal(response.status, 200, `CDN download: ${url}`);
      bytes = Buffer.from(await response.arrayBuffer());
    }
    assert.equal(`sha384-${crypto.createHash('sha384').update(bytes).digest('base64')}`, integrity, `CDN integrity: ${url}`);
    fs.writeFileSync(cache, bytes);
    thirdParty.set(url, bytes);
  }));
}

function relation(g) {
  return {
    id: 'fixture-relation', source: 'opaque:source', target: 'opaque:target',
    relation: 'shared_contract', evidence_kind: 'EXTRACTED', method: 'explicit_reference',
    evidence: g.sources.slice(0, 2).map(s => ({
      source_id: s.source_id, source_version: g.active_source_versions[s.source_id],
      file: SOURCE_FILE, line_start: 1, line_end: 2, quote: QUOTE,
    })),
  };
}

function observeResponse(page, predicate) {
  const response = page.waitForResponse(predicate);
  // These promises are armed before UI actions and awaited after deliberate
  // response holds. Preserve rejection for that await without letting an
  // early timeout abort the entire Node process outside the case reporter.
  response.catch(() => {});
  return response;
}

async function openFixture(browser, data = fixture(), viewport = { width: 1440, height: 1100 }) {
  const context = await browser.newContext({ viewport, reducedMotion: 'reduce', serviceWorkers: 'block' });
  const page = await context.newPage();
  page.setDefaultTimeout(30000);
  const jsErrors = [], consoleErrors = [], expectedHttpErrors = [], screenshots = [];
  page.on('pageerror', error => jsErrors.push(error.stack || error.message));
  page.on('console', message => {
    if (message.type() === 'error') consoleErrors.push({ text: message.text(), location: message.location() });
  });
  await context.addInitScript(() => {
    localStorage.setItem('gfy-lang', 'ko');
    sessionStorage.setItem('gfy-access', 'offline-navigation-session');
    sessionStorage.setItem('gfy-exp', String(Date.now() + 3600000));
    // Synthetic, unsigned payload. This never authenticates with a service.
    sessionStorage.setItem('gfy-id', `e30.${btoa(JSON.stringify({ email: 'offline-navigation@example.invalid' }))}.offline`);
  });
  const respond = async (route, body, status = 200) => {
    if (status >= 400) expectedHttpErrors.push({ url: route.request().url(), status });
    await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
  };
  await context.route('**/*', async route => {
    const request = route.request(), url = new URL(request.url()), method = request.method();
    if (thirdParty.has(url.href)) return route.fulfill({
      contentType: 'application/javascript', headers: { 'access-control-allow-origin': '*' }, body: thirdParty.get(url.href),
    });
    if (url.hostname === 'fonts.googleapis.com') return route.fulfill({ contentType: 'text/css', body: '' });
    if (url.origin !== ORIGIN) {
      data.external.push(url.href);
      return route.abort('blockedbyclient');
    }
    const asset = url.pathname === '/' ? 'index.html' : url.pathname.slice(1);
    if (snapshots.has(asset)) return route.fulfill({
      contentType: asset.endsWith('.html') ? 'text/html' : asset.endsWith('.css') ? 'text/css' : 'application/javascript',
      body: snapshots.get(asset),
    });
    if (asset === 'graph.js') return route.fulfill({ contentType: 'application/javascript', body: graphStub });
    if (asset === 'config.json') return respond(route, {
      apiBase: `${ORIGIN}/api`, mcpBase: MCP_BASE, playgroundStreamUrl: `${ORIGIN}/pgstream`,
      cognitoDomain: `${ORIGIN}/unused-auth`, clientId: 'offline-client', playgroundModels: [MODEL],
    });
    const body = request.postData() ? JSON.parse(request.postData()) : null;
    const pathname = url.pathname.replace(/^\/api/, '');
    data.requests.push({ method, path: pathname, query: url.search, body });
    if (pathname === '/me') return respond(route, data.me || {
      sub: 'offline-user', username: USER_NAME, is_admin: false, llm_models: [MODEL], llm_default_model: MODEL,
    });
    if (method === 'GET' && pathname === '/repos') return respond(route, { repos: data.repos });
    if (method === 'GET' && pathname === '/servers') return respond(route, { servers: data.servers });
    if (method === 'GET' && pathname === '/groups') {
      const status = data.groupFailures.shift();
      const response = status ? { error: 'Offline group list failure' } : { groups: clone(data.groups) };
      const hold = data.groupHolds.shift();
      if (hold) { hold.started(); await hold.gate; }
      return respond(route, response, status || 200);
    }
    if (method === 'GET' && pathname === '/keys') return respond(route, { keys: data.keys || [] });
    if (method === 'POST' && pathname === '/keys') return respond(route, { api_key: ISSUED_KEY });
    if (pathname === '/usage') return respond(route, { month: '2026-09', keys: {} });
    if (pathname === '/catalog') return respond(route, { servers: data.catalog || [] });
    if (method === 'GET' && pathname === '/admin/users') return respond(route, { users: data.users || [] });
    const match = pathname.match(/^\/groups\/([^/]+)(?:\/(graph|source|members))?$/);
    if (match) {
      const g = data.groups.find(item => item.group_id === match[1]);
      if (!g) return respond(route, { error: 'Offline access denied' }, 403);
      if (!match[2] && method === 'GET') return respond(route, { group: g });
      if (match[2] === 'graph') return respond(route, {
        group_id: g.group_id, version: g.active_version, revision: g.revision,
        sources: g.sources.map(s => ({ source_id: s.source_id, source_version: g.active_source_versions[s.source_id] })),
        relations: [relation(g)], next_offset: null, status: g.status,
      });
      if (match[2] === 'source' && method === 'POST') return respond(route, {
        ...body, source_version: g.active_source_versions[body.source_id], text: QUOTE,
      });
      if (match[2] === 'members') return respond(route, { group_id: g.group_id, revision: 1, members: [] });
    }
    if (pathname === '/pgstream' && method === 'POST' && body.op === 'mcp') return route.fulfill({
      contentType: 'text/event-stream',
      body: `data: ${JSON.stringify({
        type: 'mcp', ok: true, status: 200,
        body: { jsonrpc: '2.0', id: 1, result: { tools: [{ name: 'query_graph', description: 'Offline tool fixture', inputSchema: { type: 'object', properties: {} } }] } },
      })}\n\n`,
    });
    data.unexpected.push({ method, pathname, body });
    return respond(route, { error: `Unexpected fixture request: ${method} ${pathname}` }, 501);
  });
  const screenshot = async name => {
    const file = `${name}.png`;
    await page.screenshot({ path: path.join(OUT, file), fullPage: true, animations: 'disabled' });
    screenshots.push(file);
  };
  const bootStarted = Date.now();
  let bootDuration;
  try {
    await page.bringToFront();
    await page.goto(ORIGIN, { waitUntil: 'load' });
    // Cold browser contexts can spend several seconds fulfilling the initial
    // metadata requests on a busy workstation. Readiness still requires both
    // application metadata and the authenticated surface.
    await page.waitForFunction(() => !!window.SourceGroups && !!S.me && !document.getElementById('app').hidden,
      undefined, { polling: 50, timeout: 30000 });
    bootDuration = Date.now() - bootStarted;
    await page.locator('nav.tabs [data-tab="repos"]').click();
  } catch (error) {
    fs.writeFileSync(path.join(OUT, "boot-failure.json"), JSON.stringify({
      error: error.message, jsErrors, consoleErrors, unexpected: data.unexpected,
      external: data.external, requests: data.requests,
      state: await page.evaluate(() => ({
        groupsModule: !!window.SourceGroups, me: typeof S === "undefined" ? null : S.me,
        appHidden: document.getElementById("app")?.hidden, gateHidden: document.getElementById("gate")?.hidden,
      })).catch(() => null),
    }, null, 2));
    await page.screenshot({ path: path.join(OUT, "boot-failure.png"), fullPage: true }).catch(() => {});
    await context.close();
    throw error;
  }
  return {
    page, context, data, screenshots, screenshot,
    diagnostics: () => ({ bootDuration, jsErrors, consoleErrors, expectedHttpErrors, unexpected: data.unexpected, external: data.external, requests: data.requests }),
    assertClean() {
      assert.deepEqual(jsErrors, [], 'Uncaught browser JavaScript errors');
      const unexpectedErrors = consoleErrors.filter(error => !expectedHttpErrors.some(expected =>
        error.location.url === expected.url &&
        error.text === `Failed to load resource: the server responded with a status of ${expected.status} (${STATUS_CODES[expected.status] || ''})`));
      assert.deepEqual(unexpectedErrors, [], 'Unexpected browser console errors');
      assert.deepEqual(data.unexpected, [], 'Unmocked application requests');
      assert.deepEqual(data.external, [], 'Unexpected outbound requests');
    },
  };
}

const row = (page, id) => page.locator(`[data-source-group="${id}"]`);
async function assertVisible(locator) { assert.equal(await locator.isVisible(), true, `Expected visible: ${locator}`); }
async function assertHidden(locator) { assert.equal(await locator.isVisible(), false, `Expected hidden: ${locator}`); }
async function openGuide(page, id = READY) {
  await page.evaluate(id => openConnectionGuide(id), id);
  await assertVisible(page.locator('#connection-guide'));
}
async function nativeValue(page, id) { return page.locator(`#${id}`).evaluate(element => element.value); }
async function dismissPanel(page) {
  const panel = page.locator('dialog.console-panel-dialog[open]');
  if (await panel.count()) {
    await panel.locator('[data-panel-close]').first().click();
    await panel.waitFor({ state: 'hidden' });
  }
}
async function navigateFixture(page, name) {
  await dismissPanel(page);
  await page.locator(`nav.tabs [data-tab="${name}"]`).click();
}
async function interactConfirmation(page, trigger, { accept = true, value } = {}) {
  // Support both remaining native callers and the production HTML dialog.
  const html = page.locator('dialog.console-dialog:not(.console-panel-dialog)[open]');
  let nativeListener;
  const native = new Promise(resolve => {
    nativeListener = dialog => resolve({ kind: 'native', dialog });
    page.once('dialog', nativeListener);
  });
  const rendered = html.waitFor({ state: 'visible', timeout: 30000 }).then(() => ({ kind: 'html' }));
  const click = Promise.resolve().then(trigger);
  const failedTrigger = click.then(() => new Promise(() => {}));
  try {
    const opened = await Promise.race([native, rendered, failedTrigger]);
    let message;
    if (opened.kind === 'native') {
      message = opened.dialog.message();
      if (accept) await opened.dialog.accept(value); else await opened.dialog.dismiss();
    } else {
      message = await html.textContent();
      if (accept && value !== undefined) await html.locator('.console-dialog__input').fill(value);
      await html.locator(accept ? '.console-dialog__submit' : '.console-dialog__cancel').click();
      await html.waitFor({ state: 'hidden', timeout: 30000 });
    }
    await click;
    return { message, kind: opened.kind };
  } finally {
    page.off('dialog', nativeListener);
  }
}
function shellArguments(command) {
  // No real CLI can contact a service. Injection fixtures use harmless printf
  // probes; verify the exact POSIX argv, including apostrophes and whitespace.
  const result = spawnSync('/bin/sh', ['-c', 'claude() { printf "%s\\000" "$@"; }\n' + command], {
    env: { PATH: '/nonexistent-navigation-tools' }, encoding: 'utf8', timeout: 3000,
  });
  assert.equal(result.status, 0, result.stderr);
  assert.equal(result.stderr, '');
  return result.stdout.split('\0').slice(0, -1);
}

function holdGroups(data) {
  let started, release;
  const seen = new Promise(resolve => { started = resolve; });
  const gate = new Promise(resolve => { release = resolve; });
  data.groupHolds.push({ started, gate });
  return { seen, release };
}
async function assertConfigs(page, id, name, ascii = false) {
  assert.equal(await nativeValue(page, 'connection-target'), id);
  for (const client of ['claude', 'cursor', 'vscode']) {
    await page.locator(`#connection-tab-${client}`).click();
    const config = await page.locator('#connection-config').textContent();
    assert.ok(config.includes('<YOUR_KEY>'), `${client} needs a key placeholder`);
    assert.ok(!config.includes(ISSUED_KEY), 'Guide must not embed an issued key');
    assert.ok(!config.includes('/mcp/all'), `${client} must not use the hub`);
    if (ascii) assert.match(config, /^[\x00-\x7f]+$/, `${client} group config must use ASCII identifiers`);
    if (client === 'claude') assert.deepEqual(shellArguments(config), [
      'mcp', 'add', '--transport', 'http', name, endpoint(id), '--header', 'X-Graphify-Key: <YOUR_KEY>',
    ]);
    else {
      const servers = JSON.parse(config)[client === 'cursor' ? 'mcpServers' : 'servers'];
      assert.deepEqual(Object.keys(servers), [name]);
      assert.equal(servers[name].url, endpoint(id));
      assert.deepEqual(servers[name].headers, { 'X-Graphify-Key': '<YOUR_KEY>' });
      if (client === 'vscode') assert.equal(servers[name].type, 'http');
    }
  }
}
async function refreshGroups(page) { await page.evaluate(() => refreshSourceGroups()); }
async function unavailablePickers(page, id) {
  for (const selectId of ['key-scope', 'play-server']) {
    const selected = await page.locator(`#${selectId}`).evaluate(element => ({
      value: element.value, disabled: element.selectedOptions[0]?.disabled,
    }));
    assert.equal(selected.value, id, `${selectId} must retain the removed group instead of ALL`);
    assert.equal(selected.disabled, true, `${selectId} must mark the removed group unavailable`);
  }
}

testCase('My Sources uses fresh groups, gates actions, and collapses members', async h => {
  const { page, data } = h;
  assert.ok(data.requests.some(request => request.method === 'GET' && request.path === '/groups'));
  assert.equal(await page.locator('#my-source-groups [data-source-group]').count(), 5);
  for (const id of [READY, DRAFT, STALE, RECOVERY, PARTIAL]) {
    const card = row(page, id), enabled = [READY, PARTIAL].includes(id);
    await assertVisible(card);
    assert.equal(await card.getByRole('button', { name: '전체 그래프', exact: true }).isEnabled(), enabled);
    assert.equal(await card.getByRole('button', { name: '연동 가이드', exact: true }).isEnabled(), enabled);
    assert.equal(await card.getByRole('button', { name: '그룹 상세', exact: true }).isEnabled(), true);
    assert.equal(await card.locator('details').evaluate(details => details.open), false);
    await assertHidden(card.locator('.source-group-members'));
  }
  await row(page, READY).locator('summary').click();
  await assertVisible(row(page, READY).locator('.source-group-members'));
  assert.equal(await row(page, READY).locator('.source-group-member').count(), 2);
  await refreshGroups(page);
  assert.equal(await row(page, READY).locator('details').evaluate(details => details.open), true);
});

testCase('My Sources and group detail graph actions route to GraphExplorer', async h => {
  const { page } = h;
  await row(page, READY).getByRole('button', { name: '전체 그래프', exact: true }).click();
  await assertVisible(page.locator('#page-graph'));
  assert.deepEqual(await page.evaluate(() => window.__navigationGraph), [READY]);
  await navigateFixture(page, 'repos');
  await row(page, READY).getByRole('button', { name: '그룹 상세', exact: true }).click();
  await page.locator('#sg-meta [data-i18n="sg.fullGraph"]').waitFor({ state: 'visible' });
  await assertVisible(page.locator('#sg-graph-section'));
  await page.locator('#sg-meta [data-i18n="sg.fullGraph"]').click();
  await assertVisible(page.locator('#page-graph'));
  assert.deepEqual(await page.evaluate(() => window.__navigationGraph), [READY, READY]);
});

testCase('group detail and MCP row open the shared scoped connection guide', async h => {
  const { page } = h;
  await row(page, READY).getByRole('button', { name: '그룹 상세', exact: true }).click();
  await page.locator('#sg-meta [data-i18n="sg.guide"]').waitFor({ state: 'visible' });
  await page.locator('#sg-meta [data-i18n="sg.guide"]').click();
  await assertVisible(page.locator('#page-servers'));
  await assertConfigs(page, READY, CLIENT_NAME, true);
  await navigateFixture(page, 'groups');
  const card = page.locator('#sg-list .sg-list-card').filter({ hasText: READY });
  await card.locator('[data-i18n="sg.guide"]').click();
  assert.equal(await nativeValue(page, 'connection-target'), READY);
});

testCase('ordinary dedicated READY server uses the same three-client guide', async h => {
  const { page } = h;
  await navigateFixture(page, 'servers');
  const card = page.locator('#servers-list > section').filter({ hasText: NORMAL });
  await card.getByRole('button', { name: '연동 가이드', exact: true }).click();
  await assertConfigs(page, NORMAL, SOURCE_NAME);
});

testCase('group-only source hides invalid endpoints and offers only ready alternatives', async h => {
  const { page } = h;
  const source = page.locator('#repos-tbody tr').filter({ hasText: '그룹 전용 소스' });
  await source.getByRole('button', { name: '연동 가이드', exact: true }).click();
  const guide = page.locator('#connection-content');
  assert.ok((await guide.textContent()).startsWith(await page.evaluate(() => t('mygroups.groupOnlyHint'))));
  assert.equal(await guide.locator('.snippet, pre, #connection-target').count(), 0);
  assert.ok(!(await guide.textContent()).includes(endpoint(GROUP_ONLY)));
  assert.deepEqual((await guide.getByRole('button').allTextContents()).sort(), ['부분 완료 그룹', '준비된 한글 그룹'].sort());
  const available = await page.evaluate(() => connectableServers().map(server => server.server_id));
  assert.deepEqual(available.sort(), ['all', NORMAL, READY, PARTIAL].sort());
  const card = page.locator('#servers-list > section').filter({ hasText: GROUP_ONLY });
  assert.equal(await card.count(), 1, 'Retain the group-only source card');
  assert.equal(await card.locator('.snippet').count(), 0);
  await guide.getByRole('button', { name: '준비된 한글 그룹', exact: true }).click();
  assert.equal(await nativeValue(page, 'connection-target'), READY);
});

testCase('dedicated_runtime false and runtime NONE independently exclude endpoints', async h => {
  const { page, data } = h;
  for (const [flag, value] of [['dedicated_runtime', false], ['runtime_status', 'NONE'], ['connection_available', false]]) {
    const server = data.servers.find(s => s.server_id === NORMAL);
    Object.assign(server, { dedicated_runtime: true, runtime_status: 'READY', connection_available: true, [flag]: value });
    await page.evaluate(() => refreshAll());
    assert.equal(await page.evaluate(id => connectableServers().some(s => s.server_id === id), NORMAL), false, flag);
  }
});

testCase('Korean, spaces and injection group names keep stable ASCII client configs', async h => {
  const { page, data } = h;
  for (const name of ['한글 프로젝트 그룹', 'A group with spaces', `x'; printf SHELL_INJECTION; # $(printf SUBSTITUTION) <img src=x onerror="window.__navigationInjected=1">`]) {
    data.groups.find(g => g.group_id === READY).name = name;
    await refreshGroups(page);
    assert.equal(await row(page, READY).locator('strong').textContent(), name);
    await openGuide(page);
    await assertConfigs(page, READY, CLIENT_NAME, true);
    assert.equal(await page.evaluate(() => window.__navigationInjected || 0), 0);
    assert.equal(await page.locator('#connection-content img, #my-source-groups img').count(), 0);
  }
});

testCase('Claude command preserves POSIX apostrophes and does not evaluate substitutions', async h => {
  const name = `name ' with spaces $(printf SUBSTITUTION); printf INJECTION`;
  const url = `https://endpoint.invalid/path'quote/mcp/group`;
  const placeholder = `<YOUR_KEY> ' $(printf SUBSTITUTION)`;
  const command = await h.page.evaluate(({ name, url, placeholder }) => claudeCmd(url, placeholder, name), { name, url, placeholder });
  assert.deepEqual(shellArguments(command), [
    'mcp', 'add', '--transport', 'http', name, url, '--header', `X-Graphify-Key: ${placeholder}`,
  ]);
});

testCase('key issue preselects the exact group and configs never expose the issued fixture value', async h => {
  const { page, data } = h;
  await openGuide(page);
  await page.locator('#connection-content').getByRole('button', { name: '이 서버용 키 발급', exact: true }).click();
  await assertVisible(page.locator('#page-keys'));
  assert.equal(await nativeValue(page, 'key-scope'), READY);
  assert.match(await page.locator('#key-scope-combo').inputValue(), /준비된 한글 그룹/);
  await page.locator('#key-name').fill('Offline group key');
  const posted = observeResponse(page, response => response.url() === `${ORIGIN}/api/keys` && response.request().method() === 'POST');
  await page.locator('#btn-key-create').click();
  await posted;
  assert.deepEqual(data.requests.filter(r => r.path === '/keys' && r.method === 'POST').map(r => r.body.scope), [[READY]]);
  await page.locator('#modal-backdrop.open').waitFor({ state: 'visible' });
  assert.equal(await page.locator('#modal-backdrop').evaluate(dialog => dialog instanceof HTMLDialogElement && dialog.open), true);
  await page.locator('#btn-modal-close').click();
  assert.equal(await page.locator('#modal-backdrop').evaluate(dialog => dialog.open), false);
  assert.equal(await page.locator('#modal-key').textContent(), '');
  assert.equal(await page.locator('#modal-snippet').textContent(), '');
  await page.locator('#btn-key-open').click();
  await page.locator('#btn-key-create').click();
  await page.locator('#modal-backdrop.open').waitFor({ state: 'visible' });
  await page.keyboard.press('Escape');
  await page.locator('#modal-backdrop').waitFor({ state: 'hidden' });
  assert.equal(await page.locator('#modal-key').textContent(), '');
  assert.equal(await page.locator('#modal-snippet').textContent(), '');
  assert.ok(data.requests.filter(r => r.path === '/keys' && r.method === 'POST').every(r =>
    Array.isArray(r.body.scope) && r.body.scope.length === 1 && r.body.scope[0] === READY));
  await openGuide(page);
  await assertConfigs(page, READY, CLIENT_NAME, true);
});

testCase('Playground guide action selects and queries the same group', async h => {
  const { page, data } = h;
  await openGuide(page);
  await page.locator('#connection-content').getByRole('button', { name: '이 대상으로 플레이그라운드 열기', exact: true }).click();
  await assertVisible(page.locator('#page-play'));
  await page.waitForFunction(id => PS.toolsFor === id, READY);
  assert.equal(await nativeValue(page, 'play-server'), READY);
  assert.match(await page.locator('#play-server-combo').inputValue(), /준비된 한글 그룹/);
  assert.deepEqual(data.requests.filter(r => r.path === '/pgstream').map(r => r.body.server_id), [READY]);
});

testCase('group loading failure shows error and retry instead of an empty list', async h => {
  const { page, data } = h;
  data.groupFailures.push(503);
  await page.locator('#btn-repos-refresh').click();
  await page.locator('#my-source-groups [role="alert"]').waitFor({ state: 'visible' });
  assert.match(await page.locator('#my-source-groups').textContent(), /그룹 목록을 불러오지 못했습니다/);
  assert.ok(!(await page.locator('#my-source-groups').textContent()).includes('아직 참여한 소스 그룹'));
  await page.locator('#my-source-groups').getByRole('button', { name: '새로고침', exact: true }).click();
  await row(page, READY).waitFor({ state: 'visible' });
  assert.equal(await page.locator('#my-source-groups [role="alert"]').count(), 0);
});

testCase('pending groups outage invalidates pickers and guides without restoring stale server groups', async h => {
  const { page, data } = h;
  await page.evaluate(id => { issueTargetKey(id); openTargetPlayground(id); openConnectionGuide(id); }, READY);
  await page.waitForFunction(id => PS.toolsFor === id, READY);
  data.groupFailures.push(503);
  const held = holdGroups(data);
  await page.evaluate(() => { window.__pendingNavigationRefresh = refreshSourceGroups(); });
  await held.seen;
  assert.equal(await nativeValue(page, 'key-scope'), READY);
  assert.equal(await nativeValue(page, 'play-server'), READY);
  held.release();
  await page.evaluate(() => window.__pendingNavigationRefresh);
  await unavailablePickers(page, READY);
  assert.equal(await page.locator('#connection-config').count(), 0);
  assert.equal(await page.locator('#servers-list > section').filter({ hasText: READY }).count(), 0);
  assert.equal(await page.evaluate(id => connectableServers().some(s => s.server_id === id), READY), false);
  await navigateFixture(page, 'repos');
  await assertVisible(page.locator('#my-source-groups [role="alert"]'));
  await page.locator('#my-source-groups').getByRole('button', { name: '새로고침', exact: true }).click();
  await row(page, READY).waitFor({ state: 'visible' });
  assert.equal(await nativeValue(page, 'key-scope'), READY);
  assert.equal(await page.locator('#key-scope').evaluate(select => select.selectedOptions[0].disabled), false);
});

testCase('removed group selection stays unavailable and cannot issue an ALL key', async h => {
  const { page, data } = h;
  await page.evaluate(id => { issueTargetKey(id); openTargetPlayground(id); openConnectionGuide(id); }, READY);
  await page.waitForFunction(id => PS.toolsFor === id, READY);
  data.groups = data.groups.filter(g => g.group_id !== READY);
  await refreshGroups(page);
  await unavailablePickers(page, READY);
  assert.equal(await page.locator('#connection-config').count(), 0);
  assert.equal(await page.locator('#servers-list > section').filter({ hasText: READY }).count(), 0);
  await navigateFixture(page, 'keys');
  await page.locator('#btn-key-open').click();
  await page.locator('#btn-key-create').click();
  assert.equal(data.requests.filter(r => r.path === '/keys' && r.method === 'POST').length, 0);
  await navigateFixture(page, 'play');
  assert.equal(await nativeValue(page, 'play-server'), READY);
  assert.ok(data.requests.filter(r => r.path === '/pgstream').every(r => r.body.server_id === READY));
});

testCase('unavailable group entry guards cannot expose ALL key issuance or query the hub', async h => {
  const { page, data } = h;
  data.groups = data.groups.filter(g => g.group_id !== READY);
  await refreshGroups(page);
  const originalScope = await nativeValue(page, 'key-scope');
  await page.evaluate(id => issueTargetKey(id), READY);
  await assertHidden(page.locator('#page-keys'));
  assert.equal(await nativeValue(page, 'key-scope'), originalScope, 'A rejected entry must not change the previous selection');
  await assertVisible(page.locator('#flash.err'));
  await page.evaluate(id => openTargetPlayground(id), READY);
  await assertHidden(page.locator('#page-play'));
  assert.equal(data.requests.filter(r => r.path === '/keys' && r.method === 'POST').length, 0);
  assert.equal(data.requests.filter(r => r.path === '/pgstream').length, 0);
});

testCase('late groups failure cannot overwrite a newer successful refresh', async h => {
  const { page, data } = h;
  data.groupFailures.push(503);
  const held = holdGroups(data);
  await page.evaluate(() => { window.__pendingNavigationRefresh = refreshSourceGroups(); });
  await held.seen;
  data.groups.find(g => g.group_id === READY).name = 'Newest authoritative group';
  await refreshGroups(page);
  held.release();
  await page.evaluate(() => window.__pendingNavigationRefresh);
  assert.equal(await row(page, READY).locator('strong').textContent(), 'Newest authoritative group');
  assert.equal(await page.locator('#my-source-groups [role="alert"]').count(), 0);
});

testCase('language changes preserve the guide client tab and collapsed member state', async h => {
  const { page } = h;
  await row(page, READY).locator('summary').click();
  await openGuide(page);
  await page.locator('#connection-tab-vscode').click();
  const config = await page.locator('#connection-config').textContent();
  await dismissPanel(page);
  await page.locator('#lang-en').click();
  await openGuide(page);
  assert.equal(await page.locator('#connection-tab-vscode').getAttribute('aria-selected'), 'true');
  assert.equal(await page.locator('#connection-config').textContent(), config);
  assert.equal(await page.locator('#connection-title').textContent(), 'Connection guide');
  assert.equal(await row(page, READY).locator('details').evaluate(details => details.open), true);
  assert.equal(await row(page, DRAFT).locator('details').evaluate(details => details.open), false);
  await dismissPanel(page);
  await page.locator('#lang-ko').click();
  await openGuide(page);
  assert.equal(await page.locator('#connection-tab-vscode').getAttribute('aria-selected'), 'true');
});

testCase('connection client tabs support arrow, Home and End keys with focus', async h => {
  const { page } = h;
  await openGuide(page);
  await page.locator('#connection-tab-claude').focus();
  for (const [key, expected] of [['ArrowRight', 'cursor'], ['End', 'vscode'], ['Home', 'claude'], ['ArrowLeft', 'vscode']]) {
    await page.keyboard.press(key);
    const tab = page.locator(`#connection-tab-${expected}`);
    assert.equal(await tab.getAttribute('aria-selected'), 'true');
    assert.equal(await tab.evaluate(element => document.activeElement === element), true);
    assert.equal(await page.locator('#connection-config').getAttribute('aria-labelledby'), `connection-tab-${expected}`);
  }
});

testCase('customer descriptions, exact quotations and original source payloads preserve every byte', async h => {
  const { page } = h;
  assert.equal(await row(page, READY).locator(':scope > p.hint').first().textContent(), DESCRIPTION);
  await row(page, READY).getByRole('button', { name: '그룹 상세', exact: true }).click();
  await page.locator('.sg-relation').first().waitFor({ state: 'visible' });
  assert.equal(await page.locator('#sg-meta > .sg-description').textContent(), DESCRIPTION);
  await page.locator('.sg-relation').first().click();
  const quotes = await page.locator('.sg-quote').allTextContents();
  assert.deepEqual(quotes, [QUOTE, QUOTE]);
  await page.locator('#sg-evidence [data-i18n="sg.sourceRead"]').first().click();
  await page.locator('#sg-source .sg-pre').waitFor({ state: 'visible' });
  assert.equal(await page.locator('#sg-source .sg-pre').textContent(), QUOTE);
  assert.equal(await page.locator('.sg-quote svg, #sg-source .sg-pre svg').count(), 0);
  assert.equal(await page.evaluate(() => window.__navigationInjected || 0), 0);
  await page.locator('#lang-en').click();
  assert.equal(await page.locator('#sg-source .sg-pre').textContent(), QUOTE);
  assert.deepEqual(await page.locator('.sg-quote').allTextContents(), [QUOTE, QUOTE]);
});

testCase('recovery members do not restore source metadata from an older source cache', async h => {
  const { page, data } = h;
  // Source access was valid when /repos was read. A later groups-only response
  // revokes it without refreshing the cached /repos snapshot.
  data.repos.push({
    ...clone(data.repos[1]), repo_id: REVOKED, source_id: REVOKED, server_name: REVOKED_NAME,
    description: 'Previously readable private source description',
  });
  data.groups = data.groups.map(g => g.group_id === RECOVERY
    ? group(RECOVERY, { name: '복구 그룹', sources: [{ source_id: REVOKED, role: 'backend' }] }) : g);
  await page.evaluate(() => refreshAll());
  data.groups = data.groups.map(g => g.group_id === RECOVERY ? {
    group_id: RECOVERY, name: '복구 그룹', role: 'owner', revision: 1, status: 'READY',
    access_recovery: true, stale: true, data_ready: false, sources: [{ source_id: REVOKED }],
  } : g);
  await refreshGroups(page);
  await row(page, RECOVERY).locator('summary').click();
  const members = await row(page, RECOVERY).locator('.source-group-members').textContent();
  assert.ok(members.includes(REVOKED));
  assert.ok(!members.includes(REVOKED_NAME), 'Recovery must not restore a revoked source name from S.repos');
  assert.ok(!members.includes('READY'), 'Recovery must not expose cached source status');
});

testCase('detail actions remain disabled for draft, stale and recovery groups', async h => {
  const { page } = h;
  for (const id of [DRAFT, STALE, RECOVERY]) {
    await navigateFixture(page, 'repos');
    await row(page, id).getByRole('button', { name: '그룹 상세', exact: true }).click();
    await page.waitForFunction(id => document.querySelector('#sg-meta > .sg-id')?.textContent === id, id);
    assert.equal(await page.locator('#sg-meta [data-i18n="sg.fullGraph"]').isDisabled(), true);
    assert.equal(await page.locator('#sg-meta [data-i18n="sg.guide"]').isDisabled(), true);
  }
  assert.deepEqual(await page.evaluate(() => window.__navigationGraph), []);
});

testCase('platform wording excludes middle dots and em dashes without modifying customer text', async h => {
  const { page } = h;
  const translations = await page.evaluate(() => Object.entries(I18N).flatMap(([lang, words]) =>
    Object.entries(words).filter(([, value]) => /[·—]/u.test(value)).map(([key, value]) => ({ lang, key, value }))));
  assert.deepEqual(translations, [], 'Platform-authored translation strings');
  const customerStrings = [USER_NAME, SOURCE_NAME, REVOKED_NAME, QUOTE, DESCRIPTION, SOURCE_FILE];
  for (const lang of ['ko', 'en']) {
    await dismissPanel(page);
    await page.locator(`#lang-${lang}`).click();
    await openGuide(page, NORMAL);
    const violations = await page.evaluate(customerStrings => {
      const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
      const found = [];
      for (let node = walker.nextNode(); node; node = walker.nextNode()) {
        const parent = node.parentElement;
        if (!parent || parent.closest('script, style') || !parent.getClientRects().length) continue;
        let platformText = node.nodeValue;
        // Check a copy of the text, subtracting exact customer fixtures only.
        // No production text, response, label, quote, or payload is rewritten.
        for (const value of customerStrings) platformText = platformText.split(value).join('');
        if (/[·—]/u.test(platformText)) found.push({ tag: parent.tagName, text: node.nodeValue });
      }
      return found;
    }, customerStrings);
    assert.deepEqual(violations, [], `${lang} visible platform wording`);
    assert.ok((await page.locator('#connection-config').textContent()).includes(SOURCE_NAME));
  }
});

for (const [device, viewport] of [
  ['desktop', { width: 1440, height: 1100 }],
  ['mobile', { width: 390, height: 844 }],
]) {
  testCase(`${device} screenshots preserve usable group and guide layouts`, async h => {
    const { page } = h;
    const rootWidths = h.layoutSamples = [];
    const recordRootWidth = async surface => rootWidths.push({
      surface, viewportWidth: viewport.width,
      ...await page.evaluate(() => ({
        scrollWidth: document.documentElement.scrollWidth,
        bodyScrollWidth: document.body.scrollWidth,
      })),
    });
    await page.setViewportSize(viewport);
    await row(page, READY).locator('summary').click();
    await h.screenshot(`${device}-my-sources`);
    await recordRootWidth('my-sources');
    const cardBounds = await row(page, READY).evaluate(card => ({ left: card.getBoundingClientRect().left, right: card.getBoundingClientRect().right }));
    assert.ok(cardBounds.left >= 0 && cardBounds.right <= viewport.width + 1, JSON.stringify(cardBounds));
    if (device === 'mobile') {
      if (await page.locator('#btn-register-open').count()) {
        await page.locator('#btn-register-open').click();
      }
      const tip = page.locator('label[for="reg-name-git"] .tip');
      assert.equal(await tip.evaluate(element => getComputedStyle(element, '::after').display), 'none');
      await page.keyboard.press('Tab');
      await tip.focus();
      assert.equal(await tip.evaluate(element => element.matches(':focus-visible')), true);
      assert.equal(await tip.evaluate(element => getComputedStyle(element, '::after').display), 'block');
      await recordRootWidth('my-sources-tooltip-focused');
      await h.screenshot('mobile-my-sources-tooltip-focused');
      await tip.evaluate(element => element.blur());
      await dismissPanel(page);
    }
    await row(page, READY).getByRole('button', { name: '연동 가이드', exact: true }).click();
    await page.locator('#connection-tab-vscode').click();
    await h.screenshot(`${device}-connection-guide`);
    await recordRootWidth('connection-guide');
    const configBounds = await page.locator('#connection-config').evaluate(config => ({
      left: config.getBoundingClientRect().left, right: config.getBoundingClientRect().right,
      width: config.clientWidth, scrollWidth: config.scrollWidth,
    }));
    assert.ok(configBounds.left >= 0 && configBounds.right <= viewport.width + 1, JSON.stringify(configBounds));
    assert.ok(configBounds.scrollWidth <= configBounds.width + 1, 'Guide config should wrap on mobile');
    await navigateFixture(page, 'repos');
    await row(page, READY).getByRole('button', { name: '그룹 상세', exact: true }).click();
    await page.locator('.sg-relation').first().waitFor({ state: 'visible' });
    await page.locator('.sg-relation').first().click();
    await h.screenshot(`${device}-group-detail-evidence`);
    assert.deepEqual(await page.locator('.sg-quote').allTextContents(), [QUOTE, QUOTE]);
    assert.deepEqual(rootWidths.filter(sample => sample.scrollWidth > sample.viewportWidth), [],
      `${device} document root must not overflow horizontally`);
  });
}

async function main() {
  await prepareAssets();
  const browser = await chromium.launch({ channel: 'chrome', headless: true });
  const browserVersion = browser.version();
  try {
    const selected = cases.filter(item => !process.argv[2] || item.name.toLowerCase().includes(process.argv[2].toLowerCase()));
    assert.ok(selected.length, 'No matching cases');
    for (const item of selected) {
      const start = Date.now(), entry = { name: item.name, status: 'PASS' };
      let harness;
      try {
        harness = await openFixture(browser);
        await item.run(harness);
        harness.assertClean();
        console.log(`PASS ${item.name}`);
      } catch (error) {
        entry.status = 'FAIL'; entry.error = error.stack || String(error);
        console.error(`FAIL ${item.name}\n${entry.error}`);
        if (harness) {
          const file = `failure-${results.length + 1}`;
          await harness.screenshot(file).catch(() => {});
          fs.writeFileSync(path.join(OUT, `${file}.html`), await harness.page.content().catch(() => ''));
        }
      } finally {
        entry.duration_ms = Date.now() - start;
        if (harness) {
          entry.screenshots = harness.screenshots;
          entry.layoutSamples = harness.layoutSamples;
          entry.diagnostics = harness.diagnostics();
          await harness.context.close();
        }
        results.push(entry);
      }
    }
  } finally {
    await browser.close();
    const assets = [...snapshots].map(([file, bytes]) => ({
      file: `console/${file}`, sha256: digest(bytes),
      changedDuringRun: digest(fs.readFileSync(path.join(ROOT, 'console', file))) !== digest(bytes),
    }));
    const report = {
      generated_at: new Date().toISOString(), browser: browserVersion,
      status: results.some(entry => entry.status === 'FAIL') ? 'FAIL' : 'PASS',
      passed: results.filter(entry => entry.status === 'PASS').length, total: results.length,
      testFile: 'tests/test_console_group_navigation.cjs', assets,
      limitations: [
        'GraphExplorer methods are stubbed; this suite verifies its navigation arguments, not graph rendering.',
        'Google Fonts CSS is empty in the isolated fixture; screenshots use the existing CSS font fallback.',
        'Only mocked API traffic and synthetic session values are used; no live authentication, AWS, or real key issuance.',
      ],
      cases: results,
    };
    const suffix = process.argv[2] ? '-filtered' : '';
    fs.writeFileSync(path.join(OUT, `results${suffix}.json`), JSON.stringify(report, null, 2) + '\n');
    fs.writeFileSync(path.join(OUT, `summary${suffix}.md`), [
      `# Console group navigation: ${report.status}`,
      '', `${report.passed}/${report.total} cases passed in Chrome ${browserVersion}.`, '',
      ...results.map(entry => `- ${entry.status}: ${entry.name}`), '',
      '## Limitations', '', ...report.limitations.map(value => `- ${value}`), '',
      '## Tested asset hashes', '', ...assets.map(asset => `- ${asset.file}: ${asset.sha256}${asset.changedDuringRun ? ' (changed during run; rerun required)' : ''}`), '',
      '## Failures', '', ...results.filter(entry => entry.error).map(entry => `### ${entry.name}\n\n\`\`\`text\n${entry.error}\n\`\`\`\n`),
    ].join('\n'));
    console.log(`${report.passed}/${report.total} passed. Artifacts: ${OUT}`);
    if (report.status === 'FAIL' || assets.some(asset => asset.changedDuringRun)) process.exitCode = 1;
  }
}

module.exports = {
  prepareAssets, openFixture, fixture, group, interactConfirmation, observeResponse, dismissPanel, navigateFixture,
  ids: { READY, DRAFT, STALE, PARTIAL, NORMAL, GROUP_ONLY },
  origin: ORIGIN,
  assetHashes: () => [...snapshots].map(([file, bytes]) => ({ file: `console/${file}`, sha256: digest(bytes) })),
};
if (require.main === module) main().catch(error => { console.error(error); process.exitCode = 1; });
