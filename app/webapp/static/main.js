/* Entry point: wires every module together, runs boot(), drives the polls.
 *
 * boot() fetches build identity, passkey status, and Home's metrics, then arms
 * the polls once. */

import {
  els, state, THEME_KEY, WEBAUTHN_POLL_MS, DASHBOARD_POLL_MS, EXECUTION_POLL_MS, EXECUTION_IDLE_POLL_MS,
} from './state.js';
import { api, jsonApi, tokenFromUrl, toast, writeToken } from './api.js';
import { icon } from './_vendored/icons/icons.js';
import { buildReadoutText } from './_vendored/page-foot/page-foot.js';
import { initRestartSheet } from './_vendored/page-foot/restart-sheet.js';
import { wireTabs } from './tabs.js';
import { fetchWebauthnStatus } from './webauthn.js';
import { fetchDashboard, wireDashboard } from './dashboard.js';
import { fetchChats, wireChats } from './chats.js';
import { wireSettings } from './config.js';
import { fetchExecution, fetchSettingsHealth, onExecutionChange, wireExecution } from './execution.js';
import { fetchActivity, openLinkedRun, wireActivity } from './activity.js';
import { fetchFamily, wireFamily } from './family.js';
import { startPoll } from './_vendored/poll/poll.js';
import { initBuildWatch } from './_vendored/build-watch/build-watch.js';

// --------------------------------------------------------- build identity
async function fetchVersion() {
  // Visible proof of which build the PWA is running. Catches stale-cache
  // confusion before it costs a debugging session.
  try {
    const body = await jsonApi('/api/version');
    els.buildReadout.textContent = buildReadoutText(body.git_sha || 'unknown', body.built_at || '');
  } catch (exc) {
    els.buildReadout.textContent = '';
    // An auth failure must reach boot(): it stops there, before any poll timer is
    // armed that would keep 401-ing against a token the server refuses.
    if (String(exc.message) === 'auth required') throw exc;
  }
}

// The sheet's POST reads the status itself (a refusal is an answer, not an error).
async function postRestart(url, body) {
  let res;
  try {
    res = await api(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  } catch (exc) {
    if (exc && exc.message === 'auth required') return { status: 401, body: null };
    throw exc;
  }
  let data = null;
  try { data = await res.json(); } catch (_) { data = null; }
  return { status: res.status, body: data };
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
  return fetchDashboard().catch(function () {});
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
  initRestartSheet({ fetchJson: jsonApi, postJson: postRestart });
  await fetchWebauthnStatus();
  await fetchDashboard();
  await openLinkedRun();

  startPoll(fetchWebauthnStatus, WEBAUTHN_POLL_MS);
  polls.home = startPoll(refreshHome, DASHBOARD_POLL_MS, { activeWhile: homeShown });
  // A run in flight streams at the fast cadence on any tab, so leaving Activity
  // doesn't strand its queue. Otherwise only an open Activity tab polls, at the
  // slow one: a run started elsewhere (a scheduled Job) shows up within one
  // beat and the fast poll takes over. Settings' source-health card refreshes
  // while it is open (the pairing QR).
  polls.runLive = startPoll(fetchExecution, EXECUTION_POLL_MS, { activeWhile: runInFlight });
  polls.runIdle = startPoll(fetchExecution, EXECUTION_IDLE_POLL_MS, { activeWhile: activityIdle });
  polls.health = startPoll(fetchSettingsHealth, EXECUTION_POLL_MS, { activeWhile: settingsShown });
}

// --------------------------------------------------------- poll wake-ups
// A poll whose condition is false arms no timer, so whatever turns the
// condition true kicks it. The reads are single-flight, so a kick that lands
// beside a direct read shares it. Before boot() arms the polls, a kick is the
// plain read.
const polls = {};

function homeShown() { return state.tab === 'dashboard'; }
function runInFlight() { return Boolean(state.execution.active); }
function activityIdle() { return state.tab === 'activity' && !state.execution.active; }
function settingsShown() { return els.settingsDialog.open; }

function wake(name, read) {
  if (polls[name]) polls[name].kick();
  else read();
}

let runWasActive = false;
onExecutionChange(function () {
  if (runInFlight() === runWasActive) return;
  runWasActive = runInFlight();
  if (runWasActive) wake('runLive', fetchExecution);
  else if (activityIdle()) wake('runIdle', fetchExecution);
});

new MutationObserver(function () {
  if (settingsShown()) wake('health', fetchSettingsHealth);
}).observe(els.settingsDialog, { attributes: true, attributeFilter: ['open'] });

// --------------------------------------------------------- wire + go
wireTabs(function (tab) {
  if (tab === 'dashboard') wake('home', refreshHome);
  if (tab === 'chats') fetchChats().catch(function () {});
  if (tab === 'activity') {
    fetchActivity().catch(function () {});
    if (activityIdle() && polls.runIdle) polls.runIdle.kick();
  }
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
