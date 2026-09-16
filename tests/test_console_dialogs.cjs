#!/usr/bin/env node
'use strict';

/**
 * Run ONLY after the dialog implementation is ready:
 * GRAPHIFY_PLAYWRIGHT_MODULE=/existing/path/to/playwright \
 *   node tests/test_console_dialogs.cjs [case-name-substring]
 *
 * Twelve independent Chrome cases; --list lists them without launching Chrome.
 * Module lookup: explicit env override, require('playwright'), then existing
 * ~/.npm/_npx packages (or npm_config_cache). Nothing is installed/downloaded.
 * GRAPHIFY_DIALOGS_ASYNC_VALIDATE=1 enables bonus async-validator checks.
 *
 * Only console/dialogs.js and console/dialogs.css are read, once per run.
 * Each context uses setContent + inline local assets, blocks all requests and
 * WebSockets, and runs offline. No index.html, other suites, server, or AWS.
 * Diagnostics and failure screenshots go to a fresh directory under /tmp.
 */
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const ROOT = path.resolve(__dirname, '..');
const MODAL = 'dialog.console-dialog[open]';
const INPUT = '.console-dialog__input';
const CANCEL = '.console-dialog__cancel';
const SUBMIT = '.console-dialog__submit';
const ERROR = '.console-dialog__error';
const STATUS = '.console-dialog__status';
const GENERIC_ERROR = 'Check the value and try again.';
const ASYNC_VALIDATE = process.env.GRAPHIFY_DIALOGS_ASYNC_VALIDATE === '1';
const cases = [];
const testCase = (name, run, options = {}) => cases.push({ name, run, ...options });
const sha256 = content => crypto.createHash('sha256').update(content).digest('hex');
const shellQuote = value => `'${value.replace(/'/g, "'\\''")}'`;

function loadPlaywright() {
  function load(specifier) {
    const modulePath = require.resolve(specifier);
    const { chromium } = require(modulePath);
    assert.ok(chromium && typeof chromium.launch === 'function', `No Chromium API in ${modulePath}`);
    return { chromium, modulePath };
  }
  if (process.env.GRAPHIFY_PLAYWRIGHT_MODULE) return load(process.env.GRAPHIFY_PLAYWRIGHT_MODULE);
  try {
    require.resolve('playwright');
    return load('playwright');
  } catch (error) {
    if (error.code !== 'MODULE_NOT_FOUND') throw error;
  }
  const candidates = [];
  const roots = new Set([path.join(os.homedir(), '.npm'), process.env.npm_config_cache].filter(Boolean));
  for (const root of roots) {
    const cache = path.join(root, '_npx');
    if (!fs.existsSync(cache)) continue;
    for (const entry of fs.readdirSync(cache, { withFileTypes: true })) {
      if (!entry.isDirectory()) continue;
      const directory = path.join(cache, entry.name, 'node_modules', 'playwright');
      const manifest = path.join(directory, 'package.json');
      if (!fs.existsSync(manifest)) continue;
      const metadata = JSON.parse(fs.readFileSync(manifest, 'utf8'));
      if (metadata.name === 'playwright') candidates.push({ directory, version: metadata.version });
    }
  }
  // Prefer the newest stable cached package, then a cached prerelease.
  candidates.sort((a, b) => Number(a.version.includes('-')) - Number(b.version.includes('-'))
    || b.version.localeCompare(a.version, 'en', { numeric: true })
    || a.directory.localeCompare(b.directory));
  if (candidates.length) return load(candidates[0].directory);
  throw new Error('No cached Playwright found. Set GRAPHIFY_PLAYWRIGHT_MODULE to an existing Playwright package.');
}

const FIXTURE = `<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Offline console dialog tests</title>
<style>
  body { margin: 0; font: 16px/1.5 system-ui, sans-serif; }
  #fixture-background { padding: 20px; }
  #fixture-cover { position: fixed; inset: 0; z-index: 2147483647;
    background: rgba(80, 90, 100, .2); pointer-events: none; }
</style></head><body>
<main id="fixture-background">
  <button id="opener" type="button">Original opener</button>
  <button id="other-opener" type="button">Other opener</button>
</main>
<div id="fixture-cover" aria-hidden="true"></div>
</body></html>`;

async function openFixture(browser, assets, options, diagnostics) {
  const context = await browser.newContext({
    viewport: options.viewport || { width: 1280, height: 900 },
    reducedMotion: 'reduce', serviceWorkers: 'block',
    ...(options.mobile ? { isMobile: true, hasTouch: true } : {}),
  });
  try {
    await context.route('**/*', async route => {
      diagnostics.blockedRequests.push(route.request().url());
      await route.abort('blockedbyclient');
    });
    await context.routeWebSocket('**/*', socket => {
      diagnostics.webSockets.push(socket.url());
      socket.close();
    });
    await context.setOffline(true);
    context.on('request', request => diagnostics.requests.push({
      method: request.method(), url: request.url(), type: request.resourceType(),
    }));
    context.on('requestfailed', request => diagnostics.requestFailures.push({
      url: request.url(), error: request.failure()?.errorText,
    }));
    const page = await context.newPage();
    page.setDefaultTimeout(5000);
    page.on('pageerror', error => diagnostics.pageErrors.push(error.stack || String(error)));
    page.on('crash', () => diagnostics.pageErrors.push('Page crashed'));
    page.on('console', message => {
      if (message.type() === 'error') diagnostics.consoleErrors.push({
        text: message.text(), location: message.location(),
      });
    });
    page.on('dialog', dialog => {
      diagnostics.nativeDialogs.push({ type: dialog.type(), message: dialog.message() });
      dialog.dismiss().catch(error => diagnostics.pageErrors.push(String(error)));
    });
    page.on('framenavigated', frame => diagnostics.navigations.push(frame.url()));
    context.on('page', popup => {
      diagnostics.popups.push(popup.url());
      popup.close().catch(error => diagnostics.pageErrors.push(String(error)));
    });
    await page.setContent(FIXTURE);
    await page.addStyleTag({ content: assets['dialogs.css'] });
    await page.addScriptTag({ content: `(${installProbe.toString()})();` });
    if (options.lang) {
      // A classic script's top-level let is lexical, NOT a window property.
      await page.addScriptTag({ content: `let LANG = ${JSON.stringify(options.lang)};` });
    }
    await page.addScriptTag({ content: assets['dialogs.js'] });
    assert.deepEqual(await page.evaluate(() => [
      typeof window.ConsoleDialogs?.confirm,
      typeof window.ConsoleDialogs?.prompt,
      typeof window.ConsoleDialogs?.reset,
    ]), ['function', 'function', 'function'], 'The public ConsoleDialogs API must be available');
    await page.locator('#opener').focus();
    return { page, context };
  } catch (error) {
    await context.close();
    throw error;
  }
}

// Browser-side observation only: no patches to dialog, focus, or keyboard APIs.
function installProbe() {
  const probe = window.__dialogTest = {
    results: {}, nodes: {}, gates: {}, calls: [], unhandled: [], apiRejections: [],
    start(id, kind, options) {
      if (probe.results[id]) throw new Error(`Duplicate fixture operation: ${id}`);
      const result = probe.results[id] = { state: 'pending', settlements: 0 };
      const promise = window.ConsoleDialogs[kind](options);
      if (!(promise instanceof Promise)) throw new Error(`${kind} must return a Promise`);
      promise.then(value => {
        Object.assign(result, { state: 'resolved', value, settlements: result.settlements + 1 });
      }, error => {
        const message = error?.stack || String(error);
        Object.assign(result, { state: 'rejected', error: message, settlements: result.settlements + 1 });
        probe.apiRejections.push(message);
      });
    },
    defer(id) {
      if (probe.gates[id]) throw new Error(`Duplicate fixture gate: ${id}`);
      return new Promise((resolve, reject) => { probe.gates[id] = { resolve, reject }; });
    },
  };
  window.addEventListener('unhandledrejection', event => {
    probe.unhandled.push(event.reason?.stack || String(event.reason));
  });
}

async function ready(page, id) {
  await page.locator(MODAL).waitFor({ state: 'visible' });
  assert.equal(await page.locator(MODAL).count(), 1, 'Exactly one dialog may be active');
  await page.evaluate(({ id, selector }) => {
    window.__dialogTest.nodes[id] = document.querySelector(selector);
  }, { id, selector: MODAL });
  return page.locator(MODAL);
}

async function open(page, id, kind, options) {
  await page.evaluate(({ id, kind, options }) => {
    window.__dialogTest.start(id, kind, options);
  }, { id, kind, options });
  return ready(page, id);
}

// Flush promise continuations, native close events, and browser rendering without
// guessing how long a validator or submission will take.
async function nextFrames(page) {
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
}

async function outcome(page, id, expected, { closed = true, opener = '#opener' } = {}) {
  await page.waitForFunction(id => window.__dialogTest.results[id]?.state !== 'pending', id);
  assert.deepEqual(await page.evaluate(id => window.__dialogTest.results[id], id),
    { state: 'resolved', settlements: 1, value: expected }, `${id}: public promise result`);
  if (closed) {
    await page.locator(MODAL).waitFor({ state: 'hidden' });
    await nextFrames(page);
    if (opener) {
      assert.equal(await page.locator(opener).evaluate(element => element === document.activeElement),
        true, `${id}: focus returns to the original opener`);
    } else {
      assert.equal(await page.locator('#opener').evaluate(element => element === document.activeElement),
        false, `${id}: reset must not refocus a private opener`);
      assert.equal(await page.evaluate(() => document.activeElement.closest('dialog')), null);
    }
  }
}

async function pending(page, id) {
  await nextFrames(page);
  assert.deepEqual(await page.evaluate(id => window.__dialogTest.results[id], id),
    { state: 'pending', settlements: 0 }, `${id}: must not settle before success or cancellation`);
}

async function focused(page, selector) {
  assert.equal(await page.locator(selector).evaluate(element => element === document.activeElement),
    true, `Expected focus on ${selector}`);
}

async function inlineError(page, message, value, invalid = true) {
  await page.locator(`${MODAL} ${ERROR}`).waitFor({ state: 'visible' });
  await page.waitForFunction(({ selector, message }) =>
    document.querySelector(selector)?.textContent.includes(message), { selector: `${MODAL} ${ERROR}`, message });
  assert.equal(await page.locator(`${MODAL} ${ERROR}`).getAttribute('role'), 'alert');
  assert.equal(await page.locator(INPUT).inputValue(), value, 'Errors preserve the entered value');
  await page.waitForFunction(({ input, submit }) =>
    !document.querySelector(input).readOnly && !document.querySelector(submit).disabled,
  { input: INPUT, submit: SUBMIT });
  assert.equal(await page.locator(INPUT).isEnabled(), true);
  assert.equal(await page.locator(INPUT).getAttribute('aria-invalid') === 'true', invalid,
    'Only validation failures mark the input invalid');
  const errorId = await page.locator(ERROR).getAttribute('id');
  assert.ok(errorId, 'Inline error needs an accessible description ID');
  assert.ok((await page.locator(INPUT).getAttribute('aria-describedby') || '').split(/\s+/).includes(errorId),
    'The input references its inline error for assistive technology');
  assert.equal(await page.locator(MODAL).getAttribute('aria-busy'), 'false');
}

async function busy(page, id, value, text = 'Saving...', saveLabel = 'Save') {
  await page.waitForFunction(({ input, submit }) =>
    document.querySelector(input)?.readOnly && document.querySelector(submit)?.disabled,
  { input: INPUT, submit: SUBMIT });
  assert.equal(await page.locator(INPUT).inputValue(), value);
  assert.equal(await page.locator(INPUT).isEnabled(), true, 'Busy input must be readonly, not disabled');
  assert.equal(await page.locator(CANCEL).isEnabled(), true, 'Cancel stays available during submission');
  assert.equal(await page.locator(SUBMIT).textContent(), saveLabel, 'The submit label stays stable while busy');
  assert.equal(await page.locator(MODAL).getAttribute('aria-busy'), 'true');
  const status = page.locator(`${STATUS}[role="status"]`);
  await status.waitFor({ state: 'visible' });
  assert.equal((await status.textContent()).trim(), text);
  await pending(page, id);
}

async function release(page, gate, kind = 'resolve', value = undefined) {
  await page.waitForFunction(gate => !!window.__dialogTest.gates[gate], gate);
  await page.evaluate(({ gate, kind, value }) => {
    window.__dialogTest.gates[gate][kind](kind === 'reject' ? new Error(value || 'Late fixture rejection') : value);
  }, { gate, kind, value });
  await nextFrames(page);
}

testCase('native top-layer confirm has accessible structure, submit focus and opener restoration', async ({ page }) => {
  assert.equal(await page.evaluate(() => typeof LANG), 'undefined', 'Default locale fixture has no LANG binding');
  const dialog = await open(page, 'confirm', 'confirm', { title: 'Apply changes?', message: 'Apply these local changes.' });
  assert.equal(await dialog.evaluate(element => element instanceof HTMLDialogElement && element.matches(':modal')),
    true, 'An open attribute alone is insufficient: showModal must enter the native top layer');
  assert.equal(await page.getByRole('dialog', { name: 'Apply changes?', exact: true }).count(), 1);
  assert.equal(await dialog.locator('form').count(), 1);
  assert.deepEqual(await dialog.locator('form').evaluate(form => ({
    method: form.method, noValidate: form.noValidate,
  })), { method: 'dialog', noValidate: true });
  assert.equal(await dialog.locator('h2').textContent(), 'Apply changes?');
  assert.equal(await dialog.locator(`${CANCEL}:is(button)`).count(), 1);
  assert.equal(await dialog.locator(`${SUBMIT}:is(button)`).count(), 1);
  assert.equal(await page.locator(CANCEL).textContent(), 'Cancel');
  assert.equal(await page.locator(SUBMIT).textContent(), 'Confirm');
  await focused(page, SUBMIT);
  await pending(page, 'confirm');
  assert.equal(await dialog.evaluate(element => {
    const cover = document.getElementById('fixture-cover');
    cover.style.pointerEvents = 'auto';
    const box = element.getBoundingClientRect();
    const hit = document.elementFromPoint(box.x + box.width / 2, box.y + box.height / 2);
    cover.style.pointerEvents = 'none';
    return element.contains(hit);
  }), true, 'Native modal is above even the highest-z-index fixture sibling');
  await page.evaluate(() => document.getElementById('other-opener').focus());
  await focused(page, SUBMIT);
  await page.keyboard.press('Enter');
  await outcome(page, 'confirm', true);
});

testCase('dangerous confirm starts on cancel; cancel, Escape and safe Enter return cancellation', async ({ page }) => {
  for (const action of ['cancel', 'Escape', 'Enter']) {
    const id = `danger-${action}`;
    await open(page, id, 'confirm', { title: 'Delete source?', message: 'This removes the source.', danger: true });
    await focused(page, CANCEL);
    if (action === 'cancel') await page.locator(CANCEL).click();
    else await page.keyboard.press(action);
    await outcome(page, id, false);
  }
  for (const kind of ['confirm', 'prompt']) {
    for (const action of ['cancel', 'Escape']) {
      const id = `${kind}-${action}`;
      await open(page, id, kind, { title: 'Cancel this operation', message: 'No changes yet.', label: 'Name' });
      if (kind === 'prompt') await page.locator(INPUT).fill('Unsaved edit');
      if (action === 'cancel') await page.locator(CANCEL).click();
      else await page.keyboard.press('Escape');
      await outcome(page, id, kind === 'confirm' ? false : null);
    }
  }
});

testCase('titles, messages, subjects, labels and input values render as literal text', async ({ page }) => {
  const literal = '<img src="https://dialog.invalid/pixel" onerror="window.__dialogInjected=1"> & "원문"';
  const title = `Title ${literal}`, message = `Message\n${literal}`, subject = `Subject ${literal}`;
  await open(page, 'literal-confirm', 'confirm', { title, message, subject, confirmLabel: `Delete ${literal}`, danger: true });
  assert.equal(await page.locator(`${MODAL} h2`).textContent(), title);
  assert.ok((await page.locator(MODAL).textContent()).includes(message));
  assert.ok((await page.locator(MODAL).textContent()).includes(subject));
  assert.equal(await page.locator(SUBMIT).textContent(), `Delete ${literal}`);
  assert.equal(await page.locator(`${MODAL} img, ${MODAL} script, ${MODAL} svg`).count(), 0);
  await page.locator(SUBMIT).click();
  await outcome(page, 'literal-confirm', true);
  await open(page, 'literal-prompt', 'prompt', {
    title, message, label: `Label ${literal}`, value: literal, placeholder: `Placeholder ${literal}`,
  });
  assert.equal(await page.locator(`${MODAL} h2`).textContent(), title);
  assert.ok((await page.locator(MODAL).textContent()).includes(message));
  assert.equal(await page.getByLabel(`Label ${literal}`, { exact: true }).count(), 1);
  assert.equal(await page.locator(INPUT).inputValue(), literal);
  assert.equal(await page.locator(INPUT).getAttribute('placeholder'), `Placeholder ${literal}`);
  assert.equal(await page.locator(`${MODAL} img, ${MODAL} script, ${MODAL} svg`).count(), 0);
  assert.equal(await page.evaluate(() => window.__dialogInjected), undefined);
  await page.locator(SUBMIT).click();
  await outcome(page, 'literal-prompt', literal);
});

testCase('prompt selects input, traps Tab in both directions and guards IME Enter', async ({ page }) => {
  const value = '고객 original value';
  await open(page, 'keyboard', 'prompt', { title: 'Rename source', label: 'Source name', value });
  assert.equal(await page.locator(SUBMIT).textContent(), 'Save');
  assert.equal(await page.locator(INPUT).evaluate(element => element instanceof HTMLInputElement && element.type === 'text'), true);
  assert.equal(await page.locator(INPUT).evaluate(element =>
    Array.from(element.labels || []).some(label => label.textContent === 'Source name')), true);
  await focused(page, INPUT);
  assert.deepEqual(await page.locator(INPUT).evaluate(element => [element.selectionStart, element.selectionEnd]), [0, value.length]);
  for (const key of ['Tab', 'Shift+Tab']) {
    const visited = new Set();
    for (let index = 0; index < 6; index++) {
      await page.keyboard.press(key);
      const focus = await page.evaluate(selector => {
        const dialog = document.querySelector(selector), active = document.activeElement;
        return { contained: dialog.contains(active), classes: active.className };
      }, MODAL);
      assert.equal(focus.contained, true, `${key} cannot escape the modal`);
      visited.add(focus.classes);
    }
    assert.equal(visited.size, 3, `${key} reaches input, cancel, and submit without trapping on one control`);
  }
  await page.locator(INPUT).focus();
  const prevented = await page.locator(INPUT).evaluate(element => {
    element.dispatchEvent(new CompositionEvent('compositionstart', { bubbles: true, data: '입력' }));
    const enter = new KeyboardEvent('keydown', {
      key: 'Enter', code: 'Enter', keyCode: 229, isComposing: true, bubbles: true, cancelable: true,
    });
    element.dispatchEvent(enter);
    element.dispatchEvent(new CompositionEvent('compositionend', { bubbles: true, data: '입력' }));
    return enter.defaultPrevented;
  });
  assert.equal(prevented, true, 'IME Enter must prevent native form submission, not merely ignore a synthetic key');
  await pending(page, 'keyboard');
  await page.locator(INPUT).fill('완성된 이름');
  await page.keyboard.press('Enter');
  await outcome(page, 'keyboard', '완성된 이름');
});

testCase('validation string and false errors allow retry; every valid sentinel accepts input', async ({ page }) => {
  for (const validKind of ['undefined', 'null', 'empty', 'true']) {
    const id = `valid-${validKind}`;
    await page.evaluate(({ id, validKind }) => {
      let attempts = 0;
      window.__dialogTest.start(id, 'prompt', {
        title: 'Validate a name', label: 'Name', value: 'keep this input',
        validate(value) {
          window.__dialogTest.calls.push({ id, stage: 'validate', value });
          if (++attempts === 1) return validKind === 'null' ? false : 'Use a different name.';
          return { undefined: undefined, null: null, empty: '', true: true }[validKind];
        },
        onSubmit(value) { window.__dialogTest.calls.push({ id, stage: 'submit', value }); },
      });
    }, { id, validKind });
    await ready(page, id);
    await page.locator(SUBMIT).click();
    await inlineError(page, validKind === 'null' ? GENERIC_ERROR : 'Use a different name.', 'keep this input');
    await pending(page, id);
    assert.equal(await page.evaluate(id => window.__dialogTest.calls.filter(call =>
      call.id === id && call.stage === 'submit').length, id), 0, 'Invalid input never reaches onSubmit');
    await page.locator(INPUT).fill(`accepted ${validKind}`);
    await page.locator(SUBMIT).click();
    await outcome(page, id, `accepted ${validKind}`);
    assert.deepEqual(await page.evaluate(id => window.__dialogTest.calls.filter(call => call.id === id), id), [
      { id, stage: 'validate', value: 'keep this input' },
      { id, stage: 'validate', value: `accepted ${validKind}` },
      { id, stage: 'submit', value: `accepted ${validKind}` },
    ]);
  }
  if (ASYNC_VALIDATE) {
    await page.evaluate(() => {
      let attempt = 0;
      window.__dialogTest.start('async-validation', 'prompt', {
        title: 'Async validation', label: 'Name', value: 'async value',
        validate() { return window.__dialogTest.defer(`validator-${++attempt}`); },
        onSubmit(value) { window.__dialogTest.calls.push({ id: 'async-validation', stage: 'submit', value }); },
      });
    });
    await ready(page, 'async-validation');
    for (const [attempt, result] of [[1, 'Async validation failed.'], [2, null]]) {
      await page.locator(SUBMIT).click();
      await page.waitForFunction(attempt => !!window.__dialogTest.gates[`validator-${attempt}`], attempt);
      await busy(page, 'async-validation', 'async value', 'Checking...');
      assert.equal(await page.evaluate(() => window.__dialogTest.calls.filter(call => call.id === 'async-validation').length), 0);
      await release(page, `validator-${attempt}`, 'resolve', result);
      if (attempt === 1) await inlineError(page, result, 'async value');
    }
    await outcome(page, 'async-validation', 'async value');
  }
});

testCase('thrown validation and sync/async submission errors stay inline and retry resolves only success', async ({ page }) => {
  const literalError = '<img src="https://dialog.invalid/error"> Keep the original input.';
  await page.evaluate(literalError => {
    let validations = 0, submissions = 0;
    window.__dialogTest.start('retry', 'prompt', {
      title: 'Retry changes', label: 'Name', value: 'original',
      validate() { if (++validations === 1) throw new Error('Validator threw.'); },
      onSubmit(value) {
        window.__dialogTest.calls.push({ stage: 'submit', value });
        if (++submissions === 1) throw new Error('Submission threw.');
        if (submissions === 2) return Promise.reject(new Error(literalError));
        return Promise.resolve('Callback result is not the prompt value');
      },
    });
  }, literalError);
  await ready(page, 'retry');
  for (const [value, message] of [
    ['original', 'Validator threw.'], ['edited once', 'Submission threw.'], ['edited twice', literalError],
  ]) {
    await page.locator(INPUT).fill(value);
    await page.locator(SUBMIT).click();
    await inlineError(page, message, value, message === 'Validator threw.');
    await pending(page, 'retry');
    assert.equal(await page.locator(`${MODAL} img`).count(), 0, 'Error messages also use literal text');
  }
  await page.locator(INPUT).fill('successful retry');
  await page.locator(SUBMIT).click();
  await outcome(page, 'retry', 'successful retry');
  assert.deepEqual(await page.evaluate(() => window.__dialogTest.calls.map(call => call.value)),
    ['edited once', 'edited twice', 'successful retry']);
});

testCase('async submission keeps input readonly, blocks duplicate submits and waits for success', async ({ page }) => {
  await page.evaluate(() => window.__dialogTest.start('busy', 'prompt', {
    title: 'Save source', label: 'Name', value: 'pending save',
    onSubmit(value) {
      window.__dialogTest.calls.push(value);
      return window.__dialogTest.defer('save');
    },
  }));
  await ready(page, 'busy');
  await page.locator(SUBMIT).click();
  await busy(page, 'busy', 'pending save');
  await page.locator(INPUT).focus();
  await page.keyboard.type('must not edit');
  await page.keyboard.press('Enter');
  // A programmatic form submit exercises the busy handler as well as the
  // disabled button; no artificial enable/disable changes are made.
  await page.locator(`${MODAL} form`).evaluate(form => form.requestSubmit());
  await pending(page, 'busy');
  assert.equal(await page.locator(INPUT).inputValue(), 'pending save');
  assert.deepEqual(await page.evaluate(() => window.__dialogTest.calls), ['pending save']);
  await release(page, 'save', 'resolve', 'unrelated callback return');
  await outcome(page, 'busy', 'pending save');
});

testCase('busy prompts remain cancellable by button and Escape despite late success or rejection', async ({ page }) => {
  for (const [action, completion] of [['cancel', 'resolve'], ['cancel', 'reject'], ['Escape', 'resolve'], ['Escape', 'reject']]) {
    const id = `${action}-${completion}`;
    await page.evaluate(id => window.__dialogTest.start(id, 'prompt', {
      title: 'Cancelable save', label: 'Name', value: id,
      onSubmit() { return window.__dialogTest.defer(id); },
    }), id);
    await ready(page, id);
    await page.locator(SUBMIT).click();
    await busy(page, id, id);
    if (action === 'cancel') await page.locator(CANCEL).click();
    else await page.keyboard.press('Escape');
    await outcome(page, id, null);
    await release(page, id, completion);
    await outcome(page, id, null);
    assert.equal(await page.locator(MODAL).count(), 0, 'Late completion cannot reopen a canceled modal');
  }
});

testCase('reset cancels idle and busy dialogs and invalidates late callback results', async ({ page }) => {
  for (const kind of ['confirm', 'prompt']) {
    const id = `reset-idle-${kind}`;
    await open(page, id, kind, { title: 'Reset this dialog', message: 'Cancel safely.', label: 'Name' });
    await page.evaluate(() => { window.ConsoleDialogs.reset(); window.ConsoleDialogs.reset(); });
    await outcome(page, id, kind === 'confirm' ? false : null, { opener: null });
  }
  for (const stage of ASYNC_VALIDATE ? ['onSubmit', 'validate'] : ['onSubmit']) {
    for (const completion of ['resolve', 'reject']) {
      const id = `reset-${stage}-${completion}`, successor = `${id}-successor`;
      await page.evaluate(({ id, stage }) => {
        window.__dialogTest.start(id, 'prompt', {
          title: 'Reset pending operation', label: 'Name', value: 'old value',
          [stage]() { return window.__dialogTest.defer(id); },
        });
      }, { id, stage });
      await ready(page, id);
      await page.locator(SUBMIT).click();
      await page.waitForFunction(id => !!window.__dialogTest.gates[id], id);
      await pending(page, id);
      await page.evaluate(() => window.ConsoleDialogs.reset());
      await outcome(page, id, null, { opener: null });
      // Reset intentionally does not return to the old private trigger.
      // A new dialog here represents a fresh user action on a live opener.
      await page.locator('#opener').focus();
      await open(page, successor, 'prompt', { title: 'Fresh after reset', label: 'Name', value: 'new value' });
      await release(page, id, completion);
      await outcome(page, id, null, { closed: false });
      await pending(page, successor);
      await focused(page, INPUT);
      assert.equal(await page.locator(INPUT).inputValue(), 'new value');
      assert.equal(await page.locator(SUBMIT).isEnabled(), true);
      assert.equal(await page.locator(ERROR).filter({ hasText: 'Late fixture rejection' }).count(), 0);
      await page.locator(SUBMIT).click();
      await outcome(page, successor, 'new value');
    }
  }
});

testCase('replacement uses fresh nodes, cancels old promises and ignores stale events and completions', async ({ page }) => {
  // Replace while focus belongs to the outgoing dialog; the original opener
  // must survive the entire chain, including queued native close events.
  await page.locator('#other-opener').focus();
  await open(page, 'first', 'confirm', { title: 'First', message: 'Will be replaced.' });
  await open(page, 'second', 'prompt', { title: 'Second', label: 'Name', value: 'new input' });
  await outcome(page, 'first', false, { closed: false });
  await open(page, 'third', 'confirm', { title: 'Third', message: 'Latest operation.' });
  await outcome(page, 'second', null, { closed: false });
  assert.equal(await page.evaluate(() => new Set([
    window.__dialogTest.nodes.first, window.__dialogTest.nodes.second, window.__dialogTest.nodes.third,
  ]).size), 3, 'Every call creates a fresh node');
  await pending(page, 'third');
  await focused(page, SUBMIT);
  await page.locator(SUBMIT).click();
  await outcome(page, 'third', true, { opener: '#other-opener' });
  for (const completion of ['resolve', 'reject']) {
    const id = `replace-${completion}`, successor = `${id}-successor`;
    await page.evaluate(id => window.__dialogTest.start(id, 'prompt', {
      title: 'Outgoing save', label: 'Name', value: 'outgoing',
      onSubmit() { return window.__dialogTest.defer(id); },
    }), id);
    await ready(page, id);
    await page.locator(SUBMIT).click();
    await busy(page, id, 'outgoing');
    await open(page, successor, 'prompt', { title: 'Incoming prompt', label: 'Name', value: 'incoming' });
    await outcome(page, id, null, { closed: false });
    assert.equal(await page.evaluate(({ id, successor }) =>
      window.__dialogTest.nodes[id] !== window.__dialogTest.nodes[successor], { id, successor }), true);
    await page.evaluate(id => {
      const old = window.__dialogTest.nodes[id];
      old.dispatchEvent(new Event('cancel', { cancelable: true }));
      old.dispatchEvent(new Event('close'));
    }, id);
    await release(page, id, completion);
    await pending(page, successor);
    assert.equal(await page.locator(MODAL).count(), 1);
    assert.equal(await page.locator(INPUT).inputValue(), 'incoming');
    assert.equal(await page.locator(SUBMIT).isEnabled(), true);
    assert.equal(await page.locator(ERROR).filter({ hasText: 'Late fixture rejection' }).count(), 0);
    await focused(page, INPUT);
    await page.locator(SUBMIT).click();
    await outcome(page, successor, 'incoming', { opener: '#other-opener' });
  }
});

testCase('lexical LANG is read per call for Korean and English labels and saving status', async ({ page }) => {
  // Declare LANG after the product script loads, as well as changing it across
  // calls. Leave <html lang="en"> alone so it cannot mask a wrong locale lookup.
  await page.addScriptTag({ content: "let LANG = 'ko';" });
  assert.equal(await page.evaluate(() => Object.hasOwn(window, 'LANG')), false);
  for (const [lang, cancel, confirm, save, saving] of [
    ['ko', '취소', '확인', '저장', '저장 중...'],
    ['en', 'Cancel', 'Confirm', 'Save', 'Saving...'],
    ['ko', '취소', '확인', '저장', '저장 중...'],
  ]) {
    const id = `locale-${lang}-${await page.evaluate(() => Object.keys(window.__dialogTest.results).length)}`;
    await page.evaluate(lang => { LANG = lang; }, lang);
    await open(page, `${id}-confirm`, 'confirm', { title: 'Locale confirmation', message: 'Check current language.' });
    assert.equal(await page.locator(CANCEL).textContent(), cancel);
    assert.equal(await page.locator(SUBMIT).textContent(), confirm);
    await page.locator(CANCEL).click();
    await outcome(page, `${id}-confirm`, false);
    await page.evaluate(id => window.__dialogTest.start(id, 'prompt', {
      title: 'Locale prompt', label: 'Name', value: 'localized value',
      onSubmit() { return window.__dialogTest.defer(id); },
    }), id);
    await ready(page, id);
    assert.equal(await page.locator(CANCEL).textContent(), cancel);
    assert.equal(await page.locator(SUBMIT).textContent(), save);
    await page.locator(SUBMIT).click();
    await busy(page, id, 'localized value', saving, save);
    await release(page, id);
    await outcome(page, id, 'localized value');
    assert.equal(await page.evaluate(() => Object.hasOwn(window, 'LANG')), false);
  }
});

async function fitsMobile(page, label) {
  const layout = await page.locator(MODAL).evaluate(dialog => {
    const rect = element => {
      const box = element.getBoundingClientRect();
      return { left: box.left, right: box.right, width: box.width, top: box.top, bottom: box.bottom };
    };
    const title = dialog.querySelector('h2'), range = document.createRange();
    range.selectNodeContents(title);
    return {
      viewport: innerWidth, height: innerHeight, documentWidth: document.documentElement.scrollWidth,
      dialog: rect(dialog), clientWidth: dialog.clientWidth, scrollWidth: dialog.scrollWidth,
      titleLines: [...new Set(Array.from(range.getClientRects(), box => Math.round(box.top)))].length,
      fixedParts: Array.from(dialog.querySelectorAll('.console-dialog__header, .console-dialog__actions, button'), rect),
      controls: Array.from(dialog.querySelectorAll('button, input, label, h2'), element => ({
        tag: element.tagName, ...rect(element),
      })),
    };
  });
  assert.ok(layout.documentWidth <= layout.viewport + 1, `${label}: document overflow ${JSON.stringify(layout)}`);
  assert.ok(layout.dialog.left >= -1 && layout.dialog.right <= layout.viewport + 1,
    `${label}: dialog exceeds viewport ${JSON.stringify(layout)}`);
  assert.ok(layout.scrollWidth <= layout.clientWidth + 1, `${label}: dialog horizontal overflow ${JSON.stringify(layout)}`);
  assert.ok(layout.titleLines > 1, `${label}: long title must wrap`);
  assert.ok(layout.controls.every(control => control.left >= -1 && control.right <= layout.viewport + 1),
    `${label}: controls overflow ${JSON.stringify(layout)}`);
  assert.ok(layout.fixedParts.every(part => part.top >= -1 && part.bottom <= layout.height + 1),
    `${label}: header, footer and buttons stay vertically on-screen ${JSON.stringify(layout)}`);
}

async function keyboardScrollBody(page, title) {
  const body = page.locator(`${MODAL} .console-dialog__body`);
  await page.waitForFunction(selector => {
    const element = document.querySelector(selector);
    return element?.scrollHeight > element?.clientHeight && element?.getAttribute('tabindex') === '0';
  }, `${MODAL} .console-dialog__body`);
  assert.equal(await body.getAttribute('role'), 'region');
  assert.equal(await page.getByRole('region', { name: title, exact: true }).count(), 1,
    'Overflowing body is a keyboard-accessible region labelled by the dialog heading');
  for (const key of ['Tab', 'Shift+Tab']) {
    await page.locator(SUBMIT).focus();
    let reached = false;
    for (let index = 0; index < 5; index++) {
      await page.keyboard.press(key);
      assert.equal(await page.locator(MODAL).evaluate(dialog => dialog.contains(document.activeElement)), true);
      if (await body.evaluate(element => element === document.activeElement)) { reached = true; break; }
    }
    assert.equal(reached, true, `${key} reaches the overflowing body through the focus trap`);
    await body.evaluate(element => { element.scrollTop = 0; });
    const documentScroll = await page.evaluate(() => window.scrollY);
    await page.keyboard.press('PageDown');
    await page.waitForFunction(selector => document.querySelector(selector).scrollTop > 1,
      `${MODAL} .console-dialog__body`);
    assert.equal(await page.evaluate(() => window.scrollY), documentScroll, 'PageDown scrolls the modal body only');
  }
}

testCase('mobile dialogs wrap long literal content and keep confirm, input and error actions reachable', async ({ page }) => {
  const title = '긴제목LongUnbrokenTitle'.repeat(8);
  // Force real body overflow at BOTH widths; a body that fits correctly stays
  // out of the Tab order and must not be mistaken for a keyboard-scroll defect.
  const message = 'UnbrokenMessageAndSourceName'.repeat(48);
  for (const width of [320, 390]) {
    await page.setViewportSize({ width, height: 844 });
    const id = `mobile-${width}`;
    await open(page, `${id}-confirm`, 'confirm', {
      title, message, subject: 'source/path/'.repeat(16), confirmLabel: '영구적으로 삭제 확인하기', danger: true,
    });
    await nextFrames(page);
    await fitsMobile(page, `${width}px confirmation`);
    await keyboardScrollBody(page, title);
    await fitsMobile(page, `${width}px confirmation after PageDown`);
    await page.locator(SUBMIT).click();
    await outcome(page, `${id}-confirm`, true);
    await page.evaluate(({ id, title, message }) => {
      let attempt = 0;
      window.__dialogTest.start(id, 'prompt', {
        title, message, label: 'LongUnbrokenLabel'.repeat(8), value: 'Mobile edited value',
        validate() { return ++attempt === 1 ? 'LongUnbrokenValidationError'.repeat(10) : undefined; },
      });
    }, { id, title, message });
    await ready(page, id);
    await page.locator(SUBMIT).click();
    await inlineError(page, 'LongUnbrokenValidationError'.repeat(10), 'Mobile edited value');
    await fitsMobile(page, `${width}px prompt with error`);
    await keyboardScrollBody(page, title);
    await fitsMobile(page, `${width}px prompt after PageDown`);
    await page.locator(INPUT).fill(`Saved at ${width}px`);
    await page.locator(SUBMIT).click();
    await outcome(page, id, `Saved at ${width}px`);
  }
}, { viewport: { width: 390, height: 844 }, mobile: true });

function newDiagnostics() {
  return Object.fromEntries([
    'requests', 'blockedRequests', 'requestFailures', 'webSockets', 'pageErrors',
    'consoleErrors', 'nativeDialogs', 'navigations', 'popups',
  ].map(key => [key, []]));
}

async function inspectFixture(page, diagnostics) {
  await nextFrames(page);
  const probe = await page.evaluate(() => ({
    unhandled: window.__dialogTest.unhandled,
    apiRejections: window.__dialogTest.apiRejections,
    pending: Object.entries(window.__dialogTest.results).filter(([, result]) => result.state === 'pending').map(([id]) => id),
  }));
  Object.assign(diagnostics, probe);
  for (const [name, entries] of Object.entries(diagnostics)) {
    assert.deepEqual(entries, [], `Unexpected ${name}: ${JSON.stringify(entries)}`);
  }
  assert.equal(await page.locator(MODAL).count(), 0, 'Each case must leave no active modal');
}

async function main() {
  const filter = process.argv[2];
  if (filter === '--list') {
    for (const item of cases) console.log(item.name);
    return;
  }
  const selected = cases.filter(item => !filter || item.name.toLowerCase().includes(filter.toLowerCase()));
  assert.ok(selected.length, `No cases match ${JSON.stringify(filter)}`);
  const assets = Object.fromEntries(['dialogs.js', 'dialogs.css'].map(file => [
    file, fs.readFileSync(path.join(ROOT, 'console', file), 'utf8'),
  ]));
  const { chromium, modulePath } = loadPlaywright();
  console.log(`Playwright: ${modulePath}`);
  console.log(`Command: GRAPHIFY_PLAYWRIGHT_MODULE=${shellQuote(modulePath)} node tests/test_console_dialogs.cjs`);
  console.log(`Bonus async validation: ${ASYNC_VALIDATE ? 'enabled' : 'disabled (GRAPHIFY_DIALOGS_ASYNC_VALIDATE=1)'}`);
  const output = fs.mkdtempSync('/tmp/graphify-console-dialogs-');
  const results = [];
  const browser = await chromium.launch({ channel: 'chrome', headless: true });
  const version = browser.version();
  try {
    for (const item of selected) {
      const started = Date.now(), diagnostics = newDiagnostics();
      const result = { name: item.name, status: 'PASS', diagnostics };
      let fixture;
      try {
        fixture = await openFixture(browser, assets, item, diagnostics);
        await item.run(fixture);
        await inspectFixture(fixture.page, diagnostics);
      } catch (error) {
        result.status = 'FAIL';
        result.error = error.stack || String(error);
        if (fixture) {
          const prefix = path.join(output, `failure-${results.length + 1}`);
          await fixture.page.screenshot({ path: `${prefix}.png`, fullPage: true }).catch(() => {});
          fs.writeFileSync(`${prefix}.html`, await fixture.page.content().catch(() => ''));
          const probe = await fixture.page.evaluate(() => ({
            unhandled: window.__dialogTest?.unhandled,
            apiRejections: window.__dialogTest?.apiRejections,
            results: window.__dialogTest?.results,
          })).catch(() => ({}));
          result.probe = probe;
        }
      } finally {
        if (fixture) await fixture.context.close();
        result.duration_ms = Date.now() - started;
        results.push(result);
      }
      console.log(`${result.status} ${item.name}`);
      if (result.error) console.error(result.error);
    }
  } finally {
    await browser.close();
    const snapshots = Object.entries(assets).map(([file, content]) => ({
      file: `console/${file}`, sha256: sha256(content),
      changedDuringRun: !fs.existsSync(path.join(ROOT, 'console', file))
        || sha256(fs.readFileSync(path.join(ROOT, 'console', file), 'utf8')) !== sha256(content),
    }));
    const passed = results.filter(result => result.status === 'PASS').length;
    const status = passed === selected.length && !snapshots.some(asset => asset.changedDuringRun) ? 'PASS' : 'FAIL';
    fs.writeFileSync(path.join(output, 'results.json'), JSON.stringify({
      status, passed, total: selected.length, browser: version, modulePath,
      asyncValidation: ASYNC_VALIDATE, assets: snapshots, cases: results,
      scope: 'Isolated offline pages with local dialogs.js and dialogs.css; no other console assets.',
    }, null, 2) + '\n');
    console.log(`${passed}/${selected.length} passed in Chrome ${version}. Diagnostics: ${output}`);
    if (snapshots.some(asset => asset.changedDuringRun)) console.error('Dialog assets changed during this run; rerun against a stable snapshot.');
    if (status === 'FAIL') process.exitCode = 1;
  }
}

if (require.main === module) main().catch(error => { console.error(error.stack || error); process.exitCode = 1; });
