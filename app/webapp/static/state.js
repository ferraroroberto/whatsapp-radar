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
  },
};

export const EXECUTION_POLL_MS = 1500;

// "All" can be ~900 chats on a real account; cap the DOM rows we render and
// nudge the operator to search instead of scrolling a phone forever.
export const CHATS_RENDER_CAP = 150;

// ES modules are deferred — they execute after DOMContentLoaded, so
// document.getElementById is safe at module top level.
export const els = {
  tabDashboard: document.getElementById('tabDashboard'),
  tabChats: document.getElementById('tabChats'),
  tabExecution: document.getElementById('tabExecution'),
  tabAudit: document.getElementById('tabAudit'),
  paneDashboard: document.getElementById('paneDashboard'),
  paneChats: document.getElementById('paneChats'),
  paneExecution: document.getElementById('paneExecution'),
  paneAudit: document.getElementById('paneAudit'),

  // Dashboard (#9) metrics
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

  // Chats (#10)
  chatsRefresh: document.getElementById('chatsRefresh'),
  chatsFilterMonitored: document.getElementById('chatsFilterMonitored'),
  chatsFilterIgnored: document.getElementById('chatsFilterIgnored'),
  chatsFilterAll: document.getElementById('chatsFilterAll'),
  chatsSearchToggle: document.getElementById('chatsSearchToggle'),
  chatsSearch: document.getElementById('chatsSearch'),
  chatsCount: document.getElementById('chatsCount'),
  chatsList: document.getElementById('chatsList'),
  chatsEmpty: document.getElementById('chatsEmpty'),

  // History overlay (#10)
  historyOverlay: document.getElementById('historyOverlay'),
  historyTitle: document.getElementById('historyTitle'),
  historyRename: document.getElementById('historyRename'),
  historyClose: document.getElementById('historyClose'),
  historyBody: document.getElementById('historyBody'),
  historyEmpty: document.getElementById('historyEmpty'),

  // Config (#10)
  configCard: document.getElementById('configCard'),
  cfgPrompt: document.getElementById('cfgPrompt'),
  cfgRoots: document.getElementById('cfgRoots'),
  configForm: document.getElementById('configForm'),
  cfgConnector: document.getElementById('cfgConnector'),
  cfgClassifier: document.getElementById('cfgClassifier'),
  cfgNotifier: document.getElementById('cfgNotifier'),
  cfgHubBaseUrl: document.getElementById('cfgHubBaseUrl'),
  cfgHubModel: document.getElementById('cfgHubModel'),
  cfgTgToken: document.getElementById('cfgTgToken'),
  cfgTgChatId: document.getElementById('cfgTgChatId'),
  cfgNote: document.getElementById('cfgNote'),

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
  execHealthDot: document.getElementById('execHealthDot'),
  execHealthLabel: document.getElementById('execHealthLabel'),
  execHealthDetail: document.getElementById('execHealthDetail'),
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
  execPreview: document.getElementById('execPreview'),
  execPreviewText: document.getElementById('execPreviewText'),
  execOutput: document.getElementById('execOutput'),
  execRuns: document.getElementById('execRuns'),
  execRunsEmpty: document.getElementById('execRunsEmpty'),
  execRefresh: document.getElementById('execRefresh'),

  settingsPanel: document.getElementById('settingsPanel'),
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
