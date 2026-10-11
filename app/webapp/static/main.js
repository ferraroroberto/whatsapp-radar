/* Entry point: wires every module together, runs boot(), drives the polls.
 *
 * boot() fetches build identity, passkey status, and the Dashboard metrics
 * (#9). Steps 5–7 add the remaining tab fetchers here. */

import { els, state, THEME_KEY, WEBAUTHN_POLL_MS, DASHBOARD_POLL_MS, EXECUTION_POLL_MS } from './state.js';
import { jsonApi, tokenFromUrl, toast, wireLoginForm, writeToken } from './api.js';
import { icon } from './_vendored/icons/icons.js';
import { wireTabs } from './tabs.js';
import { fetchWebauthnStatus, wireWebauthn } from './webauthn.js';
import { fetchDashboard } from './dashboard.js';
import { fetchChats, wireChats } from './chats.js';
import { wireConfig, wireSettings } from './config.js';
import { fetchExecution, fetchSettingsHealth, wireExecution } from './execution.js';
import { fetchActivity, wireActivity } from './activity.js';
import { fetchFamily, wireFamily } from './family.js';

// --------------------------------------------------------- build identity
// Identical text + format to home-automation's footer readout.
function fmtBuildTime(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso).replace('T', ' ').slice(0, 16);
  const pad = function (n) { return String(n).padStart(2, '0'); };
  return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate()) +
    ' ' + pad(d.getHours()) + ':' + pad(d.getMinutes());
}

async function fetchVersion() {
  // Visible proof of which build the PWA is running. Catches stale-cache
  // confusion before it costs a debugging session.
  try {
    const body = await jsonApi('/api/version');
    const sha = body.git_sha || 'unknown';
    const ts = fmtBuildTime(body.built_at || '');
    els.buildReadout.textContent = ts ? ('Build: ' + sha + ' · ' + ts) : ('Build: ' + sha);
  } catch (exc) {
    els.buildReadout.textContent = '';
    // An auth failure must reach boot(): it stops there, before any poll timer is
    // armed that would keep 401-ing and re-clearing the password field (#348).
    if (String(exc.message) === 'auth required') throw exc;
  }
}

// --------------------------------------------------------- theme toggle
// The inline <head> script applied the stored/OS theme before first paint;
// this re-applies it so the toggle glyph matches, and owns the click flip.
function applyTheme(dark) {
  document.documentElement.dataset.theme = dark ? 'dark' : 'light';
  // Show the glyph for the action: sun to switch to light, moon to switch to dark.
  els.themeToggles.forEach(function (btn) { btn.innerHTML = icon(dark ? 'sun' : 'moon'); });
  localStorage.setItem(THEME_KEY, dark ? 'dark' : 'light');
}

function toggleTheme() {
  applyTheme(document.documentElement.dataset.theme !== 'dark');
}

(function initTheme() {
  const stored = localStorage.getItem(THEME_KEY);
  const prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
  applyTheme(stored ? stored === 'dark' : prefersDark);
})();

els.themeToggles.forEach(function (btn) { btn.addEventListener('click', toggleTheme); });

// --------------------------------------------------------- boot
function refreshHome() {
  fetchDashboard().catch(function () {});
}

// boot() re-runs after a successful password login (wireLoginForm); the poll
// timers must be registered exactly once across both runs (#348).
let pollsArmed = false;

async function boot() {
  const fromUrl = tokenFromUrl();
  if (fromUrl) writeToken(fromUrl);

  try {
    await fetchVersion();
  } catch (exc) {
    if (String(exc.message) !== 'auth required') {
      toast('Boot failed: ' + (exc.message || exc), 'error');
    }
    return;
  }
  await fetchWebauthnStatus();
  await fetchDashboard();

  if (pollsArmed) return;
  pollsArmed = true;
  setInterval(function () {
    fetchWebauthnStatus().catch(function () {});
  }, WEBAUTHN_POLL_MS);
  setInterval(function () {
    if (state.tab === 'dashboard') refreshHome();
  }, DASHBOARD_POLL_MS);
  // While Activity is open, poll runs so a live run streams; also keep polling
  // whenever a run is in flight, so leaving the tab doesn't strand its queue.
  // Settings' source-health card refreshes while it is open (the pairing QR).
  setInterval(function () {
    if (state.tab === 'activity' || state.execution.active) {
      fetchExecution().catch(function () {});
    }
    if (els.settingsDialog.open) fetchSettingsHealth();
  }, EXECUTION_POLL_MS);
}

// --------------------------------------------------------- wire + go
wireLoginForm(boot);
wireTabs(function (tab) {
  if (tab === 'dashboard') refreshHome();
  if (tab === 'chats') fetchChats().catch(function () {});
  if (tab === 'activity') fetchActivity().catch(function () {});
  if (tab === 'family') fetchFamily().catch(function () {});
});
wireWebauthn();
wireChats();
wireConfig();
wireSettings();
wireExecution();
wireActivity();
wireFamily();

boot();
