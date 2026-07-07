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
import { jsonApi, readToken, toast } from './api.js';
import { fmtLocalDateTime } from './format.js';
import { icon } from './_vendored/icons/icons.js';

// A sprite glyph wrapped for insertion next to textContent-only user data.
// Static markup only — never user content — so innerHTML is safe here.
function iconMark(name) {
  const s = document.createElement('span');
  s.className = 'msg-icon';
  s.innerHTML = icon(name);
  return s;
}

// Thousands separator with a period (29999 → "29.999"), locale-independent.
function fmtNum(n) {
  return String(n).replace(/\B(?=(\d{3})+(?!\d))/g, '.');
}
// Full LOCAL timestamp incl. year: "2026-06-06T12:47:19Z" → "2026-06-06 14:47"
// in the operator's time zone. The chat list keeps the year (old chats from
// 2022/2023 should be obvious); the Dashboard's monitored table drops it.
function fmtTsFull(ts) {
  return fmtLocalDateTime(ts);
}

const HISTORY_PAGE = 30;

// Past this many characters a message is worth an on-demand hub summary (#86):
// the Summarize control shows in the overlay and POSTs to the hub's Haiku. Keep
// in sync with _SUMMARIZE_MIN_CHARS in app/webapp/routers/chats.py (the server
// re-checks). Applies to long transcribed voice notes and long typed messages alike.
const SUMMARIZE_MIN_CHARS = 280;

// The label shown for a chat: the operator alias takes precedence, with the
// connector-derived name kept in parentheses so both are visible — e.g.
// "Tom (+44123…)". Without an alias it's just the derived name.
function chatLabel(c) {
  return c.alias ? c.alias + ' (' + c.name + ')' : c.name;
}

// Link role (derived, not stored): a chat is a CHILD when it points at a parent,
// a PARENT when other chats point at it, else STANDALONE. Depth is capped at 1,
// so a chat is never both. Children are hidden from the list and folded into the
// parent's family review (#25).
function childrenOf(parentId) {
  return state.chats.filter(function (c) { return c.parent_chat_id === parentId; });
}
function isChild(c) {
  return c.parent_chat_id != null;
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
    // Linked children never appear as their own row — they are folded into the
    // parent and managed from the parent's overlay (#25).
    if (isChild(c)) return false;
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

  // A parent shows a link-count badge; tapping it opens the overlay with the
  // link panel already expanded so the family can be managed in one tap.
  const kids = childrenOf(c.id);
  if (kids.length) {
    const badge = document.createElement('button');
    badge.type = 'button';
    badge.className = 'chat-link-badge';
    badge.innerHTML = icon('link');
    badge.appendChild(document.createTextNode(String(kids.length)));
    badge.title = kids.length + ' linked chat' + (kids.length === 1 ? '' : 's');
    badge.setAttribute('aria-label', badge.title);
    badge.addEventListener('click', function () { openHistory(c, true); });
    actions.appendChild(badge);
  }

  const watch = document.createElement('button');
  watch.type = 'button';
  watch.className = 'chat-watch';
  const monitored = c.status === 'monitored';
  watch.classList.toggle('active', monitored);
  watch.setAttribute('aria-pressed', monitored ? 'true' : 'false');
  watch.title = monitored ? 'Monitoring — tap to ignore' : 'Not monitored — tap to monitor';
  watch.innerHTML = icon('eye');
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
  // On a merged family view each message carries its origin chat so the operator
  // can tell which number it came from; absent on a single-chat view.
  const who = m.sender || '—';
  meta.textContent = (m.origin ? m.origin + ' · ' : '') + who + ' · ' + fmtTsFull(m.ts);
  const text = document.createElement('div');
  text.className = 'hist-text';
  if (m.type === 'voice') {
    // A mic glyph marks a voice note; once transcribed, m.text *is* the
    // transcript (#36). With retained audio (#86) the marker becomes a
    // tap-to-play/stop control.
    if (m.has_audio) {
      text.append(voicePlayer(m.id), document.createTextNode(' ' + voiceText(m)));
    } else {
      text.append(iconMark('mic'), document.createTextNode(' ' + voiceText(m)));
    }
  } else {
    text.textContent = m.text != null ? m.text : '(' + (m.type || 'non-text') + ')';
  }
  item.append(meta, text);
  // A long message (long voice-note transcript or long typed message) gets an
  // on-demand Summarize control wired to the hub (#86). The server reads the same
  // messages.text, so gate on m.text length here too.
  if (m.text && m.text.length >= SUMMARIZE_MIN_CHARS) item.append(summarizeControl(m.id));
  return item;
}

// An on-demand "Summarize" control for one long message. Tapping it POSTs to the
// hub-backed summarize endpoint and renders the returned summary inline beneath
// the message. The result is cached on first fetch so a second tap just re-shows
// it (toggling visibility) rather than dialling the hub again.
function summarizeControl(id) {
  const wrap = document.createElement('div');
  wrap.className = 'msg-summary-wrap';
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'summarize-action';
  btn.textContent = 'Summarize';
  const out = document.createElement('div');
  out.className = 'msg-summary';
  out.hidden = true;
  let cached = null;
  let busy = false;
  btn.addEventListener('click', async function () {
    if (busy) return;
    if (cached !== null) {  // already fetched — just toggle the panel
      out.hidden = !out.hidden;
      return;
    }
    busy = true;
    btn.disabled = true;
    btn.textContent = 'Summarizing…';
    try {
      const body = await jsonApi('/api/messages/' + id + '/summarize', { method: 'POST' });
      cached = (body && body.summary) || '';
      out.textContent = cached;
      out.hidden = false;
      btn.textContent = 'Summary';
    } catch (exc) {
      toast(String((exc && exc.message) || 'Could not summarize this message.'), 'error');
      btn.textContent = 'Summarize';
    } finally {
      busy = false;
      btn.disabled = false;
    }
  });
  wrap.append(btn, out);
  return wrap;
}

// URL for a voice note's retained audio. The token rides as ?token= so the
// <audio> element (which can't set an Authorization header) authenticates; on
// loopback there's no token and the endpoint bypasses auth anyway.
function audioUrl(id) {
  const t = readToken();
  return '/api/messages/' + id + '/audio' + (t ? '?token=' + encodeURIComponent(t) : '');
}

// A tap-to-play/stop button for one voice note. The <audio> is created lazily on
// first tap (so a long history doesn't open dozens of connections) and reset to
// the start when stopped, so the next tap replays from the top.
function voicePlayer(id) {
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'voice-play';
  // Static sprite markup only, so innerHTML is safe here.
  function face(playing) {
    btn.innerHTML = icon('mic') + icon(playing ? 'square' : 'play');
    btn.setAttribute('aria-label', playing ? 'Stop voice note' : 'Play voice note');
  }
  face(false);
  let audio = null;
  btn.addEventListener('click', function () {
    if (!audio) {
      audio = new Audio(audioUrl(id));
      audio.addEventListener('ended', function () { face(false); });
      audio.addEventListener('error', function () {
        face(false);
        toast('Could not play this voice note.', 'error');
      });
    }
    if (audio.paused) {
      audio.play().then(function () { face(true); }).catch(function () {});
    } else {
      audio.pause();
      audio.currentTime = 0;
      face(false);
    }
  });
  return btn;
}

// What to show for a voice note: the transcript when done, else an honest label
// for its transcription state (pending / failed / skipped / not enabled).
function voiceText(m) {
  if (m.transcription_status === 'done' && m.text) return m.text;
  if (m.transcription_status === 'failed') return '[voice note — transcription failed]';
  if (m.transcription_status === 'skipped_old') return '[voice note — not transcribed]';
  if (m.transcription_status === 'pending') return '[voice note — awaiting transcription]';
  return m.text != null ? m.text : '[voice note]';
}

// The API returns a page oldest→newest; the overlay shows newest-first, so each
// page is appended reversed and the oldest message in the page becomes the
// cursor for the next (older) page loaded when scrolling to the bottom.
function appendPage(msgs) {
  for (let i = msgs.length - 1; i >= 0; i--) els.historyBody.appendChild(histMsg(msgs[i]));
  if (msgs.length) { hist.oldestTs = msgs[0].ts; hist.oldestId = msgs[0].id; }
}

async function openHistory(chat, openPanel) {
  els.historyTitle.textContent = chatLabel(chat);
  els.historyBody.textContent = '';
  els.historyEmpty.hidden = true;
  if (!els.historyOverlay.open) els.historyOverlay.showModal();
  hist.chat = chat;
  hist.chatId = chat.id;
  // Panel starts collapsed on a normal open; the link badge opens it expanded.
  panelOpen = !!openPanel;
  syncLinkPanel();
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

// Reset the overlay's contents once the native <dialog> has closed — runs for
// every close path (button, backdrop tap, Esc) via the 'close' event.
function onHistoryClosed() {
  els.historyBody.textContent = '';
  els.historyLinkPanel.hidden = true;
  els.historyLinkPanel.textContent = '';
  panelOpen = false;
  hist.chat = null;
  hist.chatId = null;
}

function closeHistory() {
  if (els.historyOverlay.open) els.historyOverlay.close();
}

// ----------------------------------------------------------- link management
// All link maintenance happens inside a chat's overlay. The link button toggles
// a panel whose content depends on the chat's role:
//   standalone → "Link to a parent…" (opens the picker; this chat becomes a child)
//   child      → "Linked to <parent>" + Unlink / Change parent…
//   parent     → its children, each with an Unlink
// The link is keyed on the child, so every mutation targets a child id and the
// server enforces the depth-1 rules.
let panelOpen = false;

function linkBtn(text, onClick) {
  const b = document.createElement('button');
  b.type = 'button';
  b.className = 'link-btn';
  b.textContent = text;
  b.addEventListener('click', onClick);
  return b;
}

function renderLinkPanel(chat) {
  const panel = els.historyLinkPanel;
  panel.textContent = '';
  const kids = childrenOf(chat.id);

  if (chat.parent_chat_id != null) {
    // Child: show its parent with unlink / re-parent.
    const parent = state.chats.find(function (c) { return c.id === chat.parent_chat_id; });
    const status = document.createElement('div');
    status.className = 'link-status';
    status.textContent = 'Linked to: ' + (parent ? chatLabel(parent) : '#' + chat.parent_chat_id);
    const actions = document.createElement('div');
    actions.className = 'link-actions';
    actions.append(
      linkBtn('Unlink', function () { unlinkChat(chat); }),
      linkBtn('Change parent…', function () { openPicker(chat); })
    );
    panel.append(status, actions);
  } else if (kids.length) {
    // Parent: list children, each unlinkable. No "set a parent" — a parent can't
    // itself become a child.
    const status = document.createElement('div');
    status.className = 'link-status';
    status.textContent = 'Linked chats (' + kids.length + '):';
    panel.appendChild(status);
    const ul = document.createElement('ul');
    ul.className = 'link-children';
    for (const k of kids) {
      const li = document.createElement('li');
      const nm = document.createElement('span');
      nm.className = 'link-child-name';
      nm.textContent = chatLabel(k);
      const x = linkBtn('Unlink', function () { unlinkChat(k); });
      x.title = 'Unlink this chat';
      li.append(nm, x);
      ul.appendChild(li);
    }
    panel.appendChild(ul);
  } else {
    // Standalone: offer to fold this chat into a canonical parent.
    const hint = document.createElement('div');
    hint.className = 'link-status muted';
    hint.textContent = 'Not linked. Merge another number for the same person onto a parent chat.';
    const actions = document.createElement('div');
    actions.className = 'link-actions';
    actions.appendChild(linkBtn('Link to a parent…', function () { openPicker(chat); }));
    panel.append(hint, actions);
  }
}

function syncLinkPanel() {
  if (!hist.chat) return;
  if (panelOpen) {
    renderLinkPanel(hist.chat);
    els.historyLinkPanel.hidden = false;
  } else {
    els.historyLinkPanel.hidden = true;
    els.historyLinkPanel.textContent = '';
  }
}

function toggleLinkPanel() {
  if (!hist.chat) return;
  panelOpen = !panelOpen;
  syncLinkPanel();
}

// After any link mutation: refresh the chat list, then reload the open overlay so
// both the merged history and the link panel reflect the new family. If the chat
// itself vanished from the data (shouldn't happen) the overlay just closes.
async function refreshAfterLink() {
  await fetchChats();
  if (!els.historyOverlay.open) return;
  const fresh = state.chats.find(function (c) { return c.id === hist.chatId; });
  if (!fresh) { closeHistory(); return; }
  openHistory(fresh, true).catch(function () {});
}

async function unlinkChat(chat) {
  try {
    await jsonApi('/api/chats/' + chat.id + '/unlink', { method: 'POST' });
    toast('Unlinked.', 'good');
    await refreshAfterLink();
  } catch (exc) {
    toast('Unlink failed: ' + (exc.message || exc), 'error');
  }
}

// ----------------------------------------------------------- parent picker
const picker = { child: null };

function pickerCandidates() {
  const q = els.linkPickerSearch.value.trim().toLowerCase();
  const child = picker.child;
  return state.chats.filter(function (c) {
    if (!child || c.id === child.id) return false;     // never itself
    if (c.parent_chat_id != null) return false;        // target must be top-level
    if (c.id === child.parent_chat_id) return false;   // already this child's parent
    if (q && !chatLabel(c).toLowerCase().includes(q)) return false;
    return true;
  });
}

function renderPicker() {
  const all = pickerCandidates();
  const shown = all.slice(0, CHATS_RENDER_CAP);
  els.linkPickerList.textContent = '';
  els.linkPickerEmpty.hidden = all.length > 0;
  els.linkPickerCount.textContent = all.length > shown.length
    ? 'Showing ' + shown.length + ' of ' + fmtNum(all.length) + ' — search to narrow.'
    : (all.length ? fmtNum(all.length) + ' chat' + (all.length === 1 ? '' : 's') : '');

  for (const c of shown) {
    const li = document.createElement('li');
    li.className = 'chat-row';
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'chat-main';
    const nm = document.createElement('span');
    nm.className = 'chat-name';
    nm.textContent = chatLabel(c);
    const meta = document.createElement('span');
    meta.className = 'chat-meta';
    meta.textContent = fmtNum(c.count) + ' msgs · ' + fmtTsFull(c.last_message_at);
    b.append(nm, meta);
    b.addEventListener('click', function () { doLink(picker.child, c); });
    li.appendChild(b);
    els.linkPickerList.appendChild(li);
  }
}

function openPicker(child) {
  picker.child = child;
  els.linkPickerTitle.textContent = 'Link “' + chatLabel(child) + '” to…';
  els.linkPickerSearch.value = '';
  if (!els.linkPickerOverlay.open) els.linkPickerOverlay.showModal();
  renderPicker();
  els.linkPickerSearch.focus();
}

function onPickerClosed() {
  els.linkPickerList.textContent = '';
  picker.child = null;
}

function closePicker() {
  if (els.linkPickerOverlay.open) els.linkPickerOverlay.close();
}

async function doLink(child, parent) {
  if (!child || !parent) return;
  try {
    await jsonApi('/api/chats/' + child.id + '/link', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ parent_id: parent.id }),
    });
    toast('Linked to ' + chatLabel(parent) + '.', 'good');
    closePicker();
    await refreshAfterLink();
  } catch (exc) {
    toast('Link failed: ' + (exc.message || exc), 'error');
  }
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
  // Search lives behind an icon button (App Launcher style); reveal + focus on tap.
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
  els.historyLink.addEventListener('click', toggleLinkPanel);
  els.historyClose.addEventListener('click', closeHistory);
  // Native <dialog>: a click on the element itself is the ::backdrop; Esc fires
  // 'close' natively, so the reset lives on the 'close' event for every path.
  els.historyOverlay.addEventListener('click', function (ev) {
    if (ev.target === els.historyOverlay) closeHistory();
  });
  els.historyOverlay.addEventListener('close', onHistoryClosed);
  // Parent picker overlay (#25): search filters by name or alias; tap a result
  // in renderPicker to link. Close on the close button or a backdrop tap.
  els.linkPickerClose.addEventListener('click', closePicker);
  els.linkPickerOverlay.addEventListener('click', function (ev) {
    if (ev.target === els.linkPickerOverlay) closePicker();
  });
  els.linkPickerOverlay.addEventListener('close', onPickerClosed);
  els.linkPickerSearch.addEventListener('input', renderPicker);
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
