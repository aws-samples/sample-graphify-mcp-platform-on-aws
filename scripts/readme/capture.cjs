#!/usr/bin/env node
'use strict';

// Uses the current console files without modifying their HTML, CSS or scripts.
// All browser traffic is intercepted. Only SRI-pinned scripts and the console's
// public font assets may be downloaded by Node, without credentials.
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const zlib = require('node:zlib');
const { execFileSync } = require('node:child_process');
const { chromium } = require(process.env.GRAPHIFY_PLAYWRIGHT_MODULE || 'playwright');

const ROOT = path.resolve(__dirname, '../..');
const OUT = path.resolve(process.env.GRAPHIFY_README_LOG_DIR || path.join(os.tmpdir(), 'graphify-readme-capture'));
const DEST = path.join(ROOT, 'docs/screenshots');
const ORIGIN = 'https://console.example.com';
const FIXTURE = JSON.parse(fs.readFileSync(path.join(__dirname, 'demo-fixture.json'), 'utf8'));
const ALL_SHOTS = ['sources', 'register', 'groups', 'graph', 'connect', 'build', 'playground'];
const langs = (process.env.GRAPHIFY_README_LANGS || 'en,ko').split(',');
const shots = (process.env.GRAPHIFY_README_SHOTS || ALL_SHOTS.join(',')).split(',');
assert.ok(langs.every(lang => ['en', 'ko'].includes(lang)));
assert.ok(shots.every(shot => ALL_SHOTS.includes(shot)));
const hash = bytes => crypto.createHash('sha256').update(bytes).digest('hex');
const assets = new Map();
const publicAssets = new Map();
const report = {
  capturedAt: new Date().toISOString(),
  fixtureSha256: hash(fs.readFileSync(path.join(__dirname, 'demo-fixture.json'))),
  helperSha256: hash(fs.readFileSync(__filename)),
  demo: true, syntheticFailure: true, viewportWidth: 1440, deviceScaleFactor: 1,
  sourceReads: [], toolListings: [], blocked: [], errors: [], screenshots: [],
};

function codeSha() {
  try { return execFileSync('git', ['rev-parse', 'HEAD'], { cwd: ROOT, encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'] }).trim(); }
  catch {
    // A translated macOS shell can select the wrong Command Line Tools slice.
    if (process.platform === 'darwin') {
      return execFileSync('/usr/bin/arch', ['-arm64', '/usr/bin/git', 'rev-parse', 'HEAD'],
        { cwd: ROOT, encoding: 'utf8' }).trim();
    }
    return 'unavailable';
  }
}

async function cachedPublicAsset(url, contentType, integrity) {
  const host = new URL(url).hostname;
  assert.ok(['cdnjs.cloudflare.com', 'cdn.jsdelivr.net', 'fonts.googleapis.com', 'fonts.gstatic.com'].includes(host));
  const cache = path.join(OUT, 'assets', hash(url));
  let bytes;
  if (fs.existsSync(cache)) bytes = fs.readFileSync(cache);
  else {
    const response = await fetch(url, {
      credentials: 'omit', redirect: 'error', signal: AbortSignal.timeout(30000),
      headers: { 'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36' },
    });
    assert.equal(response.status, 200, `Public asset failed: ${url}`);
    bytes = Buffer.from(await response.arrayBuffer());
    fs.writeFileSync(cache, bytes);
  }
  if (integrity) {
    assert.match(integrity, /^sha384-/);
    assert.equal(`sha384-${crypto.createHash('sha384').update(bytes).digest('base64')}`, integrity);
  }
  publicAssets.set(url, { body: bytes, contentType, headers: { 'access-control-allow-origin': '*' } });
  return bytes;
}

async function prepareAssets() {
  fs.mkdirSync(path.join(OUT, 'assets'), { recursive: true });
  fs.mkdirSync(DEST, { recursive: true });
  for (const name of fs.readdirSync(path.join(ROOT, 'console'))) {
    if (/\.(html|css|js)$/.test(name)) assets.set(name, fs.readFileSync(path.join(ROOT, 'console', name)));
  }
  report.codeSha = codeSha();
  report.consoleSha256 = Object.fromEntries([...assets].map(([name, bytes]) => [name, hash(bytes)]));
  const stack = fs.readFileSync(path.join(ROOT, 'cdk/graphify_stack.py'), 'utf8');
  const modelList = stack.match(/\bplayground_models\s*=\s*\[([\s\S]*?)\]/)?.[1];
  assert.ok(modelList, 'Cannot locate the current CDK Playground model list');
  const playgroundModels = [...modelList.matchAll(/"([^"]+)"/g)].map(match => match[1]);
  assert.deepEqual(FIXTURE.config.playgroundModels, playgroundModels, 'Playground fixture must match CDK');
  report.cdkSha256 = hash(stack);
  report.playgroundModels = playgroundModels;
  const html = assets.get('index.html').toString('utf8');
  const scripts = [...html.matchAll(/<script\b([^>]+)>/g)].flatMap(([, attrs]) => {
    const url = attrs.match(/\bsrc="(https:[^"]+)"/)?.[1];
    const integrity = attrs.match(/\bintegrity="([^"]+)"/)?.[1];
    return url ? [{ url, integrity }] : [];
  });
  await Promise.all(scripts.map(({ url, integrity }) => {
    assert.ok(integrity, `Missing SRI for ${url}`);
    return cachedPublicAsset(url, 'application/javascript', integrity);
  }));
  const fontUrl = html.match(/href="(https:\/\/fonts\.googleapis\.com\/[^"]+)"/)?.[1];
  if (fontUrl) {
    const css = await cachedPublicAsset(fontUrl, 'text/css');
    const fonts = [...new Set([...css.toString('utf8').matchAll(/url\((https:[^)]+)\)/g)].map(match => match[1]))];
    await Promise.all(fonts.map(url => cachedPublicAsset(url, 'font/woff2')));
  }
  report.publicAssets = [...publicAssets].map(([url, asset]) => ({ url, sha256: hash(asset.body) }));
}

function localized(lang) {
  const f = structuredClone(FIXTURE);
  const { i18n } = f;
  for (const repo of f.repos.repos) {
    repo.server_name = i18n.names[repo.demo_role][lang];
    assert.match(repo.server_name, /^[A-Za-z0-9][A-Za-z0-9._-]{0,47}$/, 'Source names must be enterable in the platform');
    repo.description = i18n.descriptions[repo.demo_role][lang];
  }
  f.group.name = i18n.group_name[lang];
  f.group.description = i18n.group_description[lang];
  for (const source of f.group.sources) source.description = i18n.descriptions[source.role][lang];
  f.group.active_source_descriptions = Object.fromEntries(f.repos.repos.map(repo => [repo.repo_id, repo.description]));
  for (const page of Object.values(f.pages)) {
    for (const source of page.sources) source.description = i18n.descriptions[source.role][lang];
  }
  f.groups = { groups: [f.group] };
  assert.equal(f.me.mcp_base_url, f.config.mcpBase);
  assert.equal(f.group.mcp_url, `${f.config.mcpBase}/mcp/${f.group.group_id}`);
  f.servers = { servers: [
    { server_id: 'all', kind: 'hub', runtime_status: 'READY', mcp_url: `${f.config.mcpBase}/mcp/all` },
    { ...f.group, runtime_status: 'READY', connection_available: true },
  ] };
  return f;
}

function syntheticFailure(lang, repoId) {
  const note = lang === 'ko'
    ? '합성 실패 예시: 손상된 demo-corrupt.pdf를 읽지 못했습니다 (PdfReadError: EOF marker not found). 실제 빌드 결과가 아닙니다.'
    : 'SYNTHETIC FAILURE: demo-corrupt.pdf could not be parsed (PdfReadError: EOF marker not found). No build was executed.';
  return {
    repo_id: repoId, source_status: 'FAILED', can_rebuild: true, last_error: note,
    hints: ['document_conversion'],
    build: {
      id: 'synthetic-demo-build-001', status: 'FAILED', current_phase: 'BUILD',
      started_at: '2026-09-16T01:00:00Z', ended_at: '2026-09-16T01:00:12Z',
      duration_seconds: 12,
      phases: [{
        name: 'BUILD', status: 'FAILED', duration_seconds: 12,
        messages: [{ code: 'SYNTHETIC_DOCUMENT_CONVERSION', message: note }],
      }],
    },
    logs: {
      state: 'available', next_token: null, truncated: false,
      events: [
        { timestamp: '2026-09-16T01:00:00Z', message: '[SYNTHETIC FIXTURE] README diagnostic example. No build was executed.' },
        { timestamp: '2026-09-16T01:00:12Z', message: '[SYNTHETIC ERROR] demo-corrupt.pdf: PdfReadError: EOF marker not found.' },
      ],
    },
  };
}

async function newConsole(browser, lang) {
  const fixture = localized(lang);
  const gid = fixture.group.group_id;
  const state = { failed: false };
  const context = await browser.newContext({
    viewport: { width: 1440, height: 1120 }, deviceScaleFactor: 1,
    locale: lang === 'ko' ? 'ko-KR' : 'en-US', timezoneId: 'Asia/Seoul',
    reducedMotion: 'reduce', serviceWorkers: 'block',
  });
  await context.addInitScript(lang => {
    localStorage.setItem('gfy-lang', lang);
    sessionStorage.setItem('gfy-access', 'offline-demo-not-a-credential');
    sessionStorage.setItem('gfy-id', `e30.${btoa(JSON.stringify({ email: 'demo@example.com' }))}.offline`);
    sessionStorage.setItem('gfy-exp', '9999999999999');
    window.__readmeCanvasLabels = [];
    const fillText = CanvasRenderingContext2D.prototype.fillText;
    CanvasRenderingContext2D.prototype.fillText = function (text, x, y, ...args) {
      if (this.canvas.closest('#gx-canvas') && String(text).includes('change_fund')) {
        window.__readmeCanvasLabels.push({ text: String(text), x, y, width: this.canvas.clientWidth, height: this.canvas.clientHeight });
      }
      return fillText.call(this, text, x, y, ...args);
    };
  }, lang);
  const page = await context.newPage();
  page.setDefaultTimeout(30000);
  page.on('pageerror', error => report.errors.push({ lang, message: error.message }));
  page.on('console', message => {
    if (message.type() === 'error') report.errors.push({ lang, message: message.text() });
  });
  await context.route('**/*', async route => {
    const request = route.request(), url = new URL(request.url()), method = request.method();
    const respond = body => route.fulfill({ contentType: 'application/json', body: JSON.stringify(body) });
    try {
      if (publicAssets.has(url.href)) return await route.fulfill(publicAssets.get(url.href));
      if (url.origin !== ORIGIN) throw new Error(`Blocked external request: ${method} ${url.href}`);
      const name = url.pathname === '/' ? 'index.html' : url.pathname.slice(1);
      if (assets.has(name) && method === 'GET') {
        return await route.fulfill({
          contentType: name.endsWith('.html') ? 'text/html' : name.endsWith('.css') ? 'text/css' : 'application/javascript',
          body: assets.get(name),
        });
      }
      if (name === 'config.json' && method === 'GET') return await respond(fixture.config);
      // The only POSTs allowed are local, read-only replay operations.
      if (url.pathname === `/api/groups/${gid}/source` && method === 'POST') {
        const query = request.postDataJSON(), source = fixture.source;
        assert.equal(query.source_id, source.source_id);
        assert.equal(query.file, source.file);
        assert.equal(query.group_version, source.group_version);
        const lines = source.text.split(/\r\n|\n|\r/);
        if (lines.at(-1) === '') lines.pop();
        const start = query.start_line, end = Math.min(query.end_line, lines.length);
        assert.ok(Number.isInteger(start) && start > 0 && Number.isInteger(end) && end >= start);
        const text = lines.slice(start - 1, end).join('\n');
        report.sourceReads.push({ lang, sourceId: query.source_id, file: query.file, start, end });
        return await respond({
          ...source, start_line: start, end_line: end, line_count: lines.length,
          text, sha256: hash(source.text), quote_sha256: hash(text),
        });
      }
      if (url.pathname === '/demo-mcp-stream' && method === 'POST') {
        const body = request.postDataJSON();
        assert.equal(body.server_id, gid);
        assert.equal(body.op, 'mcp');
        assert.equal(body.payload.method, 'tools/list');
        report.toolListings.push({ lang, serverId: gid, method: 'tools/list' });
        return await route.fulfill({
          contentType: 'text/event-stream',
          body: `data: ${JSON.stringify({ type: 'mcp', ok: true, status: 200, body: {
            jsonrpc: '2.0', id: body.payload.id, result: { tools: fixture.tools },
          } })}\n\n`,
        });
      }
      assert.equal(method, 'GET', `Mutating or model request is forbidden: ${url.pathname}`);
      const routes = {
        '/api/me': fixture.me, '/api/repos': fixture.repos, '/api/servers': fixture.servers,
        '/api/groups': fixture.groups, '/api/keys': { keys: [] }, '/api/usage': {},
        '/api/catalog': { servers: [] }, [`/api/groups/${gid}`]: { group: fixture.group },
      };
      if (url.pathname === `/api/groups/${gid}/graph`) {
        const result = fixture.pages[`${url.searchParams.get('kind')}:${url.searchParams.get('offset') || '0'}`];
        assert.ok(result, 'No replay page for this graph request');
        return await respond(result);
      }
      if (state.failed && url.pathname === '/api/repos/files__demo-qa/build') {
        return await respond(syntheticFailure(lang, 'files__demo-qa'));
      }
      assert.ok(Object.hasOwn(routes, url.pathname), `Unknown API or asset: ${url.pathname}`);
      return await respond(routes[url.pathname]);
    } catch (error) {
      report.blocked.push({ lang, method, path: url.pathname, message: error.message });
      await route.abort('blockedbyclient');
    }
  });
  await page.goto(ORIGIN, { waitUntil: 'networkidle' });
  await page.waitForFunction(() => !!S.me && document.getElementById('app').getAttribute('aria-busy') === 'false');
  await page.evaluate(() => document.fonts.ready);
  return { page, context, fixture, state, gid };
}

async function settle(page) {
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  await page.mouse.move(1, 1);
}

async function choose(page, id, value) {
  const combo = page.locator(`#${id}-combo`);
  if (await combo.count()) {
    await combo.click();
    await page.locator(`#${id}-list [data-value="${value}"]`).click();
  } else {
    await page.locator(`#${id}`).selectOption(value);
  }
  await settle(page);
}

function optimizePng(file) {
  // Recompress IDAT losslessly with Node's built-in zlib. No image dependency.
  const png = fs.readFileSync(file), chunks = [];
  for (let offset = 8; offset < png.length;) {
    const size = png.readUInt32BE(offset);
    chunks.push({ type: png.toString('ascii', offset + 4, offset + 8), raw: png.subarray(offset, offset + size + 12),
      data: png.subarray(offset + 8, offset + 8 + size) });
    offset += size + 12;
  }
  const compressed = zlib.deflateSync(zlib.inflateSync(Buffer.concat(chunks.filter(c => c.type === 'IDAT').map(c => c.data))), { level: 9 });
  const bytes = Buffer.concat([Buffer.from('IDAT'), compressed]);
  let crc = 0xffffffff;
  for (const byte of bytes) {
    crc ^= byte;
    for (let bit = 0; bit < 8; bit++) crc = (crc >>> 1) ^ ((crc & 1) ? 0xedb88320 : 0);
  }
  const length = Buffer.alloc(4), checksum = Buffer.alloc(4);
  length.writeUInt32BE(compressed.length);
  checksum.writeUInt32BE((crc ^ 0xffffffff) >>> 0);
  let written = false;
  const optimized = Buffer.concat([png.subarray(0, 8), ...chunks.flatMap(c => {
    if (c.type !== 'IDAT') return [c.raw];
    if (written) return [];
    written = true; return [length, bytes, checksum];
  })]);
  if (optimized.length < png.length) fs.writeFileSync(file, optimized);
}

async function capture(page, lang, shot, clip) {
  await settle(page);
  const layout = await page.evaluate(() => ({
    viewport: { width: innerWidth, height: innerHeight },
    scrollY,
    horizontalOverflow: document.documentElement.scrollWidth > innerWidth + 1,
  }));
  assert.equal(layout.horizontalOverflow, false, `${shot}: horizontal overflow`);
  const filename = `guide-${shot}.${lang}.png`, file = path.join(DEST, filename);
  await page.screenshot({ path: file, ...(clip ? { clip } : {}), animations: 'disabled' });
  optimizePng(file);
  const bytes = fs.readFileSync(file);
  const info = { filename, ...layout, clip: clip || null, width: bytes.readUInt32BE(16), height: bytes.readUInt32BE(20),
    bytes: bytes.length, sha256: hash(bytes) };
  report.screenshots.push(info);
  fs.writeFileSync(path.join(OUT, `${shot}.${lang}.txt`), await page.locator('body').innerText());
  console.log(`${filename}: ${info.width}x${info.height}, ${Math.round(info.bytes / 1024)} KiB`);
}

async function modalCapture(page, lang, shot, panelId) {
  const dialog = page.locator(`dialog[data-panel-id="${panelId}"]`);
  await dialog.waitFor({ state: 'visible' });
  assert.equal(await dialog.evaluate(node => node.open && node.matches(':modal')), true);
  await settle(page);
  const box = await dialog.boundingBox();
  const viewport = page.viewportSize();
  const pad = 20;
  const x = Math.max(0, Math.floor(box.x - pad));
  const y = Math.max(0, Math.floor(box.y - pad));
  await capture(page, lang, shot, {
    x, y, width: Math.min(viewport.width - x, Math.ceil(box.width + pad * 2)),
    height: Math.min(viewport.height - y, Math.ceil(box.height + pad * 2)),
  });
  await dialog.locator('.console-panel__header [data-panel-close]').first().click();
}

async function runLanguage(browser, lang) {
  const { page, context, fixture, state, gid } = await newConsole(browser, lang);
  try {
    if (shots.includes('sources')) {
      await page.evaluate(() => goTab('repos'));
      await page.setViewportSize({ width: 1440, height: 1420 });
      const secondRow = await page.locator('#repos-tbody > tr').nth(1).boundingBox();
      await capture(page, lang, 'sources', { x: 0, y: 0, width: 1440, height: Math.ceil(secondRow.y + secondRow.height + 6) });
    }
    if (shots.includes('register')) {
      await page.setViewportSize({ width: 1440, height: 1120 });
      await page.evaluate(() => openRegistration());
      await page.locator('#reg-source-seg [data-src="files"]').click();
      await page.locator('#reg-description').fill(lang === 'ko'
        ? '서비스 요구사항, 펀드 변경 안내서, PDF 정책 매뉴얼입니다.'
        : 'Product requirements, fund change guides, and PDF policy manuals.');
      await page.locator('#reg-files-name').fill('pension-docs-demo');
      await page.locator('#reg-name-files').fill('pension-documents-demo');
      assert.match(await page.locator('#reg-name-files').inputValue(), /^[A-Za-z0-9][A-Za-z0-9._-]{0,47}$/);
      await choose(page, 'reg-llm-files', '1');
      await choose(page, 'reg-llm-images-files', '1');
      assert.equal(await page.locator('#reg-llm-model-files').isDisabled(), false);
      assert.equal(await page.locator('#reg-llm-images-files').isDisabled(), false);
      await modalCapture(page, lang, 'register', 'registration-panel');
    }
    if (shots.includes('groups')) {
      await page.setViewportSize({ width: 1440, height: 960 });
      await page.evaluate(async id => { goTab('groups'); await SourceGroups.open(id); }, gid);
      await page.locator('#sg-graph svg').waitFor({ state: 'visible' });
      const section = page.locator('#sg-graph').locator('..');
      await section.evaluate(node => window.scrollTo(0, node.getBoundingClientRect().top + scrollY - 85));
      await capture(page, lang, 'groups');
    }
    if (shots.includes('graph')) {
      await page.setViewportSize({ width: 1440, height: 1120 });
      await page.evaluate(id => openGraph(id), gid);
      await page.waitForFunction(id => GraphExplorer._state().M?.groupId === id && !!GraphExplorer._state().renderer, gid);
      await page.evaluate(() => {
        GraphExplorer._act.setView({ mode: 'full', scope: { kind: 'all' } });
        GraphExplorer._act.setSelected('4');
        window.scrollTo(0, 0);
      });
      await page.locator('#gx-inspector .gx-code').waitFor({ state: 'attached' });
      const fullSummary = await page.locator('.gx-source-connections').innerText();
      const fullModel = await page.evaluate(() => {
        const M = GraphExplorer._state().M;
        return JSON.stringify({ labels: M.label, provenance: M.provenance, links: M.linkProvenance });
      });
      // Use the real display control. It hides rationale/structural/inferred
      // content while the source cards still summarize the full loaded graph.
      await page.locator('#gx-simple').check();
      assert.equal(await page.locator('.gx-source-connections').innerText(), fullSummary);
      assert.equal(await page.evaluate(() => {
        const M = GraphExplorer._state().M;
        return JSON.stringify({ labels: M.label, provenance: M.provenance, links: M.linkProvenance });
      }), fullModel, 'Simple view must preserve loaded data and provenance');
      await page.evaluate(() => { window.__readmeCanvasLabels = []; });
      await page.evaluate(() => { GraphExplorer._state().renderer.resize(); GraphExplorer._state().renderer.refresh(); });
      await page.waitForFunction(name => window.__readmeCanvasLabels.some(draw =>
        draw.text.includes(`${name}: change_fund`) && draw.x >= 0 && draw.x < draw.width),
      fixture.i18n.names.backend[lang]);
      await settle(page);
      const graph = await page.evaluate(() => {
        const { M, renderer, graph, simple, view, selected } = GraphExplorer._state();
        const side = document.querySelector('.gx-side.gx-right');
        side.scrollTop = 0;
        const sideBox = side.getBoundingClientRect();
        const rows = [...document.querySelectorAll('.gx-source-connection')].map(row => {
          const box = row.getBoundingClientRect();
          return { text: row.innerText, visible: box.top >= sideBox.top && box.bottom <= sideBox.bottom };
        });
        const hiddenKindsRendered = [];
        graph.forEachNode((key, attrs) => {
          if (M.types[M.t[attrs.idx]] === 'rationale') hiddenKindsRendered.push('rationale');
        });
        graph.forEachEdge((key, attrs) => {
          const kind = M.relations[M.er[attrs.eidx]];
          if (['contains', 'rationale_for'].includes(kind) || M.einf[attrs.eidx]) hiddenKindsRendered.push(kind);
        });
        return { nodes: M.label.length, edges: M.es.length, selected: M.label[4], selectedKey: selected,
          simple, view, hiddenKindsRendered, fullLoadedSummaryPreserved: true,
          renderer: renderer.constructor.name, renderedNodes: graph.order, renderedEdges: graph.size,
          camera: renderer.getCamera().getState(), canvasLabels: window.__readmeCanvasLabels,
          text: document.querySelector('#gx-inspector').innerText, rows };
      });
      assert.equal(graph.nodes, 75); assert.equal(graph.edges, 126);
      assert.equal(graph.simple, true);
      assert.equal(graph.selectedKey, '4');
      assert.deepEqual(graph.view, { mode: 'full', scope: { kind: 'all' } });
      assert.deepEqual(graph.hiddenKindsRendered, []);
      assert.ok(Object.values(graph.camera).every(Number.isFinite), 'Sigma camera coordinates must be finite');
      assert.match(graph.selected, /change_fund/);
      assert.ok(graph.rows.length === 4 && graph.rows.every(row => row.visible), 'All four connected-source cards must be visible');
      assert.ok(graph.text.includes(fixture.i18n.names.backend[lang]));
      report.graph ||= {}; report.graph[lang] = graph;
      // Keep the actual canvas and inspector at nearly native size when the
      // README renders this crop at about 1000 CSS pixels.
      await page.locator('#gx-stage').evaluate(node => window.scrollTo(0, node.getBoundingClientRect().top + scrollY - 100));
      await settle(page);
      const stage = await page.locator('#gx-stage').boundingBox();
      const right = await page.locator('.gx-side.gx-right').boundingBox();
      await capture(page, lang, 'graph', {
        x: Math.floor(stage.x - 8), y: Math.floor(stage.y - 8),
        width: Math.ceil(right.x + right.width - stage.x + 16),
        height: Math.min(1120 - Math.floor(stage.y - 8), Math.ceil(stage.height + 16)),
      });
    }
    if (shots.includes('connect')) {
      await page.setViewportSize({ width: 1440, height: 1330 });
      await page.evaluate(id => openConnectionGuide(id), gid);
      assert.ok((await page.locator('#connection-url').innerText()).includes(`${fixture.config.mcpBase}/mcp/${gid}`));
      await modalCapture(page, lang, 'connect', 'connection-guide');
    }
    if (shots.includes('build')) {
      state.failed = true;
      const repo = fixture.repos.repos.find(repo => repo.repo_id === 'files__demo-qa');
      repo.status = 'FAILED';
      repo.server_name = 'synthetic-failure-demo';
      repo.description = lang === 'ko' ? '진단 화면을 위한 합성 실패 예시입니다.' : 'Synthetic failure for the diagnostics screenshot.';
      await page.setViewportSize({ width: 1440, height: 1500 });
      await page.evaluate(async () => { await refreshAll(); goTab('repos'); openBuildPanel('files__demo-qa'); });
      await page.waitForFunction(() => !!BP.data && !BP.loading);
      assert.match(await page.locator('#bp-last-error').textContent(), /SYNTHETIC|합성/);
      assert.match(await page.locator('#bp-last-error').textContent(), /demo-corrupt\.pdf.*PdfReadError: EOF marker not found/);
      const diagnostic = await page.evaluate(() => ({
        started: BP.data.build.started_at, ended: BP.data.build.ended_at, fetched: BP.fetchedAt,
        logTimes: BP.events.map(event => event.timestamp), lastError: BP.data.last_error,
      }));
      assert.ok(Date.parse(diagnostic.started) <= Date.parse(diagnostic.ended));
      assert.ok(Date.parse(diagnostic.ended) < diagnostic.fetched);
      assert.ok(diagnostic.logTimes.every(time => Date.parse(time) <= Date.parse(diagnostic.ended)));
      report.buildDiagnostics ||= {}; report.buildDiagnostics[lang] = diagnostic;
      await page.locator('#bp-status-note').click();
      await modalCapture(page, lang, 'build', 'build-panel');
    }
    if (shots.includes('playground')) {
      await page.setViewportSize({ width: 1440, height: 1120 });
      await page.evaluate(id => openTargetPlayground(id), gid);
      await page.waitForFunction(id => PS.toolsFor === id && PS.tools.length === 7, gid);
      assert.equal(await page.locator('#play-model').inputValue(), fixture.config.playgroundModels[0]);
      report.playground ||= {};
      report.playground[lang] = { model: await page.locator('#play-model').inputValue(), toolCount: 7, invoked: false };
      await choose(page, 'direct-tool', 'query_graph');
      await page.locator('#direct-args').fill(JSON.stringify({ query: 'REQ-102' }, null, 2));
      assert.equal(await page.locator('#chat-log').innerText(), '');
      assert.equal(await page.locator('#direct-out').isVisible(), false);
      await page.evaluate(() => window.scrollTo(0, 0));
      const direct = await page.locator('#page-play > section').last().boundingBox();
      await capture(page, lang, 'playground', {
        x: 0, y: 0, width: 1440, height: Math.min(1120, Math.ceil(direct.y + direct.height + 20)),
      });
    }
  } finally {
    await context.close();
  }
}

(async () => {
  await prepareAssets();
  const browser = await chromium.launch({
    channel: process.env.GRAPHIFY_BROWSER_CHANNEL || 'chrome',
    headless: true, args: ['--enable-unsafe-swiftshader'],
  });
  report.browser = browser.version();
  try {
    for (const lang of langs) await runLanguage(browser, lang);
    assert.deepEqual(report.blocked, []);
    assert.deepEqual(report.errors, []);
    for (const [name, sha] of Object.entries(report.consoleSha256)) {
      assert.equal(hash(fs.readFileSync(path.join(ROOT, 'console', name))), sha, `Console changed during capture: ${name}`);
    }
    report.modelCalls = 0;
    report.toolExecutions = 0;
    report.mutations = 0;
    report.passed = true;
  } finally {
    await browser.close();
    fs.writeFileSync(path.join(OUT, 'capture-report.json'), JSON.stringify(report, null, 2) + '\n');
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
