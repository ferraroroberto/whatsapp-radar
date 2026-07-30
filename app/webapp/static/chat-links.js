/* Family-chat linking (#25): merge two chats onto one canonical parent so a
 * person reachable from multiple numbers/senders is reviewed as one thread.
 *
 * All link maintenance happens inside a chat's history overlay. The link
 * button toggles a panel whose content depends on the chat's role:
 *   standalone → "Link to a parent…" (opens the picker; this chat becomes a child)
 *   child      → "Linked to <parent>" + Unlink / Change parent…
 *   parent     → its children, each with an Unlink
 * The link is keyed on the child, so every mutation targets a child id and the
 * server enforces the depth-1 rules. Rename (setting/clearing the operator
 * alias) lives here too — it is the other single-chat overlay mutation with no
 * dependency on the rest of the Messages tab.
 *
 * chats.js is the only importer and stays the aggregator: this module never
 * imports chats.js back. `wireChatLinks(deps)` wires every DOM listener this
 * feature owns (link button, rename button, parent picker); anything it needs
 * from chats.js at click time — the currently open chat, alias-aware label
 * formatting, refreshing the chat list + overlay after a mutation — is
 * threaded through `deps` rather than closed over, so the dependency stays
 * one-directional. `syncLinkPanel`/`resetLinkPanel` are the two extra calls
 * chats.js's own history-overlay open/close handlers make directly. */

import { els, state, CHATS_RENDER_CAP } from './state.js';
import { jsonApi, toast } from './api.js';
import { fmtLocalDateTime, fmtNum } from './format.js';

function childrenOf(parentId) {
  return state.chats.filter(function (c) { return c.parent_chat_id === parentId; });
}

function linkBtn(text, onClick) {
  const b = document.createElement('button');
  b.type = 'button';
  b.className = 'link-btn';
  b.textContent = text;
  b.addEventListener('click', onClick);
  return b;
}

// deps: { chatLabel(chat) => string, onLinked() => Promise } — chatLabel is
// chats.js's alias-aware formatter; onLinked reloads the chat list and the
// open overlay after a link/unlink mutation.
function renderLinkPanel(chat, deps) {
  const panel = els.historyLinkPanel;
  panel.textContent = '';
  const kids = childrenOf(chat.id);

  if (chat.parent_chat_id != null) {
    // Child: show its parent with unlink / re-parent.
    const parent = state.chats.find(function (c) { return c.id === chat.parent_chat_id; });
    const status = document.createElement('div');
    status.className = 'link-status';
    status.textContent =
      'Linked to: ' + (parent ? deps.chatLabel(parent) : '#' + chat.parent_chat_id);
    const actions = document.createElement('div');
    actions.className = 'link-actions';
    actions.append(
      linkBtn('Unlink', function () { unlinkChat(chat, deps); }),
      linkBtn('Change parent…', function () { openPicker(chat, deps); })
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
      nm.textContent = deps.chatLabel(k);
      const x = linkBtn('Unlink', function () { unlinkChat(k, deps); });
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
    actions.appendChild(linkBtn('Link to a parent…', function () { openPicker(chat, deps); }));
    panel.append(hint, actions);
  }
}

let panelOpen = false;

// Called from chats.js's openHistory (and after any link mutation reloads the
// overlay) so the panel content matches the current chat's role. `forceOpen`
// (boolean) sets the panel's open/collapsed state before syncing — chats.js
// passes it on a normal open (collapsed unless it's whatsapp) and when a
// chat's link-count badge opens the overlay with the panel pre-expanded;
// omitted, the panel keeps whatever state toggleLinkPanel last left it in.
export function syncLinkPanel(chat, deps, forceOpen) {
  if (typeof forceOpen === 'boolean') panelOpen = forceOpen;
  if (!chat) return;
  if (chat.source !== 'whatsapp') {
    els.historyLinkPanel.hidden = true;
    return;
  }
  if (panelOpen) {
    renderLinkPanel(chat, deps);
    els.historyLinkPanel.hidden = false;
  } else {
    els.historyLinkPanel.hidden = true;
    els.historyLinkPanel.textContent = '';
  }
}

// Called from chats.js's history-overlay 'close' handler so the panel starts
// collapsed and empty next time an overlay opens.
export function resetLinkPanel() {
  panelOpen = false;
  els.historyLinkPanel.hidden = true;
  els.historyLinkPanel.textContent = '';
}

async function unlinkChat(chat, deps) {
  try {
    await jsonApi('/api/chats/' + chat.id + '/unlink', { method: 'POST' });
    toast('Unlinked.', 'good');
    await deps.onLinked();
  } catch (exc) {
    toast('Unlink failed: ' + (exc.message || exc), 'error');
  }
}

// ----------------------------------------------------------- parent picker
const picker = { child: null, deps: null };

function pickerCandidates() {
  const q = els.linkPickerSearch.value.trim().toLowerCase();
  const child = picker.child;
  return state.chats.filter(function (c) {
    if (!child || c.id === child.id) return false;     // never itself
    if (c.source !== 'whatsapp') return false;
    if (c.parent_chat_id != null) return false;        // target must be top-level
    if (c.id === child.parent_chat_id) return false;   // already this child's parent
    if (q && !picker.deps.chatLabel(c).toLowerCase().includes(q)) return false;
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
    nm.textContent = picker.deps.chatLabel(c);
    const meta = document.createElement('span');
    meta.className = 'chat-meta';
    meta.textContent = fmtNum(c.count) + ' msgs · ' + fmtLocalDateTime(c.last_message_at);
    b.append(nm, meta);
    b.addEventListener('click', function () { doLink(picker.child, c); });
    li.appendChild(b);
    els.linkPickerList.appendChild(li);
  }
}

function openPicker(child, deps) {
  picker.child = child;
  picker.deps = deps;
  els.linkPickerTitle.textContent = 'Link “' + deps.chatLabel(child) + '” to…';
  els.linkPickerSearch.value = '';
  if (!els.linkPickerOverlay.open) els.linkPickerOverlay.showModal();
  renderPicker();
  els.linkPickerSearch.focus();
}

function onPickerClosed() {
  els.linkPickerList.textContent = '';
  picker.child = null;
  picker.deps = null;
}

function closePicker() {
  if (els.linkPickerOverlay.open) els.linkPickerOverlay.close();
}

async function doLink(child, parent) {
  if (!child || !parent) return;
  const deps = picker.deps;
  try {
    await jsonApi('/api/chats/' + child.id + '/link', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ parent_id: parent.id }),
    });
    toast('Linked to ' + deps.chatLabel(parent) + '.', 'good');
    closePicker();
    await deps.onLinked();
  } catch (exc) {
    toast('Link failed: ' + (exc.message || exc), 'error');
  }
}

// ----------------------------------------------------------- rename (alias)
// Set or clear the operator alias for the given chat. The derived name stays
// in the DB (and the parenthesized label); the alias is the human-friendly
// override that shows first — useful when the connector could only resolve a
// bare number (e.g. an unsaved contact).
async function renameChat(chat, deps) {
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
    deps.onRenamed(chat);
    toast(res.alias ? 'Alias saved.' : 'Alias cleared.', 'good');
  } catch (exc) {
    toast('Rename failed: ' + (exc.message || exc), 'error');
  }
}

// ----------------------------------------------------------- wiring
// deps: {
//   chatLabel(chat) => string,        alias-aware display label
//   getCurrentChat() => chat|null,    the chat behind the open history overlay
//   onLinked() => Promise,            reload the chat list + open overlay
//   onRenamed(chat) => void,          refresh the overlay title + chat-list row
// }
export function wireChatLinks(deps) {
  els.historyLink.addEventListener('click', function () {
    const chat = deps.getCurrentChat();
    if (!chat) return;
    panelOpen = !panelOpen;
    syncLinkPanel(chat, deps);
  });
  els.historyRename.addEventListener('click', function () {
    renameChat(deps.getCurrentChat(), deps).catch(function () {});
  });
  els.linkPickerClose.addEventListener('click', closePicker);
  els.linkPickerOverlay.addEventListener('click', function (ev) {
    if (ev.target === els.linkPickerOverlay) closePicker();
  });
  els.linkPickerOverlay.addEventListener('close', onPickerClosed);
  els.linkPickerSearch.addEventListener('input', renderPicker);
}
