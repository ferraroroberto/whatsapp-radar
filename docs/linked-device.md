# Linked-Device Connector — Design & Risk

Reference for the real WhatsApp Web linked-device connector. For the step-by-step operator guide, see [`manual.md`](manual.md).

## Why a sidecar

WhatsApp has no official API for personal/group chats. The practical path is a **Web linked-device** integration, which is **unofficial** and not a supported WhatsApp Business Platform use case. To contain that risk and keep the unofficial library out of our Python business logic (a fleet rule in `CLAUDE.md`), the protocol is spoken by a small **Node sidecar** built on **[Baileys](https://github.com/WhiskeySockets/Baileys)** (`@whiskeysockets/baileys`). The Python core never imports it; it only reads a local file buffer the sidecar writes.

```
WhatsApp  ──▶  Node sidecar (Baileys)  ──▶  data/linked_device/*.ndjson  ──▶  LinkedDeviceConnector (Python, read-only reader)  ──▶  pipeline
```

## Unofficial-library risk

- **Terms of Service.** Automating WhatsApp Web via an unofficial library is outside WhatsApp's official use cases and could, in principle, lead to a number being limited or banned. Mitigations baked in: **read-only** behaviour (no send/react/read-receipt/admin anywhere in the surface), `markOnlineOnConnect: false` so we don't steal presence from the phone, and no bulk/scraping behaviour beyond the chats the account already sees.
- **Library churn.** Baileys is a community reverse-engineering effort; the protocol and the library API change. Pin the version, and expect to update the sidecar occasionally. The buffer contract below is the stable boundary, so churn is isolated to `sidecar/index.js`.
- **Consent.** Only pair an account with the explicit consent of its owner. For personal/family use this is the operator and their household.

## Buffer contract (the stable boundary)

The sidecar appends NDJSON (one JSON object per line) under the ignored `data/linked_device/` directory. Append-only files are crash-safe and need no cross-language locking; duplicates are expected and resolved by last-write-wins on read, then again by storage dedupe.

- `chats.ndjson` — `{ "jid", "name", "type", "ts" }` (`type` is `group` or `dm`), plus *alias* rows `{ "jid", "alias_for", "ts" }` (see JID identity below).
- `messages.ndjson` — `{ "jid", "msg_id", "ts", "sender", "text", "type", "raw" }`.
- `status.json` — heartbeat `{ "paired", "connected", "last_update", "chats", "messages" }`, rewritten on every event and every 30s. The Python reader treats a `last_update` older than 120s as a dead sidecar.

The Python reader (`connector/linked_device.py`) maps `jid → ChatRecord.source_chat_id` and `msg_id → MessageRecord.source_message_id`, sorts messages by `(ts, msg_id)`, and exposes only read methods.

## JID identity & display names

WhatsApp addresses one contact under several JID forms: a phone JID (`<number>@s.whatsapp.net`), a legacy business form (`@c.us`), a device-scoped form (`<number>:<device>@…`), and an opaque privacy form (`<id>@lid`). Chat-metadata events and the messages they describe can arrive under *different* forms for the same identity. Left unreconciled this strands a chat with a raw-JID name or with **zero associated messages** (the messages keyed under a variant never match the chat row).

Both sides reconcile this so there is one row per identity:

- **Normalization.** Every JID is canonicalized — lower-cased, agent/device suffix dropped, `@c.us → @s.whatsapp.net` — by the sidecar at write time (Baileys `jidNormalizedUser`) and again by the reader, which keys both chats and messages by the canonical form so a variant-keyed message still associates.
- **Aliases.** The `@lid ↔ phone` pairing is known only to the protocol layer, so the sidecar emits an `alias_for` row whenever a contact event reveals it (`contact.id` / `contact.lid`). The reader folds aliases before keying, collapsing the `@lid` JID onto its phone JID. Alias rows are not themselves chats.
- **Readable fallbacks.** When no saved name exists, a DM is named from the remote's `pushName`; failing that, the reader shows a formatted `+<number>` rather than the raw `<number>@…` JID. Group messages with no `pushName` (common in history sync) fall back to a humanized participant label so the conversation overlay still attributes a sender.

This handling is unofficial-protocol behavior and may shift across Baileys releases; the reader degrades gracefully (canonical name → push name → `+<number>` → bare id) if the alias signal is absent.

## v1 message normalization set

| WhatsApp shape | `message_type` | `text` |
| --- | --- | --- |
| Plain text / extended text | `text` | the body |
| Reply (quoted message) | `reply` | the reply body (quoted ref kept in `raw`) |
| Image / video with caption | `image` / `video` | the caption, or `[image]`/`[video]` |
| Document | `document` | `[document: <filename>]` |
| Voice note / audio | `voice` | `[voice note]` |
| Edited message | `edited` | the new body (last-write-wins over the original) |
| Deleted message (revoke) | `deleted` | `[deleted]` |
| Reactions, poll votes | — | dropped (not actionable content in v1) |

**No media bytes are downloaded** — read-only, privacy-preserving, and unnecessary for text classification. Voice-note transcription (via the hub's whisper endpoint) is a possible follow-up, not part of v1.

## First Spike Questions — answers

These are the questions from [`onboarding.md`](onboarding.md), answered by this implementation.

1. **Can a linked-device connector reliably pair and reconnect on the target Windows host?** Yes. Baileys `useMultiFileAuthState('auth/')` persists the session; pairing is a one-time QR scan. `connection.update` drives automatic reconnect on transient drops; only a phone-side logout (`DisconnectReason.loggedOut`) requires re-pairing.
2. **Can it receive enough chat history and new-message events for incremental review?** Yes. `syncFullHistory: true` plus the `messaging-history.set` event provide initial history; `messages.upsert` provides the live stream. Both feed the same buffer, so review sees a continuous timeline.
3. **Are message IDs stable enough to use as cursors?** Yes. WhatsApp's per-message `key.id` is stable and unique per chat; we store it as `source_message_id`. Cursoring is owned by storage on `(message_timestamp, id)`, so even ties are ordered deterministically.
4. **Can the connector run without any write side-effects from our code?** Yes, by construction. The `MessageConnector` Protocol has no write method, the Python class is a pure file reader, and the sidecar only subscribes to events — it never calls a send/react/read API. `markOnlineOnConnect` is disabled.
5. **Does the primary phone keep receiving normal notifications while the connector is online?** Yes. A linked device is additive; the phone remains the primary and keeps its own notifications. We do not mark messages read.
6. **How does offline catch-up behave after the service is stopped for several hours?** On reconnect, Baileys delivers the messages received while offline (subject to WhatsApp's history window). Because storage dedupes idempotently and cursors only track what was analysed, catch-up simply appears as a larger delta on the next review.
7. **What message shapes are normalized for v1?** See the table above.

## Limitations / known gaps

- History depth is bounded by what WhatsApp syncs to a freshly linked device; very old history may be unavailable.
- Group sender labels come from `pushName`, falling back to a humanized participant JID when a history-synced message carries none; they are local-only and never committed.
- The sidecar lifecycle is currently manual (`npm start`); supervised running via App Launcher is a follow-up.
