/* HTTP helpers + bearer-token plumbing + login overlay + toast.
 *
 * Every module reaches the server through api()/jsonApi() so the
 * Authorization: Bearer header is attached once, and 401s flip the login
 * overlay open from one place.
 */

import { els, TOKEN_KEY } from './state.js';

// --------------------------------------------------------------- tokens
export function tokenFromUrl() {
  const params = new URLSearchParams(window.location.search);
  const t = (params.get('token') || '').trim();
  if (!t) return null;
  params.delete('token');
  const newQuery = params.toString();
  const newUrl =
    window.location.pathname + (newQuery ? '?' + newQuery : '') + window.location.hash;
  window.history.replaceState({}, '', newUrl);
  return t;
}

export function readToken() { return localStorage.getItem(TOKEN_KEY) || ''; }
export function writeToken(t) { if (t) localStorage.setItem(TOKEN_KEY, t); }

// --------------------------------------------------------------- fetch
export async function api(path, opts) {
  opts = opts || {};
  const headers = new Headers(opts.headers || {});
  const token = readToken();
  if (token) headers.set('Authorization', 'Bearer ' + token);
  const res = await fetch(path, Object.assign({}, opts, { headers }));
  if (res.status === 401) {
    showLogin();
    throw new Error('auth required');
  }
  return res;
}

export async function jsonApi(path, opts) {
  const res = await api(path, opts);
  let body = null;
  try { body = await res.json(); } catch (_) { body = null; }
  if (!res.ok) {
    const detail = (body && body.detail) || ('HTTP ' + res.status);
    const err = new Error(detail);
    err.status = res.status;
    err.body = body;
    throw err;
  }
  return body;
}

// --------------------------------------------------------------- login
export function showLogin() {
  if (!els.loginOverlay) return;
  els.loginOverlay.hidden = false;
  // The login overlay is not a <dialog>, so the nav's body:has(dialog[open])
  // rule can't see it — the vendored nav hides on this class instead.
  document.body.classList.add('nav-hidden');
  els.loginPassword.value = '';
  els.loginPassword.focus();
}

export function hideLogin() {
  if (els.loginOverlay) els.loginOverlay.hidden = true;
  document.body.classList.remove('nav-hidden');
}

// Boot hook called from main.js — passed `onLoginSuccess` so this module stays
// independent of the boot sequence.
export function wireLoginForm(onLoginSuccess) {
  els.loginForm.addEventListener('submit', async function (ev) {
    ev.preventDefault();
    els.loginError.hidden = true;
    const password = els.loginPassword.value;
    try {
      const res = await fetch('/api/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ password }),
      });
      const body = await res.json().catch(function () { return null; });
      if (!res.ok || !body || !body.token) {
        const msg = (body && body.detail) || 'Login failed';
        els.loginError.textContent = msg;
        els.loginError.hidden = false;
        return;
      }
      writeToken(body.token);
      hideLogin();
      onLoginSuccess();
    } catch (exc) {
      els.loginError.textContent = String(exc.message || exc);
      els.loginError.hidden = false;
    }
  });
}

// --------------------------------------------------------------- toast
let toastTimer = null;
export function toast(msg, kind) {
  els.toast.textContent = msg;
  els.toast.className = 'toast ' + (kind || '');
  els.toast.hidden = false;
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(function () {
    els.toast.hidden = true;
  }, kind === 'error' ? 4500 : 2200);
}
