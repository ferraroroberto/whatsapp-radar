/* Follow-ups tab (#219): pending non-routine prep items needing a manual
 * acknowledge. A minimal queue view — list pending items, tap Acknowledge,
 * it's gone. Mirrors chats.js's watch-toggle pattern: mutate state in place
 * and re-render, no full re-fetch needed on success.
 */

import { els, state } from './state.js';
import { fetchQuiet, jsonApi, toast } from './api.js';
import { emptyStateEl } from './_vendored/empty-state/empty-state.js';

function itemLi(item) {
  const li = document.createElement('li');
  li.className = 'ack-item';

  const text = document.createElement('div');
  text.className = 'ack-item-text';
  const title = document.createElement('div');
  title.className = 'ack-item-title';
  title.textContent = [item.child, item.task_category].filter(Boolean).join(' — ') || 'Follow-up';
  text.appendChild(title);
  const metaParts = [item.display_name, item.summary].filter(Boolean);
  if (metaParts.length) {
    const meta = document.createElement('div');
    meta.className = 'ack-item-meta';
    meta.textContent = metaParts.join(' · ');
    text.appendChild(meta);
  }
  li.appendChild(text);

  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'ack-btn';
  btn.textContent = 'Acknowledge';
  btn.addEventListener('click', function () { acknowledge(item, btn); });
  li.appendChild(btn);

  return li;
}

function render() {
  els.ackItems.textContent = '';
  const items = state.ack.items;
  items.forEach(function (item) { els.ackItems.appendChild(itemLi(item)); });
  els.ackItems.hidden = items.length === 0;
  els.ackEmpty.textContent = '';
  els.ackEmpty.hidden = items.length !== 0;
  if (items.length === 0) {
    els.ackEmpty.appendChild(emptyStateEl('check', 'No pending follow-ups.'));
  }
}

async function acknowledge(item, btn) {
  btn.disabled = true;
  try {
    await jsonApi('/api/ack/' + item.id + '/acknowledge', { method: 'POST' });
    state.ack.items = state.ack.items.filter(function (i) { return i.id !== item.id; });
    toast('Acknowledged.', 'good');
    render();
  } catch (exc) {
    btn.disabled = false;
    toast('Acknowledge failed: ' + (exc.message || exc), 'error');
  }
}

export async function fetchAck() {
  await fetchQuiet('/api/ack/items', function (data) {
    state.ack.items = data.items || [];
    render();
  });
}
