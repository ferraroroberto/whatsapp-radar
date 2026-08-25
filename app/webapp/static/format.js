/* Shared display formatters.
 *
 * Timestamps from WhatsApp are stored in UTC (the sidecar writes `toISOString()`,
 * e.g. "2026-06-07T09:43:00Z"). Rendering them by slicing the ISO string shows
 * UTC, not the operator's wall clock — so a message sent at 11:43 local read as
 * 09:43. These helpers parse the timestamp and format it in the browser's LOCAL
 * time zone, with a string-slice fallback for any value that won't parse. */

function _pad(n) { return String(n).padStart(2, '0'); }

// Per-kind display label, shared by the Run and Audit tabs so a run is named
// identically wherever it appears (#163).
const KIND_META = {
  scan: { label: 'Full pipeline' },
  process: { label: 'Process' },
  notify: { label: 'Message' },
  resync: { label: 'Sync' },
  reprocess: { label: 'Reprocess' },
  'calendar-scan': { label: 'Calendar sync' },
  'traffic-check': { label: 'Family: traffic' },
};

/* Priced vs unpriced commute legs (#283), in one place because two surfaces
 * count them: the Execution funnel's cells and the Audit list's summary line.
 *
 * `anchor_in_the_past` (the drive is over, no Routes call spent) and `error`
 * (the call failed) establish *nothing* about the road. Counting them as
 * "checked" makes a run in which nothing was priced report a healthy-looking
 * coverage number — real, plausible and wrong, which is how you end up trusting
 * a check that has not been running.
 *
 * This is the one JavaScript spelling of a vocabulary `src/family/leg_status.py`
 * owns on the Python side. It has to be spelled twice because a browser cannot
 * import a Python module; it does not have to be spelled twice *per language*.
 *
 * A leg with no `status` counts as priced: payloads written before #270 carry no
 * status field and every one of their legs genuinely was priced, so the other
 * default would invent a coverage gap that never existed. */
const UNPRICED_LEG_STATUSES = ['anchor_in_the_past', 'error'];

export function legPricing(legs) {
  const list = Array.isArray(legs) ? legs : [];
  let unpriced = 0;
  for (const leg of list) {
    if (leg && UNPRICED_LEG_STATUSES.indexOf(leg.status) !== -1) unpriced += 1;
  }
  return { priced: list.length - unpriced, unpriced: unpriced };
}

export function kindLabel(kind) { return (KIND_META[kind] || { label: kind }).label; }

// Funnel-cell mapping for the two family-check kinds (calendar-scan ·
// traffic-check), shared by the Run tab's live funnelCells() and the Audit
// tab's renderFamilyDetail() so a label/field change can't render differently
// on Run vs Audit (#237). `payload` is a flat object carrying the fields below
// — either a live run's `result` or a persisted run's `summary`, both shaped
// the same way.
export function familyKindCells(kind, payload) {
  const p = payload || {};
  if (kind === 'traffic-check') {
    // Two cells, not one: 'Priced' is the coverage number and 'Not priced' is
    // the gap in it. A single 'Checked' count folded the two together (#283).
    const pricing = legPricing(p.checked);
    return [
      { label: 'Priced', value: pricing.priced },
      { label: 'Not priced', value: pricing.unpriced },
      { label: 'Alerts', value: p.alerts },
      { label: 'Status', value: p.status },
    ];
  }
  const cells = [
    { label: 'Conflicts', value: (p.conflicts || []).length },
    { label: 'Missing loc.', value: (p.missing_locations || p.unknown_locations || []).length },
    { label: 'Status', value: p.status },
  ];
  return cells.concat(travelBlockCells(p.travel_blocks));
}

/* Travel-block vocabulary (#276), in one place because three surfaces say it:
 * the Family tab's last-sweep line, the Run tab's funnel cells and the Audit
 * drill-down. `short` fits a funnel cell, `label` reads as a sentence fragment.
 *
 * A gated sweep is never spelled with numbers. `disabled` / `no_routes_api_key`
 * / `no_home_address` mean nothing was computed, and a row of zeros there would
 * claim the opposite — a sweep that looked and found nothing to do. */
const TRAVEL_STATUS = {
  ok: { short: 'ok', label: 'ran' },
  disabled: { short: 'off', label: 'travel blocks are off' },
  no_routes_api_key: { short: 'no key', label: 'no Routes API key' },
  no_home_address: { short: 'no home', label: 'no home address configured' },
  unknown: { short: '?', label: 'not recorded' },
};

function travelStatus(status) {
  return TRAVEL_STATUS[status] || { short: String(status), label: String(status) };
}

function travelStatusShort(status) { return travelStatus(status).short; }
export function travelStatusLabel(status) { return travelStatus(status).label; }

// Headline travel-block numbers for a calendar-scan funnel row (#276). A run
// recorded before the sweep existed carries no section at all and gets no
// cells — an absent feature must not render as a feature that did nothing.
// A gated sweep gets one cell naming its gate, never a row of zeros.
function travelBlockCells(section) {
  if (!section || typeof section !== 'object') return [];
  const counts = section.counts;
  if (!counts) return [{ label: 'Blocks', value: travelStatusShort(section.status) }];
  return [
    { label: 'Block adds', value: counts.adds },
    { label: 'Block dels', value: counts.deletes },
    { label: 'Unpriced', value: counts.failures },
  ];
}

// Per-source sprite icon + label, shared by the Run tab's source-health cards
// and the Dashboard's last-activity grid + source list so a source is drawn
// identically wherever it appears (#165). `traffic` reuses the car glyph (#164).
export const SOURCE_ICON = {
  gmail: '#i-mail',
  calendar: '#i-calendar-days',
  whatsapp: '#i-message-circle',
  traffic: '#i-car',
};
export const SOURCE_LABEL = {
  gmail: 'Gmail', calendar: 'Calendar', whatsapp: 'WhatsApp', traffic: 'Traffic',
};

export function sourceIcon(source) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.classList.add('icon');
  svg.setAttribute('aria-hidden', 'true');
  const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
  use.setAttribute('href', SOURCE_ICON[source] || '#i-message-circle');
  svg.appendChild(use);
  return svg;
}

// Coarse relative time for at-a-glance "when did this last run" cards (#165):
// "just now" / "5m ago" / "2h ago" / "3d ago", falling back to the absolute
// local date past a week. A non-parseable value renders as an em-dash.
export function fmtRelative(ts) {
  if (!ts) return '—';
  const d = new Date(ts);
  if (isNaN(d.getTime())) return String(ts).replace('T', ' ').slice(0, 16);
  const secs = Math.round((Date.now() - d.getTime()) / 1000);
  if (secs < 45) return 'just now';
  const mins = Math.round(secs / 60);
  if (mins < 60) return mins + 'm ago';
  const hours = Math.round(mins / 60);
  if (hours < 24) return hours + 'h ago';
  const days = Math.round(hours / 24);
  if (days < 7) return days + 'd ago';
  return fmtLocalDateTime(ts, { withYear: false });
}

// Thousands separator with a period (29999 → "29.999"), deterministic across
// browsers/locales (avoids `toLocaleString()` drift). A missing value renders
// as an em-dash rather than the literal string "undefined"/"null".
export function fmtNum(n) {
  if (n === undefined || n === null) return '–';
  return String(n).replace(/\B(?=(\d{3})+(?!\d))/g, '.');
}

// "2026-06-07T09:43:00Z" → "2026-06-07 11:43" (local). With `withYear:false` the
// year is dropped ("06-07 11:43") for compact columns. A non-parseable value
// falls back to the old UTC slice rather than rendering "Invalid Date".
export function fmtLocalDateTime(ts, { withYear = true } = {}) {
  if (!ts) return '—';
  const d = new Date(ts);
  if (isNaN(d.getTime())) return String(ts).replace('T', ' ').slice(0, 16);
  const date = withYear
    ? `${d.getFullYear()}-${_pad(d.getMonth() + 1)}-${_pad(d.getDate())}`
    : `${_pad(d.getMonth() + 1)}-${_pad(d.getDate())}`;
  return `${date} ${_pad(d.getHours())}:${_pad(d.getMinutes())}`;
}

// Renders a row of `{label, value}` cells (funnel stats: synced/monitored/
// stage1/stage2/actionable) into `box` as `.exec-funnel-cell` blocks. Shared
// by the Execution and Audit tabs so the funnel markup can't drift between
// the live-run viewer and the historical trace view. Missing values render
// as an em-dash rather than "undefined"/"null".
export function renderFunnelCells(box, cells) {
  box.textContent = '';
  for (const cell of cells) {
    const div = document.createElement('div');
    div.className = 'exec-funnel-cell';
    const v = document.createElement('span');
    v.className = 'exec-funnel-val';
    v.textContent = (cell.value === undefined || cell.value === null) ? '–' : String(cell.value);
    const l = document.createElement('span');
    l.className = 'exec-funnel-label';
    l.textContent = cell.label;
    div.append(v, l);
    box.appendChild(div);
  }
}

export function renderSourceFunnels(box, sources) {
  box.textContent = '';
  for (const [source, f] of Object.entries(sources || {})) {
    const card = document.createElement('div');
    card.className = 'source-funnel-card';
    const title = document.createElement('p');
    title.className = 'source-funnel-title';
    title.textContent = source === 'gmail' ? 'Gmail' : 'WhatsApp';
    const values = document.createElement('p');
    values.className = 'source-funnel-values';
    values.textContent = [
      'sync ' + (f.sync_status || 'skipped'),
      (f.messages_synced || 0) + ' synced',
      (f.monitored_channels || 0) + ' monitored',
      (f.messages_checked || 0) + ' checked',
      (f.stage1_passed || 0) + ' Stage 1 pass',
      (f.stage1_rejected || 0) + ' Stage 1 reject',
      (f.llm_calls || 0) + ' LLM',
      (f.actionable || 0) + ' actionable',
      (f.cursors_advanced || 0) + ' cursor advanced',
    ].join(' · ');
    const explanation = document.createElement('p');
    explanation.className = 'source-funnel-values';
    if (f.sync_status === 'failed') explanation.textContent = 'Connector failed; cached messages were held and no cursor advanced.';
    else if (!f.monitored_channels) explanation.textContent = 'Nothing monitored, so no messages could enter Stage 1.';
    else if (!f.channels_with_delta) explanation.textContent = 'No new delta in monitored channels.';
    else if (f.stage1_rejected && !f.llm_calls) explanation.textContent = 'Stage 1 deterministically rejected every delta; the LLM was not called.';
    else if (!f.messages_synced && f.sync_status === 'success') explanation.textContent = 'Sync succeeded with no new matching messages.';
    else explanation.textContent = 'Source completed with the counters shown above.';
    card.append(title, values, explanation);
    box.appendChild(card);
  }
}
