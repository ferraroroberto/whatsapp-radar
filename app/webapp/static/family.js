/* Family tab (#160, #167, #376): the exact household schedule the two
 * deterministic scheduled checks — the daily calendar-conflict scan and the
 * traffic-jam alert — run against, editable from the phone.
 *
 * Recent runs live in Activity (#373); this tab holds the rules themselves as
 * inset groups (#376): Household, Schedule, Calendar scan, Traffic checks and
 * Travel blocks. Each row shows its rule's current value; a row that edits
 * something opens a sheet holding the real control. A sheet keeps one draft
 * and commits it on Done through POST /api/family: a toast on success, the
 * server's field-naming message inside the sheet on a rejection (the sheet
 * stays open). Its close button, the backdrop and Esc discard the draft. The
 * three switches on the page apply on tap. Edits land in config/local.json;
 * the next scan/traffic-check run picks them up with no restart. All values go
 * in via textContent/value only. */

import { els, state } from './state.js';
import { fetchQuiet, jsonApi, toast } from './api.js';
import { fmtLocalDateTime, travelStatusLabel } from './format.js';
import { renderHeadStatus } from './_vendored/home-head/home-head.js';
import { setSwitch } from './_vendored/switch/switch.js';
import { icon } from './_vendored/icons/icons.js';
import {
  buildSelect, el, fieldRow, setRowValue, sheetHost, switchRow, textInput, valueRow,
} from './edit-sheet.js';

const WEEKDAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];

// The "Where this lives" line every rule sheet ends on (J-07: paths and config
// keys never sit in a label).
const LOCAL_CONFIG = 'config/local.json. The next run picks a change up, no restart.';

function defRow(dl, term, value) {
  dl.append(el('dt', 'muted small', term));
  dl.append(el('dd', 'small', value));
}

// --------------------------------------------------------------- state

let lastData = null;   // last /api/family payload: the page's values, every sheet's start

const RULE_SHELL = {
  dialog: els.familySheet,
  title: els.familySheetTitle,
  close: els.familySheetClose,
  body: els.familySheetBody,
  error: els.familySheetError,
  foot: els.familySheetFoot,
  done: els.familySheetDone,
};
// The travel sheet keeps its own nodes: its sweep controls update in place
// while a run polls, under whatever the operator is typing (#276).
const TRAVEL_SHELL = {
  dialog: els.familyTravelSheet,
  title: null,
  close: els.familyTravelClose,
  body: els.familyTravelBlocks,
  error: els.familyTravelError,
  foot: null,
  done: els.familyTravelDone,
};

// Both sheets commit through the one POST and adopt its answer.
const ruleSheet = sheetHost(RULE_SHELL, postFamily, adopt);
const travelSheet = sheetHost(TRAVEL_SHELL, postFamily, adopt);

export async function fetchFamily() {
  await fetchQuiet('/api/family', adopt);
}

// The server's answer is the single source of truth for the page and for the
// next sheet opened from it.
function adopt(data) {
  state.family = data;
  lastData = data;
  renderValues();
  renderHeader(data);
  renderTraffic();
}

// Family's header line (#374): a missing calendar token (nothing can be
// checked), else a missing Routes API key while something needs it, else the
// last calendar run's missing locations, else the rules.
function renderHeader(d) {
  const cal = d.last_calendar;
  const missing = (cal && cal.missing_locations) || 0;
  const routesNeeded = !!(d.traffic.enabled || (d.travel_blocks || {}).enabled);
  if ((d.calendars || []).length && !d.token_present) {
    renderHeadStatus(els.familyStatus, [{ text: 'Calendar token missing', tone: 'danger' }]);
  } else if (routesNeeded && !d.traffic.api_key_set) {
    renderHeadStatus(els.familyStatus, [{ text: 'Routes API key missing', tone: 'danger' }]);
  } else if (missing) {
    renderHeadStatus(els.familyStatus, [{
      text: missing + ' missing location' + (missing === 1 ? '' : 's'),
      tone: 'attention',
    }]);
  } else {
    renderHeadStatus(els.familyStatus, [], 'Rules in force');
  }
}

// ----------------------------------------------------------- page values

function setValue(key, text, tone) {
  setRowValue(els.paneFamily, key, text, tone);
}

function personLabel(person) {
  if (!person) return 'nobody';
  const cal = lastData.calendars.find(function (c) { return c.person === person; });
  return cal ? (cal.label || cal.person) : person;
}

// "Mon–Fri Parent A, Sat–Sun nobody": consecutive days with the same person
// fold into one span.
function dutySummary(byDay) {
  const runs = [];
  for (const day of WEEKDAYS) {
    const who = personLabel(byDay[day] || '');
    const last = runs[runs.length - 1];
    if (last && last.who === who) last.end = day;
    else runs.push({ start: day, end: day, who: who });
  }
  if (runs.length === 1) return runs[0].who.charAt(0).toUpperCase() + runs[0].who.slice(1) + ' every day';
  return runs.map(function (r) {
    return (r.start === r.end ? r.start : r.start + '–' + r.end) + ' ' + r.who;
  }).join(', ');
}

function hourLabel(h) {
  return h == null ? '—' : String(h).padStart(2, '0') + ':00';
}

function travelMode(tb) {
  return !tb.enabled ? 'off' : (tb.dry_run !== false ? 'dry' : 'live');
}

function renderValues() {
  const d = lastData;
  const f = d.family;
  const t = d.traffic;
  const tb = d.travel_blocks || {};
  const names = d.calendars.map(function (c) { return c.label || c.person; }).join(', ');
  const tokenMissing = d.calendars.length > 0 && !d.token_present;

  setValue('home', f.home_address || 'Not set');
  setValue('calendars', (tokenMissing ? 'Token missing · ' : '') + (names || 'None configured'),
    tokenMissing ? 'danger' : null);
  setValue('scan', f.assessment_days + 'd conflict · ' + f.unknown_scan_days + 'd unknown pre-check');

  setValue('duty', dutySummary(f.responsible_by_weekday || {}));
  setValue('kids', f.kids_home_time || 'Not set');
  const n = (f.childcare_windows || []).length;
  setValue('windows', n ? n + (n === 1 ? ' window' : ' windows') : 'None');
  setValue('quiet', hourLabel(t.quiet_start_hour) + '–' + hourLabel(t.quiet_end_hour));
  setSwitch(els.familySyncSwitch, !!f.enabled);
  setSwitch(els.familyAskSwitch, f.ask_missing_locations !== false);

  setValue('cadence', 'every ' + (t.cadence_min != null ? t.cadence_min : 30) + ' min');
  setValue('delay', t.significant_delay_min + ' min');
  setValue('train', t.skip_leave_now_for_train ? 'On' : 'Off');
  setValue('checkLive', t.api_key_set
    ? 'Live — sends an alert if a jam is found'
    : 'No Routes API key — checks will error', t.api_key_set ? null : 'danger');

  const mode = travelMode(tb);
  if (tb.enabled && !tb.write_token_present) {
    setValue('travel', 'No write token', 'attention');
  } else {
    setValue('travel', { off: 'Off', dry: 'Dry run', live: 'Live' }[mode], null);
  }
}

// A missing-credential notice inside a sheet. The glyph is the sprite's
// triangle-alert, not an emoji, so the tab carries one icon set (design.md
// Icons contract, #209).
function warnNote(text) {
  const p = el('p', 'muted small family-warn');
  // Static sprite markup only — never user content — so innerHTML is safe here.
  p.innerHTML = icon('triangle-alert');
  p.append(text);
  return p;
}

// -------------------------------------------------------------- sheets

async function postFamily(payload) {
  return jsonApi('/api/family', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
}

// A switch on the page applies on tap (#376 keeps the immediate patch).
export async function patchFamily(body) {
  try {
    adopt(await postFamily(body));
  } catch (exc) {
    toast(String(exc.message || exc), 'error');
    fetchFamily().catch(function () {});  // re-sync the controls to the server's truth
  }
}

// ------------------------------------------------------- sheet controls

function fieldLabel(text) { return el('p', 'field-label', text); }

function personOptions() {
  const opts = [{ value: '', label: 'Nobody' }];
  for (const c of lastData.calendars) opts.push({ value: c.person, label: c.label || c.person });
  return opts;
}

function timeInput(value, onChange) {
  const input = document.createElement('input');
  input.type = 'time';
  input.className = 'input-native';
  input.value = value;
  input.addEventListener('input', function () { onChange(input.value); });
  return input;
}

function numberInput(value, min, max, onChange) {
  const input = document.createElement('input');
  input.type = 'number';
  input.className = 'input-native';
  input.min = String(min);
  input.max = String(max);
  input.inputMode = 'numeric';
  input.value = String(value);
  const clamp = function () {
    const v = parseInt(input.value, 10);
    return Number.isFinite(v) ? Math.max(min, Math.min(max, v)) : null;
  };
  input.addEventListener('input', function () {
    const v = clamp();
    if (v !== null) onChange(v);
  });
  input.addEventListener('change', function () {
    const v = clamp();
    if (v !== null) input.value = String(v);
  });
  return input;
}

// ---------------------------------------------------- on-duty week (#376)

/* One picker per day: a segmented control while the people fit in 2-5
 * options, otherwise (or for a stored value no calendar matches) a select. */
function personPicker(day, current, onPick) {
  const opts = personOptions();
  const known = opts.some(function (o) { return o.value === current; });
  if (!known || opts.length < 2 || opts.length > 5) {
    return buildSelect(opts, current, day, onPick);
  }
  const group = el('div', 'segmented');
  group.setAttribute('role', 'group');
  group.setAttribute('aria-label', day);
  for (const opt of opts) {
    const btn = el('button', 'segmented-item', opt.label);
    btn.type = 'button';
    btn.title = opt.label;
    btn.setAttribute('aria-pressed', String(opt.value === current));
    btn.addEventListener('click', function () {
      for (const other of group.children) other.setAttribute('aria-pressed', String(other === btn));
      onPick(opt.value);
    });
    group.append(btn);
  }
  return group;
}

function renderDuty(body, draft) {
  for (const day of WEEKDAYS) {
    const row = el('div', 'row family-day');
    row.append(el('span', 'family-day-label', day));
    row.append(personPicker(day, draft[day], function (v) { draft[day] = v; }));
    body.append(row);
  }
}

// ---------------------------------------------------- childcare windows

function windowRow(w, onRemove) {
  const card = el('div', 'family-window');

  const head = el('div', 'family-window-head');
  head.append(textInput(w.label, 'Label (e.g. swim practice)', 0, function (v) { w.label = v; }));
  const rm = document.createElement('button');
  rm.type = 'button';
  rm.className = 'icon-button danger';
  rm.innerHTML = icon('trash-2');
  rm.setAttribute('aria-label', 'Remove childcare window');
  rm.title = 'Remove';
  rm.addEventListener('click', onRemove);
  head.append(rm);
  card.append(head);

  const chips = el('div', 'weekday-chips');
  for (const day of WEEKDAYS) {
    const chip = el('button', 'weekday-chip' + (w.days.includes(day) ? ' on' : ''), day);
    chip.type = 'button';
    chip.setAttribute('aria-pressed', w.days.includes(day) ? 'true' : 'false');
    chip.addEventListener('click', function () {
      const i = w.days.indexOf(day);
      if (i >= 0) w.days.splice(i, 1); else w.days.push(day);
      chip.classList.toggle('on', i < 0);
      chip.setAttribute('aria-pressed', i < 0 ? 'true' : 'false');
    });
    chips.append(chip);
  }
  card.append(chips);

  const times = el('div', 'family-window-times');
  for (const [labelText, key] of [['Start', 'time'], ['End (optional)', 'end_time']]) {
    const label = el('label', 'stacked');
    label.append(el('span', undefined, labelText));
    label.append(timeInput(w[key], function (v) { w[key] = v; }));
    times.append(label);
  }
  card.append(times);

  return card;
}

function renderWindows(body, draft) {
  const list = el('div', 'family-windows');
  draft.forEach(function (w) {
    list.append(windowRow(w, function () {
      draft.splice(draft.indexOf(w), 1);
      ruleSheet.render();
    }));
  });
  body.append(list);

  const add = el('button', 'button-ghost btn-touch btn-block', '+ Add childcare window');
  add.type = 'button';
  add.addEventListener('click', function () {
    draft.push({ label: '', days: [], time: '', end_time: '' });
    ruleSheet.render();
  });
  body.append(add);
}

function windowsPayload(draft) {
  return {
    childcare_windows: draft
      .filter(function (w) { return w.label.trim() || w.days.length || w.time; })
      .map(function (w) {
        return {
          label: w.label.trim(),
          days: WEEKDAYS.filter(function (d) { return w.days.includes(d); }),
          time: w.time,
          end_time: w.end_time,
        };
      }),
  };
}

// --------------------------------------------------------- rule sheets

const SCHEDULE_HELP = 'Part of the household schedule the daily calendar scan and the traffic check run against.';

/* Each spec: `draft()` copies the current values out of lastData, `render`
 * draws the controls over that draft, `payload` is the partial POST body. */
const SHEETS = {
  calendars: {
    title: 'Calendars',
    readOnly: true,
    help: 'The household calendars the daily scan reads.',
    where: 'config/local.json. The Calendar token comes from the bootstrap in docs/calendar-bootstrap.md.',
    draft: function () { return {}; },
    payload: function () { return {}; },
    render: function (body) {
      for (const c of lastData.calendars) {
        const row = el('div', 'row');
        row.append(el('span', undefined, c.label || c.person));
        body.append(row);
      }
      if (!lastData.calendars.length) body.append(el('p', 'muted small', 'No household calendars configured yet.'));
      body.append(valueRow('Calendar token', lastData.token_present ? 'present' : 'missing'));
      if (!lastData.token_present) body.append(warnNote('No Calendar token — nothing can be checked.'));
    },
  },
  duty: {
    title: "Who's on duty",
    help: SCHEDULE_HELP,
    where: LOCAL_CONFIG,
    draft: function () {
      const stored = lastData.family.responsible_by_weekday || {};
      const out = {};
      for (const day of WEEKDAYS) out[day] = stored[day] || '';
      return out;
    },
    payload: function (d) { return { responsible_by_weekday: d }; },
    render: renderDuty,
  },
  kids: {
    title: 'Kids home by',
    help: SCHEDULE_HELP,
    where: LOCAL_CONFIG,
    draft: function () { return { time: lastData.family.kids_home_time || '' }; },
    payload: function (d) { return { kids_home_time: d.time }; },
    render: function (body, d) {
      body.append(fieldRow('Time', timeInput(d.time, function (v) { d.time = v; })));
    },
  },
  windows: {
    title: 'Childcare windows',
    help: SCHEDULE_HELP,
    where: LOCAL_CONFIG,
    draft: function () {
      return (lastData.family.childcare_windows || []).map(function (w) {
        return { label: w.label || '', days: (w.days || []).slice(), time: w.time || '', end_time: w.end_time || '' };
      });
    },
    payload: windowsPayload,
    render: renderWindows,
  },
  quiet: {
    title: 'Quiet hours',
    help: 'No traffic alert is sent between these hours.',
    where: LOCAL_CONFIG,
    draft: function () {
      return { from: lastData.traffic.quiet_start_hour, until: lastData.traffic.quiet_end_hour };
    },
    payload: function (d) { return { quiet_start_hour: d.from, quiet_end_hour: d.until }; },
    render: function (body, d) {
      body.append(fieldRow('From (hour)', numberInput(d.from, 0, 23, function (v) { d.from = v; })));
      body.append(fieldRow('Until (hour)', numberInput(d.until, 0, 23, function (v) { d.until = v; })));
    },
  },
  sync: {
    title: 'Calendar sync',
    help: 'Turns the daily calendar scan on or off.',
    where: LOCAL_CONFIG,
    draft: function () { return { on: !!lastData.family.enabled }; },
    payload: function (d) { return { family_enabled: d.on }; },
    render: function (body, d) {
      body.append(switchRow('Calendar sync', d.on, function (v) { d.on = v; }));
    },
  },
  ask: {
    title: 'Ask for missing locations',
    // #253: events with no location are always *assumed home* and always kept
    // in the decision trace — this only controls whether the summary nags.
    help: 'Off hides the "No location set" list from the summary; those events are still assumed to be at home and still recorded in Activity.',
    where: LOCAL_CONFIG,
    draft: function () { return { on: lastData.family.ask_missing_locations !== false }; },
    payload: function (d) { return { ask_missing_locations: d.on }; },
    render: function (body, d) {
      body.append(switchRow('Ask for missing locations', d.on, function (v) { d.on = v; }));
    },
  },
  traffic: {
    title: 'Traffic checks',
    help: 'Checks your next commute and alerts only when a jam adds more than the significant delay.',
    where: 'config/local.json. The schedule is an App Launcher job; this sets whether it runs live checks.',
    draft: function () { return { on: !!lastData.traffic.enabled }; },
    payload: function (d) { return { traffic_enabled: d.on }; },
    render: function (body, d) {
      body.append(switchRow('Traffic checks', d.on, function (v) { d.on = v; }));
      if (!lastData.traffic.api_key_set) {
        body.append(warnNote('No Routes API key configured — traffic checks will error.'));
      }
    },
  },
  cadence: {
    title: 'Check every',
    help: 'How often the scheduled traffic check runs, in minutes.',
    // #170: the job is armed at a fixed high frequency and skips itself
    // in-process against this cadence, so a change needs no re-arm.
    where: 'config/local.json. App Launcher fires the job; it skips itself between checks.',
    draft: function () {
      return { min: lastData.traffic.cadence_min != null ? lastData.traffic.cadence_min : 30 };
    },
    payload: function (d) { return { cadence_min: d.min }; },
    render: function (body, d) {
      body.append(fieldRow('Minutes', numberInput(d.min, 5, 240, function (v) { d.min = v; })));
    },
  },
  delay: {
    title: 'Significant delay',
    help: 'A traffic alert is sent only when a jam adds more than this.',
    where: LOCAL_CONFIG,
    draft: function () { return { min: lastData.traffic.significant_delay_min }; },
    payload: function (d) { return { significant_delay_min: d.min }; },
    render: function (body, d) {
      body.append(fieldRow('Minutes', numberInput(d.min, 0, 240, function (v) { d.min = v; })));
    },
  },
  train: {
    title: 'Skip train commutes',
    // #227: the leave-now nudge is a driving judgment, so a train-titled
    // commute has no use for it. The keywords stay file-edited.
    help: 'The leave-now alert is a driving judgment, so it is skipped for a commute whose title names a train.',
    where: 'config/local.json, which is also where the keywords are edited.',
    draft: function () { return { on: !!lastData.traffic.skip_leave_now_for_train }; },
    payload: function (d) { return { skip_leave_now_for_train: d.on }; },
    render: function (body, d) {
      body.append(switchRow('Skip train commutes', d.on, function (v) { d.on = v; }));
      const words = (lastData.traffic.train_keywords || []).join(', ');
      body.append(valueRow('Matches titles with', words || '—'));
    },
  },
};

// ------------------------------------------------- travel blocks (#268)

/* Three states, three shapes. `unknown` is never a quieter `writable` and
 * never a softer `not_writable`: the probe did not resolve, so nothing was
 * attempted and nobody may read the row as a pass. It gets its own colour,
 * its own glyph (never a tick, never a cross), its own word, and a dashed
 * border so it still reads apart in greyscale. Omitting the row entirely —
 * the other tempting shortcut — is exactly the failure this sheet exists to
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
  const mode = travelMode(tb);
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

/* A gated sweep says so in words. `disabled`/`no_routes_api_key`/
 * `no_home_address` mean the sweep never got as far as computing anything, and
 * the server sends `dry_run: null` / `counts: null` precisely so this line
 * cannot be dressed up as a mode and a row of zeros. */
function outcomeText(sweep) {
  if (sweep.status === 'ok') return 'ran' + sweepMode(sweep);
  if (sweep.status === 'unknown') return 'unknown — the run recorded no sweep status';
  return 'gated — ' + travelStatusLabel(sweep.status) + ' · nothing was planned';
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
  defRow(dl, 'Outcome', outcomeText(sweep));
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
  if (!counts) {
    box.append(el('p', 'opt-hint', 'No plan was computed, so there are no counts to show. '
      + 'This is not a sweep that looked and found nothing to do.'));
  }
}

function renderTravel(body, draft) {
  const tb = lastData.travel_blocks || {};

  body.prepend(travelModeBanner(tb));

  body.append(switchRow('Write travel blocks', draft.enabled, function (next) {
    draft.enabled = next;
  }));
  body.append(switchRow('Dry run', draft.dry_run, function (next) {
    draft.dry_run = next;
  }));
  body.append(el('p', 'opt-hint', 'Leave dry run on until you have read a full plan in '
    + 'Activity and confirmed nothing unexpected is listed for deletion.'));

  if (tb.enabled && !tb.write_token_present) {
    body.append(warnNote('No Calendar write token — nothing can be written.'));
  }

  body.append(fieldRow('Minimum time at home (min)',
    numberInput(draft.min_home_dwell_min, 0, 480, function (v) { draft.min_home_dwell_min = v; })));
  body.append(el('p', 'opt-hint', 'Below this much free time at home between two events, '
    + 'a direct hop is assumed and no drive-home block is written.'));

  body.append(fieldRow('Block title', textInput(draft.title_template, '',
    tb.max_title_template || 60, function (v) { draft.title_template = v; })));
  body.append(el('p', 'opt-hint', 'Never the destination — a shared calendar view must '
    + 'leak nothing about where anyone is going.'));

  renderCapability(body);
  renderLastSweep(body, tb);

  // Read-only, file-edited: widening the horizon past the days the daily scan
  // actually fetches is clamped, so it is changed next to family.unknown_scan_days.
  const dl = el('dl', 'family-rules');
  defRow(dl, 'Horizon', (tb.horizon_days || 0) + ' day(s)');
  defRow(dl, 'Write token', tb.write_token_present ? 'present' : 'missing');
  body.append(dl);
}

const TRAVEL = {
  title: 'Travel blocks',
  help: 'Commute blocks the daily calendar sync keeps on each calendar: one before a trip '
    + 'out, one for the drive home.',
  where: 'config/local.json, horizon included. Every delete backs the event up to '
    + 'data/calendar_backups/ first; the write token comes from docs/calendar-bootstrap.md.',
  draft: function () {
    const tb = lastData.travel_blocks || {};
    return {
      enabled: !!tb.enabled,
      dry_run: tb.dry_run !== false,
      min_home_dwell_min: tb.min_home_dwell_min != null ? tb.min_home_dwell_min : 45,
      title_template: tb.title_template || '',
    };
  },
  payload: function (d) {
    return {
      travel_blocks_enabled: d.enabled,
      travel_blocks_dry_run: d.dry_run,
      min_home_dwell_min: d.min_home_dwell_min,
      title_template: d.title_template,
    };
  },
  render: renderTravel,
  // The sweep controls sit below the settings, outside the draft: rendering
  // them never fires anything — only a click does.
  after: function () { renderSweepControls(els.familyTravelSweep, lastData.travel_blocks || {}); },
};

// --------------------------------------------- sweep run controls (#276)

/* Firing a sweep reuses the one execution pipeline every other run goes
 * through: POST /api/execution/run with the existing `calendar-scan` verb, then
 * poll /api/execution/runs until it leaves the active slot. #266 rejected a
 * travel-blocks-only verb (it would need either a second calendar read seam or
 * a re-run of the whole scan), so the sweep rides along inside the scan
 * exactly as the scheduled job runs it.
 *
 * Deliberately *not* routed through execution.js's client-side queue: that
 * module already imports this one (the traffic switch), and the Family tab has
 * to work whether or not anything else has rendered. What actually serialises
 * runs is the server's single-flight guard, so a 409 is the contract — and it
 * renders as "already running", not as a failure.
 *
 * Neither button is a safety mechanism. `run_travel_blocks` re-checks the gate,
 * the dry-run short-circuit and the write token on every invocation, and
 * `calendar-scan --dry-run` now forces the sweep dry server-side (#276), so a
 * rehearsal cannot write even if this file were wrong about what to disable. */
const SWEEP_POLL_MS = 2500;
// A run that never shows up in the list (killed before it registered, a record
// pruned) must not leave the buttons disabled for the rest of the session.
const SWEEP_MISSING_TICKS = 8;

const sweep = { running: false, runId: null, message: '', tone: '', ticks: 0 };
let sweepTimer = null;
const sweepEls = { rehearse: null, live: null, status: null };

function liveSweepBlockers() {
  return ((lastData && lastData.travel_blocks) || {}).live_sweep_blockers || [];
}

function setSweep(patch) {
  Object.assign(sweep, patch);
  syncSweepControls();
}

/* The controls are updated in place rather than by re-rendering the sheet: a
 * poll tick that rebuilt the DOM would throw away whatever the operator is
 * typing into the dwell/title fields. */
function syncSweepControls() {
  if (!sweepEls.status) return;
  const blocked = liveSweepBlockers().length > 0;
  sweepEls.rehearse.disabled = sweep.running;
  sweepEls.live.disabled = sweep.running || blocked;
  sweepEls.status.textContent = sweep.message;
  sweepEls.status.classList.toggle('tb-run-status--error', sweep.tone === 'error');
}

function sweepButton(text, glyph, tier, mode) {
  const btn = el('button', 'btn-block ' + tier);
  btn.type = 'button';
  // Static sprite markup only — never user content — so innerHTML is safe here.
  btn.innerHTML = icon(glyph);
  btn.append(text);
  btn.addEventListener('click', function () { startSweep(mode); });
  return btn;
}

function renderSweepControls(box, tb) {
  box.textContent = '';
  box.append(fieldLabel('Run a sweep'));
  const actions = el('div', 'tb-actions');
  const rehearse = sweepButton('Rehearse (dry run)', 'eye', 'button-ghost btn-touch', 'dry_run');
  const live = sweepButton('Run sweep', 'car', 'button-tint', 'live');
  actions.append(rehearse, live);
  box.append(actions);

  const status = el('p', 'muted small tb-run-status', '');
  box.append(status);
  sweepEls.rehearse = rehearse;
  sweepEls.live = live;
  sweepEls.status = status;

  box.append(el('p', 'opt-hint', 'Both fire the daily calendar sync — the run that '
    + 'performs the sweep. A rehearsal forces the sweep to plan only and sends no summary '
    + 'message. Both run against the saved settings, not unsaved edits.'));

  const blockers = tb.live_sweep_blockers || [];
  if (blockers.length) {
    box.append(el('p', 'opt-hint', 'A live sweep is unavailable. The sweep itself refuses '
      + 'for these same reasons — this button is not what is stopping it:'));
    for (const b of blockers) box.append(warnNote(b.message));
  }
  syncSweepControls();
}

async function startSweep(mode) {
  if (sweep.running) return;
  setSweep({ running: true, runId: null, ticks: 0, tone: '',
    message: 'Starting the calendar sync…' });
  let started;
  try {
    started = await jsonApi('/api/execution/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action: 'calendar-scan', mode: mode }),
    });
  } catch (exc) {
    // 409 is the single-flight guard doing its job, not a failure: something
    // else is running, and it will be runnable again shortly.
    if (exc.status === 409) {
      setSweep({ running: false, tone: '',
        message: 'A run is already in progress — try again when it finishes.' });
      toast('A run is already in progress');
    } else {
      const message = String(exc.message || exc);
      setSweep({ running: false, tone: 'error', message: 'Could not start: ' + message });
      toast(message, 'error');
    }
    return;
  }
  setSweep({
    runId: started.run_id,
    message: mode === 'dry_run'
      ? 'Rehearsing — the sweep will plan only and write nothing…'
      : 'Sweeping — planned blocks may be written to the calendars above…',
  });
  scheduleSweepPoll();
}

function scheduleSweepPoll() {
  clearTimeout(sweepTimer);
  sweepTimer = setTimeout(pollSweep, SWEEP_POLL_MS);
}

async function pollSweep() {
  if (!sweep.running) return;
  let data;
  try {
    data = await jsonApi('/api/execution/runs?limit=10');
  } catch (_) {
    scheduleSweepPoll();  // transient — the run is still out there, keep waiting
    return;
  }
  const record = (data.runs || []).find(function (r) { return r.run_id === sweep.runId; });
  const active = !!(data.active && data.active.run_id === sweep.runId);
  const running = active
    || (record && (record.status === 'running' || record.status === 'pending'));
  if (running || (!record && sweep.ticks < SWEEP_MISSING_TICKS)) {
    sweep.ticks += 1;
    scheduleSweepPoll();
    return;
  }
  await finishSweep(record);
}

/* What the sweep inside the finished scan actually did. Every branch that could
 * not establish something says so: a run with no section at all is not a sweep
 * that found nothing, and a gated sweep is named by its gate. */
function sweepOutcome(result) {
  const section = result && result.travel_blocks;
  if (!section) return 'The calendar sync finished, but recorded no travel-block sweep.';
  if (section.status !== 'ok') {
    return 'Sweep gated: ' + travelStatusLabel(section.status)
      + ' — nothing was planned and nothing was written.';
  }
  const c = section.counts;
  if (!c) return 'The sweep ran but recorded no counts — see the run in Activity for its payload.';
  const mode = section.dry_run === false ? 'live' : 'planned only, nothing written';
  return 'Sweep finished (' + mode + ') — ' + c.adds + ' add · ' + c.deletes + ' delete · '
    + c.keeps + ' kept · ' + c.protected + ' left alone · ' + c.failures + ' unpriced.';
}

async function finishSweep(record) {
  clearTimeout(sweepTimer);
  sweep.running = false;
  sweep.runId = null;
  if (!record) {
    setSweep({ tone: 'error', message: 'The run left the queue but no record could be read — '
      + 'open the run in Activity to see what it did.' });
  } else if (record.status === 'failed' || record.error) {
    // The record's own `error` is often empty for a webapp-launched run (the
    // captured output carries the detail), so always name where to go look.
    setSweep({ tone: 'error', message: 'The calendar sync failed'
      + (record.error ? ': ' + record.error : '')
      + '. Nothing was swept — open the run in Activity.' });
  } else {
    setSweep({ tone: '', message: sweepOutcome(record.result) });
  }
  await refreshAfterSweep();
}

/* Pull the fresh reported state so "Last sweep" reflects the run just fired —
 * without a page reload, and without discarding the open sheet's draft: the
 * sheet redraws from the draft it already holds over the new report. */
async function refreshAfterSweep() {
  await fetchQuiet('/api/family', function (data) {
    adopt(data);
    if (travelSheet.isOpen(TRAVEL)) travelSheet.render();
  });
}

// ------------------------------------------------ traffic checks (#164, #373)
// The enable switch (`els.execTrafficEnabled`, id kept from the Run tab) and
// the last-check line sit in this pane since #373 and edit this module's own
// /api/family — the traffic-jam insurance feature is a family/traffic domain
// concern (#240). execution.js keeps only `runTraffic`, the one piece that
// genuinely belongs to its run queue (firing a one-off traffic-check run).
function trafficWhen(iso) {
  return iso ? fmtLocalDateTime(iso, { withYear: false }) : 'never';
}

function renderTraffic() {
  const t = lastData && lastData.traffic;
  if (!t) return;
  setSwitch(els.execTrafficEnabled, !!t.enabled);
  els.execTrafficStatus.textContent =
    'Last check ' + trafficWhen(t.last_check) + ' · last alert ' + trafficWhen(t.last_alert);
}

// ---------------------------------------------------------------- boot

function wireSwitch(btn, key) {
  btn.addEventListener('click', function () {
    const next = btn.getAttribute('aria-checked') !== 'true';
    setSwitch(btn, next);  // optimistic; the patch answer confirms
    const body = {};
    body[key] = next;
    patchFamily(body);
  });
}

export function wireFamily() {
  els.paneFamily.addEventListener('click', function (ev) {
    const row = ev.target.closest('[data-sheet]');
    if (row && lastData) ruleSheet.open(SHEETS[row.dataset.sheet]);
  });
  els.familyTravelRow.addEventListener('click', function () {
    if (lastData) travelSheet.open(TRAVEL);
  });
  wireSwitch(els.familySyncSwitch, 'family_enabled');
  wireSwitch(els.familyAskSwitch, 'ask_missing_locations');
}
