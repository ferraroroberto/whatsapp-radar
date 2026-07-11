/* Dashboard tab (#9): read-only at-a-glance metrics from /api/dashboard.
 *
 * Six metric cards + a last-scan line + a per-monitored-channel table. Chat
 * names go in via textContent only — never innerHTML (privacy + XSS-safe). */

import { els, state } from './state.js';
import { jsonApi } from './api.js';
import { fmtLocalDateTime, fmtNum } from './format.js';

// Per-channel "Last msg" column: compact LOCAL time, no year ("06-07 14:47").
// The last-scan card below uses the full local timestamp via fmtTsFull.
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

function render(d) {
  els.mChannels.textContent = fmtNum(d.chats.monitored);
  els.mMessages.textContent = fmtNum(d.messages.total);
  els.mScans.textContent = fmtNum(d.scans.count);
  els.mBacklog.textContent = fmtNum(d.scans.messages_since_last);
  els.mActionable.textContent = fmtNum(d.alerts.actionable);
  els.mNotified.textContent = fmtNum(d.alerts.notifications_sent);

  // Two-line last-scan card: title + date on line 1, the report on line 2.
  const last = d.scans.last;
  if (last) {
    els.lastRunWhen.textContent = fmtTsFull(last.started_at);
    els.lastRunSummary.textContent =
      last.mode + ' · ' + last.status + ' · ' + fmtNum(last.messages_synced) + ' msgs · ' +
      fmtNum(last.actionable) + ' actionable · ' + (last.notification_status || 'none');
  } else {
    els.lastRunWhen.textContent = '';
    els.lastRunSummary.textContent = 'No scans yet.';
  }

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
    const row = document.createElement('div');
    row.className = 'source-summary-row';
    const name = document.createElement('span');
    name.className = 'source-badge source-' + source.source;
    name.textContent = source.source === 'gmail' ? 'Gmail' : 'WhatsApp';
    const detail = document.createElement('span');
    detail.className = 'muted small';
    detail.textContent = fmtNum(source.messages) + ' stored · ' + fmtNum(source.monitored) +
      ' monitored · latest ' + fmtTsFull(source.latest_message_at);
    row.append(name, detail);
    els.dashSources.appendChild(row);
  }
}
