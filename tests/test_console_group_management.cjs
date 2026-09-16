#!/usr/bin/env node
'use strict';

/**
 * Run with the same existing Playwright package as the navigation suite:
 * GRAPHIFY_PLAYWRIGHT_MODULE=/absolute/path/to/playwright \
 *   node tests/test_console_group_management.cjs [case-name-substring]
 *
 * Eight isolated browser cases, real console assets and UI handlers, fake API
 * responses only. No installs, AWS, credentials, or product-file edits.
 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { chromium } = require(process.env.GRAPHIFY_PLAYWRIGHT_MODULE || 'playwright');
const { prepareAssets, openFixture, fixture, ids, origin, assetHashes, interactConfirmation, observeResponse } = require('./test_console_group_navigation.cjs');
const ROOT = path.resolve(__dirname, '..');
const OUT = '/tmp/graphify-group-ui-navigation/management';
const OWNER = ids.READY, EDITOR = ids.PARTIAL, VIEWER = ids.STALE, OTHER_OWNER = ids.DRAFT;
const cases = [], results = [];
const testCase = (name, run) => cases.push({ name, run });
const myRow = (page, id) => page.locator(`[data-source-group="${id}"]`);
const listRow = (page, id) => page.locator('#sg-list .sg-list-card').filter({ hasText: id });
const changeButton = (row, action) => row.getByRole('button', { name: action === 'edit' ? '그룹 수정' : '그룹 삭제', exact: true });
const writes = h => h.data.requests.filter(r => ['POST', 'DELETE'].includes(r.method) && /^\/groups\/[^/]+$/.test(r.path));
const nextFrames = page => page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));

function managementFixture() {
  const data = fixture();
  data.groups = data.groups.filter(g => [OWNER, EDITOR, VIEWER, OTHER_OWNER].includes(g.group_id));
  for (const g of data.groups) {
    g.role = g.group_id === EDITOR ? 'editor' : g.group_id === VIEWER ? 'viewer' : 'owner';
    g.revision = g.group_id === OWNER ? 7 : 3;
    if (g.active_version) g.active_revision = g.revision;
  }
  data.servers = data.servers.filter(s => s.kind !== 'group' || data.groups.some(g => g.group_id === s.server_id));
  return data;
}

async function managementHarness(browser) {
  const h = await openFixture(browser, managementFixture());
  h.io = { failures: [], holds: [], dialogs: [] };
  h.current = id => h.data.groups.find(g => g.group_id === id);
  h.failNext = (method, id, status) => h.io.failures.push({ method, id, status });
  h.holdDetail = id => {
    let started, release;
    const seen = new Promise(resolve => { started = resolve; });
    const gate = new Promise(resolve => { release = resolve; });
    h.io.holds.push({ id, started, gate });
    return { seen, release };
  };
  const respond = async (route, body, status = 200) => {
    if (status >= 400) h.diagnostics().expectedHttpErrors.push({ url: route.request().url(), status });
    await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
  };
  await h.context.route(`${origin}/api/groups/**`, async route => {
    const request = route.request(), url = new URL(request.url());
    const match = url.pathname.match(/^\/api\/groups\/([^/]+)(?:\/(graph))?$/);
    if (!match) return route.fallback();
    const [, id, resource] = match, method = request.method();
    const failureIndex = h.io.failures.findIndex(f => f.method === method && f.id === (resource ? `${id}/${resource}` : id));
    const holdIndex = !resource && method === 'GET' ? h.io.holds.findIndex(item => item.id === id) : -1;
    if (!['POST', 'DELETE'].includes(method) && failureIndex < 0 && holdIndex < 0) return route.fallback();
    const body = request.postData() ? JSON.parse(request.postData()) : null;
    h.data.requests.push({ method, path: url.pathname.slice(4), query: url.search, body });
    const g = h.current(id);
    if (!g) return respond(route, { error: 'Group not found' }, 404);
    if (failureIndex >= 0) {
      const failure = h.io.failures.splice(failureIndex, 1)[0];
      if (failure.status === 409) { g.revision++; g.active_revision = g.revision; }
      return respond(route, { error: `Offline ${failure.status} management failure` }, failure.status);
    }
    if (holdIndex >= 0) {
      const hold = h.io.holds.splice(holdIndex, 1)[0], snapshot = structuredClone(g);
      hold.started(); await hold.gate;
      return respond(route, { group: snapshot });
    }
    if (resource) return route.fallback();
    if (g.role !== 'owner' && !(method === 'POST' && g.role === 'editor')) return respond(route, { error: 'Management access denied' }, 403);
    if (body?.expected_revision !== g.revision) return respond(route, { error: 'Revision changed' }, 409);
    if (method === 'DELETE') {
      h.data.groups = h.data.groups.filter(item => item.group_id !== id);
      return respond(route, { group_id: id, deleted: true });
    }
    const { expected_revision, ...configuration } = body;
    Object.assign(g, configuration, { revision: g.revision + 1, status: 'STALE', stale: true, data_ready: false });
    return respond(route, { group: g });
  });
  return h;
}

async function openManagement(page) {
  await page.locator('nav.tabs [data-tab="groups"]').click();
  await listRow(page, OWNER).waitFor({ state: 'visible' });
}
async function editorVisible(page, expectedName) {
  await page.locator('#sg-form').waitFor({ state: 'visible' });
  assert.equal(await page.locator('#sg-name').inputValue(), expectedName);
}
async function confirmClick(h, button, accept) {
  const result = await interactConfirmation(h.page, () => button.click(), { accept });
  h.io.dialogs.push({ ...result, accept });
}
async function saveName(h, name) {
  await h.page.locator('#sg-name').fill(name);
  await h.page.locator('#sg-save').click();
}
async function saved(h, id, name) {
  await h.page.waitForFunction(({ id, name }) => S.groups.some(g => g.group_id === id && g.name === name), { id, name });
  assert.equal(await h.page.locator('#sg-form').count(), 0);
}

testCase('owner/editor/viewer management actions are visible on both list surfaces', async h => {
  const { page } = h;
  const viewerHelp = await page.evaluate(() => t('sg.viewerHelp'));
  assert.notEqual(viewerHelp, 'sg.viewerHelp', 'Viewer needs a translated read-only explanation');
  for (const surface of ['my-sources', 'management-list']) {
    if (surface === 'management-list') await openManagement(page);
    const card = id => surface === 'my-sources' ? myRow(page, id) : listRow(page, id);
    for (const id of [OWNER, OTHER_OWNER]) {
      assert.equal(await changeButton(card(id), 'edit').isVisible(), true);
      assert.equal(await changeButton(card(id), 'delete').isVisible(), true);
    }
    assert.equal(await changeButton(card(EDITOR), 'edit').isVisible(), true);
    assert.equal(await changeButton(card(EDITOR), 'delete').count(), 0);
    assert.equal(await changeButton(card(VIEWER), 'edit').count(), 0);
    assert.equal(await changeButton(card(VIEWER), 'delete').count(), 0);
    assert.ok((await card(VIEWER).textContent()).includes(viewerHelp));
  }
  await page.screenshot({ path: path.join(OUT, 'owner-manager-list.png'), fullPage: true });
});

testCase('My Sources edit saves name, descriptions and source-member role using the fresh revision', async h => {
  const { page } = h, g = h.current(OWNER);
  // The already-rendered My Sources row still has revision 7.
  g.revision = 8; g.active_revision = 8;
  await changeButton(myRow(page, OWNER), 'edit').click();
  await editorVisible(page, g.name);
  assert.ok(h.data.requests.some(r => r.method === 'GET' && r.path === `/groups/${OWNER}`));
  assert.equal(h.data.requests.filter(r => r.path.endsWith('/graph')).length, 0, 'Editing must not fetch graph data');
  const name = '수정된·그룹—이름', description = '수정된·설명—원문\n두 번째 줄';
  await page.locator('#sg-name').fill(name);
  await page.locator('#sg-description').fill(description);
  await page.locator('#sg-role-0').selectOption('qa');
  await page.locator('#sg-source-desc-0').fill('소스·역할—설명');
  await page.screenshot({ path: path.join(OUT, 'owner-edit-form.png'), fullPage: true });
  await page.locator('#sg-save').click();
  await saved(h, OWNER, name);
  const post = writes(h);
  assert.equal(post.length, 1);
  assert.equal(post[0].path, `/groups/${OWNER}`);
  assert.equal(post[0].body.expected_revision, 8);
  assert.equal(post[0].body.name, name);
  assert.equal(post[0].body.description, description);
  assert.deepEqual(post[0].body.sources.find(s => s.source_id === ids.NORMAL), {
    source_id: ids.NORMAL, role: 'qa', description: '소스·역할—설명',
  });
  assert.equal(h.current(OWNER).revision, 9);
  await page.locator('nav.tabs [data-tab="repos"]').click();
  assert.equal(await myRow(page, OWNER).locator('strong').textContent(), name);
});

testCase('manager-list deletion confirms the named group, supports cancel, and preserves original sources', async h => {
  const { page } = h, originalRepos = structuredClone(h.data.repos);
  await openManagement(page);
  await confirmClick(h, changeButton(listRow(page, OWNER), 'delete'), false);
  assert.equal(writes(h).length, 0);
  assert.ok(h.io.dialogs[0].message.includes(h.current(OWNER).name));
  assert.match(h.io.dialogs[0].message, /원본 소스/);
  assert.match(await page.locator("#sg-graph").textContent(), /관계 그래프가 준비되어 있습니다/);
  assert.doesNotMatch(await page.locator("#sg-graph").textContent(), /아직 게시된/);
  await confirmClick(h, changeButton(listRow(page, OWNER), 'delete'), true);
  await page.waitForFunction(id => !S.groups.some(g => g.group_id === id), OWNER);
  assert.deepEqual(writes(h).map(r => ({ method: r.method, path: r.path, body: r.body })), [{
    method: 'DELETE', path: `/groups/${OWNER}`, body: { expected_revision: 7 },
  }]);
  assert.equal(await listRow(page, OWNER).count(), 0);
  assert.deepEqual(h.data.repos, originalRepos);
});

testCase('stale owner controls cannot edit after viewer downgrade or delete after editor downgrade', async h => {
  const { page } = h;
  const dialogs = [];
  page.on('dialog', dialog => { dialogs.push(dialog.message()); dialog.dismiss(); });
  for (const [action, role] of [['edit', 'viewer'], ['delete', 'editor']]) {
    h.current(OWNER).role = 'owner';
    await page.evaluate(() => refreshAll());
    await page.locator('nav.tabs [data-tab="repos"]').click();
    const button = changeButton(myRow(page, OWNER), action);
    assert.equal(await button.isVisible(), true);
    h.current(OWNER).role = role;
    const freshRead = observeResponse(page, response =>
      response.url() === `${origin}/api/groups/${OWNER}` && response.request().method() === 'GET');
    await button.click();
    await (await freshRead).finished();
    await page.waitForFunction(role =>
      document.querySelector('#sg-meta .sg-detail-heading > .hint')?.textContent === t(`sg.${role}`), role);
    await nextFrames(page);
    await page.locator('#sg-notice [role="alert"]').waitFor({ state: 'visible' });
    assert.equal(await page.locator('#sg-form').count(), 0);
    assert.ok((await page.locator('#sg-notice').textContent()).includes(await page.evaluate(() => t('sg.actionDenied'))));
  }
  assert.deepEqual(dialogs, []);
  assert.equal(await page.locator('dialog.console-dialog[open]').count(), 0);
  assert.equal(writes(h).length, 0);
});

testCase('save 500 retry and 409 reload keep controls usable and preserve CAS revisions', async h => {
  const { page } = h;
  await openManagement(page);
  await changeButton(listRow(page, OWNER), 'edit').click();
  await editorVisible(page, h.current(OWNER).name);
  h.failNext('POST', OWNER, 500);
  await saveName(h, 'Retry after 500');
  await page.locator('#sg-editor-message [role="alert"]').waitFor({ state: 'visible' });
  assert.equal(await page.locator('#sg-save').isEnabled(), true);
  assert.equal(await page.locator('#sg-name').isEnabled(), true);
  assert.equal(await page.locator('#sg-name').inputValue(), 'Retry after 500');
  await page.locator('#sg-save').click();
  await saved(h, OWNER, 'Retry after 500');
  await changeButton(page.locator('#sg-meta'), 'edit').click();
  await editorVisible(page, 'Retry after 500');
  h.failNext('POST', OWNER, 409);
  await saveName(h, 'Retry after conflict');
  await page.locator('#sg-editor-message [role="alert"]').waitFor({ state: 'visible' });
  assert.equal(await page.locator('#sg-save').isEnabled(), true);
  await page.locator('#sg-editor-message [data-i18n="sg.conflictReload"]').click();
  await page.locator('#sg-meta').waitFor({ state: 'visible' });
  await changeButton(page.locator('#sg-meta'), 'edit').click();
  await editorVisible(page, 'Retry after 500');
  await saveName(h, 'Retry after conflict');
  await saved(h, OWNER, 'Retry after conflict');
  assert.deepEqual(writes(h).map(r => r.body.expected_revision), [7, 7, 8, 9]);
  assert.equal(h.current(OWNER).revision, 10);
});

testCase('DELETE 500 and 409 retries re-read metadata and do not freeze management controls', async h => {
  const { page } = h;
  await openManagement(page);
  for (const status of [500, 409]) {
    h.failNext('DELETE', OWNER, status);
    await confirmClick(h, changeButton(listRow(page, OWNER), 'delete'), true);
    await page.locator('#sg-action-message [role="alert"]').waitFor({ state: 'visible' });
    assert.equal(await changeButton(listRow(page, OWNER), 'delete').isEnabled(), true);
    assert.equal(await changeButton(listRow(page, OWNER), 'edit').isEnabled(), true);
  }
  await confirmClick(h, changeButton(listRow(page, OWNER), 'delete'), true);
  await page.waitForFunction(id => !S.groups.some(g => g.group_id === id), OWNER);
  assert.deepEqual(writes(h).map(r => r.body.expected_revision), [7, 7, 8]);
});

testCase('failed graph read preserves header management controls and editing skips further graph reads', async h => {
  const { page } = h;
  h.failNext('GET', `${OWNER}/graph`, 500);
  await myRow(page, OWNER).getByRole('button', { name: '그룹 상세', exact: true }).click();
  await page.locator('#sg-graph [role="alert"]').waitFor({ state: 'visible' });
  const heading = page.locator('#sg-meta .sg-detail-heading');
  assert.equal(await changeButton(heading, 'edit').isVisible(), true);
  assert.equal(await changeButton(heading, 'delete').isVisible(), true);
  const graphReads = h.data.requests.filter(r => r.path.endsWith('/graph')).length;
  await changeButton(heading, 'edit').click();
  await editorVisible(page, h.current(OWNER).name);
  assert.equal(h.data.requests.filter(r => r.path.endsWith('/graph')).length, graphReads);
  await saveName(h, 'Managed despite graph failure');
  await saved(h, OWNER, 'Managed despite graph failure');
});

testCase('late detail responses cannot edit or delete another group after navigation', async h => {
  const { page } = h;
  await openManagement(page);
  // Force delivery after AbortController cancellation for this exact fake
  // endpoint, exercising generation checks instead of relying on transport.
  await page.evaluate(url => {
    const originalFetch = window.fetch;
    window.fetch = (input, options) => String(input) === url
      ? originalFetch(input, { ...options, signal: undefined })
      : originalFetch(input, options);
  }, `${origin}/api/groups/${OWNER}`);
  const originalOwner = structuredClone(h.current(OWNER));
  const heldEdit = h.holdDetail(OWNER);
  const oldEditResponse = observeResponse(page, response => response.url() === `${origin}/api/groups/${OWNER}`);
  await changeButton(listRow(page, OWNER), 'edit').click();
  await heldEdit.seen;
  await changeButton(listRow(page, EDITOR), 'edit').click();
  await editorVisible(page, h.current(EDITOR).name);
  heldEdit.release();
  await (await oldEditResponse).finished();
  await nextFrames(page);
  assert.equal(await page.locator('#sg-name').inputValue(), h.current(EDITOR).name);
  await saveName(h, 'Only editor group changed');
  await saved(h, EDITOR, 'Only editor group changed');
  assert.deepEqual(writes(h).map(r => r.path), [`/groups/${EDITOR}`]);
  assert.deepEqual(h.current(OWNER), originalOwner);

  const dialogs = [];
  page.on('dialog', dialog => { dialogs.push(dialog.message()); dialog.dismiss(); });
  const heldDelete = h.holdDetail(OWNER);
  const oldDeleteResponse = observeResponse(page, response => response.url() === `${origin}/api/groups/${OWNER}`);
  await changeButton(listRow(page, OWNER), 'delete').click();
  await heldDelete.seen;
  await listRow(page, OTHER_OWNER).locator('.sg-list-open').click();
  await page.waitForFunction(id => document.querySelector('#sg-meta > .sg-id')?.textContent === id, OTHER_OWNER);
  heldDelete.release();
  await (await oldDeleteResponse).finished();
  await nextFrames(page);
  assert.deepEqual(dialogs, [], 'The abandoned delete intent must not prompt for either group');
  assert.equal(await page.locator('dialog.console-dialog[open]').count(), 0);
  assert.equal(writes(h).filter(r => r.method === 'DELETE').length, 0);
  assert.equal(await page.locator('#sg-meta > .sg-id').textContent(), OTHER_OWNER);
});

async function main() {
  fs.mkdirSync(OUT, { recursive: true });
  await prepareAssets();
  const assets = assetHashes();
  const browser = await chromium.launch({ channel: 'chrome', headless: true });
  const browserVersion = browser.version();
  try {
    const selected = cases.filter(item => !process.argv[2] || item.name.toLowerCase().includes(process.argv[2].toLowerCase()));
    assert.ok(selected.length, 'No matching management cases');
    for (const item of selected) {
      const entry = { name: item.name, status: 'PASS' }, start = Date.now();
      let h;
      try {
        h = await managementHarness(browser);
        await item.run(h);
        h.assertClean();
        console.log(`PASS ${item.name}`);
      } catch (error) {
        entry.status = 'FAIL'; entry.error = error.stack || String(error);
        console.error(`FAIL ${item.name}\n${entry.error}`);
        if (h) {
          await h.page.screenshot({ path: path.join(OUT, `failure-${results.length + 1}.png`), fullPage: true }).catch(() => {});
          fs.writeFileSync(path.join(OUT, `failure-${results.length + 1}.html`), await h.page.content().catch(() => ''));
        }
      } finally {
        if (h) {
          entry.diagnostics = h.diagnostics(); entry.dialogs = h.io.dialogs;
          await h.context.close();
        }
        entry.duration_ms = Date.now() - start; results.push(entry);
      }
    }
  } finally {
    await browser.close();
    for (const asset of assets) asset.changedDuringRun =
      crypto.createHash('sha256').update(fs.readFileSync(path.join(ROOT, asset.file))).digest('hex') !== asset.sha256;
    const report = {
      generated_at: new Date().toISOString(), browser: browserVersion, assets,
      status: results.some(r => r.status === 'FAIL') ? 'FAIL' : 'PASS',
      passed: results.filter(r => r.status === 'PASS').length, total: results.length,
      tests: results, scope: 'Actual console management UI; fake HTTP API and synthetic session only. GraphExplorer stub inherited from navigation fixture.',
    };
    const suffix = process.argv[2] ? '-filtered' : '';
    fs.writeFileSync(path.join(OUT, `results${suffix}.json`), JSON.stringify(report, null, 2) + '\n');
    fs.writeFileSync(path.join(OUT, `summary${suffix}.md`), [
      `# Group management: ${report.passed}/${report.total} passed`, '', report.scope, '',
      ...results.map(r => `- ${r.status}: ${r.name}`), '',
      ...results.filter(r => r.error).map(r => `## ${r.name}\n\n\`\`\`text\n${r.error}\n\`\`\`\n`),
    ].join('\n'));
    console.log(`${report.passed}/${report.total} passed. Artifacts: ${OUT}`);
    if (report.status === 'FAIL' || assets.some(a => a.changedDuringRun)) process.exitCode = 1;
  }
}

if (require.main === module) main().catch(error => { console.error(error); process.exitCode = 1; });
