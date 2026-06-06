/* Dashboard tab (#9): read-only at-a-glance metrics from /api/dashboard.
 *
 * Six metric cards + a last-scan line + a per-monitored-channel table. Chat
 * names go in via textContent only — never innerHTML (privacy + XSS-safe). */

import { els, state } from './state.js';
import { jsonApi } from './api.js';

// Per-channel "Last msg" column: compact, no year ("06-06 12:47"). The last-scan
// card below still uses the full timestamp via fmtTsFull.
function fmtTs(ts) {
  if (!ts) return '—';
  return String(ts).replace('T', ' ').slice(5, 16);
}
function fmtTsFull(ts) {
  if (!ts) return '—';
  return String(ts).replace('T', ' ').slice(0, 16);
}

// Thousands separator with a period (29999 → "29.999"), deterministic across
// browsers/locales.
function fmtNum(n) {
  return String(n).replace(/\B(?=(\d{3})+(?!\d))/g, '.');
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
    name.textContent = ch.name;
    const count = document.createElement('td');
    count.className = 'num';
    count.textContent = fmtNum(ch.count);
    const ts = document.createElement('td');
    ts.className = 'when';
    ts.textContent = fmtTs(ch.last_message_at);
    tr.append(name, count, ts);
    body.appendChild(tr);
  }
}
