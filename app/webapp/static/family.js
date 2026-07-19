/* Family tab (#160, #167): the exact household schedule the two deterministic
 * scheduled checks — the daily calendar-conflict scan and the traffic-jam
 * alert — run against, editable from the phone.
 *
 * Run controls (enable toggles, cadence, "run now") and recent runs moved to
 * the Run tab in #164/#163 — this tab's only job now is the rules themselves:
 * on-duty weekday pattern, kids-home time, childcare windows, quiet hours,
 * significant-delay threshold, and the daily-scan enable switch. Edits POST to
 * /api/family (the same endpoint the Run tab's traffic card reads/writes) and
 * land in config/local.json; the next scan/traffic-check run picks them up
 * with no restart. All values go in via textContent/value only. */

import { els, state } from './state.js';
import { jsonApi, toast } from './api.js';
import { setSwitch } from './_vendored/switch/switch.js';
import { icon } from './_vendored/icons/icons.js';

const WEEKDAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
}

function defRow(dl, term, value) {
  dl.append(el('dt', 'muted small', term));
  dl.append(el('dd', 'small', value));
}

// --------------------------------------------------------------- state

let lastData = null;   // last /api/family payload (read-only display + option lists)
let draft = null;       // editable working copy, mutated in place by the widgets below
let baseline = '';      // serializeDraft() snapshot at load/save time (dirty gate)
let saveBtn = null;
let statusEl = null;

export async function fetchFamily() {
  let data;
  try {
    data = await jsonApi('/api/family');
  } catch (exc) {
    return; // 401 flips the login overlay in api.js; stay quiet otherwise.
  }
  state.family = data;
  lastData = data;
  draft = toDraft(data);
  baseline = serializeDraft();
  render();
}

function toDraft(d) {
  const responsible = {};
  const stored = d.family.responsible_by_weekday || {};
  for (const day of WEEKDAYS) responsible[day] = stored[day] || '';
  return {
    enabled: !!d.family.enabled,
    kids_home_time: d.family.kids_home_time || '',
    responsible: responsible,
    windows: (d.family.childcare_windows || []).map(function (w) {
      return {
        label: w.label || '',
        days: new Set(w.days || []),
        time: w.time || '',
        end_time: w.end_time || '',
      };
    }),
    quiet_start_hour: d.traffic.quiet_start_hour,
    quiet_end_hour: d.traffic.quiet_end_hour,
    significant_delay_min: d.traffic.significant_delay_min,
  };
}

function serializeDraft() {
  return JSON.stringify({
    enabled: draft.enabled,
    kids_home_time: draft.kids_home_time,
    responsible: draft.responsible,
    windows: draft.windows.map(function (w) {
      return { label: w.label, days: Array.from(w.days).sort(), time: w.time, end_time: w.end_time };
    }),
    quiet_start_hour: draft.quiet_start_hour,
    quiet_end_hour: draft.quiet_end_hour,
    significant_delay_min: draft.significant_delay_min,
  });
}

function markDirty() {
  if (saveBtn) saveBtn.disabled = serializeDraft() === baseline;
  if (statusEl) statusEl.textContent = '';
}

// ----------------------------------------------------------- read-only

function renderReadOnly(box) {
  const d = lastData;
  const dl = el('dl', 'family-rules');
  defRow(dl, 'Home', d.family.home_address || '—');
  defRow(dl, 'Calendars', d.calendars.map(function (c) { return c.label || c.person; }).join(', ') || '—');
  defRow(dl, 'Scan window', d.family.assessment_days + 'd conflict · ' + d.family.unknown_scan_days + 'd unknown pre-check');
  box.append(dl);

  if (!d.token_present) {
    box.append(el('p', 'muted small', '⚠️ No Calendar token — run the bootstrap (see docs/calendar-bootstrap.md).'));
  }
  if (!d.traffic.api_key_set) {
    box.append(el('p', 'muted small', '⚠️ No Routes API key configured — traffic checks will error.'));
  }
}

// -------------------------------------------------------------- widgets

function toggleRow(labelText, enabled, onToggle) {
  const row = el('div', 'family-control-row');
  row.append(el('span', 'family-control-label', labelText));
  const btn = el('button', 'toggle' + (enabled ? ' on' : ''));
  btn.type = 'button';
  btn.setAttribute('role', 'switch');
  btn.setAttribute('aria-checked', enabled ? 'true' : 'false');
  btn.setAttribute('aria-label', labelText);
  btn.innerHTML = '<span class="knob"></span><span class="toggle-label">' + (enabled ? 'ON' : 'OFF') + '</span>';
  btn.addEventListener('click', function () {
    const next = btn.getAttribute('aria-checked') !== 'true';
    setSwitch(btn, next);
    onToggle(next);
  });
  row.append(btn);
  return row;
}

function fieldLabel(text) { return el('p', 'field-label', text); }

function personOptions() {
  const opts = [{ value: '', label: '— nobody —' }];
  for (const c of lastData.calendars) opts.push({ value: c.person, label: c.label || c.person });
  return opts;
}

function buildSelect(options, current, onChange) {
  const sel = document.createElement('select');
  sel.className = 'select-native';
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
  sel.addEventListener('change', function () { onChange(sel.value); markDirty(); });
  return sel;
}

function timeField(labelText, value, onChange) {
  const label = el('label', 'stacked');
  label.append(el('span', undefined, labelText));
  const input = document.createElement('input');
  input.type = 'time';
  input.className = 'input-native';
  input.value = value;
  input.addEventListener('change', function () { onChange(input.value); markDirty(); });
  label.append(input);
  return label;
}

function numberField(labelText, value, min, max, onChange) {
  const label = el('label', 'stacked');
  label.append(el('span', undefined, labelText));
  const input = document.createElement('input');
  input.type = 'number';
  input.className = 'input-native';
  input.min = String(min);
  input.max = String(max);
  input.inputMode = 'numeric';
  input.value = String(value);
  input.addEventListener('change', function () {
    const v = parseInt(input.value, 10);
    if (Number.isFinite(v)) onChange(Math.max(min, Math.min(max, v)));
    markDirty();
  });
  label.append(input);
  return label;
}

function textField(placeholder, value, onChange) {
  const input = document.createElement('input');
  input.type = 'text';
  input.className = 'input-native';
  input.placeholder = placeholder;
  input.value = value;
  input.addEventListener('change', function () { onChange(input.value); markDirty(); });
  return input;
}

// ---------------------------------------------------- on-duty pattern

function renderResponsible(box) {
  box.append(fieldLabel('On duty'));
  const grid = el('div', 'cfg-fields');
  const opts = personOptions();
  for (const day of WEEKDAYS) {
    const label = el('label', 'stacked');
    label.append(el('span', undefined, day));
    label.append(buildSelect(opts, draft.responsible[day], function (v) { draft.responsible[day] = v; }));
    grid.append(label);
  }
  box.append(grid);
}

// ---------------------------------------------------- childcare windows

function windowRow(w, onRemove) {
  const card = el('div', 'family-window');

  const head = el('div', 'family-window-head');
  head.append(textField('Label (e.g. swim practice)', w.label, function (v) { w.label = v; }));
  const rm = document.createElement('button');
  rm.type = 'button';
  rm.className = 'icon-btn danger';
  rm.innerHTML = icon('trash-2');
  rm.setAttribute('aria-label', 'Remove childcare window');
  rm.title = 'Remove';
  rm.addEventListener('click', onRemove);
  head.append(rm);
  card.append(head);

  const chips = el('div', 'weekday-chips');
  for (const day of WEEKDAYS) {
    const chip = el('button', 'weekday-chip' + (w.days.has(day) ? ' on' : ''), day);
    chip.type = 'button';
    chip.setAttribute('aria-pressed', w.days.has(day) ? 'true' : 'false');
    chip.addEventListener('click', function () {
      if (w.days.has(day)) w.days.delete(day); else w.days.add(day);
      chip.classList.toggle('on');
      chip.setAttribute('aria-pressed', w.days.has(day) ? 'true' : 'false');
      markDirty();
    });
    chips.append(chip);
  }
  card.append(chips);

  const times = el('div', 'family-window-times');
  times.append(timeField('Start', w.time, function (v) { w.time = v; }));
  times.append(timeField('End (optional)', w.end_time, function (v) { w.end_time = v; }));
  card.append(times);

  return card;
}

function renderWindows(box) {
  box.append(fieldLabel('Childcare windows'));
  const list = el('div', 'family-windows');
  draft.windows.forEach(function (w) {
    list.append(windowRow(w, function () {
      draft.windows.splice(draft.windows.indexOf(w), 1);
      markDirty();
      renderEditable();
    }));
  });
  box.append(list);

  const add = el('button', 'ghost-btn', '+ Add childcare window');
  add.type = 'button';
  add.addEventListener('click', function () {
    draft.windows.push({ label: '', days: new Set(), time: '', end_time: '' });
    markDirty();
    renderEditable();
  });
  box.append(add);
}

// -------------------------------------------------------------- save

async function saveDraft() {
  const windows = draft.windows
    .filter(function (w) { return w.label.trim() || w.days.size || w.time; })
    .map(function (w) {
      return { label: w.label.trim(), days: Array.from(w.days), time: w.time, end_time: w.end_time };
    });
  const payload = {
    family_enabled: draft.enabled,
    kids_home_time: draft.kids_home_time,
    responsible_by_weekday: draft.responsible,
    childcare_windows: windows,
    quiet_start_hour: draft.quiet_start_hour,
    quiet_end_hour: draft.quiet_end_hour,
    significant_delay_min: draft.significant_delay_min,
  };
  saveBtn.disabled = true;
  try {
    const data = await jsonApi('/api/family', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    state.family = data;
    lastData = data;
    draft = toDraft(data);
    baseline = serializeDraft();
    toast('Schedule saved.', 'good');
    render();
  } catch (exc) {
    toast(exc.message || String(exc), 'error');
    statusEl.textContent = exc.message || String(exc);
    saveBtn.disabled = false;
  }
}

// -------------------------------------------------------------- editable

function renderEditable(box) {
  const target = box || els.familyEditable;
  target.textContent = '';

  target.append(toggleRow('Calendar sync', draft.enabled, function (next) {
    draft.enabled = next;
    markDirty();
  }));

  renderResponsible(target);

  target.append(fieldLabel('Kids home by'));
  const kidsGrid = el('div', 'cfg-fields');
  kidsGrid.append(timeField('Time', draft.kids_home_time, function (v) { draft.kids_home_time = v; }));
  target.append(kidsGrid);

  renderWindows(target);

  target.append(fieldLabel('Quiet hours'));
  const quietGrid = el('div', 'cfg-fields');
  quietGrid.append(numberField('From (hour)', draft.quiet_start_hour, 0, 23, function (v) { draft.quiet_start_hour = v; }));
  quietGrid.append(numberField('Until (hour)', draft.quiet_end_hour, 0, 23, function (v) { draft.quiet_end_hour = v; }));
  target.append(quietGrid);

  target.append(fieldLabel('Significant delay'));
  const delayGrid = el('div', 'cfg-fields');
  delayGrid.append(numberField('Minutes', draft.significant_delay_min, 0, 240, function (v) { draft.significant_delay_min = v; }));
  target.append(delayGrid);

  const save = el('button', 'run-btn', 'Save schedule');
  save.type = 'button';
  save.disabled = serializeDraft() === baseline;
  save.addEventListener('click', saveDraft);
  target.append(save);
  saveBtn = save;

  const status = el('p', 'muted small', '');
  target.append(status);
  statusEl = status;
}

// ---------------------------------------------------------------- boot

function render() {
  els.familyReadOnly.textContent = '';
  renderReadOnly(els.familyReadOnly);
  renderEditable(els.familyEditable);
}

export function wireFamily() {
  // No boot-time wiring beyond fetch-on-activate; controls bind on render.
}
