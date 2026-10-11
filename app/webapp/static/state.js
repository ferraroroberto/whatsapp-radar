/* Shared singletons: app state, DOM-element references, constants.
 *
 * Auth: a bearer token is stored in localStorage. The page extracts it from
 * ?token=… on first load and strips it from the URL. On 401, the login overlay
 * shows; password → /api/login → bearer token.
 *
 * The four tabs (Home · Activity · Inbox · Family) each own a state slice
 * below; Settings is a modal, not a tab.
 */

export const APP_PREFIX = 'wa-radar'; // localStorage namespace (token, theme, text size)
export const TOKEN_KEY = APP_PREFIX + '.token';
export const THEME_KEY = APP_PREFIX + '.theme';

export const WEBAUTHN_POLL_MS = 15000;
export const DASHBOARD_POLL_MS = 15000;

export const state = {
  tab: 'dashboard',
  webauthn: { configured: false, enrollment_open: false, devices: [] },
  dashboard: null,
  // Chats & Config (#10)
  chats: [],
  chatsFilter: 'monitored',  // 'monitored' | 'all'
  chatsSourceFilter: 'all',  // 'all' | 'whatsapp' | 'gmail'
  chatsSearch: '',
  tripwire: {
    hits: [],
    phase: 'loading',  // 'loading' | 'ready' | 'empty' | 'stale' | 'error'
    windowDays: 7,
    truncated: false,
  },
  config: null,
  // Run queue + run controls (#11), driven from Activity's Run-now sheet (#373).
  execution: {
    mode: 'live',          // 'live' | 'dry_run'  (applies to scan + process)
    window: 'new',         // 'new' | 'days'      (dry-run scan only)
    days: 7,
    runs: [],              // /api/execution/runs records (both stores, #163)
    skippedCount: 0,       // self-skips hidden from `runs` (#234, #277)
    active: null,          // {kind, run_id} of the in-flight run, or null
    queue: [],             // pending chained actions (multi-step run), fired in order
    sidecar: null,         // last /api/sidecar/status snapshot (connection health)
    syncs: [],             // recent sync_log rows (per-sync ingest deltas)
    syncTotals: null,      // {chats, messages} current stored totals
    sourceHealth: [],      // secret-free per-source status snapshots
    sourceHealthAt: 0,
  },
  // Activity (#373): one list over the run store, scoped, plus the run sheet.
  activity: {
    scope: 'messages',     // 'messages' | 'calendar' | 'traffic'
    runs: [],              // /api/audit/runs rows for the scope, newest first
    coverageGaps: [],      // contiguous multi-run connector outages (#195)
    hasMore: false,
    request: 0,            // discard out-of-order responses after fast scope changes
    loaded: false,
    headerRuns: [],        // newest message runs, for the header line on any scope
    openDays: {},          // traffic day key -> true while that day's fold is open
    open: null,            // {kind, runId, dbId} the run sheet shows, or null
    exec: null,            // the open run's execution record (output, Telegram, status)
    trace: null,           // the open run's audit detail {run, traces}
    filtered: [],          // recent cross-run decisions that did not alert (#194)
    filteredDays: 30,
    filteredTotal: 0,
    filteredHasMore: false,
    filteredRequest: 0,
  },
  // Family checks (#160): last /api/family snapshot (rules + toggles + runs).
  family: null,
};

export const EXECUTION_POLL_MS = 1500;

// "All" can be ~900 chats on a real account; cap the DOM rows we render and
// nudge the operator to search instead of scrolling a phone forever.
export const CHATS_RENDER_CAP = 150;

// ES modules are deferred — they execute after DOMContentLoaded, so
// document.getElementById is safe at module top level.
export const els = {
  // Page header (home-head on every pane), Settings dialog, Dashboard grid (#9, #165, #337)
  themeToggles: Array.from(document.querySelectorAll('.theme-toggle')),
  settingsButtons: Array.from(document.querySelectorAll('.home-settings')),
  settingsDialog: document.getElementById('settingsDialog'),
  settingsClose: document.getElementById('settingsClose'),
  textSizeControl: document.getElementById('textSizeControl'),
  settingsStatus: document.getElementById('settingsStatus'),
  // Home (#374): header line, glance card, Latest runs; the source counts card
  // lives in Settings.
  dashStatus: document.getElementById('dashStatus'),
  dashGlance: document.getElementById('dashGlance'),
  dashLatest: document.getElementById('dashLatest'),
  dashAllActivity: document.getElementById('dashAllActivity'),
  dashSources: document.getElementById('dashSources'),

  // Chats (#10)
  chatsStatus: document.getElementById('chatsStatus'),
  chatsFilterMonitored: document.getElementById('chatsFilterMonitored'),
  chatsFilterIgnored: document.getElementById('chatsFilterIgnored'),
  chatsFilterAll: document.getElementById('chatsFilterAll'),
  chatsSource: document.getElementById('chatsSource'),
  chatsDetailPane: document.getElementById('chatsDetailPane'),
  chatsSearch: document.getElementById('chatsSearch'),
  chatsCount: document.getElementById('chatsCount'),
  chatsList: document.getElementById('chatsList'),
  chatsEmpty: document.getElementById('chatsEmpty'),
  tripwireCard: document.getElementById('tripwireCard'),
  tripwireTitle: document.getElementById('tripwireTitle'),
  tripwireMeta: document.getElementById('tripwireMeta'),
  tripwireList: document.getElementById('tripwireList'),

  // History overlay (#10)
  historyOverlay: document.getElementById('historyOverlay'),
  historyCard: document.getElementById('historyCard'),
  historyAvatar: document.getElementById('historyAvatar'),
  historyTitle: document.getElementById('historyTitle'),
  historySubtitle: document.getElementById('historySubtitle'),
  historyMenu: document.getElementById('historyMenu'),
  historyTripwire: document.getElementById('historyTripwire'),
  historyLinkPanel: document.getElementById('historyLinkPanel'),
  historyClose: document.getElementById('historyClose'),
  historyBody: document.getElementById('historyBody'),
  historyEmpty: document.getElementById('historyEmpty'),

  // Link picker overlay (#25)
  linkPickerOverlay: document.getElementById('linkPickerOverlay'),
  linkPickerTitle: document.getElementById('linkPickerTitle'),
  linkPickerClose: document.getElementById('linkPickerClose'),
  linkPickerSearch: document.getElementById('linkPickerSearch'),
  linkPickerCount: document.getElementById('linkPickerCount'),
  linkPickerList: document.getElementById('linkPickerList'),
  linkPickerEmpty: document.getElementById('linkPickerEmpty'),

  // Settings sheets (#377): the rule shell and the per-source shell.
  settingsSheet: document.getElementById('settingsSheet'),
  settingsSheetTitle: document.getElementById('settingsSheetTitle'),
  settingsSheetClose: document.getElementById('settingsSheetClose'),
  settingsSheetBody: document.getElementById('settingsSheetBody'),
  settingsSheetError: document.getElementById('settingsSheetError'),
  settingsSheetFoot: document.getElementById('settingsSheetFoot'),
  settingsSheetDone: document.getElementById('settingsSheetDone'),
  sourceSheet: document.getElementById('sourceSheet'),
  sourceSheetTitle: document.getElementById('sourceSheetTitle'),
  sourceSheetClose: document.getElementById('sourceSheetClose'),
  sourceSheetBody: document.getElementById('sourceSheetBody'),
  sourceSheetError: document.getElementById('sourceSheetError'),
  sourceSheetFoot: document.getElementById('sourceSheetFoot'),
  sourceSheetDone: document.getElementById('sourceSheetDone'),

  // Run-now sheet (#11, #373)
  runNowSheet: document.getElementById('runNowSheet'),
  runNowClose: document.getElementById('runNowClose'),
  execDryRun: document.getElementById('execDryRun'),
  execModeHint: document.getElementById('execModeHint'),
  execDryOpts: document.getElementById('execDryOpts'),
  execWindow: document.getElementById('execWindow'),
  execWinNew: document.getElementById('execWinNew'),
  execWinDays: document.getElementById('execWinDays'),
  execDays: document.getElementById('execDays'),
  execStageSync: document.getElementById('execStageSync'),
  execStageProcess: document.getElementById('execStageProcess'),
  execStageMessage: document.getElementById('execStageMessage'),
  execStageCalendar: document.getElementById('execStageCalendar'),
  execRunScan: document.getElementById('execRunScan'),
  execBusy: document.getElementById('execBusy'),
  execReprocess: document.getElementById('execReprocess'),
  // Traffic jam insurance card (#164)
  execTrafficCard: document.getElementById('execTrafficCard'),
  execTrafficEnabled: document.getElementById('execTrafficEnabled'),
  execTrafficRunLive: document.getElementById('execTrafficRunLive'),
  execTrafficRunDry: document.getElementById('execTrafficRunDry'),
  execTrafficStatus: document.getElementById('execTrafficStatus'),
  execSources: document.getElementById('execSources'),
  execReconnect: document.getElementById('execReconnect'),
  execReconnectMsg: document.getElementById('execReconnectMsg'),
  execReconnectBtn: document.getElementById('execReconnectBtn'),
  execQr: document.getElementById('execQr'),
  execSyncs: document.getElementById('execSyncs'),
  execSyncsEmpty: document.getElementById('execSyncsEmpty'),

  // Activity (#373): header line, scope, run list, run sheet, filtered sheet.
  paneActivity: document.getElementById('paneActivity'),
  activityStatus: document.getElementById('activityStatus'),
  activityRunNow: document.getElementById('activityRunNow'),
  activityScope: document.getElementById('activityScope'),
  activityList: document.getElementById('activityList'),
  activityEmpty: document.getElementById('activityEmpty'),
  activityMore: document.getElementById('activityMore'),
  activitySkipped: document.getElementById('activitySkipped'),
  activityDetailPane: document.getElementById('activityDetailPane'),
  runSheet: document.getElementById('runSheet'),
  runSheetCard: document.getElementById('runSheetCard'),
  runSheetTitle: document.getElementById('runSheetTitle'),
  runSheetClose: document.getElementById('runSheetClose'),
  runSheetMeta: document.getElementById('runSheetMeta'),
  runFunnel: document.getElementById('runFunnel'),
  runSourceFunnel: document.getElementById('runSourceFunnel'),
  runTraces: document.getElementById('runTraces'),
  runTracesEmpty: document.getElementById('runTracesEmpty'),
  runPreview: document.getElementById('runPreview'),
  runPreviewText: document.getElementById('runPreviewText'),
  runOutputCard: document.getElementById('runOutputCard'),
  runOutput: document.getElementById('runOutput'),
  runSheetFoot: document.getElementById('runSheetFoot'),
  runStop: document.getElementById('runStop'),
  filteredSheet: document.getElementById('filteredSheet'),
  filteredClose: document.getElementById('filteredClose'),
  auditFilteredCard: document.getElementById('auditFilteredCard'),
  auditFilteredDays: document.getElementById('auditFilteredDays'),
  auditFilteredStatus: document.getElementById('auditFilteredStatus'),
  auditFiltered: document.getElementById('auditFiltered'),
  auditFilteredEmpty: document.getElementById('auditFilteredEmpty'),
  auditFilteredMore: document.getElementById('auditFilteredMore'),

  // Family rules (#160, #167) as inset groups with sheets (#376); the traffic
  // group lives here since #373.
  paneFamily: document.getElementById('paneFamily'),
  familyStatus: document.getElementById('familyStatus'),
  familySyncSwitch: document.getElementById('familySyncSwitch'),
  familyAskSwitch: document.getElementById('familyAskSwitch'),
  familySheet: document.getElementById('familySheet'),
  familySheetTitle: document.getElementById('familySheetTitle'),
  familySheetClose: document.getElementById('familySheetClose'),
  familySheetBody: document.getElementById('familySheetBody'),
  familySheetError: document.getElementById('familySheetError'),
  familySheetFoot: document.getElementById('familySheetFoot'),
  familySheetDone: document.getElementById('familySheetDone'),
  // Commute travel blocks (#268): knobs + per-calendar write capability, in
  // their own sheet (#376).
  familyTravelRow: document.getElementById('familyTravelRow'),
  familyTravelSheet: document.getElementById('familyTravelSheet'),
  familyTravelClose: document.getElementById('familyTravelClose'),
  familyTravelBlocks: document.getElementById('familyTravelBlocks'),
  familyTravelError: document.getElementById('familyTravelError'),
  familyTravelSweep: document.getElementById('familyTravelSweep'),
  familyTravelDone: document.getElementById('familyTravelDone'),

  buildReadout: document.getElementById('buildReadout'),

  loginOverlay: document.getElementById('loginOverlay'),
  loginForm: document.getElementById('loginForm'),
  loginPassword: document.getElementById('loginPassword'),
  loginError: document.getElementById('loginError'),
};
