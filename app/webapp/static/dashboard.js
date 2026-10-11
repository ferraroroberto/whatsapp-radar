/* Home (#9, #165, #374): is the radar working, and does anything need me?
 *
 * One glance card (WhatsApp · Gmail · Calendar · Traffic): a row per source
 * with a liveness badge on its glyph, the last run as a relative time, and
 * chips for exceptions only — never an OK. The header line names the most
 * severe exception and, like every row, opens that run's sheet in Activity
 * (the owner's first stop when an alert looks odd). Below it, Latest runs: the
 * last three runs of any kind as Activity's own row, plus "All activity".
 *
 * Liveness comes from the source-health poll (execution.js, also feeding
 * Settings' source sheets) and runs from /api/dashboard; each lands on its
 * own, so neither delays the first paint. The per-source counts live in each
 * Settings source sheet since #374/#377 and are still rendered here from the
 * same answer. While the tray's passkey enrollment window is open the header
 * says so and opens the Passkeys sheet (#377: the card left Home). Chat names go in
 * via textContent only — never innerHTML. */

import { els, state } from './state.js';
import { fetchQuiet, jsonApi } from './api.js';
import {
  fmtLocalDateTime, fmtNum, fmtRelative, setHeaderStatus, SOURCE_ICON, SOURCE_LABEL,
  sourceIcon, statusChip,
} from './format.js';
import { setTab } from './tabs.js';
import { openRunInActivity, runRowFor } from './activity.js';
import { fetchHealth, isRunning } from './execution.js';
import { openPasskeys } from './config.js';
import { onWebauthnChange } from './webauthn.js';
import { icon } from './_vendored/icons/icons.js';

const GLANCE = ['whatsapp', 'gmail', 'calendar', 'traffic'];

export async function fetchDashboard() {
  fetchHealth().then(renderGlance).catch(function () {});
  fetchLatest().catch(function () {});
  await fetchQuiet('/api/dashboard', function (data) {
    state.dashboard = data;
    renderGlance();
    renderSourceCounts(data);
  });
}

// ----------------------------------------------------------- glance card

function card(source) {
  const d = state.dashboard;
  return d ? (d.last_activity || []).find(function (c) { return c.source === source; }) : null;
}

// Alive / down for the avatar badge: connected and expected to be, or should be
// up and is not. Traffic has no connection of its own, so it never gets one; a
// source not yet reported or turned off gets none either (design.md avatar).
function liveness(source) {
  const ex = state.execution;
  const health = (ex.sourceHealth || []).find(function (s) { return s.source === source; });
  if (!health || !health.enabled) return null;
  const connected = source === 'whatsapp' && ex.sidecar ? !!ex.sidecar.is_live : !!health.connected;
  return connected ? 'up' : 'down';
}

// Exceptions only (#374): no OK, and success is never a chip.
function glanceChips(source, c, live) {
  const chips = [];
  if (live === 'down') chips.push(statusChip('Offline', 'danger'));
  if (!c || !c.started_at) return chips;
  if (c.status === 'failed') chips.push(statusChip('Failed', 'danger'));
  else if (isRunning(c.status)) chips.push(statusChip('Running', 'neutral'));
  if (c.alerts > 0) {
    if (source === 'traffic') chips.push(statusChip(c.alerts + (c.alerts === 1 ? ' alert' : ' alerts'), 'attention'));
    else if (source === 'calendar') chips.push(statusChip(c.alerts + ' to check', 'attention'));
    else chips.push(statusChip(c.alerts + ' actionable', 'accent'));
  }
  return chips;
}

function glanceRow(source) {
  const c = card(source);
  const live = liveness(source);
  const ran = !!(c && c.kind && c.started_at);
  const li = document.createElement('li');
  li.className = 'action-row glance-row';
  li.dataset.source = source;
  const main = document.createElement('button');
  main.type = 'button';
  main.className = 'action-row-main';
  main.disabled = !ran;
  if (ran) {
    main.setAttribute('aria-haspopup', 'dialog');
    main.addEventListener('click', function () {
      openRunInActivity({ kind: c.kind, dbId: c.db_run_id });
    });
  }
  const avatar = document.createElement('span');
  avatar.className = 'action-row-avatar';
  avatar.setAttribute('aria-hidden', 'true');
  if (live) avatar.dataset.badge = live;
  // Static sprite markup only — never user content — so innerHTML is safe here.
  avatar.innerHTML = icon(SOURCE_ICON[source].slice(3));
  const text = document.createElement('span');
  text.className = 'action-row-text';
  const title = document.createElement('span');
  title.className = 'action-row-title';
  title.textContent = SOURCE_LABEL[source];
  const meta = document.createElement('span');
  meta.className = 'action-row-meta';
  if (!state.dashboard) meta.textContent = '—';
  else meta.textContent = ran ? fmtRelative(c.started_at) + (c.summary ? ' · ' + c.summary : '') : 'no run yet';
  text.append(title, meta);
  main.append(avatar, text);
  li.appendChild(main);
  const chips = glanceChips(source, c, live);
  if (chips.length) {
    const trail = document.createElement('span');
    trail.className = 'action-row-trail run-chips';
    trail.append(...chips);
    li.appendChild(trail);
  }
  return li;
}

// The most severe exception across the sources, else one muted fact.
function renderHeader() {
  const offline = GLANCE.find(function (s) { return liveness(s) === 'down'; });
  const failed = GLANCE.find(function (s) { const c = card(s); return c && c.status === 'failed'; });
  const btn = els.dashStatus;
  const w = state.webauthn || {};
  let target = null;
  let open = null;
  if (offline) {
    setHeaderStatus(btn, SOURCE_LABEL[offline] + ' offline', 'danger');
    target = card(offline);
  } else if (failed) {
    setHeaderStatus(btn, SOURCE_LABEL[failed] + (failed === 'traffic' ? ' check failed'
      : failed === 'calendar' ? ' sync failed' : ' scan failed'), 'danger');
    target = card(failed);
  } else if (w.configured && w.enrollment_open) {
    setHeaderStatus(btn, 'Passkey enrollment open', 'accent');
    open = openPasskeys;
  } else {
    const last = state.dashboard && state.dashboard.scans && state.dashboard.scans.last;
    setHeaderStatus(btn, state.dashboard
      ? (last ? 'Scanned ' + fmtLocalDateTime(last.started_at, { withYear: false }).slice(6) : 'No scan yet')
      : '', null);
  }
  // The exception line is a link into that run in Activity; a plain fact is not.
  const ref = target && target.kind && target.started_at ? { kind: target.kind, dbId: target.db_run_id } : null;
  if (ref) open = function () { openRunInActivity(ref); };
  btn.disabled = !open;
  btn.onclick = open;
}

function renderGlance() {
  els.dashGlance.textContent = '';
  for (const source of GLANCE) els.dashGlance.appendChild(glanceRow(source));
  renderHeader();
}

// ----------------------------------------------------------- latest runs

async function fetchLatest() {
  const data = await jsonApi('/api/audit/runs?limit=3');
  const runs = data.runs || [];
  els.dashLatest.textContent = '';
  for (const run of runs) els.dashLatest.appendChild(runRowFor(run, openRunInActivity));
  if (!runs.length) {
    const li = document.createElement('li');
    li.className = 'dash-latest-empty muted small';
    li.textContent = 'No runs yet.';
    els.dashLatest.appendChild(li);
  }
}

// ------------------------------------------- source counts (Settings' source sheets)

function renderSourceCounts(d) {
  els.dashSources.textContent = '';
  const sources = d.sources || [];
  if (!sources.length) {
    const empty = document.createElement('p');
    empty.className = 'muted small';
    empty.textContent = 'No messages ingested from any source.';
    els.dashSources.appendChild(empty);
  }
  for (const source of sources) {
    els.dashSources.appendChild(sourceRow(source.source,
      fmtNum(source.messages) + ' stored · ' + fmtNum(source.monitored) +
      ' monitored · latest ' + fmtLocalDateTime(source.latest_message_at)));
  }
  // Calendar is a read-only, non-ingesting source (#164/#165): no stored/
  // monitored counts, so its row shows provenance + freshness of the last scan.
  const cal = card('calendar');
  if (cal) {
    els.dashSources.appendChild(sourceRow('calendar', cal.kind && cal.started_at
      ? 'read-only · last scan ' + fmtRelative(cal.started_at)
      : 'read-only · not yet scanned'));
  }
}

// One source-counts row: an icon+label source badge plus a muted detail line.
function sourceRow(sourceKey, detailText) {
  const row = document.createElement('div');
  row.className = 'source-summary-row';
  row.dataset.source = sourceKey;
  const name = document.createElement('span');
  name.className = 'source-badge source-' + sourceKey;
  name.append(sourceIcon(sourceKey));
  name.append(document.createTextNode(SOURCE_LABEL[sourceKey] || sourceKey));
  const detail = document.createElement('span');
  detail.className = 'muted small';
  detail.textContent = detailText;
  row.append(name, detail);
  return row;
}

export function wireDashboard() {
  // Draw the rows at once, a dash for every value still loading (#374).
  renderGlance();
  onWebauthnChange(renderHeader);
  els.dashAllActivity.addEventListener('click', function () { setTab('activity'); });
}
