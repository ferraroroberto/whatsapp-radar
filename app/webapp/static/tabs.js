/* Four-tab switcher: Dashboard | Chats | Execution | Audit.
 *
 * Routing + an optional onTab callback so a tab can refresh its data when it
 * becomes visible (Dashboard wired in Step 4 / #9). Chats · Execution · Audit
 * panes are still placeholders that Steps 5–7 (#10–#12) fill in. */

import { els, state } from './state.js';

const TABS = ['dashboard', 'chats', 'execution', 'audit'];

export function setTab(tab) {
  state.tab = tab;
  els.tabDashboard.classList.toggle('active', tab === 'dashboard');
  els.tabChats.classList.toggle('active', tab === 'chats');
  els.tabExecution.classList.toggle('active', tab === 'execution');
  els.tabAudit.classList.toggle('active', tab === 'audit');
  els.paneDashboard.hidden = tab !== 'dashboard';
  els.paneChats.hidden = tab !== 'chats';
  els.paneExecution.hidden = tab !== 'execution';
  els.paneAudit.hidden = tab !== 'audit';
}

export function wireTabs(onTab) {
  function go(tab) {
    setTab(tab);
    if (onTab) onTab(tab);
  }
  els.tabDashboard.addEventListener('click', function () { go('dashboard'); });
  els.tabChats.addEventListener('click', function () { go('chats'); });
  els.tabExecution.addEventListener('click', function () { go('execution'); });
  els.tabAudit.addEventListener('click', function () { go('audit'); });
}

export { TABS };
