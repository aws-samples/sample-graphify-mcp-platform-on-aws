/* Include dialogs.css and load this script before console action handlers.
 *
 * await ConsoleDialogs.confirm({ title, message, subject, danger, confirmLabel });
 * await ConsoleDialogs.prompt({ title, label, value, validate, onSubmit });
 *
 * validate(value) may return an error string (or false for a generic error).
 * undefined, null, true and "" accept the value. Async validators also work.
 * onSubmit(value) may return a promise; rejection stays inline for retry.
 * Values are returned exactly as entered. Callers own any normalization.
 * reset(), Cancel, Escape and replacement resolve false/null. They invalidate
 * pending callbacks but cannot undo a server operation already in progress.
 *
 * mountPanel(content, options) moves permanent content into a reusable modal.
 * Its controller owns visibility, not form values or application state.
 * A false close/open result means the requested transition was refused.
 */
(function () {
  'use strict';

  const COPY = {
    en: {
      confirm: 'Confirm',
      cancel: 'Cancel',
      save: 'Save',
      checking: 'Checking...',
      saving: 'Saving...',
      invalid: 'Check the value and try again.',
      failed: 'Could not save. Try again.',
      close: 'Close',
      panel: 'Panel',
      busy: 'Wait for the current action to finish',
    },
    ko: {
      confirm: '확인',
      cancel: '취소',
      save: '저장',
      checking: '확인 중...',
      saving: '저장 중...',
      invalid: '입력한 값을 확인해 주세요.',
      failed: '저장하지 못했습니다. 다시 시도해 주세요.',
      close: '닫기',
      panel: '패널',
      busy: '현재 작업이 끝날 때까지 기다려 주세요',
    },
  };

  let active = null;
  let activePanel = null;
  let closingPanel = null;
  let sequence = 0;
  let panelId = 0;
  let resetting = false;
  const panels = new WeakMap();
  const FOCUSABLE = 'a[href], area[href], button, input, select, textarea, summary, iframe, [tabindex], [contenteditable="true"]';

  function language() {
    // The console declares LANG with top-level let, not window.LANG. Read it
    // when opening, including when this script loads before that declaration.
    try {
      return typeof LANG !== 'undefined' && LANG === 'ko' ? 'ko' : 'en';
    } catch (_) {
      return 'en';
    }
  }

  function element(tag, className, text) {
    const node = document.createElement(tag);
    node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function focusable(node) {
    return node instanceof HTMLElement && node.isConnected && typeof node.focus === 'function'
      && node !== document.body && node !== document.documentElement
      && !node.matches(':disabled') && !node.closest('[hidden], [inert]')
      && node.getClientRects().length > 0
      && !['hidden', 'collapse'].includes(getComputedStyle(node).visibility);
  }

  function focus(node) {
    if (!focusable(node)) return false;
    node.focus({ preventScroll: true });
    return document.activeElement === node;
  }

  function focusFirst(candidates, valid, container) {
    for (const candidate of candidates) {
      if (!valid()) return;
      const node = typeof candidate === 'function' ? candidate() : candidate;
      if (!valid()) return;
      if (node && (!container || container.contains(node)) && focus(node)) return;
    }
  }

  function syncScrollLock() {
    const locked = !!active || !!activePanel;
    document.documentElement.classList.toggle('console-dialog-open', locked);
    document.body.classList.toggle('console-dialog-open', locked);
  }

  function closeNative(state) {
    if (!state.dialog.open) return;
    // Native close() restores focus synchronously, before onClose can remove
    // private UI or start another modal. Suppress that one implicit restoration;
    // the manager restores a validated target after its lifecycle is complete.
    const opener = state.nativeReturnFocus;
    const suppress = opener instanceof HTMLElement && opener.isConnected && !opener.inert
      && opener !== document.body && opener !== document.documentElement && !opener.contains(state.dialog);
    if (suppress) opener.inert = true;
    try {
      state.dialog.close();
    } finally {
      if (suppress) opener.inert = false;
    }
  }

  function current(state) {
    return active === state && !state.done;
  }

  function finish(state, value, restoreFocus = true) {
    if (!state || state.done) return;
    const request = sequence;
    const panel = activePanel;
    state.done = true;
    if (state.resizeObserver) state.resizeObserver.disconnect();
    if (active === state) active = null;
    // Native close events are queued. Each request uses its own node so an old
    // close event cannot accidentally cancel its replacement.
    closeNative(state);
    state.dialog.remove();
    syncScrollLock();
    try {
      if (panel && panel.awaitingOverlay && panel.isOpen && activePanel === panel
        && !active && sequence === request && !resetting) showPanel(panel, state.returnFocus);
      if (restoreFocus && !resetting) {
        focusFirst([state.returnFocus, ...(panel ? panelFocusTargets(panel) : [])],
          () => sequence === request && !active && activePanel === panel,
          panel && panel.dialog);
      }
    } finally {
      state.resolve(value);
    }
  }

  function updateScrollAccess(state) {
    if (!current(state) || !state.dialog.open) return;
    const scrollable = state.body.scrollHeight > state.body.clientHeight + 1;
    state.body.tabIndex = scrollable ? 0 : -1;
    if (scrollable) {
      state.body.setAttribute('role', 'region');
      state.body.setAttribute('aria-labelledby', state.dialog.getAttribute('aria-labelledby'));
    } else {
      state.body.removeAttribute('role');
      state.body.removeAttribute('aria-labelledby');
    }
  }

  function clearError(state) {
    state.error.textContent = '';
    state.error.hidden = true;
    if (state.input) {
      state.input.removeAttribute('aria-invalid');
      if (state.descriptionId) state.input.setAttribute('aria-describedby', state.descriptionId);
      else state.input.removeAttribute('aria-describedby');
    }
    updateScrollAccess(state);
  }

  function showError(state, message, invalid) {
    state.error.hidden = false;
    state.error.textContent = message;
    if (invalid) state.input.setAttribute('aria-invalid', 'true');
    state.input.setAttribute('aria-describedby', [state.descriptionId, state.error.id].filter(Boolean).join(' '));
    focus(state.input);
    // Keep long errors reachable inside the independently scrolling body.
    state.error.scrollIntoView({ block: 'nearest' });
    updateScrollAccess(state);
  }

  function errorText(error, fallback) {
    if (typeof error === 'string' && error.trim()) return error;
    if (error && typeof error.message === 'string' && error.message.trim()) return error.message;
    return fallback;
  }

  function setBusy(state, busy, message) {
    const moveFocus = busy && document.activeElement === state.submit;
    state.busy = busy;
    state.dialog.setAttribute('aria-busy', String(busy));
    state.submit.disabled = busy;
    // Readonly preserves focus and selection while preventing the submitted
    // value from changing. Cancellation stays available during pending work.
    state.input.readOnly = busy;
    if (moveFocus) focus(state.input);
    state.status.textContent = busy ? message : '';
    state.status.hidden = !busy;
    updateScrollAccess(state);
  }

  async function submitPrompt(state) {
    if (!current(state) || state.busy || state.composing) return;
    const value = state.input.value;
    clearError(state);
    let validating = true;
    setBusy(state, true, state.copy.checking);
    try {
      if (state.validate) {
        const result = await state.validate(value);
        if (!current(state)) return;
        if (result !== undefined && result !== null && result !== true && result !== '') {
          setBusy(state, false);
          showError(state, typeof result === 'string' ? result : state.copy.invalid, true);
          return;
        }
      }
      if (!current(state)) return;
      validating = false;
      if (state.onSubmit) {
        setBusy(state, true, state.copy.saving);
        await state.onSubmit(value);
        if (!current(state)) return;
      }
      finish(state, value);
    } catch (error) {
      if (!current(state)) return;
      setBusy(state, false);
      showError(state, errorText(error, validating ? state.copy.invalid : state.copy.failed), validating);
    }
  }

  function open(kind, options) {
    if (resetting) return Promise.resolve(kind === 'prompt' ? null : false);
    const request = ++sequence;
    const previous = active;
    const focused = document.activeElement;
    const returnFocus = previous && previous.dialog.contains(focused) ? previous.returnFocus : focused;
    finish(previous, previous && previous.cancelValue, false);
    // Closing restores native focus synchronously. If a focus handler opens a
    // newer dialog or resets the UI, that newer intent owns the active slot.
    if (request !== sequence) return Promise.resolve(kind === 'prompt' ? null : false);

    return new Promise((resolve, reject) => {
      const lang = language();
      const copy = COPY[lang];
      const prefix = `console-dialog-${request}`;
      const isPrompt = kind === 'prompt';
      const dialog = element('dialog', 'console-dialog');
      dialog.lang = lang;
      dialog.setAttribute('aria-modal', 'true');
      dialog.setAttribute('aria-labelledby', `${prefix}-title`);
      const form = element('form', 'console-dialog__form');
      form.method = 'dialog';
      form.noValidate = true;
      const header = element('div', 'console-dialog__header');
      const title = element('h2', 'console-dialog__title', options.title);
      title.id = `${prefix}-title`;
      header.append(title);
      const body = element('div', 'console-dialog__body');
      let descriptionId = '';
      if (options.message !== undefined && options.message !== null && options.message !== '') {
        const message = element('p', 'console-dialog__message', options.message);
        message.id = `${prefix}-message`;
        descriptionId = message.id;
        dialog.setAttribute('aria-describedby', message.id);
        body.append(message);
      }
      if (!isPrompt && options.subject !== undefined && options.subject !== null && options.subject !== '') {
        const subject = element('p', 'console-dialog__subject', options.subject);
        subject.id = `${prefix}-subject`;
        dialog.setAttribute('aria-describedby', [descriptionId, subject.id].filter(Boolean).join(' '));
        body.append(subject);
      }
      let input = null;
      if (isPrompt) {
        const field = element('div', 'console-dialog__field');
        const label = element('label', 'console-dialog__label', options.label);
        label.htmlFor = `${prefix}-input`;
        input = element('input', 'console-dialog__input');
        input.id = label.htmlFor;
        input.name = 'value';
        input.type = 'text';
        input.autocomplete = 'off';
        input.value = options.value == null ? '' : String(options.value);
        input.placeholder = options.placeholder == null ? '' : String(options.placeholder);
        if (descriptionId) input.setAttribute('aria-describedby', descriptionId);
        field.append(label, input);
        body.append(field);
      }
      const error = element('p', 'console-dialog__error');
      error.id = `${prefix}-error`;
      error.setAttribute('role', 'alert');
      error.setAttribute('aria-atomic', 'true');
      error.hidden = true;
      const status = element('p', 'console-dialog__status');
      status.setAttribute('role', 'status');
      status.setAttribute('aria-atomic', 'true');
      status.hidden = true;
      body.append(error, status);
      const actions = element('div', 'console-dialog__actions');
      const cancel = element('button', 'console-dialog__cancel', copy.cancel);
      cancel.type = 'button';
      const submit = element('button', 'console-dialog__submit',
        isPrompt ? copy.save : (options.confirmLabel || copy.confirm));
      submit.type = 'submit';
      if (!isPrompt && options.danger) submit.classList.add('console-dialog__submit--danger');
      actions.append(cancel, submit);
      form.append(header, body, actions);
      dialog.append(form);
      const state = {
        dialog, body, input, error, status, submit, descriptionId, copy, returnFocus, resolve,
        panel: activePanel,
        cancelValue: isPrompt ? null : false,
        validate: typeof options.validate === 'function' ? options.validate : null,
        onSubmit: typeof options.onSubmit === 'function' ? options.onSubmit : null,
        busy: false, composing: false, done: false,
      };

      cancel.addEventListener('click', () => finish(state, state.cancelValue));
      dialog.addEventListener('cancel', event => {
        event.preventDefault();
        event.stopPropagation();
        if (event.target !== dialog) return;
        finish(state, state.cancelValue);
      });
      dialog.addEventListener('close', event => {
        event.stopPropagation();
        if (event.target === dialog) finish(state, state.cancelValue);
      });
      form.addEventListener('submit', event => {
        event.preventDefault();
        if (!current(state)) return;
        if (isPrompt) void submitPrompt(state);
        else finish(state, true);
      });
      if (input) {
        input.addEventListener('input', () => { if (!state.busy) clearError(state); });
        input.addEventListener('compositionstart', () => { state.composing = true; });
        input.addEventListener('compositionend', () => { state.composing = false; });
      }
      dialog.addEventListener('keydown', event => {
        // Do not let console-level shortcuts act on content behind the modal.
        event.stopPropagation();
        if (event.key === 'Enter' && (event.isComposing || state.composing || event.keyCode === 229)) {
          event.preventDefault();
        }
        if (event.key !== 'Tab' || event.altKey || event.ctrlKey || event.metaKey) return;
        const controls = [body.tabIndex === 0 ? body : null, input, cancel, submit]
          .filter(node => node && !node.disabled && node.getClientRects().length);
        const index = controls.indexOf(document.activeElement);
        const next = index < 0 ? (event.shiftKey ? controls.length - 1 : 0)
          : (index + (event.shiftKey ? -1 : 1) + controls.length) % controls.length;
        event.preventDefault();
        focus(controls[next]);
      });

      active = state;
      try {
        document.body.append(dialog);
        state.nativeReturnFocus = document.activeElement;
        syncScrollLock();
        dialog.showModal();
        if (!current(state)) return;
        updateScrollAccess(state);
        if (typeof ResizeObserver !== 'undefined') {
          state.resizeObserver = new ResizeObserver(() => updateScrollAccess(state));
          state.resizeObserver.observe(body);
        }
        const initialFocus = input || (options.danger ? cancel : submit);
        focus(initialFocus);
        if (input && current(state)) input.select();
      } catch (error) {
        state.done = true;
        if (state.resizeObserver) state.resizeObserver.disconnect();
        if (active === state) active = null;
        closeNative(state);
        dialog.remove();
        syncScrollLock();
        if (!active && sequence === request && !resetting) focus(returnFocus);
        reject(error);
      }
    });
  }

  function panelControls(container) {
    return Array.from(container.querySelectorAll(FOCUSABLE))
      .filter(node => node.tabIndex >= 0 && focusable(node))
      .sort((a, b) => (a.tabIndex || Infinity) - (b.tabIndex || Infinity));
  }

  function panelFocusTargets(state, preferred) {
    return [preferred, state.options.initialFocus, () => state.content.querySelector('[autofocus]'),
      () => panelControls(state.content)[0], () => panelControls(state.header)[0], state.body];
  }

  function updatePanelScrollAccess(state) {
    if (!state.isOpen || !state.dialog.open) return;
    state.body.tabIndex = state.body.scrollHeight > state.body.clientHeight + 1 ? 0 : -1;
  }

  function revealPanelFocus(state) {
    const node = document.activeElement;
    if (!state.isOpen || activePanel !== state || active || !state.body.contains(node) || node === state.body) return;
    const viewport = state.body.getBoundingClientRect();
    const target = node.getBoundingClientRect();
    const inset = Math.min(8, Math.max(0, (viewport.height - target.height) / 2));
    // Leave room for the focus outline and round toward visibility. scrollTop
    // can round to whole pixels while layout coordinates remain fractional.
    if (target.top < viewport.top + inset) {
      state.body.scrollTop = Math.floor(state.body.scrollTop + target.top - viewport.top - inset);
    } else if (target.bottom > viewport.bottom - inset) {
      state.body.scrollTop = Math.ceil(state.body.scrollTop + target.bottom - viewport.bottom + inset);
    }
  }

  function panelNotice(state, kind, message, deferred = false) {
    if (!state || !state.isOpen || (!state.dialog.open && !deferred)) return false;
    const target = kind === 'error' || kind === 'err' ? state.error : state.status;
    state.error.hidden = true;
    state.status.hidden = true;
    state.error.textContent = '';
    state.status.textContent = '';
    target.textContent = message == null ? '' : String(message);
    target.hidden = !target.textContent;
    updatePanelScrollAccess(state);
    // Do not move focus or scroll the parent while a confirmation is above it.
    if (!active && !target.hidden) target.scrollIntoView({ block: 'nearest' });
    return true;
  }

  function showPanel(state, preferred, resetScroll = false) {
    if (!state.isOpen || activePanel !== state || resetting || active) return;
    const version = state.version;
    const request = sequence;
    state.awaitingOverlay = false;
    state.nativeReturnFocus = document.activeElement;
    state.dialog.showModal();
    const valid = () => state.isOpen && state.version === version && sequence === request
      && activePanel === state && !active && !resetting;
    if (!valid()) return;
    // A display:none dialog has no scrolling box. Reset only after showModal,
    // before revealing the requested field in this new lifecycle.
    if (resetScroll) state.body.scrollTop = 0;
    updatePanelScrollAccess(state);
    focusFirst(panelFocusTargets(state, preferred), valid, state.dialog);
    if (valid()) revealPanelFocus(state);
  }

  function closePanel(state, reason = 'close', restoreFocus = true) {
    // Closing an already closed panel is a successful no-op, like closePanels.
    if (!state || !state.isOpen) return true;
    state.closeAttempts++;
    const version = state.version;
    const request = sequence;
    const forced = reason === 'success' || reason === 'reset';
    if (!forced && state.checkingClose) return false;
    if (!forced && typeof state.options.beforeClose === 'function') {
      let allowed;
      state.checkingClose = true;
      try {
        allowed = state.options.beforeClose(reason) !== false;
      } finally {
        state.checkingClose = false;
      }
      if (state.version !== version || sequence !== request) return !state.isOpen;
      if (!allowed) {
        // A direct native close() can also arrive here. Reinstate the modal if
        // its application vetoes that close, without starting a new lifecycle.
        if (!state.dialog.open) {
          // Reinstating the panel now would push it above a still-live native
          // confirmation. Finish that overlay first, then restore this layer.
          if (active) state.awaitingOverlay = true;
          else showPanel(state);
        }
        const message = state.options.busyMessage;
        panelNotice(state, 'error', typeof message === 'function' ? message() : COPY[language()].busy, true);
        return false;
      }
    }

    state.isOpen = false;
    state.awaitingOverlay = false;
    state.version++;
    state.content.hidden = true;
    if (activePanel === state) activePanel = null;
    if (state.resizeObserver) state.resizeObserver.disconnect();
    // Navigation/replacement must not leave a detached confirmation context.
    if (active && active.panel === state) finish(active, active.cancelValue, false);
    closeNative(state);
    syncScrollLock();
    const closedVersion = state.version;
    const focused = document.activeElement;
    const focusedInPanel = state.dialog.contains(focused);
    const previousClosing = closingPanel;
    closingPanel = state;
    try {
      if (typeof state.options.onClose === 'function') state.options.onClose(reason);
    } finally {
      closingPanel = previousClosing;
      if (restoreFocus && reason !== 'reset' && !resetting) {
        const canRestore = () => sequence === request && state.version === closedVersion && !active && !activePanel
          && (document.activeElement === focused || (focusedInPanel && !focused.isConnected
            && document.activeElement === document.body));
        focusFirst([state.returnFocus, state.options.returnFocus, state.returnFallback], canRestore);
        // If every opener has disappeared or become unavailable, do not leave
        // keyboard focus on a retained header in a closed, hidden dialog.
        if (canRestore() && state.dialog.contains(document.activeElement)) document.activeElement.blur();
      }
    }
    return true;
  }

  function mountPanel(content, options = {}) {
    if (!(content instanceof HTMLElement)) throw new TypeError('Panel content must be an HTMLElement');
    if (panels.has(content)) return panels.get(content).controller;
    const prefix = `console-panel-${++panelId}`;
    const placeholder = document.createComment(prefix);
    const originalHidden = content.hidden;
    content.before(placeholder);
    const dialog = element('dialog', 'console-dialog console-panel-dialog');
    dialog.dataset.panelId = content.id;
    dialog.dataset.size = ['md', 'lg', 'xl'].includes(options.size) ? options.size : 'lg';
    dialog.setAttribute('aria-modal', 'true');
    const header = element('div', 'console-panel__header');
    const body = element('div', 'console-panel__body');
    body.tabIndex = -1;
    body.setAttribute('role', 'region');
    const heading = options.heading || content.querySelector('header, h1, h2, h3, h4, h5, h6')
      || element('h2', 'console-dialog__title', content.getAttribute('aria-label') || COPY[language()].panel);
    if (!(heading instanceof HTMLElement) || heading === content || heading.contains(content)) {
      placeholder.remove();
      throw new TypeError('Panel heading must be a heading element separate from the content');
    }
    const headingPlaceholder = document.createComment(`${prefix}-heading`);
    heading.before(headingPlaceholder);
    const requestedTitle = options.titleId && document.getElementById(options.titleId);
    let title = requestedTitle && heading.contains(requestedTitle) ? requestedTitle
      : heading.matches('h1, h2, h3, h4, h5, h6') ? heading
        : heading.querySelector('h1, h2, h3, h4, h5, h6') || heading;
    // A legacy h2 may also contain its close button. Prefer its existing title
    // span so the dialog name does not include the close action's label.
    if (!requestedTitle && !title.id && title.querySelector('[data-panel-close]')) {
      title = title.querySelector(':scope > span:not(.dot)') || title;
    }
    const originalTitleId = title.id;
    if (!title.id) title.id = options.titleId || `${prefix}-title`;
    const assignedTitleId = title.id;
    dialog.setAttribute('aria-labelledby', title.id);
    body.setAttribute('aria-labelledby', title.id);
    header.append(heading);
    let closeButton = null;
    if (!heading.matches('[data-panel-close]') && !heading.querySelector('[data-panel-close]')) {
      closeButton = element('button', 'console-panel__close');
      closeButton.type = 'button';
      closeButton.dataset.panelClose = '';
      header.append(closeButton);
    }
    const error = element('p', 'console-dialog__error console-panel__error');
    error.setAttribute('role', 'alert');
    error.setAttribute('aria-atomic', 'true');
    error.hidden = true;
    const status = element('p', 'console-dialog__status console-panel__status');
    status.setAttribute('role', 'status');
    status.setAttribute('aria-atomic', 'true');
    status.hidden = true;
    content.hidden = true;
    body.append(error, status, content);
    dialog.append(header, body);
    document.body.append(dialog);
    const state = {
      dialog, header, body, content, error, status, options,
      isOpen: false, disposed: false, version: 0, checkingClose: false,
      closeAttempts: 0, awaitingOverlay: false,
      returnFocus: null, returnFallback: null, nativeReturnFocus: null,
    };
    if (typeof ResizeObserver !== 'undefined') {
      state.resizeObserver = new ResizeObserver(() => updatePanelScrollAccess(state));
    }

    const controller = Object.freeze({
      element: dialog,
      get isOpen() { return state.isOpen; },
      open({ focus: preferred, returnFocus } = {}) {
        if (state.disposed || resetting || state.checkingClose) return false;
        if (state.isOpen && dialog.open) return true;
        const request = ++sequence;
        const previous = activePanel;
        const context = previous || closingPanel;
        const focused = document.activeElement;
        const inherit = context && (context.dialog.contains(focused)
          || (context === closingPanel && focused === document.body)
          || (active && active.panel === context && active.dialog.contains(focused)));
        const opener = returnFocus || (inherit ? context.returnFocus
          : active && active.dialog.contains(focused) ? active.returnFocus : focused);
        const fallback = inherit ? context.options.returnFocus || context.returnFallback : null;
        if (previous && !closePanel(previous, 'replace', false)) return false;
        if (request !== sequence || state.disposed || resetting) return false;
        if (active) finish(active, active.cancelValue, false);
        if (request !== sequence || state.disposed || resetting) return false;
        state.returnFocus = opener;
        state.returnFallback = fallback;
        state.isOpen = true;
        state.awaitingOverlay = false;
        state.version++;
        activePanel = state;
        content.hidden = false;
        dialog.lang = language();
        if (closeButton) closeButton.textContent = typeof options.closeLabel === 'function'
          ? options.closeLabel() : COPY[language()].close;
        if (request !== sequence || !state.isOpen || state.disposed) return false;
        error.hidden = true;
        status.hidden = true;
        error.textContent = '';
        status.textContent = '';
        syncScrollLock();
        const version = state.version;
        try {
          showPanel(state, preferred, true);
          if (state.isOpen && state.version === version && state.resizeObserver) {
            state.resizeObserver.observe(body);
            state.resizeObserver.observe(content);
          }
        } catch (error) {
          if (state.version === version) closePanel(state, 'reset', false);
          throw error;
        }
        return state.isOpen && state.version === version && activePanel === state;
      },
      close(reason = 'close') { return closePanel(state, reason); },
      dispose() {
        if (state.disposed) return;
        state.disposed = true;
        try {
          closePanel(state, 'reset', false);
        } finally {
          if (state.resizeObserver) state.resizeObserver.disconnect();
          if (headingPlaceholder.parentNode) headingPlaceholder.replaceWith(heading);
          if (title.id === assignedTitleId && !originalTitleId) title.removeAttribute('id');
          if (placeholder.parentNode) placeholder.replaceWith(content);
          else content.remove();
          content.hidden = originalHidden;
          dialog.remove();
          panels.delete(content);
          syncScrollLock();
        }
      },
    });
    state.controller = controller;
    panels.set(content, state);

    // Existing button handlers run before delegation. A handler that closes
    // and reopens the panel must not have its new lifecycle closed by this click.
    const clickVersions = new WeakMap();
    dialog.addEventListener('click', event => {
      clickVersions.set(event, { version: state.version, attempts: state.closeAttempts });
    }, true);
    dialog.addEventListener('click', event => {
      const button = event.target instanceof Element && event.target.closest('[data-panel-close]');
      if (!button || !dialog.contains(button)) return;
      event.stopPropagation();
      const prevented = event.defaultPrevented;
      // Existing close controls may still be submit buttons associated with a
      // form. Their default submission must not close a reopened lifecycle.
      event.preventDefault();
      const click = clickVersions.get(event);
      if (prevented || active || !click || click.version !== state.version
        || click.attempts !== state.closeAttempts) return;
      closePanel(state);
    });
    dialog.addEventListener('cancel', event => {
      event.preventDefault();
      event.stopPropagation();
      if (event.target === dialog && !active && dialog.open) closePanel(state, 'escape');
    });
    dialog.addEventListener('close', event => {
      event.stopPropagation();
      // close events are queued tasks on this reusable node. An event from an
      // earlier lifetime is irrelevant whenever the dialog is open again.
      if (event.target === dialog && !dialog.open && state.isOpen) closePanel(state);
    });
    dialog.addEventListener('keydown', event => {
      event.stopPropagation();
      if (active || !state.isOpen || event.key !== 'Tab' || event.altKey || event.ctrlKey || event.metaKey) return;
      updatePanelScrollAccess(state);
      const controls = panelControls(dialog);
      const index = controls.indexOf(document.activeElement);
      const next = index < 0 ? (event.shiftKey ? controls.length - 1 : 0)
        : (index + (event.shiftKey ? -1 : 1) + controls.length) % controls.length;
      event.preventDefault();
      focus(controls[next] || body);
      revealPanelFocus(state);
    });
    return controller;
  }

  window.ConsoleDialogs = Object.freeze({
    confirm: options => open('confirm', options),
    prompt: options => open('prompt', options),
    mountPanel,
    closePanels: (reason = 'navigation') => closePanel(activePanel, reason),
    isPanelOpen: () => !!activePanel,
    isTopDialog: dialog => {
      const top = active?.dialog || activePanel?.dialog || null;
      return top === dialog && (!top || top.open);
    },
    notify: (kind, message) => panelNotice(activePanel, kind, message),
    reset: () => {
      sequence++;
      const wasResetting = resetting;
      resetting = true;
      try {
        if (active) finish(active, active.cancelValue, false);
        if (activePanel) closePanel(activePanel, 'reset', false);
      } finally {
        resetting = wasResetting;
        syncScrollLock();
      }
    },
  });
})();
