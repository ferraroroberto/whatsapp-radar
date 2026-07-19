/* Dashboard tab (#9, #165): the app's front door — a per-kind "last activity"
 * grid plus the folded Sources / Monitored-channels detail.
 *
 * The 2×2 grid (WhatsApp · Gmail · Traffic · Calendar) answers "when did each
 * last run and what did it find" at a glance; tapping a card jumps to that
 * run's detail on the Run tab (#163's unified store makes CLI/Jobs runs visible
 * here too). Chat names go in via textContent only — never innerHTML. */

import { els, state } from './state.js';
import { jsonApi } from './api.js';
import { fmtLocalDateTime, fmtNum, fmtRelative, sourceIcon, SOURCE_LABEL } from './format.js';
import { setTab } from './tabs.js';
import { showRun } from './execution.js';

// Per-channel "Last msg" column: compact LOCAL time, no year ("06-07 14:47").
function fmtTs(ts) {
  return fmtLocalDateTime(ts, { withYear: false });
}
function fmtTsFull(ts) {
  return fmtLocalDateTime(ts);
}

export async function fetchDashboard() {
  let data;
  try {
    data = await jsonApi('/api/dashboard');
  } catch (exc) {
    // 401 already flips the login overlay open in api.js; stay quiet otherwise.
    return;
  }
  state.dashboard = data;
  render(data);
}

// The badge distils each card's outcome to one of: never ran · KO · running ·
// N alerts · OK — mapped to a status-color class (design.md: status colors
// signal state only).
function activityBadge(card) {
  if (!card.kind || !card.started_at) return { text: 'never ran', cls: 'muted' };
  if (card.status === 'failed') return { text: 'KO', cls: 'ko' };
  if (card.status === 'running' || card.status === 'pending') return { text: '···', cls: 'run' };
  if (card.alerts > 0) {
    return { text: card.alerts + ' alert' + (card.alerts === 1 ? '' : 's'), cls: 'act' };
  }
  return { text: 'OK', cls: 'ok' };
}

function activityCard(card) {
  const ran = !!(card.kind && card.started_at);
  // A ran card is a button (deep-links to the Run tab); a never-ran card is an
  // inert div — nothing to open.
  const el = document.createElement(ran ? 'button' : 'div');
  el.className = 'activity-card card' + (ran ? '' : ' is-idle');
  if (ran) {
    el.type = 'button';
    el.addEventListener('click', function () {
      setTab('execution');
      showRun({ kind: card.kind, run_id: 'db-' + card.db_run_id });
    });
  }

  // Line 1: icon + kind name, given the full row so a name never truncates.
  const head = document.createElement('div');
  head.className = 'activity-head';
  const ico = sourceIcon(card.source);
  ico.classList.add('activity-icon');
  const name = document.createElement('span');
  name.className = 'activity-name';
  name.textContent = SOURCE_LABEL[card.source] || card.source;
  head.append(ico, name);

  // Line 2: relative last-run time on the left, the outcome badge pinned right.
  const meta = document.createElement('div');
  meta.className = 'activity-meta';
  const when = document.createElement('span');
  when.className = 'activity-when muted small';
  when.textContent = ran ? fmtRelative(card.started_at) : 'no run yet';
  const b = activityBadge(card);
  const badge = document.createElement('span');
  badge.className = 'activity-badge ' + b.cls;
  badge.textContent = b.text;
  meta.append(when, badge);

  const summary = document.createElement('span');
  summary.className = 'activity-summary';
  summary.textContent = card.summary || '—';

  el.append(head, meta, summary);
  return el;
}

function renderActivity(cards) {
  els.dashActivity.textContent = '';
  for (const card of cards || []) els.dashActivity.appendChild(activityCard(card));
}

function render(d) {
  renderActivity(d.last_activity);

  const body = els.dashChannelsBody;
  body.textContent = '';
  const channels = d.messages.per_channel || [];
  els.dashChannelsEmpty.hidden = channels.length > 0;
  for (const ch of channels) {
    const tr = document.createElement('tr');
    const name = document.createElement('td');
    name.className = 'name';
    name.title = ch.name;  // full name on hover/long-press; cell truncates with …
    name.textContent = (ch.source === 'gmail' ? 'Gmail · ' : 'WhatsApp · ') + ch.name;
    const count = document.createElement('td');
    count.className = 'num';
    count.textContent = fmtNum(ch.count);
    const ts = document.createElement('td');
    ts.className = 'when';
    ts.textContent = fmtTs(ch.last_message_at);
    tr.append(name, count, ts);
    body.appendChild(tr);
  }

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
      ' monitored · latest ' + fmtTsFull(source.latest_message_at)));
  }
  // Calendar is a read-only, non-ingesting source (#164/#165): no stored/
  // monitored counts, so its row shows provenance + freshness of the last scan.
  const cal = (d.last_activity || []).find(function (c) { return c.source === 'calendar'; });
  if (cal) {
    els.dashSources.appendChild(sourceRow('calendar', cal.kind && cal.started_at
      ? 'read-only · last scan ' + fmtRelative(cal.started_at)
      : 'read-only · not yet scanned'));
  }
}

// One Sources-card row: an icon+label source badge plus a muted detail line.
function sourceRow(sourceKey, detailText) {
  const row = document.createElement('div');
  row.className = 'source-summary-row';
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
