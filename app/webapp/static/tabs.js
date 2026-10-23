/* Four-tab switcher: Home | Activity | Inbox | Family.
 *
 * Thin adapter over the vendored _vendored/nav/nav-tabs.js — that file owns
 * tab/pane discovery, ARIA + roving tabindex, localStorage persistence, the
 * standalone-PWA scroll reset, and the visualViewport pin. This module only
 * keeps state.tab in sync and forwards the per-tab refresh callback. */

import { state } from './state.js';
import { readToken } from './api.js';
import { initNavTabs } from './_vendored/nav/nav-tabs.js';
import { initUsageLog } from './_vendored/usage-log/usage-log.js';

let nav = null;

// Local-only view/tap log: same-origin, ids only, never content. The bearer
// token is read per send so a rotated one is picked up.
const usage = initUsageLog({
  headers: function () {
    const token = readToken();
    return token ? { Authorization: 'Bearer ' + token } : {};
  },
});

export function setTab(tab) {
  if (nav) nav.setTab(tab);
}

const STORAGE_KEY = 'wa-radar.tab';

// Run and Audit merged into Activity: an installed PWA that last had
// one of them open must reopen on Activity, not on nothing.
function migrateStoredTab() {
  try {
    const stored = localStorage.getItem(STORAGE_KEY);
    if (stored === 'execution' || stored === 'audit') localStorage.setItem(STORAGE_KEY, 'activity');
  } catch (_) { /* private mode */ }
}

export function wireTabs(onTab) {
  migrateStoredTab();
  nav = initNavTabs({
    storageKey: STORAGE_KEY,
    onChange: function (tab) {
      state.tab = tab;
      usage.trackView(tab);
      if (onTab) onTab(tab);
    },
  });
}
