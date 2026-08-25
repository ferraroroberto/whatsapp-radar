/* Whitelist redaction for the Audit tab's raw run-payload dump (#285).
 *
 * The dump exists so an operator can see exactly what a run recorded, and
 * deleting it would cost real diagnostic value the structured blocks above it
 * do not fully replace. So this redacts rather than removes — and it redacts by
 * *whitelist*, not by blacklist, so a payload field nobody has thought about
 * yet is withheld by default instead of exposed by default.
 *
 * The problem it solves: since #263 a `calendar-scan` payload carries a
 * `travel_blocks` section whose `adds[]` / `deletes[]` / `failures[]` entries
 * each hold a `calendar_id`, and in a real household those are **email
 * addresses**. A travel leg's `origin` / `destination` and an event's
 * `raw_location` are literal street addresses. Every purpose-built surface on
 * this page already honours the no-calendar-id rule — the #268 capability rows
 * render `label || person`, and #276's audit block is keyed on person and leg —
 * and the generic dump immediately beneath them did not.
 *
 * That matters more than a loopback-only admin panel would, because this webapp
 * is deliberately reachable beyond localhost (Tailscale TLS is live). Anything
 * painted into the DOM can end up in a screenshot, a browser cache or a shared
 * session, and screenshots of admin UIs travel.
 *
 * Scope: the generic `Run payload` dump, which only ever renders a **family**
 * run's payload (`renderFamilyDetail`). The message-pipeline trace fields next
 * to it — prompts, raw LLM response, parsed verdict — are deliberately verbatim
 * chat-analysis content that the Audit tab exists to show; running a
 * calendar-shaped whitelist over those would gut them without protecting
 * anything, since no calendar id or address reaches them. */

/** What a withheld value renders as. Visible on purpose: an operator has to be
 *  able to tell the dump is not the whole record, or they will read a redacted
 *  payload as a complete one. */
export const WITHHELD = '⟨withheld⟩';

/* Every key a family run payload may render. Deliberately exhaustive rather
 * than clever: the payload's key set is bounded (two run kinds, ~90 names), so
 * enumerating it is cheap and a regex over key names would be a blacklist
 * wearing a whitelist's clothes.
 *
 * Keys absent from this list and *why* they are absent — this is the part worth
 * reading before adding one back:
 *   calendar_id   a real email address in this household's configuration
 *   origin        a literal street address (or the live-fix label it stands in for)
 *   origin_label  same value, before it is copied to `origin`
 *   origin_latlng a raw GPS fix — the one thing more precise than an address
 *   destination   a literal street address
 *   raw_location  the source event's own location string, i.e. an address
 *   location      a travel block's `location` field, which *is* the destination
 *
 * `event` (the event title) IS renderable: the structured Event-decisions and
 * travel-block blocks directly above the dump already render titles, so
 * withholding it here would protect nothing and cost the dump its readability.
 * `dedup_key` is `person::event-title`, so it discloses nothing those blocks do
 * not already show, and it is how you answer "why did this not re-alert?".
 *
 * `detail` is the one whitelisted key carrying **free-form text from an
 * exception**, and it is renderable only because #285 also fixed it at source:
 * `src/family/travel_blocks_write.py` used to build it from raw `str(exc)`,
 * where `MarkerGuardError` prints two calendar ids verbatim and a propagated
 * googleapiclient `HttpError` prints the request URI with the URL-encoded id.
 * Those sites now go through `calendar_readonly.safe_error_detail`, as the read
 * path already did — and the raw text still reaches the log, so nothing is lost
 * that a local log cannot hold. Withholding `detail` instead would have cost the
 * dump its most useful field for exactly the runs you open it to diagnose. */
const RENDERABLE_KEYS = new Set([
  // envelope
  'kind', 'status', 'run_id', 'error', 'dry_run', 'reason', 'detail', 'text',
  // calendar-scan
  'conflicts', 'missing_locations', 'unknown_locations', 'decisions', 'summary',
  'live_coverage', 'travel_blocks', 'day', 'window', 'windows', 'assessed',
  'assumed', 'commute', 'video_link', 'source', 'person', 'event', 'start', 'end',
  'at_home', 'distance_from_home_km', 'margin_min', 'feasible',
  // traffic-check
  'checked', 'alerts', 'checked_at', 'anchor', 'departure_anchor', 'eta_min',
  'delay_min', 'normal_min', 'traffic_min', 'gap_min', 'depart_in_min',
  'dedup_key', 'leave_key', 'dedup_window_min', 'leave_margin_min', 'alerted',
  'leave_now_alerted', 'leave_now_suppressed', 'infeasible_suppressed', 'entry',
  'priced', 'unpriced',
  // presence
  'location_source', 'presence_status', 'presence_age_min', 'presence_refreshed',
  // travel blocks: plan
  'counts', 'desired', 'adds', 'deletes', 'keeps', 'protected', 'failures',
  'routes_calls', 'horizon_start', 'horizon_end', 'leg', 'minutes', 'hash',
  'schema_version', 'source_event_id', 'event_id', 'overrides',
  // travel blocks: apply
  'apply', 'inserted', 'deleted', 'kept', 'skipped', 'backups', 'operation',
  'delete_reason', 'write_capability', 'duplicate_calendars', 'label',
]);

/* Keys whose value is a **map keyed by data**, not by field names — for these
 * the sensitive string sits in *key* position, where a value-only whitelist
 * cannot see it. `write_capability` is `{calendar_id: state}`, so the ids are
 * the keys and every one of them is an email address in this household.
 *
 * Found by this fix's own sentinel test rather than by reading the payload,
 * which is the argument for asserting on a planted token across the whole
 * serialised DOM instead of on the fields you remembered to check.
 *
 * The keys are replaced positionally rather than dropped, so the diagnostic
 * content survives intact: how many calendars there are, and what state each
 * one is in. Only *which* calendar is withheld. */
const KEYED_BY_SENSITIVE = new Set(['write_capability']);

/** The payload with every non-renderable key's value replaced by {@link WITHHELD}.
 *
 * The *key* is kept — only its value is replaced — so the dump says "this field
 * exists and was withheld" rather than quietly having one fewer line. Recurses
 * through arrays and nested objects; a whitelisted key's nested object is
 * itself filtered, so `travel_blocks.adds[].calendar_id` is caught even though
 * `travel_blocks` and `adds` are both renderable.
 *
 * Non-object leaves pass through untouched, including `null`. */
export function redactPayload(value) {
  if (Array.isArray(value)) return value.map(redactPayload);
  if (value === null || typeof value !== 'object') return value;
  const out = {};
  for (const key of Object.keys(value)) {
    if (!RENDERABLE_KEYS.has(key)) { out[key] = WITHHELD; continue; }
    out[key] = KEYED_BY_SENSITIVE.has(key)
      ? redactMapKeys(value[key])
      : redactPayload(value[key]);
  }
  return out;
}

/** A `{sensitive_id: value}` map with its keys replaced positionally.
 *  The values are still redacted normally, and a non-object is passed through —
 *  the payload shape is not something to assume. */
function redactMapKeys(value) {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) {
    return redactPayload(value);
  }
  const out = {};
  Object.keys(value).forEach((key, index) => {
    out[`⟨withheld #${index + 1}⟩`] = redactPayload(value[key]);
  });
  return out;
}

/** Every key this payload had a value withheld for, deduped and sorted.
 *  Drives the one-line note above the dump, so the operator is told *which*
 *  fields are missing rather than having to spot `⟨withheld⟩` in the JSON. */
export function withheldKeys(value, found) {
  const seen = found || new Set();
  if (Array.isArray(value)) {
    value.forEach((item) => withheldKeys(item, seen));
  } else if (value !== null && typeof value === 'object') {
    for (const key of Object.keys(value)) {
      if (!RENDERABLE_KEYS.has(key)) seen.add(key);
      else if (KEYED_BY_SENSITIVE.has(key)) seen.add(`${key} (keys)`);
      else withheldKeys(value[key], seen);
    }
  }
  return Array.from(seen).sort();
}
