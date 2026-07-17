/* Manual summary read-aloud (#94): stream hub PCM16 through Web Audio.
 *
 * The AudioContext is created, resumed, and fed one silent sample synchronously
 * inside the button gesture. That activation window is essential on iOS Safari:
 * the real audio arrives only after health + synthesis network waits, too late
 * to unlock a context that did not produce output during the original tap.
 */

import { api, jsonApi } from './api.js';

const playback = {
  ctx: null,
  reader: null,
  abort: null,
  endTimer: null,
  playHead: 0,
  onState: null,
};

function setSpeaking(on) {
  if (playback.onState) {
    try { playback.onState(on); } catch (_) { /* UI callback is best-effort. */ }
  }
}

function teardown() {
  if (playback.endTimer) {
    clearTimeout(playback.endTimer);
    playback.endTimer = null;
  }
  if (playback.reader) {
    try { playback.reader.cancel(); } catch (_) { /* best effort */ }
    playback.reader = null;
  }
  if (playback.abort) {
    try { playback.abort.abort(); } catch (_) { /* best effort */ }
    playback.abort = null;
  }
  if (playback.ctx) {
    const ctx = playback.ctx;
    playback.ctx = null;
    try { ctx.close(); } catch (_) { /* best effort */ }
  }
}

export function cancelSummarySpeech() {
  const notify = playback.onState;
  teardown();
  playback.onState = null;
  if (notify) {
    try { notify(false); } catch (_) { /* best effort */ }
  }
}

function finishNaturally(owner) {
  if (playback.abort !== owner) return;
  const notify = playback.onState;
  teardown();
  playback.onState = null;
  if (notify) {
    try { notify(false); } catch (_) { /* best effort */ }
  }
}

// Synchronous gesture-tick prologue. The silent sample is intentional: on iOS,
// create+resume alone does not bless a context whose first output arrives later.
function prepare(onState) {
  cancelSummarySpeech();
  const AudioCtx = window.AudioContext || window.webkitAudioContext;
  if (!AudioCtx) throw new Error('Web Audio API unavailable');
  const ctx = new AudioCtx();
  playback.ctx = ctx;
  playback.onState = onState;
  try { ctx.resume(); } catch (_) { /* best effort */ }
  try {
    const silent = ctx.createBuffer(1, 1, ctx.sampleRate || 22050);
    const source = ctx.createBufferSource();
    source.buffer = silent;
    source.connect(ctx.destination);
    source.start(0);
  } catch (_) { /* best effort */ }
  const abort = new AbortController();
  playback.abort = abort;
  setSpeaking(true);
  return { ctx: ctx, abort: abort };
}

async function pumpPcm(ctx, response, abort) {
  try { ctx.resume(); } catch (_) { /* recover a context suspended during awaits */ }
  const sampleRate = parseInt(response.headers.get('X-Sample-Rate') || '24000', 10) || 24000;
  playback.playHead = ctx.currentTime + 0.15;
  let leftover = new Uint8Array(0);
  let finalNode = null;
  const reader = response.body.getReader();
  playback.reader = reader;

  for (;;) {
    const chunk = await reader.read();
    if (chunk.done) break;
    if (abort.signal.aborted) return;
    const value = chunk.value;
    if (!value || value.length === 0) continue;
    const merged = new Uint8Array(leftover.length + value.length);
    merged.set(leftover, 0);
    merged.set(value, leftover.length);
    const usable = merged.length - (merged.length % 2);
    leftover = merged.slice(usable);
    if (!usable) continue;

    const samples = new Int16Array(merged.buffer.slice(0, usable));
    const floats = new Float32Array(samples.length);
    for (let i = 0; i < samples.length; i++) floats[i] = samples[i] / 32768;
    const buffer = ctx.createBuffer(1, floats.length, sampleRate);
    buffer.copyToChannel(floats, 0);
    const node = ctx.createBufferSource();
    node.buffer = buffer;
    node.connect(ctx.destination);
    if (playback.playHead < ctx.currentTime + 0.02) {
      playback.playHead = ctx.currentTime + 0.02;
    }
    node.start(playback.playHead);
    playback.playHead += buffer.duration;
    finalNode = node;
  }

  if (!finalNode) {
    finishNaturally(abort);
    return;
  }
  finalNode.onended = function () { finishNaturally(abort); };
  const remainingMs = Math.max(0, (playback.playHead - ctx.currentTime) * 1000) + 1500;
  playback.endTimer = setTimeout(function () { finishNaturally(abort); }, remainingMs);
}

// messageId identifies the already-summarized message to read aloud; the
// server resolves language + voice from that message's own context (#157) —
// the client sends no text or voice choice of its own.
export async function speakSummary(messageId, onState) {
  if (messageId == null) return false;
  const handle = prepare(onState); // must run before this function's first await
  try {
    const health = await jsonApi('/api/tts/health');
    if (!health || !health.available) throw new Error('Text-to-speech is unavailable.');
    const response = await api('/api/tts/speak', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message_id: messageId }),
      signal: handle.abort.signal,
    });
    if (!response.ok) throw new Error('Text-to-speech request failed.');
    if (!response.body || !response.body.getReader) {
      throw new Error('Streaming audio is unavailable in this browser.');
    }
    await pumpPcm(handle.ctx, response, handle.abort);
    return true;
  } catch (exc) {
    if (handle.abort.signal.aborted) return true;
    if (playback.abort === handle.abort) cancelSummarySpeech();
    throw exc;
  }
}
