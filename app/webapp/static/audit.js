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
import { fmtLocalDateTime, kindLabel, renderFunnelCells, renderSourceFunnels } from './format.js';

function auditState() { return state.audit; }

const FAMILY_KINDS = ['traffic-check', 'calendar-scan'];

function isFamilyKind(kind) { return FAMILY_KINDS.indexOf(kind) !== -1; }

// Kind filter (#163): 'messages' groups the message-pipeline kinds; the family
// checks filter individually.
function matchesKindFilter(run, filter) {
  if (!filter || filter === 'all') return true;
  if (filter === 'messages') return !isFamilyKind(run.kind);
  return run.kind === filter;
}

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
  actionable: { label: 'Actionable', cls: 'act' },
  not_actionable: { label: 'Not actionable', cls: 'muted' },
  contract_error: { label: 'Contract error', cls: 'err' },
  llm_truncated: { label: 'LLM truncated', cls: 'err' },
};

function actionMeta(action) { return ACTION_META[action] || { label: action || '?', cls: 'muted' }; }

// One compact line summarizing the run for the list: the funnel for message
// runs, the structured payload counts for family checks (#163).
function runSummaryLine(run) {
  if (isFamilyKind(run.kind)) {
    const s = run.summary || {};
    if (run.kind === 'traffic-check') {
      return `${(s.checked || []).length} checked · ${s.alerts || 0} alert(s)`;
    }
    return `${(s.conflicts || []).length} conflict(s) · `
      + `${(s.missing_locations || s.unknown_locations || []).length} missing location(s)`;
  }
  const f = run.funnel;
  if (!f) return '';
  return [
    `${f.messages_synced} synced`,
    `${f.chats_monitored} monitored`,
    ...(f.transcriptions ? [`${f.transcriptions} voice`] : []),
    `${f.stage1_passed} Stage 1`,
    `${f.stage2_llm_calls} LLM`,
    `${f.actionable} actionable`,
  ].join(' · ');
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

  const name = document.createElement('span');
  name.className = 'exec-run-name small';
  name.textContent = kindLabel(run.kind);

  const mm = modeMeta(run.mode);
  const mode = document.createElement('span');
  mode.className = 'audit-mode-badge ' + mm.cls;
  mode.textContent = mm.label;

  const when = document.createElement('span');
  when.className = 'audit-run-when muted small';
  const params = paramsSummary(run.params);
  when.textContent = fmtLocalDateTime(run.started_at) + (params ? ' · ' + params : '');

  top.append(status, name, mode, when);

  const summary = document.createElement('p');
  summary.className = 'audit-run-funnel muted small';
  summary.textContent = runSummaryLine(run);

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
  tag.textContent = sync.source === 'reprocess' ? 'Rebuild' : 'Resync';

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
  const shown = ax.runs.filter(function (r) { return matchesKindFilter(r, ax.kindFilter); });
  els.auditRunsEmpty.hidden = shown.length > 0;
  for (const run of shown) els.auditRuns.appendChild(runListItem(run));
}

// ----------------------------------------------------------- run detail

function funnelCells(run) {
  const f = run.funnel || {};
  return [
    { label: 'Synced', value: f.messages_synced },
    { label: 'Monitored', value: f.chats_monitored },
    { label: 'Reviewed', value: f.chats_reviewed },
    { label: 'Transcribed', value: f.transcriptions },
    { label: 'Stage 1', value: f.stage1_passed },
    { label: 'LLM', value: f.stage2_llm_calls },
    { label: 'Actionable', value: f.actionable },
    { label: 'Notify', value: run.notification_status },
  ];
}

function renderFunnel(run) {
  renderFunnelCells(els.auditFunnel, funnelCells(run));
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

// One message inside a chat trace: its text plus why it did / didn't trigger.
// Stage-1 shows the exact keyword roots that message matched (or "no keyword");
// the LLM column is shown only when the LLM ran, marking whether the model
// flagged this message as evidence. Eliminates the "which ones triggered?" black
// box (#12). All content via textContent (privacy + no markup eval).
function messageRow(m, llmCalled, evidence) {
  const li = document.createElement('li');
  li.className = 'audit-msg';

  const head = document.createElement('div');
  head.className = 'audit-msg-head';

  const sender = document.createElement('span');
  sender.className = 'audit-msg-sender small';
  sender.textContent = m.sender || 'unknown';
  head.appendChild(sender);

  // A voice badge marks a note whose text is the transcription fed into
  // analysis (#36).
  if (m.type === 'voice') {
    const voice = document.createElement('span');
    voice.className = 'audit-msg-badge muted';
    voice.textContent = 'voice';
    head.appendChild(voice);
  }

  const roots = Array.isArray(m.roots) ? m.roots : [];
  const buckets = Array.isArray(m.buckets) ? m.buckets : [];
  if (buckets.length) {
    const bucket = document.createElement('span');
    bucket.className = 'audit-msg-badge act';
    bucket.textContent = buckets.join(', ');
    head.appendChild(bucket);
  }
  const s1 = document.createElement('span');
  s1.className = 'audit-msg-badge ' + (roots.length ? 'act' : 'muted');
  s1.textContent = roots.length ? roots.join(', ') : 'no keyword';
  head.appendChild(s1);

  if (llmCalled) {
    const flagged = evidence.some(function (e) { return String(e) === String(m.id); });
    const s2 = document.createElement('span');
    s2.className = 'audit-msg-badge ' + (flagged ? 'act' : 'muted');
    s2.textContent = flagged ? 'LLM flagged' : 'LLM: not flagged';
    head.appendChild(s2);
  }

  const text = document.createElement('p');
  text.className = 'audit-msg-text';
  text.textContent = m.text || '(no text)';

  li.append(head, text);
  return li;
}

function messagesList(t) {
  const msgs = Array.isArray(t.messages) ? t.messages : [];
  if (!msgs.length) return null;
  const evidence = (t.parsed_result && Array.isArray(t.parsed_result.evidence_message_ids))
    ? t.parsed_result.evidence_message_ids : [];
  const wrap = document.createElement('div');
  wrap.className = 'audit-field';
  const h = document.createElement('h5');
  h.className = 'audit-field-title';
  h.textContent = `Messages (${msgs.length})`;
  const ul = document.createElement('ul');
  ul.className = 'audit-msg-list';
  for (const m of msgs) ul.appendChild(messageRow(m, t.llm_called, evidence));
  wrap.append(h, ul);
  return wrap;
}

function traceBlock(t) {
  // A vendored disclosure card (card--collapsible): title + verdict in the
  // summary's main cluster, chevron pinned right.
  const det = document.createElement('details');
  det.className = 'audit-trace card card--collapsible';

  const sum = document.createElement('summary');
  sum.className = 'collapse-summary';
  const main = document.createElement('span');
  main.className = 'collapse-main';
  const name = document.createElement('h3');
  name.className = 'collapse-title';
  name.textContent = t.display_name;
  const am = actionMeta(t.final_action);
  const verdict = document.createElement('span');
  verdict.className = 'audit-verdict ' + am.cls;
  verdict.textContent = am.label;
  main.append(name, verdict);
  const chevron = document.createElement('span');
  chevron.className = 'collapse-chevron';
  chevron.setAttribute('aria-hidden', 'true');
  chevron.textContent = '›';
  sum.append(main, chevron);
  det.appendChild(sum);

  const body = document.createElement('div');
  body.className = 'collapse-body audit-trace-body';

  // Stage progress line: did it pass Stage 1, was the LLM called?
  const stages = document.createElement('p');
  stages.className = 'muted small';
  const source = t.source === 'gmail' ? 'Gmail' : 'WhatsApp';
  const s1 = t.stage1_passed ? 'Stage 1 passed' : 'Stage 1 filtered';
  const s2 = t.llm_called ? 'LLM called' : 'LLM skipped';
  stages.textContent = `${source} · ${s1} · ${s2}`;
  body.appendChild(stages);

  // Per-message breakdown when the trace carries it (#12); older rows fall back
  // to the rendered input blob so historical traces still render.
  const perMessage = messagesList(t);
  const roots = Array.isArray(t.stage1_roots) ? t.stage1_roots.join(', ') : t.stage1_roots;
  const buckets = Array.isArray(t.stage1_buckets)
    ? t.stage1_buckets.join(', ') : t.stage1_buckets;
  const fields = [
    perMessage || traceField('Input messages', t.input_text),
    traceField('Stage-1 buckets matched', buckets),
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

// Family-check drill-down (#163): the run's structured payload IS the trace —
// show the headline counts as funnel cells and the full payload verbatim.
function renderFamilyDetail(run) {
  const s = run.summary || {};
  const cells = run.kind === 'traffic-check'
    ? [
      { label: 'Checked', value: (s.checked || []).length },
      { label: 'Alerts', value: s.alerts },
      { label: 'Status', value: s.status },
    ]
    : [
      { label: 'Conflicts', value: (s.conflicts || []).length },
      { label: 'Missing loc.', value: (s.missing_locations || s.unknown_locations || []).length },
      { label: 'Status', value: s.status },
    ];
  renderFunnelCells(els.auditFunnel, cells);
  renderSourceFunnels(els.auditSourceFunnel, {});
  els.auditTraces.textContent = '';
  // Calendar-sync runs carry a per-event decision trace + the sent summary
  // (#168) — render those readably above the raw payload.
  const summaryMsg = s.summary && s.summary.text
    ? traceField(`Summary message (${s.summary.status || 'unknown'})`, s.summary.text)
    : null;
  if (summaryMsg) els.auditTraces.appendChild(summaryMsg);
  const decisions = (s.decisions || []).map((d) => {
    const when = fmtLocalDateTime(d.start);
    const flags = [];
    if (d.assumed) flags.push('missing location — assumed home');
    if (d.commute) flags.push('commute');
    return `${d.person} · ${when} · "${d.event}" → ${d.kind}`
      + ` (${d.source})${flags.length ? ' [' + flags.join(', ') + ']' : ''}`;
  });
  const decisionsBlock = decisions.length
    ? traceField(`Event decisions (${decisions.length})`, decisions.join('\n'))
    : null;
  if (decisionsBlock) els.auditTraces.appendChild(decisionsBlock);
  // Live phone-position coverage judgment (#177) — derived values only, the
  // payload never carries coordinates.
  const live = (s.live_coverage || []).map((c) => {
    if (!c.assessed) {
      return `${c.person}: no live fix (${c.presence_status}) — `
        + `calendar inference stands for ${(c.windows || []).join(', ')}`;
    }
    const whereabouts = c.at_home ? 'at home' : `~${c.eta_min} min from home`;
    return `${c.person} ${whereabouts} → '${c.window}' `
      + `${c.feasible ? 'reachable' : 'AT RISK'}`
      + ` (margin ${c.margin_min} min, fix ${c.presence_age_min} min old)`;
  });
  const liveBlock = live.length
    ? traceField(`Live coverage (${live.length})`, live.join('\n'))
    : null;
  if (liveBlock) els.auditTraces.appendChild(liveBlock);
  const payload = traceField('Run payload', run.summary);
  if (payload) els.auditTraces.appendChild(payload);
  els.auditTracesEmpty.hidden = !!payload;
  if (!payload) els.auditTracesEmpty.textContent = 'No payload recorded for this run.';
}

function renderDetail(data) {
  const run = data.run;
  els.auditDetailCard.hidden = false;
  const mm = modeMeta(run.mode);
  const kindBit = isFamilyKind(run.kind) ? kindLabel(run.kind) + ' — ' : '';
  // "Live run #40" / "Dry run #3" — don't double the word when the mode label
  // already ends in "run".
  els.auditDetailTitle.textContent = kindBit + (mm.label.toLowerCase().endsWith('run')
    ? `${mm.label} #${run.id}`
    : `${mm.label} run #${run.id}`);

  const bits = [run.status];
  if (run.started_at) bits.push('started ' + fmtLocalDateTime(run.started_at));
  const params = paramsSummary(run.params);
  if (params) bits.push(params);
  if (run.error) bits.push('error: ' + run.error);
  els.auditDetailMeta.textContent = bits.join(' · ');

  if (isFamilyKind(run.kind)) {
    renderFamilyDetail(run);
    els.auditDetailCard.scrollIntoView({ behavior: 'smooth', block: 'start' });
    return;
  }

  renderFunnel(run);
  renderSourceFunnels(els.auditSourceFunnel, run.sources || {});

  els.auditTraces.textContent = '';
  const traces = data.traces || [];
  els.auditTracesEmpty.hidden = traces.length > 0;
  // Reconcile the funnel when there's nothing to drill into: a live scan can sync
  // messages yet trace nothing because none landed in a monitored chat. Say so
  // explicitly rather than a bare "no trace" — without surfacing the unmonitored
  // chats' content (scope stays monitored-only, #12).
  if (!traces.length) {
    const synced = (run.funnel && run.funnel.messages_synced) || 0;
    els.auditTracesEmpty.textContent = synced
      ? `${synced} message${synced === 1 ? '' : 's'} synced, but none in a monitored chat — nothing to analyze.`
      : 'No per-chat trace recorded for this run.';
  }
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
    /* transient — re-tapping the run retries */
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
  els.auditDetailClose.addEventListener('click', closeDetail);
  els.auditDetailCard.hidden = true;
  // Kind filter chips (#163): client-side filter over the fetched run list.
  els.auditKindFilter.addEventListener('click', function (ev) {
    const btn = ev.target.closest('.range-tab');
    if (!btn) return;
    auditState().kindFilter = btn.dataset.kind || 'all';
    for (const b of els.auditKindFilter.querySelectorAll('.range-tab')) {
      b.classList.toggle('active', b === btn);
    }
    renderRuns();
  });
}
