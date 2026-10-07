/* Passkey enrollment for the admin webapp (no sign-in ceremony, #353).
 *
 * Enrollment is provisioning state only. Route access is gated by the
 * bearer-token middleware; loopback callers bypass that gate entirely on the
 * server side. */

import { els, state } from './state.js';
import { jsonApi, toast } from './api.js';
import { icon } from './_vendored/icons/icons.js';

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

// ----------------------------------------------------------- flows
export async function fetchWebauthnStatus() {
  try {
    state.webauthn = await jsonApi('/api/webauthn/status');
    renderWebauthn();
  } catch (_) { /* best-effort */ }
}

function renderWebauthn() {
  const w = state.webauthn || {};
  if (!els.webauthnStatus) return;
  // The enrollment card only exists while the tray's enrollment window is open
  // (the everyday UI carries no settings surface — the tray menu opens the
  // window, then this card appears on the Dashboard).
  const show = !!(w.configured && w.enrollment_open);
  els.enrollCard.hidden = !show;
  if (!show) {
    els.webauthnDevices.innerHTML = '';
    els.enrollDeviceBtn.hidden = true;
    return;
  }
  const n = (w.devices || []).length;
  const msg = (n ? n + ' device(s) enrolled.' : 'No device enrolled yet.')
    + ' Enrollment window open (' + w.enrollment_seconds_left + 's).';
  els.webauthnStatus.textContent = msg;
  els.webauthnDevices.innerHTML = '';
  (w.devices || []).forEach(function (d) {
    const li = document.createElement('li');
    const label = document.createElement('span');
    label.textContent = d.label + ' · added ' + d.added_at;
    li.appendChild(label);
    const rm = document.createElement('button');
    rm.type = 'button';
    rm.className = 'icon-button danger';
    rm.innerHTML = icon('trash-2');
    rm.setAttribute('aria-label', 'Remove passkey');
    rm.title = 'Remove passkey';
    rm.addEventListener('click', function () { removeDevice(d); });
    li.appendChild(rm);
    els.webauthnDevices.appendChild(li);
  });
  els.enrollDeviceBtn.hidden = !w.enrollment_open;
}

async function removeDevice(d) {
  if (!confirm('Remove passkey "' + d.label + '"?')) return;
  try {
    await jsonApi('/api/webauthn/devices/' + encodeURIComponent(d.id), { method: 'DELETE' });
    toast('Removed ' + d.label);
    fetchWebauthnStatus();
  } catch (exc) {
    toast('Remove failed: ' + (exc.message || exc), 'error');
  }
}

async function enrollDevice() {
  if (!window.PublicKeyCredential) {
    toast('This browser has no passkey support.', 'error');
    return;
  }
  const label = prompt('Name this device', 'iPhone');
  if (!label) return;
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

export function wireWebauthn() {
  els.enrollDeviceBtn.addEventListener('click', enrollDevice);
}
