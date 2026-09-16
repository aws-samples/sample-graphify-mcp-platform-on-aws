#!/usr/bin/env node
'use strict';

/**
 * Offline component coverage. Run only after the parent coordinates browser use:
 * node tests/test_console_panels.cjs [case-name-substring]
 * --list lists cases without loading Playwright or launching a browser.
 *
 * Uses the existing Playwright installation below, or an explicit
 * GRAPHIFY_PLAYWRIGHT_MODULE override. No installs, server, index.html or AWS.
 * Each case gets a separate offline context; all cases run sequentially.
 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const ROOT = path.resolve(__dirname, '..');
const PLAYWRIGHT = process.env.GRAPHIFY_PLAYWRIGHT_MODULE
  || 'playwright';
const TIMEOUT = 30000;
const PANEL = 'dialog.console-panel-dialog[open]';
const ACTION = 'dialog.console-dialog:not(.console-panel-dialog)[open]';
const cases = [];
const test = (name, run) => cases.push({ name, run });

const HTML = `<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Offline panel fixture</title><style>
body { margin:0; min-height:2400px; font:16px/1.5 system-ui,sans-serif; }
main { padding:20px; } input,textarea { max-width:100%; font-size:16px; }
header { margin:0; } h2 { margin:0; } button { min-height:32px; }
</style></head><body><main id="host">
<button id="opener">Open settings</button><button id="other-opener">Other opener</button>
<button id="fallback">Sources</button><span id="before">Before panels</span>
<section id="alpha" hidden aria-labelledby="alpha-title">
  <header id="alpha-header"><h2 id="alpha-title">Source settings</h2>
    <button id="alpha-close" data-panel-close type="button">Close settings</button></header>
  <form id="alpha-form"><label for="alpha-input">Source name</label>
    <input id="alpha-input" value="original source">
    <label for="alpha-notes">Notes</label><textarea id="alpha-notes">original notes</textarea>
    <button id="alpha-child" type="button">Open confirmation</button>
  </form><div id="alpha-long"></div>
</section>
<section id="beta" hidden><h2>Access settings</h2>
  <label for="beta-input">Member</label><input id="beta-input" value="member">
</section>
<section id="gamma" hidden>
  <h2><span class="dot"></span><span id="gamma-title">Build logs</span>
    <button type="button" data-panel-close>Close logs</button></h2>
  <label for="gamma-input">Filter</label><input id="gamma-input" value="log">
</section><span id="after">After panels</span>
</main></body></html>`;

function installFixture() {
  const fixture = window.fixture = {
    panels: {}, nodes: {}, closed: [], vetoes: [], blocked: false, closeClicks: 0,
    changes: 0, keys: [], unhandled: [], results: {}, gates: {}, locale: 'en',
    node(id) { return document.getElementById(id); },
    mount(id, extra = {}) {
      const node = fixture.node(id);
      const options = {
        heading: node.querySelector('header, h2'),
        titleId: id === 'beta' ? undefined : `${id}-title`,
        initialFocus: () => fixture.node(`${id}-input`),
        returnFocus: () => fixture.node('fallback'),
        closeLabel: () => fixture.locale === 'ko' ? '닫기' : 'Close',
        busyMessage: () => 'Wait for upload',
        beforeClose(reason) {
          fixture.vetoes.push({ id, reason });
          return !fixture.blocked;
        },
        onClose(reason) {
          fixture.closed.push({
            id, reason, hidden: node.hidden, open: fixture.panels[id].isOpen,
          });
        },
        ...extra,
      };
      return fixture.panels[id] = ConsoleDialogs.mountPanel(node, options);
    },
    child(kind = 'confirm', options = {}) {
      fixture.results[kind] = { pending: true, count: 0 };
      ConsoleDialogs[kind]({
        title: 'Nested action', message: 'Review this change', label: 'New name', value: 'unchanged',
        ...options,
      }).then(value => {
        fixture.results[kind] = { value, pending: false, count: fixture.results[kind].count + 1 };
      });
    },
  };
  for (const id of ['alpha', 'beta', 'gamma']) {
    fixture.nodes[id] = fixture.node(id);
    fixture.nodes[`${id}-heading`] = fixture.node(id).querySelector('header, h2');
  }
  fixture.node('alpha-close').addEventListener('click', () => { fixture.closeClicks++; });
  fixture.node('alpha-input').addEventListener('input', () => { fixture.changes++; });
  fixture.node('alpha-child').onclick = () => fixture.child();
  document.addEventListener('keydown', event => fixture.keys.push(event.key));
  window.addEventListener('unhandledrejection', event => fixture.unhandled.push(String(event.reason)));
  fixture.originalOrder = Array.from(fixture.node('host').children, node => node.id);
  fixture.mount('alpha', { size: 'lg' });
  fixture.mount('beta', { size: 'md' });
  fixture.mount('gamma', { size: 'xl' });
}

async function frames(page) {
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
}

async function open(page, id = 'alpha', focus) {
  assert.equal(await page.evaluate(({ id, focus }) => fixture.panels[id].open({
    focus: focus ? fixture.node(focus) : undefined,
  }), { id, focus }), true);
  await page.locator(`${PANEL}[data-panel-id="${id}"]`).waitFor({ state: 'visible' });
}

async function focused(page, id) {
  assert.equal(await page.evaluate(id => document.activeElement === fixture.node(id), id), true,
    `Focus should be on ${id}`);
}

async function locked(page, expected) {
  assert.deepEqual(await page.evaluate(() => [document.documentElement, document.body].map(node =>
    node.classList.contains('console-dialog-open') && getComputedStyle(node).overflow === 'hidden')),
  [expected, expected], 'The modal stack owns a shared scroll lock');
}

async function result(page, kind, value) {
  await page.waitForFunction(kind => fixture.results[kind]?.pending === false, kind);
  assert.deepEqual(await page.evaluate(kind => fixture.results[kind], kind),
    { value, pending: false, count: 1 });
}

test('mount is idempotent and preserves DOM nodes, IDs, handlers, labels and dispose positions', async page => {
  assert.deepEqual(await page.evaluate(() => ({
    frozen: Object.isFrozen(ConsoleDialogs),
    same: ConsoleDialogs.mountPanel(fixture.nodes.alpha) === fixture.panels.alpha,
    wrappers: document.querySelectorAll('.console-panel-dialog').length,
    bodyParent: fixture.nodes.alpha.parentElement.className,
    headingParent: fixture.nodes['alpha-heading'].parentElement.className,
    hidden: fixture.nodes.alpha.hidden,
    controls: fixture.panels.alpha.element.querySelectorAll('[data-panel-close]').length,
    label: fixture.node('alpha-input').labels[0].textContent,
    heading: fixture.panels.alpha.element.getAttribute('aria-labelledby'),
    title: fixture.panels.gamma.element.getAttribute('aria-labelledby'),
  })), {
    frozen: true, same: true, wrappers: 3, bodyParent: 'console-panel__body',
    headingParent: 'console-panel__header', hidden: true, controls: 1,
    label: 'Source name', heading: 'alpha-title', title: 'gamma-title',
  });
  await open(page);
  assert.equal(await page.getByRole('dialog', { name: 'Source settings', exact: true }).count(), 1);
  await page.locator('#alpha-input').fill('retained edit');
  await page.locator('#alpha-close').click();
  assert.deepEqual(await page.evaluate(() => [fixture.closeClicks, fixture.changes]), [1, 1]);
  await page.evaluate(() => {
    for (const panel of Object.values(fixture.panels)) { panel.dispose(); panel.dispose(); }
  });
  assert.deepEqual(await page.evaluate(() => Array.from(fixture.node('host').children, node => node.id)),
    await page.evaluate(() => fixture.originalOrder));
  assert.equal(await page.locator('.console-panel-dialog').count(), 0);
  assert.equal(await page.locator('#alpha-input').inputValue(), 'retained edit');
  assert.deepEqual(await page.evaluate(() => [
    fixture.nodes.alpha === fixture.node('alpha'),
    fixture.nodes['alpha-heading'] === fixture.node('alpha-header'),
    fixture.node('alpha-header').parentElement === fixture.nodes.alpha,
    fixture.nodes['beta-heading'].hasAttribute('id'),
    fixture.panels.alpha.open(),
  ]), [true, true, true, false, false]);
  await page.evaluate(() => fixture.mount('alpha'));
  await open(page);
  await page.locator('#alpha-close').click();
  assert.equal(await page.evaluate(() => fixture.closeClicks), 2);
  await page.evaluate(() => {
    fixture.mount('beta');
    fixture.panels.beta.element.id = 'modal-backdrop';
  });
  await open(page, 'beta');
  assert.equal(await page.locator('#modal-backdrop').evaluate(node => node.matches(':modal')), true);
  await page.evaluate(() => fixture.panels.beta.close());
});

test('duplicate open is a no-op, closes settle once and reopening preserves form values', async page => {
  await open(page);
  await page.locator('#alpha-notes').fill('Keep this draft');
  await page.evaluate(() => ConsoleDialogs.notify('status', 'Draft retained'));
  assert.equal(await page.evaluate(() => fixture.panels.alpha.open({
    focus: fixture.node('alpha-input'), returnFocus: fixture.node('other-opener'),
  })), true);
  await focused(page, 'alpha-notes');
  assert.equal(await page.locator('.console-panel__status:visible').textContent(), 'Draft retained');
  assert.deepEqual(await page.evaluate(() => [
    fixture.panels.alpha.close(), fixture.panels.alpha.close(), ConsoleDialogs.closePanels(),
  ]), [true, true, true]);
  await focused(page, 'opener');
  assert.deepEqual(await page.evaluate(() => fixture.closed),
    [{ id: 'alpha', reason: 'close', hidden: true, open: false }]);
  await open(page);
  assert.equal(await page.locator('#alpha-notes').inputValue(), 'Keep this draft');
  assert.equal(await page.locator('.console-panel__status:visible').count(), 0);
  assert.equal(await page.evaluate(() => ConsoleDialogs.isPanelOpen()), true);
  await page.evaluate(() => fixture.panels.alpha.close('success'));
  assert.equal(await page.evaluate(() => ConsoleDialogs.isPanelOpen()), false);
});

test('veto blocks close, Escape, navigation and replacement while success bypasses it', async page => {
  await open(page);
  await page.evaluate(() => { fixture.blocked = true; });
  assert.deepEqual(await page.evaluate(() => [
    fixture.panels.alpha.close(), ConsoleDialogs.closePanels(), fixture.panels.beta.open(),
  ]), [false, false, false]);
  await page.keyboard.press('Escape');
  await frames(page);
  assert.equal(await page.locator(PANEL).count(), 1);
  assert.equal(await page.locator(PANEL).getAttribute('data-panel-id'), 'alpha');
  assert.equal(await page.locator('.console-panel__error:visible').textContent(), 'Wait for upload');
  assert.deepEqual(await page.evaluate(() => fixture.vetoes.map(item => item.reason)),
    ['close', 'navigation', 'replace', 'escape']);
  assert.deepEqual(await page.evaluate(() => fixture.closed), []);
  await focused(page, 'alpha-input');
  await locked(page, true);
  await page.evaluate(() => {
    fixture.node('alpha-close').addEventListener('click', () => fixture.panels.alpha.close());
  });
  await page.locator('#alpha-close').click();
  assert.equal(await page.evaluate(() => fixture.vetoes.length), 5,
    'An existing close binding plus delegation runs the close veto once per click');
  assert.equal(await page.evaluate(() => fixture.panels.alpha.close('success')), true);
  await focused(page, 'opener');
  await locked(page, false);
});

test('replacement keeps opener context across panels and cancels a nested action only after veto passes', async page => {
  await open(page);
  await page.locator('#alpha-child').click();
  await page.locator(ACTION).waitFor();
  await page.evaluate(() => { fixture.blocked = true; });
  assert.equal(await page.evaluate(() => fixture.panels.beta.open()), false);
  assert.equal(await page.locator(ACTION).count(), 1);
  assert.equal(await page.evaluate(() => fixture.results.confirm.pending), true);
  assert.equal(await page.locator(`${ACTION} .console-dialog__submit`).evaluate(node =>
    node === document.activeElement), true);
  await page.evaluate(() => { fixture.blocked = false; });
  await open(page, 'beta');
  await result(page, 'confirm', false);
  await open(page, 'gamma');
  await page.evaluate(() => fixture.panels.gamma.close());
  await focused(page, 'opener');
  assert.deepEqual(await page.evaluate(() => fixture.closed.map(({ id, reason }) => [id, reason])),
    [['alpha', 'replace'], ['beta', 'replace'], ['gamma', 'close']]);
  assert.equal(await page.locator(PANEL).count(), 0);
  await locked(page, false);
  await page.evaluate(() => fixture.child('confirm'));
  await open(page, 'beta');
  await result(page, 'confirm', false);
  await page.evaluate(() => fixture.panels.beta.close());
  await focused(page, 'opener');
});

test('nested confirm and prompt trap focus and Escape closes only the top dialog', async page => {
  await open(page);
  await locked(page, true);
  await page.locator('#alpha-child').click();
  for (const key of ['Tab', 'Tab', 'Shift+Tab']) {
    await page.keyboard.press(key);
    assert.equal(await page.locator(ACTION).evaluate(node => node.contains(document.activeElement)), true);
  }
  // The backdrop belongs to the native modal, and clicking it cannot dismiss.
  await page.mouse.click(1, 1);
  assert.equal(await page.locator(ACTION).count(), 1);
  await page.keyboard.press('Escape');
  await result(page, 'confirm', false);
  await focused(page, 'alpha-child');
  assert.equal(await page.locator(PANEL).count(), 1);
  await locked(page, true);
  await page.evaluate(() => fixture.child('prompt'));
  await page.locator(`${ACTION} input`).fill('Changed in nested prompt');
  await page.keyboard.press('Enter');
  await result(page, 'prompt', 'Changed in nested prompt');
  await focused(page, 'alpha-child');
  await locked(page, true);
  await page.evaluate(() => fixture.child('confirm'));
  await page.locator(`${ACTION} .console-dialog__submit`).click();
  await result(page, 'confirm', true);
  await page.mouse.click(1, 1);
  assert.equal(await page.locator(PANEL).count(), 1);
  for (let index = 0; index < 8; index++) {
    await page.keyboard.press(index % 2 ? 'Shift+Tab' : 'Tab');
    assert.equal(await page.locator(PANEL).evaluate(node => node.contains(document.activeElement)), true);
  }
  await page.keyboard.press('Escape');
  await frames(page);
  assert.deepEqual(await page.evaluate(() => fixture.closed),
    [{ id: 'alpha', reason: 'escape', hidden: true, open: false }]);
  assert.deepEqual(await page.evaluate(() => fixture.keys), [], 'No modal keyboard event reaches page shortcuts');
  await focused(page, 'opener');
  await locked(page, false);
});

test('reset forces both layers closed and invalidates pending prompts without focusing private controls', async page => {
  await open(page);
  await page.locator('#alpha-notes').fill('Private draft');
  await page.evaluate(() => {
    fixture.blocked = true;
    fixture.privateFocus = 0;
    for (const id of ['opener', 'alpha-input', 'alpha-notes']) {
      fixture.node(id).addEventListener('focus', () => { fixture.privateFocus++; });
    }
    fixture.child('prompt', {
      onSubmit: () => new Promise((resolve, reject) => { fixture.gates.save = { resolve, reject }; }),
    });
  });
  await page.locator(`${ACTION} .console-dialog__submit`).click();
  await page.waitForFunction(() => !!fixture.gates.save);
  const before = await page.evaluate(() => fixture.privateFocus);
  await page.evaluate(() => { ConsoleDialogs.reset(); ConsoleDialogs.reset(); });
  await result(page, 'prompt', null);
  assert.equal(await page.locator('dialog[open]').count(), 0);
  assert.deepEqual(await page.evaluate(() => fixture.closed),
    [{ id: 'alpha', reason: 'reset', hidden: true, open: false }]);
  assert.equal(await page.evaluate(() => fixture.privateFocus), before);
  assert.equal(await page.locator('#alpha-notes').inputValue(), 'Private draft');
  await locked(page, false);
  await open(page, 'beta');
  await page.evaluate(() => fixture.gates.save.reject(new Error('Late save failure')));
  await frames(page);
  await focused(page, 'beta-input');
  assert.equal(await page.locator('.console-dialog__error:visible').count(), 0);
  await page.evaluate(() => ConsoleDialogs.reset());
  await page.evaluate(() => fixture.child('confirm'));
  await locked(page, true);
  await page.evaluate(() => ConsoleDialogs.reset());
  await result(page, 'confirm', false);
  await locked(page, false);
});

test('queued close events and bubbling descendant events cannot close a reopened panel', async page => {
  await open(page);
  assert.equal(await page.evaluate(() => {
    fixture.panels.alpha.close();
    return fixture.panels.alpha.open();
  }), true);
  await frames(page);
  assert.equal(await page.locator(PANEL).count(), 1);
  await page.evaluate(() => {
    fixture.panels.alpha.element.dispatchEvent(new Event('close'));
    fixture.node('alpha-input').dispatchEvent(new Event('close', { bubbles: true }));
    fixture.node('alpha-input').dispatchEvent(new Event('cancel', { bubbles: true, cancelable: true }));
  });
  assert.equal(await page.locator(PANEL).count(), 1);
  // External close plus synchronous reopen must also outlive the old task.
  assert.equal(await page.evaluate(() => {
    fixture.panels.alpha.element.close();
    return fixture.panels.alpha.open();
  }), true);
  await frames(page);
  assert.equal(await page.locator(PANEL).count(), 1);
  assert.equal(await page.evaluate(() => fixture.closed.length), 2);
  await page.evaluate(() => fixture.panels.alpha.element.close());
  await page.waitForFunction(() => !fixture.panels.alpha.isOpen);
  assert.equal(await page.evaluate(() => fixture.closed.length), 3);
  await locked(page, false);
});

test('a native close veto reinstates the same lifecycle and reset prevents callback reentry', async page => {
  await open(page);
  await page.evaluate(() => { fixture.blocked = true; fixture.panels.alpha.element.close(); });
  await page.waitForFunction(() => fixture.vetoes.length === 1 && fixture.panels.alpha.element.open);
  await focused(page, 'alpha-input');
  assert.deepEqual(await page.evaluate(() => fixture.closed), []);
  await locked(page, true);
  await page.locator('#alpha-child').click();
  await page.evaluate(() => fixture.panels.alpha.element.close());
  await page.waitForFunction(() => fixture.vetoes.length === 2);
  assert.equal(await page.locator(ACTION).evaluate(node => node.contains(document.activeElement)), true);
  await page.keyboard.press('Escape');
  await result(page, 'confirm', false);
  assert.equal(await page.locator(PANEL).count(), 1, 'The panel returns after its nested overlay finishes');
  await focused(page, 'alpha-child');
  assert.deepEqual(await page.evaluate(() => fixture.closed), []);
  await locked(page, true);
  await page.evaluate(() => {
    fixture.panels.alpha.dispose();
    fixture.mount('alpha', {
      onClose(reason) {
        fixture.closed.push({ id: 'alpha', reason, hidden: fixture.nodes.alpha.hidden,
          open: fixture.panels.alpha.isOpen });
        fixture.reentry = fixture.panels.beta.open();
        fixture.child('confirm');
      },
    });
  });
  await open(page);
  await page.evaluate(() => ConsoleDialogs.reset());
  await result(page, 'confirm', false);
  assert.equal(await page.evaluate(() => fixture.reentry), false);
  assert.equal(await page.locator('dialog[open]').count(), 0);
  await locked(page, false);
});

test('callback and focus reentry own the newest lifecycle and delegated close clicks cannot close it twice', async page => {
  await page.evaluate(() => {
    fixture.panels.alpha.dispose();
    let reopen = true;
    fixture.mount('alpha', {
      onClose(reason) {
        fixture.closed.push({ id: 'alpha', reason, hidden: fixture.nodes.alpha.hidden,
          open: fixture.panels.alpha.isOpen });
        if (reopen) { reopen = false; fixture.panels.alpha.open(); }
      },
    });
    // The parent's existing close binding runs before the manager's delegation.
    fixture.node('alpha-close').type = 'submit';
    fixture.node('alpha-close').setAttribute('form', 'alpha-form');
    fixture.node('alpha-form').method = 'dialog';
    fixture.node('alpha-close').addEventListener('click', () => fixture.panels.alpha.close());
  });
  await open(page);
  await page.locator('#alpha-close').click();
  await frames(page);
  assert.equal(await page.locator(PANEL).count(), 1);
  assert.deepEqual(await page.evaluate(() => fixture.closed),
    [{ id: 'alpha', reason: 'close', hidden: true, open: false }]);
  await focused(page, 'alpha-input');
  await page.evaluate(() => fixture.panels.alpha.close());
  await page.evaluate(() => {
    fixture.node('opener').addEventListener('focus', () => fixture.panels.beta.open(), { once: true });
  });
  await open(page);
  await page.evaluate(() => fixture.panels.alpha.close());
  await frames(page);
  assert.equal(await page.locator(PANEL).getAttribute('data-panel-id'), 'beta');
  await focused(page, 'beta-input');
  await page.evaluate(() => fixture.panels.beta.close());
});

test('replacement respects newer callback intent and synchronous initial-focus closure', async page => {
  await page.evaluate(() => {
    fixture.panels.alpha.dispose();
    fixture.mount('alpha', {
      onClose() { fixture.panels.gamma.open(); },
    });
  });
  await open(page);
  assert.equal(await page.evaluate(() => fixture.panels.beta.open()), false);
  assert.equal(await page.locator(PANEL).getAttribute('data-panel-id'), 'gamma');
  await focused(page, 'gamma-input');
  await page.evaluate(() => ConsoleDialogs.reset());
  await page.evaluate(() => {
    fixture.node('beta-input').addEventListener('focus', () => fixture.panels.beta.close(), { once: true });
  });
  assert.equal(await page.evaluate(() => fixture.panels.beta.open()), false);
  await frames(page);
  assert.equal(await page.locator(PANEL).count(), 0);
  assert.equal(await page.evaluate(() => fixture.nodes.beta.hidden), true);
  await locked(page, false);
});

test('notify writes text only, uses inline status and alert roles, and leaves input and nested focus intact', async page => {
  assert.equal(await page.evaluate(() => ConsoleDialogs.notify('error', 'No panel')), false);
  await open(page);
  const literal = '<img src="https://invalid.example/pixel"> Use another name';
  assert.equal(await page.evaluate(message => ConsoleDialogs.notify('error', message), literal), true);
  assert.equal(await page.locator('.console-panel__error:visible').getAttribute('role'), 'alert');
  assert.equal(await page.locator('.console-panel__error:visible').textContent(), literal);
  assert.equal(await page.locator('img').count(), 0);
  await focused(page, 'alpha-input');
  assert.equal(await page.locator('#alpha-input').inputValue(), 'original source');
  await page.evaluate(message => ConsoleDialogs.notify('err', message), literal);
  assert.equal(await page.locator('.console-panel__error:visible').textContent(), literal);
  await page.locator('#alpha-child').click();
  await page.evaluate(() => ConsoleDialogs.notify('ok', 'Saved settings'));
  assert.equal(await page.locator('.console-panel__status:visible').textContent(), 'Saved settings');
  assert.equal(await page.locator('.console-panel__status:visible').getAttribute('role'), 'status');
  assert.equal(await page.locator('.console-panel__error:visible').count(), 0);
  assert.equal(await page.locator(ACTION).evaluate(node => node.contains(document.activeElement)), true);
  await page.keyboard.press('Escape');
  await result(page, 'confirm', false);
  await page.evaluate(() => fixture.panels.alpha.close());
  assert.equal(await page.evaluate(() => ConsoleDialogs.notify('status', 'Closed')), false);
  await page.evaluate(() => { fixture.locale = 'ko'; });
  await open(page, 'beta');
  assert.equal(await page.locator(`${PANEL} .console-panel__close`).textContent(), '닫기');
  await page.evaluate(() => fixture.panels.beta.close());
});

test('focus uses explicit targets and valid live fallbacks after opener removal or disabling', async page => {
  assert.equal(await page.evaluate(() => {
    const opener = document.createElement('button');
    fixture.node('host').append(opener);
    opener.focus();
    opener.remove();
    return document.activeElement === document.body;
  }), true, 'Removing a focused row leaves document.body as the captured opener');
  await open(page);
  await page.evaluate(() => fixture.panels.alpha.close());
  await focused(page, 'fallback');
  assert.equal(await page.evaluate(() => fixture.panels.alpha.open({
    focus: fixture.node('alpha-notes'), returnFocus: fixture.node('other-opener'),
  })), true);
  await focused(page, 'alpha-notes');
  await page.evaluate(() => fixture.panels.alpha.close());
  await focused(page, 'other-opener');
  await open(page);
  await page.evaluate(() => fixture.node('other-opener').remove());
  await page.evaluate(() => fixture.panels.alpha.close());
  await focused(page, 'fallback');
  await page.locator('#opener').focus();
  await open(page);
  await page.evaluate(() => { fixture.node('opener').disabled = true; });
  await open(page, 'beta');
  await page.evaluate(() => fixture.panels.beta.close());
  await focused(page, 'fallback');
  await page.evaluate(() => { fixture.node('fallback').hidden = true; });
  await open(page);
  await page.evaluate(() => fixture.panels.alpha.close());
  assert.equal(await page.evaluate(() => document.activeElement.closest('.console-panel-dialog') === null), true);
});

test('mobile panels fit the viewport and retain a visible header while their body scrolls', async page => {
  await page.evaluate(() => {
    fixture.node('alpha-title').textContent = 'Source settings and configuration';
    for (let i = 0; i < 70; i++) {
      const paragraph = document.createElement('p');
      paragraph.textContent = `Row ${i} ${'LongContentName'.repeat(12)}`;
      fixture.node('alpha-long').append(paragraph);
    }
  });
  for (const viewport of [{ width: 320, height: 568 }, { width: 390, height: 844 }, { width: 667, height: 375 }]) {
    await page.setViewportSize(viewport);
    await open(page);
    await frames(page);
    const before = await page.locator(PANEL).evaluate(dialog => {
      const box = node => {
        const rect = node.getBoundingClientRect();
        return { top: rect.top, left: rect.left, bottom: rect.bottom, right: rect.right };
      };
      const body = dialog.querySelector('.console-panel__body');
      return {
        dialog: box(dialog), header: box(dialog.querySelector('.console-panel__header')),
        close: box(dialog.querySelector('[data-panel-close]')),
        width: innerWidth, height: innerHeight,
        horizontalOverflow: dialog.scrollWidth - dialog.clientWidth,
        documentOverflow: document.documentElement.scrollWidth - innerWidth,
        bodyOverflow: body.scrollHeight - body.clientHeight, bodyTabIndex: body.tabIndex,
      };
    });
    for (const name of ['dialog', 'header', 'close']) {
      assert.ok(before[name].left >= 0 && before[name].right <= before.width + 1,
        `${viewport.width}px ${name} fits horizontally: ${JSON.stringify(before)}`);
      assert.ok(before[name].top >= 0 && before[name].bottom <= before.height + 1,
        `${viewport.width}px ${name} fits vertically: ${JSON.stringify(before)}`);
    }
    assert.ok(before.horizontalOverflow <= 1 && before.documentOverflow <= 1);
    assert.ok(before.bodyOverflow > 100);
    assert.equal(before.bodyTabIndex, 0);
    const scroll = await page.evaluate(() => scrollY);
    await page.locator(`${PANEL} .console-panel__body`).focus();
    await page.keyboard.press('PageDown');
    await page.waitForFunction(() => document.querySelector('.console-panel-dialog[open] .console-panel__body').scrollTop > 0);
    const after = await page.locator(`${PANEL} .console-panel__header`).boundingBox();
    assert.equal(after.y, before.header.top, 'Body scrolling leaves the header fixed');
    assert.equal(await page.evaluate(() => scrollY), scroll);
    await page.locator(`${PANEL} .console-panel__body`).evaluate(node => { node.scrollTop = node.scrollHeight; });
    await page.locator('#alpha-close').click();
    await locked(page, false);
    await open(page);
    const reopened = await page.locator('#alpha-input').evaluate(node => {
      const input = node.getBoundingClientRect();
      const scrollBody = node.closest('.console-panel__body');
      const body = scrollBody.getBoundingClientRect();
      return {
        focused: document.activeElement === node,
        inputTop: input.top, inputBottom: input.bottom, bodyTop: body.top, bodyBottom: body.bottom,
        scrollTop: scrollBody.scrollTop,
      };
    });
    assert.equal(reopened.focused && reopened.inputTop >= reopened.bodyTop && reopened.inputBottom <= reopened.bodyBottom,
      true, `Reopening a long form makes its initial focus visible: ${JSON.stringify({ viewport, ...reopened })}`);
    await page.locator('#alpha-close').click();
  }
});

async function fixture(browser, assets) {
  const context = await browser.newContext({
    viewport: { width: 1280, height: 900 }, serviceWorkers: 'block', reducedMotion: 'reduce',
  });
  const errors = [];
  await context.route('**/*', route => {
    errors.push(`Unexpected network request: ${route.request().url()}`);
    return route.abort();
  });
  await context.routeWebSocket('**/*', socket => {
    errors.push(`Unexpected WebSocket: ${socket.url()}`);
    socket.close();
  });
  await context.setOffline(true);
  const page = await context.newPage();
  page.setDefaultTimeout(TIMEOUT);
  page.setDefaultNavigationTimeout(TIMEOUT);
  page.on('pageerror', error => errors.push(error.stack || String(error)));
  page.on('dialog', dialog => { errors.push(`Unexpected native ${dialog.type()}`); void dialog.dismiss(); });
  await page.setContent(HTML);
  await page.addStyleTag({ content: assets.css });
  await page.addScriptTag({ content: assets.js });
  await page.addScriptTag({ content: `(${installFixture.toString()})()` });
  await page.locator('#opener').focus();
  return { page, context, errors };
}

async function main() {
  const filter = process.argv[2];
  if (filter === '--list') {
    cases.forEach(item => console.log(item.name));
    return;
  }
  const selected = cases.filter(item => !filter || item.name.toLowerCase().includes(filter.toLowerCase()));
  assert.ok(selected.length, `No cases match ${filter}`);
  const assets = {
    js: fs.readFileSync(path.join(ROOT, 'console/dialogs.js'), 'utf8'),
    css: fs.readFileSync(path.join(ROOT, 'console/dialogs.css'), 'utf8'),
  };
  const { chromium } = require(PLAYWRIGHT);
  const browser = await chromium.launch({ channel: 'chrome', headless: true, timeout: TIMEOUT });
  const output = fs.mkdtempSync('/tmp/graphify-console-panels-');
  const results = [];
  try {
    for (const item of selected) {
      const started = Date.now();
      let current, timer;
      const report = { name: item.name, status: 'PASS' };
      try {
        current = await fixture(browser, assets);
        await Promise.race([
          item.run(current.page),
          new Promise((_, reject) => { timer = setTimeout(() => reject(new Error('Case exceeded 30s')), TIMEOUT); }),
        ]);
        await frames(current.page);
        assert.deepEqual(current.errors, []);
        assert.deepEqual(await current.page.evaluate(() => fixture.unhandled), []);
        assert.equal(await current.page.locator('dialog[open]').count(), 0, 'Each case closes its modal stack');
        await locked(current.page, false);
      } catch (error) {
        report.status = 'FAIL';
        report.error = error.stack || String(error);
        if (current) {
          await current.page.screenshot({ path: path.join(output, `failure-${results.length + 1}.png`) }).catch(() => {});
          report.pageErrors = current.errors;
        }
      } finally {
        clearTimeout(timer);
        if (current) await current.context.close();
        report.duration_ms = Date.now() - started;
        results.push(report);
      }
      console.log(`${report.status} ${item.name}`);
      if (report.error) console.error(report.error);
    }
  } finally {
    await browser.close();
  }
  const hash = text => crypto.createHash('sha256').update(text).digest('hex');
  const snapshots = Object.entries(assets).map(([extension, content]) => ({
    path: `console/dialogs.${extension}`, sha256: hash(content),
    unchanged: fs.readFileSync(path.join(ROOT, `console/dialogs.${extension}`), 'utf8') === content,
  }));
  fs.writeFileSync(path.join(output, 'results.json'), JSON.stringify({ results, assets: snapshots }, null, 2) + '\n');
  const passed = results.filter(item => item.status === 'PASS').length;
  console.log(`${passed}/${results.length} passed. Diagnostics: ${output}`);
  if (passed !== results.length || snapshots.some(item => !item.unchanged)) process.exitCode = 1;
}

if (require.main === module) main().catch(error => { console.error(error.stack || error); process.exitCode = 1; });
