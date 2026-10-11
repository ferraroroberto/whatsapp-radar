/* Settings (#337, #377): the home-head gear's dialog on every pane, never a
 * tab, laid out as inset groups like Family: Display, Sources, How messages
 * are judged, Alerts, Maintenance, This install. Each row shows its current
 * value and opens a sheet (edit-sheet.js) holding the real control at 44px.
 *
 * A sheet commits on Done with a partial POST /api/config holding only its own
 * fields (connector/sources/classifier/notifier/hub → config/local.json,
 * Telegram → webapp_config.json); a rejection names the value and keeps the
 * sheet open; Close, the backdrop and Esc discard. The bot token is never
 * echoed: its sheet says set or not set, and a token goes out only when one is
 * typed. The prompt and keyword rules are read-only (edited in their files, by
 * design). */

import { APP_PREFIX, els, state } from './state.js';
import { fetchQuiet, jsonApi } from './api.js';
import {
  fetchSettingsHealth, rebuildCache, renderSettingsStatus, renderSourceRows, renderSyncs,
} from './execution.js';
import { bindTextSize } from './_vendored/text-size/text-size.js';
import {
  buildSelect, el, fieldRow, setRowValue, sheetHost, switchRow, textInput, valueRow,
} from './edit-sheet.js';
import {
  onWebauthnChange, passkeyShape, passkeySummary, renderPasskeys, updatePasskeyStatus,
} from './webauthn.js';

const LOCAL_CONFIG = 'config/local.json.';
const SOURCE_TITLE = { whatsapp: 'WhatsApp', gmail: 'Gmail', calendar: 'Calendar' };

export async function fetchConfig() {
  await fetchQuiet('/api/config', function (data) {
    state.config = data;
    renderRows();
  });
}

async function postConfig(payload) {
  return jsonApi('/api/config', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
}

// POST /api/config answers {ok}; the fresh state (and the masked token hint)
// comes from a re-read.
function reread() { fetchConfig().catch(function () {}); }

const ruleSheet = sheetHost({
  dialog: els.settingsSheet,
  title: els.settingsSheetTitle,
  close: els.settingsSheetClose,
  body: els.settingsSheetBody,
  error: els.settingsSheetError,
  foot: els.settingsSheetFoot,
  done: els.settingsSheetDone,
}, postConfig, reread);
// A source's sheet keeps the health, sync and count nodes the pollers write in
// place; only its draft (switch, connector) is redrawn.
const sourceSheet = sheetHost({
  dialog: els.sourceSheet,
  title: els.sourceSheetTitle,
  close: els.sourceSheetClose,
  body: els.sourceSheetBody,
  error: els.sourceSheetError,
  foot: els.sourceSheetFoot,
  done: els.sourceSheetDone,
}, postConfig, reread);

// ----------------------------------------------------------- row values

function settings() { return (state.config && state.config.settings) || {}; }

function renderRows() {
  const s = settings();
  const d = state.config || {};
  const tok = (d.telegram || {}).token || {};
  setRowValue(els.settingsDialog, 'classifier', s.classifier || '—');
  if (s.notifier === 'telegram' && !tok.configured) {
    setRowValue(els.settingsDialog, 'delivery', 'Bot token not set', 'attention');
  } else {
    setRowValue(els.settingsDialog, 'delivery', s.notifier || '—');
  }
  setRowValue(els.settingsDialog, 'hub', (s.hub && s.hub.model) || '—');
  renderSourceRows();
  renderInstallRows();
}

function renderInstallRows() {
  const pk = passkeySummary();
  setRowValue(els.settingsDialog, 'passkeys', pk.text, pk.tone);
  setRowValue(els.settingsDialog, 'build', els.buildReadout.textContent.replace(/^Build: /, '') || '—');
}

// ------------------------------------------------------------ sheets

function options(list) {
  return (list || []).map(function (v) { return { value: v, label: v }; });
}

function passwordInput(placeholder, onChange) {
  const input = document.createElement('input');
  input.type = 'password';
  input.className = 'input-native';
  input.autocomplete = 'off';
  input.placeholder = placeholder;
  input.addEventListener('input', function () { onChange(input.value); });
  return input;
}

function enabledSources() { return new Set(settings().sources || ['whatsapp']); }

function sourcesWith(source, on) {
  const set = enabledSources();
  if (on) set.add(source); else set.delete(source);
  return ['whatsapp', 'gmail'].filter(function (s) { return set.has(s); });
}

/* A source's sheet: its switch (and for WhatsApp the connector) as the draft;
 * below it, outside the draft, what the pollers keep current. Calendar has
 * nothing to edit here — the daily calendar scan is switched on in Family. */
function sourceSpec(source) {
  const editable = source !== 'calendar';
  return {
    title: SOURCE_TITLE[source],
    readOnly: !editable,
    help: {
      whatsapp: 'Read-only: messages are ingested and judged, never sent.',
      gmail: 'Read-only, whitelisted mail only.',
      calendar: 'Read-only. The daily calendar scan is switched on and off in Family.',
    }[source] + ' ' + ((state.config || {}).note || ''),
    where: editable ? LOCAL_CONFIG : null,
    draft: function () {
      return { on: enabledSources().has(source), connector: settings().connector };
    },
    payload: function (d) {
      if (!editable) return null;
      const body = { sources: sourcesWith(source, d.on) };
      if (source === 'whatsapp') body.connector = d.connector;
      return body;
    },
    render: function (body, d) {
      if (!editable) return;
      body.append(switchRow('Enabled', d.on, function (v) { d.on = v; }));
      if (source === 'whatsapp') {
        body.append(fieldRow('Connector', buildSelect(options(((state.config || {}).options || {}).connector),
          d.connector, 'WhatsApp connector', function (v) { d.connector = v; })));
      }
    },
  };
}

function textSheet(title, help, assetKey, id, extra) {
  return {
    title: title,
    readOnly: true,
    help: help,
    draft: function () { return {}; },
    payload: function () { return null; },
    render: function (body) {
      const asset = assetKey(((state.config || {}).classification_assets) || {}) || {};
      const pre = el('pre', 'codebox', asset.content || '');
      pre.id = id;
      pre.setAttribute('aria-label', title);
      body.append(pre);
      if (extra) extra(body);
      // The path, once, on the sheet's last line (J-07).
      if (asset.file) body.append(el('p', 'sheet-where', 'Where this lives: ' + asset.file));
    },
  };
}

function gmailWhitelist(body) {
  const gmail = (state.config || {}).gmail || {};
  const senders = (gmail.senders || []).map(function (item) {
    return item.name && item.name !== item.address ? item.name + ' <' + item.address + '>' : item.address;
  });
  const labels = (gmail.labels || []).map(function (item) {
    return item.display_name && item.display_name !== item.name
      ? item.display_name + ' (' + item.name + ')' : item.name;
  });
  const whitelist = senders.concat(labels);
  const line = el('p', 'source-config-summary', 'Gmail whitelist: '
    + (whitelist.length ? whitelist.join(' · ') : 'empty') + '. ' + (gmail.history_scope || ''));
  line.id = 'cfgGmailSummary';
  body.append(line);
}

let passkeysShape = '';

const SHEETS = {
  classifier: {
    title: 'Classifier',
    help: 'How a new message is judged before anything is sent.',
    where: LOCAL_CONFIG,
    draft: function () { return { v: settings().classifier }; },
    payload: function (d) { return { classifier: d.v }; },
    render: function (body, d) {
      body.append(fieldRow('Classifier', buildSelect(options(((state.config || {}).options || {}).classifier),
        d.v, 'Classifier', function (v) { d.v = v; })));
    },
  },
  'rules-whatsapp': textSheet('Keyword rules · WhatsApp',
    'The keyword pass a WhatsApp message must clear before the model sees it.',
    function (a) { return (a.whatsapp || {}).stage1_rules; }, 'cfgRoots'),
  'rules-gmail': textSheet('Keyword rules · Gmail',
    'The bucket and keyword pass a Gmail message must clear before the model sees it.',
    function (a) { return (a.gmail || {}).stage1_rules; }, 'cfgGmailRoots', gmailWhitelist),
  taxonomy: textSheet('Gmail survey taxonomy',
    'A reference for writing the Gmail keyword rules; it is never sent to the model.',
    function (a) { return (a.gmail || {}).taxonomy; }, 'cfgGmailTaxonomy'),
  instructions: textSheet('Instructions to the model',
    'Both sources share these instructions; each message adds which source it came from. '
      + 'Activity shows the exact prompt for every call.',
    function (a) { return a.shared_system_prompt; }, 'cfgPrompt'),
  delivery: {
    title: 'Delivery',
    help: 'Where alerts go.',
    where: 'config/local.json for the notifier; the Telegram chat and bot token in config/webapp_config.json.',
    draft: function () {
      return { notifier: settings().notifier, chat: ((state.config || {}).telegram || {}).chat_id || '', token: '' };
    },
    payload: function (d) {
      const body = { notifier: d.notifier, telegram_chat_id: d.chat.trim() };
      // Only a newly typed token goes out; blank keeps the stored one.
      if (d.token.trim()) body.telegram_bot_token = d.token.trim();
      return body;
    },
    render: function (body, d) {
      const tok = ((state.config || {}).telegram || {}).token || {};
      body.append(fieldRow('Notifier', buildSelect(options(((state.config || {}).options || {}).notifier),
        d.notifier, 'Notifier', function (v) { d.notifier = v; })));
      const chat = textInput(d.chat, '', 0, function (v) { d.chat = v; });
      chat.inputMode = 'numeric';
      body.append(fieldRow('Telegram chat ID', chat));
      body.append(valueRow('Bot token', tok.configured ? 'set ' + (tok.hint || '') : 'not set'));
      body.append(fieldRow('New bot token', passwordInput(
        tok.configured ? 'leave blank to keep' : 'not set', function (v) { d.token = v; })));
    },
  },
  hub: {
    title: 'Model hub',
    help: 'The local LLM hub the classifier calls.',
    where: LOCAL_CONFIG,
    draft: function () {
      const hub = settings().hub || {};
      return { base: hub.base_url || '', model: hub.model || '' };
    },
    payload: function (d) { return { hub_base_url: d.base.trim(), hub_model: d.model.trim() }; },
    render: function (body, d) {
      const base = textInput(d.base, '', 0, function (v) { d.base = v; });
      base.inputMode = 'url';
      body.append(fieldRow('Base URL', base));
      body.append(fieldRow('Model', textInput(d.model, '', 0, function (v) { d.model = v; })));
    },
  },
  rebuild: {
    title: 'Rebuild cache',
    danger: true,
    doneLabel: 'Rebuild cache',
    rule: 'Close cancels; nothing is rebuilt.',
    help: 'Reconstructs the local store from the connector buffer, for when the reader logic changed.',
    render: function (body) {
      body.append(valueRow('Preserved', 'monitored, ignored and alias choices'));
      body.append(valueRow('Backed up first', 'the database'));
      body.append(valueRow('Resets', 'run and analysis history'));
      body.append(el('p', 'sheet-help', "Activity's Run now → Sync messages is the incremental pull, "
        + 'safe to run often; this is the full rebuild.'));
    },
    action: function () { rebuildCache(); },
  },
  passkeys: {
    title: 'Passkeys',
    readOnly: true,
    render: function (body) {
      passkeysShape = passkeyShape();
      renderPasskeys(body, function () { ruleSheet.render(); });
    },
  },
};

// Rows drawn from /api/config wait for it; Rebuild and Passkeys do not.
function openRow(key) {
  if (!state.config && key !== 'rebuild' && key !== 'passkeys') return;
  if (key.startsWith('source-')) {
    const source = key.slice('source-'.length);
    els.sourceSheet.dataset.source = source;
    sourceSheet.open(sourceSpec(source));
    renderSyncs();
    fetchSettingsHealth();
    return;
  }
  ruleSheet.open(SHEETS[key]);
}

// Home's header line opens this while the tray's enrollment window is open.
export function openPasskeys() {
  openSettings();
  ruleSheet.open(SHEETS.passkeys);
}

// ------------------------------------------------------------ settings view
// The gear opens this dialog on every pane (#337).
function openSettings() {
  if (!els.settingsDialog.open) els.settingsDialog.showModal();
  if (!state.config) fetchConfig().catch(function () {});
  renderInstallRows();
  fetchSettingsHealth();
}

export function wireSettings() {
  els.settingsButtons.forEach(function (btn) { btn.addEventListener('click', openSettings); });
  bindTextSize(els.textSizeControl, APP_PREFIX);
  els.settingsClose.addEventListener('click', function () { els.settingsDialog.close(); });
  els.settingsDialog.addEventListener('click', function (ev) {
    if (ev.target === els.settingsDialog) { els.settingsDialog.close(); return; }
    const row = ev.target.closest('[data-settings-sheet]');
    if (row) openRow(row.dataset.settingsSheet);
  });
  onWebauthnChange(function () {
    renderInstallRows();
    renderSettingsStatus();
    if (!ruleSheet.isOpen(SHEETS.passkeys)) return;
    if (passkeyShape() !== passkeysShape) ruleSheet.render();
    else updatePasskeyStatus();
  });
}
