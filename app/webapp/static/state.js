/* Shared singletons: app state, DOM-element references, constants.
 *
 * Auth: a bearer token is stored in localStorage. The page extracts it from
 * ?token=… on first load and strips it from the URL. On 401, the login overlay
 * shows; password → /api/login → bearer token.
 *
 * The four tabs (Dashboard · Chats · Execution · Audit) are empty shells in
 * Step 3; Steps 4–7 fill their bodies + state slices.
 */

export const TOKEN_KEY = 'wa-radar.token';
export const THEME_KEY = 'wa-radar.theme';
export const UNLOCK_KEY = 'wa-radar.unlock';
export const UNLOCK_EXP_KEY = 'wa-radar.unlock.exp';

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
  config: null,
  // Execution (#11)
  execution: {
    mode: 'live',          // 'live' | 'dry_run'  (applies to scan + process)
    window: 'new',         // 'new' | 'days'      (dry-run scan only)
    days: 7,
    runs: [],
    active: null,          // {kind, run_id} of the in-flight run, or null
    selected: null,        // {kind, run_id} the viewer is showing, or null
    detail: null,          // last fetched run detail
    queue: [],             // pending chained actions (multi-step run), fired in order
    sidecar: null,         // last /api/sidecar/status snapshot (connection health)
    syncs: [],             // recent sync_log rows (per-sync ingest deltas)
    syncTotals: null,      // {chats, messages} current stored totals
    sourceHealth: [],      // secret-free per-source status snapshots
    sourceHealthAt: 0,
  },
  // Audit (#12): per-run trace drill-down (read-only).
  audit: {
    runs: [],              // recent runs of every kind (funnel or summary, #163)
    syncs: [],             // resync/reprocess maintenance markers
    selected: null,        // selected run id, or null
    detail: null,          // last fetched {run, traces}
    kindFilter: 'all',     // 'all' | 'messages' | 'traffic-check' | 'calendar-scan'
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
  // Dashboard (#9) header + metrics
  themeToggle: document.getElementById('themeToggle'),
  mChannels: document.getElementById('mChannels'),
  mMessages: document.getElementById('mMessages'),
  mScans: document.getElementById('mScans'),
  mBacklog: document.getElementById('mBacklog'),
  mActionable: document.getElementById('mActionable'),
  mNotified: document.getElementById('mNotified'),
  lastRunWhen: document.getElementById('lastRunWhen'),
  lastRunSummary: document.getElementById('lastRunSummary'),
  dashChannelsBody: document.getElementById('dashChannelsBody'),
  dashChannelsEmpty: document.getElementById('dashChannelsEmpty'),
  dashSources: document.getElementById('dashSources'),

  // Chats (#10)
  chatsFilterMonitored: document.getElementById('chatsFilterMonitored'),
  chatsFilterIgnored: document.getElementById('chatsFilterIgnored'),
  chatsFilterAll: document.getElementById('chatsFilterAll'),
  chatsSourceAll: document.getElementById('chatsSourceAll'),
  chatsSourceWhatsapp: document.getElementById('chatsSourceWhatsapp'),
  chatsSourceGmail: document.getElementById('chatsSourceGmail'),
  chatsSearchToggle: document.getElementById('chatsSearchToggle'),
  chatsSearch: document.getElementById('chatsSearch'),
  chatsCount: document.getElementById('chatsCount'),
  chatsList: document.getElementById('chatsList'),
  chatsEmpty: document.getElementById('chatsEmpty'),

  // History overlay (#10)
  historyOverlay: document.getElementById('historyOverlay'),
  historyTitle: document.getElementById('historyTitle'),
  historySource: document.getElementById('historySource'),
  historyRename: document.getElementById('historyRename'),
  historyLink: document.getElementById('historyLink'),
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

  // Config (#10)
  configCard: document.getElementById('configCard'),
  cfgPrompt: document.getElementById('cfgPrompt'),
  cfgRoots: document.getElementById('cfgRoots'),
  cfgGmailRoots: document.getElementById('cfgGmailRoots'),
  cfgGmailTaxonomy: document.getElementById('cfgGmailTaxonomy'),
  cfgGmailSummary: document.getElementById('cfgGmailSummary'),
  configForm: document.getElementById('configForm'),
  cfgConnector: document.getElementById('cfgConnector'),
  cfgSourceWhatsapp: document.getElementById('cfgSourceWhatsapp'),
  cfgSourceGmail: document.getElementById('cfgSourceGmail'),
  cfgClassifier: document.getElementById('cfgClassifier'),
  cfgNotifier: document.getElementById('cfgNotifier'),
  cfgHubBaseUrl: document.getElementById('cfgHubBaseUrl'),
  cfgHubModel: document.getElementById('cfgHubModel'),
  cfgTgToken: document.getElementById('cfgTgToken'),
  cfgTgChatId: document.getElementById('cfgTgChatId'),
  cfgNote: document.getElementById('cfgNote'),
  cfgSave: document.getElementById('cfgSave'),

  // Execution (#11)
  execMode: document.getElementById('execMode'),
  execModeLive: document.getElementById('execModeLive'),
  execModeDry: document.getElementById('execModeDry'),
  execModeHint: document.getElementById('execModeHint'),
  execDryOpts: document.getElementById('execDryOpts'),
  execWindow: document.getElementById('execWindow'),
  execWinNew: document.getElementById('execWinNew'),
  execWinDays: document.getElementById('execWinDays'),
  execDays: document.getElementById('execDays'),
  execStageSync: document.getElementById('execStageSync'),
  execStageProcess: document.getElementById('execStageProcess'),
  execStageMessage: document.getElementById('execStageMessage'),
  execRunScan: document.getElementById('execRunScan'),
  execBusy: document.getElementById('execBusy'),
  execReprocess: document.getElementById('execReprocess'),
  execSourcesCard: document.getElementById('execSourcesCard'),
  execSources: document.getElementById('execSources'),
  execSourcesCount: document.getElementById('execSourcesCount'),
  execReconnect: document.getElementById('execReconnect'),
  execReconnectMsg: document.getElementById('execReconnectMsg'),
  execReconnectBtn: document.getElementById('execReconnectBtn'),
  execQr: document.getElementById('execQr'),
  execSyncs: document.getElementById('execSyncs'),
  execSyncsEmpty: document.getElementById('execSyncsEmpty'),
  execSyncTotals: document.getElementById('execSyncTotals'),
  execRunsCard: document.getElementById('execRunsCard'),
  execViewerCard: document.getElementById('execViewerCard'),
  execViewer: document.getElementById('execViewer'),
  execViewerTitle: document.getElementById('execViewerTitle'),
  execViewerMeta: document.getElementById('execViewerMeta'),
  execViewerEmpty: document.getElementById('execViewerEmpty'),
  execKill: document.getElementById('execKill'),
  execFunnel: document.getElementById('execFunnel'),
  execSourceFunnel: document.getElementById('execSourceFunnel'),
  execPreview: document.getElementById('execPreview'),
  execPreviewText: document.getElementById('execPreviewText'),
  execOutput: document.getElementById('execOutput'),
  execRuns: document.getElementById('execRuns'),
  execRunsEmpty: document.getElementById('execRunsEmpty'),

  // Audit (#12)
  auditRuns: document.getElementById('auditRuns'),
  auditRunsEmpty: document.getElementById('auditRunsEmpty'),
  auditKindFilter: document.getElementById('auditKindFilter'),
  auditDetailCard: document.getElementById('auditDetailCard'),
  auditDetailTitle: document.getElementById('auditDetailTitle'),
  auditDetailClose: document.getElementById('auditDetailClose'),
  auditDetailMeta: document.getElementById('auditDetailMeta'),
  auditFunnel: document.getElementById('auditFunnel'),
  auditSourceFunnel: document.getElementById('auditSourceFunnel'),
  auditTraces: document.getElementById('auditTraces'),
  auditTracesEmpty: document.getElementById('auditTracesEmpty'),

  // Family checks (#160)
  familyControls: document.getElementById('familyControls'),
  familyRules: document.getElementById('familyRules'),
  familyRuns: document.getElementById('familyRuns'),
  familyRunsEmpty: document.getElementById('familyRunsEmpty'),

  enrollCard: document.getElementById('enrollCard'),
  webauthnStatus: document.getElementById('webauthnStatus'),
  webauthnDevices: document.getElementById('webauthnDevices'),
  enrollDeviceBtn: document.getElementById('enrollDeviceBtn'),
  buildReadout: document.getElementById('buildReadout'),

  toast: document.getElementById('toast'),

  loginOverlay: document.getElementById('loginOverlay'),
  loginForm: document.getElementById('loginForm'),
  loginPassword: document.getElementById('loginPassword'),
  loginError: document.getElementById('loginError'),
};
