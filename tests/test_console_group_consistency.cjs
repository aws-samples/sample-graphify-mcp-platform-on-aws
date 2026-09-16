#!/usr/bin/env node
'use strict';

/**
 * Parent coordinates execution AFTER the shared console/groups edits settle:
 * GRAPHIFY_PLAYWRIGHT_MODULE=/existing/cache/node_modules/playwright \
 *   node tests/test_console_group_consistency.cjs [case-name-substring ...]
 *
 * --list and node --check do not load Playwright or launch a browser.
 * Eight sequential, isolated cases; 30s operations and 60s cases including boot.
 * Uses actual asset bytes and the navigation/UX fixtures via Module._compile.
 * Only already-cached, SRI-verified CDN assets are allowed; no downloads/install,
 * AWS, credentials, group writes, key issuance, or backend behavior tests.
 * JSON, HTML failure captures, screenshots: GRAPHIFY_CONSISTENCY_OUT or
 * /tmp/graphify-group-consistency. Do not run alongside production asset edits.
 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const Module = require('node:module');

const ROOT = path.resolve(__dirname, '..');
const OUT = path.resolve(process.env.GRAPHIFY_CONSISTENCY_OUT || '/tmp/graphify-group-consistency');
const OPERATION_MS = 30000, CASE_MS = 60000;
const WIDTHS = [1440, 768, 390], LANGUAGES = ['ko', 'en'];
const GROUP_ID = /^grp_[0-9a-f]{32}$/;
const LONG_NAME = `고객 "single'quoted" <img src=x onerror="window.__consistencyInjected=1"> ${'LongName'.repeat(5)}`;
const LONG_SOURCE = `files__${'original-source-without-a-short-break-'.repeat(9)}'quoted'"<tag>고객`;
const RAW_VALUE = `  literal <img src=x onerror="window.__consistencyInjected=1"> "double" 'single' & 고객\n  second line\r\nlast line  `;
const cases = [];
const testCase = (name, run) => cases.push({ name, run });
const hash = bytes => crypto.createHash('sha256').update(bytes).digest('hex');
const scriptAtRead = { file: path.relative(ROOT, __filename), sha256: hash(fs.readFileSync(__filename)) };
let chromium, uxHarness, prepareAssets, goto, choose, translatedButton, round;
let assertPanelOpen, closePanelModal, group, assetHashes, origin;
const fixtureHashes = [];
const rows = page => page.locator('#sg-list #groups-table tbody tr.sg-list-card');
const row = (page, id) => rows(page).filter({ hasText: id });
const codePre = viewer => viewer.locator('pre.snippet.code-viewer__content');
const copyButton = viewer => viewer.locator('.code-viewer__actions button');

function bounded(promise, timeout, label) {
  let timer;
  const result = Promise.race([promise, new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error(`${label} exceeded ${timeout}ms`)), timeout);
  })]).finally(() => clearTimeout(timer));
  // These promises may reject while their trigger is still being awaited.
  result.catch(() => {});
  return result;
}

function loadHarness() {
  ({ chromium } = require(process.env.GRAPHIFY_PLAYWRIGHT_MODULE || 'playwright'));
  const compile = (name, exports, navigation) => {
    const filename = path.join(__dirname, name), bytes = fs.readFileSync(filename);
    fixtureHashes.push({ file: path.relative(ROOT, filename), sha256: hash(bytes) });
    const mod = new Module(filename, module);
    mod.filename = filename;
    mod.paths = Module._nodeModulePaths(path.dirname(filename));
    if (navigation) {
      mod.require = specifier => specifier === './test_console_group_navigation.cjs'
        ? navigation : Module.prototype.require.call(mod, specifier);
    }
    mod._compile(bytes.toString('utf8') + (exports ? `\nmodule.exports = { ${exports} };\n` : ''), filename);
    return mod.exports;
  };
  const navigation = compile('test_console_group_navigation.cjs');
  ({ group, assetHashes, origin } = navigation);
  ({ uxHarness, prepareAssets, goto, choose, translatedButton, round, assertPanelOpen, closePanelModal } =
    compile('test_console_ux.cjs',
      'uxHarness, prepareAssets, goto, choose, translatedButton, round, assertPanelOpen, closePanelModal',
      navigation));
}

function requireCachedAssets() {
  // prepareAssets normally permits an SRI-pinned download on a cache miss.
  // Preflight every dependency so this suite remains entirely offline.
  const html = fs.readFileSync(path.join(ROOT, 'console/index.html'), 'utf8');
  for (const [, attrs] of html.matchAll(/<script\b([^>]+)>/g)) {
    const url = attrs.match(/\bsrc="(https:[^"]+)"/)?.[1];
    if (!url) continue;
    const integrity = attrs.match(/\bintegrity="([^"]+)"/)?.[1];
    const cache = path.join('/tmp/graphify-group-ui-navigation/cdn', `${hash(url).slice(0, 16)}.js`);
    assert.ok(fs.existsSync(cache), `Offline CDN cache missing: ${cache}; URL=${url}. Parent must populate it separately.`);
    assert.equal(`sha384-${crypto.createHash('sha384').update(fs.readFileSync(cache)).digest('base64')}`,
      integrity, `Cached CDN bytes fail SRI: ${url}; cache=${cache}`);
  }
}

function currentHashes(assets) {
  return assets.map(asset => {
    try {
      const currentSha256 = hash(fs.readFileSync(path.join(ROOT, asset.file)));
      return { ...asset, currentSha256, changedDuringRun: currentSha256 !== asset.sha256 };
    } catch (error) {
      return { ...asset, changedDuringRun: true, error: error.stack || String(error) };
    }
  });
}

function assertStable(assets, phase) {
  assert.deepEqual(currentHashes(assets).filter(asset => asset.changedDuringRun), [],
    `${phase}: asset or test bytes changed since read; coordinate a fresh run against settled files`);
}

async function settled(h) {
  await bounded((async () => {
    for (;;) {
      await round(h.page);
      const requests = [...h.consistency.pending];
      if (!requests.length) {
        await round(h.page);
        if (!h.consistency.pending.size) return;
      } else {
        await Promise.all(requests.map(async request => {
          const response = await request.response();
          if (response) await response.finished();
        }));
      }
    }
  })(), OPERATION_MS, 'Fixture reads and rendering settle');
}

async function seedGroups(h, { count = 36, ready = false, longText = false, recovery = false } = {}) {
  if (longText) {
    const source = h.data.repos[0], oldId = source.repo_id;
    Object.assign(source, { repo_id: LONG_SOURCE, source_id: LONG_SOURCE, server_name: LONG_NAME });
    const server = h.data.servers.find(server => server.server_id === oldId);
    Object.assign(server, source, { server_id: LONG_SOURCE, mcp_url: endpoint(LONG_SOURCE) });
  }
  const sources = h.data.repos.slice(0, 2);
  h.data.groups = Array.from({ length: count }, (_, index) => {
    const id = `grp_${(0xc001n + BigInt(index)).toString(16).padStart(32, '0')}`;
    const status = ready ? 'READY' : ['READY', 'DRAFT', 'STALE', 'BUILDING', 'PARTIAL', 'FAILED'][index % 6];
    const available = ['READY', 'PARTIAL'].includes(status);
    const accessRecovery = recovery && index === 6;
    return group(id, {
      name: longText && index === 0 ? LONG_NAME : `Team ${String(count - index).padStart(2, '0')} ${index % 2 ? 'Birch' : 'Maple'}`,
      description: index === 4 ? 'Find this description: consistency-needle' : `Original group description ${index}`,
      role: ['owner', 'editor', 'viewer'][index % 3], revision: 7, active_revision: 7,
      created_at: new Date(Date.UTC(2026, 8, 1, index)).toISOString(),
      status, stale: status === 'STALE', data_ready: available && !accessRecovery,
      ...(accessRecovery ? { access_recovery: true } : {}),
      sources: sources.map((source, i) => ({ source_id: source.repo_id, role: i ? 'backend' : 'frontend',
        description: longText ? RAW_VALUE : `Membership ${index}/${i}` })),
      active_source_versions: Object.fromEntries(sources.map(source => [source.repo_id, source.active_source_version])),
    });
  });
  assert.equal(new Set(h.data.groups.map(g => g.group_id)).size, count, 'Synthetic group IDs must be unique');
  for (const g of h.data.groups) assert.match(g.group_id, GROUP_ID);
  h.data.servers = [
    ...h.data.servers.filter(server => server.kind !== 'group'),
    ...h.data.groups.filter(g => g.data_ready && !g.stale).map(g => ({ ...structuredClone(g), runtime_status: 'READY' })),
  ];
  // Re-entering from another tab triggers the group's authoritative list read,
  // including when this case previously displayed an empty group fixture.
  await goto(h.page, 'repos');
  await h.page.evaluate(() => refreshAll());
  await goto(h.page, 'groups');
  await settled(h);
  await h.page.waitForFunction(count => S.groups.length === count, count);
  await h.page.locator('#groups-table').waitFor({ state: 'visible' });
  h.consistency.seedIds = h.data.groups.map(g => g.group_id);
}

function endpoint(id) {
  return `https://mcp.navigation.test/mcp/${encodeURIComponent(id)}`;
}

async function openGroup(h, id) {
  assert.equal(await row(h.page, id).count(), 1, `Exactly one visible table row for ${id}`);
  await row(h.page, id).locator('.sg-list-open').click();
  await h.page.waitForFunction(id => document.querySelector('#sg-meta > .sg-id')?.textContent === id, id);
  await settled(h);
}

async function canonicalState(h) {
  return h.page.evaluate(() => ({
    groups: S.groups,
    connectable: connectableServers().map(server => server.server_id).sort(),
    keyOptions: [...document.getElementById('key-scope').options].map(option => ({
      value: option.value, disabled: option.disabled, label: option.textContent,
    })),
    selectedId: document.querySelector('#sg-meta > .sg-id')?.textContent,
  }));
}

async function displayedIds(page) {
  return rows(page).evaluateAll(nodes => nodes.map(node => {
    const matches = [...node.querySelectorAll('.sg-id')].map(id => id.textContent)
      .filter(value => /^grp_[0-9a-f]{32}$/.test(value));
    if (matches.length !== 1) throw new Error(`Expected one literal group ID per row: ${node.outerHTML}`);
    return matches[0];
  }));
}

const displayStatus = g => g.access_recovery ? 'RECOVERY' : g.stale && g.status === 'READY' ? 'STALE' : g.status;

function sortedGroups(groups, sort = 'name', lang = 'ko') {
  const name = (a, b) => a.name.localeCompare(b.name, lang, { numeric: true, sensitivity: 'base' })
    || a.group_id.localeCompare(b.group_id);
  return [...groups].sort((a, b) => (sort === 'newest' ? Date.parse(b.created_at) - Date.parse(a.created_at)
    : sort === 'status' ? displayStatus(a).localeCompare(displayStatus(b)) : 0) || name(a, b));
}

async function expectRows(h, expected, label) {
  await settled(h);
  const actual = await displayedIds(h.page), wanted = expected.map(g => g.group_id);
  assert.deepEqual(actual, wanted, `${label}: exact table IDs/order; expected=${JSON.stringify(wanted)} actual=${JSON.stringify(actual)}`);
}

async function expectCount(h, shown, total) {
  const count = h.page.locator('#sg-count'), text = (await count.textContent()).trim();
  assert.ok(text, 'The group count must not be empty');
  // Both totals are required when filtered; permit a single value when equal.
  const numbers = (text.match(/\d[\d,]*/g) || []).map(value => Number(value.replaceAll(',', '')));
  assert.ok(numbers.includes(shown) && numbers.includes(total),
    `Count must expose matches=${shown} and canonical total=${total}; text=${JSON.stringify(text)}`);
  h.consistency.counts.push({ shown, total, text });
}

async function search(h, value) {
  await h.page.locator('#sg-search').fill(value);
  await settled(h);
}

async function pager(h, key) {
  return translatedButton(h.page, h.page.locator('#pager-groups'), `pg.${key}`);
}

async function expectClipboard(h, button, expected, label) {
  const before = await h.page.evaluate(() => window.__consistencyCopies.length);
  await button.click();
  await h.page.waitForFunction(count => window.__consistencyCopies.length > count, before);
  const copies = await h.page.evaluate(() => window.__consistencyCopies);
  assert.equal(copies.length, before + 1, `${label}: one copy per click`);
  assert.equal(copies[before], expected, `${label}: copy must preserve every character, quote, newline and surrounding space`);
  assert.deepEqual(Buffer.from(copies[before], 'utf8'), Buffer.from(expected, 'utf8'), `${label}: exact UTF-8 bytes`);
}

async function viewerStructure(viewer, expected, label) {
  assert.equal(await viewer.count(), 1, `${label}: exactly one shared .code-viewer`);
  const facts = await viewer.evaluate(node => {
    const pre = node.querySelector('pre.snippet.code-viewer__content'), code = pre?.querySelector(':scope > code');
    return {
      preCount: node.querySelectorAll('pre').length, raw: pre?.textContent, codeText: code?.textContent,
      children: pre ? [...pre.children].map(child => child.tagName) : [],
      nestedMarkup: code?.children.length, footerCount: node.querySelectorAll('.code-viewer__actions').length,
      copyCount: node.querySelectorAll('.code-viewer__actions button').length,
      footerOutsidePre: !!pre && !pre.contains(node.querySelector('.code-viewer__actions')),
    };
  });
  assert.deepEqual(facts, { preCount: 1, raw: expected, codeText: expected, children: ['CODE'],
    nestedMarkup: 0, footerCount: 1, copyCount: 1, footerOutsidePre: true }, `${label}: literal code and separate copy footer`);
}

async function viewerMeasurements(h, viewer, label) {
  await copyButton(viewer).scrollIntoViewIfNeeded();
  await copyButton(viewer).click({ trial: true });
  const facts = await viewer.evaluate(node => {
    const pre = node.querySelector('pre'), button = node.querySelector('.code-viewer__actions button');
    const footer = node.querySelector('.code-viewer__actions'), code = pre.querySelector('code');
    const rect = element => {
      const b = element.getBoundingClientRect();
      return { left: b.left, right: b.right, top: b.top, bottom: b.bottom, width: b.width, height: b.height };
    };
    const styles = (element, keys) => Object.fromEntries(keys.map(key => [key, getComputedStyle(element)[key]]));
    return {
      viewport: innerWidth, rootWidth: document.documentElement.scrollWidth,
      pre: rect(pre), button: rect(button), footer: rect(footer),
      gap: button.getBoundingClientRect().top - pre.getBoundingClientRect().bottom,
      footerGap: footer.getBoundingClientRect().top - pre.getBoundingClientRect().bottom,
      rightGap: pre.getBoundingClientRect().right - button.getBoundingClientRect().right,
      preStyle: styles(pre, ['backgroundColor', 'color', 'borderRadius', 'paddingTop', 'paddingRight',
        'paddingBottom', 'paddingLeft', 'fontFamily', 'fontSize', 'lineHeight', 'whiteSpace', 'overflowWrap']),
      codeStyle: styles(code, ['fontFamily', 'fontSize', 'lineHeight', 'whiteSpace', 'overflowWrap']),
      buttonClipped: button.scrollWidth > button.clientWidth + 1 || button.scrollHeight > button.clientHeight + 1,
    };
  });
  h.consistency.measurements.push({ label, ...facts });
  assert.ok(facts.gap >= 12 && facts.footerGap >= 12, `${label}: copy footer requires >=12px BELOW block: ${JSON.stringify(facts)}`);
  assert.ok(Math.abs(facts.rightGap) <= 3, `${label}: copy right edge must align with block: ${JSON.stringify(facts)}`);
  assert.ok(facts.rootWidth <= facts.viewport + 1, `${label}: root overflow: ${JSON.stringify(facts)}`);
  assert.ok(facts.pre.width > 0 && facts.pre.left >= -1 && facts.pre.right <= facts.viewport + 1,
    `${label}: code block outside viewport: ${JSON.stringify(facts)}`);
  assert.equal(facts.buttonClipped, false, `${label}: clipped copy text: ${JSON.stringify(facts)}`);
  return facts;
}

async function capture(h, suffix) {
  const filename = `${h.consistency.index}-${suffix}.png`;
  await h.page.screenshot({ path: path.join(OUT, filename), fullPage: true, animations: 'disabled' });
  h.consistency.screenshots.push(filename);
}

testCase('server cards group URL command and JSON copies in one responsive viewer footer', async h => {
  await seedGroups(h, { count: 1, ready: true });
  const target = h.data.groups[0], url = endpoint(target.group_id);
  const name = `graphify-group-${target.group_id.slice(4, 16)}`;
  await goto(h.page, 'servers');
  await h.page.locator('#servers-search').fill(target.group_id);
  const card = h.page.locator('#servers-list > section').filter({ hasText: target.group_id });
  const viewer = card.locator('.code-viewer');
  const expected = [
    ['srv.copyurl', url],
    ['srv.copyclaude', `claude mcp add --transport http '${name}' '${url}' --header 'X-Graphify-Key: <YOUR_KEY>'`],
    ['srv.copyjson', JSON.stringify({ mcpServers: { [name]: { type: 'http', url, headers: { 'X-Graphify-Key': '<YOUR_KEY>' } } } }, null, 2)],
  ];
  assert.equal(await viewer.locator('button').count(), 3);
  assert.equal(await codePre(viewer).textContent(), url);
  for (const [key, value] of expected) {
    await expectClipboard(h, await translatedButton(h.page, viewer, key), value, key);
  }
  for (const width of [1440, 390]) {
    await h.page.setViewportSize({ width, height: 1000 });
    await viewer.locator('button').last().scrollIntoViewIfNeeded();
    const bounds = await viewer.evaluate(node => {
      const pre = node.querySelector('pre').getBoundingClientRect();
      const footer = node.querySelector('.code-viewer__actions').getBoundingClientRect();
      const last = [...node.querySelectorAll('button')].at(-1).getBoundingClientRect();
      return { gap: footer.top - pre.bottom, right: pre.right - last.right,
        root: document.documentElement.scrollWidth, width: innerWidth };
    });
    assert.ok(bounds.gap >= 12 && Math.abs(bounds.right) <= 3 && bounds.root <= bounds.width + 1, JSON.stringify(bounds));
  }
});

testCase('guide three clients ko en preserve exact raw clipboard bytes and literal custom content', async h => {
  await seedGroups(h, { count: 3, ready: true, longText: true });
  const source = h.data.repos[0], url = endpoint(source.repo_id), name = source.server_name;
  const quote = value => "'" + value.replaceAll("'", "'\\''") + "'";
  const http = { type: 'http', url, headers: { 'X-Graphify-Key': '<YOUR_KEY>' } };
  const expected = {
    claude: `claude mcp add --transport http ${quote(name)} ${quote(url)} --header ${quote('X-Graphify-Key: <YOUR_KEY>')}`,
    cursor: JSON.stringify({ mcpServers: { [name]: { url, headers: http.headers } } }, null, 2),
    vscode: JSON.stringify({ servers: { [name]: http } }, null, 2),
  };
  for (const lang of LANGUAGES) {
    await h.page.evaluate(lang => setLang(lang), lang);
    await h.page.evaluate(id => openConnectionGuide(id), source.repo_id);
    await assertPanelOpen(h.page, 'connection-guide');
    const guideURL = h.page.locator('#connection-content .code-viewer').filter({
      has: h.page.locator('pre', { hasText: url }),
    }).filter({ hasNot: h.page.locator('#connection-config') });
    await viewerStructure(guideURL, url, `${lang}: guide URL`);
    await expectClipboard(h, copyButton(guideURL), url, `${lang}: guide URL`);
    for (const client of ['claude', 'cursor', 'vscode']) {
      await h.page.locator(`#connection-tab-${client}`).click();
      const pre = h.page.locator('#connection-config'), viewer = pre.locator('..');
      assert.equal(await viewer.evaluate(node => node.classList.contains('code-viewer')), true, 'Config PRE belongs to shared viewer');
      await viewerStructure(viewer, expected[client], `${lang}/${client}`);
      assert.equal(await pre.evaluate(node => node.tagName), 'PRE', 'Raw config keeps its PRE ID');
      assert.equal(await pre.getAttribute('role'), 'tabpanel');
      assert.equal(await pre.getAttribute('aria-labelledby'), `connection-tab-${client}`);
      assert.equal(await h.page.locator(`#connection-tab-${client}`).getAttribute('aria-selected'), 'true');
      await expectClipboard(h, copyButton(viewer), expected[client], `${lang}/${client}`);
    }
    assert.equal(await h.page.locator('#connection-content img, #connection-content script, #connection-content svg').count(), 0);
    await closePanelModal(h.page, 'connection-guide');
  }
  // Exercise optional label/ARIA forwarding with content that must never parse.
  await h.page.evaluate(value => {
    const host = document.createElement('section'); host.id = 'consistency-viewer-probe';
    const label = document.createElement('h2'); label.id = 'consistency-probe-title'; label.textContent = 'Literal code probe';
    host.append(label, codeViewer(value, { id: 'consistency-probe-pre', role: 'region',
      labelledBy: label.id, label: 'Optional <b>literal label</b>' }), codeViewer('unlabelled exact text'));
    document.getElementById('page-servers').append(host);
  }, RAW_VALUE);
  const probe = h.page.locator('#consistency-viewer-probe .code-viewer').first();
  await viewerStructure(probe, RAW_VALUE, 'Custom viewer');
  assert.equal(await codePre(probe).getAttribute('id'), 'consistency-probe-pre');
  assert.equal(await codePre(probe).getAttribute('role'), 'region');
  assert.equal(await codePre(probe).getAttribute('aria-labelledby'), 'consistency-probe-title');
  assert.equal(await probe.locator('b, img, svg, script').count(), 0);
  assert.ok((await probe.textContent()).includes('Optional <b>literal label</b>'));
  await expectClipboard(h, copyButton(probe), RAW_VALUE, 'Custom viewer');
  await viewerStructure(h.page.locator('#consistency-viewer-probe .code-viewer').nth(1), 'unlabelled exact text', 'Unlabelled viewer');
  await h.page.locator('#consistency-viewer-probe').evaluate(node => node.remove());
});

testCase('group endpoint and guide share computed viewer styles and footer geometry at 1440 768 390', async h => {
  await seedGroups(h, { count: 3, ready: true });
  const target = h.data.groups[0];
  for (const lang of LANGUAGES) for (const width of WIDTHS) {
    await h.page.setViewportSize({ width, height: 1000 });
    await h.page.evaluate(lang => setLang(lang), lang);
    await goto(h.page, 'groups');
    await settled(h);
    await openGroup(h, target.group_id);
    const groupViewer = h.page.locator('#sg-meta .sg-mcp .code-viewer');
    await viewerStructure(groupViewer, endpoint(target.group_id), `${lang}/${width}: group endpoint`);
    const groupMetrics = await viewerMeasurements(h, groupViewer, `${lang}/${width}: group endpoint`);
    await expectClipboard(h, copyButton(groupViewer), endpoint(target.group_id), `${lang}/${width}: group endpoint`);
    await capture(h, `${lang}-${width}-group-viewer`);
    await h.page.locator('#sg-meta [data-i18n="sg.guide"]').click();
    await assertPanelOpen(h.page, 'connection-guide');
    const viewers = h.page.locator('#connection-content .code-viewer');
    assert.equal(await viewers.count(), 2, 'Guide URL and config use shared viewers');
    for (const [index, viewer] of (await viewers.all()).entries()) {
      const metrics = await viewerMeasurements(h, viewer, `${lang}/${width}: guide viewer ${index}`);
      assert.deepEqual(metrics.preStyle, groupMetrics.preStyle, `${lang}/${width}: shared PRE computed styles`);
      assert.deepEqual(metrics.codeStyle, groupMetrics.codeStyle, `${lang}/${width}: shared CODE computed styles`);
    }
    const spacing = await h.page.locator('.connection-steps').evaluate(steps => {
      const children = [...steps.children], gaps = children.slice(1).map((step, index) =>
        step.getBoundingClientRect().top - children[index].getBoundingClientRect().bottom);
      const ctas = [children[0], children[2]].map(step => {
        const paragraph = step.querySelector('p'), button = step.querySelector('button');
        return { text: button?.textContent, gap: button && paragraph
          ? button.getBoundingClientRect().top - paragraph.getBoundingClientRect().bottom : null };
      });
      return { gaps, ctas };
    });
    h.consistency.measurements.push({ label: `${lang}/${width}: guide steps`, ...spacing });
    assert.ok(spacing.gaps.length === 2 && spacing.gaps.every(gap => gap >= 12),
      `Guide steps need vertical separation: ${JSON.stringify(spacing)}`);
    assert.ok(spacing.ctas.every(cta => cta.gap !== null && cta.gap >= 12),
      `Issue/verify CTAs need separation below paragraphs: ${JSON.stringify(spacing)}`);
    await capture(h, `${lang}-${width}-guide-viewers`);
    await closePanelModal(h.page, 'connection-guide');
  }
});

testCase('36 group rows search status recovery sort and pagination return correct IDs and counts', async h => {
  await seedGroups(h, { recovery: true });
  const all = h.data.groups, byName = sortedGroups(all);
  assert.equal(await h.page.locator('#sg-list #groups-table').count(), 1, 'List owns one semantic table');
  assert.equal(await h.page.locator('#pager-groups .pager-size').inputValue(), '25', 'Default page size is 25');
  await expectRows(h, byName.slice(0, 25), 'Default page');
  await expectCount(h, 36, 36);
  assert.equal(await (await pager(h, 'prev')).isDisabled(), true);
  await (await pager(h, 'next')).click();
  await expectRows(h, byName.slice(25), 'Second page');
  assert.equal(await (await pager(h, 'next')).isDisabled(), true);
  assert.equal(await h.page.locator('#pager-groups .pager-page').textContent(), '2 / 2');
  await search(h, all[0].group_id);
  await expectRows(h, [all[0]], 'ID search resets page');
  await expectCount(h, 1, 36);
  await search(h, '  mApLe  ');
  const maples = byName.filter(g => g.name.includes('Maple'));
  await expectRows(h, maples, 'Case-insensitive trimmed name search');
  await expectCount(h, maples.length, 36);
  await choose(h.page, 'sg-status-filter', 'READY');
  const readyMaples = maples.filter(g => displayStatus(g) === 'READY');
  await expectRows(h, readyMaples, 'Search AND status excludes access recovery');
  await expectCount(h, readyMaples.length, 36);
  await search(h, '');
  for (const status of ['DRAFT', 'READY', 'BUILDING', 'STALE', 'PARTIAL', 'FAILED', 'RECOVERY']) {
    await choose(h.page, 'sg-status-filter', status);
    const expected = byName.filter(g => displayStatus(g) === status);
    await expectRows(h, expected, `${status} filter`);
    await expectCount(h, expected.length, 36);
    if (status === 'RECOVERY') {
      assert.equal(expected.length, 1, 'Fixture includes one READY group requiring access recovery');
      const recoveryRow = row(h.page, expected[0].group_id);
      assert.equal(await recoveryRow.locator('.sg-status').textContent(),
        await h.page.evaluate(() => t('sg.recoveryStatus')), 'Recovery has its own display label');
      assert.equal(await recoveryRow.locator('.sg-status-ready').count(), 0, 'Recovery must not display a green READY badge');
      assert.deepEqual(await h.page.evaluate(id => {
        const g = S.groups.find(g => g.group_id === id);
        return { status: g.status, access_recovery: g.access_recovery };
      }, expected[0].group_id), { status: 'READY', access_recovery: true }, 'RECOVERY is display-only; API status stays unchanged');
    }
  }
  await choose(h.page, 'sg-status-filter', '');
  await search(h, 'consistency-needle');
  await expectRows(h, [all[4]], 'Description search');
  await search(h, h.data.repos[0].repo_id);
  await expectRows(h, byName.slice(0, 25), 'Constituent source ID search');
  await expectCount(h, 36, 36);
  await search(h, '');
  for (const sort of ['newest', 'status', 'name']) {
    await choose(h.page, 'sg-sort', sort);
    await expectRows(h, sortedGroups(all, sort).slice(0, 25), `${sort} sort`);
  }
  if (await h.page.locator('#sg-role-filter').count()) {
    await choose(h.page, 'sg-role-filter', 'editor');
    const editors = byName.filter(g => g.role === 'editor');
    await expectRows(h, editors, 'Optional editor role filter');
    await expectCount(h, editors.length, 36);
    await choose(h.page, 'sg-role-filter', '');
  }
  await h.page.locator('#pager-groups .pager-size').selectOption('10');
  await expectRows(h, byName.slice(0, 10), 'Ten per page');
  await (await pager(h, 'next')).click();
  await expectRows(h, byName.slice(10, 20), 'Ten per page, second page');
  await h.page.locator('#pager-groups .pager-size').selectOption('50');
  await expectRows(h, byName, 'Fifty per page');
});

testCase('filters preserve canonical groups scopes and selected detail and restore every original page', async h => {
  await seedGroups(h);
  const target = sortedGroups(h.data.groups).find(g => g.status === 'READY');
  await openGroup(h, target.group_id);
  const before = await canonicalState(h);
  assert.equal(before.groups.length, 36);
  assert.equal(before.selectedId, target.group_id);
  assert.ok(before.connectable.includes(target.group_id), 'Selected READY group is connectable before filtering');
  assert.ok(before.keyOptions.some(option => option.value === target.group_id && !option.disabled));
  const requestsBefore = h.data.requests.length;
  const mutations = [
    () => search(h, 'missing-consistency-match'),
    () => choose(h.page, 'sg-status-filter', 'DRAFT'),
    () => choose(h.page, 'sg-sort', 'newest'),
    () => search(h, ''),
    () => choose(h.page, 'sg-status-filter', ''),
    () => (async () => { await (await pager(h, 'next')).click(); })(),
  ];
  if (await h.page.locator('#sg-role-filter').count()) {
    mutations.push(() => choose(h.page, 'sg-role-filter', 'viewer'), () => choose(h.page, 'sg-role-filter', ''));
  }
  for (const [index, change] of mutations.entries()) {
    await change(); await settled(h);
    assert.deepEqual(await canonicalState(h), before, `Presentation change ${index + 1} must not mutate canonical state`);
    assert.equal(await h.page.locator('#sg-meta > .sg-id').textContent(), target.group_id,
      `Presentation change ${index + 1} retains selected detail`);
    assert.equal(await h.page.locator('#sg-meta').isVisible(), true);
  }
  await search(h, target.group_id);
  assert.equal(await row(h.page, target.group_id).locator('.sg-list-open').getAttribute('aria-current'), 'true',
    'Previously hidden selection is still selected when it reappears');
  await expectRows(h, [target], 'Selected group search');
  await expectCount(h, 1, 36);
  await h.page.locator('#sg-toolbar [data-i18n="sg.clearFilters"]').click();
  await settled(h);
  assert.equal(await h.page.locator('#sg-search').inputValue(), '');
  assert.equal(await h.page.locator('#sg-status-filter').inputValue(), '');
  if (await h.page.locator('#sg-role-filter').count()) {
    assert.equal(await h.page.locator('#sg-role-filter').inputValue(), '');
  }
  assert.equal(await h.page.locator('#pager-groups .pager-size').inputValue(), '25');
  assert.equal(await (await pager(h, 'prev')).isDisabled(), true, 'Clear filters restores the first page');
  const original = sortedGroups(before.groups, await h.page.locator('#sg-sort').inputValue());
  const restoredIds = [];
  // Every original row must reappear from the retained list, including those
  // outside the first page. No refresh is allowed to repair a narrowed cache.
  for (let start = 0; start < original.length; start += 25) {
    await expectRows(h, original.slice(start, start + 25), `Restored page ${start / 25 + 1}`);
    restoredIds.push(...await displayedIds(h.page));
    await expectCount(h, original.length, original.length);
    assert.deepEqual(await canonicalState(h), before, 'Restoring pages preserves groups, scopes and selected detail');
    if (start + 25 < original.length) await (await pager(h, 'next')).click();
  }
  assert.equal(await (await pager(h, 'next')).isDisabled(), true, 'Restored pagination ends at the original last page');
  assert.deepEqual(restoredIds, original.map(g => g.group_id), 'All original IDs are restored exactly once across pages');
  assert.deepEqual(h.data.requests.slice(requestsBefore), [],
    'Filtering, clearing and paging must use retained data without fetching new source/group metadata');
});

testCase('owner editor viewer actions and accessible group modals remain reachable from table', async h => {
  await seedGroups(h, { count: 3, ready: true });
  for (const lang of LANGUAGES) {
    await h.page.evaluate(lang => setLang(lang), lang);
    for (const target of h.data.groups) {
      const currentRow = row(h.page, target.group_id);
      const actions = ['edit', 'manageMembers', 'delete'];
      const wanted = target.role === 'owner' ? actions : target.role === 'editor' ? ['edit'] : [];
      const actual = [];
      for (const action of actions) {
        const button = currentRow.locator(`[data-i18n="sg.${action}"]`);
        if (await button.count() && await button.isVisible()) {
          actual.push(action);
          assert.equal(await button.isDisabled(), false, `${lang}/${target.role}/${action} available`);
        }
      }
      assert.deepEqual(actual, wanted, `${lang}/${target.role}: management action matrix`);
      for (const action of ['guide', 'fullGraph']) {
        assert.equal(await currentRow.locator(`[data-i18n="sg.${action}"]`).isDisabled(), false,
          `${target.role}: built data remains readable`);
      }
      const trigger = currentRow.locator('.sg-list-open');
      assert.equal(await trigger.evaluate(node => node.tagName), 'BUTTON');
      assert.equal(await trigger.getAttribute('aria-controls'), 'sg-detail');
      await trigger.focus();
      await h.page.keyboard.press('Enter');
      await h.page.waitForFunction(id => document.querySelector('#sg-meta > .sg-id')?.textContent === id, target.group_id);
      await settled(h);
      assert.equal(await h.page.locator('#sg-meta [data-i18n="sg.rebuild"]').count(), target.role === 'viewer' ? 0 : 1);
      if (target.role === 'viewer') continue;
      for (const [action, panelId] of target.role === 'owner'
        ? [['edit', 'sg-editor'], ['manageMembers', 'sg-members-section']] : [['edit', 'sg-editor']]) {
        const button = row(h.page, target.group_id).locator(`[data-i18n="sg.${action}"]`);
        await button.click();
        const dialog = await assertPanelOpen(h.page, panelId);
        const accessible = await dialog.evaluate(node => {
          const ids = (node.getAttribute('aria-labelledby') || '').split(/\s+/).filter(Boolean);
          return {
            labelled: !!node.getAttribute('aria-label') || ids.some(id => document.getElementById(id)?.textContent.trim()),
            focusedInside: node.contains(document.activeElement), modal: node.matches(':modal'),
          };
        });
        assert.deepEqual(accessible, { labelled: true, focusedInside: true, modal: true },
          `${lang}/${target.role}/${panelId}: accessible native modal`);
        if (panelId === 'sg-editor') assert.equal(await h.page.locator('#sg-name').inputValue(), target.name);
        else await h.page.locator('#sg-member-email').waitFor({ state: 'visible' });
        await closePanelModal(h.page, panelId);
        await settled(h);
        assert.equal(await button.evaluate(node => node === document.activeElement), true,
          `${panelId}: focus returns to the same table action`);
      }
    }
  }
});

testCase('long literal names and source IDs wrap with readable headers actions and full width list', async h => {
  await seedGroups(h, { count: 3, ready: true, longText: true });
  const target = h.data.groups[0];
  for (const lang of LANGUAGES) for (const width of WIDTHS) {
    await h.page.setViewportSize({ width, height: 1000 });
    await h.page.evaluate(lang => setLang(lang), lang);
    await settled(h);
    const trigger = row(h.page, target.group_id).locator('.sg-list-open');
    assert.equal(await trigger.locator('strong').textContent(), target.name, 'Table name preserves literal custom content');
    await openGroup(h, target.group_id);
    assert.equal(await h.page.locator('#sg-meta .sg-detail-heading h2').textContent(), target.name);
    assert.equal(await h.page.locator('#sg-meta .sg-source-card .sg-id').first().textContent(), LONG_SOURCE);
    assert.equal(await h.page.locator('#sg-root img, #sg-root script, #sg-root .sg-list-open tag').count(), 0);
    const facts = await h.page.locator('#sg-root').evaluate(root => {
      const table = root.querySelector('#groups-table'), list = root.querySelector('#sg-list');
      const tableContainer = table.parentElement, ts = getComputedStyle(tableContainer);
      const container = list.closest('section') || list.parentElement, cs = getComputedStyle(container);
      const detail = root.querySelector('#sg-detail');
      const rect = node => {
        const r = node.getBoundingClientRect();
        return { left: r.left, right: r.right, top: r.top, bottom: r.bottom, width: r.width };
      };
      const targets = [...root.querySelectorAll('h2,h3,th,label,button,.sg-source-card .sg-id,.sg-detail-heading,.sg-list-open strong')]
        .filter(node => node.getClientRects().length && getComputedStyle(node).visibility !== 'hidden')
        // Mobile retains a visually clipped semantic thead; visible per-cell
        // data-labels replace it and are asserted separately below.
        .filter(node => !node.closest('thead') || getComputedStyle(node.closest('thead')).clipPath === 'none');
      const clipped = targets.filter(node => node.clientWidth > 0
        && (node.scrollWidth > node.clientWidth + 2 || node.scrollHeight > node.clientHeight + 2))
        .map(node => ({ tag: node.tagName, id: node.id, text: node.textContent.slice(0, 180),
          clientWidth: node.clientWidth, scrollWidth: node.scrollWidth, clientHeight: node.clientHeight, scrollHeight: node.scrollHeight }));
      const source = root.querySelector('.sg-source-card .sg-id'), range = document.createRange();
      range.selectNodeContents(source);
      const lines = new Set([...range.getClientRects()].map(r => Math.round(r.top))).size;
      return {
        viewport: innerWidth, rootWidth: document.documentElement.scrollWidth, clipped, sourceLines: lines,
        table: rect(table), list: rect(list), detail: rect(detail),
        contentWidth: container.clientWidth - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight),
        tableContentWidth: tableContainer.clientWidth - parseFloat(ts.paddingLeft) - parseFloat(ts.paddingRight),
        tableHeaders: [...table.querySelectorAll('th')].map(node => node.textContent.trim()),
        mobileLabels: [...table.querySelectorAll('tr.sg-list-card:first-child td')].map(node => node.dataset.label || ''),
      };
    });
    h.consistency.measurements.push({ label: `${lang}/${width}: long group text`, ...facts });
    assert.deepEqual(facts.clipped, [], `${lang}/${width}: unreadable headers/actions/IDs`);
    assert.ok(facts.rootWidth <= width + 1, `${lang}/${width}: root overflow: ${JSON.stringify(facts)}`);
    assert.ok(Math.abs(facts.list.width - facts.contentWidth) <= 4, `${lang}/${width}: list must fill content width: ${JSON.stringify(facts)}`);
    assert.ok(Math.abs(facts.table.width - facts.tableContentWidth) <= 4,
      `${lang}/${width}: table must fill list's scrollport, excluding its scrollbar gutter: ${JSON.stringify(facts)}`);
    assert.ok(facts.detail.top >= facts.list.bottom - 1, `${lang}/${width}: detail follows the full-width list`);
    assert.ok(facts.tableHeaders.length >= 3 && facts.tableHeaders.every(Boolean), 'Semantic table headers have readable names');
    if (width === 390) {
      assert.ok(facts.sourceLines > 1, `Long source ID must wrap into multiple lines: ${JSON.stringify(facts)}`);
      assert.ok(facts.mobileLabels.length >= 3 && facts.mobileLabels.every(Boolean), 'Mobile cells retain their column labels');
    }
    await capture(h, `${lang}-${width}-long-text`);
  }
});

testCase('empty filtered empty refresh and locale changes preserve filter inputs', async h => {
  await seedGroups(h, { count: 0 });
  for (const lang of LANGUAGES) {
    await h.page.evaluate(lang => setLang(lang), lang);
    assert.equal(await rows(h.page).count(), 0);
    const emptyText = await h.page.locator('#sg-list').textContent();
    assert.ok(emptyText.includes(await h.page.evaluate(() => t('sg.empty'))), `${lang}: true empty message`);
    await expectCount(h, 0, 0);
  }
  // The same context now receives records through a normal list refresh.
  await seedGroups(h);
  await h.page.evaluate(() => setLang('ko'));
  await search(h, 'no-result-consistency');
  await choose(h.page, 'sg-status-filter', 'READY');
  await choose(h.page, 'sg-sort', 'newest');
  const inputs = async () => h.page.evaluate(() => Object.fromEntries(
    ['sg-search', 'sg-status-filter', 'sg-sort', 'sg-role-filter'].filter(id => document.getElementById(id))
      .map(id => [id, document.getElementById(id).value])));
  if (await h.page.locator('#sg-role-filter').count()) await choose(h.page, 'sg-role-filter', 'editor');
  const before = await inputs();
  for (const lang of ['en', 'ko']) {
    await h.page.evaluate(lang => setLang(lang), lang);
    assert.deepEqual(await inputs(), before, `${lang}: locale preserves all filter values`);
    await h.page.locator('#sg-root .sg-intro [data-i18n="sg.refresh"]').click();
    await settled(h);
    assert.deepEqual(await inputs(), before, `${lang}: refresh preserves all filter values`);
    await expectRows(h, [], `${lang}: filtered empty stays empty`);
    await expectCount(h, 0, 36);
    const content = (await h.page.locator('#sg-list').textContent()).trim();
    assert.ok(content && !content.includes(await h.page.evaluate(() => t('sg.empty'))),
      `${lang}: filtered-empty copy is distinct from true empty: ${JSON.stringify(content)}`);
    const clear = h.page.locator('#sg-list [data-i18n="sg.clearFilters"]');
    assert.equal(await clear.isVisible(), true, 'Filtered empty offers a visible reset action');
  }
  await h.page.locator('#sg-list [data-i18n="sg.clearFilters"]').click();
  const after = await inputs();
  assert.equal(after['sg-search'], '');
  assert.equal(after['sg-status-filter'], '');
  if ('sg-role-filter' in after) assert.equal(after['sg-role-filter'], '');
  await expectRows(h, sortedGroups(h.data.groups, after['sg-sort']).slice(0, 25), 'Clearing filters restores canonical list');
  await expectCount(h, 36, 36);
});

async function runCase(browser, item, index) {
  const started = Date.now(), contexts = new Set();
  const state = { index: index + 1, pending: new Set(), measurements: [], counts: [], screenshots: [],
    blockedWrites: [], nativeDialogs: [], errors: [], closing: false };
  const entry = { name: item.name, status: 'PASS' };
  let h;
  const scopedBrowser = {
    async newContext(options) {
      if (state.closing) throw new Error('Case closed before context creation');
      const context = await browser.newContext(options);
      contexts.add(context);
      if (state.closing) { await context.close(); throw new Error('Case closed during context creation'); }
      context.setDefaultTimeout(OPERATION_MS);
      context.setDefaultNavigationTimeout(OPERATION_MS);
      await context.addInitScript(() => {
        window.__consistencyCopies = [];
        window.__consistencyUnhandled = [];
        Object.defineProperty(navigator, 'clipboard', { configurable: true, value: {
          writeText: async value => { window.__consistencyCopies.push(value); },
        } });
        addEventListener('unhandledrejection', event =>
          window.__consistencyUnhandled.push(event.reason?.stack || String(event.reason)));
      });
      context.on('page', page => {
        page.on('request', request => state.pending.add(request));
        page.on('requestfinished', request => state.pending.delete(request));
        page.on('requestfailed', request => state.pending.delete(request));
        page.on('dialog', dialog => {
          state.nativeDialogs.push({ type: dialog.type(), message: dialog.message() });
          dialog.dismiss().catch(error => { if (!state.closing) state.errors.push(error.stack || String(error)); });
        });
      });
      return context;
    },
  };
  const work = (async () => {
    h = await uxHarness(scopedBrowser);
    h.consistency = state;
    if (state.closing) return;
    // Installed last, this guard runs before all UX/navigation fixture routes.
    await h.context.route('**/*', async route => {
      const request = route.request();
      if (['GET', 'HEAD'].includes(request.method())) return route.fallback();
      state.blockedWrites.push({ method: request.method(), url: request.url(), body: request.postData() });
      return route.abort('blockedbyclient');
    });
    await item.run(h);
    await settled(h);
    h.assertClean();
    assert.deepEqual(state.blockedWrites, [], 'Presentation tests must not submit mutations; full attempted requests are recorded');
    assert.deepEqual(state.errors, [], 'Fixture/teardown callback errors');
    assert.deepEqual(state.nativeDialogs, [], 'No blocking native dialogs');
    assert.deepEqual(await h.page.evaluate(() => window.__consistencyUnhandled), [], 'Unhandled application promises');
    assert.equal(await h.page.evaluate(() => window.__consistencyInjected || window.__uxInjected || 0), 0,
      'Literal names/source IDs/code never execute as HTML');
  })();
  work.catch(() => {});
  try {
    await bounded(work, CASE_MS, item.name);
  } catch (error) {
    entry.status = 'FAIL'; entry.error = error.stack || String(error);
    if (h && !h.page.isClosed()) {
      await bounded((async () => {
        const base = `${index + 1}-failure`;
        await h.page.screenshot({ path: path.join(OUT, `${base}.png`), fullPage: true, timeout: 2500 });
        state.screenshots.push(`${base}.png`);
        fs.writeFileSync(path.join(OUT, `${base}.html`), await h.page.content());
        entry.html = `${base}.html`;
        entry.visibleState = await h.page.evaluate(() => ({
          viewport: innerWidth, rootWidth: document.documentElement.scrollWidth,
          count: document.getElementById('sg-count')?.textContent,
          selected: document.querySelector('#sg-meta > .sg-id')?.textContent,
          filters: Object.fromEntries(['sg-search', 'sg-status-filter', 'sg-sort', 'sg-role-filter'].map(id =>
            [id, document.getElementById(id)?.value])),
          rows: [...document.querySelectorAll('#groups-table tr.sg-list-card')].map(row => row.textContent.slice(0, 500)),
        }));
      })(), 5000, 'Failure diagnostics').catch(error => { entry.captureError = error.stack || String(error); });
    }
  } finally {
    state.closing = true;
    if (h) entry.diagnostics = h.diagnostics();
    entry.measurements = state.measurements;
    entry.counts = state.counts;
    entry.screenshots = state.screenshots;
    entry.blockedWrites = state.blockedWrites;
    entry.nativeDialogs = state.nativeDialogs;
    entry.callbackErrors = state.errors;
    const cleanup = await Promise.allSettled([...contexts].map(context => bounded((async () => {
      try { await context.unrouteAll({ behavior: 'ignoreErrors' }); }
      finally { await context.close(); }
    })(), OPERATION_MS, 'Isolated context cleanup')));
    entry.cleanupErrors = cleanup.filter(result => result.status === 'rejected').map(result => String(result.reason));
    if (entry.cleanupErrors.length) entry.status = 'FAIL';
    entry.duration_ms = Date.now() - started;
  }
  return entry;
}

async function main() {
  const filters = process.argv.slice(2).filter(value => value !== '--list').map(value => value.toLowerCase());
  const selected = cases.filter(item => !filters.length || filters.some(filter => item.name.toLowerCase().includes(filter)));
  assert.ok(selected.length, `No matching consistency cases: ${JSON.stringify(filters)}`);
  if (process.argv.includes('--list')) { console.log(selected.map(item => item.name).join('\n')); return; }
  fs.mkdirSync(OUT, { recursive: true });
  const lock = path.join(OUT, '.consistency.lock');
  const fd = fs.openSync(lock, 'wx'); // Never overlap another author/run using this artifact directory.
  fs.writeFileSync(fd, JSON.stringify({ pid: process.pid, started_at: new Date().toISOString() }));
  fs.closeSync(fd);
  const report = {
    generated_at: new Date().toISOString(), status: 'FAIL', filters, total: selected.length, tests: [],
    scope: 'Actual HTML/CSS/JS; fake member session and 36 valid groups; strict intercepted offline requests. Read-only UI and clipboard/geometry assertions. GraphExplorer uses the existing fixture stub.',
    assumptions: [
      'Parent runs this suite only after shared codeViewer, console styles and group table integration are complete.',
      'List IDs/classes follow the contract; sort values are name/newest/status, status values uppercase, all-filter value empty.',
      'Optional role filter uses owner/editor/viewer; group counts expose filtered and canonical totals.',
      'Group editor and member dialogs retain existing IDs and data-i18n action selectors; no save, delete, invite or CAS operation is executed.',
      'Canonical group preservation is checked through shared state, selected detail and restoration of every original page without new requests; cases run sequentially in fresh contexts.',
      'Navigation CDN cache must exist and pass SRI. No package installation or network cache fill occurs.',
    ],
    operation_timeout_ms: OPERATION_MS, case_timeout_ms: CASE_MS,
    assets: [], testedScripts: [scriptAtRead],
  };
  let browser;
  try {
    loadHarness();
    report.testedScripts.push(...fixtureHashes);
    requireCachedAssets();
    const fetch = globalThis.fetch;
    globalThis.fetch = async input => { throw new Error(`Offline asset setup forbids a network fetch: ${String(input)}`); };
    try { await prepareAssets(); }
    finally { globalThis.fetch = fetch; }
    report.assets = assetHashes();
    assert.ok(report.assets.length > 0, 'Snapshot actual production assets before launching Chrome');
    const allBytes = [...report.assets, ...report.testedScripts];
    assertStable(allBytes, 'Before browser launch');
    browser = await chromium.launch({ channel: 'chrome', headless: true, timeout: OPERATION_MS });
    report.browser = browser.version();
    for (const [index, item] of selected.entries()) {
      assertStable(allBytes, `Before case ${index + 1}`);
      const entry = await runCase(browser, item, index);
      report.tests.push(entry);
      fs.writeFileSync(path.join(OUT, `case-${index + 1}.json`), JSON.stringify(entry, null, 2) + '\n');
      console.log(`${entry.status} ${entry.name}`);
      if (entry.error) console.error(entry.error);
      assertStable(allBytes, `After case ${index + 1}`);
    }
  } catch (error) {
    report.error = error.stack || String(error);
    console.error(report.error);
  } finally {
    if (browser) await bounded(browser.close(), OPERATION_MS, 'Browser shutdown')
      .catch(error => { report.shutdownError = error.stack || String(error); });
    report.assets = currentHashes(report.assets);
    report.testedScripts = currentHashes(report.testedScripts);
    report.passed = report.tests.filter(entry => entry.status === 'PASS').length;
    report.status = !report.error && !report.shutdownError && report.passed === report.total && report.assets.length > 0
      && [...report.assets, ...report.testedScripts].every(asset => !asset.changedDuringRun) ? 'PASS' : 'FAIL';
    const artifact = path.join(OUT, 'results.json');
    try {
      fs.writeFileSync(artifact, JSON.stringify(report, null, 2) + '\n');
      console.log(JSON.stringify({ status: report.status, passed: report.passed, total: report.total, artifact }));
    } finally { fs.unlinkSync(lock); }
    if (report.status === 'FAIL') process.exitCode = 1;
  }
}

if (require.main === module) main().catch(error => { console.error(error.stack || String(error)); process.exitCode = 1; });
