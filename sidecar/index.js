/**
 * WhatsApp Radar — read-only linked-device sidecar.
 *
 * This long-running process is the ONLY component that speaks the unofficial
 * WhatsApp Web protocol (via Baileys). It keeps the Python core free of that
 * dependency. It is deliberately READ-ONLY: it pairs a linked device, listens
 * for chat metadata and messages, and appends what it sees to a local NDJSON
 * buffer that the Python `LinkedDeviceConnector` reads. It never sends a
 * message, reaction, read receipt, or any other write to WhatsApp.
 *
 * Storage layout (all under ignored paths, never committed):
 *   auth/                          linked-device credentials (multi-file auth)
 *   data/linked_device/chats.ndjson      one JSON line per chat upsert; also
 *                                        alias rows {jid, alias_for} mapping a
 *                                        contact's @lid form onto its phone JID
 *   data/linked_device/messages.ndjson   one JSON line per message
 *   data/linked_device/status.json       heartbeat for the Python `status()`
 *
 * WhatsApp addresses one contact under several JID forms (phone @s.whatsapp.net,
 * device-scoped <num>:<dev>@…, and the opaque privacy form <id>@lid). We normalize
 * every JID we write (jidNormalizedUser) and, whenever a contact event reveals the
 * @lid↔phone pairing, emit an alias row so the Python reader can fold both onto one
 * identity. This is unofficial-protocol behavior and may shift across Baileys
 * releases; the reader degrades gracefully (raw → humanized name) if it does.
 *
 * Lifecycle is explicit: run `npm start` (or `node index.js`) and leave it
 * running so it catches live messages. On first run it prints a QR code to
 * pair; the session persists and reconnects automatically afterwards.
 */

import { mkdirSync, appendFileSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import makeWASocket, {
  DisconnectReason,
  useMultiFileAuthState,
  fetchLatestBaileysVersion,
  jidNormalizedUser,
  isJidGroup,
  isJidUser,
  isLidUser,
} from "@whiskeysockets/baileys";
import qrcode from "qrcode-terminal";
import QRCode from "qrcode";

const __dirname = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(__dirname, "..");

// Paths are overridable by env so App Launcher / tests can redirect them.
const authDir = process.env.WR_AUTH_DIR || join(repoRoot, "auth");
const bufferDir =
  process.env.WR_LINKED_DEVICE_DIR || join(repoRoot, "data", "linked_device");
const chatsFile = join(bufferDir, "chats.ndjson");
const messagesFile = join(bufferDir, "messages.ndjson");
const statusFile = join(bufferDir, "status.json");
const qrPngFile = join(bufferDir, "qr.png");

mkdirSync(bufferDir, { recursive: true });

let chatsSeen = 0;
let messagesSeen = 0;
let currentConnected = false;
let currentPaired = false;

function isoFromTimestamp(ts) {
  // Baileys timestamps are seconds (number or Long-like). Default to now.
  const seconds =
    typeof ts === "number" ? ts : ts && typeof ts.toNumber === "function" ? ts.toNumber() : null;
  const ms = seconds ? seconds * 1000 : Date.now();
  return new Date(ms).toISOString();
}

function writeStatus(connected, paired) {
  currentConnected = connected;
  currentPaired = paired;
  const status = {
    paired,
    connected,
    last_update: new Date().toISOString(),
    chats: chatsSeen,
    messages: messagesSeen,
  };
  writeFileSync(statusFile, JSON.stringify(status), "utf-8");
}

// Periodic heartbeat so the Python reader can detect a dead/stale sidecar even
// when no messages are arriving (status() treats a stale file as disconnected).
setInterval(() => writeStatus(currentConnected, currentPaired), 30_000).unref();

function appendChat(jid, name, type) {
  if (!jid || jid === "status@broadcast") return;
  const norm = jidNormalizedUser(jid) || jid;
  appendFileSync(
    chatsFile,
    JSON.stringify({ jid: norm, name: name || null, type, ts: new Date().toISOString() }) + "\n",
    "utf-8",
  );
  chatsSeen += 1;
}

/**
 * Record that a contact's hidden @lid address and phone JID are the same person,
 * so the reader folds messages/metadata under either onto one chat row.
 */
function appendAlias(lidJid, phoneJid) {
  if (!lidJid || !phoneJid) return;
  const alias = jidNormalizedUser(lidJid) || lidJid;
  const target = jidNormalizedUser(phoneJid) || phoneJid;
  if (alias === target) return;
  appendFileSync(
    chatsFile,
    JSON.stringify({ jid: alias, alias_for: target, ts: new Date().toISOString() }) + "\n",
    "utf-8",
  );
}

function chatType(jid) {
  return isJidGroup(jid) ? "group" : "dm";
}

/**
 * Normalize one Baileys message into the buffer's flat shape, or null if it
 * carries nothing worth storing. Keeps the v1 normalization set explicit.
 */
function normalizeMessage(m) {
  const content = m.message;
  if (!content) return null;

  // Unwrap ephemeral / view-once / device-sent wrappers.
  const inner =
    content.ephemeralMessage?.message ||
    content.viewOnceMessage?.message ||
    content.viewOnceMessageV2?.message ||
    content.deviceSentMessage?.message ||
    content;

  if (inner.conversation) {
    return { text: inner.conversation, type: "text" };
  }
  if (inner.extendedTextMessage) {
    const isReply = Boolean(inner.extendedTextMessage.contextInfo?.quotedMessage);
    return { text: inner.extendedTextMessage.text || "", type: isReply ? "reply" : "text" };
  }
  if (inner.imageMessage) {
    return { text: inner.imageMessage.caption || "[image]", type: "image" };
  }
  if (inner.videoMessage) {
    return { text: inner.videoMessage.caption || "[video]", type: "video" };
  }
  if (inner.documentMessage) {
    const name = inner.documentMessage.fileName || "document";
    return { text: `[document: ${name}]`, type: "document" };
  }
  if (inner.audioMessage) {
    return { text: "[voice note]", type: "voice" };
  }
  if (inner.protocolMessage) {
    // type 0 = REVOKE (deleted); editedMessage carries an edit.
    if (inner.protocolMessage.editedMessage) {
      const edited = normalizeMessage({ message: inner.protocolMessage.editedMessage });
      return edited ? { text: edited.text, type: "edited" } : null;
    }
    if (inner.protocolMessage.type === 0) {
      return { text: "[deleted]", type: "deleted" };
    }
    return null;
  }
  if (inner.reactionMessage || inner.pollUpdateMessage) {
    return null; // reactions / poll votes are not actionable content in v1
  }
  return null;
}

function appendMessage(m) {
  const jid = m.key?.remoteJid;
  const msgId = m.key?.id;
  if (!jid || !msgId || jid === "status@broadcast") return;

  const norm = normalizeMessage(m);
  if (!norm) return;

  const participant = m.key.participant ? jidNormalizedUser(m.key.participant) : null;
  const record = {
    jid: jidNormalizedUser(jid) || jid,
    msg_id: msgId,
    ts: isoFromTimestamp(m.messageTimestamp),
    sender: m.pushName || (m.key.fromMe ? "me" : null),
    text: norm.text,
    type: norm.type,
    raw: {
      from_me: Boolean(m.key.fromMe),
      participant,
    },
  };
  appendFileSync(messagesFile, JSON.stringify(record) + "\n", "utf-8");
  messagesSeen += 1;
}

async function start() {
  const { state, saveCreds } = await useMultiFileAuthState(authDir);
  const { version } = await fetchLatestBaileysVersion();

  const sock = makeWASocket({
    version,
    auth: state,
    printQRInTerminal: false, // we render it ourselves below
    markOnlineOnConnect: false, // stay invisible; do not steal presence from the phone
    syncFullHistory: true, // pull history on first pair for incremental review
  });

  sock.ev.on("creds.update", saveCreds);

  sock.ev.on("connection.update", async (update) => {
    const { connection, lastDisconnect, qr } = update;
    if (qr) {
      console.log("\nScan this QR in WhatsApp → Linked devices → Link a device:\n");
      qrcode.generate(qr, { small: true });
      // Also write a crisp PNG (overwritten on each refresh) for headless pairing.
      QRCode.toFile(qrPngFile, qr, { width: 512, margin: 2 }).catch((e) =>
        console.error("Could not write QR PNG:", e),
      );
      writeStatus(false, false);
    }
    if (connection === "open") {
      console.log("Linked device connected. Listening read-only…");
      writeStatus(true, true);
      // One read-only call to label every group the account is in (subjects).
      try {
        const groups = await sock.groupFetchAllParticipating();
        for (const [jid, meta] of Object.entries(groups || {})) {
          appendChat(jid, meta.subject, "group");
        }
        console.log(`Labelled ${Object.keys(groups || {}).length} groups.`);
      } catch (e) {
        console.error("Could not fetch group subjects:", e);
      }
    }
    if (connection === "close") {
      const code = lastDisconnect?.error?.output?.statusCode;
      const loggedOut = code === DisconnectReason.loggedOut;
      writeStatus(false, !loggedOut);
      if (loggedOut) {
        console.log("Logged out on the phone. Delete auth/ and re-run to pair again.");
        process.exit(0);
      } else {
        console.log("Connection closed, reconnecting…");
        start();
      }
    }
  });

  // Contact names (for DMs) — only write rows that actually carry a name.
  // Contacts also reveal the @lid↔phone pairing (ct.id / ct.lid), which we record
  // as an alias so messages under either address land on one chat row.
  const onContacts = (contacts) => {
    for (const ct of contacts || []) {
      const name = ct.name || ct.notify || ct.verifiedName;
      if (ct.id && name) appendChat(ct.id, name, chatType(ct.id));
      const phone = isJidUser(ct.id) ? ct.id : isJidUser(ct.lid) ? ct.lid : null;
      const lid = isLidUser(ct.id) ? ct.id : isLidUser(ct.lid) ? ct.lid : null;
      if (phone && lid) appendAlias(lid, phone);
    }
  };
  sock.ev.on("contacts.upsert", onContacts);
  sock.ev.on("contacts.update", onContacts);

  // History sync after pairing: chats, contacts, and their messages.
  sock.ev.on("messaging-history.set", ({ chats, contacts, messages }) => {
    for (const c of chats || []) appendChat(c.id, c.name || c.subject, chatType(c.id));
    onContacts(contacts);
    for (const m of messages || []) appendMessage(m);
    writeStatus(true, true);
  });

  sock.ev.on("chats.upsert", (chats) => {
    for (const c of chats || []) appendChat(c.id, c.name || c.subject, chatType(c.id));
  });

  sock.ev.on("groups.update", (updates) => {
    for (const g of updates || []) if (g.id && g.subject) appendChat(g.id, g.subject, "group");
  });

  // Live messages.
  sock.ev.on("messages.upsert", ({ messages }) => {
    for (const m of messages || []) {
      const rj = m.key?.remoteJid;
      // On a 1:1 chat the remote's push name *is* the contact name, so feed it in
      // as the chat name — this is often the only label an unsaved contact ever gets.
      const dmName =
        rj && !isJidGroup(rj) && !m.key?.fromMe ? m.pushName || undefined : undefined;
      appendChat(rj, dmName, chatType(rj));
      appendMessage(m);
    }
    writeStatus(true, true);
  });
}

start().catch((err) => {
  console.error("Sidecar failed:", err);
  writeStatus(false, false);
  process.exit(1);
});
