"""The browser poll helper's schedule, driven by a fake clock under Node.

``static/poll.js`` is single-flight and visibility-aware: a hidden page makes
no requests apart from one catch-up read on return, and a slow read is never
stacked on. The helper takes its clock and visibility as an ``env`` argument, so
the schedule is proven deterministically, with no real timers and no browser.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from typing import Any

import pytest

from src.paths import PROJECT_ROOT
from src.subprocess_flags import NO_WINDOW

POLL_JS = PROJECT_ROOT / "app" / "webapp" / "static" / "poll.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="needs Node")

# A manual clock: timers fire only when advance() crosses them, in order.
_HARNESS = """
import { startPoll } from __MODULE__;

function makeEnv(hidden) {
  const env = { isHidden: hidden, now: 0, timers: new Map(), nextId: 1, listeners: [] };
  env.hidden = () => env.isHidden;
  env.onVisible = (fn) => env.listeners.push(fn);
  env.setTimer = (fn, ms) => {
    const id = env.nextId++;
    env.timers.set(id, { fn, ms, due: env.now + ms });
    return id;
  };
  env.clearTimer = (id) => env.timers.delete(id);
  env.advance = (ms) => {
    const end = env.now + ms;
    for (;;) {
      const ready = [...env.timers.values()].filter((t) => t.due <= end);
      const due = ready.sort((a, b) => a.due - b.due)[0];
      if (!due) break;
      env.now = due.due;
      due.due += due.ms;
      due.fn();
    }
    env.now = end;
  };
  env.setHidden = (value) => { env.isHidden = value; env.listeners.forEach((fn) => fn()); };
  return env;
}
const settle = () => new Promise((resolve) => setImmediate(resolve));
const out = {};
__BODY__
console.log(JSON.stringify(out));
"""


def _run_node(body: str) -> dict[str, Any]:
    script = _HARNESS.replace("__MODULE__", json.dumps(POLL_JS.as_uri())).replace("__BODY__", body)
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True, text=True, encoding="utf-8", timeout=60, check=False,
        creationflags=NO_WINDOW,
    )
    assert proc.returncode == 0, proc.stderr
    result: dict[str, Any] = json.loads(proc.stdout.strip().splitlines()[-1])
    return result


def test_a_hidden_page_reads_nothing_but_one_catch_up_on_return() -> None:
    out = _run_node("""
      const env = makeEnv(true);
      let calls = 0;
      startPoll(async () => { calls += 1; }, 1500, { env });
      env.advance(10 * 60 * 1000);
      await settle();
      out.hiddenAtStart = calls;
      env.setHidden(false);
      await settle();
      out.afterReturn = calls;
      env.advance(1500);
      await settle();
      out.nextTick = calls;
      env.setHidden(true);
      env.advance(10 * 60 * 1000);
      await settle();
      out.hiddenAgain = calls;
    """)
    assert out == {"hiddenAtStart": 0, "afterReturn": 1, "nextTick": 2, "hiddenAgain": 2}


def test_a_visible_page_polls_on_the_interval_and_a_redundant_event_adds_nothing() -> None:
    out = _run_node("""
      const env = makeEnv(false);
      let calls = 0;
      startPoll(async () => { calls += 1; }, 1500, { env });
      for (let i = 0; i < 4; i += 1) { env.advance(1500); await settle(); }
      out.ticks = calls;
      env.setHidden(false);
      await settle();
      out.afterRedundantEvent = calls;
    """)
    assert out == {"ticks": 4, "afterRedundantEvent": 4}


def test_a_slow_read_is_never_stacked_on() -> None:
    out = _run_node("""
      const env = makeEnv(false);
      let calls = 0;
      let release;
      const slow = () => { calls += 1; return new Promise((resolve) => { release = resolve; }); };
      startPoll(slow, 1500, { env });
      env.advance(1500 * 5);
      await settle();
      out.whileInFlight = calls;
      release();
      await settle();
      env.advance(1500);
      await settle();
      out.afterSettled = calls;
    """)
    assert out == {"whileInFlight": 1, "afterSettled": 2}


def test_a_failed_read_does_not_stop_the_poll() -> None:
    out = _run_node("""
      const env = makeEnv(false);
      let calls = 0;
      startPoll(async () => { calls += 1; throw new Error('down'); }, 1500, { env });
      env.advance(1500);
      await settle();
      env.advance(1500);
      await settle();
      out.calls = calls;
    """)
    assert out == {"calls": 2}


def test_enabled_gates_every_tick_and_the_catch_up() -> None:
    out = _run_node("""
      const env = makeEnv(false);
      let calls = 0;
      let on = false;
      startPoll(async () => { calls += 1; }, 1500, { env, enabled: () => on });
      env.advance(1500 * 3);
      await settle();
      out.whileOff = calls;
      env.setHidden(true);
      env.setHidden(false);
      await settle();
      out.catchUpWhileOff = calls;
      on = true;
      env.advance(1500);
      await settle();
      out.whileOn = calls;
    """)
    assert out == {"whileOff": 0, "catchUpWhileOff": 0, "whileOn": 1}
