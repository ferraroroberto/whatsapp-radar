/* Entry point: wires every module together, runs boot(), drives the poll.
 *
 * Step 3 keeps boot() deliberately thin — the four tabs are empty shells, so
 * there is nothing to fetch beyond build identity + passkey status. Steps 4–7
 * add their fetchers here. */

import { els, state, WEBAUTHN_POLL_MS } from './state.js';
import { jsonApi, tokenFromUrl, toast, wireLoginForm, writeToken } from './api.js';
import { wireTabs } from './tabs.js';
import { fetchWebauthnStatus, wireWebauthn } from './webauthn.js';

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

  setInterval(function () {
    fetchWebauthnStatus().catch(function () {});
  }, WEBAUTHN_POLL_MS);
}

// --------------------------------------------------------- wire + go
wireLoginForm(boot);
wireTabs();
wireWebauthn();

boot();
