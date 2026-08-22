/* Family tab (#160, #167): the exact household schedule the two deterministic
 * scheduled checks — the daily calendar-conflict scan and the traffic-jam
 * alert — run against, editable from the phone.
 *
 * Run controls (enable toggles, cadence, "run now") and recent runs moved to
 * the Run tab in #164/#163 — this tab's only job now is the rules themselves:
 * on-duty weekday pattern, kids-home time, childcare windows, quiet hours,
 * significant-delay threshold, the train-commute leave-now exemption (#227),
 * and the daily-scan enable switch. Edits POST to
 * /api/family (the same endpoint the Run tab's traffic card reads/writes) and
 * land in config/local.json; the next scan/traffic-check run picks them up
 * with no restart. All values go in via textContent/value only. */

import { els, state } from './state.js';
import { fetchQuiet, jsonApi, toast } from './api.js';
import { fmtLocalDateTime } from './format.js';
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
let travelDraft = null;      // travel-block working copy (#268), same contract
let travelBaseline = '';

/* The tab has two independently-savable forms (the schedule and the
 * travel blocks), each in its own card with its own Save button — a control
 * whose Save sits in a different card reads as broken. They share one dirty
 * gate: every widget calls markDirty(), which re-evaluates each registered
 * form against its own baseline. Re-registered on every render because the
 * buttons are rebuilt with the DOM; the baselines are module state and only
 * move when the server's answer does. */
const forms = { rules: null, travel: null };

function registerForm(name, serialize, base, btn, status) {
  forms[name] = { serialize: serialize, baseline: base, btn: btn, status: status };
  btn.disabled = serialize() === base;
}

export async function fetchFamily() {
  await fetchQuiet('/api/family', adopt);
}

// The server's answer is the single source of truth for both drafts and both
// baselines — after a load or a save, nothing is dirty.
function adopt(data) {
  state.family = data;
  lastData = data;
  draft = toDraft(data);
  baseline = serializeDraft();
  travelDraft = toTravelDraft(data);
  travelBaseline = serializeTravel();
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
    skip_leave_now_for_train: !!d.traffic.skip_leave_now_for_train,
    ask_missing_locations: d.family.ask_missing_locations !== false,
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
    skip_leave_now_for_train: draft.skip_leave_now_for_train,
    ask_missing_locations: draft.ask_missing_locations,
  });
}

function toTravelDraft(d) {
  const tb = d.travel_blocks || {};
  return {
    enabled: !!tb.enabled,
    dry_run: tb.dry_run !== false,
    min_home_dwell_min: tb.min_home_dwell_min != null ? tb.min_home_dwell_min : 45,
    title_template: tb.title_template || '',
  };
}

function serializeTravel() {
  return JSON.stringify(travelDraft);
}

function markDirty() {
  for (const name of Object.keys(forms)) {
    const form = forms[name];
    if (!form) continue;
    if (form.btn) form.btn.disabled = form.serialize() === form.baseline;
    if (form.status) form.status.textContent = '';
  }
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
    box.append(warnNote('No Calendar token — run the bootstrap (see docs/calendar-bootstrap.md).'));
  }
  if (!d.traffic.api_key_set) {
    box.append(warnNote('No Routes API key configured — traffic checks will error.'));
  }
}

// A missing-credential notice. The glyph is the sprite's triangle-alert, not an
// emoji, so the tab carries one icon set (design.md Icons contract, #209).
function warnNote(text) {
  const p = el('p', 'muted small family-warn');
  // Static sprite markup only — never user content — so innerHTML is safe here.
  p.innerHTML = icon('triangle-alert');
  p.append(text);
  return p;
}

// -------------------------------------------------------------- widgets

function toggleRow(labelText, enabled, onToggle) {
  const row = el('div', 'family-control-row');
  row.append(el('span', 'family-control-label', labelText));
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.setAttribute('role', 'switch');
  btn.setAttribute('aria-label', labelText);
  setSwitch(btn, enabled);
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

// Same stacked label/control shape as timeField/numberField, for free text.
function labelledTextField(labelText, value, maxLength, onChange) {
  const label = el('label', 'stacked');
  label.append(el('span', undefined, labelText));
  const input = document.createElement('input');
  input.type = 'text';
  input.className = 'input-native';
  input.value = value;
  if (maxLength) input.maxLength = maxLength;
  input.addEventListener('change', function () { onChange(input.value); markDirty(); });
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
    skip_leave_now_for_train: draft.skip_leave_now_for_train,
    ask_missing_locations: draft.ask_missing_locations,
  };
  await postForm('rules', payload, 'Schedule saved.');
}

/* One POST path for both cards: disable the form's own button, send, adopt the
 * server's fresh payload (which re-renders and re-baselines both forms), and on
 * a rejection surface the server's message — which names the offending field —
 * in that card rather than as a toast alone. */
async function postForm(name, payload, okText) {
  const form = forms[name];
  if (form && form.btn) form.btn.disabled = true;
  try {
    const data = await jsonApi('/api/family', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    toast(okText, 'good');
    adopt(data);
  } catch (exc) {
    const message = exc.message || String(exc);
    toast(message, 'error');
    if (form && form.status) form.status.textContent = message;
    if (form && form.btn) form.btn.disabled = false;
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
  // Sits directly under the switch it qualifies rather than at the far end of
  // the form. #253: events with no location are always *assumed home* and
  // always kept in the decision trace — this only controls whether the daily
  // summary nags to fill them in, for a household where some never will be.
  target.append(toggleRow('Ask for missing locations', draft.ask_missing_locations, function (next) {
    draft.ask_missing_locations = next;
    markDirty();
  }));
  target.append(el('p', 'opt-hint', 'Off hides the "No location set" list from the summary. Events are still assumed to be at home and still recorded in the Audit trace.'));

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

  // #227: the leave-now nudge is a driving judgment, so a train-titled commute
  // has no use for it. Keywords themselves stay file-edited (config/local.json).
  target.append(fieldLabel('Leave-now alerts'));
  target.append(toggleRow('Skip train commutes', draft.skip_leave_now_for_train, function (next) {
    draft.skip_leave_now_for_train = next;
    markDirty();
  }));
  const trainWords = (lastData.traffic.train_keywords || []).join(', ');
  if (trainWords) {
    target.append(el('p', 'opt-hint', 'Matches event titles containing: ' + trainWords));
  }

  const save = el('button', 'run-btn', 'Save schedule');
  save.type = 'button';
  save.addEventListener('click', saveDraft);
  target.append(save);

  const status = el('p', 'muted small', '');
  target.append(status);
  registerForm('rules', serializeDraft, baseline, save, status);
}

// ------------------------------------------------- travel blocks (#268)

/* Three states, three shapes. `unknown` is never a quieter `writable` and
 * never a softer `not_writable`: the probe did not resolve, so nothing was
 * attempted and nobody may read the row as a pass. It gets its own colour,
 * its own glyph (never a tick, never a cross), its own word, and a dashed
 * border so it still reads apart in greyscale. Omitting the row entirely —
 * the other tempting shortcut — is exactly the failure this card exists to
 * fix. */
const CAP_LABEL = { writable: 'Writable', not_writable: 'Not writable', unknown: 'Unknown' };
const CAP_ICON = { writable: 'check', not_writable: 'x', unknown: 'triangle-alert' };
const CAP_NOTE = {
  writable: 'Blocks are created and removed on this calendar.',
  not_writable:
    'Shared read-only. Ask for "Make changes to events" — nothing is written until then.',
  unknown:
    'Not established — no sweep has reported on this calendar, so nothing was attempted. '
    + 'This is neither permission nor a refusal.',
};
const CAP_CLASS = {
  writable: 'tb-cap--writable',
  not_writable: 'tb-cap--not-writable',
  unknown: 'tb-cap--unknown',
};

function capState(value) {
  return CAP_LABEL[value] ? value : 'unknown';
}

function capabilityBadge(state) {
  const badge = el('span', 'tb-cap ' + CAP_CLASS[state]);
  // Static sprite markup only — never user content — so innerHTML is safe here.
  badge.innerHTML = icon(CAP_ICON[state]);
  badge.append(CAP_LABEL[state]);
  return badge;
}

/* The banner a glance has to land on: planning-only must never look like
 * writing. Off / dry run / live are three different words, glyphs and colours.
 * It reports the state the server currently holds — never the unsaved draft —
 * so it can never claim a mode that is not yet in force. */
function travelModeBanner(tb) {
  const mode = !tb.enabled ? 'off' : (tb.dry_run !== false ? 'dry' : 'live');
  const text = {
    off: 'Off — no plan is computed and no Routes call is made.',
    dry: 'Dry run — the plan is computed and logged. Nothing is written to any calendar.',
    live: 'Live — planned blocks are written to and deleted from the calendars below.',
  }[mode];
  const banner = el('p', 'tb-mode tb-mode--' + mode);
  banner.innerHTML = icon({ off: 'square', dry: 'eye', live: 'car' }[mode]);
  banner.append(text);
  return banner;
}

function renderCapability(box) {
  box.append(fieldLabel('Write access per calendar'));
  const rows = (lastData.travel_blocks || {}).write_capability || [];
  if (!rows.length) {
    box.append(el('p', 'muted small', 'No household calendars configured yet.'));
    return;
  }
  const list = el('div', 'tb-cap-list');
  for (const row of rows) {
    // Person/label only — a calendar id is an email address (privacy rule).
    const state = capState(row.state);
    const item = el('div', 'tb-cap-row');
    const text = el('div', 'tb-cap-text');
    text.append(el('span', 'tb-cap-name', row.label || row.person || '—'));
    text.append(el('span', 'tb-cap-note muted small', CAP_NOTE[state]));
    item.append(text);
    item.append(capabilityBadge(state));
    list.append(item);
  }
  box.append(list);
}

function sweepMode(sweep) {
  if (sweep.dry_run === true) return ' · planned only, nothing written';
  if (sweep.dry_run === false) return ' · live';
  return '';
}

function renderLastSweep(box, tb) {
  box.append(fieldLabel('Last sweep'));
  const sweep = tb.last_sweep;
  if (!sweep) {
    box.append(el('p', 'muted small', 'No calendar sync has recorded a sweep yet.'));
    return;
  }
  const dl = el('dl', 'family-rules');
  defRow(dl, 'When', fmtLocalDateTime(sweep.started_at, { withYear: false }));
  defRow(dl, 'Outcome', sweep.status + sweepMode(sweep));
  const counts = sweep.counts;
  if (counts) {
    defRow(dl, 'Plan', counts.desired + ' leg(s) · ' + counts.adds + ' add · '
      + counts.deletes + ' delete · ' + counts.keeps + ' kept · '
      + counts.protected + ' left alone');
    defRow(dl, 'Unpriced legs', String(counts.failures));
    defRow(dl, 'Routes calls', String(sweep.routes_calls));
  }
  if (sweep.apply) {
    const a = sweep.apply.counts;
    defRow(dl, 'Written', a.inserted + ' inserted · ' + a.deleted + ' deleted · '
      + a.skipped + ' skipped · ' + a.backups + ' backup(s) [' + sweep.apply.status + ']');
    if (sweep.apply.failures) {
      defRow(dl, 'Write failures', String(sweep.apply.failures));
    }
  }
  box.append(dl);
}

function renderTravel(box) {
  const target = box || els.familyTravelBlocks;
  target.textContent = '';
  const tb = lastData.travel_blocks || {};

  target.append(travelModeBanner(tb));

  target.append(toggleRow('Write travel blocks', travelDraft.enabled, function (next) {
    travelDraft.enabled = next;
    markDirty();
  }));
  target.append(toggleRow('Dry run', travelDraft.dry_run, function (next) {
    travelDraft.dry_run = next;
    markDirty();
  }));
  target.append(el('p', 'opt-hint', 'Leave dry run on until you have read a full plan in '
    + 'the Audit tab and confirmed nothing unexpected is listed for deletion. Every delete '
    + 'backs the event up to data/calendar_backups/ first.'));

  if (tb.enabled && !tb.write_token_present) {
    target.append(warnNote('No Calendar write token — nothing can be written. '
      + 'See docs/calendar-bootstrap.md.'));
  }

  target.append(fieldLabel('Minimum time at home'));
  const dwellGrid = el('div', 'cfg-fields');
  dwellGrid.append(numberField('Minutes', travelDraft.min_home_dwell_min, 0, 480, function (v) {
    travelDraft.min_home_dwell_min = v;
  }));
  target.append(dwellGrid);
  target.append(el('p', 'opt-hint', 'Below this much free time at home between two events, '
    + 'a direct hop is assumed and no drive-home block is written.'));

  target.append(fieldLabel('Block title'));
  const titleGrid = el('div', 'cfg-fields');
  titleGrid.append(labelledTextField('Shown on the calendar', travelDraft.title_template,
    tb.max_title_template || 60, function (v) { travelDraft.title_template = v; }));
  target.append(titleGrid);
  target.append(el('p', 'opt-hint', 'Never the destination — a shared calendar view must '
    + 'leak nothing about where anyone is going.'));

  renderCapability(target);
  renderLastSweep(target, tb);

  // Read-only, file-edited: widening the horizon past the days the daily scan
  // actually fetches is clamped, so it is changed next to family.unknown_scan_days.
  const dl = el('dl', 'family-rules');
  defRow(dl, 'Horizon', (tb.horizon_days || 0) + ' day(s) (config/local.json)');
  defRow(dl, 'Write token', tb.write_token_present ? 'present' : 'missing');
  target.append(dl);

  const save = el('button', 'run-btn', 'Save travel blocks');
  save.type = 'button';
  save.addEventListener('click', saveTravel);
  target.append(save);

  const status = el('p', 'muted small', '');
  target.append(status);
  registerForm('travel', serializeTravel, travelBaseline, save, status);
}

async function saveTravel() {
  await postForm('travel', {
    travel_blocks_enabled: travelDraft.enabled,
    travel_blocks_dry_run: travelDraft.dry_run,
    min_home_dwell_min: travelDraft.min_home_dwell_min,
    title_template: travelDraft.title_template,
  }, 'Travel blocks saved.');
}

// ---------------------------------------------------------------- boot

function render() {
  els.familyReadOnly.textContent = '';
  renderReadOnly(els.familyReadOnly);
  renderEditable(els.familyEditable);
  renderTravel(els.familyTravelBlocks);
}

export function wireFamily() {
  // No boot-time wiring beyond fetch-on-activate; controls bind on render.
}

// ----------------------------------------------- Run-tab traffic card (#164)
// The enable toggle + cadence render on the Execution tab (`els.execTraffic*`)
// but the config they edit is this module's own /api/family — the traffic-jam
// insurance feature is a family/traffic domain concern that happens to be
// surfaced from the Run tab (#240). execution.js keeps only `runTraffic`, the
// one piece that genuinely belongs to its run queue (firing a one-off
// traffic-check run); everything here is read/render/patch of config state,
// sharing nothing with that queue.
function trafficWhen(iso) {
  return iso ? fmtLocalDateTime(iso, { withYear: false }) : 'never';
}

function renderTraffic() {
  const t = state.execution.traffic;
  if (!t) return;
  setSwitch(els.execTrafficEnabled, !!t.enabled);
  // Don't clobber the field while the operator is typing in it.
  if (document.activeElement !== els.execTrafficCadence) {
    els.execTrafficCadence.value = t.cadence_min != null ? String(t.cadence_min) : '30';
  }
  els.execTrafficStatus.textContent =
    'Last check: ' + trafficWhen(t.last_check) + ' · Last alert: ' + trafficWhen(t.last_alert);
}

// Config comes from /api/family; throttled since it changes rarely (a POST also
// returns the fresh slice). `force` bypasses the throttle after a run/edit.
export async function fetchTraffic(force) {
  const ex = state.execution;
  if (!force && ex.trafficAt && Date.now() - ex.trafficAt < 12000) return;
  await fetchQuiet('/api/family', function (data) {
    ex.traffic = data.traffic || {};
    ex.trafficAt = Date.now();
    renderTraffic();
  });
}

export async function patchTraffic(body) {
  try {
    const data = await jsonApi('/api/family', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    state.execution.traffic = data.traffic || {};
    renderTraffic();
  } catch (exc) {
    toast(String(exc.message || exc), 'error');
    fetchTraffic(true);  // re-sync the controls to the server's truth
  }
}
