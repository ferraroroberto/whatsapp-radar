/* Entry point: wires every module together, runs boot(), drives the polls.
 *
 * boot() fetches build identity, passkey status, and the Dashboard metrics
 * (#9). Steps 5–7 add the remaining tab fetchers here. */

import { els, state, WEBAUTHN_POLL_MS, DASHBOARD_POLL_MS, EXECUTION_POLL_MS } from './state.js';
import { jsonApi, tokenFromUrl, toast, wireLoginForm, writeToken } from './api.js';
import { wireTabs } from './tabs.js';
import { fetchWebauthnStatus, wireWebauthn } from './webauthn.js';
import { fetchDashboard } from './dashboard.js';
import { fetchChats, wireChats } from './chats.js';
import { fetchConfig, wireConfig } from './config.js';
import { fetchExecution, wireExecution } from './execution.js';
import { fetchAudit, wireAudit } from './audit.js';

// --------------------------------------------------------- build identity
async function fetchVersion() {
  // Visible proof of which build the PWA is running. Catches stale-cache
  // confusion before it costs a debugging session.
  try {
    const body = await jsonApi('/api/version');
    const sha = body.git_sha || 'unknown';
    const ts = (body.built_at || '').replace('T', ' ').slice(0, 16);
    const hash = body.asset_hash ? ' · assets ' + body.asset_hash : '';
    els.buildReadout.textContent = (ts ? 'Build: ' + sha + ' · ' + ts : 'Build: ' + sha) + hash;
  } catch (_) {
    els.buildReadout.textContent = '';
  }
}

// --------------------------------------------------------- boot
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
    if (state.tab === 'dashboard') fetchDashboard().catch(function () {});
  }, DASHBOARD_POLL_MS);
  // While the Execution tab is open, poll runs so a live run streams; also keep
  // polling whenever a run is in flight, so leaving the tab doesn't strand it.
  setInterval(function () {
    if (state.tab === 'execution' || state.execution.active) {
      fetchExecution().catch(function () {});
    }
  }, EXECUTION_POLL_MS);
}

// --------------------------------------------------------- wire + go
wireLoginForm(boot);
wireTabs(function (tab) {
  if (tab === 'dashboard') fetchDashboard().catch(function () {});
  if (tab === 'chats') {
    fetchChats().catch(function () {});
    if (!state.config) fetchConfig().catch(function () {});
  }
  if (tab === 'execution') fetchExecution().catch(function () {});
  if (tab === 'audit') fetchAudit().catch(function () {});
});
wireWebauthn();
wireChats();
wireConfig();
wireExecution();
wireAudit();

boot();
