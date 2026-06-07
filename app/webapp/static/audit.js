/* Audit tab (#12): per-run trace drill-down — the reason the radar is trustworthy.
 *
 * Read-only view over the persisted trace (#7). The run list shows each run's
 * mode (live/dry-run), parameters, and funnel; clicking a run drills into the
 * per-chat decision record: the input delta, the Stage-1 roots, the exact LLM
 * prompts, the raw response, the parsed verdict, the final action, and the
 * Telegram text. Resync/reprocess maintenance events are interleaved so the whole
 * timeline is visible. All chat names / message text / prompts go in via
 * textContent only — never innerHTML (privacy). */

import { els, state } from './state.js';
import { jsonApi } from './api.js';
import { fmtLocalDateTime } from './format.js';

function auditState() { return state.audit; }

// Live = a real run that synced + (maybe) delivered; dry_run = replay on stored
// data; review = the legacy process-only verb. Badge keys map to CSS colors.
const MODE_META = {
  live: { label: 'Live', cls: 'live' },
  dry_run: { label: 'Dry run', cls: 'dry' },
  review: { label: 'Process', cls: 'review' },
};

function modeMeta(mode) { return MODE_META[mode] || { label: mode || '?', cls: 'review' }; }

function statusBadge(status) {
  if (status === 'completed') return { text: 'OK', cls: 'ok' };
  if (status === 'failed') return { text: 'KO', cls: 'ko' };
  if (status === 'running' || status === 'pending') return { text: '··', cls: 'run' };
  return { text: '··', cls: 'run' };
}

// The final per-chat verdict, mapped to a short label + tone for the trace header.
const ACTION_META = {
  actionable: { label: '🔔 Actionable', cls: 'act' },
  not_actionable: { label: 'Not actionable', cls: 'muted' },
  contract_error: { label: '⚠ Contract error', cls: 'err' },
  llm_truncated: { label: '⚠ LLM truncated', cls: 'err' },
};

function actionMeta(action) { return ACTION_META[action] || { label: action || '?', cls: 'muted' }; }

// One compact line summarizing the funnel for the run list.
function voiceFunnelBits(f) {
  if (!f) return '';
  const bits = [];
  if (f.voice_transcribed) bits.push(`${f.voice_transcribed} transcribed`);
  if (f.voice_failed) bits.push(`${f.voice_failed} voice failed`);
  if (f.voice_skipped_old) bits.push(`${f.voice_skipped_old} skipped (old)`);
  return bits.join(' · ');
}

function funnelSummary(f) {
  if (!f) return '';
  const parts = [
    `${f.messages_synced} synced`,
    `${f.chats_monitored} monitored`,
    `${f.stage1_passed} Stage 1`,
    `${f.stage2_llm_calls} LLM`,
    `${f.actionable} actionable`,
  ];
  const voice = voiceFunnelBits(f);
  if (voice) parts.push(voice);
  return parts.join(' · ');
}

function paramsSummary(params) {
  if (!params || typeof params !== 'object') return '';
  const bits = [];
  for (const k of Object.keys(params)) {
    if (params[k] !== null && params[k] !== undefined) bits.push(`${k}=${params[k]}`);
  }
  return bits.join(' ');
}

// ----------------------------------------------------------- run list

function runListItem(run) {
  const li = document.createElement('li');
  li.className = 'audit-run-li';
  const ax = auditState();
  if (ax.selected === run.id) li.classList.add('selected');

  const top = document.createElement('div');
  top.className = 'audit-run-top';

  const sb = statusBadge(run.status);
  const status = document.createElement('span');
  status.className = 'exec-run-badge ' + sb.cls;
  status.textContent = sb.text;

  const mm = modeMeta(run.mode);
  const mode = document.createElement('span');
  mode.className = 'audit-mode-badge ' + mm.cls;
  mode.textContent = mm.label;

  const when = document.createElement('span');
  when.className = 'audit-run-when muted small';
  const params = paramsSummary(run.params);
  when.textContent = fmtLocalDateTime(run.started_at) + (params ? ' · ' + params : '');

  top.append(status, mode, when);

  const summary = document.createElement('p');
  summary.className = 'audit-run-funnel muted small';
  summary.textContent = funnelSummary(run.funnel);

  li.append(top, summary);
  li.addEventListener('click', function () { selectRun(run.id); });
  return li;
}

// A resync/reprocess maintenance marker, visually distinct from review runs.
function syncListItem(sync) {
  const li = document.createElement('li');
  li.className = 'audit-sync-li';

  const tag = document.createElement('span');
  tag.className = 'audit-sync-tag';
  tag.textContent = sync.source === 'reprocess' ? '♻️ Rebuild' : '⟳ Resync';

  const when = document.createElement('span');
  when.className = 'audit-run-when muted small';
  when.textContent = fmtLocalDateTime(sync.ran_at);

  const delta = document.createElement('span');
  delta.className = 'audit-sync-delta muted small';
  const chatBit = sync.chats_added ? ` · +${sync.chats_added} chat${sync.chats_added > 1 ? 's' : ''}` : '';
  delta.textContent = `+${sync.messages_added} msg${sync.messages_added === 1 ? '' : 's'}${chatBit}`;

  li.append(tag, when, delta);
  return li;
}

function renderRuns() {
  const ax = auditState();
  els.auditRuns.textContent = '';
  els.auditRunsEmpty.hidden = ax.runs.length > 0 || ax.syncs.length > 0;
  for (const run of ax.runs) els.auditRuns.appendChild(runListItem(run));
  for (const sync of ax.syncs) els.auditRuns.appendChild(syncListItem(sync));
}

// ----------------------------------------------------------- run detail

function funnelCells(run) {
  const f = run.funnel || {};
  const cells = [
    { label: 'Synced', value: f.messages_synced },
    { label: 'Monitored', value: f.chats_monitored },
    { label: 'Reviewed', value: f.chats_reviewed },
    { label: 'Stage 1', value: f.stage1_passed },
    { label: 'LLM', value: f.stage2_llm_calls },
    { label: 'Actionable', value: f.actionable },
    { label: 'Notify', value: run.notification_status },
  ];
  if (f.voice_transcribed) cells.push({ label: 'Transcribed', value: f.voice_transcribed });
  if (f.voice_failed) cells.push({ label: 'Voice failed', value: f.voice_failed });
  if (f.voice_skipped_old) cells.push({ label: 'Voice skipped', value: f.voice_skipped_old });
  return cells;
}

function renderFunnel(run) {
  const box = els.auditFunnel;
  box.textContent = '';
  for (const cell of funnelCells(run)) {
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

// A labelled <pre> block; skipped entirely when there's nothing to show so the
// trace stays compact. Text goes in via textContent (privacy + no markup eval).
function traceField(title, text) {
  if (text === null || text === undefined || text === '') return null;
  const wrap = document.createElement('div');
  wrap.className = 'audit-field';
  const h = document.createElement('h5');
  h.className = 'audit-field-title';
  h.textContent = title;
  const pre = document.createElement('pre');
  pre.className = 'codebox audit-pre';
  pre.textContent = typeof text === 'string' ? text : JSON.stringify(text, null, 2);
  wrap.append(h, pre);
  return wrap;
}

function traceBlock(t) {
  const det = document.createElement('details');
  det.className = 'audit-trace card coding-card';

  const sum = document.createElement('summary');
  sum.className = 'coding-summary';
  const row = document.createElement('div');
  row.className = 'coding-summary-row';
  const name = document.createElement('span');
  name.className = 'coding-summary-title';
  name.textContent = t.display_name;
  const am = actionMeta(t.final_action);
  const verdict = document.createElement('span');
  verdict.className = 'audit-verdict ' + am.cls;
  verdict.textContent = am.label;
  row.append(name, verdict);
  sum.appendChild(row);
  det.appendChild(sum);

  const body = document.createElement('div');
  body.className = 'audit-trace-body';

  // Stage progress line: did it pass Stage 1, was the LLM called?
  const stages = document.createElement('p');
  stages.className = 'muted small';
  const s1 = t.stage1_passed ? 'Stage 1 ✓' : 'Stage 1 ✗ (filtered)';
  const s2 = t.llm_called ? 'LLM called' : 'LLM skipped';
  stages.textContent = `${s1} · ${s2}`;
  body.appendChild(stages);

  const roots = Array.isArray(t.stage1_roots) ? t.stage1_roots.join(', ') : t.stage1_roots;
  const fields = [
    traceField('Input messages', t.input_text),
    traceField('Stage-1 roots triggered', roots),
    traceField('LLM system prompt', t.llm_system_prompt),
    traceField('LLM user prompt', t.llm_user_prompt),
    traceField('Raw LLM response', t.llm_raw_response),
    traceField('Parsed verdict', t.parsed_result),
    traceField('Telegram text', t.telegram_text),
    traceField('Error', t.error),
  ];
  for (const f of fields) if (f) body.appendChild(f);

  det.appendChild(body);
  return det;
}

function renderDetail(data) {
  const run = data.run;
  els.auditDetailCard.hidden = false;
  const mm = modeMeta(run.mode);
  els.auditDetailTitle.textContent = `${mm.label} run #${run.id}`;

  const bits = [run.status];
  if (run.started_at) bits.push('started ' + fmtLocalDateTime(run.started_at));
  const params = paramsSummary(run.params);
  if (params) bits.push(params);
  if (run.error) bits.push('error: ' + run.error);
  els.auditDetailMeta.textContent = bits.join(' · ');

  renderFunnel(run);

  els.auditTraces.textContent = '';
  const traces = data.traces || [];
  els.auditTracesEmpty.hidden = traces.length > 0;
  for (const t of traces) els.auditTraces.appendChild(traceBlock(t));

  // Bring the detail into view on a phone after tapping a run up the list.
  els.auditDetailCard.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

async function selectRun(runId) {
  const ax = auditState();
  ax.selected = runId;
  renderRuns();
  try {
    const data = await jsonApi(`/api/audit/runs/${runId}`);
    ax.detail = data;
    renderDetail(data);
  } catch (_) {
    /* transient — the refresh button retries */
  }
}

function closeDetail() {
  const ax = auditState();
  ax.selected = null;
  ax.detail = null;
  els.auditDetailCard.hidden = true;
  renderRuns();
}

// ----------------------------------------------------------- fetch + wire

export async function fetchAudit() {
  let data;
  try {
    data = await jsonApi('/api/audit/runs');
  } catch (_) {
    return;  // 401 flips the login overlay; stay quiet otherwise.
  }
  const ax = auditState();
  ax.runs = data.runs || [];
  ax.syncs = data.syncs || [];
  renderRuns();

  // If a previously-selected run is gone (e.g. after a reprocess reset), drop it.
  if (ax.selected && !ax.runs.some(function (r) { return r.id === ax.selected; })) {
    closeDetail();
  }
}

export function wireAudit() {
  els.auditRefresh.addEventListener('click', function () { fetchAudit().catch(function () {}); });
  els.auditDetailClose.addEventListener('click', closeDetail);
  els.auditDetailCard.hidden = true;
}
