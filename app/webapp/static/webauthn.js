/* Passkey enrollment for the admin webapp (no sign-in ceremony, #353).
 *
 * Enrollment is provisioning state only. Route access is gated by the
 * bearer-token middleware; loopback callers bypass that gate entirely on the
 * server side. Since #377 the devices and enrollment live in Settings' This
 * install -> Passkeys sheet, and the header lines say when the tray's
 * enrollment window is open. */

import { state } from './state.js';
import { jsonApi, toast } from './api.js';
import { icon } from './_vendored/icons/icons.js';
import { el, fieldRow, textInput } from './edit-sheet.js';

// ----------------------------------------------------------- b64url helpers
function b64urlToBuf(s) {
  s = String(s).replace(/-/g, '+').replace(/_/g, '/');
  const pad = s.length % 4 ? '='.repeat(4 - (s.length % 4)) : '';
  const bin = atob(s + pad);
  const buf = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) buf[i] = bin.charCodeAt(i);
  return buf.buffer;
}

function bufToB64url(buf) {
  const bytes = new Uint8Array(buf);
  let bin = '';
  for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
  return btoa(bin).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

function prepCreate(o) {
  o.challenge = b64urlToBuf(o.challenge);
  o.user.id = b64urlToBuf(o.user.id);
  (o.excludeCredentials || []).forEach(function (c) { c.id = b64urlToBuf(c.id); });
  return o;
}

function serializeReg(c) {
  return {
    id: c.id,
    rawId: bufToB64url(c.rawId),
    type: c.type,
    response: {
      attestationObject: bufToB64url(c.response.attestationObject),
      clientDataJSON: bufToB64url(c.response.clientDataJSON),
    },
    clientExtensionResults: c.getClientExtensionResults ? c.getClientExtensionResults() : {},
    authenticatorAttachment: c.authenticatorAttachment || undefined,
  };
}

// ----------------------------------------------------------- status
// Settings' Passkeys row and sheet and both header lines (Home, Settings)
// re-render from here when the status poll moves (#377).
const listeners = [];
export function onWebauthnChange(fn) { listeners.push(fn); }

export async function fetchWebauthnStatus() {
  try {
    state.webauthn = await jsonApi('/api/webauthn/status');
  } catch (_) { return; /* best-effort */ }
  for (const fn of listeners) fn();
}

// The Passkeys row's value: how many are enrolled, and whether the tray's
// enrollment window is open right now.
export function passkeySummary() {
  const w = state.webauthn || {};
  if (!w.configured) return { text: 'Not set up', tone: null };
  const n = (w.devices || []).length;
  return {
    text: (n ? n + ' enrolled' : 'None enrolled') + (w.enrollment_open ? ' · enrollment open' : ''),
    tone: w.enrollment_open ? 'accent' : null,
  };
}

/* What the sheet is drawn from, minus the countdown: a poll that only moves
 * the seconds updates the status line in place instead of redrawing (which
 * would drop a half-typed device name). */
export function passkeyShape() {
  const w = state.webauthn || {};
  return JSON.stringify([!!w.configured, !!w.enrollment_open,
    (w.devices || []).map(function (d) { return d.id; }), confirming]);
}

function statusText(w) {
  const n = (w.devices || []).length;
  return (n ? n + ' device(s) enrolled.' : 'No device enrolled yet.')
    + (w.enrollment_open ? ' Enrollment window open (' + w.enrollment_seconds_left + 's).' : '');
}

export function updatePasskeyStatus() {
  const line = document.getElementById('webauthnStatus');
  if (line) line.textContent = statusText(state.webauthn || {});
}

// ----------------------------------------------------------- the sheet

let deviceName = 'iPhone';
let confirming = null;   // the device whose removal waits on the in-sheet confirm
let redraw = function () {};

/* The Passkeys sheet (#377): the enrolled devices, each removable behind an
 * in-sheet confirm (no native dialog), and — only while the tray's enrollment
 * window is open — a name field and "Enroll this device". */
export function renderPasskeys(body, rerender) {
  redraw = rerender;
  const w = state.webauthn || {};
  if (!w.configured) {
    body.append(el('p', 'muted small', 'Passkeys are not set up on this install.'));
    return;
  }
  const status = el('p', 'muted small', statusText(w));
  status.id = 'webauthnStatus';
  body.append(status);

  const list = el('ul', 'webauthn-devices');
  list.id = 'webauthnDevices';
  (w.devices || []).forEach(function (d) {
    const li = document.createElement('li');
    if (confirming === d.id) {
      li.append(el('span', undefined, 'Remove "' + d.label + '"?'));
      const cancel = el('button', 'button-ghost btn-touch', 'Cancel');
      cancel.type = 'button';
      cancel.addEventListener('click', function () { confirming = null; redraw(); });
      const remove = el('button', 'button-tint danger', 'Remove');
      remove.type = 'button';
      remove.addEventListener('click', function () { removeDevice(d); });
      li.append(cancel, remove);
    } else {
      li.append(el('span', undefined, d.label + ' · added ' + d.added_at));
      const rm = document.createElement('button');
      rm.type = 'button';
      rm.className = 'icon-button danger';
      rm.innerHTML = icon('trash-2');
      rm.setAttribute('aria-label', 'Remove passkey');
      rm.title = 'Remove passkey';
      rm.addEventListener('click', function () { confirming = d.id; redraw(); });
      li.append(rm);
    }
    list.append(li);
  });
  body.append(list);

  if (w.enrollment_open) {
    body.append(fieldRow('Device name', textInput(deviceName, '', 60, function (v) { deviceName = v; })));
    const enroll = el('button', 'button-tint btn-block', 'Enroll this device');
    enroll.type = 'button';
    enroll.id = 'enrollDeviceBtn';
    enroll.addEventListener('click', function () { enrollDevice(deviceName.trim() || 'device'); });
    body.append(enroll);
  } else {
    body.append(el('p', 'muted small', 'Enrollment opens from the tray menu on the PC, for a few minutes at a time.'));
  }
}

async function removeDevice(d) {
  try {
    await jsonApi('/api/webauthn/devices/' + encodeURIComponent(d.id), { method: 'DELETE' });
    toast('Removed ' + d.label);
  } catch (exc) {
    toast('Remove failed: ' + (exc.message || exc), 'error');
  }
  confirming = null;
  await fetchWebauthnStatus();
  redraw();
}

async function enrollDevice(label) {
  if (!window.PublicKeyCredential) {
    toast('This browser has no passkey support.', 'error');
    return;
  }
  try {
    const opts = await jsonApi('/api/webauthn/enroll/begin', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ label: label }),
    });
    const cred = await navigator.credentials.create({ publicKey: prepCreate(opts) });
    await jsonApi('/api/webauthn/enroll/finish', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(serializeReg(cred)),
    });
    toast('Device enrolled.');
    fetchWebauthnStatus();
  } catch (exc) {
    toast('Enrollment failed: ' + (exc.message || exc), 'error');
  }
}
