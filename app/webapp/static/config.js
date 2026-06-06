/* Config section (#10): read-only classifier view + editable safe settings.
 *
 * The system prompt and keyword roots are shown read-only (edited in their
 * files, by design). The settings form writes the safe subset back: connector/
 * classifier/notifier/hub → config/local.json, Telegram secrets →
 * webapp_config.json. The bot token is masked — a blank field on save leaves the
 * stored token untouched. */

import { els, state } from './state.js';
import { jsonApi, toast } from './api.js';

function fillSelect(sel, options, current) {
  sel.textContent = '';
  for (const opt of options) {
    const o = document.createElement('option');
    o.value = opt;
    o.textContent = opt;
    if (opt === current) o.selected = true;
    sel.appendChild(o);
  }
}

export async function fetchConfig() {
  let data;
  try {
    data = await jsonApi('/api/config');
  } catch (exc) {
    return; // 401 handled in api.js
  }
  state.config = data;
  render(data);
}

function render(d) {
  els.cfgPrompt.textContent = d.prompt || '';
  els.cfgRoots.textContent = d.keyword_roots || '';

  const s = d.settings || {};
  const opts = d.options || {};
  fillSelect(els.cfgConnector, opts.connector || [], s.connector);
  fillSelect(els.cfgClassifier, opts.classifier || [], s.classifier);
  fillSelect(els.cfgNotifier, opts.notifier || [], s.notifier);
  els.cfgHubBaseUrl.value = (s.hub && s.hub.base_url) || '';
  els.cfgHubModel.value = (s.hub && s.hub.model) || '';

  // Token never arrives in clear: placeholder reflects whether one is stored.
  const tg = d.telegram || {};
  const tok = tg.token || {};
  els.cfgTgToken.value = '';
  els.cfgTgToken.placeholder = tok.configured
    ? ('set ' + (tok.hint || '') + ' — leave blank to keep')
    : 'not set';
  els.cfgTgChatId.value = tg.chat_id || '';

  els.cfgNote.textContent = d.note || '';
}

async function submit(ev) {
  ev.preventDefault();
  const payload = {
    connector: els.cfgConnector.value,
    classifier: els.cfgClassifier.value,
    notifier: els.cfgNotifier.value,
    hub_base_url: els.cfgHubBaseUrl.value.trim(),
    hub_model: els.cfgHubModel.value.trim(),
    telegram_chat_id: els.cfgTgChatId.value.trim(),
  };
  // Only send a token when the operator typed a new one (blank = keep stored).
  const tok = els.cfgTgToken.value.trim();
  if (tok) payload.telegram_bot_token = tok;

  try {
    await jsonApi('/api/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    toast('Settings saved.', 'good');
    await fetchConfig(); // refresh the masked token hint
  } catch (exc) {
    toast('Save failed: ' + (exc.message || exc), 'error');
  }
}

export function wireConfig() {
  els.configForm.addEventListener('submit', submit);
}
