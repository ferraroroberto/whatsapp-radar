/* The editing sheet (#376, #377): a native <dialog> holding one draft for one
 * rule, opened from a Family or Settings row.
 *
 * Done commits the spec's payload (a straight close when nothing changed): on
 * success a toast, the sheet closes and the server's answer goes to `saved`;
 * on a rejection the server's message, which names the field, shows inside
 * the sheet and the sheet stays open. The close button, the backdrop and Esc
 * discard the draft. Values go in via textContent/value only.
 *
 * A spec: `title`, optional `help` (one line on top), `where` (the "Where this
 * lives" line, the only place a path appears), `readOnly` (no footer),
 * `draft()` (a fresh copy of the current values), `payload(draft)` (the
 * partial POST body), `render(body, draft)`, optional `after()` (content drawn
 * outside the draft), and for a confirm sheet `action(draft)` with
 * `doneLabel` / `danger` / `rule` (the footer line) in place of a payload. */

import { toast } from './api.js';
import { setSwitch, switchEl } from './_vendored/switch/switch.js';

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
}

/* `shell`: the dialog's nodes ({dialog, title, close, body, error, foot,
 * done}; title/foot may be null). `commit(payload)` resolves to the server's
 * answer, `saved(answer)` adopts it. */
export function sheetHost(shell, commit, saved) {
  const doneClass = shell.done.className;
  const ruleLine = shell.foot && shell.foot.querySelector('.sheet-rule');
  const ruleText = ruleLine ? ruleLine.textContent : '';
  let open = null;

  function showError(message) {
    shell.error.textContent = message;
    shell.error.hidden = !message;
    if (message) shell.error.scrollIntoView({ block: 'nearest' });
  }

  function render() {
    const spec = open.spec;
    shell.body.textContent = '';
    if (spec.help) shell.body.append(el('p', 'sheet-help', spec.help));
    spec.render(shell.body, open.draft);
    if (spec.where) shell.body.append(el('p', 'sheet-where', 'Where this lives: ' + spec.where));
    if (spec.after) spec.after();
  }

  function openSheet(spec) {
    const draft = spec.draft ? spec.draft() : {};
    const payload = spec.payload ? spec.payload(draft) : null;
    open = { spec: spec, draft: draft, baseline: JSON.stringify(payload) };
    if (shell.title) shell.title.textContent = spec.title;
    if (shell.foot) shell.foot.hidden = !!spec.readOnly;
    shell.done.textContent = spec.doneLabel || 'Done';
    shell.done.className = spec.danger ? 'button-tint danger detail-save-btn' : doneClass;
    if (ruleLine) ruleLine.textContent = spec.rule || ruleText;
    showError('');
    render();
    if (!shell.dialog.open) shell.dialog.showModal();
  }

  async function done() {
    if (!open) return;
    const current = open;
    const spec = current.spec;
    const payload = spec.payload ? spec.payload(current.draft) : null;
    if (!spec.action && JSON.stringify(payload) === current.baseline) {
      shell.dialog.close();
      return;
    }
    shell.done.disabled = true;
    try {
      const answer = spec.action ? await spec.action(current.draft) : await commit(payload);
      if (!spec.action) toast(spec.title + ' saved.');
      open = null;
      shell.dialog.close();
      if (!spec.action) saved(answer);
    } catch (exc) {
      showError(exc.message || String(exc));
    } finally {
      shell.done.disabled = false;
    }
  }

  shell.close.addEventListener('click', function () { shell.dialog.close(); });
  // Native <dialog>: a click on the element itself is the ::backdrop.
  shell.dialog.addEventListener('click', function (ev) {
    if (ev.target === shell.dialog) shell.dialog.close();
  });
  // Close, backdrop and Esc all end here, and all discard the draft. The event
  // is queued, so only drop a sheet that is actually gone.
  shell.dialog.addEventListener('close', function () {
    if (!shell.dialog.open) open = null;
  });
  shell.done.addEventListener('click', done);

  return {
    open: openSheet,
    // Redraw the open sheet over the draft it already holds.
    render: function () { if (open) render(); },
    isOpen: function (spec) { return !!open && (!spec || open.spec === spec); },
  };
}

// ------------------------------------------------------- sheet controls
// Shared by every sheet: label left, control right (the modal's .row
// contract), switches as the vendored .toggle, inputs at --control-h.

export { el };

// An inset row's value (Family #376, Settings #377): every `[data-value=key]`
// under `scope` reads `text`, toned by the status tone map when it needs
// attention.
export function setRowValue(scope, key, text, tone) {
  for (const node of scope.querySelectorAll('[data-value="' + key + '"]')) {
    node.textContent = text;
    if (tone) node.dataset.tone = tone; else delete node.dataset.tone;
  }
}

export function switchRow(labelText, on, onToggle) {
  const row = el('div', 'row');
  row.append(el('span', undefined, labelText), switchControl(labelText, on, onToggle));
  return row;
}

// A sheet draft never re-renders on toggle, so this adapter keeps the
// optimistic flip the row needs: reflect `next` on the button immediately,
// then hand it to the caller's draft-mutating `onToggle(next)`.
export function switchControl(labelText, on, onToggle) {
  return switchEl(on, {
    label: labelText,
    onToggle: function (next, btn) {
      setSwitch(btn, next);
      onToggle(next);
    },
  });
}

export function fieldRow(labelText, control) {
  const row = el('label', 'row');
  row.append(el('span', undefined, labelText), control);
  return row;
}

export function valueRow(labelText, value) {
  const row = el('div', 'row');
  row.append(el('span', undefined, labelText), el('span', 'sheet-value', value));
  return row;
}

export function buildSelect(options, current, labelText, onChange) {
  const sel = document.createElement('select');
  sel.className = 'select-native';
  sel.setAttribute('aria-label', labelText);
  let matched = false;
  for (const opt of options) {
    const o = document.createElement('option');
    o.value = opt.value;
    o.textContent = opt.label;
    if (opt.value === current) { o.selected = true; matched = true; }
    sel.append(o);
  }
  if (!matched && current) {
    const extra = document.createElement('option');
    extra.value = current;
    extra.textContent = current + ' (unrecognized)';
    extra.selected = true;
    sel.append(extra);
  }
  sel.addEventListener('change', function () { onChange(sel.value); });
  return sel;
}

export function textInput(value, placeholder, maxLength, onChange) {
  const input = document.createElement('input');
  input.type = 'text';
  input.className = 'input-native';
  input.value = value;
  if (placeholder) input.placeholder = placeholder;
  if (maxLength) input.maxLength = maxLength;
  input.addEventListener('input', function () { onChange(input.value); });
  return input;
}
