/* Run trace (#12): the per-run decision record — the reason the radar is
 * trustworthy. Pure renderers for Activity's run sheet (#373), which shows every
 * run kind through one detail.
 *
 * Read-only view over the persisted trace (#7): for a message run, the per-chat
 * decision record (the input delta, the Stage-1 roots, the exact LLM prompts,
 * the raw response, the parsed verdict, the final action, and the Telegram
 * text); for a family check, its structured payload. All chat names / message
 * text / prompts go in via textContent only — never innerHTML (privacy). */

import { familyKindCells, fmtLocalDateTime, renderFunnelCells, renderSourceFunnels, travelStatusLabel } from './format.js';
import { redactPayload, withheldKeys } from './redact.js';

const FAMILY_KINDS = ['traffic-check', 'calendar-scan'];

export function isFamilyKind(kind) { return FAMILY_KINDS.indexOf(kind) !== -1; }

// Live = a real run that synced + (maybe) delivered; dry_run = replay on stored
// data; review = the legacy process-only verb.
const MODE_LABEL = { live: 'Live', dry_run: 'Dry run', review: 'Process' };

export function modeLabel(mode) { return MODE_LABEL[mode] || mode || '?'; }

// The final per-chat verdict, mapped to a short label + tone for the trace header.
const ACTION_META = {
  actionable: { label: 'Actionable', cls: 'act' },
  not_actionable: { label: 'Not actionable', cls: 'muted' },
  contract_error: { label: 'Contract error', cls: 'err' },
  llm_truncated: { label: 'LLM truncated', cls: 'err' },
};

export function actionMeta(action) { return ACTION_META[action] || { label: action || '?', cls: 'muted' }; }

export function paramsSummary(params) {
  if (!params || typeof params !== 'object') return '';
  const bits = [];
  for (const k of Object.keys(params)) {
    if (params[k] !== null && params[k] !== undefined) bits.push(`${k}=${params[k]}`);
  }
  return bits.join(' ');
}

/* The one funnel (#373): Run and Audit each had their own. `data` is either a
 * DB run (a `funnel` from the run's columns plus `notification_status`) or a
 * live run's parsed `result`, which carries the same funnel keys for the
 * message runs and its own counters for the maintenance verbs. Null when the
 * run has nothing to count yet (a run still in flight). */
export function funnelCells(kind, data) {
  const d = data || {};
  if (isFamilyKind(kind)) return familyKindCells(kind, d);
  if (kind === 'resync') {
    return [
      { label: 'Chats +', value: d.chats_added },
      { label: 'Chats ~', value: d.chats_updated },
      { label: 'Messages +', value: d.messages_added },
    ];
  }
  if (kind === 'reprocess') {
    return [
      { label: 'Chats', value: d.chats_after },
      { label: 'Messages', value: d.messages_after },
      { label: 'Monitored', value: d.monitored_preserved },
      { label: 'Ignored', value: d.ignored_preserved },
      { label: 'Aliases', value: d.aliases_preserved },
    ];
  }
  if (kind === 'notify') return [{ label: 'Notify', value: d.notification_status }];
  const f = d.funnel;
  if (!f) return null;
  return [
    { label: 'Synced', value: f.messages_synced },
    { label: 'Monitored', value: f.chats_monitored },
    { label: 'Reviewed', value: f.chats_reviewed ?? f.chats_with_delta },
    { label: 'Transcribed', value: f.transcriptions },
    { label: 'Stage 1', value: f.stage1_passed },
    { label: 'LLM', value: f.stage2_llm_calls },
    { label: 'Actionable', value: f.actionable },
    { label: 'Notify', value: d.notification_status },
  ];
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

// "2026-08-20 06:00–06:25", collapsing the date on a same-day box.
function timeBox(start, end) {
  const from = fmtLocalDateTime(start);
  const to = fmtLocalDateTime(end);
  return from + '–' + (from.slice(0, 10) === to.slice(0, 10) ? to.slice(11) : to);
}

function appendTravelList(wrap, title, lines) {
  if (!lines.length) return;
  wrap.appendChild(traceField(title + ' (' + lines.length + ')', lines.join('\n')));
}

/* Travel-block sweep (#276), readable above the raw payload dump.
 *
 * Person, leg kind, event title, time box and minutes for each planned block;
 * every removal and every left-alone block with the reason that put it there;
 * every unpriced leg with its `reason` discriminator. `protected` gets its own
 * heading rather than being folded into `keeps` — "we left this alone because
 * we could not price its leg" is a different fact from "we checked it and it
 * was already right", and only one of them is a warning.
 *
 * No calendar id is printed. Lines are keyed on person and leg, as the Family
 * tab's capability rows are; `_block_payload` carries no person and no event
 * title (a block's summary is the configured template, which says nothing), so
 * a removal line is leg + start + reason and nothing more.
 *
 * The `.audit-travel` wrapper carries no styling — it exists so this whole
 * section is addressable as one unit (tests, and any future collapse). */
function travelBlockSection(section) {
  if (!section || typeof section !== 'object') return null;
  const wrap = document.createElement('div');
  wrap.className = 'audit-travel';

  const head = [];
  if (section.status !== 'ok') {
    head.push('Gated: ' + travelStatusLabel(section.status) + '.');
    head.push('No plan was computed, so there are no counts — this is not a sweep that '
      + 'looked and found nothing to do.');
  } else {
    const c = section.counts || {};
    head.push('Mode: ' + (section.dry_run === false
      ? 'live — blocks were written to and deleted from the calendars'
      : 'dry run — planned only, nothing written'));
    head.push('Plan: ' + c.desired + ' leg(s) · ' + c.adds + ' add · ' + c.deletes
      + ' delete · ' + c.keeps + ' kept · ' + c.protected + ' left alone · '
      + c.failures + ' unpriced');
    head.push('Routes calls: ' + section.routes_calls);
    if (section.horizon_start && section.horizon_end) {
      head.push('Horizon: ' + timeBox(section.horizon_start, section.horizon_end));
    }
    const apply = section.apply;
    if (apply) {
      const a = apply.counts || {};
      head.push('Written [' + apply.status + ']: ' + a.inserted + ' inserted · '
        + a.deleted + ' deleted · ' + a.kept + ' kept · ' + a.skipped + ' skipped · '
        + a.backups + ' backup(s)');
      const failed = apply.failures || [];
      if (failed.length) head.push('Write failures: ' + failed.length);
    } else {
      head.push('Written: not recorded — this run stored no apply result.');
    }
  }
  wrap.appendChild(traceField('Travel blocks', head.join('\n')));

  appendTravelList(wrap, 'Travel blocks — planned blocks', (section.adds || []).map((leg) =>
    `${leg.person} · ${leg.leg} · "${leg.event}" · ${timeBox(leg.start, leg.end)}`
    + ` · ${leg.minutes} min`));
  appendTravelList(wrap, 'Travel blocks — planned removals', (section.deletes || []).map((b) =>
    `${b.leg} · ${fmtLocalDateTime(b.start)} — ${b.reason}`));
  appendTravelList(wrap, 'Travel blocks — left alone', (section.protected || []).map((b) =>
    `${b.leg} · ${fmtLocalDateTime(b.start)} — ${b.reason}`));
  appendTravelList(wrap, 'Travel blocks — unpriced legs', (section.failures || []).map((f) =>
    `${f.person} · ${f.leg} · "${f.event}" · ${f.status} (${f.reason})`
    + (f.detail ? ` — ${f.detail}` : '')));
  return wrap;
}

/* Family-check drill-down (#163): the run's structured payload IS the trace —
 * the headline counts as funnel cells and the payload readably, then redacted.
 * `t` holds the sheet's target elements: {funnel, sources, traces, empty}. */
export function renderFamilyTrace(t, run) {
  const s = run.summary || {};
  renderFunnelCells(t.funnel, familyKindCells(run.kind, s));
  renderSourceFunnels(t.sources, {});
  t.traces.textContent = '';
  // Calendar-sync runs carry a per-event decision trace + the sent summary
  // (#168) — render those readably above the raw payload.
  const summaryMsg = s.summary && s.summary.text
    ? traceField(`Summary message (${s.summary.status || 'unknown'})`, s.summary.text)
    : null;
  if (summaryMsg) t.traces.appendChild(summaryMsg);
  const decisions = (s.decisions || []).map((d) => {
    const when = fmtLocalDateTime(d.start);
    const flags = [];
    if (d.assumed) flags.push('missing location — assumed home');
    if (d.commute) flags.push('commute');
    return `${d.person} · ${when} · "${d.event}": ${d.kind}`
      + ` (${d.source})${flags.length ? ' [' + flags.join(', ') + ']' : ''}`;
  });
  const decisionsBlock = decisions.length
    ? traceField(`Event decisions (${decisions.length})`, decisions.join('\n'))
    : null;
  if (decisionsBlock) t.traces.appendChild(decisionsBlock);
  // Live phone-position coverage judgment (#177) — derived values only, the
  // payload never carries coordinates.
  const live = (s.live_coverage || []).map((c) => {
    if (!c.assessed) {
      return `${c.person}: no live fix (${c.presence_status}) — `
        + `calendar inference stands for ${(c.windows || []).join(', ')}`;
    }
    const whereabouts = c.at_home ? 'at home' : `~${c.eta_min} min from home`;
    return `${c.person} ${whereabouts}, '${c.window}': `
      + `${c.feasible ? 'reachable' : 'AT RISK'}`
      + ` (margin ${c.margin_min} min, fix ${c.presence_age_min} min old)`;
  });
  const liveBlock = live.length
    ? traceField(`Live coverage (${live.length})`, live.join('\n'))
    : null;
  if (liveBlock) t.traces.appendChild(liveBlock);
  // The commute travel-block sweep (#276) — above the dump, since the dump is
  // where this record was previously only technically visible.
  const travel = travelBlockSection(s.travel_blocks);
  if (travel) t.traces.appendChild(travel);
  // The dump is redacted by whitelist (#285): it used to serialise the whole
  // payload verbatim, calendar ids and street addresses included, immediately
  // beneath the purpose-built blocks that carefully do not show them.
  const withheld = withheldKeys(run.summary);
  const payload = traceField('Run payload', redactPayload(run.summary));
  if (payload) {
    if (withheld.length) {
      // Named, not merely marked: an operator has to know the dump is not the
      // whole record, and which fields it is missing.
      const note = document.createElement('p');
      note.className = 'audit-redaction-note';
      note.textContent = 'Withheld from this dump (privacy): ' + withheld.join(', ')
        + '. Values show as ⟨withheld⟩.';
      payload.appendChild(note);
    }
    t.traces.appendChild(payload);
  }
  t.empty.hidden = !!payload;
  if (!payload) t.empty.textContent = 'No payload recorded for this run.';
}

/* Message-run drill-down (#12): the funnel, the per-source split and one
 * disclosure per traced chat. Same `t` targets as renderFamilyTrace. */
export function renderMessageTrace(t, run, traces) {
  renderFunnelCells(t.funnel, funnelCells(run.kind, run) || []);
  renderSourceFunnels(t.sources, run.sources || {});
  t.traces.textContent = '';
  const list = traces || [];
  t.empty.hidden = list.length > 0;
  // Reconcile the funnel when there's nothing to drill into: a live scan can sync
  // messages yet trace nothing because none landed in a monitored chat. Say so
  // explicitly rather than a bare "no trace" — without surfacing the unmonitored
  // chats' content (scope stays monitored-only, #12).
  if (!list.length) {
    const synced = (run.funnel && run.funnel.messages_synced) || 0;
    t.empty.textContent = synced
      ? `${synced} message${synced === 1 ? '' : 's'} synced, but none in a monitored chat — nothing to analyze.`
      : 'No per-chat trace recorded for this run.';
  }
  for (const tr of list) t.traces.appendChild(traceBlock(tr));
}
