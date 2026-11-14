/* Entry point: wires every module together, runs boot(), drives the polls.
 *
 * boot() fetches build identity, passkey status, and Home's metrics, then arms
 * the polls once. */

import { els, state, THEME_KEY, WEBAUTHN_POLL_MS, DASHBOARD_POLL_MS, EXECUTION_POLL_MS } from './state.js';
import { jsonApi, tokenFromUrl, toast, writeToken } from './api.js';
import { icon } from './_vendored/icons/icons.js';
import { buildReadoutText } from './_vendored/page-foot/page-foot.js';
import { wireTabs } from './tabs.js';
import { fetchWebauthnStatus } from './webauthn.js';
import { fetchDashboard, wireDashboard } from './dashboard.js';
import { fetchChats, wireChats } from './chats.js';
import { wireSettings } from './config.js';
import { fetchExecution, fetchSettingsHealth, renderSettingsStatus, wireExecution } from './execution.js';
import { fetchActivity, wireActivity } from './activity.js';
import { fetchFamily, wireFamily } from './family.js';
import { initBuildWatch } from './_vendored/build-watch/build-watch.js';

// --------------------------------------------------------- build identity
async function fetchVersion() {
  // Visible proof of which build the PWA is running. Catches stale-cache
  // confusion before it costs a debugging session.
  try {
    const body = await jsonApi('/api/version');
    els.buildReadout.textContent = buildReadoutText(body.git_sha || 'unknown', body.built_at || '');
    renderSettingsStatus();
  } catch (exc) {
    els.buildReadout.textContent = '';
    // An auth failure must reach boot(): it stops there, before any poll timer is
    // armed that would keep 401-ing against a token the server refuses.
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
wireTabs(function (tab) {
  if (tab === 'dashboard') refreshHome();
  if (tab === 'chats') fetchChats().catch(function () {});
  if (tab === 'activity') fetchActivity().catch(function () {});
  if (tab === 'family') fetchFamily().catch(function () {});
});
wireChats();
wireSettings();
wireExecution();
wireActivity();
wireDashboard();
wireFamily();

initBuildWatch({ fetchJson: jsonApi });
boot();
