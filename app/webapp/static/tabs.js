/* Four-tab switcher: Dashboard | Chats | Run | Audit.
 *
 * Thin adapter over the vendored _vendored/nav/nav-tabs.js — that file owns
 * tab/pane discovery, ARIA + roving tabindex, localStorage persistence, the
 * standalone-PWA scroll reset, and the visualViewport pin. This module only
 * keeps state.tab in sync and forwards the per-tab refresh callback. */

import { state } from './state.js';
import { initNavTabs } from './_vendored/nav/nav-tabs.js';

let nav = null;

export function setTab(tab) {
  if (nav) nav.setTab(tab);
}

export function wireTabs(onTab) {
  nav = initNavTabs({
    storageKey: 'wa-radar.tab',
    onChange: function (tab) {
      state.tab = tab;
      if (onTab) onTab(tab);
    },
  });
}
