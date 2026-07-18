/* Family tab (#160): the two deterministic scheduled checks, made transparent.
 *
 * Shows the exact rules in force (read from config), the enable toggles +
 * significant-delay threshold (editable → POST /api/family), a "run now"
 * (dry-run) trigger per check, and the recent runs with their outcomes — so the
 * checks are never a black box. All values go in via textContent only. */

import { els, state } from './state.js';
import { jsonApi, toast } from './api.js';
import { fmtLocalDateTime } from './format.js';

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
}

export async function fetchFamily() {
  let data;
  try {
    data = await jsonApi('/api/family');
  } catch (exc) {
    return; // 401 flips the login overlay in api.js; stay quiet otherwise.
  }
  state.family = data;
  render(data);
}

function onOff(enabled) {
  return enabled ? 'ON' : 'OFF';
}

function toggleButton(label, enabled, onClick) {
  const btn = el('button', 'btn' + (enabled ? ' btn-on' : ''), label + ': ' + onOff(enabled));
  btn.type = 'button';
  btn.setAttribute('aria-pressed', enabled ? 'true' : 'false');
  btn.addEventListener('click', onClick);
  return btn;
}

async function patchFamily(body) {
  try {
    const data = await jsonApi('/api/family', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    state.family = data;
    render(data);
  } catch (exc) {
    toast(exc.message || String(exc), 'error');
  }
}

async function runNow(action) {
  try {
    await jsonApi('/api/execution/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action: action, mode: 'dry_run' }),
    });
    toast(action + ' started (dry-run) — see Execution/Audit for detail', 'ok');
    setTimeout(function () { fetchFamily().catch(function () {}); }, 1500);
  } catch (exc) {
    if (exc.status === 409) toast('A run is already in progress', 'error');
    else toast(exc.message || String(exc), 'error');
  }
}

function renderControls(d) {
  const box = els.familyControls;
  box.textContent = '';

  const traffic = el('div', 'family-control-row');
  traffic.append(
    toggleButton('Traffic check', d.traffic.enabled, function () {
      patchFamily({ traffic_enabled: !d.traffic.enabled });
    }),
  );
  const trafficRun = el('button', 'btn btn-ghost', 'Run now (dry)');
  trafficRun.type = 'button';
  trafficRun.addEventListener('click', function () { runNow('traffic-check'); });
  traffic.append(trafficRun);
  box.append(traffic);

  const family = el('div', 'family-control-row');
  family.append(
    toggleButton('Daily scan', d.family.enabled, function () {
      patchFamily({ family_enabled: !d.family.enabled });
    }),
  );
  const familyRun = el('button', 'btn btn-ghost', 'Run now (dry)');
  familyRun.type = 'button';
  familyRun.addEventListener('click', function () { runNow('calendar-scan'); });
  family.append(familyRun);
  box.append(family);

  if (!d.traffic.api_key_set) {
    box.append(el('p', 'muted small', '⚠️ No Routes API key configured — traffic checks will error.'));
  }
  if (!d.token_present) {
    box.append(el('p', 'muted small', '⚠️ No Calendar token — run the bootstrap (see docs/calendar-bootstrap.md).'));
  }
}

function defRow(dl, term, value) {
  dl.append(el('dt', 'muted small', term));
  dl.append(el('dd', 'small', value));
}

function renderRules(d) {
  const box = els.familyRules;
  box.textContent = '';
  const dl = el('dl', 'family-rules');

  defRow(dl, 'Home', d.family.home_address || '—');
  defRow(dl, 'Calendars', d.calendars.map(function (c) { return c.label || c.person; }).join(', ') || '—');
  const resp = d.family.responsible_by_weekday || {};
  defRow(dl, 'On duty', Object.keys(resp).map(function (k) { return k + ' ' + resp[k]; }).join(' · ') || '—');
  defRow(dl, 'Kids home by', d.family.kids_home_time || '—');
  defRow(dl, 'Childcare',
    (d.family.childcare_windows || []).map(function (w) {
      return w.label + ' (' + (w.days || []).join('/') + ' ' + w.time + ')';
    }).join(' · ') || '—');
  defRow(dl, 'Quiet hours', d.traffic.quiet_start_hour + ':00–' + d.traffic.quiet_end_hour + ':00');
  defRow(dl, 'Significant delay', '> ' + d.traffic.significant_delay_min + ' min');
  defRow(dl, 'Scan window', d.family.assessment_days + 'd conflict · ' + d.family.unknown_scan_days + 'd unknown pre-check');
  box.append(dl);

  // Editable threshold: significant-delay minutes.
  const editor = el('label', 'stacked');
  editor.append(el('span', undefined, 'Significant delay (min)'));
  const input = document.createElement('input');
  input.type = 'number';
  input.min = '0';
  input.max = '240';
  input.className = 'input-native';
  input.value = String(d.traffic.significant_delay_min);
  input.addEventListener('change', function () {
    const v = parseInt(input.value, 10);
    if (Number.isFinite(v)) patchFamily({ significant_delay_min: v });
  });
  editor.append(input);
  box.append(editor);
}

function runLine(run) {
  const li = el('li', 'card-list-item');
  const badge = el('span', 'exec-run-badge ' + (run.status === 'completed' ? 'ok' : run.status === 'failed' ? 'ko' : 'run'),
    run.status === 'completed' ? 'OK' : run.status === 'failed' ? 'KO' : '··');
  const kind = el('span', 'small', run.kind === 'traffic-check' ? 'Traffic' : 'Daily scan');
  let detail = run.result_status || run.status || '';
  if (run.kind === 'traffic-check') detail += ' · ' + (run.checked || 0) + ' checked · ' + (run.alerts || 0) + ' alert(s)';
  else detail += ' · ' + (run.conflicts || 0) + ' conflict(s) · ' + (run.unknown_locations || 0) + ' unknown';
  const meta = el('span', 'muted small', fmtLocalDateTime(run.started_at) + ' · ' + detail);
  li.append(badge, kind, meta);
  return li;
}

function renderRuns(d) {
  const list = els.familyRuns;
  list.textContent = '';
  const runs = d.runs || [];
  els.familyRunsEmpty.hidden = runs.length > 0;
  for (const run of runs) list.append(runLine(run));
}

function render(d) {
  renderControls(d);
  renderRules(d);
  renderRuns(d);
}

export function wireFamily() {
  // No boot-time wiring beyond fetch-on-activate; controls bind on render.
}
