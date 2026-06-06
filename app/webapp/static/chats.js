/* Chats tab (#10): pick which chats are watched, peek at recent history.
 *
 * Filtered into one of three buckets (Monitored | Ignored | All) + a name
 * search; "All" is capped at CHATS_RENDER_CAP rows so a ~900-chat account stays
 * usable on a phone. Each row is three lines (name · count + last-msg time ·
 * preview) with a single watch toggle on the right — lit when monitored, dim
 * otherwise; tapping it monitors (baselining the cursor server side) or ignores.
 * The history overlay loads the most recent messages and lazily pages older ones
 * in as you scroll up. All chat-derived text goes in via textContent. */

import { els, state, CHATS_RENDER_CAP } from './state.js';
import { jsonApi, toast } from './api.js';

// Thousands separator with a period (29999 → "29.999"), locale-independent.
function fmtNum(n) {
  return String(n).replace(/\B(?=(\d{3})+(?!\d))/g, '.');
}
// Full timestamp incl. year: "2026-06-06T12:47:19Z" → "2026-06-06 12:47". The
// chat list keeps the year (old chats from 2022/2023 should be obvious); only
// the Dashboard's monitored table drops it, since those are always recent.
function fmtTsFull(ts) {
  if (!ts) return '—';
  return String(ts).replace('T', ' ').slice(0, 16);
}

const HISTORY_PAGE = 30;

// The label shown for a chat: the operator alias takes precedence, with the
// connector-derived name kept in parentheses so both are visible — e.g.
// "Tom (+44123…)". Without an alias it's just the derived name.
function chatLabel(c) {
  return c.alias ? c.alias + ' (' + c.name + ')' : c.name;
}

export async function fetchChats() {
  let data;
  try {
    data = await jsonApi('/api/chats');
  } catch (exc) {
    return; // 401 flips the login overlay in api.js; stay quiet otherwise.
  }
  state.chats = data.chats || [];
  render();
}

function visibleChats() {
  const q = state.chatsSearch.trim().toLowerCase();
  return state.chats.filter(function (c) {
    // Two states that matter: monitored, or not. A chat is "ignored" by default
    // (never-touched 'discovered' chats included), so the Ignored bucket is
    // simply everything that isn't monitored.
    if (state.chatsFilter === 'monitored' && c.status !== 'monitored') return false;
    if (state.chatsFilter === 'ignored' && c.status === 'monitored') return false;
    if (q && !chatLabel(c).toLowerCase().includes(q)) return false;
    return true;
  });
}

function render() {
  const all = visibleChats();
  const shown = all.slice(0, CHATS_RENDER_CAP);

  els.chatsList.textContent = '';
  els.chatsEmpty.hidden = all.length > 0;

  if (all.length > shown.length) {
    els.chatsCount.textContent =
      'Showing ' + shown.length + ' of ' + fmtNum(all.length) + ' — search to narrow.';
  } else {
    els.chatsCount.textContent = all.length
      ? fmtNum(all.length) + ' chat' + (all.length === 1 ? '' : 's')
      : '';
  }

  for (const c of shown) els.chatsList.appendChild(row(c));
}

function row(c) {
  const li = document.createElement('li');
  li.className = 'chat-row';

  // Tapping the body (the three text lines) opens the conversation overlay.
  const body = document.createElement('button');
  body.type = 'button';
  body.className = 'chat-main';
  body.addEventListener('click', function () { openHistory(c); });

  const name = document.createElement('span');
  name.className = 'chat-name';
  name.title = chatLabel(c);
  name.textContent = chatLabel(c);

  const meta = document.createElement('span');
  meta.className = 'chat-meta';
  meta.textContent = fmtNum(c.count) + ' msgs · ' + fmtTsFull(c.last_message_at);

  const sub = document.createElement('span');
  sub.className = 'chat-sub';
  sub.textContent = c.last_message_text ? String(c.last_message_text) : '—';

  body.append(name, meta, sub);

  // Single watch toggle: lit/active when monitored, dim otherwise.
  const actions = document.createElement('div');
  actions.className = 'chat-actions';
  const watch = document.createElement('button');
  watch.type = 'button';
  watch.className = 'chat-watch';
  const monitored = c.status === 'monitored';
  watch.classList.toggle('active', monitored);
  watch.setAttribute('aria-pressed', monitored ? 'true' : 'false');
  watch.title = monitored ? 'Monitoring — tap to ignore' : 'Not monitored — tap to monitor';
  watch.textContent = '🔬';
  watch.addEventListener('click', function () {
    setStatus(c, monitored ? 'ignored' : 'monitored');
  });
  actions.appendChild(watch);

  li.append(body, actions);
  return li;
}

async function setStatus(chat, status) {
  try {
    const res = await jsonApi('/api/chats/' + chat.id + '/status', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ status }),
    });
    chat.status = res.status;
    toast(status === 'monitored'
      ? (res.baselined ? 'Now monitoring — baselined to new messages.' : 'Now monitoring.')
      : 'No longer monitoring.', 'good');
    render();
  } catch (exc) {
    toast('Update failed: ' + (exc.message || exc), 'error');
  }
}

// --------------------------------------------------------- history overlay
const hist = {
  chat: null, chatId: null, oldestTs: null, oldestId: null, hasMore: false, loading: false,
};

function histMsg(m) {
  const item = document.createElement('div');
  item.className = 'hist-msg';
  const meta = document.createElement('div');
  meta.className = 'hist-meta';
  meta.textContent = (m.sender || '—') + ' · ' + fmtTsFull(m.ts);
  const text = document.createElement('div');
  text.className = 'hist-text';
  text.textContent = m.text != null ? m.text : '(' + (m.type || 'non-text') + ')';
  item.append(meta, text);
  return item;
}

// The API returns a page oldest→newest; the overlay shows newest-first, so each
// page is appended reversed and the oldest message in the page becomes the
// cursor for the next (older) page loaded when scrolling to the bottom.
function appendPage(msgs) {
  for (let i = msgs.length - 1; i >= 0; i--) els.historyBody.appendChild(histMsg(msgs[i]));
  if (msgs.length) { hist.oldestTs = msgs[0].ts; hist.oldestId = msgs[0].id; }
}

async function openHistory(chat) {
  els.historyTitle.textContent = chatLabel(chat);
  els.historyBody.textContent = '';
  els.historyEmpty.hidden = true;
  els.historyOverlay.hidden = false;
  hist.chat = chat;
  hist.chatId = chat.id;
  hist.oldestTs = null;
  hist.oldestId = null;
  hist.hasMore = false;
  hist.loading = true;

  let data;
  try {
    data = await jsonApi('/api/chats/' + chat.id + '/history?limit=' + HISTORY_PAGE);
  } catch (exc) {
    els.historyEmpty.hidden = false;
    els.historyEmpty.textContent = 'Could not load history.';
    hist.loading = false;
    return;
  }

  const msgs = data.messages || [];
  els.historyEmpty.hidden = msgs.length > 0;
  appendPage(msgs);
  hist.hasMore = !!data.has_more;
  hist.loading = false;
  els.historyBody.scrollTop = 0; // newest at the top
}

async function loadOlder() {
  if (!hist.hasMore || hist.loading || hist.chatId == null) return;
  hist.loading = true;
  let data;
  try {
    data = await jsonApi(
      '/api/chats/' + hist.chatId + '/history?limit=' + HISTORY_PAGE +
      '&before_ts=' + encodeURIComponent(hist.oldestTs) +
      '&before_id=' + encodeURIComponent(hist.oldestId)
    );
  } catch (exc) {
    hist.loading = false;
    return;
  }
  // Older messages append below the current oldest; the viewport stays put.
  appendPage(data.messages || []);
  hist.hasMore = !!data.has_more;
  hist.loading = false;
}

function closeHistory() {
  els.historyOverlay.hidden = true;
  els.historyBody.textContent = '';
  hist.chat = null;
  hist.chatId = null;
}

// Rename: set or clear the operator alias for the chat in the open overlay. The
// derived name stays in the DB (and the parenthesized label); the alias is the
// human-friendly override that shows first — useful when the connector could only
// resolve a bare number (e.g. an unsaved contact).
async function renameChat() {
  const chat = hist.chat;
  if (!chat) return;
  const next = window.prompt('Alias for this chat (blank to clear):', chat.alias || '');
  if (next === null) return; // cancelled
  try {
    const res = await jsonApi('/api/chats/' + chat.id + '/alias', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ alias: next }),
    });
    chat.alias = res.alias;
    els.historyTitle.textContent = chatLabel(chat);
    render();
    toast(res.alias ? 'Alias saved.' : 'Alias cleared.', 'good');
  } catch (exc) {
    toast('Rename failed: ' + (exc.message || exc), 'error');
  }
}

// --------------------------------------------------------- wiring
export function wireChats() {
  els.chatsRefresh.addEventListener('click', function () { fetchChats().catch(function () {}); });
  els.chatsFilterMonitored.addEventListener('click', function () { setFilter('monitored'); });
  els.chatsFilterIgnored.addEventListener('click', function () { setFilter('ignored'); });
  els.chatsFilterAll.addEventListener('click', function () { setFilter('all'); });
  // Search lives behind a 🔍 button (App Launcher style); reveal + focus on tap.
  els.chatsSearchToggle.addEventListener('click', function () {
    const show = els.chatsSearch.hidden;
    els.chatsSearch.hidden = !show;
    els.chatsSearchToggle.classList.toggle('active', show);
    if (show) {
      els.chatsSearch.focus();
    } else if (state.chatsSearch) {
      els.chatsSearch.value = '';
      state.chatsSearch = '';
      render();
    }
  });
  els.chatsSearch.addEventListener('input', function () {
    state.chatsSearch = els.chatsSearch.value;
    render();
  });
  els.historyRename.addEventListener('click', function () { renameChat().catch(function () {}); });
  els.historyClose.addEventListener('click', closeHistory);
  els.historyOverlay.addEventListener('click', function (ev) {
    if (ev.target === els.historyOverlay) closeHistory();
  });
  // Newest is at the top; scrolling to the bottom pages in older messages.
  els.historyBody.addEventListener('scroll', function () {
    const b = els.historyBody;
    if (b.scrollTop + b.clientHeight >= b.scrollHeight - 48) loadOlder().catch(function () {});
  });
}

function setFilter(filter) {
  state.chatsFilter = filter;
  els.chatsFilterMonitored.classList.toggle('active', filter === 'monitored');
  els.chatsFilterIgnored.classList.toggle('active', filter === 'ignored');
  els.chatsFilterAll.classList.toggle('active', filter === 'all');
  render();
}
