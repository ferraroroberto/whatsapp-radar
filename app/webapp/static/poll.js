/* One poll helper for every timer-driven read: single-flight and visibility-aware.
 *
 * A tick that finds the previous read still in flight is dropped, so a slow
 * server never stacks requests. A hidden page arms no timer at all; coming back
 * to the foreground makes one catch-up read, then the timer resumes. `enabled`
 * is asked on every tick and on the catch-up, so a poll follows state (the open
 * tab, a run in flight) without being torn down and rebuilt.
 *
 * `env` is the page's clock and visibility, injectable so the schedule can be
 * driven by a fake clock in a test.
 */

const pageEnv = {
  hidden: function () { return document.hidden; },
  onVisible: function (fn) { document.addEventListener('visibilitychange', fn); },
  setTimer: function (fn, ms) { return setInterval(fn, ms); },
  clearTimer: function (id) { clearInterval(id); },
};

export function startPoll(run, intervalMs, { enabled = function () { return true; }, env = pageEnv } = {}) {
  let inFlight = false;
  let timer = null;

  async function fire() {
    if (inFlight || !enabled()) return;
    inFlight = true;
    try {
      await run();
    } catch (_) {
      // A failed read is the caller's to report; the next tick tries again.
    } finally {
      inFlight = false;
    }
  }

  function arm() {
    if (timer === null) timer = env.setTimer(fire, intervalMs);
  }

  function disarm() {
    if (timer !== null) env.clearTimer(timer);
    timer = null;
  }

  env.onVisible(function () {
    if (env.hidden()) {
      disarm();
      return;
    }
    // Back in the foreground: one catch-up read, then the timer resumes. A
    // visibilitychange that fires while already visible must not double it.
    if (timer === null) {
      fire();
      arm();
    }
  });

  if (!env.hidden()) arm();
}
