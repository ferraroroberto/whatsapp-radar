/* Shared display formatters.
 *
 * Timestamps from WhatsApp are stored in UTC (the sidecar writes `toISOString()`,
 * e.g. "2026-06-07T09:43:00Z"). Rendering them by slicing the ISO string shows
 * UTC, not the operator's wall clock — so a message sent at 11:43 local read as
 * 09:43. These helpers parse the timestamp and format it in the browser's LOCAL
 * time zone, with a string-slice fallback for any value that won't parse. */

function _pad(n) { return String(n).padStart(2, '0'); }

// Thousands separator with a period (29999 → "29.999"), deterministic across
// browsers/locales (avoids `toLocaleString()` drift).
export function fmtNum(n) {
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
