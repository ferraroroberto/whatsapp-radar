/* Execution tab (#11): run the pipeline — whole or in pieces — and watch it live.
 *
 * Mirrors App Launcher's job-stub model: each action spawns `launcher.py <cmd>`
 * server-side; this module polls the run record and streams its growing output
 * into a viewer, with the structured funnel parsed from the run's result. The
 * pipeline is exposed both whole (Run full pipeline → scan) and in pieces (Sync
 * → resync, Process → review, Message → notify), live or dry-run, plus a guarded
 * Reprocess. Chat names / message text go in via textContent only (privacy). */

import { els, state, EXECUTION_POLL_MS } from './state.js';
import { jsonApi, toast } from './api.js';
import {
  fmtLocalDateTime, kindLabel, renderFunnelCells, renderSourceFunnels,
  sourceIcon, SOURCE_LABEL,
} from './format.js';
import { setSwitch } from './_vendored/switch/switch.js';

// Guards the brief window between firing a run and the server reporting it
// active, so the poll loop never fires the next chained step twice.
let firing = false;

// The vendored switch stores its state in aria-checked (class + aria move
// together through setSwitch — the one write path).
function stageOn(btn) { return btn.getAttribute('aria-checked') === 'true'; }

function isRunning(status) { return status === 'running' || status === 'pending'; }

// ----------------------------------------------------------- run actions

function execState() { return state.execution; }

function selection() {
  return {
    sync: stageOn(els.execStageSync),
    process: stageOn(els.execStageProcess),
    message: stageOn(els.execStageMessage),
    calendar: stageOn(els.execStageCalendar),
  };
}

// Translate the ticked steps + mode into the run(s) to fire. The message stages
// compose as before — dry-run simulates the whole pipeline on stored data (one
// scan --dry-run); live composes all three → the integrated scan, otherwise each
// step's own command in order ("Message" = deliver, so Process without Message
// previews, Process with Message delivers — never both). The independent Calendar
// switch appends a calendar-scan of the same mode after the message run(s).
function buildChain(sel, mode) {
  const ex = execState();
  const chain = [];
  if (mode === 'dry_run') {
    if (sel.sync || sel.process || sel.message) {
      const body = { action: 'scan', mode: 'dry_run' };
      if (ex.window === 'days') body.days = Number(ex.days) || 7;
      chain.push(body);
    }
  } else if (sel.sync && sel.process && sel.message) {
    chain.push({ action: 'scan', mode: 'live' });
  } else {
    if (sel.sync) chain.push({ action: 'resync' });
    if (sel.process) chain.push({ action: 'process', mode: sel.message ? 'live' : 'dry_run' });
    if (sel.message && !sel.process) chain.push({ action: 'notify' });
  }
  if (sel.calendar) chain.push({ action: 'calendar-scan', mode });
  return chain;
}

async function startOne(body) {
  try {
    const started = await jsonApi('/api/execution/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const ex = execState();
    ex.selected = { kind: started.kind, run_id: started.run_id };
    ex.detail = null;
    return started;
  } catch (exc) {
    execState().queue = [];  // abort the rest of the chain on any failure
    if (exc.status === 409) toast('A run is already in progress', 'error');
    else if (exc.status === 403) toast('That action needs confirmation', 'error');
    else toast(String(exc.message || exc), 'error');
    return null;
  }
}

// Fire the next queued action if nothing is running. Driven both by the run
// button (which seeds the queue) and by the poll loop (which advances it once
// the current run finishes) — so multi-step runs play out one after another.
async function pumpQueue() {
  const ex = execState();
  if (firing || ex.active || ex.queue.length === 0) return;
  firing = true;
  try {
    const started = await startOne(ex.queue.shift());
    ex.active = started ? { kind: started.kind, run_id: started.run_id } : null;
  } finally {
    firing = false;
  }
  renderControls();
}

async function runSelection() {
  const chain = buildChain(selection(), execState().mode);
  if (chain.length === 0) {
    toast('Pick at least one step to run', 'error');
    return;
  }
  execState().queue = chain;
  toast(chain.length > 1 ? `Running ${chain.length} steps…` : 'Running…', '');
  await pumpQueue();
  fetchExecution().catch(function () {});
}

function confirmReprocess() {
  // Destructive: a full rebuild that resets run history. Guard with an explicit
  // confirm even though the API also requires confirm:true (defence in depth).
  const ok = window.confirm(
    'Rebuild reconstructs the local cache from the connector buffer.\n\n' +
    'Your monitored/ignored/alias choices are preserved and the DB is backed up '
    + 'first, but run & analysis history will reset.\n\nProceed?'
  );
  if (!ok) return;
  execState().queue = [{ action: 'reprocess', confirm: true }];
  pumpQueue().then(function () { fetchExecution().catch(function () {}); });
}

async function killSelected() {
  const sel = execState().selected;
  if (!sel) return;
  try {
    await jsonApi(`/api/execution/runs/${sel.kind}/${sel.run_id}/kill`, { method: 'POST' });
    toast('Stopping run…', '');
  } catch (exc) {
    toast(String(exc.message || exc), 'error');
  }
  fetchExecution().catch(function () {});
}

// ----------------------------------------------------------- polling + render

// Connection liveness (home-automation's Internet-tile pattern): a bold status
// word — Online / Offline / Connecting / Needs QR — instead of a dot. When the
// source is down, the card below grows a one-tap relaunch and — if a fresh QR
// is needed — the pairing image, so the connection can be recovered from a
// phone without a terminal (#29). Refreshed with the poll.
// Re-stamp the QR src at most every 20s (the pairing code rotates ~that often)
// so the <img> doesn't flicker on every 1.5s poll.
function qrSrc() {
  return '/api/sidecar/qr?t=' + Math.floor(Date.now() / 20000);
}

function fmtCount(n) { return (n === undefined || n === null) ? '–' : Number(n).toLocaleString(); }

// Compact local timestamp for sync/health lines — the one shared formatter so
// no surface can drift to a different clock or format again (#163).
function fmtSyncWhen(iso) {
  if (!iso) return '';
  return fmtLocalDateTime(iso, { withYear: false });
}

// When the source is live, show what's actually *stored* and the last sync delta
// (truthful) instead of the sidecar's session counters, which reset on reconnect
// and misread as "empty" (#31). Abbreviated ("msg") so the line never wraps.
function addStatusLine(list, label, value) {
  const li = document.createElement('li');
  li.textContent = label + ': ' + (value || '—');
  list.appendChild(li);
}

// The Calendar row (#164) is a read-only, non-ingesting source: it has no sync
// history or stored-message counters, so it renders its own compact detail set.
function renderCalendarHealth(source) {
  const card = document.createElement('section');
  card.className = 'source-status-card';
  const head = document.createElement('div');
  head.className = 'source-status-head';
  const badge = document.createElement('span');
  badge.className = 'source-badge source-calendar';
  badge.textContent = 'Calendar';
  badge.prepend(sourceIcon('calendar'));
  const stateWord = document.createElement('span');
  stateWord.className = 'source-status-state ' + (source.connected ? 'is-online' : 'is-offline');
  stateWord.textContent = source.connected ? 'Connected' : 'Not connected';
  head.append(badge, stateWord);
  const details = document.createElement('ul');
  details.className = 'source-status-details';
  addStatusLine(details, 'State',
    (source.configured ? 'configured' : 'not configured') + ' · ' +
    (source.enabled ? 'daily scan enabled' : 'daily scan disabled') + ' · ' +
    (source.authorized ? 'authorized' : 'not authorized'));
  addStatusLine(details, 'Mode', 'read-only Google Calendar');
  addStatusLine(details, 'Authorization', source.token_present ? 'token present' : 'token missing');
  addStatusLine(details, 'Calendars',
    fmtCount(source.account_count) + ((source.accounts || []).length ? ' · ' + source.accounts.join(' · ') : ''));
  addStatusLine(details, 'Last successful fetch', source.last_success_at ? fmtSyncWhen(source.last_success_at) : 'never');
  card.append(head, details);
  return card;
}

function renderSourceHealth() {
  const ex = execState();
  els.execSources.textContent = '';
  els.execSourcesCount.textContent = ex.sourceHealth.length
    ? ex.sourceHealth.length + ' sources'
    : '';
  if (!ex.sourceHealth.length) {
    const unavailable = document.createElement('section');
    unavailable.className = 'source-status-card muted small';
    unavailable.textContent = 'Source status unavailable.';
    els.execSources.appendChild(unavailable);
    return;
  }
  for (const source of ex.sourceHealth) {
    if (source.source === 'calendar') {
      els.execSources.appendChild(renderCalendarHealth(source));
      continue;
    }
    const sidecar = source.source === 'whatsapp' ? ex.sidecar : null;
    const connected = sidecar ? !!sidecar.is_live : !!source.connected;
    const card = document.createElement('section');
    card.className = 'source-status-card';
    const head = document.createElement('div');
    head.className = 'source-status-head';
    const badge = document.createElement('span');
    badge.className = 'source-badge source-' + source.source;
    badge.textContent = SOURCE_LABEL[source.source] || source.source;
    badge.prepend(sourceIcon(source.source));
    const stateWord = document.createElement('span');
    stateWord.className = 'source-status-state ' + (connected ? 'is-online' : 'is-offline');
    stateWord.textContent = connected ? 'Connected' : 'Not connected';
    head.append(badge, stateWord);
    const details = document.createElement('ul');
    details.className = 'source-status-details';
    addStatusLine(details, 'State',
      (source.configured ? 'configured' : 'not configured') + ' · ' +
      (source.enabled ? 'enabled' : 'disabled') + ' · ' +
      (source.authorized ? 'authorized' : 'not authorized'));
    if (source.source === 'gmail') {
      addStatusLine(details, 'Mode', 'read-only Gmail API');
      addStatusLine(details, 'Authorization',
        (source.token_present ? 'token present' : 'token missing') + ' · ' +
        (source.whitelist_valid ? 'whitelist validated' : 'whitelist not validated'));
      addStatusLine(details, 'Account', source.account || 'authorization unavailable');
      const whitelist = source.whitelist || {};
      const senders = (whitelist.senders || []).map(function (x) { return x.address; });
      const labels = (whitelist.labels || []).map(function (x) { return x.name; });
      addStatusLine(details, 'Whitelist', senders.concat(labels).join(' · ') || 'empty');
      addStatusLine(details, 'Scope', source.history_scope);
    } else {
      addStatusLine(details, 'Mode', 'linked-device read-only application behavior');
      addStatusLine(details, 'Connection', sidecar ? (sidecar.detail || sidecar.state) : source.detail);
    }
    const liveTotals = ((ex.syncTotals || {}).by_source || {})[source.source] || {};
    const storedMessages = liveTotals.messages ?? source.stored_messages;
    const storedChannels = liveTotals.channels ?? source.stored_channels;
    const monitoredChannels = liveTotals.monitored ?? source.monitored_channels;
    const latestStored = liveTotals.latest_message_at || source.latest_stored_at;
    addStatusLine(details, 'Stored', fmtCount(storedMessages) + ' messages in ' + fmtCount(storedChannels) + ' channels');
    addStatusLine(details, 'Monitored', fmtCount(monitoredChannels) + ' channels');
    addStatusLine(details, 'Latest stored', fmtSyncWhen(latestStored));
    const sourceSyncs = ex.syncs.filter(function (row) { return row.connector_source === source.source; });
    const attempt = sourceSyncs[0] || source.last_attempt;
    addStatusLine(details, 'Last checked', attempt
      ? fmtSyncWhen(attempt.ran_at) + ' · ' + attempt.status + ' · +' + attempt.messages_added
      : 'never');
    const success = sourceSyncs.find(function (row) { return row.status === 'success'; }) || source.last_success;
    addStatusLine(details, 'Last successful sync', success ? fmtSyncWhen(success.ran_at) : 'never');
    if (!storedMessages) addStatusLine(details, 'Ingestion', 'no matching messages stored');
    if (!monitoredChannels) addStatusLine(details, 'Analysis', 'nothing monitored; messages will not reach Stage 1');
    if (!connected && source.detail) addStatusLine(details, 'Action needed', source.detail);
    card.append(head, details);
    els.execSources.appendChild(card);
  }
}

export async function fetchHealth() {
  let s;
  try {
    s = await jsonApi('/api/sidecar/status');
  } catch (_) {
    return;
  }
  execState().sidecar = s;
  const ex = execState();
  if (!ex.sourceHealthAt || Date.now() - ex.sourceHealthAt > 60000) {
    try {
      const health = await jsonApi('/api/execution/health');
      ex.sourceHealth = health.sources || [];
      ex.sourceHealthAt = Date.now();
    } catch (_) { /* retain the last truthful snapshot */ }
  }
  renderSourceHealth();
  renderReconnect(s);
}

// Recent sync deltas (sync_log): proof the ingest is actually pulling new data,
// covering scheduled Jobs the run viewer never sees.
export async function fetchSyncs() {
  let data;
  try {
    data = await jsonApi('/api/execution/syncs');
  } catch (_) {
    return;
  }
  const ex = execState();
  ex.syncs = data.syncs || [];
  ex.syncTotals = data.totals || null;
  renderSyncs();
  renderSourceHealth();
}

function syncRow(s) {
  const li = document.createElement('li');
  li.className = 'exec-sync-li';
  const when = document.createElement('span');
  when.className = 'exec-sync-when muted small';
  when.textContent = fmtSyncWhen(s.ran_at);
  const delta = document.createElement('span');
  delta.className = 'exec-sync-delta';
  const chatBit = s.chats_added ? ` · +${s.chats_added} chat${s.chats_added > 1 ? 's' : ''}` : '';
  delta.textContent = `+${s.messages_added} msg${s.messages_added === 1 ? '' : 's'}${chatBit}`;
  const src = document.createElement('span');
  src.className = 'exec-sync-src muted small';
  const source = s.connector_source === 'gmail' ? 'Gmail' : 'WhatsApp';
  src.textContent = source + ' · ' + kindLabel(s.source);
  li.append(when, delta, src);
  return li;
}

function renderSyncs() {
  const ex = execState();
  els.execSyncs.textContent = '';
  els.execSyncsEmpty.hidden = ex.syncs.length > 0;
  for (const s of ex.syncs) els.execSyncs.appendChild(syncRow(s));
  els.execSyncTotals.textContent = ex.syncTotals
    ? `${fmtCount(ex.syncTotals.chats)} chats · ${fmtCount(ex.syncTotals.messages)} msgs`
    : '';
}

// ----------------------------------------------------- traffic jam insurance

// The Run-tab traffic card (#164): the enable toggle + cadence are config
// (persisted through the family safe-override path), and the status line reads
// the last check / last alert from the unified run store (#163). Scheduling
// itself is an App Launcher job — nothing runs in-process on this cadence.
function trafficWhen(iso) { return iso ? fmtSyncWhen(iso) : 'never'; }

function renderTraffic() {
  const t = execState().traffic;
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
  const ex = execState();
  if (!force && ex.trafficAt && Date.now() - ex.trafficAt < 12000) return;
  let data;
  try {
    data = await jsonApi('/api/family');
  } catch (_) {
    return;  // 401 flips the login overlay; stay quiet otherwise.
  }
  ex.traffic = data.traffic || {};
  ex.trafficAt = Date.now();
  renderTraffic();
}

async function patchTraffic(body) {
  try {
    const data = await jsonApi('/api/family', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    execState().traffic = data.traffic || {};
    renderTraffic();
  } catch (exc) {
    toast(String(exc.message || exc), 'error');
    fetchTraffic(true);  // re-sync the controls to the server's truth
  }
}

function runTraffic(mode) {
  const ex = execState();
  if (ex.active || ex.queue.length > 0 || firing) {
    toast('A run is already in progress', 'error');
    return;
  }
  ex.queue = [{ action: 'traffic-check', mode: mode }];
  toast('Running…', '');
  pumpQueue().then(function () { fetchExecution().catch(function () {}); });
}

function renderReconnect(s) {
  if (s.is_live) {
    els.execReconnect.hidden = true;
    els.execQr.hidden = true;
    return;
  }
  els.execReconnect.hidden = false;
  els.execSourcesCard.open = true;
  if (s.state === 'needs_qr') {
    els.execReconnectMsg.textContent =
      'Open WhatsApp → Linked devices → Link a device, then scan this code.';
    els.execReconnectBtn.textContent = s.has_qr ? 'Refresh QR' : 'Start & show QR';
    if (s.has_qr) {
      const next = qrSrc();
      if (els.execQr.dataset.src !== next) { els.execQr.src = next; els.execQr.dataset.src = next; }
      els.execQr.hidden = false;
    } else {
      els.execQr.hidden = true;
    }
  } else {
    els.execReconnectMsg.textContent = s.detail || 'The WhatsApp sidecar is not connected.';
    els.execReconnectBtn.textContent = 'Reconnect WhatsApp';
    els.execQr.hidden = true;
  }
  els.execReconnectBtn.disabled = (s.state === 'connecting');
}

async function reconnectSidecar() {
  els.execReconnectBtn.disabled = true;
  try {
    const res = await jsonApi('/api/sidecar/start', { method: 'POST' });
    toast(res.launched ? 'Starting WhatsApp sidecar…' : 'Sidecar already running', '');
  } catch (exc) {
    if (exc.status === 503) toast(String(exc.message || 'Cannot start sidecar'), 'error');
    else toast(String(exc.message || exc), 'error');
  } finally {
    els.execReconnectBtn.disabled = false;
  }
  fetchHealth().catch(function () {});
}

export async function fetchExecution() {
  fetchHealth().catch(function () {});
  fetchSyncs().catch(function () {});
  fetchTraffic().catch(function () {});
  let data;
  try {
    data = await jsonApi('/api/execution/runs');
  } catch (_) {
    return;  // 401 flips the login overlay; stay quiet otherwise.
  }
  const ex = execState();
  ex.runs = data.runs || [];
  ex.active = data.active || null;

  // Advance a chained run once the current step finishes (server-authoritative).
  if (!firing && !ex.active && ex.queue.length > 0) await pumpQueue();

  // Auto-follow the live run when nothing is being inspected yet.
  if (!ex.selected && ex.active) ex.selected = { ...ex.active };

  renderControls();
  renderRuns();

  // Refresh the selected run's detail while it is live; a finished run is static.
  const sel = ex.selected;
  if (sel) {
    const needFetch = !ex.detail
      || ex.detail.run_id !== sel.run_id
      || isRunning(ex.detail.status);
    if (needFetch) await fetchDetail(sel);
  }
}

async function fetchDetail(sel) {
  try {
    const data = await jsonApi(`/api/execution/runs/${sel.kind}/${sel.run_id}`);
    execState().detail = data.run;
    renderViewer(data.run);
  } catch (_) {
    /* transient — next poll retries */
  }
}

// Deep-link entry point: the Dashboard's last-activity cards select a specific
// run here after switching to this tab (#165). run_id is the unified id the
// runs list/detail endpoints use (a "db-<id>" for CLI/Jobs-launched runs).
export function showRun(sel) {
  const ex = execState();
  ex.selected = { kind: sel.kind, run_id: sel.run_id };
  ex.detail = null;
  renderRuns();
  fetchDetail(ex.selected);
}

function updateRunLabel() {
  const ex = execState();
  const sel = selection();
  if (ex.mode === 'dry_run') {
    els.execRunScan.textContent = sel.calendar ? 'Run dry-run + calendar' : 'Run dry-run';
    return;
  }
  const n = (sel.sync ? 1 : 0) + (sel.process ? 1 : 0) + (sel.message ? 1 : 0);
  const parts = [];
  if (n === 3) parts.push('full pipeline');
  else if (n > 0) parts.push(n + ' step' + (n > 1 ? 's' : ''));
  if (sel.calendar) parts.push('calendar');
  els.execRunScan.textContent = parts.length ? 'Run ' + parts.join(' + ') : 'Run';
}

function renderControls() {
  const ex = execState();
  const busy = !!ex.active || ex.queue.length > 0 || firing;
  // In dry-run the whole pipeline is simulated on stored data, so the per-step
  // checklist doesn't apply — grey it out (also disabled while a run is busy).
  const stagesDisabled = busy || ex.mode === 'dry_run';
  for (const c of [els.execStageSync, els.execStageProcess, els.execStageMessage]) {
    c.disabled = stagesDisabled;
  }
  // Calendar sync is an independent action (not part of the dry-run scan
  // simulation), so it stays togglable in dry-run — only a live run locks it.
  els.execStageCalendar.disabled = busy;
  els.execRunScan.disabled = busy;
  els.execReprocess.disabled = busy;
  for (const c of [els.execTrafficRunLive, els.execTrafficRunDry,
                   els.execTrafficEnabled, els.execTrafficCadence]) {
    c.disabled = busy;
  }
  els.execBusy.hidden = !busy;
  if (busy) {
    const label = ex.active ? kindLabel(ex.active.kind) : 'next step';
    const queued = ex.queue.length ? ` (+${ex.queue.length} queued)` : '';
    els.execBusy.textContent = label + ' in progress…' + queued;
  }
  updateRunLabel();
}

// One lean line per run: OK/KO badge first (right or wrong at a glance), then
// the kind, then when. No per-row icons.
function statusBadge(status) {
  if (status === 'completed') return { text: 'OK', cls: 'ok' };
  if (status === 'failed') return { text: 'KO', cls: 'ko' };
  return { text: '··', cls: 'run' };
}

function runsListItem(run) {
  const li = document.createElement('li');
  li.className = 'exec-run-li';
  const ex = execState();
  if (ex.selected && ex.selected.kind === run.kind && ex.selected.run_id === run.run_id) {
    li.classList.add('selected');
  }
  const badge = document.createElement('span');
  const b = statusBadge(run.status);
  badge.className = 'exec-run-badge ' + b.cls;
  badge.textContent = b.text;
  const name = document.createElement('span');
  name.className = 'exec-run-name';
  name.textContent = kindLabel(run.kind);
  const when = document.createElement('span');
  when.className = 'exec-run-when muted small';
  when.textContent = fmtLocalDateTime(run.started_at);
  li.append(badge, name);
  // A dry run must be visibly distinct in the list — four unlabeled "Full
  // pipeline" dry entries read as four real runs (#163).
  if (run.mode === 'dry_run') {
    const dry = document.createElement('span');
    dry.className = 'audit-mode-badge dry';
    dry.textContent = 'Dry run';
    li.append(dry);
  }
  li.append(when);
  li.addEventListener('click', function () {
    const e = execState();
    e.selected = { kind: run.kind, run_id: run.run_id };
    e.detail = null;
    renderRuns();
    fetchDetail(e.selected);
  });
  return li;
}

function renderRuns() {
  const ex = execState();
  const list = els.execRuns;
  list.textContent = '';
  els.execRunsEmpty.hidden = ex.runs.length > 0;
  for (const run of ex.runs) list.appendChild(runsListItem(run));
}

// Funnel cells per kind. Each cell is {label, value}; rendered as a small grid.
function funnelCells(result) {
  if (!result) return null;
  const f = result.funnel || {};
  if (result.kind === 'scan') {
    return [
      { label: 'Synced', value: f.messages_synced },
      { label: 'Monitored', value: f.chats_monitored },
      { label: 'New (Δ)', value: f.chats_with_delta },
      { label: 'Transcribed', value: f.transcriptions },
      { label: 'Stage 1', value: f.stage1_passed },
      { label: 'LLM', value: f.stage2_llm_calls },
      { label: 'Actionable', value: f.actionable },
      { label: 'Notify', value: result.notification_status },
    ];
  }
  if (result.kind === 'process') {
    return [
      { label: 'New (Δ)', value: f.chats_with_delta },
      { label: 'Processed', value: f.messages_processed },
      { label: 'Actionable', value: f.actionable },
      { label: 'Notify', value: result.notification_status },
    ];
  }
  if (result.kind === 'resync') {
    return [
      { label: 'Chats +', value: result.chats_added },
      { label: 'Chats ~', value: result.chats_updated },
      { label: 'Messages +', value: result.messages_added },
    ];
  }
  if (result.kind === 'reprocess') {
    return [
      { label: 'Chats', value: result.chats_after },
      { label: 'Messages', value: result.messages_after },
      { label: 'Monitored', value: result.monitored_preserved },
      { label: 'Ignored', value: result.ignored_preserved },
      { label: 'Aliases', value: result.aliases_preserved },
    ];
  }
  if (result.kind === 'notify') {
    return [{ label: 'Notify', value: result.notification_status }];
  }
  if (result.kind === 'calendar-scan') {
    return [
      { label: 'Conflicts', value: (result.conflicts || []).length },
      { label: 'Unknown loc.', value: (result.unknown_locations || []).length },
      { label: 'Status', value: result.status },
    ];
  }
  if (result.kind === 'traffic-check') {
    return [
      { label: 'Checked', value: (result.checked || []).length },
      { label: 'Alerts', value: result.alerts },
      { label: 'Status', value: result.status },
    ];
  }
  return null;
}

function renderFunnel(run) {
  const box = els.execFunnel;
  const cells = funnelCells(run.result);
  if (!cells) {
    box.textContent = '';
    const p = document.createElement('p');
    p.className = 'muted small';
    p.textContent = isRunning(run.status) ? 'Running… watch the output below.'
      : 'No funnel for this run.';
    box.appendChild(p);
    return;
  }
  renderFunnelCells(box, cells);
}

function renderViewer(run) {
  els.execViewer.hidden = false;
  els.execViewerEmpty.hidden = true;
  els.execViewerCard.open = true;  // reveal the detail when a run is selected
  els.execViewerTitle.textContent = kindLabel(run.kind);

  const bits = [run.status || '?'];
  if (run.started_at) bits.push('started ' + fmtLocalDateTime(run.started_at));
  if (run.result && run.result.backup_path) bits.push('backup: ' + run.result.backup_path);
  if (run.result && Array.isArray(run.result.unmapped) && run.result.unmapped.length) {
    bits.push(run.result.unmapped.length + ' unmapped');
  }
  els.execViewerMeta.textContent = bits.join(' · ');

  // Stop button only while this exact run is the live one.
  const ex = execState();
  const live = isRunning(run.status) && ex.active
    && ex.active.kind === run.kind && ex.active.run_id === run.run_id;
  els.execKill.hidden = !live;

  renderFunnel(run);
  renderSourceFunnels(els.execSourceFunnel, (run.result && run.result.sources) || {});

  const tg = run.result && run.result.telegram_text;
  els.execPreview.hidden = !tg;
  if (tg) els.execPreviewText.textContent = tg;

  els.execOutput.textContent = run.output_tail || '(no output)';
}

// ----------------------------------------------------------- mode controls

function setMode(mode) {
  const ex = execState();
  ex.mode = mode;
  els.execModeLive.classList.toggle('active', mode === 'live');
  els.execModeDry.classList.toggle('active', mode === 'dry_run');
  els.execDryOpts.hidden = mode !== 'dry_run';
  els.execModeHint.textContent = mode === 'dry_run'
    ? 'Dry-run simulates the pipeline on stored messages — no sync, no delivery.'
    : 'Live runs the steps for real (Message sends the alerts).';
  renderControls();
}

function setWindow(win) {
  const ex = execState();
  ex.window = win;
  els.execWinNew.classList.toggle('active', win === 'new');
  els.execWinDays.classList.toggle('active', win === 'days');
  els.execDays.hidden = win !== 'days';
}

// ----------------------------------------------------------- wiring

export function wireExecution() {
  els.execModeLive.addEventListener('click', function () { setMode('live'); });
  els.execModeDry.addEventListener('click', function () { setMode('dry_run'); });
  els.execWinNew.addEventListener('click', function () { setWindow('new'); });
  els.execWinDays.addEventListener('click', function () { setWindow('days'); });
  els.execDays.addEventListener('change', function () {
    execState().days = Math.max(1, Math.min(3650, Number(els.execDays.value) || 7));
    els.execDays.value = execState().days;
  });

  els.execRunScan.addEventListener('click', runSelection);
  els.execReprocess.addEventListener('click', confirmReprocess);
  els.execReconnectBtn.addEventListener('click', reconnectSidecar);
  // Stop lives inside a <summary> element; stop its click from toggling the
  // surrounding <details>.
  els.execKill.addEventListener('click', function (ev) {
    ev.preventDefault(); ev.stopPropagation(); killSelected();
  });

  // Pipeline-step switches (vendored switch component): flip on tap, then
  // recompute the run button's label. setSwitch keeps class + aria in sync.
  for (const c of [els.execStageSync, els.execStageProcess, els.execStageMessage,
                   els.execStageCalendar]) {
    c.addEventListener('click', function () {
      setSwitch(c, !stageOn(c));
      updateRunLabel();
    });
  }

  // Traffic jam insurance card: enable toggle + cadence persist through the
  // family safe-override path; the two Run-now buttons fire a one-off check.
  els.execTrafficEnabled.addEventListener('click', function () {
    const next = !stageOn(els.execTrafficEnabled);
    setSwitch(els.execTrafficEnabled, next);  // optimistic; patch confirms
    patchTraffic({ traffic_enabled: next });
  });
  els.execTrafficCadence.addEventListener('change', function () {
    const v = Math.max(5, Math.min(240, Number(els.execTrafficCadence.value) || 30));
    els.execTrafficCadence.value = String(v);
    patchTraffic({ cadence_min: v });
  });
  els.execTrafficRunLive.addEventListener('click', function () { runTraffic('live'); });
  els.execTrafficRunDry.addEventListener('click', function () { runTraffic('dry_run'); });

  // Viewer starts empty until a run is selected.
  els.execViewer.hidden = true;

  // Tap-to-copy the whole output pane (mirrors App Launcher's run output copy).
  els.execOutput.addEventListener('click', function () {
    const text = els.execOutput.textContent;
    if (!text || text === '(no output)') return;
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(
        function () { toast('Output copied', ''); },
        function () { toast('Copy failed', 'error'); }
      );
    }
  });

  setMode('live');
  setWindow('new');
}
