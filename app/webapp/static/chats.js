/* Inbox (#61, #375): inspect and monitor WhatsApp and Gmail channels.
 *
 * One scope switch (Monitored | Ignored | All) and one filter field (a name
 * search with the source as a chip inside it); "All" is capped at
 * CHATS_RENDER_CAP rows so a ~900-chat account stays usable on a phone. Each
 * chat is one vendored action-row: an initial avatar with a source mark, the
 * name, and one line of relative time plus the last message's preview, with a
 * plain watch icon button trailing (accent when monitored); tapping it
 * monitors (baselining the cursor server side) or ignores. The chat sheet (a
 * right-hand pane on a wide screen) loads the most recent messages and lazily
 * pages older ones in; its one labelled menu holds Rename, Link chats and the
 * monitor toggle. All chat-derived text goes in via textContent. */

import { els, state, CHATS_RENDER_CAP } from './state.js';
import { jsonApi, readToken, toast } from './api.js';
import { actionRow, fmtLocalDateTime, fmtNum, fmtRelative } from './format.js';
import { renderHeadStatus } from './_vendored/home-head/home-head.js';
import { icon } from './_vendored/icons/icons.js';
import { cancelSummarySpeech, speakSummary } from './tts-playback.js';
import {
  childrenOf, openLinkPanel, renameCurrentChat, resetLinkPanel, syncLinkPanel, wireChatLinks,
} from './chat-links.js';
import { createRowMenu } from './_vendored/row-menu/row-menu.js';

// A sprite glyph wrapped for insertion next to textContent-only user data.
// Static markup only — never user content — so innerHTML is safe here.
function iconMark(name) {
  const s = document.createElement('span');
  s.className = 'msg-icon';
  s.innerHTML = icon(name);
  return s;
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
function isChild(c) {
  return c.parent_chat_id != null;
}

// What a row represents (#166): with the Gmail source filter active a row is a
// sender (email address), not a WhatsApp "channel" — this is the one wording
// difference that survives; the monitored/ignored vocabulary itself is shared
// across sources (#182).
function gmailFilterActive() {
  return state.chatsSourceFilter === 'gmail';
}
function unitNoun(n) {
  if (gmailFilterActive()) return n === 1 ? 'sender' : 'senders';
  return n === 1 ? 'channel' : 'channels';
}
// The Gmail sender address behind a "sender:addr" chat id, else null (labels /
// WhatsApp). Shown in the chat sheet's subtitle.
function gmailSenderAddress(c) {
  if (c.source !== 'gmail' || typeof c.source_chat_id !== 'string') return null;
  if (!c.source_chat_id.startsWith('sender:')) return null;
  return c.source_chat_id.slice('sender:'.length);
}

export async function fetchChats() {
  state.tripwire.phase = 'loading';
  const results = await Promise.allSettled([
    jsonApi('/api/chats'),
    jsonApi('/api/chats/tripwire'),
  ]);
  if (results[0].status === 'fulfilled') {
    state.chats = results[0].value.chats || [];
  }
  if (results[1].status === 'fulfilled') {
    const data = results[1].value;
    state.tripwire.hits = data.hits || [];
    state.tripwire.windowDays = data.window_days || 7;
    state.tripwire.truncated = !!data.truncated;
    state.tripwire.phase = state.tripwire.hits.length ? 'ready' : 'empty';
  } else {
    state.tripwire.phase = state.tripwire.hits.length ? 'stale' : 'error';
  }
  render();
}

// The status scope alone: Monitored, or everything else (Ignored), or All.
function inScope(c) {
  if (state.chatsFilter === 'monitored') return c.status === 'monitored';
  if (state.chatsFilter === 'ignored') return c.status !== 'monitored';
  return true;
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
    if (!inScope(c)) return false;
    if (state.chatsSourceFilter !== 'all' && c.source !== state.chatsSourceFilter) return false;
    if (q && !chatLabel(c).toLowerCase().includes(q)) return false;
    return true;
  });
}

// Inbox's header line (#374): chats worth a look, else how many are watched.
function renderHeader() {
  const worth = state.tripwire.hits.length;
  if (worth) {
    renderHeadStatus(els.chatsStatus, [{ text: worth + ' worth a look', tone: 'attention' }]);
    return;
  }
  const monitored = state.chats.filter(function (c) {
    return c.status === 'monitored' && !isChild(c);
  }).length;
  renderHeadStatus(els.chatsStatus, [], fmtNum(monitored) + ' monitored');
}

function render() {
  renderTripwire();
  renderHeader();
  const all = visibleChats();
  const shown = all.slice(0, CHATS_RENDER_CAP);

  els.chatsList.textContent = '';
  els.chatsEmpty.hidden = all.length > 0;

  // The count shows only while the search or the source chip narrows the list
  // (#375); the header line carries the monitored count otherwise.
  const narrowed = !!state.chatsSearch.trim() || state.chatsSourceFilter !== 'all';
  let count = '';
  if (narrowed) {
    const scoped = state.chats.filter(function (c) { return !isChild(c) && inScope(c); }).length;
    count = fmtNum(all.length) + ' of ' + fmtNum(scoped) + ' ' + unitNoun(scoped) + ' match';
  } else if (all.length > shown.length) {
    count = 'Showing ' + shown.length + ' of ' + fmtNum(all.length) + ' — search to narrow.';
  }
  els.chatsCount.textContent = count;
  els.chatsCount.hidden = !count;

  for (const c of shown) els.chatsList.appendChild(row(c));
}

// One action-row avatar: the chat's initial, with its source as a small mark
// in the corner (#375; the outlined source pill left the row).
function avatar(label, source) {
  const span = document.createElement('span');
  span.className = 'action-row-avatar chat-avatar';
  span.setAttribute('aria-hidden', 'true');
  fillAvatar(span, label, source);
  return span;
}

function fillAvatar(span, label, source) {
  span.textContent = (String(label || '?').trim().charAt(0) || '?').toUpperCase();
  const mark = document.createElement('span');
  mark.className = 'chat-avatar-source';
  // Static sprite markup only — never user content — so innerHTML is safe here.
  mark.innerHTML = icon(source === 'gmail' ? 'mail' : 'message-circle');
  span.appendChild(mark);
}

function tripwireScopeNote() {
  const tw = state.tripwire;
  return 'Stage 1 only · last ' + tw.windowDays + ' days'
    + (tw.truncated ? ' · bounded scan' : '')
    + (tw.phase === 'stale' ? ' · showing the last successful check' : '');
}

function tripwireHit(chatId) {
  return state.tripwire.hits.find(function (hit) { return hit.id === chatId; }) || null;
}

function hitSignals(hit) {
  return hit.source === 'gmail' && hit.buckets.length ? hit.buckets : hit.roots;
}

function renderTripwire() {
  const tw = state.tripwire;
  const hits = tw.hits.filter(function (hit) {
    const chat = state.chats.find(function (candidate) { return candidate.id === hit.id; });
    return !chat || chat.status === 'discovered';
  });
  tw.hits = hits;
  els.tripwireList.textContent = '';

  if (!hits.length && tw.phase !== 'error') {
    els.tripwireCard.hidden = true;
    return;
  }
  els.tripwireCard.hidden = false;
  els.tripwireMeta.hidden = tw.phase !== 'error';
  if (tw.phase === 'error') {
    els.tripwireMeta.textContent = 'Could not check recent unmonitored chats.';
    return;
  }
  // The scope note sits behind the section header's tooltip and in the chat
  // sheet, not as a permanent line (#375).
  els.tripwireTitle.title = tripwireScopeNote();

  for (const hit of hits) {
    const chat = state.chats.find(function (candidate) { return candidate.id === hit.id; });

    // The vendored icon-button: a glyph on nothing at rest (COMP-05).
    const promote = document.createElement('button');
    promote.type = 'button';
    promote.className = 'icon-button action-row-trail tripwire-promote';
    promote.title = 'Not monitored — tap to monitor';
    promote.setAttribute('aria-label', promote.title);
    promote.innerHTML = icon('eye');
    promote.addEventListener('click', function () {
      if (chat) setStatus(chat, 'monitored');
    });

    const { li } = actionRow({
      liClass: 'tripwire-row',
      disabled: !chat,
      haspopup: !!chat,
      leading: avatar(hit.name, hit.source),
      title: hit.name,
      metaClass: 'tripwire-reason',
      meta: 'Matched: ' + hitSignals(hit).join(', '),
      onClick: chat ? function () { openHistory(chat); } : undefined,
      trail: promote,
    });
    els.tripwireList.appendChild(li);
  }
}

// Monitored, or not; Gmail still demotes to 'discovered' internally (so the
// 30-day discovery window and retention exemptions keep applying), never
// 'ignored' — only the UI-facing label is shared with WhatsApp (#182).
function demoteTo(c) { return c.source === 'gmail' ? 'discovered' : 'ignored'; }

function toggleWatch(c) {
  setStatus(c, c.status === 'monitored' ? demoteTo(c) : 'monitored');
}

// One chat row (vendored action-row, #375): avatar, name, and one muted line of
// relative time (kept outside the ellipsis) plus the preview, which is cut.
function row(c) {
  // The watch toggle: a plain icon button, accent (pressed) when monitored.
  const monitored = c.status === 'monitored';
  const watch = document.createElement('button');
  watch.type = 'button';
  watch.className = 'icon-button action-row-trail chat-watch';
  watch.setAttribute('aria-pressed', monitored ? 'true' : 'false');
  watch.title = monitored ? 'Monitoring — tap to ignore' : 'Not monitored — tap to monitor';
  watch.setAttribute('aria-label', watch.title);
  watch.innerHTML = icon('eye');
  watch.addEventListener('click', function () { toggleWatch(c); });

  const { li, meta } = actionRow({
    liClass: 'chat-row',
    mainClass: 'chat-main',
    haspopup: true,
    leading: avatar(chatLabel(c), c.source),
    titleClass: 'chat-name',
    title: chatLabel(c),
    titleAttr: chatLabel(c),
    metaClass: 'chat-line',
    onClick: function () { openHistory(c); },
    trail: watch,
  });
  const when = document.createElement('span');
  when.className = 'chat-when';
  when.textContent = c.last_message_at ? fmtRelative(c.last_message_at) : '—';
  const preview = document.createElement('span');
  preview.className = 'chat-preview';
  preview.textContent = c.last_message_text ? String(c.last_message_text) : '';
  meta.append(when, preview);

  if (hist.chatId === c.id) li.classList.add('is-open');
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
    if (status === 'monitored') {
      state.tripwire.hits = state.tripwire.hits.filter(function (hit) {
        return hit.id !== chat.id;
      });
      if (!state.tripwire.hits.length) state.tripwire.phase = 'empty';
    }
    toast(status === 'monitored'
      ? (res.baselined ? 'Now monitoring — baselined to new messages.' : 'Now monitoring.')
      : 'No longer monitoring.');
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
  meta.textContent = (m.origin ? m.origin + ' · ' : '') + who + ' · ' + fmtLocalDateTime(m.ts);
  if (m.source === 'gmail') {
    const subject = document.createElement('div');
    subject.className = 'hist-subject';
    subject.textContent = m.subject || '(no subject)';
    item.append(meta, subject);
  }
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
  if (m.source !== 'gmail') item.append(meta);
  item.append(text);
  if (m.source === 'gmail' && m.thread_id) {
    const thread = document.createElement('div');
    thread.className = 'hist-thread muted small';
    thread.textContent = 'Thread ' + m.thread_id;
    item.append(thread);
  }
  // A long message (long voice-note transcript or long typed message) gets an
  // on-demand Summarize control wired to the hub (#86). The server reads the same
  // messages.text, so gate on m.text length here too. m.summary (#157) is any
  // summary already persisted for this message, so a reopened overlay renders it
  // straight from the history payload with no extra round trip.
  if (m.text && m.text.length >= SUMMARIZE_MIN_CHARS) {
    item.append(summarizeControl(m.id, m.summary));
  }
  // Any message with text can be sent to task-os as an Inbox task (#307) — no
  // length gate, unlike Summarize (which only pays for a hub call past
  // SUMMARIZE_MIN_CHARS). m.task_exported_at (already-sent state) rides the
  // same history payload as m.summary does, for the same reopened-overlay reason.
  if (m.text) {
    item.append(taskExportControl(m.id, m.task_exported_at));
  }
  return item;
}

// An on-demand "Summarize" control for one long message. Tapping it POSTs to the
// hub-backed summarize endpoint (a cheap read-through once a summary is stored,
// #157) and renders the returned summary inline beneath the message. A summary
// already present in the history payload (existingSummary) pre-populates the
// panel with no fetch at all; otherwise the result is cached on first fetch so a
// second tap just re-shows it (toggling visibility) rather than dialling the hub
// again.
function summarizeControl(id, existingSummary) {
  const wrap = document.createElement('div');
  wrap.className = 'msg-summary-wrap';
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'summarize-action';
  const out = document.createElement('div');
  out.className = 'msg-summary';
  const summaryText = document.createElement('div');
  summaryText.className = 'msg-summary-text';
  const speak = document.createElement('button');
  speak.type = 'button';
  speak.className = 'summary-speech-action';
  speak.textContent = 'Play summary aloud';
  speak.setAttribute('aria-pressed', 'false');
  function speaking(on) {
    speak.setAttribute('aria-pressed', on ? 'true' : 'false');
    speak.textContent = on ? 'Stop reading' : 'Play summary aloud';
  }
  speak.addEventListener('click', function () {
    if (speak.getAttribute('aria-pressed') === 'true') {
      cancelSummarySpeech();
      return;
    }
    speakSummary(id, speaking).catch(function (exc) {
      toast(String((exc && exc.message) || 'Could not read this summary aloud.'), 'error');
    });
  });
  out.append(summaryText, speak);
  let cached = existingSummary || null;
  let busy = false;
  out.hidden = true;
  if (cached) {
    summaryText.textContent = cached;
    speak.hidden = false;
    btn.textContent = 'Summary';
  } else {
    speak.hidden = true;
    btn.textContent = 'Summarize';
  }
  btn.addEventListener('click', async function () {
    if (busy) return;
    if (cached !== null) {  // already have it — just toggle the panel
      out.hidden = !out.hidden;
      return;
    }
    busy = true;
    btn.disabled = true;
    btn.textContent = 'Summarizing…';
    try {
      const body = await jsonApi('/api/messages/' + id + '/summarize', { method: 'POST' });
      cached = (body && body.summary) || '';
      summaryText.textContent = cached;
      speak.hidden = !cached;
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

// A "Send to Task-OS" control for one message (#307). Tapping it POSTs to the
// task-export endpoint, which persists an export timestamp server-side the
// first time it succeeds — so an already-exported message (existingExportedAt,
// from the history payload, or the endpoint's own response) renders as a
// disabled, done state and a second tap never re-sends it.
function taskExportControl(id, existingExportedAt) {
  const wrap = document.createElement('div');
  wrap.className = 'msg-task-export-wrap';
  const btn = document.createElement('button');
  btn.type = 'button';
  // Distinct class from Summarize's (same visual styling, shared in CSS) so
  // e2e/UI locators that target one control never pick up the other.
  btn.className = 'task-export-action';
  let exported = existingExportedAt || null;
  let busy = false;
  function render() {
    btn.disabled = exported != null;
    btn.textContent = exported ? 'Sent to Task-OS' : 'Send to Task-OS';
  }
  render();
  btn.addEventListener('click', async function () {
    if (busy || exported) return;
    busy = true;
    btn.disabled = true;
    btn.textContent = 'Sending…';
    try {
      const body = await jsonApi('/api/messages/' + id + '/task-export', { method: 'POST' });
      exported = (body && body.exported_at) || true;
      toast('Sent to Task-OS.');
    } catch (exc) {
      toast(String((exc && exc.message) || 'Could not send to Task-OS.'), 'error');
    } finally {
      busy = false;
      render();
    }
  });
  wrap.append(btn);
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

// The sheet's subtitle (#375): source, message count and monitoring state, and
// for a Gmail sender the address (filter parity, #166: whose mail this is).
function renderSheetHeader(chat) {
  els.historyTitle.textContent = chatLabel(chat);
  fillAvatar(els.historyAvatar, chatLabel(chat), chat.source);
  const bits = [chat.source === 'gmail' ? 'Gmail' : 'WhatsApp',
    fmtNum(chat.count) + (chat.count === 1 ? ' message' : ' messages'),
    chat.status === 'monitored' ? 'monitoring' : 'not monitored'];
  const senderAddress = gmailSenderAddress(chat);
  if (senderAddress) bits.push(senderAddress);
  els.historySubtitle.textContent = bits.join(' · ');
  // A chat the tripwire flagged says why, with the scan's scope (#375).
  const hit = tripwireHit(chat.id);
  els.historyTripwire.hidden = !hit;
  els.historyTripwire.textContent = hit
    ? 'Worth a look — matched ' + hitSignals(hit).join(', ') + ' (' + tripwireScopeNote() + ')'
    : '';
}

// A phone opens the chat as a sheet; a wide pointer screen (design.md
// layout.wide) shows it in the right-hand pane beside the list (#375).
const WIDE = window.matchMedia('(min-width: 1100px) and (pointer: fine)');

function placeSheet() {
  if (WIDE.matches) {
    if (els.historyOverlay.open) els.historyOverlay.close();
    els.chatsDetailPane.appendChild(els.historyCard);
    els.chatsDetailPane.hidden = false;
  } else {
    els.chatsDetailPane.hidden = true;
    els.historyOverlay.appendChild(els.historyCard);
    if (!els.historyOverlay.open) els.historyOverlay.showModal();
  }
}

async function openHistory(chat, openPanel) {
  els.historyBody.textContent = '';
  els.historyEmpty.hidden = true;
  hist.chat = chat;
  hist.chatId = chat.id;
  renderSheetHeader(chat);
  placeSheet();
  render();
  // The link panel starts collapsed; the menu's Link chats… opens it.
  syncLinkPanel(chat, linkDeps, chat.source === 'whatsapp' && !!openPanel);
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

// Reset the sheet's contents once it closes — every close path (button,
// backdrop tap, Esc, and the wide pane's close) ends here.
function onHistoryClosed() {
  cancelSummarySpeech();
  els.historyBody.textContent = '';
  els.historyTripwire.hidden = true;
  resetLinkPanel();
  hist.chat = null;
  hist.chatId = null;
  render();
}

function closeHistory() {
  sheetMenu.close();
  if (els.historyOverlay.open) {
    els.historyOverlay.close();  // its 'close' event resets the sheet
  } else if (!els.chatsDetailPane.hidden) {
    els.chatsDetailPane.hidden = true;
    onHistoryClosed();
  }
}

// The sheet's one labelled menu (#375; vendored row-menu): Rename…, Link
// chats… with the linked count, and the monitor toggle.
const sheetMenu = createRowMenu({ className: 'history-menu-list' });

function wireSheetMenu() {
  sheetMenu.attach('chat-sheet', els.historyMenu, [
    { label: 'Rename…', glyph: 'pencil', onTap: function () { renameCurrentChat(linkDeps); } },
    {
      label: function () {
        const n = hist.chat ? childrenOf(hist.chat.id).length : 0;
        return n ? 'Link chats… (' + n + ' linked)' : 'Link chats…';
      },
      glyph: 'link',
      hidden: function () { return !hist.chat || hist.chat.source !== 'whatsapp'; },
      onTap: function () { openLinkPanel(linkDeps); },
    },
    {
      label: function () {
        return hist.chat && hist.chat.status === 'monitored' ? 'Stop monitoring' : 'Monitor';
      },
      glyph: 'eye',
      onTap: function () { if (hist.chat) toggleWatch(hist.chat); },
    },
  ]);
}

// ------------------------------------------------------- link management
// Extracted to chat-links.js (#240): the link/unlink/re-parent picker and the
// rename (alias) control. This section keeps only what genuinely belongs to
// chats.js — the post-mutation refresh and the `deps` chat-links.js is wired
// with, since both need chats.js-owned state (`hist`, `render`, `fetchChats`).

// After any link mutation: refresh the chat list, then reload the open overlay so
// both the merged history and the link panel reflect the new family. If the chat
// itself vanished from the data (shouldn't happen) the overlay just closes.
async function refreshAfterLink() {
  await fetchChats();
  if (hist.chatId == null) return;  // no sheet open (dialog or wide pane)
  const fresh = state.chats.find(function (c) { return c.id === hist.chatId; });
  if (!fresh) { closeHistory(); return; }
  openHistory(fresh, true).catch(function () {});
}

// deps chat-links.js's link/rename/picker controls are wired with once, in
// wireChats() below. getCurrentChat reads the live `hist.chat` (not a snapshot
// taken at wiring time) so every click always acts on whichever chat's overlay
// is currently open; onRenamed mirrors what a rename used to do inline here.
function onChatRenamed(chat) {
  renderSheetHeader(chat);
  render();
}

const linkDeps = {
  chatLabel: chatLabel,
  getCurrentChat: function () { return hist.chat; },
  onLinked: refreshAfterLink,
  onRenamed: onChatRenamed,
};

// --------------------------------------------------------- wiring
export function wireChats() {
  els.chatsFilterMonitored.addEventListener('click', function () { setFilter('monitored'); });
  els.chatsFilterIgnored.addEventListener('click', function () { setFilter('ignored'); });
  els.chatsFilterAll.addEventListener('click', function () { setFilter('all'); });
  els.chatsSource.addEventListener('change', function () { setSourceFilter(els.chatsSource.value); });
  els.chatsSearch.addEventListener('input', function () {
    state.chatsSearch = els.chatsSearch.value;
    render();
  });
  els.historyClose.addEventListener('click', closeHistory);
  // Native <dialog>: a click on the element itself is the ::backdrop; Esc fires
  // 'close' natively, so the reset lives on the 'close' event for every path.
  els.historyOverlay.addEventListener('click', function (ev) {
    if (ev.target === els.historyOverlay) closeHistory();
  });
  // A wide screen closes the dialog only to move the card into the pane.
  els.historyOverlay.addEventListener('close', function () {
    if (!WIDE.matches) onHistoryClosed();
  });
  // Link/rename controls + the parent-picker overlay (#25) are chat-links.js's
  // own feature (#240) — one call wires everything it owns.
  wireChatLinks();
  wireSheetMenu();
  WIDE.addEventListener('change', function () { if (hist.chat) placeSheet(); });
  // Newest is at the top; scrolling to the bottom pages in older messages.
  els.historyBody.addEventListener('scroll', function () {
    const b = els.historyBody;
    if (b.scrollTop + b.clientHeight >= b.scrollHeight - 48) loadOlder().catch(function () {});
  });
}

function setSourceFilter(source) {
  state.chatsSourceFilter = source;
  els.chatsSource.value = source;
  render();
}

function setFilter(filter) {
  state.chatsFilter = filter;
  els.chatsFilterMonitored.setAttribute('aria-pressed', String(filter === 'monitored'));
  els.chatsFilterIgnored.setAttribute('aria-pressed', String(filter === 'ignored'));
  els.chatsFilterAll.setAttribute('aria-pressed', String(filter === 'all'));
  render();
}
