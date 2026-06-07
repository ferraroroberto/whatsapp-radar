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

// Guards the brief window between firing a run and the server reporting it
// active, so the poll loop never fires the next chained step twice.
let firing = false;

// Per-kind display: the chip label + an icon for the runs list and viewer title.
const KIND_META = {
  scan: { label: 'Full pipeline', icon: '▶' },
  process: { label: 'Process', icon: '▷' },
  notify: { label: 'Message', icon: '✉' },
  resync: { label: 'Sync', icon: '⟳' },
  reprocess: { label: 'Reprocess', icon: '♻️' },
};

function kindLabel(kind) { return (KIND_META[kind] || { label: kind }).label; }
function kindIcon(kind) { return (KIND_META[kind] || { icon: '•' }).icon; }

function statusIcon(status) {
  if (status === 'running' || status === 'pending') return '⏳';
  if (status === 'completed') return '✅';
  if (status === 'failed') return '❌';
  return '•';
}

function fmtTs(ts) {
  if (!ts) return '';
  return String(ts).replace('T', ' ').slice(0, 19);
}

function isRunning(status) { return status === 'running' || status === 'pending'; }

// ----------------------------------------------------------- run actions

function execState() { return state.execution; }

function selection() {
  return {
    sync: els.execStageSync.checked,
    process: els.execStageProcess.checked,
    message: els.execStageMessage.checked,
  };
}

// Translate the ticked steps + mode into the run(s) to fire. Dry-run simulates
// the whole pipeline on stored data (one scan --dry-run). Live composes: all
// three steps → the integrated scan; otherwise each step's own command, in
// order. "Message" = deliver, so Process without Message analyzes without
// sending (a preview), and Process with Message delivers — never both.
function buildChain(sel, mode) {
  const ex = execState();
  if (mode === 'dry_run') {
    const body = { action: 'scan', mode: 'dry_run' };
    if (ex.window === 'days') body.days = Number(ex.days) || 7;
    return [body];
  }
  if (sel.sync && sel.process && sel.message) {
    return [{ action: 'scan', mode: 'live' }];
  }
  const chain = [];
  if (sel.sync) chain.push({ action: 'resync' });
  if (sel.process) chain.push({ action: 'process', mode: sel.message ? 'live' : 'dry_run' });
  if (sel.message && !sel.process) chain.push({ action: 'notify' });
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

// Connection liveness dot (mirrors local-llm-hub's green-dot health pill): is
// the WhatsApp sidecar paired & fresh? Refreshed alongside the runs poll.
export async function fetchHealth() {
  let h;
  try {
    h = await jsonApi('/api/execution/health');
  } catch (_) {
    return;
  }
  els.execHealthDot.classList.remove('up', 'down', 'unknown');
  els.execHealthDot.classList.add(h.connected ? 'up' : 'down');
  els.execHealthDetail.textContent = h.detail || (h.connected ? 'connected' : 'not connected');
}

export async function fetchExecution() {
  fetchHealth().catch(function () {});
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

function updateRunLabel() {
  const ex = execState();
  if (ex.mode === 'dry_run') { els.execRunScan.textContent = '🧪 Run dry-run'; return; }
  const sel = selection();
  const n = (sel.sync ? 1 : 0) + (sel.process ? 1 : 0) + (sel.message ? 1 : 0);
  if (n === 3) els.execRunScan.textContent = '▶ Run full pipeline';
  else if (n === 0) els.execRunScan.textContent = '▶ Run';
  else els.execRunScan.textContent = '▶ Run ' + n + ' step' + (n > 1 ? 's' : '');
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
  els.execRunScan.disabled = busy;
  els.execReprocess.disabled = busy;
  els.execBusy.hidden = !busy;
  if (busy) {
    const label = ex.active ? kindLabel(ex.active.kind) : 'next step';
    const queued = ex.queue.length ? ` (+${ex.queue.length} queued)` : '';
    els.execBusy.textContent = '⏳ ' + label + ' in progress…' + queued;
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
  when.textContent = fmtTs(run.started_at);
  li.append(badge, name, when);
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
  return null;
}

function renderFunnel(run) {
  const box = els.execFunnel;
  box.textContent = '';
  const cells = funnelCells(run.result);
  if (!cells) {
    const p = document.createElement('p');
    p.className = 'muted small';
    p.textContent = isRunning(run.status) ? 'Running… watch the output below.'
      : 'No funnel for this run.';
    box.appendChild(p);
    return;
  }
  for (const cell of cells) {
    const div = document.createElement('div');
    div.className = 'exec-funnel-cell';
    const v = document.createElement('span');
    v.className = 'exec-funnel-val';
    v.textContent = (cell.value === undefined || cell.value === null) ? '–' : String(cell.value);
    const l = document.createElement('span');
    l.className = 'exec-funnel-label';
    l.textContent = cell.label;
    div.append(v, l);
    box.appendChild(div);
  }
}

function renderViewer(run) {
  els.execViewer.hidden = false;
  els.execViewerEmpty.hidden = true;
  els.execViewerCard.open = true;  // reveal the detail when a run is selected
  els.execViewerTitle.textContent =
    kindIcon(run.kind) + ' ' + kindLabel(run.kind);

  const bits = [statusIcon(run.status) + ' ' + (run.status || '?')];
  if (run.started_at) bits.push('started ' + fmtTs(run.started_at));
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
  // Refresh + Stop live inside <summary> elements; stop their clicks from
  // toggling the surrounding <details>.
  els.execKill.addEventListener('click', function (ev) {
    ev.preventDefault(); ev.stopPropagation(); killSelected();
  });
  els.execRefresh.addEventListener('click', function (ev) {
    ev.preventDefault(); ev.stopPropagation(); fetchExecution().catch(function () {});
  });

  for (const c of [els.execStageSync, els.execStageProcess, els.execStageMessage]) {
    c.addEventListener('change', updateRunLabel);
  }

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
