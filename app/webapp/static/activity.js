/* Activity tab (#373): one list over the one run store, the run sheet, and the
 * header line. Run and Audit used to be two views of the same runs; this is
 * both, built for the owner's actual question when an alert looks odd: what
 * ran, and why did it decide what it decided.
 *
 * The list is the DB run store (/api/audit/runs, scoped in SQL), plus what only
 * the run poll knows: the run in flight and webapp-launched runs the DB does not
 * record (a sync, a rebuild). A row opens the run sheet, which reads both detail
 * endpoints for one run: the trace (/api/audit/runs/{id}: funnel, per-source
 * split, per-chat traces or the family payload) and the execution record
 * (/api/execution/runs/{kind}/{id}: status, Telegram text, raw output, Stop).
 * Chat names / message text go in via textContent only (privacy). */

import { els, state } from './state.js';
import { jsonApi, toast } from './api.js';
import {
  fmtLocalDateTime, fmtRelative, kindIcon, kindLabel, legPricing, renderFunnelCells,
  renderSourceFunnels, setHeaderStatus, statusChip,
} from './format.js';
import { icon } from './_vendored/icons/icons.js';
import {
  actionMeta, funnelCells, isFamilyKind, modeLabel, paramsSummary, renderFamilyTrace,
  renderMessageTrace,
} from './run-trace.js';
import { busyLine, fetchExecution, isRunning, killRun, onExecutionChange, runSelection } from './execution.js';

function ax() { return state.activity; }

// Page sizes: a day of traffic checks is ~48 rows, so Traffic reads ~4 days.
const PAGE = { messages: 60, calendar: 60, traffic: 200 };

// Which run kinds each scope holds; Messages is every non-family kind (the
// server's `scope=messages` spells the same rule).
function inScope(kind, scope) {
  if (scope === 'calendar') return kind === 'calendar-scan';
  if (scope === 'traffic') return kind === 'traffic-check';
  return !isFamilyKind(kind);
}

// A phone opens the run as a sheet; a wide pointer screen (design.md
// layout.wide) shows it in the right-hand pane beside the list.
const WIDE = window.matchMedia('(min-width: 1100px) and (pointer: fine)');

// ----------------------------------------------------------- row content

function dayKey(ts) {
  const d = new Date(ts);
  if (isNaN(d.getTime())) return 'unknown';
  return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0')
    + '-' + String(d.getDate()).padStart(2, '0');
}

function dayGroup(key) {
  const today = dayKey(Date.now());
  const yesterday = dayKey(Date.now() - 86_400_000);
  if (key === today) return 'Today';
  if (key === yesterday) return 'Yesterday';
  return 'Earlier';
}

// "18:00" under Today / Yesterday (the group heading names the day), else
// "10-05 18:00".
function whenLabel(ts) {
  const full = fmtLocalDateTime(ts, { withYear: false });
  return dayGroup(dayKey(ts)) === 'Earlier' ? full : full.slice(6);
}

// The one muted context line after the time: the funnel's last numbers.
function summaryBits(item) {
  const r = item.run;
  if (!r) return [];
  const s = r.summary || {};
  if (item.kind === 'traffic-check') {
    // Priced first, and the unpriced half named only when there is one, so a
    // run where nothing was priced cannot read as a healthy count (#283).
    const pricing = legPricing(s.checked);
    return [pricing.priced + ' priced', ...(pricing.unpriced ? [pricing.unpriced + ' not priced'] : [])];
  }
  if (item.kind === 'calendar-scan') {
    return [(s.decisions || []).length + ' events'];
  }
  // A non-zero actionable count is the row's chip, so the line does not repeat it.
  const f = r.funnel || {};
  const quiet = f.actionable ? [] : ['0 actionable'];
  if (item.kind === 'process') return [f.chats_reviewed + ' reviewed', ...quiet];
  return [f.messages_synced + ' new', ...quiet];
}

// A webapp-launched run the DB store does not hold carries its own result.
function execSummaryBits(rec) {
  const res = rec.result || {};
  if (rec.kind === 'resync') return ['+' + (res.messages_added || 0) + ' messages'];
  if (rec.kind === 'notify') return [String(res.notification_status || 'sent')];
  return [];
}

// Exception chips only (#373): no OK, no Live.
function rowChips(item) {
  const chips = [];
  if (item.status === 'failed') chips.push(statusChip('Failed', 'danger'));
  else if (isRunning(item.status)) chips.push(statusChip('Running', 'neutral'));
  if (item.mode === 'dry_run') chips.push(statusChip('Dry run', 'neutral'));
  const r = item.run;
  if (r) {
    const s = r.summary || {};
    if (item.kind === 'traffic-check' && s.alerts > 0) {
      chips.push(statusChip(s.alerts + (s.alerts === 1 ? ' alert' : ' alerts'), 'attention'));
    } else if (item.kind === 'calendar-scan') {
      const conflicts = (s.conflicts || []).length;
      const missing = (s.missing_locations || s.unknown_locations || []).length;
      if (conflicts) chips.push(statusChip(conflicts + (conflicts === 1 ? ' conflict' : ' conflicts'), 'attention'));
      if (missing) chips.push(statusChip(missing + ' missing location' + (missing === 1 ? '' : 's'), 'attention'));
    } else if (r.funnel && r.funnel.actionable > 0) {
      chips.push(statusChip(r.funnel.actionable + ' actionable', 'accent'));
    }
  }
  return chips;
}

function glyph(name) {
  const span = document.createElement('span');
  span.className = 'action-row-avatar';
  span.setAttribute('aria-hidden', 'true');
  // Static sprite markup only — never user content — so innerHTML is safe here.
  span.innerHTML = icon(name);
  return span;
}

// One run row (vendored action-row): a kind glyph, the title, one muted line
// of when plus the funnel's last numbers, and exception chips trailing.
function runRow(item) {
  const li = document.createElement('li');
  li.className = 'action-row run-row';
  const open = ax().open;
  if (open && ((item.dbId && open.dbId === item.dbId) || (item.runId && open.runId === item.runId))) {
    li.classList.add('is-open');
  }
  const main = document.createElement('button');
  main.type = 'button';
  main.className = 'action-row-main';
  main.setAttribute('aria-haspopup', 'dialog');
  const text = document.createElement('span');
  text.className = 'action-row-text';
  const title = document.createElement('span');
  title.className = 'action-row-title';
  title.textContent = kindLabel(item.kind);
  const meta = document.createElement('span');
  meta.className = 'action-row-meta';
  meta.textContent = [whenLabel(item.started_at), ...item.bits].join(' · ');
  text.append(title, meta);
  main.append(glyph(kindIcon(item.kind)), text);
  main.addEventListener('click', function () {
    openRun({ kind: item.kind, dbId: item.dbId, runId: item.runId });
  });
  li.appendChild(main);
  const chips = rowChips(item);
  if (chips.length) {
    const trail = document.createElement('span');
    trail.className = 'action-row-trail run-chips';
    trail.append(...chips);
    li.appendChild(trail);
  }
  return li;
}

// A connector outage collapses its offline scans into one marker (#195).
function gapRow(gap) {
  const li = document.createElement('li');
  li.className = 'action-row audit-gap-li';
  const box = document.createElement('div');
  box.className = 'action-row-main';
  const text = document.createElement('span');
  text.className = 'action-row-text';
  const title = document.createElement('span');
  title.className = 'action-row-title';
  title.textContent = 'Coverage gap';
  const meta = document.createElement('span');
  meta.className = 'action-row-meta';
  const dayWord = gap.duration_days === 1 ? 'day' : 'days';
  const scanWord = gap.failed_runs === 1 ? 'scan' : 'scans';
  const recovery = gap.recovered_at
    ? ` · recovered ${fmtLocalDateTime(gap.recovered_at)}`
    : ' · recovery not yet recorded';
  meta.textContent = `${fmtLocalDateTime(gap.started_at)} to ${fmtLocalDateTime(gap.ended_at)}`
    + ` · ${gap.duration_days} ${dayWord} · ${gap.failed_runs} ${scanWord} offline${recovery}`;
  text.append(title, meta);
  box.append(glyph('triangle-alert'), text);
  li.appendChild(box);
  const trail = document.createElement('span');
  trail.className = 'action-row-trail run-chips';
  trail.appendChild(statusChip('Offline', 'danger'));
  li.appendChild(trail);
  return li;
}

// A day of repetitive traffic checks folds to one row (#373), which expands
// to the individual checks; a day with an alert says so on the fold itself.
function foldRow(key, items, label) {
  const li = document.createElement('li');
  li.className = 'action-row run-fold';
  const expanded = !!ax().openDays[key];
  const main = document.createElement('button');
  main.type = 'button';
  main.className = 'action-row-main';
  main.setAttribute('aria-expanded', String(expanded));
  const text = document.createElement('span');
  text.className = 'action-row-text';
  const title = document.createElement('span');
  title.className = 'action-row-title';
  const alerts = items.reduce(function (n, it) {
    return n + ((it.run && it.run.summary && it.run.summary.alerts) || 0);
  }, 0);
  const failed = items.filter(function (it) { return it.status === 'failed'; }).length;
  title.textContent = `${label} · ${items.length} traffic check${items.length === 1 ? '' : 's'}`;
  const meta = document.createElement('span');
  meta.className = 'action-row-meta';
  meta.textContent = (alerts ? `${alerts} alert${alerts === 1 ? '' : 's'}` : 'no alerts')
    + (failed ? ` · ${failed} failed` : '');
  text.append(title, meta);
  main.append(glyph('car'), text);
  main.addEventListener('click', function () {
    ax().openDays[key] = !expanded;
    renderList();
  });
  li.appendChild(main);
  // One trailing item: the alert chip when the day had one, then the chevron.
  const trail = document.createElement('span');
  trail.className = 'action-row-trail run-chips';
  if (alerts) trail.appendChild(statusChip(alerts + (alerts === 1 ? ' alert' : ' alerts'), 'attention'));
  const chevron = document.createElement('span');
  chevron.className = 'run-fold-chevron';
  chevron.setAttribute('aria-hidden', 'true');
  chevron.textContent = '›';
  trail.appendChild(chevron);
  li.appendChild(trail);
  return li;
}

function filteredFoldRow() {
  const a = ax();
  const li = document.createElement('li');
  li.className = 'action-row run-fold';
  const main = document.createElement('button');
  main.type = 'button';
  main.id = 'activityFilteredOpen';
  main.className = 'action-row-main';
  main.setAttribute('aria-haspopup', 'dialog');
  const text = document.createElement('span');
  text.className = 'action-row-text';
  const title = document.createElement('span');
  title.className = 'action-row-title';
  title.textContent = 'Filtered out';
  const meta = document.createElement('span');
  meta.className = 'action-row-meta';
  meta.textContent = `${a.filteredTotal} reviewed without an alert · last ${a.filteredDays} days`;
  text.append(title, meta);
  main.append(glyph('eye'), text);
  main.addEventListener('click', openFiltered);
  li.appendChild(main);
  return li;
}

// ----------------------------------------------------------- list model

function auditItem(run) {
  return {
    kind: run.kind, status: run.status, mode: run.mode, started_at: run.started_at,
    dbId: run.id, runId: null, run: run, bits: summaryBits({ kind: run.kind, run: run }),
  };
}

// Runs only the poll knows (#373): the run in flight, and webapp-launched runs
// the DB store does not record (a sync, a rebuild). A DB row the active run
// already wrote (same kind, still running, started with it) is the same run.
function liveItems(scope) {
  const ex = state.execution;
  const active = ex.active;
  const out = [];
  for (const rec of ex.runs) {
    if (rec.origin === 'db' || typeof rec.db_run_id === 'number') continue;
    if (!inScope(rec.kind, scope)) continue;
    const isActive = active && active.kind === rec.kind && active.run_id === rec.run_id;
    if (!isActive && isRunning(rec.status)) continue;  // a stale record; the DB row tells it
    out.push({
      kind: rec.kind, status: isActive ? 'running' : rec.status, mode: rec.mode,
      started_at: rec.started_at, dbId: null, runId: rec.run_id, run: null,
      bits: isActive ? ['in progress'] : execSummaryBits(rec), active: isActive,
    });
  }
  return out;
}

function sameAsActive(item, live) {
  const act = live.find(function (l) { return l.active; });
  if (!act || item.kind !== act.kind || !isRunning(item.status)) return false;
  return new Date(item.started_at).getTime() >= new Date(act.started_at).getTime() - 5_000;
}

function listItems() {
  const a = ax();
  const live = liveItems(a.scope);
  const db = a.runs.map(auditItem).filter(function (it) { return !sameAsActive(it, live); });
  // Only merge live rows within the loaded window, so paging stays honest.
  const oldest = a.hasMore && db.length ? db[db.length - 1].started_at : '';
  const extra = live.filter(function (l) { return l.active || !oldest || l.started_at >= oldest; });
  const items = db.concat(extra);
  items.sort(function (x, y) { return String(y.started_at).localeCompare(String(x.started_at)); });
  // The run in flight sits at the top, whatever its start time.
  items.sort(function (x, y) { return (y.active ? 1 : 0) - (x.active ? 1 : 0); });
  return items;
}

// A day group: an overline heading over one full-bleed action-list card. A
// null label is a headless group (the Filtered-out fold at the end).
function groupSection(label) {
  const section = document.createElement('section');
  section.className = 'run-group';
  if (label) {
    const h = document.createElement('h3');
    h.className = 'run-group-title';
    h.textContent = label;
    section.appendChild(h);
  }
  const card = document.createElement('div');
  card.className = 'card action-list';
  const ul = document.createElement('ul');
  ul.className = 'action-rows';
  card.appendChild(ul);
  section.appendChild(card);
  return { section: section, ul: ul };
}

export function renderList() {
  const a = ax();
  const box = els.activityList;
  if (!a.loaded) return;
  box.textContent = '';
  box.dataset.state = 'ready';
  const items = listItems();
  const gapByRunId = new Map();
  if (a.scope === 'messages') {
    for (const gap of a.coverageGaps) for (const id of gap.run_ids || []) gapByRunId.set(id, gap);
  }
  const groups = new Map();
  for (const it of items) {
    const label = it.active ? 'Today' : dayGroup(dayKey(it.started_at));
    if (!groups.has(label)) groups.set(label, []);
    groups.get(label).push(it);
  }
  for (const [label, list] of groups) {
    const g = groupSection(label);
    if (a.scope === 'traffic') {
      // Fold per calendar day; "Earlier" holds one fold per day.
      const byDay = new Map();
      for (const it of list) {
        const key = dayKey(it.started_at);
        if (!byDay.has(key)) byDay.set(key, []);
        byDay.get(key).push(it);
      }
      for (const [key, dayItems] of byDay) {
        const dayLabel = label === 'Earlier'
          ? fmtLocalDateTime(dayItems[0].started_at, { withYear: false }).slice(0, 5) : label;
        g.ul.appendChild(foldRow(key, dayItems, dayLabel));
        if (a.openDays[key]) for (const it of dayItems) g.ul.appendChild(runRow(it));
      }
    } else {
      const shownGaps = new Set();
      for (const it of list) {
        const gap = it.dbId ? gapByRunId.get(it.dbId) : null;
        if (gap) {
          if (!shownGaps.has(gap)) { g.ul.appendChild(gapRow(gap)); shownGaps.add(gap); }
          continue;
        }
        g.ul.appendChild(runRow(it));
      }
    }
    box.appendChild(g.section);
  }
  if (a.scope === 'messages') {
    const g = groupSection(null);
    g.ul.appendChild(filteredFoldRow());
    box.appendChild(g.section);
  }
  els.activityEmpty.hidden = items.length > 0;
  els.activityMore.hidden = !a.hasMore;
  const skipped = state.execution.skippedCount || 0;
  // Self-skips are hidden from the list (#234) but must stay visible in count:
  // traffic-check's cadence window (#170) and calendar-scan's run_hour (#277).
  els.activitySkipped.hidden = !(skipped && a.scope !== 'messages');
  if (skipped) {
    els.activitySkipped.textContent =
      skipped === 1 ? '1 self-skipped run hidden' : `${skipped} self-skipped runs hidden`;
  }
}

// ----------------------------------------------------------- header line

// The most severe exception, else one muted fact (#373).
function renderHeader() {
  const busy = busyLine();
  if (busy) { setHeaderStatus(els.activityStatus, busy, 'neutral'); return; }
  const runs = ax().headerRuns;
  const scans = runs.filter(function (r) { return r.kind === 'scan' && r.mode !== 'dry_run'; });
  if (scans.length && scans[0].status === 'failed') {
    setHeaderStatus(els.activityStatus, 'Last scan failed', 'danger');
    return;
  }
  const today = dayKey(Date.now());
  const actionable = runs.reduce(function (n, r) {
    const f = r.funnel || {};
    return n + (r.mode !== 'dry_run' && dayKey(r.started_at) === today ? (f.actionable || 0) : 0);
  }, 0);
  if (actionable) {
    setHeaderStatus(els.activityStatus, `${actionable} actionable today`, 'accent');
    return;
  }
  setHeaderStatus(els.activityStatus,
    scans.length ? 'Last scan ' + fmtRelative(scans[0].started_at) : 'No live scan yet', null);
}

// ----------------------------------------------------------- fetch

async function fetchRuns(append) {
  const a = ax();
  const request = ++a.request;
  const scope = a.scope;
  const before = append && a.runs.length ? `&before=${a.runs[a.runs.length - 1].id}` : '';
  try {
    const data = await jsonApi(`/api/audit/runs?scope=${scope}&limit=${PAGE[scope]}${before}`);
    if (request !== a.request) return;
    a.runs = append ? a.runs.concat(data.runs || []) : (data.runs || []);
    a.coverageGaps = data.coverage_gaps || [];
    a.hasMore = !!data.has_more;
    a.loaded = true;
    if (scope === 'messages') a.headerRuns = a.runs.slice(0, 20);
    renderList();
    renderHeader();
  } catch (_) {
    if (request !== a.request || a.loaded) return;
    els.activityList.dataset.state = 'error';
    els.activityList.textContent = '';
    const p = document.createElement('p');
    p.className = 'muted small activity-loading';
    p.textContent = 'Runs are unavailable. Try again.';
    els.activityList.appendChild(p);
  }
}

async function fetchHeaderRuns() {
  try {
    const data = await jsonApi('/api/audit/runs?scope=messages&limit=20');
    ax().headerRuns = data.runs || [];
    renderHeader();
  } catch (_) { /* the header keeps its last line */ }
}

export async function fetchActivity() {
  const a = ax();
  const jobs = [fetchRuns(false), fetchExecution()];
  if (a.scope === 'messages') jobs.push(fetchFiltered(false));
  else jobs.push(fetchHeaderRuns());
  await Promise.all(jobs);
}

function setScope(scope) {
  const a = ax();
  if (a.scope === scope) return;
  a.scope = scope;
  a.runs = [];
  a.hasMore = false;
  a.loaded = false;
  for (const b of els.activityScope.querySelectorAll('.segmented-item')) {
    b.setAttribute('aria-pressed', String(b.dataset.scope === scope));
  }
  els.activityList.dataset.state = 'loading';
  fetchActivity().catch(function () {});
}

// ----------------------------------------------------------- run sheet

function sheetTargets() {
  return { funnel: els.runFunnel, sources: els.runSourceFunnel, traces: els.runTraces, empty: els.runTracesEmpty };
}

function duration(start, end) {
  if (!start || !end) return '';
  const secs = Math.round((new Date(end).getTime() - new Date(start).getTime()) / 1000);
  if (!(secs >= 0)) return '';
  return secs < 90 ? secs + 's' : Math.round(secs / 60) + ' min';
}

function isLive(exec) {
  const active = state.execution.active;
  return !!(exec && isRunning(exec.status) && active
    && active.kind === exec.kind && active.run_id === exec.run_id);
}

function renderSheet() {
  const a = ax();
  const open = a.open;
  if (!open) return;
  const run = a.trace && a.trace.run;
  const exec = a.exec;
  const kind = (run && run.kind) || (exec && exec.kind) || open.kind;
  const mode = (run && run.mode) || (exec && exec.mode);
  const id = run ? ' #' + run.id : '';
  els.runSheetTitle.textContent = kindLabel(kind) + id;

  const status = (exec && exec.status) || (run && run.status) || '?';
  const bits = [status];
  if (mode) bits.push(modeLabel(mode));
  const started = (run && run.started_at) || (exec && exec.started_at);
  if (started) bits.push('started ' + fmtLocalDateTime(started));
  const took = duration(started, (run && run.completed_at) || (exec && exec.finished_at));
  if (took) bits.push(took);
  const params = run ? paramsSummary(run.params) : '';
  if (params) bits.push(params);
  const res = (exec && exec.result) || {};
  if (res.backup_path) bits.push('backup: ' + res.backup_path);
  if (Array.isArray(res.unmapped) && res.unmapped.length) bits.push(res.unmapped.length + ' unmapped');
  const error = (run && run.error) || (exec && exec.error);
  if (error) bits.push('error: ' + error);
  els.runSheetMeta.textContent = bits.join(' · ');

  const t = sheetTargets();
  if (run && isFamilyKind(kind)) {
    renderFamilyTrace(t, run);
  } else if (run) {
    renderMessageTrace(t, run, a.trace.traces);
  } else {
    // No DB record (a sync, a rebuild, a run still in flight): the funnel comes
    // from the run's own parsed result.
    const cells = funnelCells(kind, res);
    if (cells) renderFunnelCells(t.funnel, cells);
    else {
      t.funnel.textContent = '';
      const p = document.createElement('p');
      p.className = 'muted small';
      p.textContent = isRunning(status) ? 'Running… the output below streams live.' : 'No funnel for this run.';
      t.funnel.appendChild(p);
    }
    renderSourceFunnels(t.sources, res.sources || {});
    t.traces.textContent = '';
    t.empty.hidden = true;
  }

  const tg = res.telegram_text;
  els.runPreview.hidden = !tg;
  if (tg) els.runPreviewText.textContent = tg;
  els.runOutput.textContent = (exec && exec.output_tail) || '(no output)';
  // Stop only for the live run (#373: in the footer, never in a summary).
  els.runSheetFoot.hidden = !isLive(exec);
}

// Read the open run's two records. `refresh` re-reads the trace too (a live
// run's trace lands when it finishes); otherwise a trace already read stays.
async function loadSheet(refresh) {
  const a = ax();
  const open = a.open;
  if (!open) return;
  const jobs = [];
  if (open.dbId && (refresh || !a.trace)) {
    jobs.push(jsonApi(`/api/audit/runs/${open.dbId}`).then(function (d) {
      if (a.open === open) a.trace = d;
    }).catch(function () {}));
  }
  const execPath = open.runId
    ? `/api/execution/runs/${open.kind}/${open.runId}`
    : open.kind ? `/api/execution/runs/${open.kind}/db-${open.dbId}` : null;
  if (execPath) {
    jobs.push(jsonApi(execPath).then(function (d) {
      if (a.open === open) a.exec = d.run;
    }).catch(function () {}));
  }
  await Promise.all(jobs);
  if (a.open !== open) return;
  // A run opened from the list or Home before its kind was known (the
  // Filtered-out drill-through) reads its execution record once the trace says.
  if (!open.kind && a.trace && a.trace.run) {
    open.kind = a.trace.run.kind;
    await loadSheet();
    return;
  }
  // A webapp-launched run gains its DB id when it finishes: read its trace too.
  if (!open.dbId && a.exec && typeof a.exec.db_run_id === 'number') {
    open.dbId = a.exec.db_run_id;
    await loadSheet();
    return;
  }
  renderSheet();
}

function placeSheet() {
  if (WIDE.matches) {
    if (els.runSheet.open) els.runSheet.close();
    els.activityDetailPane.appendChild(els.runSheetCard);
    els.activityDetailPane.hidden = false;
  } else {
    els.activityDetailPane.hidden = true;
    els.runSheet.appendChild(els.runSheetCard);
    if (!els.runSheet.open) els.runSheet.showModal();
  }
}

/* Open one run in the sheet (wide: the right-hand pane). `ref` is
 * {kind, dbId, runId}: a DB run id, a run-record id, or both. Home's run
 * links land here too. */
export function openRun(ref) {
  const a = ax();
  a.open = { kind: ref.kind || null, dbId: ref.dbId || null, runId: ref.runId || null };
  a.exec = null;
  a.trace = null;
  els.runSheetTitle.textContent = ref.kind ? kindLabel(ref.kind) : 'Run';
  els.runSheetMeta.textContent = 'Reading the run…';
  for (const box of [els.runFunnel, els.runSourceFunnel, els.runTraces]) box.textContent = '';
  els.runTracesEmpty.hidden = true;
  els.runPreview.hidden = true;
  els.runSheetFoot.hidden = true;
  els.runOutput.textContent = '(no output)';
  els.runOutputCard.open = false;
  placeSheet();
  renderList();
  loadSheet().catch(function () {});
}

function closeRun() {
  const a = ax();
  a.open = null;
  a.exec = null;
  a.trace = null;
  if (els.runSheet.open) els.runSheet.close();
  els.activityDetailPane.hidden = true;
  renderList();
}

// The run poll moved: refresh the Running row, the header and a live sheet.
function onExecution() {
  if (state.tab !== 'activity') return;
  renderList();
  renderHeader();
  const a = ax();
  if (!a.open || !a.exec) return;
  const wasLive = isRunning(a.exec.status);
  if (wasLive) {
    loadSheet(true).then(function () {
      // Finished: the list and header now hold it as a stored run.
      if (a.exec && !isRunning(a.exec.status)) fetchActivity().catch(function () {});
    }).catch(function () {});
  }
}

// ----------------------------------------------------------- filtered out

function filteredReason(item) {
  if (item.final_action !== 'not_actionable') return actionMeta(item.final_action).label;
  if (!item.stage1_passed) return 'Stage 1 filtered · no actionable keyword';
  const verdict = item.parsed_result || {};
  const reason = verdict.summary || 'Stage 2 marked this not actionable';
  return verdict.priority ? `${reason} · ${verdict.priority} priority` : reason;
}

function filteredListItem(item) {
  const li = document.createElement('li');
  li.className = 'audit-filtered-row';

  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'audit-filtered-main';
  button.setAttribute('aria-label', `Open run ${item.run_id} for ${item.display_name}`);

  const top = document.createElement('span');
  top.className = 'audit-filtered-top';
  const name = document.createElement('strong');
  name.className = 'audit-filtered-name';
  name.textContent = item.display_name;
  const source = document.createElement('span');
  source.className = 'source-badge';
  source.textContent = item.source === 'gmail' ? 'Gmail' : 'WhatsApp';
  const when = document.createElement('span');
  when.className = 'audit-filtered-when muted small';
  when.textContent = fmtLocalDateTime(item.created_at);
  top.append(name, source, when);

  const reason = document.createElement('span');
  reason.className = 'audit-filtered-reason muted small';
  reason.textContent = filteredReason(item);
  button.append(top, reason);
  button.addEventListener('click', function () {
    els.filteredSheet.close();
    openRun({ dbId: item.run_id });
  });
  li.appendChild(button);
  return li;
}

function renderFiltered() {
  const a = ax();
  els.auditFiltered.textContent = '';
  for (const item of a.filtered) els.auditFiltered.appendChild(filteredListItem(item));
  els.auditFilteredEmpty.hidden = a.filtered.length > 0;
  els.auditFilteredMore.hidden = !a.filteredHasMore;
}

async function fetchFiltered(append) {
  const a = ax();
  const request = ++a.filteredRequest;
  const previous = a.filtered.length > 0;
  const offset = append ? a.filtered.length : 0;
  els.auditFilteredCard.dataset.state = previous ? 'ready' : 'loading';
  els.auditFilteredStatus.hidden = false;
  els.auditFilteredStatus.textContent = append
    ? 'Reading more filtered decisions…'
    : 'Reading filtered decisions…';
  try {
    const data = await jsonApi(
      `/api/audit/filtered?days=${a.filteredDays}&limit=50&offset=${offset}`
    );
    if (request !== a.filteredRequest) return;
    a.filtered = append ? a.filtered.concat(data.items || []) : (data.items || []);
    a.filteredTotal = data.total || 0;
    a.filteredHasMore = !!data.has_more;
    els.auditFilteredCard.dataset.state = a.filtered.length ? 'ready' : 'empty';
    els.auditFilteredStatus.hidden = true;
    renderFiltered();
    renderList();
  } catch (_) {
    if (request !== a.filteredRequest) return;
    els.auditFilteredCard.dataset.state = previous ? 'stale' : 'error';
    els.auditFilteredStatus.textContent = previous
      ? 'Showing the previous list · live data unavailable'
      : 'Filtered decisions are unavailable. Try again.';
  }
}

function openFiltered() {
  els.filteredSheet.showModal();
  fetchFiltered(false).catch(function () {});
}

// ----------------------------------------------------------- wiring

function wireDialog(dialog, closeBtn, onClose) {
  closeBtn.addEventListener('click', function () { dialog.close(); });
  // Native <dialog>: a click on the element itself is the ::backdrop.
  dialog.addEventListener('click', function (ev) { if (ev.target === dialog) dialog.close(); });
  if (onClose) dialog.addEventListener('close', onClose);
}

export function wireActivity() {
  els.activityScope.addEventListener('click', function (ev) {
    const btn = ev.target.closest('.segmented-item');
    if (btn) setScope(btn.dataset.scope);
  });
  els.activityMore.addEventListener('click', function () { fetchRuns(true).catch(function () {}); });

  // Run sheet: the close button, the backdrop and Esc all end in 'close'.
  els.runSheetClose.addEventListener('click', closeRun);
  els.runSheet.addEventListener('click', function (ev) { if (ev.target === els.runSheet) closeRun(); });
  els.runSheet.addEventListener('close', function () {
    if (!WIDE.matches && ax().open) closeRun();
  });
  WIDE.addEventListener('change', function () { if (ax().open) placeSheet(); });
  els.runStop.addEventListener('click', function () {
    const exec = ax().exec;
    if (isLive(exec)) killRun(exec.kind, exec.run_id);
  });
  // Tap-to-copy the whole output pane (mirrors App Launcher's run output copy).
  els.runOutput.addEventListener('click', function () {
    const text = els.runOutput.textContent;
    if (!text || text === '(no output)') return;
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(
        function () { toast('Output copied'); },
        function () { toast('Copy failed', 'error'); }
      );
    }
  });

  // Run now (#373): one header tap; Run starts the queue and follows the run.
  els.activityRunNow.addEventListener('click', function () { els.runNowSheet.showModal(); });
  wireDialog(els.runNowSheet, els.runNowClose);
  els.execRunScan.addEventListener('click', async function () {
    const started = await runSelection();
    if (!started) return;
    els.runNowSheet.close();
    openRun({ kind: started.kind, runId: started.run_id });
  });

  wireDialog(els.filteredSheet, els.filteredClose);
  els.auditFilteredDays.addEventListener('click', function (ev) {
    const btn = ev.target.closest('.segmented-item');
    if (!btn) return;
    ax().filteredDays = Number(btn.dataset.days) || 30;
    for (const b of els.auditFilteredDays.querySelectorAll('.segmented-item')) {
      b.setAttribute('aria-pressed', String(b === btn));
    }
    fetchFiltered(false);
  });
  els.auditFilteredMore.addEventListener('click', function () { fetchFiltered(true); });

  onExecutionChange(onExecution);
}
