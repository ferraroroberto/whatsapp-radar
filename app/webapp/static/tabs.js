/* Four-tab switcher: Dashboard | Chats | Execution | Audit.
 *
 * Routing only in Step 3 — the panes are empty placeholders that Steps 4–7
 * (#9–#12) fill in. */

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

export function wireTabs() {
  els.tabDashboard.addEventListener('click', function () { setTab('dashboard'); });
  els.tabChats.addEventListener('click', function () { setTab('chats'); });
  els.tabExecution.addEventListener('click', function () { setTab('execution'); });
  els.tabAudit.addEventListener('click', function () { setTab('audit'); });
}

export { TABS };
