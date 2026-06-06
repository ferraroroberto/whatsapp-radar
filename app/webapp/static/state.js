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

export const state = {
  tab: 'dashboard',
  webauthn: { configured: false, enrollment_open: false, devices: [] },
};

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
