/* Config section (#10): read-only classifier view + editable safe settings.
 *
 * The system prompt and keyword roots are shown read-only (edited in their
 * files, by design). The settings form writes the safe subset back: connector/
 * classifier/notifier/hub → config/local.json, Telegram secrets →
 * webapp_config.json. The bot token is masked — a blank field on save leaves the
 * stored token untouched. */

import { els, state } from './state.js';
import { jsonApi, toast } from './api.js';
import { setSwitch } from './_vendored/switch/switch.js';

// Save is gated on an actual change: the rendered values are snapshotted and
// the button stays disabled until the form diverges from that snapshot.
let baseline = '';

function formSnapshot() {
  return JSON.stringify([
    els.cfgConnector.value,
    els.cfgSourceWhatsapp.getAttribute('aria-checked'),
    els.cfgSourceGmail.getAttribute('aria-checked'),
    els.cfgClassifier.value,
    els.cfgNotifier.value,
    els.cfgHubBaseUrl.value.trim(),
    els.cfgHubModel.value.trim(),
    els.cfgTgToken.value.trim(),
    els.cfgTgChatId.value.trim(),
  ]);
}

function refreshDirty() {
  els.cfgSave.disabled = formSnapshot() === baseline;
}

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
  const assets = d.classification_assets || {};
  const wa = assets.whatsapp || {};
  const gm = assets.gmail || {};
  els.cfgPrompt.textContent = (assets.shared_system_prompt || {}).content || d.prompt || '';
  els.cfgRoots.textContent = (wa.stage1_rules || {}).content || d.keyword_roots || '';
  els.cfgGmailRoots.textContent = (gm.stage1_rules || {}).content || '';
  els.cfgGmailTaxonomy.textContent = (gm.taxonomy || {}).content || '';
  const gmail = d.gmail || {};
  const senders = (gmail.senders || []).map(function (item) {
    return item.name && item.name !== item.address ? item.name + ' <' + item.address + '>' : item.address;
  });
  const labels = (gmail.labels || []).map(function (item) {
    return item.display_name && item.display_name !== item.name
      ? item.display_name + ' (' + item.name + ')' : item.name;
  });
  const whitelist = senders.concat(labels);
  els.cfgGmailSummary.textContent = 'Gmail whitelist: ' +
    (whitelist.length ? whitelist.join(' · ') : 'empty') + '. ' + (gmail.history_scope || '');

  const s = d.settings || {};
  const opts = d.options || {};
  fillSelect(els.cfgConnector, opts.connector || [], s.connector);
  const sources = new Set(s.sources || ['whatsapp']);
  setSwitch(els.cfgSourceWhatsapp, sources.has('whatsapp'));
  setSwitch(els.cfgSourceGmail, sources.has('gmail'));
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

  baseline = formSnapshot();
  refreshDirty();
}

async function submit(ev) {
  ev.preventDefault();
  const payload = {
    sources: [
      ...(els.cfgSourceWhatsapp.getAttribute('aria-checked') === 'true' ? ['whatsapp'] : []),
      ...(els.cfgSourceGmail.getAttribute('aria-checked') === 'true' ? ['gmail'] : []),
    ],
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
  for (const control of [els.cfgSourceWhatsapp, els.cfgSourceGmail]) {
    control.addEventListener('click', function () {
      setSwitch(control, control.getAttribute('aria-checked') !== 'true');
      refreshDirty();
    });
  }
  els.configForm.addEventListener('submit', submit);
  els.configForm.addEventListener('input', refreshDirty);
  els.configForm.addEventListener('change', refreshDirty);
}
