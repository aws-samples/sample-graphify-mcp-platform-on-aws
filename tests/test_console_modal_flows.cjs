#!/usr/bin/env node
'use strict';

/**
 * Run AFTER console/index.html, CSS, groups, and dialogs integration is ready:
 * GRAPHIFY_PLAYWRIGHT_MODULE=/path/to/existing/playwright \
 *   node tests/test_console_modal_flows.cjs [case-name-substring ...]
 *
 * --list does not launch a browser. No installs, AWS, live invitations, or real
 * credentials. Reuses the actual-assets/fake-API UX harness via Module._compile,
 * as test_console_spacing.cjs does; it does not execute that suite's main().
 * Defaults: 30s operations, 60s per case INCLUDING harness startup.
 * Artifacts: GRAPHIFY_MODAL_OUT or /tmp/graphify-console-modals.
 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const Module = require('node:module');

const { chromium } = require(process.env.GRAPHIFY_PLAYWRIGHT_MODULE || 'playwright');
const ROOT = path.resolve(__dirname, '..');
const OUT = path.resolve(process.env.GRAPHIFY_MODAL_OUT || '/tmp/graphify-console-modals');
const filename = path.join(__dirname, 'test_console_ux.cjs');
const uxModule = new Module(filename);
uxModule.filename = filename;
uxModule.paths = Module._nodeModulePaths(path.dirname(filename));
uxModule._compile(fs.readFileSync(filename, 'utf8') + `
module.exports = { uxHarness, prepareAssets, goto, choose, translatedButton,
  sourceAction, ignoreCancellation, round, changeRequests, OWN_A, OWN_B };
`, filename);
const {
  uxHarness, prepareAssets, goto, choose, translatedButton, sourceAction,
  ignoreCancellation, round, changeRequests, OWN_A, OWN_B,
} = uxModule.exports;
const { assetHashes, origin, observeResponse } = require('./test_console_group_navigation.cjs');
const OPERATION_MS = 30000, CASE_MS = 60000;
const ISSUED_KEY = 'OFFLINE_ISSUED_VALUE_NOT_A_CREDENTIAL'; // Existing fake /keys response.
const PANEL_IDS = [
  'registration-panel', 'key-form', 'invite-form', 'llm-panel',
  'members-panel', 'files-panel', 'build-panel', 'connection-guide',
];
// Panel dialogs also carry console-dialog; exclude them when finding a child.
const CONFIRM = 'dialog.console-dialog:not(.console-panel-dialog)[open]';
const cases = [];
const testCase = (name, run, options = {}) => cases.push({ name, run, ...options });
const panel = (page, id) => page.locator(`dialog.console-panel-dialog[data-panel-id="${id}"]`);
const headerClose = (page, id) => panel(page, id)
  .locator('.console-panel__header [data-panel-close], .console-panel__header .console-panel__close').first();
const postBodies = (h, pathname) => changeRequests(h)
  .filter(request => request.method === 'POST' && request.path === pathname).map(request => request.body);

function bounded(promise, milliseconds, label) {
  let timer;
  const result = Promise.race([
    promise,
    new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error(`${label} exceeded ${milliseconds}ms`)), milliseconds);
    }),
  ]).finally(() => clearTimeout(timer));
  // Observers can reject while another UI action is still awaited.
  result.catch(() => {});
  return result;
}

function deferred(h) {
  let started, release;
  const seen = new Promise(resolve => { started = resolve; });
  const gate = new Promise(resolve => { release = resolve; });
  h.modal.releases.push(release);
  return { seen, release, wait: () => { started(); return gate; } };
}

function holdRead(h, pathname) {
  const held = h.hold(pathname);
  h.modal.releases.push(held.release);
  return held;
}

async function responseFinished(page, pathname, method, trigger) {
  const response = observeResponse(page, response =>
    response.url() === `${origin}/api${pathname}` && response.request().method() === method);
  await trigger();
  const error = await (await response).finished();
  assert.equal(error, null, `${method} ${pathname} must finish before assertions`);
  await round(page);
  await round(page);
}

// Extend only fixture endpoints missing a particular response/hold. Everything
// else falls back through uxHarness and its strict no-outbound-request handler.
async function fakeApi(h, method, pathname, respond) {
  await h.context.route(`${origin}/api${pathname}`, async route => {
    const request = route.request();
    if (request.method() !== method) return route.fallback();
    try {
      const body = request.postData() ? JSON.parse(request.postData()) : null;
      h.data.requests.push({ method, path: pathname, body });
      const result = await respond(body);
      const status = result.status || 200;
      if (status >= 400) h.diagnostics().expectedHttpErrors.push({ url: request.url(), status });
      await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(result.body) });
    } catch (error) {
      if (!h.modal.closing) h.modal.routeErrors.push(error.stack || String(error));
      await route.abort().catch(() => {});
    }
  });
}

async function opened(h, id) {
  const dialog = panel(h.page, id);
  await dialog.waitFor({ state: 'visible' });
  const facts = await dialog.evaluate((dialog, id) => {
    const content = document.getElementById(id);
    const titleIds = (dialog.getAttribute('aria-labelledby') || '').split(/\s+/).filter(Boolean);
    return {
      native: dialog instanceof HTMLDialogElement && dialog.open && dialog.matches(':modal'),
      original: content === window.__modalOriginals[id],
      movedIntoBody: !!dialog.querySelector('.console-panel__body')?.contains(content),
      contentHidden: content.hidden,
      labelled: !!dialog.getAttribute('aria-label') || titleIds.some(title =>
        document.getElementById(title)?.textContent.trim()),
      uniqueContent: document.querySelectorAll(`[id="${id}"]`).length === 1,
      focusedInside: dialog.contains(document.activeElement),
    };
  }, id);
  assert.deepEqual(facts, {
    native: true, original: true, movedIntoBody: true, contentHidden: false,
    labelled: true, uniqueContent: true, focusedInside: true,
  }, `${id}: native, labelled modal containing the original content`);
  assert.equal(await headerClose(h.page, id).isVisible(), true, `${id}: header close is available`);
  h.modal.panels.add(id);
  return dialog;
}

async function closed(page, id) {
  await panel(page, id).waitFor({ state: 'hidden' });
  await page.waitForFunction(id => document.getElementById(id).hidden, id);
  assert.equal(await panel(page, id).evaluate(dialog => dialog.open), false, `${id}: native close`);
}

async function closePanel(h, id) {
  await headerClose(h.page, id).click();
  await closed(h.page, id);
}

async function focused(page, selector) {
  await page.waitForFunction(selector => document.querySelector(selector) === document.activeElement, selector);
}

async function openForm(h, tab, id, buttonId, key) {
  await goto(h.page, tab);
  const button = h.page.locator(`#${buttonId}`);
  // Support the planned stable ID and the existing goTab(...) toolbar action.
  if (await button.count()) await button.click();
  else await (await translatedButton(h.page, h.page.locator(`#page-${tab} .page-actions`), key)).click();
  return opened(h, id);
}

async function inlineError(h, id) {
  const alert = panel(h.page, id).locator('[role="alert"]:visible').first();
  await alert.waitFor({ state: 'visible' });
  assert.ok((await alert.textContent()).trim().length > 0, `${id}: accessible inline error has text`);
  assert.equal(await panel(h.page, id).evaluate(dialog => dialog.open), true, `${id}: failure stays modal`);
  return alert;
}

async function busyCannotNavigate(h, id, tab) {
  assert.equal(await h.page.evaluate(() => ConsoleDialogs.closePanels('navigation')), false,
    'closePanels must report a busy form veto');
  await h.page.evaluate(tab => switchTab(tab), tab);
  assert.equal(await h.page.locator(`#page-${tab}`).isVisible(), false, 'Busy navigation keeps the current page');
  await h.page.keyboard.press('Escape');
  assert.equal(await panel(h.page, id).evaluate(dialog => dialog.open), true, 'Escape cannot hide a busy primary form');
}

testCase('clipboard fallback stays inside the active modal and never reports a failed copy as success', async h => {
  const { page } = h;
  await page.evaluate(() => {
    window.__modalCopies = [];
    window.__copyAllowed = true;
    Object.defineProperty(navigator, 'clipboard', { configurable: true,
      value: { writeText: async () => { throw new Error('Offline clipboard permission failure'); } } });
    document.execCommand = command => {
      const input = document.activeElement;
      window.__modalCopies.push({ command, value: input.value,
        modal: input.closest('dialog')?.dataset.panelId, open: input.closest('dialog')?.open });
      return window.__copyAllowed;
    };
  });
  await page.evaluate(id => openConnectionGuide(id), OWN_A);
  await opened(h, 'connection-guide');
  const expected = await page.locator('#connection-config').textContent();
  await page.locator('#connection-content').getByRole('button', { name: '설정 복사', exact: true }).click();
  await page.waitForFunction(() => window.__modalCopies.length === 1);
  assert.deepEqual(await page.evaluate(() => window.__modalCopies[0]), {
    command: 'copy', value: expected, modal: 'connection-guide', open: true,
  });
  await closePanel(h, 'connection-guide');
  await page.evaluate(id => { window.__copyAllowed = false; openConnectionGuide(id); }, OWN_A);
  const copy = page.locator('#connection-content').getByRole('button', { name: '설정 복사', exact: true });
  await copy.click();
  await inlineError(h, 'connection-guide');
  assert.equal(await copy.textContent(), '설정 복사');
  assert.equal(await page.locator('textarea[aria-hidden="true"]').count(), 0, 'Temporary copy fields are removed');
});

testCase('Escape dismisses an open combobox before it can dismiss the editing modal', async h => {
  const { page } = h;
  await page.evaluate(id => openLlmPanel(S.repos.find(repo => repo.repo_id === id)), OWN_A);
  await opened(h, 'llm-panel');
  const select = page.locator('#lp-enabled-combo');
  await select.click();
  await page.locator('#lp-enabled-list').waitFor({ state: 'visible' });
  await page.keyboard.press('Escape');
  assert.equal(await select.getAttribute('aria-expanded'), 'false');
  assert.equal(await panel(page, 'llm-panel').evaluate(node => node.open), true);
  await page.keyboard.press('Escape');
  await closed(page, 'llm-panel');
  assert.deepEqual(changeRequests(h), []);
});

testCase('delayed diagnostics clipboard fallback cannot run underneath a newer confirmation layer', async h => {
  const { page } = h;
  await fakeApi(h, 'GET', `/repos/${OWN_A}/build`, async () => ({
    body: { repo_id: OWN_A, source_status: 'READY', can_rebuild: true, last_error: '', hints: [],
      build: null, logs: { state: 'available', events: [], next_token: null, truncated: false } },
  }));
  await page.evaluate(() => {
    window.__fallbackCopies = 0;
    Object.defineProperty(navigator, 'clipboard', { configurable: true,
      value: { writeText: () => new Promise((_, reject) => { window.__rejectClipboard = reject; }) } });
    document.execCommand = () => { window.__fallbackCopies++; return true; };
  });
  await page.evaluate(id => openBuildPanel(id), OWN_A);
  await opened(h, 'build-panel');
  await page.locator('#bp-copy').waitFor({ state: 'visible' });
  await page.locator('#bp-copy').click();
  await page.waitForFunction(() => !!window.__rejectClipboard);
  await page.evaluate(() => { window.__nestedPrompt = ConsoleDialogs.prompt({ title: 'Next task', label: 'Value', value: '200' }); });
  await page.locator(CONFIRM).waitFor({ state: 'visible' });
  await page.evaluate(() => window.__rejectClipboard(new Error('Offline delayed denial')));
  await round(page); await round(page);
  assert.equal(await page.evaluate(() => window.__fallbackCopies), 0);
  assert.equal(await page.evaluate(() => BP.copyNote), '');
  assert.equal(await page.locator(CONFIRM).isVisible(), true);
  await page.locator(CONFIRM).locator('.console-dialog__cancel').click();
  await opened(h, 'build-panel');
});

testCase('manual file build vetoes close and retargeting until its own response settles', async h => {
  const { page } = h, held = deferred(h);
  await fakeApi(h, 'POST', `/repos/${OWN_A}/rebuild`, async () => {
    await held.wait();
    return { body: { build_id: 'offline-build-source-a' } };
  });
  await sourceAction(h, OWN_A, 'fp.files');
  await opened(h, 'files-panel');
  await page.waitForFunction(() => !FP.loading);
  await page.locator('#fp-build').click();
  await bounded(held.seen, OPERATION_MS, 'Manual build started');
  assert.equal(await page.locator('#fp-build').isDisabled(), true);
  assert.equal(await page.locator('#fp-refresh').isDisabled(), true);
  await busyCannotNavigate(h, 'files-panel', 'keys');
  await page.evaluate(id => openFilesPanel(id), OWN_B);
  assert.equal(await page.locator('#fp-repo').textContent(), OWN_A);
  held.release();
  await page.waitForFunction(() => !FP.busy);
  assert.ok((await panel(page, 'files-panel').textContent()).includes('offline-build-source-a'));
  assert.equal(postBodies(h, `/repos/${OWN_A}/rebuild`).length, 1);
  assert.equal(postBodies(h, `/repos/${OWN_B}/rebuild`).length, 0);
  await closePanel(h, 'files-panel');
  await sourceAction(h, OWN_B, 'fp.files');
  await opened(h, 'files-panel');
  assert.equal((await panel(page, 'files-panel').textContent()).includes('offline-build-source-a'), false);
});

testCase('list-only defaults keep registration key and invite fields hidden until explicit open', async h => {
  const { page } = h;
  for (const [tab, table, id, input] of [
    ['repos', 'repos-tbody', 'registration-panel', 'reg-url'],
    ['keys', 'keys-tbody', 'key-form', 'key-name'],
    ['admin', 'users-tbody', 'invite-form', 'inv-email'],
  ]) {
    await goto(page, tab);
    assert.equal(await page.locator(`#${table}`).isVisible(), true);
    assert.ok(await page.locator(`#${table} > tr`).count(), `${tab}: list is populated`);
    assert.equal(await page.locator(`#${id}`).evaluate(node => node.hidden), true);
    assert.equal(await page.locator(`#${input}`).isVisible(), false);
    assert.equal(await page.locator('dialog[open]').count(), 0);
  }
  await openForm(h, 'keys', 'key-form', 'btn-key-open', 'key.new');
  await closePanel(h, 'key-form');
  if (await page.locator('#btn-key-open').count()) await focused(page, '#btn-key-open');
  await openForm(h, 'admin', 'invite-form', 'btn-invite-open', 'adm.invite');
  await closePanel(h, 'invite-form');
  if (await page.locator('#btn-invite-open').count()) await focused(page, '#btn-invite-open');
  assert.deepEqual(changeRequests(h), []);
}, { admin: true });

testCase('registration type drafts survive close reopen Escape backdrop and background isolation', async h => {
  const { page } = h;
  await page.locator('#btn-register-open').click();
  const dialog = await opened(h, 'registration-panel');
  const draft = {
    'reg-description': '고객·설명—원문 보존\nSecond line <img src=x>',
    'reg-url': 'https://example.invalid/draft.git',
    'reg-ref': 'feature/modal-draft',
    'reg-docs-url': 'https://example.invalid/docs/draft/',
    'reg-files-name': 'modal-draft',
  };
  await page.locator('#reg-description').fill(draft['reg-description']);
  for (const [type, inputs] of [['git', ['reg-url', 'reg-ref']], ['url', ['reg-docs-url']], ['files', ['reg-files-name']]]) {
    await page.locator(`#reg-source-seg [data-src="${type}"]`).click();
    assert.equal(await page.locator(`#reg-source-seg [data-src="${type}"]`).getAttribute('aria-pressed'), 'true');
    for (const other of ['git', 'url', 'files']) {
      assert.equal(await page.locator(`#reg-${other}-fields`).isVisible(), type === other);
    }
    for (const input of inputs) await page.locator(`#${input}`).fill(draft[input]);
  }
  // Check actual browser hit testing. A short negative actionability probe is
  // intentional: a blocked background click should not consume a 30s timeout.
  const background = page.locator('nav.tabs [data-tab="keys"]');
  await assert.rejects(background.click({ timeout: 750 }), error => error.name === 'TimeoutError',
    'The native modal must prevent a background tab click');
  await background.evaluate(button => button.focus());
  assert.equal(await dialog.evaluate(node => node.contains(document.activeElement)), true,
    'Native inertness also prevents background focus');
  const point = await dialog.evaluate(node => {
    const r = node.getBoundingClientRect();
    return [
      { x: r.left / 2, y: innerHeight / 2 }, { x: (r.right + innerWidth) / 2, y: innerHeight / 2 },
      { x: innerWidth / 2, y: r.top / 2 }, { x: innerWidth / 2, y: (r.bottom + innerHeight) / 2 },
    ].find(p => p.x > 0 && p.y > 0 && p.x < innerWidth && p.y < innerHeight
      && (p.x < r.left || p.x > r.right || p.y < r.top || p.y > r.bottom));
  });
  assert.ok(point, 'Modal leaves a measurable backdrop');
  await page.mouse.click(point.x, point.y);
  assert.equal(await dialog.evaluate(node => node.open), true, 'Backdrop click does not dismiss the draft');
  await closePanel(h, 'registration-panel');
  await focused(page, '#btn-register-open');
  assert.equal(await page.locator('#btn-register-open').getAttribute('aria-expanded'), 'false');
  await page.locator('#btn-register-open').click();
  await opened(h, 'registration-panel');
  assert.equal(await page.locator('#btn-register-open').getAttribute('aria-expanded'), 'true');
  assert.equal(await page.locator('#reg-files-fields').isVisible(), true, 'Selected source type is preserved');
  for (const [id, value] of Object.entries(draft)) assert.equal(await page.locator(`#${id}`).inputValue(), value);
  await page.keyboard.press('Escape');
  await closed(page, 'registration-panel');
  await focused(page, '#btn-register-open');
  assert.deepEqual(changeRequests(h), []);
});

testCase('registration failure stays inline and retry closes only after successful fake submission', async h => {
  const { page } = h, held = deferred(h);
  const registeredId = 'github.com__modal__registered__main';
  let attempts = 0;
  await fakeApi(h, 'POST', '/repos', async body => {
    if (++attempts === 1) return { status: 503, body: { error: 'Offline registration retry required' } };
    await held.wait();
    const repo = { ...structuredClone(h.data.repos.find(repo => repo.source_type === 'git')),
      ...body, repo_id: registeredId, source_id: registeredId, server_name: 'modal-registered',
      owned: true, manageable: true, scope_editable: true };
    h.data.repos.push(repo);
    h.data.servers.push({ ...repo, kind: 'repo', server_id: registeredId,
      mcp_url: `https://mcp.navigation.test/mcp/${registeredId}` });
    return { body: { repo_id: registeredId } };
  });
  await page.locator('#btn-register-open').click();
  await opened(h, 'registration-panel');
  await page.locator('#reg-url').fill('https://example.invalid/modal.git');
  await page.locator('#reg-ref').fill('main');
  await page.locator('#reg-description').fill('Modal registration description');
  await choose(page, 'reg-scope-git', 'private');
  const submit = page.locator('#reg-git-fields .reg-submit');
  await responseFinished(page, '/repos', 'POST', () => submit.click());
  await inlineError(h, 'registration-panel');
  assert.equal(await page.locator('#reg-url').inputValue(), 'https://example.invalid/modal.git');
  const completed = observeResponse(page, response =>
    response.url() === `${origin}/api/repos` && response.request().method() === 'POST' && response.status() === 200);
  await submit.click();
  await bounded(held.seen, OPERATION_MS, 'Registration request start');
  assert.equal(await submit.isDisabled(), true);
  await busyCannotNavigate(h, 'registration-panel', 'keys');
  held.release();
  await (await completed).finished();
  await closed(page, 'registration-panel');
  await page.waitForFunction(id => S.repos.some(repo => repo.repo_id === id), registeredId);
  assert.deepEqual(postBodies(h, '/repos'), Array.from({ length: 2 }, () => ({
    git_url: 'https://example.invalid/modal.git', ref: 'main', trigger: 'poll',
    graph_scope: 'private', description: 'Modal registration description',
  })));
});

async function assertKeyCleared(page) {
  assert.equal(await page.locator('#modal-key').textContent(), '');
  assert.equal(await page.locator('#modal-snippet').textContent(), '');
  assert.equal(await page.locator('#modal-key').isVisible(), false);
  assert.equal(await page.evaluate(() => ['btn-copy-key', 'btn-copy-cmd', 'btn-copy-json']
    .every(id => document.getElementById(id).onclick === null)), true, 'Clear closures retaining the once-only key');
  assert.equal((await page.content()).includes(ISSUED_KEY), false, 'No retained key in hidden DOM');
}

testCase('key target scope exact issuance once-only close and auth reset discard late results', async h => {
  const { page } = h;
  await sourceAction(h, OWN_B, 'connection.title');
  await opened(h, 'connection-guide');
  await (await translatedButton(page, page.locator('#connection-content'), 'connection.issue')).click();
  await opened(h, 'key-form');
  await closed(page, 'connection-guide');
  assert.equal(await page.locator('#key-scope').inputValue(), OWN_B);
  await page.locator('#key-name').fill('modal-target-key');
  await page.locator('#key-days').fill('17');
  h.fail('/keys');
  await responseFinished(page, '/keys', 'POST', () => page.locator('#btn-key-create').click());
  await inlineError(h, 'key-form');
  assert.equal(await page.locator('#key-name').inputValue(), 'modal-target-key');
  assert.equal(await page.locator('#key-scope').inputValue(), OWN_B);
  await responseFinished(page, '/keys', 'POST', () => page.locator('#btn-key-create').click());
  await page.locator('#modal-key').waitFor({ state: 'visible' });
  await closed(page, 'key-form');
  assert.equal(await page.locator('#modal-key').textContent(), ISSUED_KEY);
  assert.equal(await page.locator('#modal-key').evaluate(node => {
    const dialog = node.closest('dialog');
    return dialog instanceof HTMLDialogElement && dialog.open && dialog.matches(':modal');
  }), true, 'Once-only result uses a native modal without assuming its wrapper shape');
  await page.locator('#btn-modal-close').click();
  await assertKeyCleared(page);
  assert.equal(await page.locator('dialog[open]').count(), 0);

  await openForm(h, 'keys', 'key-form', 'btn-key-open', 'key.new');
  await choose(page, 'key-scope', OWN_B);
  await page.locator('#key-name').fill('modal-target-key');
  await page.locator('#key-days').fill('17');
  await responseFinished(page, '/keys', 'POST', () => page.locator('#btn-key-create').click());
  await page.locator('#modal-key').waitFor({ state: 'visible' });
  await page.evaluate(() => ConsoleDialogs.reset());
  await assertKeyCleared(page);
  assert.equal(await page.locator('dialog[open]').count(), 0);

  const held = deferred(h);
  await fakeApi(h, 'POST', '/keys', async () => {
    await held.wait();
    return { body: { api_key: ISSUED_KEY } };
  });
  await ignoreCancellation(page, '/keys');
  await openForm(h, 'keys', 'key-form', 'btn-key-open', 'key.new');
  await choose(page, 'key-scope', OWN_B);
  await page.locator('#key-name').fill('modal-target-key');
  await page.locator('#key-days').fill('17');
  const late = observeResponse(page, response =>
    response.url() === `${origin}/api/keys` && response.request().method() === 'POST');
  await page.locator('#btn-key-create').click();
  await bounded(held.seen, OPERATION_MS, 'Key request start');
  await page.evaluate(() => showGate());
  await assertReset(page);
  held.release();
  await (await late).finished();
  await round(page); await round(page);
  await assertKeyCleared(page);
  await assertReset(page);
  assert.deepEqual(postBodies(h, '/keys'), Array.from({ length: 4 }, () => ({
    name: 'modal-target-key', scope: [OWN_B], expires_days: 17,
  })));
});

testCase('member identity cannot open or submit the admin invite modal', async h => {
  const { page } = h;
  assert.equal(await page.locator('nav.tabs [data-tab="admin"]').isVisible(), false);
  assert.equal(await page.locator('#btn-invite-open').isVisible(), false);
  // Guard the existing direct action as well as its hidden navigation entry.
  await page.evaluate(() => {
    goTab('admin', 'invite-form');
    document.getElementById('inv-email').value = 'offline-only@example.invalid';
  });
  await page.evaluate(() => invite());
  assert.equal(await page.locator('#page-admin').isVisible(), false);
  assert.equal(await page.locator('#invite-form').evaluate(node => node.hidden), true);
  assert.equal(await page.locator('#inv-email').isVisible(), false);
  assert.equal(await page.locator('dialog[open]').count(), 0);
  assert.deepEqual(h.data.requests.filter(request => request.path.startsWith('/admin/')), []);
});

testCase('admin invite error preserves input and role for a busy retry then closes on success', async h => {
  const { page } = h, held = deferred(h);
  const email = 'modal-invite@example.invalid';
  let attempts = 0;
  await fakeApi(h, 'POST', '/admin/invites', async body => {
    if (++attempts === 1) return { status: 503, body: { error: 'Offline invite retry required' } };
    await held.wait();
    h.data.users.push({ sub: 'modal-invited-user', email: body.email, is_admin: body.admin, status: 'FORCE_CHANGE_PASSWORD' });
    return { body: { email: body.email, group: body.admin ? 'admin' : 'member' } };
  });
  await openForm(h, 'admin', 'invite-form', 'btn-invite-open', 'adm.invite');
  await page.locator('#inv-email').fill(email);
  await choose(page, 'inv-admin', '1');
  await responseFinished(page, '/admin/invites', 'POST', () => page.locator('#btn-invite').click());
  await inlineError(h, 'invite-form');
  assert.equal(await page.locator('#inv-email').inputValue(), email);
  assert.equal(await page.locator('#inv-admin').inputValue(), '1');
  assert.equal(await page.locator('#btn-invite').isEnabled(), true);
  const completed = observeResponse(page, response =>
    response.url() === `${origin}/api/admin/invites` && response.status() === 200);
  await page.locator('#btn-invite').click();
  await bounded(held.seen, OPERATION_MS, 'Invite request start');
  assert.equal(await page.locator('#btn-invite').isDisabled(), true);
  assert.equal(await page.locator('#inv-email').isDisabled(), true);
  await busyCannotNavigate(h, 'invite-form', 'repos');
  held.release(); await (await completed).finished();
  await closed(page, 'invite-form');
  await page.waitForFunction(email => S.users.some(user => user.email === email), email);
  assert.equal(await page.locator('#inv-email').inputValue(), '');
  assert.deepEqual(postBodies(h, '/admin/invites'), [{ email, admin: true }, { email, admin: true }]);
}, { admin: true });

async function reachable(h, id, selector, label) {
  const target = h.page.locator(selector);
  await target.scrollIntoViewIfNeeded();
  // Trial click checks clipping, overlays, and native inertness without posting.
  await target.click({ trial: true });
  const facts = await target.evaluate(node => {
    const dialog = node.closest('dialog'), body = dialog.querySelector('.console-panel__body');
    const rect = node.getBoundingClientRect(), d = dialog.getBoundingClientRect();
    const b = body.getBoundingClientRect(), c = dialog.querySelector('.console-panel__header [data-panel-close], .console-panel__close').getBoundingClientRect();
    const rectObject = r => ({ left: r.left, top: r.top, right: r.right, bottom: r.bottom, width: r.width, height: r.height });
    return {
      viewport: { width: innerWidth, height: innerHeight }, rootWidth: document.documentElement.scrollWidth,
      dialogWidth: dialog.clientWidth, dialogScrollWidth: dialog.scrollWidth,
      bodyWidth: body.clientWidth, bodyScrollWidth: body.scrollWidth,
      bodyScrollHeight: body.scrollHeight, bodyHeight: body.clientHeight,
      target: rectObject(rect), dialog: rectObject(d), body: rectObject(b), close: rectObject(c),
    };
  });
  h.modal.measurements.push({ label, ...facts });
  const { viewport, target: r, dialog: d, close: c } = facts;
  assert.ok(facts.rootWidth <= viewport.width + 1 && facts.dialogScrollWidth <= facts.dialogWidth + 1
    && facts.bodyScrollWidth <= facts.bodyWidth + 1, `${label}: no horizontal overflow: ${JSON.stringify(facts)}`);
  for (const [name, box] of [['action', r], ['dialog', d], ['close', c]]) {
    assert.ok(box.width > 0 && box.height > 0 && box.left >= -1 && box.right <= viewport.width + 1
      && box.top >= -1 && box.bottom <= viewport.height + 1, `${label}: ${name} is reachable: ${JSON.stringify(box)}`);
  }
  assert.ok(r.left >= facts.body.left - 1 && r.right <= facts.body.right + 1
    && r.top >= facts.body.top - 1 && r.bottom <= facts.body.bottom + 1,
  `${label}: the entire primary action fits inside the scrolling body`);
  await headerClose(h.page, id).click({ trial: true });
  return facts;
}

testCase('desktop and mobile long forms keep primary actions and header close reachable without overflow', async h => {
  const { page } = h;
  for (const viewport of [{ width: 1440, height: 900, lang: 'en' }, { width: 390, height: 600, lang: 'ko' }]) {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await page.evaluate(lang => setLang(lang), viewport.lang);
    await goto(page, 'repos');
    await page.locator('#btn-register-open').click();
    await opened(h, 'registration-panel');
    await page.locator('#reg-description').fill('Long original description · 원문—보존 '.repeat(12));
    let mobileScrolls = false;
    for (const type of ['git', 'url', 'files']) {
      await page.locator(`#reg-source-seg [data-src="${type}"]`).click();
      const facts = await reachable(h, 'registration-panel', `#reg-${type}-fields .reg-submit`, `${viewport.width}-registration-${type}`);
      mobileScrolls ||= facts.bodyScrollHeight > facts.bodyHeight + 1;
    }
    if (viewport.width === 390) assert.ok(mobileScrolls, 'Mobile fixture must actually exercise a scrolling long form');
    await closePanel(h, 'registration-panel');
    await openForm(h, 'keys', 'key-form', 'btn-key-open', 'key.new');
    await page.locator('#key-name').fill('long-modal-key-name-'.repeat(3));
    await reachable(h, 'key-form', '#btn-key-create', `${viewport.width}-key`);
    await closePanel(h, 'key-form');
    await openForm(h, 'admin', 'invite-form', 'btn-invite-open', 'adm.invite');
    await page.locator('#inv-email').fill(`${'long-original-email-'.repeat(9)}@example.invalid`);
    await reachable(h, 'invite-form', '#btn-invite', `${viewport.width}-invite`);
    await closePanel(h, 'invite-form');
    await goto(page, 'repos');
    await sourceAction(h, OWN_A, 'repo.llm.settings');
    await opened(h, 'llm-panel');
    await choose(page, 'lp-enabled', '1');
    await reachable(h, 'llm-panel', '#lp-save', `${viewport.width}-llm`);
    await closePanel(h, 'llm-panel');
  }
  assert.deepEqual(changeRequests(h), [], 'Geometry probes must not submit any form');
}, { admin: true });

const SOURCE_PANELS = [
  { id: 'members-panel', action: 'mp.open', operation: 'members', prefix: 'mp', remove: 'mp.remove', deletion: 'members/remove' },
  { id: 'files-panel', action: 'fp.files', operation: 'uploads', prefix: 'fp', remove: 'fp.delete', deletion: 'uploads/delete' },
];

testCase('source panel replacements ignore old reads and build actions keep the selected repo', async h => {
  const { page } = h;
  for (const spec of SOURCE_PANELS) {
    const pathname = `/repos/${OWN_A}/${spec.operation}`;
    await ignoreCancellation(page, pathname);
    const held = holdRead(h, pathname);
    const old = observeResponse(page, response => response.url() === `${origin}/api${pathname}`);
    await sourceAction(h, OWN_A, spec.action);
    await opened(h, spec.id);
    await bounded(held.seen, OPERATION_MS, 'Old source read start');
    // A modal makes the list inert. Exercise the existing replacement entry
    // point directly instead of force-clicking through that modal.
    await page.evaluate(({ id, repo }) => {
      if (id === 'members-panel') openMembersPanel(repo);
      else openFilesPanel(repo);
    }, { id: spec.id, repo: OWN_B });
    await page.waitForFunction(({ prefix, repo }) =>
      document.getElementById(`${prefix}-tbody`).textContent.includes(repo), { prefix: spec.prefix, repo: OWN_B });
    held.release(); await (await old).finished();
    await round(page); await round(page);
    await opened(h, spec.id);
    assert.equal(await page.locator(`#${spec.prefix}-repo`).textContent(), OWN_B);
    assert.equal(await page.evaluate(prefix => prefix === 'mp' ? MP.repoId : FP.repoId, spec.prefix), OWN_B);
    assert.equal((await page.locator(`#${spec.prefix}-tbody`).textContent()).includes(OWN_A), false);
    assert.equal(await page.locator('dialog.console-panel-dialog[open]').count(), 1);
    await closePanel(h, spec.id);
  }
  await fakeApi(h, 'GET', `/repos/${OWN_B}/build`, async () => ({
    body: { repo_id: OWN_B, source_status: 'READY', can_rebuild: true, last_error: '', hints: [],
      build: null, logs: { state: 'available', events: [], next_token: null, truncated: false } },
  }));
  await sourceAction(h, OWN_B, 'bp.title');
  await opened(h, 'build-panel');
  await page.locator('#bp-content').waitFor({ state: 'visible' });
  assert.equal(await page.locator('#bp-repo').textContent(), OWN_B);
  await page.locator('#bp-ai').click();
  await opened(h, 'llm-panel');
  await closed(page, 'build-panel');
  assert.equal(await page.locator('#lp-repo').textContent(), OWN_B);
  assert.equal(await page.evaluate(() => LP.repo.repo_id), OWN_B);
  assert.equal(await page.locator('dialog.console-panel-dialog[open]').count(), 1);
  await page.evaluate(() => switchTab('keys'));
  await closed(page, 'llm-panel');
  assert.equal(await page.locator('#page-keys').isVisible(), true, 'Idle panel permits navigation');
  assert.deepEqual(changeRequests(h), []);
});

testCase('nested member and file delete confirmations restore parent on Cancel and pin exact deletion IDs', async h => {
  const { page } = h;
  const originalMembers = structuredClone(h.io.members.get(OWN_A));
  const originalFiles = structuredClone(h.io.files.get(OWN_A));
  for (const spec of SOURCE_PANELS) {
    await sourceAction(h, OWN_B, spec.action);
    await opened(h, spec.id);
    const row = page.locator(`#${spec.prefix}-tbody tr`);
    const remove = await translatedButton(page, row, spec.remove);
    await remove.waitFor({ state: 'visible' });
    const before = changeRequests(h).length;
    for (const cancelWith of ['button', 'Escape']) {
      await remove.click();
      const child = page.locator(CONFIRM);
      await child.waitFor({ state: 'visible' });
      assert.equal(await page.locator('dialog[open]').count(), 2, 'Confirmation overlays its open parent');
      assert.equal(await child.evaluate(node => node.matches(':modal') && node.contains(document.activeElement)), true);
      assert.equal(await panel(page, spec.id).evaluate(node => node.open), true);
      const subject = spec.id === 'members-panel' ? h.io.members.get(OWN_B)[0].label : h.io.files.get(OWN_B)[0].path;
      assert.ok((await child.textContent()).includes(subject), 'Confirmation retains the literal selected member/path');
      if (cancelWith === 'button') await child.locator('.console-dialog__cancel').click();
      else await page.keyboard.press('Escape');
      await child.waitFor({ state: 'hidden' });
      await page.waitForFunction(({ prefix }) => document.querySelector(`#${prefix}-tbody button`) === document.activeElement, spec);
      await opened(h, spec.id);
      assert.equal(changeRequests(h).length, before, 'Cancel must not perform deletion');
    }
    const body = spec.id === 'members-panel'
      ? { sub: `${OWN_B}-member` } : { paths: [h.io.files.get(OWN_B)[0].path] };
    await remove.click();
    await page.locator(CONFIRM).waitFor({ state: 'visible' });
    await responseFinished(page, `/repos/${OWN_B}/${spec.deletion}`, 'POST',
      () => page.locator(CONFIRM).locator('.console-dialog__submit').click());
    await page.waitForFunction(prefix => !document.querySelector(`#${prefix}-tbody button`), spec.prefix);
    await page.waitForFunction(prefix => prefix === 'mp' ? !MP.busy && !MP.loading : !FP.busy && !FP.loading, spec.prefix);
    assert.equal(await panel(page, spec.id).evaluate(node => node.open), true, 'Deletion refresh stays in its parent panel');
    assert.deepEqual(postBodies(h, `/repos/${OWN_B}/${spec.deletion}`), [body]);
    await closePanel(h, spec.id);
  }
  assert.deepEqual(h.io.members.get(OWN_A), originalMembers);
  assert.deepEqual(h.io.files.get(OWN_A), originalFiles);
  assert.deepEqual(changeRequests(h).map(request => [request.method, request.path]), [
    ['POST', `/repos/${OWN_B}/members/remove`], ['POST', `/repos/${OWN_B}/uploads/delete`],
  ]);
});

async function assertReset(page) {
  await page.locator('#gate').waitFor({ state: 'visible' });
  assert.equal(await page.locator('#app').isVisible(), false);
  assert.equal(await page.locator('dialog[open]').count(), 0, 'Reset closes parents, confirmations, and key results');
  assert.equal(await page.evaluate(ids => ids.every(id => document.getElementById(id).hidden), PANEL_IDS), true);
  assert.deepEqual(await page.evaluate(() => ({
    me: S.me, repos: S.repos, users: S.users, keys: S.keys,
    memberRepo: MP.repoId, fileRepo: FP.repoId, llmRepo: LP.repo, buildRepo: BP.repoId, connection: CONNECTION.id,
  })), { me: null, repos: [], users: [], keys: [], memberRepo: '', fileRepo: '', llmRepo: null, buildRepo: '', connection: '' });
}

testCase('auth reset closes a nested confirmation and late source admin reads cannot restore private UI', async h => {
  const { page } = h, pathname = `/repos/${OWN_A}/members`;
  await ignoreCancellation(page, pathname);
  const members = holdRead(h, pathname), admin = holdRead(h, '/admin/users');
  const memberResponse = observeResponse(page, response => response.url() === `${origin}/api${pathname}`);
  const adminResponse = observeResponse(page, response => response.url() === `${origin}/api/admin/users`);
  await sourceAction(h, OWN_A, 'mp.open');
  await opened(h, 'members-panel');
  await bounded(members.seen, OPERATION_MS, 'Private member read start');
  await page.evaluate(repo => {
    window.__lateModalAdmin = loadUsers().catch(error => ({ error: error.name }));
    openFilesPanel(repo);
  }, OWN_B);
  await bounded(admin.seen, OPERATION_MS, 'Private admin read start');
  await opened(h, 'files-panel');
  await closed(page, 'members-panel');
  const remove = await translatedButton(page, page.locator('#fp-tbody tr'), 'fp.delete');
  await remove.waitFor({ state: 'visible' });
  await remove.click();
  await page.locator(CONFIRM).waitFor({ state: 'visible' });
  assert.equal(await page.locator('dialog[open]').count(), 2);
  await page.evaluate(() => showGate());
  await assertReset(page);
  members.release(); admin.release();
  await Promise.all([(await memberResponse).finished(), (await adminResponse).finished()]);
  await page.evaluate(() => window.__lateModalAdmin);
  await round(page); await round(page);
  await assertReset(page);
  assert.equal(await page.locator('#mp-tbody').textContent(), '');
  assert.equal(await page.locator('#fp-tbody').textContent(), '');
  await assertKeyCleared(page);
  assert.deepEqual(changeRequests(h), [], 'Reset cancels an unaccepted deletion without issuing a write');
}, { admin: true });

async function runCase(browser, item, index) {
  const entry = { name: item.name, admin: !!item.admin, status: 'PASS' };
  const contexts = new Set(), started = Date.now();
  let h, closing = false;
  // Capture contexts before uxHarness returns, so boot failures/timeouts cannot
  // leak a context. A context resolving after the deadline closes immediately.
  const scopedBrowser = {
    async newContext(options) {
      if (closing) throw new Error('Case already closed');
      const context = await browser.newContext(options);
      contexts.add(context);
      if (closing) { await context.close(); throw new Error('Case closed during context creation'); }
      context.setDefaultTimeout(OPERATION_MS);
      context.setDefaultNavigationTimeout(OPERATION_MS);
      return context;
    },
  };
  const work = (async () => {
    h = await uxHarness(scopedBrowser, item.admin);
    if (closing) return;
    h.modal = { releases: [], panels: new Set(), measurements: [], routeErrors: [], nativeDialogs: [], closing: false };
    h.page.setDefaultTimeout(OPERATION_MS);
    h.page.setDefaultNavigationTimeout(OPERATION_MS);
    h.page.on('dialog', dialog => {
      h.modal.nativeDialogs.push({ type: dialog.type(), message: dialog.message() });
      dialog.dismiss().catch(error => { if (!h.modal.closing) h.modal.routeErrors.push(String(error)); });
    });
    await h.page.evaluate(ids => {
      window.__modalOriginals = Object.fromEntries(ids.map(id => [id, document.getElementById(id)]));
      window.__modalUnhandled = [];
      addEventListener('unhandledrejection', event => {
        window.__modalUnhandled.push(event.reason?.stack || String(event.reason));
      });
    }, PANEL_IDS);
    await item.run(h);
    await round(h.page);
    h.assertClean();
    assert.deepEqual(h.modal.routeErrors, [], 'Fake route errors');
    assert.deepEqual(h.modal.nativeDialogs, [], 'No blocking browser alert/confirm/prompt fallback');
    assert.deepEqual(await h.page.evaluate(() => window.__modalUnhandled), [], 'Unhandled application promises');
  })();
  work.catch(() => {});
  try {
    await bounded(work, CASE_MS, 'Modal case');
  } catch (error) {
    entry.status = 'FAIL'; entry.error = error.stack || String(error);
    closing = true;
    if (h?.modal) h.modal.closing = true;
    if (h && !h.page.isClosed()) {
      const base = `failure-${index + 1}`;
      await h.page.screenshot({ path: path.join(OUT, `${base}.png`), fullPage: true, timeout: 3000 })
        .then(() => { entry.screenshot = `${base}.png`; }).catch(() => {});
      await bounded(h.page.content(), 3000, 'Failure HTML capture').then(html => {
        fs.writeFileSync(path.join(OUT, `${base}.html`), html);
        entry.html = `${base}.html`;
      }).catch(() => {});
    }
  } finally {
    closing = true;
    if (h?.modal) {
      h.modal.closing = true;
      for (const release of h.modal.releases) release();
      entry.panels = [...h.modal.panels];
      entry.measurements = h.modal.measurements;
      entry.nativeDialogs = h.modal.nativeDialogs;
      entry.routeErrors = h.modal.routeErrors;
    }
    if (h) entry.diagnostics = h.diagnostics();
    const cleanup = await Promise.allSettled([...contexts].map(async context => {
      // Released fixture reads may still be fulfilling when the context closes.
      // Ignore only teardown-time route rejections, never in-case diagnostics.
      try { await context.unrouteAll({ behavior: 'ignoreErrors' }); }
      finally { await context.close(); }
    }));
    entry.cleanupErrors = cleanup.filter(result => result.status === 'rejected').map(result => String(result.reason));
    if (entry.cleanupErrors.length) entry.status = 'FAIL';
    entry.duration_ms = Date.now() - started;
  }
  return entry;
}

async function main() {
  if (process.argv.includes('--list')) { console.log(cases.map(item => item.name).join('\n')); return; }
  const filters = process.argv.slice(2).map(value => value.toLowerCase());
  const selected = cases.filter(item => !filters.length || filters.some(filter => item.name.toLowerCase().includes(filter)));
  assert.ok(selected.length, 'No matching modal flow cases');
  fs.mkdirSync(OUT, { recursive: true });
  const report = {
    generated_at: new Date().toISOString(), status: 'FAIL', filters,
    scope: 'Actual console HTML/CSS/handlers; synthetic admin/member sessions; intercepted fake API only. GraphExplorer is the existing harness stub.',
    assumptions: [
      'Shared mountPanel preserves original panel nodes inside .console-panel__body and supplies a header close action.',
      'Primary form errors stay accessible inside the modal; success closes it; busy navigation is vetoed; reset forces closure.',
      'Panel dialog IDs are data-panel-id values; key-result wrapper shape is not assumed.',
      'Group editor functionality is covered by the separate group suites.',
    ],
    operation_timeout_ms: OPERATION_MS, case_timeout_ms: CASE_MS, assets: [], tests: [],
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
      fs.writeFileSync(path.join(OUT, `case-${index + 1}.json`), JSON.stringify(entry, null, 2) + '\n');
      console.log(`${entry.status} ${entry.name}`);
    }
  } catch (error) {
    report.error = error.stack || String(error);
  } finally {
    if (browser) await browser.close().catch(error => { report.error = error.stack || String(error); });
    for (const asset of report.assets) {
      try {
        asset.changedDuringRun = crypto.createHash('sha256')
          .update(fs.readFileSync(path.join(ROOT, asset.file))).digest('hex') !== asset.sha256;
      } catch (error) { asset.changedDuringRun = true; asset.error = String(error); }
    }
    report.passed = report.tests.filter(item => item.status === 'PASS').length;
    report.total = selected.length;
    report.status = !report.error && report.passed === report.total
      && report.assets.length > 0 && report.assets.every(asset => !asset.changedDuringRun) ? 'PASS' : 'FAIL';
    const artifact = path.join(OUT, `results${filters.length ? '-filtered' : ''}.json`);
    fs.writeFileSync(artifact, JSON.stringify(report, null, 2) + '\n');
    console.log(JSON.stringify({ status: report.status, passed: report.passed, total: report.total, artifact }));
    if (report.status === 'FAIL') process.exitCode = 1;
  }
}

if (require.main === module) main().catch(error => { console.error(error); process.exitCode = 1; });
