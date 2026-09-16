#!/usr/bin/env node
'use strict';

/**
 * Five bounded group-modal cases. Run only after shared panel integration:
 * GRAPHIFY_PLAYWRIGHT_MODULE=/absolute/path/to/existing/playwright \
 *   node tests/test_console_group_modals.cjs [case-name-substring ...]
 *
 * --list does not launch Chrome. No installs, AWS, real users or invitations.
 * Reuses managementHarness through Module._compile without running its suite.
 * JSON: GRAPHIFY_GROUP_MODAL_OUT or /tmp/graphify-group-ui-navigation/modals.
 * Held responses deliberately ignore transport cancellation to test UI guards.
 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const NodeModule = require('node:module');
const { chromium } = require(process.env.GRAPHIFY_PLAYWRIGHT_MODULE || 'playwright');
const ROOT = path.resolve(__dirname, '..');
const OUT = path.resolve(process.env.GRAPHIFY_GROUP_MODAL_OUT || '/tmp/graphify-group-ui-navigation/modals');
const filename = path.join(__dirname, 'test_console_group_management.cjs');
const managementModule = new NodeModule(filename);
managementModule.filename = filename;
managementModule.paths = NodeModule._nodeModulePaths(path.dirname(filename));
managementModule._compile(fs.readFileSync(filename, 'utf8') + `
module.exports = { managementHarness, openManagement, listRow, nextFrames };
`, filename);
const { managementHarness, openManagement, listRow, nextFrames } = managementModule.exports;
const { prepareAssets, assetHashes, group, ids, origin, observeResponse } = require('./test_console_group_navigation.cjs');
const OWNER = ids.READY, OTHER = ids.DRAFT, EDITOR = ids.PARTIAL, VIEWER = ids.STALE;
const CREATED = 'grp_ffffffffffffffffffffffffffffffff';
const INVITEE = 'already-authorized@example.invalid';
const PRIVATE_DRAFT = 'PRIVATE_MODAL_DRAFT <img src=x onerror="window.__groupModalInjected=1"> 고객·원문—보존';
const OPERATION_MS = 30000, CASE_MS = 60000;
const cases = [];
const testCase = (name, run) => cases.push({ name, run });
const panel = (page, id) => page.locator(`dialog.console-panel-dialog[data-panel-id="${id}"]`);
const closeButton = (page, id) => panel(page, id).locator('.console-panel__header [data-panel-close]').first();
const changes = h => h.data.requests.filter(r => ['POST', 'DELETE'].includes(r.method));
const posts = (h, pathname) => changes(h).filter(r => r.method === 'POST' && r.path === pathname);
const memberPath = id => `/groups/${id}/members`;
const guestEmail = id => `guest-${id.slice(4, 8)}@example.invalid`;
const grantsSnapshot = h => [...h.modal.sourceGrants].map(([email, sources]) => [email, [...sources].sort()]);
const digest = bytes => crypto.createHash('sha256').update(bytes).digest('hex');

function bounded(promise, timeout, label) {
  let timer;
  const result = Promise.race([promise, new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error(`${label} exceeded ${timeout}ms`)), timeout);
  })]).finally(() => clearTimeout(timer));
  result.catch(() => {});
  return result;
}

async function opened(page, id) {
  const dialog = panel(page, id);
  await dialog.waitFor({ state: 'visible' });
  const facts = await dialog.evaluate((node, id) => {
    const content = document.getElementById(id);
    const title = document.getElementById(node.getAttribute('aria-labelledby'));
    return {
      modal: node instanceof HTMLDialogElement && node.open && node.matches(':modal'),
      body: !!content && !content.hidden && !!node.querySelector('.console-panel__body')?.contains(content),
      headerTitle: !!title?.textContent.trim() && !!node.querySelector('.console-panel__header')?.contains(title),
      unique: document.querySelectorAll(`[id="${id}"]`).length === 1,
      outsideDetail: !document.getElementById('sg-detail')?.contains(content),
    };
  }, id);
  assert.deepEqual(facts, { modal: true, body: true, headerTitle: true, unique: true, outsideDetail: true },
    `${id}: unique content and title belong to the shared modal, outside the read-only detail`);
  return dialog;
}

async function closed(page, id) {
  await panel(page, id).waitFor({ state: 'hidden' });
  await page.waitForFunction(id => !document.getElementById(id) || document.getElementById(id).hidden, id);
}

async function labelledButton(page, within, key) {
  return within.getByRole('button', { name: await page.evaluate(key => t(key), key), exact: true });
}

async function choose(page, id, value) {
  const combo = page.locator(`#${id}-combo`);
  if (await combo.count()) {
    await combo.click();
    await page.locator(`#${id}-list [data-value="${value}"]`).click();
  } else await page.locator(`#${id}`).selectOption(value);
  await nextFrames(page);
}

async function openEditor(h, id) {
  await (await labelledButton(h.page, listRow(h.page, id), 'sg.edit')).click();
  return opened(h.page, 'sg-editor');
}

async function openMembers(h, id, waitForMembers = true) {
  await (await labelledButton(h.page, listRow(h.page, id), 'sg.manageMembers')).click();
  const dialog = await opened(h.page, 'sg-members-section');
  if (waitForMembers) await dialog.locator('#sg-member-email').waitFor({ state: 'visible' });
  return dialog;
}

async function sameNode(locator, original, message) {
  assert.equal(await locator.evaluate((node, original) => node === original, original), true, message);
}

async function busyVeto(h, id, currentTab, destination) {
  await closeButton(h.page, id).click();
  await opened(h.page, id);
  await h.page.keyboard.press('Escape');
  await opened(h.page, id);
  assert.equal(await h.page.evaluate(() => ConsoleDialogs.closePanels('navigation')), false);
  await h.page.evaluate(tab => switchTab(tab), destination);
  assert.equal(await h.page.locator(`#page-${currentTab}`).isVisible(), true, 'Busy navigation keeps the current page');
  assert.equal(await h.page.locator(`#page-${destination}`).isVisible(), false);
  await opened(h.page, id);
}

async function ignoreCancellation(page, pathname) {
  await page.evaluate(url => {
    const fetch = window.fetch;
    window.fetch = (input, options) => String(input) === url
      ? fetch(input, { ...options, signal: undefined }) : fetch(input, options);
  }, `${origin}/api${pathname}`);
}

function observe(page, method, pathname) {
  return observeResponse(page, response => response.url() === `${origin}/api${pathname}`
    && response.request().method() === method);
}

async function delivered(page, response) {
  assert.equal(await (await response).finished(), null, 'Held fake HTTP response must finish');
  await nextFrames(page);
}

async function settled(h) {
  // Wait for the save's chained metadata/list reads before starting a new
  // permission change or confirmation. No fixed sleep or network-idle guess.
  await bounded((async () => {
    for (;;) {
      await nextFrames(h.page);
      const pending = [...h.modal.pending];
      if (!pending.length) {
        await nextFrames(h.page);
        if (!h.modal.pending.size) return;
      } else {
        await Promise.all(pending.map(async request => {
          const response = await request.response();
          if (response) await response.finished();
        }));
      }
    }
  })(), OPERATION_MS, 'Chained fake API reads');
}

// Only the create, group-members and common-description endpoints extend the
// existing harness. All other requests retain its strict fake-HTTP routing.
async function installGroupApi(h) {
  h.modal = { holds: [], failures: [], releases: [], members: new Map(), creates: new Map(),
    sourceGrants: new Map(), pending: new Set(), routeErrors: [], nativeDialogs: [], closing: false };
  h.page.on('request', request => {
    if (request.url().startsWith(`${origin}/api/`)) h.modal.pending.add(request);
  });
  h.page.on('requestfinished', request => h.modal.pending.delete(request));
  h.page.on('requestfailed', request => h.modal.pending.delete(request));
  for (const id of [OWNER, OTHER]) {
    h.modal.members.set(id, [
      { sub: `${id}-owner`, email: 'offline-navigation@example.invalid', role: 'owner', you: true },
      { sub: `${id}-guest`, email: guestEmail(id), role: 'viewer', you: false },
    ]);
    h.modal.sourceGrants.set(guestEmail(id), new Set(h.current(id).sources.map(s => s.source_id)));
  }
  h.modal.sourceGrants.set(INVITEE, new Set(h.current(OWNER).sources.map(s => s.source_id)));
  h.failModal = (method, pathname, status, afterCommit = false) =>
    h.modal.failures.push({ method, pathname, status, afterCommit });
  h.holdModal = (method, pathname) => {
    let started, release;
    const seen = new Promise(resolve => { started = resolve; });
    const gate = new Promise(resolve => { release = resolve; });
    h.modal.holds.push({ method, pathname, started, gate });
    h.modal.releases.push(release);
    return { seen: bounded(seen, OPERATION_MS, `${method} ${pathname} start`), release };
  };
  const ok = body => ({ status: 200, body });
  const failure = (status, error) => ({ status, body: { error } });
  function responseFor(method, pathname, body) {
    if (pathname === '/groups') {
      if (!body?.idempotency_key) return failure(400, 'An idempotency key is required');
      const { idempotency_key: key, ...config } = body;
      const fingerprint = JSON.stringify(config), previous = h.modal.creates.get(key);
      if (previous) return previous.fingerprint === fingerprint
        ? ok({ group: h.current(previous.id) }) : failure(409, 'Key payload changed');
      if (h.current(CREATED)) return failure(409, 'A second create intent would duplicate this fixture group');
      const created = group(CREATED, { ...config, role: 'owner', revision: 1, status: 'DRAFT',
        data_ready: false, stale: true, active_revision: null, active_version: null, active_source_versions: {} });
      h.modal.creates.set(key, { id: CREATED, fingerprint });
      h.data.groups.push(created);
      return ok({ group: created });
    }
    const membership = pathname.match(/^\/groups\/([^/]+)\/members(?:\/([^/]+))?$/);
    if (membership) {
      const [, id, target] = membership, g = h.current(id);
      if (!g || g.role !== 'owner' || g.access_recovery) return failure(403, 'Only the accessible group owner can manage members');
      const members = h.modal.members.get(id) || [];
      if (method === 'GET') return ok({ group_id: id, revision: g.revision, members });
      if (body?.expected_revision !== g.revision) return failure(409, 'Membership revision changed');
      let member;
      if (method === 'POST') {
        if (!['viewer', 'editor'].includes(body.role)) return failure(400, 'Invalid member role');
        if (!g.sources.every(s => h.modal.sourceGrants.get(body.email)?.has(s.source_id))) {
          return failure(403, 'The invitee must already have access to every source');
        }
        member = { sub: `${id}-invited`, email: body.email, role: body.role, you: false };
        h.modal.members.set(id, [...members.filter(m => m.email !== body.email), member]);
      } else {
        member = members.find(m => m.sub === decodeURIComponent(target || ''));
        if (!member) return failure(404, 'Member not found');
        if (member.role === 'owner' || member.you) return failure(403, 'The owner cannot be removed');
        h.modal.members.set(id, members.filter(m => m !== member));
      }
      Object.assign(g, { revision: g.revision + 1, status: 'STALE', stale: true, data_ready: false });
      return ok({ group_id: id, revision: g.revision, member });
    }
    const repoId = decodeURIComponent(pathname.match(/^\/repos\/([^/]+)\/description$/)[1]);
    const repo = h.data.repos.find(r => r.repo_id === repoId);
    if (!repo?.manageable) return failure(403, 'Source description access revoked');
    repo.description = body.description;
    return ok({ repo_id: repoId, description: body.description });
  }
  await h.context.route(`${origin}/api/**`, async route => {
    const request = route.request(), method = request.method();
    const pathname = new URL(request.url()).pathname.slice(4);
    const supported = (pathname === '/groups' && method === 'POST')
      || (/^\/groups\/[^/]+\/members(?:\/[^/]+)?$/.test(pathname) && ['GET', 'POST', 'DELETE'].includes(method))
      || (/^\/repos\/[^/]+\/description$/.test(pathname) && method === 'POST');
    if (!supported) return route.fallback();
    try {
      const body = request.postData() ? JSON.parse(request.postData()) : null;
      h.data.requests.push({ method, path: pathname, body });
      const failureIndex = h.modal.failures.findIndex(f => f.method === method && f.pathname === pathname);
      const injected = failureIndex < 0 ? null : h.modal.failures.splice(failureIndex, 1)[0];
      let result = injected && !injected.afterCommit
        ? failure(injected.status, 'Intentional offline modal failure') : responseFor(method, pathname, body);
      if (injected?.afterCommit && result.status < 400) result = failure(injected.status, 'Committed response unavailable');
      // Snapshot before waiting: later permission or metadata changes must not
      // rewrite the old response whose cancellation guards are being tested.
      result = structuredClone(result);
      const holdIndex = h.modal.holds.findIndex(item => item.method === method && item.pathname === pathname);
      if (holdIndex >= 0) {
        const hold = h.modal.holds.splice(holdIndex, 1)[0];
        hold.started(); await hold.gate;
      }
      if (result.status >= 400) h.diagnostics().expectedHttpErrors.push({ url: request.url(), status: result.status });
      await route.fulfill({ status: result.status, contentType: 'application/json', body: JSON.stringify(result.body) });
    } catch (error) {
      if (!h.modal.closing) h.modal.routeErrors.push(error.stack || String(error));
      await route.abort().catch(() => {});
    }
  });
}

testCase('create and edit panels discard Cancel/Escape drafts but preserve live language changes', async h => {
  const { page } = h, original = structuredClone(h.current(OWNER));
  await openManagement(page);
  await listRow(page, OWNER).locator('.sg-list-open').click();
  await page.locator('#sg-graph .sg-svg').waitFor({ state: 'visible' });
  await page.locator('#sg-root .sg-intro [data-i18n="sg.new"]').click();
  const create = await opened(page, 'sg-editor');
  assert.equal(await page.locator('#sg-meta').isVisible(), true);
  assert.equal(await page.locator('#sg-graph .sg-svg').isVisible(), true, 'The read-only graph stays on-page');
  await create.locator('#sg-name').fill(PRIVATE_DRAFT);
  await create.locator('[data-i18n="sg.cancel"]').click();
  await closed(page, 'sg-editor');
  await page.locator('#sg-root .sg-intro [data-i18n="sg.new"]').click();
  await opened(page, 'sg-editor');
  assert.equal(await page.locator('#sg-name').inputValue(), '', 'Cancelled creation is not restored');
  await page.keyboard.press('Escape');
  await closed(page, 'sg-editor');

  const edit = await openEditor(h, OWNER), form = await edit.locator('#sg-form').elementHandle();
  await edit.locator('#sg-name').fill('Language-preserved group draft');
  await edit.locator('#sg-description').fill(PRIVATE_DRAFT);
  await choose(page, 'sg-role-0', 'qa');
  await edit.locator('#sg-source-desc-0').fill('Role description retained across language changes');
  await edit.locator('#sg-source-search').fill(ids.NORMAL);
  await page.evaluate(() => setLang('en'));
  await opened(page, 'sg-editor');
  await sameNode(edit.locator('#sg-form'), form, 'Language changes must not replace the mounted editor form');
  assert.equal(await edit.locator('#sg-name').inputValue(), 'Language-preserved group draft');
  assert.equal(await edit.locator('#sg-description').inputValue(), PRIVATE_DRAFT);
  assert.equal(await edit.locator('#sg-role-0').inputValue(), 'qa');
  assert.equal(await edit.locator('#sg-source-desc-0').inputValue(), 'Role description retained across language changes');
  assert.equal(await edit.locator('#sg-source-search').inputValue(), ids.NORMAL);
  assert.equal(await edit.locator('.console-panel__header h2').textContent(), 'Edit group');
  assert.equal(await page.locator('#sg-detail #sg-form').count(), 0);
  // Verify the current browser's search-specific Escape behavior separately
  // from general modal cancellation, which is tested from a normal field.
  await page.keyboard.press('Escape');
  assert.equal(await edit.evaluate(node => node.open), true, 'Search Escape preserves the edit dialog');
  assert.equal(await edit.locator('#sg-source-search').inputValue(), '', 'Search Escape clears its query');
  await edit.locator('#sg-name').focus();
  await page.keyboard.press('Escape');
  await closed(page, 'sg-editor');
  await openEditor(h, OWNER);
  assert.equal(await page.locator('#sg-name').inputValue(), original.name, 'Escape discards the editor draft');
  assert.equal(await page.locator('#sg-description').inputValue(), original.description);
  await panel(page, 'sg-editor').locator('[data-i18n="sg.cancel"]').click();
  await closed(page, 'sg-editor');
  assert.deepEqual(changes(h), []);
  assert.deepEqual(h.current(OWNER), original);
});

testCase('create save blocks close and navigation, retries one idempotent intent, then edits with fresh CAS', async h => {
  const { page } = h, repos = structuredClone(h.data.repos);
  await openManagement(page);
  await page.locator('#sg-root .sg-intro [data-i18n="sg.new"]').click();
  const dialog = await opened(page, 'sg-editor');
  await dialog.locator('#sg-name').fill('Create retry keeps the intent');
  await dialog.locator('#sg-description').fill('Modal create description');
  await dialog.locator(`[data-sg-pick="${ids.NORMAL}"]`).check();
  await choose(page, 'sg-role-0', 'qa');
  await dialog.locator('#sg-source-desc-0').fill('Group-specific source description');
  h.failModal('POST', '/groups', 503, true);
  const hold = h.holdModal('POST', '/groups'), response = observe(page, 'POST', '/groups');
  await dialog.locator('#sg-save').click();
  await hold.seen;
  await busyVeto(h, 'sg-editor', 'groups', 'repos');
  await page.evaluate(() => setLang('en'));
  assert.equal(await dialog.locator('#sg-name').isDisabled(), true);
  assert.equal(await dialog.locator('#sg-save').isDisabled(), true);
  assert.equal(await dialog.locator('#sg-name').inputValue(), 'Create retry keeps the intent');
  hold.release(); await delivered(page, response);
  await dialog.locator('#sg-editor-message [role="alert"]').waitFor({ state: 'visible' });
  await page.waitForFunction(() => !document.getElementById('sg-save').disabled);
  assert.equal(await dialog.locator('#sg-name').inputValue(), 'Create retry keeps the intent');
  const first = structuredClone(posts(h, '/groups')[0].body);
  assert.match(first.idempotency_key, /^[0-9a-f-]{36}$/i);
  assert.equal(Object.hasOwn(first, 'expected_revision'), false);
  await dialog.locator('#sg-save').click();
  await closed(page, 'sg-editor');
  await page.waitForFunction(id => document.querySelector('#sg-meta > .sg-id')?.textContent === id, CREATED);
  assert.equal(posts(h, '/groups').length, 2);
  assert.deepEqual(posts(h, '/groups')[1].body, first, 'Retry must reuse the complete create intent');
  assert.equal(h.data.groups.filter(g => g.group_id === CREATED).length, 1);
  await settled(h);

  // The subsequent edit must use a fresh GET revision, not the create response.
  h.current(CREATED).revision = 4;
  await openEditor(h, CREATED);
  await page.locator('#sg-name').fill('Saved modal edit');
  await page.evaluate(() => setLang('ko'));
  await page.locator('#sg-save').click();
  await closed(page, 'sg-editor');
  await page.waitForFunction(() => document.querySelector('#sg-meta h2')?.textContent === 'Saved modal edit');
  const edit = posts(h, `/groups/${CREATED}`);
  assert.equal(edit.length, 1);
  assert.equal(edit[0].body.expected_revision, 4);
  assert.equal(Object.hasOwn(edit[0].body, 'idempotency_key'), false);
  assert.deepEqual(edit[0].body.sources, [{ source_id: ids.NORMAL, role: 'qa', description: 'Group-specific source description' }]);
  assert.equal(h.current(CREATED).revision, 5);
  assert.deepEqual(h.data.repos, repos, 'Group writes never mutate original sources');
});

testCase('members require an explicit owner action, retain CAS and nested confirmations, and clear on permission loss', async h => {
  const { page } = h, repos = structuredClone(h.data.repos), grants = grantsSnapshot(h);
  await openManagement(page);
  await listRow(page, OWNER).locator('.sg-list-open').click();
  await page.locator('#sg-meta').waitFor({ state: 'visible' });
  assert.equal(await page.locator('#sg-member-email').count(), 0, 'No inline invitation form');
  assert.equal(h.data.requests.filter(r => r.path.includes('/members')).length, 0, 'Detail opening does not list members');
  for (const id of [EDITOR, VIEWER]) {
    assert.equal(await listRow(page, id).locator('[data-i18n="sg.manageMembers"]').count(), 0);
  }
  await page.evaluate(id => SourceGroups.manage(id, 'members'), EDITOR);
  assert.equal(await panel(page, 'sg-members-section').count(), 0, 'A stale/programmatic non-owner action cannot open members');
  assert.equal(h.data.requests.filter(r => r.path.includes('/members')).length, 0);
  const dialog = await openMembers(h, OWNER), original = await dialog.elementHandle();
  const ownerRow = dialog.locator('.sg-member').filter({ hasText: 'offline-navigation@example.invalid' });
  assert.equal(await ownerRow.locator('button').isDisabled(), true, 'The group owner cannot be removed');
  assert.ok((await dialog.textContent()).includes(await page.evaluate(() => t('sg.acl'))));
  await dialog.locator('#sg-member-email').fill(INVITEE);
  await choose(page, 'sg-member-role', 'editor');
  const form = await dialog.locator('.sg-member-form').elementHandle();
  await page.evaluate(() => setLang('en'));
  await sameNode(dialog.locator('.sg-member-form'), form, 'Language changes retain the member draft');
  assert.equal(await dialog.locator('#sg-member-email').inputValue(), INVITEE);
  assert.equal(await dialog.locator('#sg-member-role').inputValue(), 'editor');
  const revision = h.current(OWNER).revision;
  h.current(OWNER).revision++; // An independent server-side change wins the CAS race.
  await dialog.locator('.sg-member-form button[type="submit"]').click();
  await dialog.locator('#sg-members-message [role="alert"]').waitFor({ state: 'visible' });
  assert.equal(await dialog.locator('#sg-member-email').inputValue(), INVITEE);
  await dialog.locator('#sg-members-message [data-i18n="sg.refresh"]').click();
  await dialog.locator('#sg-member-email').waitFor({ state: 'visible' });
  await dialog.locator('#sg-member-email').fill(INVITEE);
  await choose(page, 'sg-member-role', 'editor');
  await dialog.locator('.sg-member-form button[type="submit"]').click();
  await dialog.locator('.sg-member').filter({ hasText: INVITEE }).waitFor({ state: 'visible' });
  await settled(h);
  await sameNode(dialog, original, 'Saving members keeps the same parent modal');
  assert.deepEqual(posts(h, memberPath(OWNER)).map(r => r.body.expected_revision), [revision, revision + 1]);
  assert.equal(h.modal.members.get(OWNER).find(m => m.email === INVITEE).role, 'editor');

  const guest = dialog.locator('.sg-member').filter({ hasText: guestEmail(OWNER) });
  const confirmation = page.locator('dialog.console-dialog:not(.console-panel-dialog)[open]');
  for (const accept of [false, true]) {
    await guest.locator('button').click();
    await confirmation.waitFor({ state: 'visible' });
    assert.equal(await dialog.evaluate(node => node.open), true, 'Confirmation overlays the member panel');
    assert.ok((await confirmation.textContent()).includes(guestEmail(OWNER)));
    await confirmation.locator(accept ? '.console-dialog__submit' : '.console-dialog__cancel').click();
    await confirmation.waitFor({ state: 'hidden' });
    if (!accept) assert.equal(changes(h).filter(r => r.method === 'DELETE').length, 0);
  }
  await guest.waitFor({ state: 'hidden' });
  await settled(h);
  await sameNode(dialog, original, 'Removing a member keeps the parent modal');
  const removal = changes(h).filter(r => r.method === 'DELETE');
  assert.deepEqual(removal.map(r => [r.path, r.body]), [[
    `${memberPath(OWNER)}/${OWNER}-guest`, { expected_revision: revision + 2 },
  ]]);
  assert.deepEqual(h.data.repos, repos);
  assert.deepEqual(grantsSnapshot(h), grants, 'Inviting or removing a group member never grants/revokes source access');
  assert.equal(changes(h).some(r => r.path.startsWith('/repos/')), false);

  await dialog.locator('#sg-member-email').fill('private-member-draft@example.invalid');
  h.current(OWNER).role = 'editor';
  await page.evaluate(() => refreshAll());
  await closed(page, 'sg-members-section');
  assert.equal(await page.locator('#sg-member-email').count(), 0);
  assert.equal(await page.locator('#sg-meta [data-i18n="sg.manageMembers"]').count(), 0);
  assert.equal(await page.locator('#sg-meta [data-i18n="sg.edit"]').isVisible(), true, 'Editor access still permits group editing');
  assert.equal((await page.locator('body').textContent()).includes('private-member-draft@example.invalid'), false);
});

testCase('late member reads after close/navigation and an update after reset cannot restore another group', async h => {
  const { page } = h, pathname = memberPath(OWNER);
  await openManagement(page);
  await ignoreCancellation(page, pathname);
  for (const reason of ['close', 'navigation']) {
    const hold = h.holdModal('GET', pathname), response = observe(page, 'GET', pathname);
    await openMembers(h, OWNER, false);
    await hold.seen;
    if (reason === 'close') await closeButton(page, 'sg-members-section').click();
    else {
      await page.evaluate(() => switchTab('repos'));
      assert.equal(await page.locator('#page-repos').isVisible(), true);
    }
    await closed(page, 'sg-members-section');
    if (reason === 'navigation') await openManagement(page);
    const other = await openMembers(h, OTHER), original = await other.elementHandle();
    hold.release(); await delivered(page, response);
    await sameNode(other, original, `${reason}: a late read cannot replace the other group's modal`);
    assert.equal(await page.locator('#sg-meta > .sg-id').textContent(), OTHER);
    assert.ok((await other.locator('.sg-member-list').textContent()).includes(guestEmail(OTHER)));
    assert.equal((await other.locator('.sg-member-list').textContent()).includes(guestEmail(OWNER)), false);
    await closeButton(page, 'sg-members-section').click();
    await closed(page, 'sg-members-section');
  }
  const otherBefore = structuredClone(h.current(OTHER));
  const dialog = await openMembers(h, OWNER), revision = h.current(OWNER).revision;
  await dialog.locator('#sg-member-email').fill(INVITEE);
  const hold = h.holdModal('POST', pathname), response = observe(page, 'POST', pathname);
  await dialog.locator('.sg-member-form button[type="submit"]').click();
  await hold.seen;
  await busyVeto(h, 'sg-members-section', 'groups', 'repos');
  assert.equal(await page.evaluate(id => SourceGroups.open(id), OTHER), false, 'Busy group navigation is also vetoed');
  assert.equal(await page.locator('#sg-meta > .sg-id').textContent(), OWNER);
  await page.evaluate(() => SourceGroups.reset());
  await closed(page, 'sg-members-section');
  assert.equal(await page.locator('#sg-member-email').count(), 0);
  await page.evaluate(() => SourceGroups.onShow());
  const other = await openMembers(h, OTHER), original = await other.elementHandle();
  const ownerReads = h.data.requests.filter(r => r.method === 'GET' && r.path.startsWith(`/groups/${OWNER}`)).length;
  hold.release(); await delivered(page, response);
  await sameNode(other, original, 'A reset mutation must not replace or close the new group modal');
  assert.equal(await page.locator('#sg-meta > .sg-id').textContent(), OTHER);
  assert.ok((await other.locator('.sg-member-list').textContent()).includes(guestEmail(OTHER)));
  assert.equal((await other.textContent()).includes(INVITEE), false);
  assert.equal(h.data.requests.filter(r => r.method === 'GET' && r.path.startsWith(`/groups/${OWNER}`)).length, ownerReads,
    'The abandoned mutation must not start a detail/member refresh');
  assert.deepEqual(posts(h, pathname).map(r => r.body), [{ email: INVITEE, role: 'viewer', expected_revision: revision }]);
  assert.equal(h.modal.members.get(OWNER).some(m => m.email === INVITEE), true,
    'Reset invalidates UI work; it cannot undo a server mutation already committed');
  assert.deepEqual(h.current(OTHER), otherBefore);
  await closeButton(page, 'sg-members-section').click();
  await closed(page, 'sg-members-section');
});

testCase('common source description cancels drafts, vetoes busy close, saves literally and clears private edits on 403', async h => {
  const { page } = h, repo = h.data.repos.find(r => r.repo_id === ids.NORMAL);
  repo.manageable = true; repo.owned = true;
  await page.evaluate(() => refreshAll());
  const groupSources = structuredClone(h.current(OWNER).sources), original = repo.description;
  const otherRepo = structuredClone(h.data.repos.find(r => r.repo_id === ids.GROUP_ONLY));
  const pathname = `/repos/${encodeURIComponent(ids.NORMAL)}/description`;
  const sourceRow = () => page.locator('#repos-tbody tr').filter({ hasText: ids.NORMAL });
  const openDescription = async () => {
    await (await labelledButton(page, sourceRow(), 'sg.editDescription')).click();
    return opened(page, 'sg-description-editor');
  };
  let dialog = await openDescription();
  const translatedTextarea = await dialog.locator('#sg-common-description').elementHandle();
  await dialog.locator('#sg-common-description').fill(PRIVATE_DRAFT);
  await page.evaluate(() => setLang('en'));
  await sameNode(dialog.locator('#sg-common-description'), translatedTextarea, 'Language changes retain the source form');
  assert.equal(await dialog.locator('#sg-common-description').inputValue(), PRIVATE_DRAFT);
  await dialog.locator('[data-i18n="sg.cancel"]').click();
  await closed(page, 'sg-description-editor');
  dialog = await openDescription();
  assert.equal(await dialog.locator('#sg-common-description').inputValue(), original);
  await page.keyboard.press('Escape');
  await closed(page, 'sg-description-editor');
  assert.equal(posts(h, pathname).length, 0);

  const saveOpener = await (await labelledButton(page, sourceRow(), 'sg.editDescription')).elementHandle();
  dialog = await openDescription();
  const saved = 'Saved common description <img src=x onerror="window.__groupModalInjected=1"> 고객·원문—보존';
  await dialog.locator('#sg-common-description').fill(saved);
  const hold = h.holdModal('POST', pathname), response = observe(page, 'POST', pathname);
  await dialog.locator('button[type="submit"]').click();
  await hold.seen;
  await busyVeto(h, 'sg-description-editor', 'repos', 'groups');
  assert.equal(await dialog.locator('#sg-common-description').isDisabled(), true);
  assert.equal(await saveOpener.evaluate(node => node.isConnected), true,
    'The success-focus regression must start with a live opener, not one removed by a language re-render');
  hold.release(); await delivered(page, response);
  await closed(page, 'sg-description-editor');
  await page.waitForFunction(() => document.activeElement === document.getElementById('repos-search'));
  await page.evaluate(() => refreshAll());
  await settled(h);
  assert.equal(await page.locator('#repos-search').evaluate(node => node === document.activeElement), true,
    'Successful description save restores stable focus that survives row and metadata refreshes');
  assert.equal(await page.evaluate(id => S.repos.find(r => r.repo_id === id).description, ids.NORMAL), saved);
  assert.ok((await sourceRow().textContent()).includes(saved));
  assert.equal(await sourceRow().locator('img').count(), 0);
  assert.equal(await page.evaluate(() => window.__groupModalInjected || 0), 0);
  assert.deepEqual(h.current(OWNER).sources, groupSources, 'Common descriptions never overwrite group-specific descriptions');
  assert.deepEqual(h.data.repos.find(r => r.repo_id === ids.GROUP_ONLY), otherRepo);

  dialog = await openDescription();
  await dialog.locator('#sg-common-description').fill(PRIVATE_DRAFT);
  repo.manageable = false; // Keep the rendered client button stale; the fake API now denies it.
  const denied = observe(page, 'POST', pathname);
  await dialog.locator('button[type="submit"]').click();
  assert.equal((await denied).status(), 403);
  await delivered(page, denied);
  await closed(page, 'sg-description-editor');
  assert.equal(await page.locator('#sg-common-description').count(), 0, 'Denied private draft controls are disposed');
  assert.equal((await page.locator('body').textContent()).includes(PRIVATE_DRAFT), false);
  assert.equal(repo.description, saved, 'A denied description write cannot overwrite the source');
  assert.deepEqual(posts(h, pathname).map(r => r.body), [{ description: saved }, { description: PRIVATE_DRAFT }]);
  await page.evaluate(() => refreshAll());
  assert.equal(await (await labelledButton(page, sourceRow(), 'sg.editDescription')).count(), 0,
    'Refreshed source permissions remove the stale edit action');
});

async function runCase(browser, item, index) {
  const entry = { name: item.name, status: 'PASS' }, contexts = new Set(), started = Date.now();
  let h, closing = false;
  const scopedBrowser = {
    async newContext(options) {
      if (closing) throw new Error('Case already closed');
      const context = await browser.newContext(options);
      contexts.add(context);
      if (closing) { await context.close(); throw new Error('Case closed during fixture startup'); }
      return context;
    },
  };
  const work = (async () => {
    h = await managementHarness(scopedBrowser);
    if (closing) return;
    await installGroupApi(h);
    h.page.setDefaultTimeout(OPERATION_MS);
    h.page.on('dialog', dialog => {
      h.modal.nativeDialogs.push({ type: dialog.type(), message: dialog.message() });
      dialog.dismiss().catch(() => {});
    });
    await h.page.evaluate(() => {
      window.__groupModalUnhandled = [];
      addEventListener('unhandledrejection', event => {
        window.__groupModalUnhandled.push(event.reason?.stack || String(event.reason));
      });
    });
    await item.run(h);
    await settled(h);
    h.assertClean();
    assert.deepEqual(h.modal.routeErrors, [], 'Unexpected fake API errors');
    assert.deepEqual(h.modal.nativeDialogs, [], 'Group modals must not fall back to blocking native dialogs');
    assert.deepEqual(await h.page.evaluate(() => window.__groupModalUnhandled), [], 'Unhandled application promises');
  })();
  work.catch(() => {});
  try {
    await bounded(work, CASE_MS, item.name);
  } catch (error) {
    entry.status = 'FAIL'; entry.error = error.stack || String(error); closing = true;
    if (h?.modal) h.modal.closing = true;
    if (h && !h.page.isClosed()) {
      entry.screenshot = `failure-${index + 1}.png`;
      await h.page.screenshot({ path: path.join(OUT, entry.screenshot), fullPage: true, timeout: 3000 }).catch(() => {});
    }
  } finally {
    closing = true;
    if (h?.modal) {
      h.modal.closing = true;
      h.modal.releases.forEach(release => release());
      entry.routeErrors = h.modal.routeErrors;
      entry.nativeDialogs = h.modal.nativeDialogs;
    }
    if (h) entry.diagnostics = h.diagnostics();
    const cleanup = await Promise.allSettled([...contexts].map(async context => {
      try { await context.unrouteAll({ behavior: 'ignoreErrors' }); }
      finally { await context.close(); }
    }));
    entry.cleanupErrors = cleanup.filter(r => r.status === 'rejected').map(r => String(r.reason));
    if (entry.cleanupErrors.length) entry.status = 'FAIL';
    entry.duration_ms = Date.now() - started;
  }
  return entry;
}

async function main() {
  if (process.argv.includes('--list')) { console.log(cases.map(item => item.name).join('\n')); return; }
  const filters = process.argv.slice(2).map(value => value.toLowerCase());
  const selected = cases.filter(item => !filters.length || filters.some(filter => item.name.toLowerCase().includes(filter)));
  assert.ok(selected.length, 'No matching group-modal cases');
  fs.mkdirSync(OUT, { recursive: true });
  const report = {
    generated_at: new Date().toISOString(), status: 'FAIL', filters, tests: [], assets: [],
    test_sha256: digest(fs.readFileSync(__filename)), operation_timeout_ms: OPERATION_MS, case_timeout_ms: CASE_MS,
    scope: 'Five actual-console group-modal cases; isolated synthetic sessions and intercepted fake API. Existing GraphExplorer stub; no AWS.',
  };
  let browser;
  try {
    await prepareAssets();
    report.assets = assetHashes();
    browser = await chromium.launch({ channel: 'chrome', headless: true, timeout: OPERATION_MS });
    report.browser = browser.version();
    for (const [index, item] of selected.entries()) {
      const entry = await runCase(browser, item, index);
      report.tests.push(entry);
      console.log(`${entry.status} ${entry.name}`);
    }
  } catch (error) {
    report.error = error.stack || String(error);
  } finally {
    if (browser) await browser.close().catch(error => { report.error = error.stack || String(error); });
    for (const asset of report.assets) {
      try { asset.changedDuringRun = digest(fs.readFileSync(path.join(ROOT, asset.file))) !== asset.sha256; }
      catch (error) { asset.changedDuringRun = true; asset.error = String(error); }
    }
    report.passed = report.tests.filter(item => item.status === 'PASS').length;
    report.total = selected.length;
    report.status = !report.error && report.passed === report.total && report.assets.length > 0
      && report.assets.every(asset => !asset.changedDuringRun) ? 'PASS' : 'FAIL';
    const artifact = path.join(OUT, `results${filters.length ? '-filtered' : ''}.json`);
    fs.writeFileSync(artifact, JSON.stringify(report, null, 2) + '\n');
    console.log(JSON.stringify({ status: report.status, passed: report.passed, total: report.total, artifact }));
    if (report.status === 'FAIL') process.exitCode = 1;
  }
}

if (require.main === module) main().catch(error => { console.error(error); process.exitCode = 1; });
