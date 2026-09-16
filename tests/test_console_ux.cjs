#!/usr/bin/env node
'use strict';

/**
 * Bounded whole-console UX regression checks. Run only after the UI is ready:
 * GRAPHIFY_PLAYWRIGHT_MODULE=/existing/path/to/playwright \
 *   node tests/test_console_ux.cjs [case-name-substring ...]
 *
 * Real console assets, synthetic admin/member identities, fake API responses.
 * No installs, AWS, real users, mail, uploads, or live authentication.
 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { chromium } = require(process.env.GRAPHIFY_PLAYWRIGHT_MODULE || 'playwright');
const { prepareAssets, openFixture, fixture, origin, assetHashes, interactConfirmation, observeResponse } = require('./test_console_group_navigation.cjs');
const ROOT = path.resolve(__dirname, '..');
const OUT = process.env.GRAPHIFY_UX_OUT || '/tmp/graphify-group-ui-navigation/ux';
const OWN_A = 'files__ux-owned-a', OWN_B = 'files__ux-owned-b';
const MEMBER = 'github.com__ux__member-source__main';
const SELF = 'ux-self', COUNT = 36;
const USER_TEXT = '고객·원문—보존 (범위 1–2)\n<img src=x onerror="window.__uxInjected=1"> "인용문"';
const LONG_PATH = 'a-long-original-source-path-without-convenient-breaks-'.repeat(4);
const cases = [], results = [];
const testCase = (name, run, options = {}) => cases.push({ name, run, ...options });
const round = page => page.evaluate(() => new Promise(resolve => requestAnimationFrame(resolve)));
const rowFor = (page, table, id) => page.locator(`#${table}-tbody tr`).filter({ hasText: id });
const changeRequests = h => h.data.requests.filter(r => !['GET', 'HEAD'].includes(r.method));

function uxFixture(admin = false) {
  const data = fixture(), normal = data.repos[0];
  data.me = {
    sub: SELF, username: 'Offline UX member', is_admin: admin,
    llm_models: ['global.anthropic.claude-sonnet-5'], llm_default_model: 'global.anthropic.claude-sonnet-5',
  };
  data.groups = [];
  data.repos = Array.from({ length: COUNT }, (_, i) => {
    const sourceType = i < 2 ? 'files' : i === 2 ? 'git' : ['git', 'url', 'files'][i % 3];
    const suffix = String(i).padStart(2, '0');
    const id = i === 0 ? OWN_A : i === 1 ? OWN_B : i === 2 ? MEMBER
      : sourceType === 'files' ? `files__ux-source-${suffix}` : sourceType === 'url' ? `url__ux-${suffix}.example.invalid`
        : `github.com__ux__source-${suffix}__main`;
    const owned = i < 2 || (i > 2 && i % 3 === 0);
    return {
      ...structuredClone(normal), repo_id: id, source_id: id,
      server_name: `ux-source-${String(COUNT - 1 - i).padStart(2, '0')}-a-long-descriptive-name`,
      source_type: sourceType,
      git_url: `https://example.invalid/${LONG_PATH}/${i}/README.md`,
      description: USER_TEXT, owned, manageable: owned, scope_editable: owned,
      graph_scope: owned || i === 4 ? 'private' : 'public', subscriber_count: i + 1,
      created_at: `2026-09-${String(i % 28 + 1).padStart(2, '0')}T00:00:00Z`,
    };
  });
  data.catalog = Array.from({ length: COUNT }, (_, i) => ({
    ...structuredClone(normal), repo_id: `github.com__catalog__record-${String(i).padStart(2, '0')}__main`,
    server_name: `ux-catalog-${String(COUNT - 1 - i).padStart(2, '0')}-a-long-descriptive-name`,
    source_type: ['git', 'url', 'files'][i % 3],
    git_url: `https://example.invalid/${LONG_PATH}/catalog/${i}`,
    owner: `catalog-owner-${i}-${'long.email.'.repeat(8)}@example.invalid`,
    graph_scope: 'public', status: 'READY', owned: false, joined: false,
    subscriber_count: i, created_at: `2026-09-${String(i % 28 + 1).padStart(2, '0')}T00:00:00Z`,
  }));
  data.keys = Array.from({ length: COUNT }, (_, i) => ({
    kid: `ux-key-${String(i).padStart(2, '0')}`, name: `UX key ${String(i).padStart(2, '0')} 고객·이름—${'long name '.repeat(5)}`,
    key_prefix: `gfy_fixture${i}`, last4: String(i).padStart(4, '0'),
    scope_type: 'SERVERS', scope_server_ids: [data.repos[i].repo_id],
    status: i % 5 === 4 ? 'revoked' : 'active',
    created_at: i === 3 ? '2026-08-01T00:00:00Z' : '2026-09-15T00:00:00Z',
    ...(i === 3 ? { expires_at: Date.parse('2026-09-01T00:00:00Z') / 1000 } : {}),
  }));
  data.users = Array.from({ length: COUNT }, (_, i) => ({
    sub: i === 0 ? SELF : `ux-user-${String(i).padStart(2, '0')}`,
    email: `user-${String(i).padStart(2, '0')}-${'long.email.'.repeat(8)}@example.invalid`,
    you: i === 0, is_admin: i % 4 === 0, status: i % 3 === 0 ? 'FORCE_CHANGE_PASSWORD' : 'CONFIRMED',
    created_at: '2026-09-15T00:00:00Z',
  }));
  data.servers = [
    { server_id: 'all', kind: 'hub', runtime_status: 'READY', mcp_url: 'https://mcp.navigation.test/mcp/all' },
    ...data.repos.map(r => ({
      ...r, kind: 'repo', server_id: r.repo_id, build_status: r.status,
      mcp_url: `https://mcp.navigation.test/mcp/${encodeURIComponent(r.repo_id)}`,
    })),
  ];
  return data;
}

async function uxHarness(browser, admin = false) {
  const h = await openFixture(browser, uxFixture(admin));
  h.io = { failures: [], holds: [], members: new Map(), files: new Map(), toolNames: new Map(), dialogs: [] };
  for (const id of [OWN_A, OWN_B]) {
    h.io.members.set(id, [{ sub: `${id}-member`, label: `${id} member·label—kept`, kind: 'member', added_at: '2026-09-15' }]);
    h.io.files.set(id, [{ path: `${id}/file·quote—"kept".md`, size: 125, last_modified: '2026-09-15T00:00:00Z' }]);
  }
  h.fail = (pathname, status = 503) => h.io.failures.push({ pathname, status });
  h.hold = pathname => {
    let started, release;
    const seen = new Promise(resolve => { started = resolve; });
    const gate = new Promise(resolve => { release = resolve; });
    h.io.holds.push({ pathname, gate, started });
    return { seen, release };
  };
  const respond = async (route, body, status = 200) => {
    if (status >= 400) h.diagnostics().expectedHttpErrors.push({ url: route.request().url(), status });
    await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
  };
  await h.context.route(`${origin}/api/**`, async route => {
    const request = route.request(), url = new URL(request.url()), method = request.method();
    const pathname = url.pathname.slice(4), body = request.postData() ? JSON.parse(request.postData()) : null;
    const failureIndex = h.io.failures.findIndex(f => f.pathname === pathname);
    if (failureIndex >= 0) {
      const { status } = h.io.failures.splice(failureIndex, 1)[0];
      h.data.requests.push({ method, path: pathname, body });
      return respond(route, { error: 'Intentional offline list failure' }, status);
    }
    const adminHold = method === 'GET' && pathname === '/admin/users'
      ? h.io.holds.findIndex(item => item.pathname === pathname) : -1;
    if (adminHold >= 0) {
      const held = h.io.holds.splice(adminHold, 1)[0], response = { users: structuredClone(h.data.users) };
      h.data.requests.push({ method, path: pathname, body });
      held.started(); await held.gate;
      return respond(route, response);
    }
    const repoMatch = pathname.match(/^\/repos\/([^/]+)(?:\/(members(?:\/remove)?|uploads(?:\/delete)?|name|rebuild|join))?$/);
    if (repoMatch) {
      const id = decodeURIComponent(repoMatch[1]), operation = repoMatch[2];
      const repo = h.data.repos.find(r => r.repo_id === id);
      const read = method === 'GET' && ['members', 'uploads', undefined].includes(operation);
      const write = method !== 'GET' && ['members/remove', 'uploads/delete', 'name', 'rebuild', 'join', undefined].includes(operation);
      if (!read && !write) return route.fallback();
      h.data.requests.push({ method, path: pathname, body });
      if (read) {
        if (!repo) return respond(route, { error: 'Source not found' }, 404);
        // GET /repos/{id} returns the source object itself, not an envelope.
        const response = !operation ? structuredClone(repo)
          : operation === 'members' ? { members: structuredClone(h.io.members.get(id) || []) }
          : { files: structuredClone(h.io.files.get(id) || []), total_bytes: 125 };
        const held = h.io.holds.findIndex(item => item.pathname === pathname);
        if (held >= 0) { const item = h.io.holds.splice(held, 1)[0]; item.started(); await item.gate; }
        return respond(route, response);
      }
      if (operation === 'join' && method === 'POST') {
        const catalog = h.data.catalog.find(r => r.repo_id === id);
        assert.ok(catalog, 'Join must target a fixture catalog record');
        catalog.joined = true;
        h.data.repos.push({ ...structuredClone(catalog), manageable: false, owned: false, scope_editable: false });
        return respond(route, { repo_id: id, joined: true });
      }
      assert.ok(repo, `Write must target a fixture source: ${pathname}`);
      if (!operation && method === 'DELETE') {
        h.data.repos = h.data.repos.filter(r => r.repo_id !== id);
        h.data.servers = h.data.servers.filter(s => s.server_id !== id);
        return respond(route, { repo_id: id });
      }
      if (operation === 'name' && method === 'POST') {
        repo.server_name = body.server_name;
        const server = h.data.servers.find(s => s.server_id === id);
        if (server) server.server_name = body.server_name;
        return respond(route, { repo_id: id, server_name: body.server_name });
      }
      if (operation === 'rebuild' && method === 'POST') return respond(route, { repo_id: id, build_id: `offline-${id}` }, 202);
      if (operation === 'members/remove' && method === 'POST') {
        h.io.members.set(id, (h.io.members.get(id) || []).filter(m => m.sub !== body.sub));
        return respond(route, { repo_id: id });
      }
      if (operation === 'uploads/delete' && method === 'POST') {
        h.io.files.set(id, (h.io.files.get(id) || []).filter(file => !body.paths.includes(file.path)));
        return respond(route, { repo_id: id });
      }
      throw new Error(`Unsupported fixture write ${method} ${pathname}`);
    }
    if (method === 'DELETE' && pathname.startsWith('/keys/')) {
      h.data.requests.push({ method, path: pathname, body });
      const key = h.data.keys.find(k => k.kid === pathname.slice(6));
      assert.ok(key, 'Key revoke must target a fixture key'); key.status = 'revoked';
      return respond(route, { revoked: true });
    }
    if (method === 'POST' && ['/admin/users/reset-password', '/admin/users/delete'].includes(pathname)) {
      h.data.requests.push({ method, path: pathname, body });
      const user = h.data.users.find(u => u.sub === body.sub);
      if (!h.data.me.is_admin || !user || (pathname.endsWith('/delete') && user.you)) {
        return respond(route, { error: 'Forbidden fixture admin action' }, 403);
      }
      if (pathname.endsWith('/delete')) {
        h.data.users = h.data.users.filter(u => u.sub !== body.sub);
      }
      return respond(route, { revoked_keys: 0, dropped_grants: 0 });
    }
    return route.fallback();
  });
  await h.context.route(`${origin}/pgstream`, async route => {
    const body = JSON.parse(route.request().postData());
    if (body.op !== 'mcp') return route.fallback();
    h.data.requests.push({ method: 'POST', path: '/pgstream', body });
    const method = body.payload?.method;
    const heldIndex = h.io.holds.findIndex(item => item.pathname === `mcp:${method}:${body.server_id}`);
    if (heldIndex >= 0) { const held = h.io.holds.splice(heldIndex, 1)[0]; held.started(); await held.gate; }
    const tool = name => ({
      name, description: `Private fixture tool ${name}`,
      inputSchema: { type: 'object', properties: { query: { type: 'string' } }, required: ['query'] },
    });
    const result = method === 'tools/list' ? { tools: (h.io.toolNames.get(body.server_id) || ['search_code', 'query_graph']).map(tool) }
      : { content: [{ type: 'text', text: `DIRECT_RESULT:${body.server_id}` }] };
    await route.fulfill({
      contentType: 'text/event-stream',
      body: `data: ${JSON.stringify({ type: 'mcp', ok: true, status: 200,
        body: { jsonrpc: '2.0', id: body.payload.id, result } })}\n\n`,
    });
  });
  await h.context.route(`${origin}/unused-auth/oauth2/token`, async route => {
    const fields = new URLSearchParams(route.request().postData());
    assert.equal(fields.get('grant_type'), 'refresh_token');
    h.data.requests.push({ method: 'POST', path: '/unused-auth/oauth2/token', body: { grant_type: 'refresh_token' } });
    const index = h.io.holds.findIndex(item => item.pathname === 'token-refresh');
    if (index >= 0) { const held = h.io.holds.splice(index, 1)[0]; held.started(); await held.gate; }
    await respond(route, {
      access_token: 'STALE_OFFLINE_ACCESS_NOT_A_CREDENTIAL',
      id_token: 'STALE_OFFLINE_ID_NOT_A_CREDENTIAL',
      refresh_token: 'STALE_OFFLINE_REFRESH_NOT_A_CREDENTIAL',
      expires_in: 3600,
    });
  });
  if (admin) await h.page.waitForFunction(() => (S.users || []).length > 20);
  return h;
}

async function goto(page, tab) { await page.locator(`nav.tabs button[data-tab="${tab}"]`).click(); }
const panelDialog = (page, id) => page.locator(`dialog.console-panel-dialog[data-panel-id="${id}"]`);
async function assertPanelOpen(page, id) {
  const dialog = panelDialog(page, id);
  await dialog.waitFor({ state: 'visible' });
  assert.equal(await dialog.evaluate((dialog, id) => {
    const content = document.getElementById(id);
    return dialog instanceof HTMLDialogElement && dialog.open && dialog.matches(':modal')
      && !!content && !content.hidden && !!dialog.querySelector('.console-panel__body')?.contains(content);
  }, id), true, `${id}: native modal contains the original panel body`);
  return dialog;
}
async function closePanelModal(page, id) {
  const dialog = panelDialog(page, id);
  await dialog.locator('.console-panel__header [data-panel-close], .console-panel__header .console-panel__close').first().click();
  await dialog.waitFor({ state: 'hidden' });
  // Static panels remain hidden; dynamically mounted group editors dispose.
  await page.waitForFunction(id => !document.getElementById(id) || document.getElementById(id).hidden, id);
  if (await dialog.count()) assert.equal(await dialog.evaluate(node => node.open), false, `${id}: native dialog closed`);
}
async function translatedButton(page, within, key) {
  return within.getByRole('button', { name: await page.evaluate(key => t(key), key), exact: true });
}
async function search(page, control, value) { await page.locator(`#${control}`).fill(value); await round(page); }
async function choose(page, control, value) {
  const combo = page.locator(`#${control}-combo`);
  if (await combo.count()) {
    await combo.click();
    await page.locator(`#${control}-list [data-value="${value}"]`).click();
  } else await page.locator(`#${control}`).selectOption(value);
  await round(page);
}
async function confirmAction(h, button, answer = true, promptText) {
  let result;
  if (await h.page.locator('dialog.console-panel-dialog[open]').count()) {
    // The shared navigation helper also matches parent panels. Scope nested
    // confirmations to their action dialog without closing the parent.
    const child = h.page.locator('dialog.console-dialog:not(.console-panel-dialog)[open]');
    await button.click();
    await child.waitFor({ state: 'visible' });
    assert.equal(await child.evaluate(node => node.matches(':modal')), true);
    const message = await child.textContent();
    if (answer && promptText !== undefined) await child.locator('.console-dialog__input').fill(promptText);
    await child.locator(answer ? '.console-dialog__submit' : '.console-dialog__cancel').click();
    await child.waitFor({ state: 'hidden' });
    result = { kind: 'html', message };
  } else {
    result = await interactConfirmation(h.page, () => button.click(), { accept: answer, value: promptText });
  }
  h.io.dialogs.push({ ...result, answer });
}
async function scopes(page) {
  return page.evaluate(() => Object.fromEntries(['key-scope', 'play-server'].map(id => [
    id, [...document.getElementById(id).options].filter(o => !o.disabled).map(o => o.value).sort(),
  ])));
}
async function rootFits(page, label) {
  const bounds = await page.evaluate(() => ({ viewport: innerWidth, width: document.documentElement.scrollWidth }));
  assert.ok(bounds.width <= bounds.viewport, `${label}: ${JSON.stringify(bounds)}`);
}
async function assertEnabledWithoutDisabledAttribute(button) {
  assert.equal(await button.isEnabled(), true);
  assert.equal(await button.getAttribute('disabled'), null, 'An enabled control must not serialize disabled="null"');
}

testCase('sources open list-first and registration opens/closes without losing input', async h => {
  const { page } = h;
  const original = await page.locator('#registration-panel').elementHandle();
  assert.equal(await page.locator('#registration-panel').isVisible(), false);
  assert.equal(await page.locator('#registration-panel').evaluate(node => node.hidden), true);
  assert.equal(await page.locator('#repos-search').isVisible(), true);
  assert.equal(await page.locator('#repos-tbody').isVisible(), true);
  assert.equal(await page.locator('dialog[open]').count(), 0, 'Sources start with a list and no open task form');
  await page.locator('#btn-register-open').click();
  await assertPanelOpen(page, 'registration-panel');
  assert.equal(await page.locator('#registration-panel').evaluate((node, original) => node === original, original), true);
  assert.equal(await page.locator('#registration-panel').isVisible(), true);
  assert.equal(await page.locator('#btn-register-open').getAttribute('aria-expanded'), 'true');
  await page.locator('#reg-url').fill('https://example.invalid/keep-this-unsaved-source');
  await closePanelModal(page, 'registration-panel');
  assert.equal(await page.locator('#registration-panel').isVisible(), false);
  await page.waitForFunction(() => document.getElementById('btn-register-open') === document.activeElement);
  await page.locator('#btn-register-open').click();
  await assertPanelOpen(page, 'registration-panel');
  assert.equal(await page.locator('#reg-url').inputValue(), 'https://example.invalid/keep-this-unsaved-source');
  await closePanelModal(page, 'registration-panel');
  assert.equal(changeRequests(h).length, 0);
});

testCase('source search, paging, type filter and display-name sorting work on 36 records', async h => {
  const { page, data } = h;
  assert.equal(await page.locator('#repos-tbody > tr').count(), 25);
  const firstPage = await page.locator('#repos-tbody').textContent();
  await (await translatedButton(page, page.locator('#pager-repos'), 'pg.next')).click();
  assert.equal(await page.locator('#repos-tbody > tr').count(), 11);
  assert.notEqual(await page.locator('#repos-tbody').textContent(), firstPage);
  await search(page, 'repos-search', OWN_A);
  assert.equal(await page.locator('#repos-tbody > tr').count(), 1);
  assert.equal(await rowFor(page, 'repos', OWN_A).count(), 1);
  await search(page, 'repos-search', '');
  await choose(page, 'repos-type-filter', 'files');
  const matches = data.repos.filter(r => r.source_type === 'files');
  assert.equal(await page.locator('#repos-tbody > tr').count(), matches.length);
  for (const r of matches) assert.equal(await rowFor(page, 'repos', r.repo_id).count(), 1);
  await choose(page, 'repos-type-filter', '');
  await choose(page, 'repos-sort', 'newest');
  await choose(page, 'repos-sort', 'name');
  const expected = [...data.repos].sort((a, b) => a.server_name.localeCompare(b.server_name));
  assert.ok((await page.locator('#repos-tbody > tr').first().textContent()).includes(expected[0].server_name),
    'Name sort must follow the displayed name, not a differently ordered raw ID');
  await choose(page, 'repos-sort', 'newest');
  const newest = [...data.repos].sort((a, b) => b.created_at.localeCompare(a.created_at))[0];
  assert.ok((await page.locator('#repos-tbody > tr').first().textContent()).includes(newest.repo_id));
});

testCase('catalog search, pagination and sorting preserve complete catalog data', async h => {
  const { page, data } = h;
  const before = structuredClone(data.catalog);
  await page.locator('#catalog-search').scrollIntoViewIfNeeded();
  assert.equal(await page.locator('#catalog-tbody > tr').count(), 25);
  await (await translatedButton(page, page.locator('#pager-catalog'), 'pg.next')).click();
  assert.equal(await page.locator('#catalog-tbody > tr').count(), 11);
  await search(page, 'catalog-search', data.catalog[30].repo_id);
  assert.equal(await page.locator('#catalog-tbody > tr').count(), 1);
  await search(page, 'catalog-search', '');
  await choose(page, 'catalog-sort', 'newest');
  await choose(page, 'catalog-sort', 'name');
  const nameFirst = [...data.catalog].sort((a, b) => a.server_name.localeCompare(b.server_name))[0];
  assert.ok((await page.locator('#catalog-tbody > tr').first().textContent()).includes(nameFirst.server_name));
  assert.deepEqual(data.catalog, before);
});

testCase('source/server/key filters never narrow API key or Playground scope options', async h => {
  const { page, data } = h, before = await scopes(page);
  await search(page, 'repos-search', OWN_A);
  await choose(page, 'repos-type-filter', 'files');
  assert.deepEqual(await scopes(page), before);
  await goto(page, 'servers');
  await search(page, 'servers-search', MEMBER);
  assert.ok((await page.locator('#servers-list').textContent()).includes(MEMBER));
  assert.ok(!(await page.locator('#servers-list').textContent()).includes(OWN_A));
  assert.deepEqual(await scopes(page), before);
  await goto(page, 'keys');
  await search(page, 'keys-search', 'UX key 30');
  assert.equal(await page.locator('#keys-tbody > tr').count(), 1);
  assert.ok((await page.locator('#keys-tbody').textContent()).includes(data.keys[30].name));
  assert.deepEqual(await scopes(page), before);
});

testCase('admin users support paging/search/role filtering and no self-delete', async h => {
  const { page, data } = h;
  await goto(page, 'admin');
  assert.equal(await page.locator('#users-tbody > tr').count(), 25);
  await (await translatedButton(page, page.locator('#pager-users'), 'pg.next')).click();
  assert.equal(await page.locator('#users-tbody > tr').count(), 11);
  await choose(page, 'users-role-filter', 'admin');
  assert.equal(await page.locator('#users-tbody > tr').count(), data.users.filter(u => u.is_admin).length);
  await choose(page, 'users-role-filter', '');
  await search(page, 'users-search', data.users[0].email);
  const selfRow = page.locator('#users-tbody tr').filter({ hasText: data.users[0].email });
  assert.equal(await (await translatedButton(page, selfRow, 'adm.delete')).count(), 0);
  const dialogs = [];
  const unexpectedDialog = dialog => { dialogs.push(dialog.message()); dialog.dismiss(); };
  page.on('dialog', unexpectedDialog);
  await page.evaluate(user => deleteUser(user), data.users[0]);
  await round(page);
  assert.equal(dialogs.length, 0, 'Self-delete must also be guarded at the handler');
  assert.equal(await page.locator('dialog.console-dialog[open]').count(), 0);
  assert.equal(changeRequests(h).filter(r => r.path.startsWith('/admin/')).length, 0);
  page.off('dialog', unexpectedDialog);
  const target = data.users[1];
  await search(page, 'users-search', target.email);
  const targetRow = page.locator('#users-tbody tr').filter({ hasText: target.email });
  await assertEnabledWithoutDisabledAttribute(await translatedButton(page, targetRow, 'adm.delete'));
  await confirmAction(h, await translatedButton(page, targetRow, 'adm.reset'));
  await confirmAction(h, await translatedButton(page, targetRow, 'adm.delete'));
  await page.waitForFunction(sub => !S.users.some(u => u.sub === sub), target.sub);
  assert.deepEqual(changeRequests(h).map(r => [r.path, r.body]), [
    ['/admin/users/reset-password', { sub: target.sub }], ['/admin/users/delete', { sub: target.sub }],
  ]);
}, { admin: true });

testCase('owner source actions rename, rebuild and delete only the selected source', async h => {
  const { page, data } = h;
  const other = structuredClone(data.repos.find(r => r.repo_id === OWN_B));
  await search(page, 'repos-search', OWN_A);
  const row = rowFor(page, 'repos', OWN_A);
  await confirmAction(h, await translatedButton(page, row, 'srv.rename'), true, 'renamed-ux-source');
  await page.waitForFunction(id => S.repos.find(r => r.repo_id === id)?.server_name === 'renamed-ux-source', OWN_A);
  const rebuilt = observeResponse(page, response => response.url().endsWith(`/repos/${OWN_A}/rebuild`));
  await (await translatedButton(page, rowFor(page, 'repos', OWN_A), 'repo.rebuild')).click();
  await rebuilt;
  await confirmAction(h, await translatedButton(page, rowFor(page, 'repos', OWN_A), 'ux.deleteSource'), false);
  assert.equal(changeRequests(h).filter(r => r.method === 'DELETE').length, 0);
  await confirmAction(h, await translatedButton(page, rowFor(page, 'repos', OWN_A), 'ux.deleteSource'));
  await page.waitForFunction(id => !S.repos.some(r => r.repo_id === id), OWN_A);
  const changed = changeRequests(h);
  assert.deepEqual(changed.map(r => [r.method, r.path]), [
    ['POST', `/repos/${OWN_A}/name`], ['POST', `/repos/${OWN_A}/rebuild`], ['DELETE', `/repos/${OWN_A}`],
  ]);
  assert.deepEqual(changed[0].body, { server_name: 'renamed-ux-source' });
  assert.equal(data.requests.filter(r => r.method === 'GET' && r.path === `/repos/${OWN_A}`).length, 2,
    'Each removal attempt must fetch fresh source metadata before confirmation');
  assert.deepEqual(data.repos.find(r => r.repo_id === OWN_B), other);
});

testCase('member source actions omit owner controls and catalog join/unsubscribe use exact IDs', async h => {
  const { page, data } = h;
  await search(page, 'repos-search', MEMBER);
  const member = rowFor(page, 'repos', MEMBER);
  for (const key of ['srv.rename', 'sg.editDescription', 'mp.open', 'ux.deleteSource']) {
    assert.equal(await (await translatedButton(page, member, key)).count(), 0, key);
  }
  const target = data.catalog[0].repo_id;
  await search(page, 'catalog-search', target);
  await (await translatedButton(page, rowFor(page, 'catalog', target), 'cat.join')).click();
  await page.waitForFunction(id => S.repos.some(r => r.repo_id === id), target);
  await search(page, 'repos-search', MEMBER);
  await confirmAction(h, await translatedButton(page, rowFor(page, 'repos', MEMBER), 'ux.unsubscribe'));
  await page.waitForFunction(id => !S.repos.some(r => r.repo_id === id), MEMBER);
  const privateMember = data.repos.find(r => !r.owned && r.graph_scope === 'private').repo_id;
  await search(page, 'repos-search', privateMember);
  assert.equal(await (await translatedButton(page, rowFor(page, 'repos', privateMember), 'ux.deleteSource')).count(), 0);
  await confirmAction(h, await translatedButton(page, rowFor(page, 'repos', privateMember), 'ux.leaveSource'));
  await page.waitForFunction(id => !S.repos.some(r => r.repo_id === id), privateMember);
  assert.deepEqual(changeRequests(h).map(r => [r.method, r.path]), [
    ['POST', `/repos/${target}/join`], ['DELETE', `/repos/${MEMBER}`], ['DELETE', `/repos/${privateMember}`],
  ]);
  for (const id of [MEMBER, privateMember]) assert.ok(data.requests.some(r => r.method === 'GET' && r.path === `/repos/${id}`));
  assert.equal(data.catalog.length, COUNT);
});

testCase('key/user lists open first and modal entry controls plus fake key revoke remain usable', async h => {
  const { page, data } = h;
  for (const [tab, tbody, form, opener, input, label] of [
    ['keys', 'keys-tbody', 'key-form', 'btn-key-open', 'key-name', 'key.new'],
    ['admin', 'users-tbody', 'invite-form', 'btn-invite-open', 'inv-email', 'adm.invite'],
  ]) {
    await goto(page, tab);
    assert.equal(await page.locator(`#${tbody}`).isVisible(), true);
    assert.equal(await page.locator(`#${tbody} > tr`).count(), 25);
    assert.equal(await page.locator(`#${form}`).evaluate(node => node.hidden), true);
    assert.equal(await page.locator(`#${input}`).isVisible(), false);
    assert.equal(await page.locator('dialog[open]').count(), 0);
    const button = page.locator(`#${opener}`);
    assert.equal(await button.textContent(), await page.evaluate(key => t(key), label));
    await button.click();
    await assertPanelOpen(page, form);
    assert.equal(await page.locator(`#${form}`).isVisible(), true);
    await page.locator(`#${input}`).scrollIntoViewIfNeeded();
    await page.waitForFunction(input => {
      const bounds = document.getElementById(input).getBoundingClientRect();
      return bounds.top >= 0 && bounds.top < innerHeight;
    }, input);
    await closePanelModal(page, form);
    await page.waitForFunction(opener => document.getElementById(opener) === document.activeElement, opener);
  }
  await goto(page, 'keys');
  await search(page, 'keys-search', 'UX key 03');
  assert.equal(await page.locator('#keys-tbody tr .record-state .pill').textContent(),
    await page.evaluate(() => t('ux.keyExpired')));
  await search(page, 'keys-search', 'UX key 00');
  await confirmAction(h, await translatedButton(page, page.locator('#keys-tbody tr'), 'key.revoke'));
  await page.waitForFunction(() => S.keys.find(k => k.kid === 'ux-key-00')?.status === 'revoked');
  assert.equal(data.keys[0].status, 'revoked');
  assert.deepEqual(changeRequests(h).map(r => r.path), ['/keys/ux-key-00']);
}, { admin: true });

testCase('navigation remains buttons with aria-current, keyboard navigation and member admin guard', async h => {
  const { page } = h;
  assert.equal(await page.locator('nav.tabs [role="tab"]').count(), 0);
  assert.equal(await page.locator('nav.tabs button[data-tab="admin"]').isVisible(), false);
  await goto(page, 'repos');
  const visibleTabs = await page.locator('nav.tabs button:visible').evaluateAll(buttons => buttons.map(b => b.dataset.tab));
  for (const [key, expected] of [['End', visibleTabs.at(-1)], ['Home', visibleTabs[0]], ['ArrowRight', visibleTabs[1]]]) {
    await page.keyboard.press(key);
    const active = page.locator(`nav.tabs button[data-tab="${expected}"]`);
    assert.ok(['page', 'true'].includes(await active.getAttribute('aria-current')));
    assert.equal(await active.evaluate(e => e === document.activeElement), true);
  }
  await page.evaluate(() => { switchTab('admin'); return loadUsers(); });
  assert.equal(await page.locator('#page-admin').isVisible(), false);
  assert.equal(h.data.requests.filter(r => r.path.startsWith('/admin/')).length, 0);
});

testCase('catalog/admin failures show retry errors rather than silently empty lists', async h => {
  const { page } = h;
  for (const [tab, apiPath, refresh, errorBox, table] of [
    ['repos', '/catalog', 'btn-catalog-refresh', 'catalog-error', 'catalog-tbody'],
    ['admin', '/admin/users', 'btn-users-refresh', 'users-error', 'users-tbody'],
  ]) {
    await goto(page, tab);
    h.fail(apiPath);
    await page.locator(`#${refresh}`).click();
    const error = page.locator(`#${errorBox} [role="alert"]`);
    await error.waitFor({ state: 'visible' });
    assert.ok((await error.textContent()).trim().length > 10);
    const retry = await translatedButton(page, error, 'common.refresh');
    const recovered = observeResponse(page, response =>
      response.url() === `${origin}/api${apiPath}` && response.request().method() === 'GET' && response.status() === 200);
    await retry.click();
    await (await recovered).finished();
    await error.waitFor({ state: 'hidden' });
    await page.waitForFunction(table => document.querySelectorAll(`#${table} > tr`).length === 25, table);
    assert.equal(await page.locator(`#${table} > tr`).count(), 25);
  }
}, { admin: true });

async function ignoreCancellation(page, pathname) {
  await page.evaluate(url => {
    const original = window.fetch;
    window.fetch = (input, options) => String(input) === url
      ? original(input, { ...options, signal: undefined }) : original(input, options);
  }, `${origin}/api${pathname}`);
}
async function sourceAction(h, id, key) {
  await search(h.page, 'repos-search', id);
  await (await translatedButton(h.page, rowFor(h.page, 'repos', id), key)).click();
}

testCase('late source-member reads cannot replace another source or retarget removal', async h => {
  const { page } = h, pathname = `/repos/${OWN_A}/members`;
  await ignoreCancellation(page, pathname);
  const held = h.hold(pathname);
  const oldResponse = observeResponse(page, response => response.url() === `${origin}/api${pathname}`);
  await sourceAction(h, OWN_A, 'mp.open');
  await assertPanelOpen(page, 'members-panel');
  await held.seen;
  await closePanelModal(page, 'members-panel');
  await sourceAction(h, OWN_B, 'mp.open');
  await assertPanelOpen(page, 'members-panel');
  await page.waitForFunction(id => document.getElementById('mp-tbody').textContent.includes(id), OWN_B);
  held.release(); await (await oldResponse).finished(); await round(page); await round(page);
  assert.equal(await page.locator('#mp-repo').textContent(), OWN_B);
  assert.ok(!(await page.locator('#mp-tbody').textContent()).includes(OWN_A));
  const remove = await translatedButton(page, page.locator('#mp-tbody tr'), 'mp.remove');
  await assertEnabledWithoutDisabledAttribute(remove);
  await confirmAction(h, remove);
  await page.waitForFunction(() => !document.querySelector('#mp-tbody button'));
  assert.deepEqual(changeRequests(h).map(r => [r.path, r.body]), [
    [`/repos/${OWN_B}/members/remove`, { sub: `${OWN_B}-member` }],
  ]);
});

testCase('late file reads preserve current source and exact quoted deletion path', async h => {
  const { page } = h, pathname = `/repos/${OWN_A}/uploads`;
  const file = h.io.files.get(OWN_B)[0].path;
  await ignoreCancellation(page, pathname);
  const held = h.hold(pathname);
  const oldResponse = observeResponse(page, response => response.url() === `${origin}/api${pathname}`);
  await sourceAction(h, OWN_A, 'fp.files');
  await assertPanelOpen(page, 'files-panel');
  await held.seen;
  await closePanelModal(page, 'files-panel');
  await sourceAction(h, OWN_B, 'fp.files');
  await assertPanelOpen(page, 'files-panel');
  await page.waitForFunction(id => document.getElementById('fp-tbody').textContent.includes(id), OWN_B);
  held.release(); await (await oldResponse).finished(); await round(page); await round(page);
  assert.equal(await page.locator('#fp-repo').textContent(), OWN_B);
  assert.equal(await page.locator('#fp-tbody tr .record-name').textContent(), file);
  const remove = await translatedButton(page, page.locator('#fp-tbody tr'), 'fp.delete');
  await assertEnabledWithoutDisabledAttribute(remove);
  await confirmAction(h, remove);
  await page.waitForFunction(() => !document.querySelector('#fp-tbody button'));
  assert.deepEqual(changeRequests(h).map(r => [r.path, r.body]), [
    [`/repos/${OWN_B}/uploads/delete`, { paths: [file] }],
  ]);
});

testCase('long user content remains literal and three-column record tables use data labels', async h => {
  const { page, data } = h;
  await search(page, 'repos-search', OWN_A);
  const source = rowFor(page, 'repos', OWN_A);
  assert.equal(await source.locator('td').count(), 3);
  assert.equal(await source.locator('td[data-label]').count(), 3);
  assert.ok((await source.textContent()).includes(USER_TEXT));
  await (await translatedButton(page, source, 'sg.editDescription')).click();
  const descriptionDialog = await assertPanelOpen(page, 'sg-description-editor');
  assert.equal(await page.locator('#sg-common-description').inputValue(), USER_TEXT);
  await (await translatedButton(page, descriptionDialog, 'sg.cancel')).click();
  await descriptionDialog.waitFor({ state: 'hidden' });
  assert.equal(await source.locator('img').count(), 0);
  assert.equal(await page.evaluate(() => window.__uxInjected || 0), 0);
  await goto(page, 'admin');
  await search(page, 'users-search', data.users[1].email);
  assert.ok((await page.locator('#users-tbody').textContent()).includes(data.users[1].email));
}, { admin: true });

testCase('empty data and zero matches have explanatory states and a working clear-filter action', async h => {
  const { page, data } = h;
  for (const [tab, control, target] of [
    ['repos', 'repos-search', 'repos-tbody'], ['repos', 'catalog-search', 'catalog-tbody'],
    ['servers', 'servers-search', 'servers-list'], ['keys', 'keys-search', 'keys-tbody'],
    ['admin', 'users-search', 'users-tbody'],
  ]) {
    await goto(page, tab); await search(page, control, 'no-such-fixture-record');
    const container = page.locator(`#${target}`);
    assert.ok((await container.textContent()).includes(await page.evaluate(() => t('ux.noMatches'))));
    await (await translatedButton(page, container, 'ux.clearFilters')).click();
    assert.equal(await page.locator(`#${control}`).inputValue(), '');
    assert.ok(!(await container.textContent()).includes(await page.evaluate(() => t('ux.noMatches'))));
  }
  data.repos = []; data.catalog = []; data.servers = []; data.keys = []; data.users = [];
  await page.evaluate(() => refreshAll());
  await page.evaluate(() => loadUsers());
  for (const [tab, target] of [
    ['repos', 'repos-tbody'], ['repos', 'catalog-tbody'], ['servers', 'servers-list'],
    ['keys', 'keys-tbody'], ['admin', 'users-tbody'],
  ]) {
    await goto(page, tab);
    assert.ok((await page.locator(`#${target}`).textContent()).trim().length > 10, `${target} must explain the empty state`);
  }
}, { admin: true });

testCase('desktop records expose visible view/manage action rows without horizontal panning', async h => {
  const { page } = h;
  await search(page, 'repos-search', OWN_A);
  const source = rowFor(page, 'repos', OWN_A);
  const actionRows = source.locator('.record-actions > div');
  assert.equal(await actionRows.count(), 2);
  for (const actionRow of await actionRows.all()) assert.equal(await actionRow.isVisible(), true);
  assert.deepEqual(await source.locator('.record-actions .action-label').allTextContents(),
    await page.evaluate(() => [t('ux.viewActions'), t('ux.manageActions')]));
  const bounds = await source.locator('td').last().locator('button').evaluateAll(buttons =>
    buttons.filter(b => b.getClientRects().length).map(b => {
      const r = b.getBoundingClientRect(); return { top: Math.round(r.top), left: r.left, right: r.right };
    }));
  assert.ok(bounds.length >= 5, 'An owned private file source must expose its actions');
  assert.ok(new Set(bounds.map(b => b.top)).size >= 2, 'View/manage action groups must occupy separate rows and may wrap');
  assert.ok(bounds.every(b => b.left >= 0 && b.right <= 1440));
  await rootFits(page, 'desktop sources');
  await page.screenshot({ path: path.join(OUT, 'desktop-source-actions.png'), fullPage: true });
  await goto(page, 'admin'); await search(page, 'users-search', 'user-01');
  await rootFits(page, 'desktop admin');
  await page.screenshot({ path: path.join(OUT, 'desktop-admin.png'), fullPage: true });
}, { admin: true });

testCase('mobile long records, forms and action areas fit the root viewport without panning', async h => {
  const { page } = h;
  await page.setViewportSize({ width: 390, height: 844 });
  for (const [tab, control, query, table] of [
    ['repos', 'repos-search', OWN_A, 'repos-tbody'],
    ['repos', 'catalog-search', 'record-00', 'catalog-tbody'],
    ['servers', 'servers-search', OWN_A, 'servers-list'],
    ['keys', 'keys-search', 'UX key 00', 'keys-tbody'],
    ['admin', 'users-search', 'user-01', 'users-tbody'],
  ]) {
    await goto(page, tab); await search(page, control, query);
    await page.locator(`#${table}`).scrollIntoViewIfNeeded();
    await rootFits(page, `mobile ${table}`);
    const buttons = await page.locator(`#${table} button`).evaluateAll(buttons => buttons
      .filter(b => b.getClientRects().length).map(b => { const r = b.getBoundingClientRect(); return { label: b.textContent, left: r.left, right: r.right }; }));
    assert.ok(buttons.every(b => b.left >= 0 && b.right <= 390), `Offscreen actions: ${JSON.stringify(buttons)}`);
    await page.screenshot({ path: path.join(OUT, `mobile-${table}.png`), fullPage: true });
  }
  await goto(page, 'repos');
  await page.locator('#btn-register-open').click();
  const registration = await assertPanelOpen(page, 'registration-panel');
  await rootFits(page, 'mobile registration');
  assert.equal(await registration.evaluate(dialog => {
    const body = dialog.querySelector('.console-panel__body');
    return body.scrollWidth <= body.clientWidth + 1;
  }), true, 'Mobile registration also fits its own modal body');
  await registration.screenshot({ path: path.join(OUT, 'mobile-registration.png'), animations: 'disabled' });
  await closePanelModal(page, 'registration-panel');
}, { admin: true });

testCase('showGate prevents late admin/tools/token responses from restoring private state', async h => {
  const { page } = h;
  const admin = h.hold('/admin/users'), tools = h.hold(`mcp:tools/list:${OWN_A}`), token = h.hold('token-refresh');
  await page.evaluate(id => {
    const reset = GraphExplorer.reset;
    window.__uxGraphResets = 0;
    GraphExplorer.reset = (...args) => { window.__uxGraphResets++; return reset(...args); };
    openGraph(id);
    S.repos.find(repo => repo.repo_id === id).status = 'BUILDING';
    pollBuilds();
    document.getElementById('play-server').value = id;
    sessionStorage.setItem('gfy-refresh', 'OFFLINE_REFRESH_FIXTURE');
    window.__lateAdmin = loadUsers().catch(error => error.name);
    window.__lateTools = playLoadTools({ quiet: true }).catch(error => error.name);
    window.__lateToken = refreshTokens();
  }, OWN_A);
  await Promise.all([admin.seen, tools.seen, token.seen]);
  assert.equal(await page.evaluate(() => Boolean(_pollTimer)), true);
  await page.evaluate(() => {
    PS.messages = [{ role: 'assistant', content: 'PRIVATE_TRANSCRIPT_SENTINEL' }];
    PS.tools = [{ name: 'PRIVATE_CACHED_TOOL' }];
    chatBubble('assistant', 'PRIVATE_TRANSCRIPT_SENTINEL');
    document.getElementById('direct-out').textContent = 'PRIVATE_DIRECT_SENTINEL';
    document.getElementById('direct-out').hidden = false;
    showGate();
  });
  admin.release(); tools.release(); token.release();
  await page.evaluate(() => Promise.allSettled([window.__lateAdmin, window.__lateTools, window.__lateToken]));
  const state = await page.evaluate(() => ({
    me: S.me, users: S.users, repos: S.repos,
    messages: PS.messages, tools: PS.tools, toolsFor: PS.toolsFor,
    directBusy: PS.directBusy, busy: PS.busy,
    chat: document.getElementById('chat-log').textContent,
    direct: document.getElementById('direct-out').textContent,
    toolUI: document.getElementById('play-tools').textContent,
    tokens: ['gfy-access', 'gfy-id', 'gfy-exp', 'gfy-refresh'].map(tget),
    resets: window.__uxGraphResets,
    buildPoll: _pollTimer,
    appHidden: document.getElementById('app').hidden, gateHidden: document.getElementById('gate').hidden,
  }));
  assert.equal(state.me, null);
  for (const key of ['users', 'repos', 'messages', 'tools']) assert.deepEqual(state[key], [], key);
  for (const key of ['toolsFor', 'chat', 'direct', 'toolUI']) assert.equal(state[key], '', key);
  assert.deepEqual(state.tokens, [null, null, null, null]);
  assert.equal(state.buildPoll, null);
  assert.equal(state.busy, false); assert.equal(state.directBusy, false);
  assert.ok(state.resets >= 1, 'Ordinary private GraphExplorer must be reset');
  assert.equal(state.appHidden, true); assert.equal(state.gateHidden, false);
  assert.equal(await page.evaluate(() => window.__lateToken), false);
}, { admin: true });

testCase('lazy direct-tool loading preserves exact input JSON, chosen tool and server', async h => {
  const { page } = h;
  await goto(page, 'play');
  await page.waitForFunction(() => PS.toolsFor === 'all' && PS.tools.length === 2);
  await choose(page, 'direct-tool', 'query_graph');
  await choose(page, 'play-server', OWN_A);
  const argsText = ' {\n  "query": "원문·값—\\\"인용\\\"",\n  "filters": ["a", {"path": "folder/quote\\\"name.md"}],\n  "limit": 7\n} ';
  await page.locator('#direct-args').fill(argsText);
  const held = h.hold(`mcp:tools/list:${OWN_A}`);
  const called = observeResponse(page, response => {
    if (response.url() !== `${origin}/pgstream`) return false;
    return JSON.parse(response.request().postData()).payload?.method === 'tools/call';
  });
  await page.locator('#btn-direct-run').click();
  await held.seen;
  assert.equal(await page.locator('#play-server').isDisabled(), true);
  assert.equal(await page.locator('#btn-direct-run').isDisabled(), true);
  assert.equal(await page.locator('#direct-tool').isDisabled(), true);
  assert.equal(await page.locator('#direct-args').evaluate(input => input.readOnly), true);
  assert.equal(await page.locator('#btn-chat-send').isDisabled(), true);
  held.release(); await (await called).finished();
  await page.waitForFunction(() => !PS.directBusy);
  const calls = h.data.requests.filter(r => r.path === '/pgstream' && r.body.payload?.method === 'tools/call');
  assert.equal(calls.length, 1);
  assert.equal(calls[0].body.server_id, OWN_A);
  assert.equal(calls[0].body.payload.params.name, 'query_graph');
  assert.deepEqual(calls[0].body.payload.params.arguments, JSON.parse(argsText));
  assert.equal(await page.locator('#direct-args').inputValue(), argsText, 'Lazy loading must not replace the entered JSON with a skeleton');
  assert.equal(await page.locator('#play-server').evaluate(select => select.value), OWN_A);
  assert.equal(await page.locator('#direct-out').textContent(), `DIRECT_RESULT:${OWN_A}`);
  assert.equal(await page.locator('#play-server').isEnabled(), true);
  assert.equal(await page.locator('#btn-direct-run').isEnabled(), true);
  assert.equal(await page.locator('#direct-tool').isEnabled(), true);
  assert.equal(await page.locator('#direct-args').evaluate(input => input.readOnly), false);
  h.io.toolNames.set(OWN_B, ['search_code']);
  await choose(page, 'play-server', OWN_B);
  await page.locator('#btn-direct-run').click();
  await page.waitForFunction(() => !PS.directBusy && document.getElementById('direct-out').textContent.includes(t('ux.toolChanged')));
  assert.equal(h.data.requests.filter(r => r.path === '/pgstream' && r.body.payload?.method === 'tools/call').length, 1,
    'An unavailable selected tool must not silently execute another tool');
  assert.equal(await page.locator('#btn-direct-run').isEnabled(), true);
  assert.equal(await page.locator('#play-server').isEnabled(), true);
});

async function main() {
  fs.mkdirSync(OUT, { recursive: true });
  await prepareAssets();
  const assets = assetHashes();
  const browser = await chromium.launch({ channel: 'chrome', headless: true });
  const browserVersion = browser.version();
  try {
    const filters = process.argv.slice(2).map(value => value.toLowerCase());
    const selected = cases.filter(item => !filters.length || filters.some(filter => item.name.toLowerCase().includes(filter)));
    assert.ok(selected.length, 'No matching UX cases');
    for (const item of selected) {
      const entry = { name: item.name, admin: !!item.admin, status: 'PASS' }, start = Date.now();
      let h;
      let deadline;
      try {
        h = await uxHarness(browser, item.admin);
        await Promise.race([
          item.run(h),
          new Promise((_, reject) => { deadline = setTimeout(() => reject(new Error('UX case exceeded 60 seconds')), 60000); }),
        ]);
        h.assertClean();
        console.log(`PASS ${item.name}`);
      } catch (error) {
        entry.status = 'FAIL'; entry.error = error.stack || String(error);
        console.error(`FAIL ${item.name}\n${entry.error}`);
        if (h) {
          const modalOpen = await h.page.locator('dialog[open]').count().catch(() => 0);
          await h.page.screenshot({ path: path.join(OUT, `failure-${results.length + 1}.png`), fullPage: !modalOpen }).catch(() => {});
          fs.writeFileSync(path.join(OUT, `failure-${results.length + 1}.html`), await h.page.content().catch(() => ''));
        }
      } finally {
        clearTimeout(deadline);
        if (h) { entry.diagnostics = h.diagnostics(); entry.dialogs = h.io.dialogs; await h.context.close(); }
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
      passed: results.filter(r => r.status === 'PASS').length, total: results.length, tests: results,
      scope: 'Real console HTML/CSS/handlers, synthetic admin/member sessions and fake API responses. GraphExplorer remains a navigation stub.',
    };
    const suffix = process.argv[2] ? '-filtered' : '';
    fs.writeFileSync(path.join(OUT, `results${suffix}.json`), JSON.stringify(report, null, 2) + '\n');
    fs.writeFileSync(path.join(OUT, `summary${suffix}.md`), [
      `# Console UX: ${report.passed}/${report.total} passed`, '', report.scope, '',
      ...results.map(r => `- ${r.status}: ${r.name}`), '',
      ...results.filter(r => r.error).map(r => `## ${r.name}\n\n\`\`\`text\n${r.error}\n\`\`\`\n`),
    ].join('\n'));
    console.log(`${report.passed}/${report.total} passed. Artifacts: ${OUT}`);
    if (report.status === 'FAIL' || assets.some(a => a.changedDuringRun)) process.exitCode = 1;
  }
}

if (require.main === module) main().catch(error => { console.error(error); process.exitCode = 1; });
