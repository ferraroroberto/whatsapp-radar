# Family checks — design, configuration, and rollout

Two deterministic scheduled checks plus one opt-in calendar-write feature, living alongside the WhatsApp/Gmail pipeline. They reuse this app's run store, notifier, config, and admin UI — but not its message-analysis core, and **no LLM runs in any of these loops**. That is a deliberate constraint carried over from a retired LLM-driven predecessor whose postmortem recorded duplicate-alert spam and hallucinated traffic status; detection here is plain Python (`src/family/`, `src/traffic/`), unit-tested offline.

| Feature | Verb | Default | Purpose |
| --- | --- | --- | --- |
| Traffic-jam insurance | `wr traffic-check` | off | alert when a household commute is about to run late |
| Calendar sync | `wr calendar-scan` | off | flag childcare coverage gaps and same-person overlaps |
| Commute travel blocks | rides inside `wr calendar-scan` | off + dry | maintain drive-time blocks on each person's calendar |

Everything household-identifying — home address, calendar ids, the responsibility pattern, childcare windows, the Routes API key — lives only in the gitignored `config/local.json`; `config/default.json` is the committed schema. Provisioning (Calendar OAuth + the Routes key) is in [`calendar-bootstrap.md`](calendar-bootstrap.md).

The README's [Family checks](../README.md#family-checks-calendar-conflicts-traffic-travel-blocks) section is the short version; this file is the reference.

---

## Traffic-jam insurance (`wr traffic-check`)

Finds each household member's next commute event, resolves the origin, prices the drive with the Google Routes API, and alerts on Telegram only when something is actually wrong. Two alerts can fire for one event and are deduped independently of each other.

### The two alerts

**Delay alert.** Fires when the traffic-aware drive exceeds the free-flow drive by `traffic.significant_delay_min`. The message stamps the moment the leg was priced for — `At 09:00 ~40 min vs 10 min normal` — rather than always claiming *Now*. Deduped so the same still-ongoing delay never re-alerts, and suppressed during quiet hours.

**Leave-now alert.** `🚗 Leave now — …: drive is ~22 min with traffic; it starts at 17:30.` Fires the moment `event.start − (now + eta + traffic.leave_margin_min)` reaches zero. It requires a **live** phone fix — it never fires from a calendar-inferred origin, because with no live position the app is making no claim about where the person is. Its timeliness is bounded by `traffic.cadence_min`: the nudge lands on the first check after the departure moment, so keep the cadence low if you rely on it.

The check also flags a **back-to-back hop it can't make**: when the traffic-aware drive between two chained events exceeds the gap between them, it sends a *"Tight schedule"* alert.

### Departure anchoring — which moment each leg is priced for

Google Routes v2 honours only `departureTime` for a `DRIVE` route. An `arrivalTime` request is *accepted and then silently ignored*, returning the depart-now baseline — so pricing a 09:00 school run at a 07:00 sweep against "arrival at 09:00" quietly judges it by 07:00 traffic. The anchor is therefore chosen per leg and recorded in the decision trace as `anchor` / `departure_anchor`:

| Leg shape | Anchor | Why |
| --- | --- | --- |
| Has a **live phone fix** | *depart-now* (no time field sent) | the leave-now nudge asks "if they set off **now**, do they make it?" |
| **Chained off a preceding event** | that event's end | the same instant the `gap_min` budget it is judged against is measured from |
| Chained, and that end has **passed** | *depart-now* (`depart_now_overdue`) | a driver still sitting there is late, not finished — 13:10 on a 13:00→13:15 hop is exactly when the warning is worth most |
| **From home, no live fix** | the event's start | the closest knowable departure; the true one is one drive-length earlier, which is the number the call is about to return |
| The leg's **own event has already started** | none — `anchor_in_the_past` | no departure left to price |

An `anchor_in_the_past` leg costs **no Routes call**: the API rejects a past `departureTime` outright, and both alerts it would have fed ("leave earlier", "you may not make it") are advice about a drive that is already decided. Reporting it as a fabricated routes error would hide a schedule fact behind a transport fault.

### Coverage numbers: priced vs not priced

A leg that established nothing about the road is never counted as a route that was checked, and is never folded into an all-clear:

- The CLI prints `0 route(s) priced, 3 not priced` rather than `3 route(s) checked`.
- `GET /api/family`'s run summary carries `priced` and `unpriced` beside the old `checked` total.
- The Execution funnel and the Audit drill-down render **Priced** and **Not priced** as two cells.
- A mixed run reads `no significant delay · 1 not priced` on the Dashboard.

A coverage number that silently counts non-coverage is how you end up trusting a check that has not been running. A leg carrying no `status` at all counts as **priced** — run rows written before the split have no such field and were genuinely all priced, so the other default would invent a gap that never existed.

### Train commutes

The ETA behind the leave-now nudge and the *"Tight schedule"* alert is a **driving** ETA, so it says nothing about a train departure — a daily by-train office run would otherwise nudge every morning for nothing. Any event whose title contains one of `traffic.train_keywords` (default `tren`, `train`, matched case-insensitively — e.g. `trabajo desde la oficina (en tren)`) is exempt from **both driving-ETA judgments** while `traffic.skip_leave_now_for_train` is on. Each skipped leg records `leave_now_suppressed` / `infeasible_suppressed` in its decision trace, so the Audit tab shows *why* nothing was sent.

The **delay** alert deliberately still fires for a train commute — congestion on the road is a real-world signal regardless of who is driving.

Edit `traffic.train_keywords` in `config/local.json` if a genuine *drive to the train station* is being caught.

### Dedup window floor

`traffic.dedup_window_min` (default 180) is **floored at the lookahead**. One event stays checkable for `traffic.lookahead_hours` and is re-checked every `traffic.cadence_min` throughout, so a dedup window shorter than the lookahead mathematically guarantees the same event alerts more than once. A shorter configured value is raised to the floor and the override logged — never silently ignored.

### Configuration (`traffic` in `config/local.json`)

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `false` | master switch (`WR_TRAFFIC_ENABLED`) |
| `api_key` | `""` | Google Routes API key |
| `significant_delay_min` | `15` | minutes of excess drive time that make a delay worth an alert |
| `leave_margin_min` | `5` | buffer added to the ETA before the leave-now nudge fires |
| `lookahead_hours` | `3` | how far ahead an event stays checkable |
| `cadence_min` | `30` | effective check frequency; the CLI self-skips inside this window |
| `dedup_window_min` | `180` | re-alert suppression, floored at `lookahead_hours` |
| `origin_lookback_min` | `60` | how far back to look for a preceding event to chain from |
| `quiet_start_hour` / `quiet_end_hour` | `20` / `5` | routine alerts suppressed inside this range (hard alerts bypass it) |
| `skip_leave_now_for_train` | `true` | exempt train commutes from both driving-ETA judgments |
| `train_keywords` | `["tren", "train"]` | title substrings that mark a leg as a train commute |

`significant_delay_min` and `leave_margin_min` were originally tuned against depart-now numbers; with per-leg departure anchoring in force they are worth re-checking against a few real mornings.

---

## Calendar sync (`wr calendar-scan`)

Scans the next few days of both household Google Calendars, flags coverage gaps against the fixed weekly responsibility pattern (who is home which afternoon, the childcare pickups) plus same-person two-places-at-once overlaps, and records a per-event decision trace — visible in the Audit tab — explaining every verdict.

Every live run sends exactly one Telegram summary: the findings, or an explicit all-clear. Coverage gaps and overlaps are hard alerts and bypass quiet hours; a routine all-clear inside quiet hours is suppressed.

### Events with no location

A no-location event is **assumed home**. The assumption is logged, the event is flagged `assumed` in the decision trace and listed under `missing_locations` in the run payload, and it is surfaced as a "please add a location" ask.

`family.ask_missing_locations` (default on — Family tab → *Daily calendar sync → Ask for missing locations*, or `WR_FAMILY_ASK_MISSING_LOCATIONS`) hides only the `📍 No location set` section, for a household where some events will never get a location and the daily repeat just drowns the coverage alerts it shares a message with. The setting silences the nag; it does not make the assumption invisible.

### `family.run_hour` — the earliest hour an unattended scan acts

`family.run_hour` (default `7`) is the earliest local hour an unattended scan will do anything. A fire before it self-skips, spending no Calendar read and no Routes call, and says so.

It is a **floor, not a fire time**: the App Launcher job decides when the verb is invoked (18:05 by default); this decides whether an *unforced* invocation acts. So the job can be armed as generously as you like and the effective earliest run follows the config with no re-arm. Set it to `0` to opt out entirely and let the job's schedule be the whole schedule.

An unusable value lands in the same place. Anything that is not an hour in `0..23` — a hand edit to `config/local.json` never passes the Family tab's own validation — is **read as `0`, with a `⚠️` warning naming it**, rather than clamped to the nearest hour. A knob whose only power is to suppress the daily safety check has to fail towards *not* suppressing it: honouring `25` would gate the scan off permanently, and honouring `"seven"` would take the webapp down on every request. `GET /api/family` then reports the `0` actually in force, not the value in the file, so the app never displays a schedule it does not honour.

An explicitly requested run ignores `run_hour` entirely: `wr calendar-scan --force`, and every button in the webapp — the Execution tab's Calendar-sync step and the Family tab's *Rehearse* / *Run sweep* — which all send `--force`, because the webapp schedules nothing of its own and a press at 22:00 is a person asking for a run now.

### Configuration (`family` in `config/local.json`)

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `false` | master switch (`WR_FAMILY_ENABLED`) |
| `run_hour` | `7` | earliest local hour an unattended scan acts; `0` disables the gate |
| `home_address` | `""` | the household origin used for inferred legs |
| `kids_home_time` | `"17:30"` | when the coverage requirement starts |
| `responsible_by_weekday` | `{}` | a person (or "nobody") per weekday |
| `childcare_windows` | `[]` | label + weekdays + start (and optional end) per window |
| `unknown_scan_days` | `7` | how many days of events to fetch |
| `assessment_days` | `2` | how many of those days are judged |
| `ask_missing_locations` | `true` | surface the "please add a location" ask |
| `travel_blocks` | see below | the commute-block feature |

Everything on that table except `home_address`, the calendar accounts, and `travel_blocks.horizon_days` is editable from the phone in the Family tab's *Rules in force* card. Edits save straight to `config/local.json` and the next run picks them up — no restart. Server-side validation rejects a bad save with a message naming the field: every time must parse as `HH:MM`, the on-duty pattern must name exactly the 7 weekdays, and a childcare window's end must come after its start.

### Duplicate calendar entries are collapsed

A `calendar.accounts` entry sharing its `calendar_id` with an earlier one — a calendar belongs to one person by construction — is **collapsed to that first entry at config-parse time**, never left to reach the travel-blocks reconcile: two accounts pointed at one physical calendar would otherwise churn 2 deletes + 2 inserts on every sweep forever.

The collapse is loud, never silent. A warning names both calendars **by label**, never by the raw calendar id, and the dropped label rides the Family tab's travel-blocks payload as `duplicate_calendars` (an always-present list, empty when nothing collided), so a misconfigured `config/local.json` is visible without reading a log. Refusing to boot on the duplicate was considered and rejected: it would take down a live app for a household whose config already has the mistake — a worse failure than the bounded, backed-up churn it prevents.

One real cost: if the dropped account was a person's *only* configured calendar, that person's key disappears from the daily coverage check's roster entirely (neither `away` nor `available`) until the duplicate entry is removed. The collapse warning names this explicitly.

---

## Commute travel blocks

`family.travel_blocks` makes the daily `wr calendar-scan` also compute the commute blocks each person's calendar *should* carry, and write the difference. It ships **off** (`family.travel_blocks.enabled: false`) and **dry** (`dry_run: true`), and always will: `config/default.json` is the committed schema, and turning it on is a per-household operator action — the [rollout runbook](#rollout-runbook) below.

### What it plans

An **outbound** block ending when a commuting event starts, and a **return-home** block after it unless the person chains straight on to the next event. Below `min_home_dwell_min` (default 45) of `drive_home + drive_out + dwell` gap, the round trip home buys nothing, so a direct A→B hop is assumed.

It reuses the events the scan already fetched — no second calendar read — over `travel_blocks.horizon_days` (default 2, matching `family.assessment_days`) from today's local midnight, clamped to the days the scan actually fetches (`max(unknown_scan_days, assessment_days)`). A horizon past the fetched events would orphan-delete every block beyond it on every run.

Each leg is priced by **one live traffic-aware Routes call for its own departure moment**, so a 07:00 sweep predicts the actual 09:00 traffic. Identical `(origin, destination, minute)` legs within one sweep share a single call, and the call count rides the run payload and the log so the per-leg billing stays visible.

Blocks are titled from `title_template` — never the destination, so a shared calendar view leaks nothing — carry the destination in `location` so tapping one opens navigation, and are busy, `private` and reminder-free.

**Skipped by design:** all-day events, `(en casa)` events, video-only or location-less events, a destination equal to the origin, and train commutes (`traffic.train_keywords` — a DRIVE duration is meaningless for a train).

### Reconcile — what gets written

The plan is reconciled against the blocks already on each calendar, and only the difference is written:

| Outcome | Meaning |
| --- | --- |
| **kept** | unchanged — zero API calls; re-running an unchanged sweep performs no writes at all |
| **re-inserted** | the source event moved or the block's hash changed: delete + insert |
| **deleted** | the source event was cancelled or left the horizon, and the block has not already happened |
| **protected** | left exactly where it is, with the reason that spared it |

A leg whose Routes call fails, is quota-rejected, or returns no route is **dropped and reported as `unpriced`** with its reason — never given a guessed or zero-length block, and never allowed to abort the sweep. An unpriceable drive has to look different from a drive that was correctly judged unnecessary. A block already on the calendar for such a leg is **protected**, counted alongside its `unpriced` failure.

Every `protected` entry names *which* unestablished fact spared it — `leg_not_planned`, `beyond_planning_horizon`, `start_not_established` — the same way every delete names what made its block stale. **Failing to establish what a block should look like is not a reason to remove it**, so a transient Routes outage cannot wipe the horizon's blocks, and a second sweep later the same day never deletes what the morning's sweep correctly wrote. Re-running the scan is safe at any hour.

The feature being off, no `traffic.api_key`, or no `family.home_address` each report themselves as their own status — `disabled` / `no_routes_api_key` / `no_home_address` — with zero Routes calls, rather than as an empty plan that would read like a computed all-clear.

### Horizon edges — two Calendar API asymmetries

The marker-scoped listing that finds this app's own blocks has to work around both ends of Google's `timeMin`/`timeMax` semantics.

**Start edge.** `timeMin` is an exclusive lower bound on an event's *end*, and the listing starts at the same local midnight the horizon does. A block that ended **before today's local midnight** is never returned and therefore never judged. Earlier *today* is still fair game: a block that ended at 08:00 this morning is listed and judged like any other.

The consequence is that **past blocks are not retro-cleaned**. When a source event drops off the horizon's start edge as the day rolls forward, the blocks it left behind on previous days survive on the calendar indefinitely, while the ones from today are deleted as orphans. Two blocks of one departed event get opposite fates, and from the calendar owner's side the split looks arbitrary. It is stated here because it is deliberate, not because it is tidy: widening the read backwards would expose every historical block this feature ever wrote to the orphan sweep at once, so the first run after such a change would be a mass delete dressed as a cleanup. The leftovers are past-dated clutter in calendar history, never noise in the days anyone is planning, so they are removed deliberately and by hand — [step 7](#7-undo-removing-every-block-the-feature-ever-created) of the runbook.

**End edge.** `timeMax` is exclusive on an event's *start*, so the listing deliberately reads **past** the horizon — up to the latest in-horizon source event's end (where its return block starts) plus a day. Without that padding, the return block of an evening event running past local midnight is never returned, so every sweep re-inserts it and the duplicates stack up until the horizon rolls forward.

Widening the read never widens the plan. A block found out there with no desired counterpart is **left exactly where it is** and reported as `protected` with reason `beyond_planning_horizon`, never orphan-deleted: nothing was computed for that region, so "no counterpart" says nothing about it.

### Deletion safety

- The reconcile lists **only this app's own blocks**, server-side, via `privateExtendedProperty=wr_travel_block=1`. A human's event is never even fetched.
- The one code path to a calendar delete takes the *fetched event resource* and refuses it loudly — a `MarkerGuardError`, never a silent skip — unless it carries that marker.
- Every delete writes the full resource as JSON to `data/calendar_backups/<YYYY-MM-DD>/` **before** the API call. A failed backup aborts that delete, so an unbacked delete cannot happen.
- `travel_blocks.dry_run` ships **true** even once `enabled` is flipped. A dry run computes and logs the complete add/delete plan and performs zero inserts, zero deletes and zero backup writes — it never even builds a write client.
- Write capability is probed per person from the calendar's `accessRole` (non-mutating) and recorded as `writable` / `not_writable` / `unknown`. Only `writable` is written to; `unknown` is skipped and reported as its own state, never folded into either of the others.
- A missing or revoked write token, a failed insert, and a failed delete each degrade to a recorded status in the run payload's `travel_blocks.apply` section, and never abort the scan.

### Running and reading a sweep from the phone

The **Family** tab's *Travel blocks* card drives everything. A banner states which of three modes is in force: **Off** (no plan, no Routes call), **Dry run** (the plan is computed and logged, nothing is written) or **Live** (blocks are created and removed).

*Run a sweep* offers **Rehearse (dry run)** and **Run sweep**. Both post the existing `calendar-scan` verb to `POST /api/execution/run` — the same pipeline the Execution tab and the scheduled job use — because the sweep rides along inside the daily scan and a travel-blocks-only verb would need a whole second calendar read path. Rehearse sends `--dry-run`, which forces the sweep dry *server-side* as well as suppressing the summary message.

**Run sweep** is unavailable — with every reason spelled out, never silently greyed out — whenever travel blocks are off, `dry_run` is on, the write token is missing, there is no Routes API key, or no home address is configured. Those reasons are *reported* by `GET /api/family` as `live_sweep_blockers`; they are *enforced* independently by `gate_status` and the apply short-circuit, which re-run on every invocation, so the button is a courtesy and never the thing standing between a click and a calendar write. A second run while one is in flight returns `409` and reads as "already running", not as a failure. When a run finishes, the card re-reads `/api/family` so **Last sweep** shows it with no page reload. Opening or rendering the tab never triggers a sweep — only a button press does.

*Write access per calendar* lists every configured household calendar in one of three states — **Writable**, **Not writable**, **Unknown** — plus the last sweep's counts (legs, adds/deletes/keeps, Routes calls, unpriced legs, and what was actually written). `Unknown` renders as its own state, with its own colour, its own glyph and a dashed outline: it is never a tick, never a cross, and never an omitted row, because an unresolved probe is not permission. It is what you see before the first sweep, and if the calendar listing itself failed — the fix is to run a sync and re-read the card, never to assume the calendar is fine. The whole card renders from the newest recorded `calendar-scan` run, so opening the tab spends no Routes quota.

**In the Execution tab,** a `calendar-scan` row's funnel carries the sweep's headline numbers (block adds, block deletes, unpriced legs), or one cell naming its gate when the sweep never computed a plan.

**In the Audit tab,** the drill-down renders the whole record above the raw payload dump: mode, counts, Routes calls, horizon and what was written; then each planned block as person · leg · event title · time box · minutes; each removal and each **left alone** block with the reason that spared it; and each unpriced leg with its `reason` discriminator. `protected` keeps its own heading rather than being folded into `keeps` — "left alone because its leg could not be priced" is a warning, "checked and already right" is not. A gated sweep is named by its gate and never rendered as a plan of zeros: `dry_run` and `counts` come back `null` precisely so nothing can dress "nothing was computed" up as "nothing needed doing".

### Redaction in the Audit payload dump

No calendar id appears anywhere in the rendered travel-block section — lines are keyed on person and leg, as the write-capability rows are — **nor in the raw `Run payload` dump beneath it**. That dump is redacted by **whitelist**, so a payload field nobody has thought about yet is withheld by default rather than exposed by default: recognised keys render, anything else renders as `⟨withheld⟩`, and a map *keyed* by calendar id (`write_capability`) keeps its states while its keys become `⟨withheld #1⟩`.

The dump is redacted, never gutted — counts, reasons, statuses, timings and hashes all survive, because it is still the only place the complete record is visible. A line above it **names** the withheld fields rather than leaving the operator to spot the marker, so a redacted dump can never be mistaken for a complete one.

This matters more than it would for a loopback-only panel: the webapp is deliberately reachable over Tailscale, and anything painted into the DOM can end up in a screenshot or a browser cache.

One field is fixed at the *source* rather than the renderer: a write failure's `detail` is free-form exception text — the marker guard's refusal names two calendars, and a Google API error stringifies to the request URI — so those sites go through the same `safe_error_detail` sanitiser the read path uses. The full exception still reaches the log; it just stops being persisted into a payload that a browser renders.

### Configuration (`family.travel_blocks` in `config/local.json`)

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `false` | master switch (Family tab → *Write travel blocks*) |
| `dry_run` | `true` | compute and log the plan, write nothing |
| `horizon_days` | `2` | days from today's local midnight to maintain; clamped to the days the scan fetches |
| `min_home_dwell_min` | `45` | gap below which a return home is not planned (0–480, server-validated) |
| `title_template` | `🚗 Trayecto` | block title; non-blank, ≤60 chars, server-validated |

`min_home_dwell_min` and `title_template` are editable from the Family tab. `horizon_days` stays a file edit — widening it past the days the daily scan fetches is clamped, so change it with `family.unknown_scan_days` in view. Calendar ids and the home address stay read-only and file-provisioned.

---

## Rollout runbook

Turning travel blocks on, and undoing it. Steps 1–2 are interactive Google steps that no code can do for you. **Nothing writes to a calendar until step 5.** Do the steps in order and do not improvise around them.

### 1. Grant the write-scope token

From the repo root, once, interactively — it opens the system browser and asks for `calendar.events` only, never the broader `calendar` scope:

```powershell
.\.venv\Scripts\python.exe -m scripts.auth_calendar_write
```

It writes `auth/calendar/write_token.json`. Confirm it is ignored without printing it:

```powershell
git check-ignore auth\calendar\write_token.json
```

Then verify the write path end-to-end. This creates one throwaway event a few minutes out, confirms it round-trips, and deletes it again, leaving nothing behind:

```powershell
.\.venv\Scripts\python.exe -m calendar_write.smoke --calendar <your-calendar-id>
```

Full detail in [`calendar-bootstrap.md`](calendar-bootstrap.md).

**Undo:** delete `auth/calendar/write_token.json` and revoke the grant at [myaccount.google.com/permissions](https://myaccount.google.com/permissions). With no write token the sweep degrades to a recorded `no_write_token` status and writes nothing — it does not crash and it does not delete.

### 2. Re-share every household calendar at "Make changes to events"

The sharing level the conflict scan and the traffic check were set up with — *"See all event details"* — is a read grant, and is not enough to write a block. Each other member opens Google Calendar → their calendar's *Settings and sharing → Share with specific people* → change the bootstrapping account's permission to **Make changes to events**.

Then open the **Family** tab → *Travel blocks* → *Write access per calendar* and confirm every row reads **Writable**. A row reading **Unknown** is not "probably fine" — it means the role was never established (no sweep has reported on that calendar yet, or the listing failed); run step 3 and re-read the card. A row reading **Not writable** is still shared read-only. Only `writable` calendars are ever written to.

**Undo:** set the sharing back down to *See all event details*; the next sweep reports that calendar `not_writable` and leaves it alone.

### 3. Enable, with `dry_run` still on

Family tab → *Travel blocks* → turn **Write travel blocks** on and leave **Dry run** on. The card's banner must read **Dry run**. (Equivalently, `family.travel_blocks.enabled: true` in `config/local.json` — never in `config/default.json`.) No restart: the next run picks it up.

**Undo:** turn the toggle back off; a disabled sweep computes nothing and spends no Routes call.

### 4. Rehearse, and read the plan for a real day

Press **Rehearse (dry run)** on the same card, or:

```powershell
.\wr.bat calendar-scan --dry-run --force
```

`--force` is what makes it run regardless of `family.run_hour`; `--dry-run` forces the sweep dry server-side, so this cannot write even if step 5 has already happened.

Then open the **Audit** tab, select that run, and read the travel-block section: every block it would **add** (person · leg · event title · time box · minutes), every block it would **delete** *with the reason*, every block **left alone** with the reason that spared it, and every **unpriced** leg. Two things to confirm before going further:

1. The adds match the journeys you actually expect for a day with a known school run.
2. **Nothing outside that plan is listed for deletion.**

Rehearse as many times as you like — a dry run performs zero inserts, zero deletes and zero backup writes, and never even builds a write client. It does spend Routes calls (see [What it costs](#what-it-costs)), which is the only reason not to loop it.

### 5. Go live

Only now turn **Dry run** off. The banner must read **Live**. The next scheduled `family-radar-calendar-sync` writes the plan; **Run sweep** on the Family tab does it immediately. Re-read the Audit record afterwards: `apply` reports what was actually inserted, deleted, kept, skipped, and how many backups were written. Watch one real day before walking away.

**Undo:** turn **Dry run** back on. That stops all further writing instantly, but it does **not** remove the blocks already written — see step 7.

### 6. Undo: restoring one wrongly-removed event

Every delete writes the complete fetched event resource as JSON under `data/calendar_backups/<YYYY-MM-DD>/` **before** the API call, and a failed backup aborts that delete.

**Find it by the date directory, not by the filename.** The name is `<calendar>-<event-id>.json`, but both halves are sanitized for Windows first (`_safe_component`): every character outside `A-Z a-z 0-9 . _ -` becomes `_`, and each half is cut to 80 characters. So a calendar id `parent-a@example.com` lands as `parent-a_example.com`, and grepping for the literal `@` address finds nothing — exactly the wrong discovery to make mid-incident. The dated directory holds every delete from that day, so listing it is the reliable route. The file itself is the raw Google event: `summary`, `start`, `end`, `location`, `description`, `extendedProperties`, ids, timestamps.

Note what can actually be lost: the one code path to a delete refuses, loudly, any event that does not carry this app's own `wr_travel_block=1` marker — so **the worst case is losing an auto-generated travel block, never a human's event**. In order of effort:

1. **Do nothing.** If the block is still wanted, the next sweep re-creates it, because the reconcile writes whatever the plan says is missing.
2. **Check the calendar's Bin** in the Google Calendar web UI, which normally holds recently deleted events and restores them in one click.
3. **Re-create it by hand** from the backup JSON's `summary` / `start` / `end` / `location`, which needs no credentials and takes a minute.

`data/` is gitignored and these files hold real calendar content — do not move them out of it, and do not commit them.

### 7. Undo: removing every block the feature ever created

Turning the feature off does **not** remove existing blocks: a disabled sweep computes nothing, so it also deletes nothing, and blocks beyond the planning horizon are deliberately `protected` rather than orphan-deleted. Removing them is a separate, deliberate act — and it is *bounded*. This is also the route for the [past-dated leftovers](#horizon-edges--two-calendar-api-asymmetries) no sweep ever revisits.

Every block this app writes carries the private extended property **`wr_travel_block=1`**, and Google Calendar's list API filters on exactly that: `privateExtendedProperty=wr_travel_block%3D1` on `GET /calendars/{calendarId}/events` returns this app's blocks and **nothing else**, no matter what else is on the calendar. That marker is the difference between a bounded cleanup and hand-auditing a shared calendar, which is why it is written here rather than left to be discovered mid-incident.

Practically:

1. Turn **Dry run** on (or **Write travel blocks** off) first, so nothing re-appears behind you.
2. Enumerate with that filter — Google's API Explorer against `calendar.events.list`, or any script using the same `auth/calendar/write_token.json`.
3. Delete what it returns. Deletions land in the calendar's Bin, so a mistake here is recoverable too.

In the Google Calendar UI the same set is *approximately* findable by searching for the block title (`travel_blocks.title_template`, default `🚗 Trayecto`) — approximate because the title is configurable and a human could have used the same words, whereas the marker cannot be typed by accident.

There is deliberately no `wr` verb for this: a one-way bulk delete is not something to make one keystroke away.

### What it costs

One Google Routes call per distinct `(origin, destination, departure minute)` leg per sweep — roughly two calls per commuting event (out and back), deduplicated within the sweep, and zero for a gated or already-satisfied sweep. The count rides every run payload (`travel_blocks.routes_calls`), the log line, and the Family tab, so the billing is never invisible.

Pre-delete backups land in `data/calendar_backups/<YYYY-MM-DD>/`, inside the already-gitignored `data/` tree. They hold real calendar content, so do not move them.

---

## Live phone location (presence)

When the `presence` block is enabled (`config/default.json` / `WR_PRESENCE_ENABLED`), the traffic check reads the responsible parent's live position from [home-automation](../../home-automation)'s read-only presence API — `GET http://127.0.0.1:8447/api/presence` (loopback bypasses its bearer token), `POST /api/presence/refresh` to force a fresh Find My locate.

Freshness is derived **client-side from `last_seen`**, never from the API's own `stale` flag, which is hard-coded `false` for iCloud entities. A fix newer than `max_age_min` (5 min) is used as-is; a stale one triggers one bounded refresh + re-read; anything else — feature off, home-automation down, person not tracked, no usable fix — falls back cleanly to calendar inference.

Presence is **disabled by default**, and the family checks work with no home-automation running at all. Person resolution matches the whatsapp-radar person key against each entity's display name / role; `presence.person_aliases` adds role hits like `dad` / `mom`.

**Privacy:** raw coordinates are used only to build the one outbound Routes request. They are never written to the DB, run traces, or logs — only derived values (freshness age, distance, ETA minutes).

### Presence over HTTPS

home-automation may serve `:8447` with its Tailscale certificate (HTTPS-only). Two deployment shapes work:

- `presence.base_url = https://<host>.ts.net:8447` — verifies fully, but requires that host's bearer token, since the loopback auth bypass no longer applies.
- `https://127.0.0.1:8447` with `presence.verify_tls = false` — the cert's `ts.net` hostname can never match an IP literal, and the relaxation is safe only because the loopback hop never leaves the machine.

`verify_tls` defaults to `true` (env override `WR_PRESENCE_VERIFY_TLS`); keep it that way for any non-loopback base URL.

### Live coverage judgment

With presence enabled, the calendar sync additionally judges **today's imminent childcare windows** (within `traffic.lookahead_hours`) by the responsible parent's phone → home ETA. Leaving now and still arriving late raises a `coverage_eta` hard alert: *"~N min from home but 'kids home' starts at 17:30 — M min short even leaving now."*

The judgment is **additive** — it never suppresses a calendar-based coverage gap, because position and calendar answer different questions (where the parent *is* vs. what they *intend*). Windows beyond the lookahead, days beyond today, and every presence-unavailable case remain governed by calendar inference alone. Each run's `live_coverage` trace (Audit → run detail) records the source, freshness age, ETA, and margin per window — never coordinates.

---

## Scheduling

The three App Launcher jobs are listed in the README's [Home-stack wiring](../README.md#home-stack-wiring-app-launcher) section and provisioned in [`bootstrapping.md`](bootstrapping.md) Step 7. Two scheduling decisions are worth recording here.

### Why the calendar sync is its own job

`family-radar-calendar-sync` is deliberately its own job on its own schedule rather than chained after the message scan: a dead WhatsApp sidecar must never suppress the family calendar summary.

### Why 18:05, and what that costs

The evening slot is chosen for the *summary*: it lands when the household is together and tomorrow is still changeable, and it is late enough that the day's own calendar edits are in. The travel-block sweep inherits that slot.

`travel_blocks.horizon_days` defaults to `2` and the window runs from **today's local midnight to midnight + `horizon_days`** (`src/family/travel_blocks.py::travel_block_horizon`), so an 18:05 sweep maintains blocks for the rest of today and all of tomorrow — and *nothing* of the day after, since the window ends at 00:00 on it. Tomorrow morning's school run is written the evening before, which is the point.

The honest cost: **an evening sweep prices tomorrow morning's drive some thirteen hours ahead of it.** The minutes on the block are a traffic-aware forecast for that departure moment, not a live reading, and if the real morning differs, nothing re-prices the block until the next sweep. Catching that on the day is the traffic check's job — `family-radar-traffic-check`, armed every 5 minutes, which prices each leg for its own departure moment and alerts — not the sweep's.

This is recorded as a decision, not an inherited accident: the sweep's value is a *maintained calendar* over today and tomorrow, and one nightly reconcile delivers that at roughly two Routes calls per commuting event per day. A second, morning fire is the obvious upgrade if the blocks ever need to reflect same-day changes — add a second Job (or arm the one Job hourly) and set `family.run_hour` to the earliest hour you want it acting, which is exactly what that knob is for.

Whatever you arm, keep `family.run_hour` at or below the earliest armed hour. At the shipped default of `7` against an 18:05 job it never gates; raising it above 18 would skip every day — logged and recorded as a skip each time, but with no summary sent and no sweep run.

---

## Run records and self-skips

Every check execution — CLI, scheduled App Launcher Job, or webapp-launched — records a run row in the unified run store, so full per-run detail (every route checked, every conflict) is inspectable in the Execution and Audit tabs regardless of who launched it. A `--dry-run` never sends an alert, but the run row itself is recorded and badged.

The exceptions are the two **self-skips**, which record no run row at all: a fire that deliberately did nothing is not a run, and recording one every few minutes would drown the Audit tab in no-ops. Both still write the usual *filesystem* run record (`webapp/runs/<kind>/…`), which reads as `skipped` with its reason, so the fire stays auditable without polluting the run history the Family and Audit tabs reason over. Both are hidden from **Recent runs** behind an "N self-skipped runs hidden" line rather than deleted, and stay fetchable with `GET /api/execution/runs?include_skipped=true`.

| Self-skip | Trigger | Exemption |
| --- | --- | --- |
| `traffic-check` cadence | a **live** fire landing before `traffic.cadence_min` has elapsed | `--dry-run` — an explicit, on-demand human test pass (the Execution tab's *Run now (dry)*) always executes, and its run row never counts towards the live cadence clock |
| `calendar-scan` run hour | a fire before `family.run_hour` | `--force` — the *mode* is not the permission, so a scheduled `--dry-run` is gated like any other unattended fire, while every webapp button (which sends `--force`) always runs |

The run-hour skip returning before the run row is opened is also what keeps it out of the Family tab's "last sweep" lookup, which scans recorded `calendar-scan` runs for the newest travel-block section: a skip must never be mistaken for a completed sweep, nor blank out the genuine earlier one.
