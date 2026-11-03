/* HTTP helpers + bearer-token plumbing + sign-in + toast.
 *
 * Every module reaches the server through api()/jsonApi() so the
 * Authorization: Bearer header is attached once, and a 401 awaits the one
 * vendored sign-in sheet from one place.
 */

import { TOKEN_KEY } from './state.js';
import { requestSignIn } from './_vendored/sign-in/sign-in.js';

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
  const send = function () {
    const headers = new Headers(opts.headers || {});
    const token = readToken();
    if (token) headers.set('Authorization', 'Bearer ' + token);
    return fetch(path, Object.assign({}, opts, { headers }));
  };
  const res = await send();
  if (res.status !== 401) return res;
  // Every call that hits a 401 waits on the same open sheet, then retries once
  // with the new token; a second 401 means the fresh token was refused too.
  await signIn();
  const retry = await send();
  if (retry.status === 401) throw new Error('auth required');
  return retry;
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

// Every tab's "load my data" entry point repeated the same
// try/jsonApi/catch-and-return-undefined scaffold: a 401 already flips
// the sign-in sheet open in api(), so the catch here just needs to stay
// quiet and skip the render. `onOk` runs only on a successful fetch, and may
// itself be async — awaited so a caller that needs the whole thing settled
// (a second fetch, a following state write) can `await fetchQuiet(...)`.
export async function fetchQuiet(path, onOk) {
  let data;
  try {
    data = await jsonApi(path);
  } catch (exc) {
    return; // stay quiet — 401 already handled, anything else retries next poll
  }
  await onOk(data);
}

// --------------------------------------------------------------- sign-in
// The sheet keeps one promise open while it is showing, so concurrent 401s share it.
function signIn() {
  return requestSignIn({
    endpoint: '/api/login',
    hint: 'Enter the password to use Family Radar.',
    onToken: writeToken,
  });
}

// --------------------------------------------------------------- toast
// The fleet's one neutral frosted toast (vendored); only 'error' tints.
export { showToast as toast } from './_vendored/toast/toast.js';
