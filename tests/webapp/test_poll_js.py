"""The browser poll schedule the PWA runs on, driven by a fake clock under Node.

``static/_vendored/poll/poll.js`` arms the next read only after the last one
settled, arms nothing while the page is hidden or its ``activeWhile`` condition
is false, and shares one in-flight read per shape (``singleFlight``). The page's
``window`` timers and ``document`` visibility are replaced with a manual clock,
so each schedule is proven with no real timers and no browser.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from typing import Any

import pytest

from src.paths import PROJECT_ROOT
from src.subprocess_flags import NO_WINDOW

POLL_JS = PROJECT_ROOT / "app" / "webapp" / "static" / "_vendored" / "poll" / "poll.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="needs Node")

# A manual one-shot clock: a timer fires only when advance() crosses it.
_HARNESS = """
const clock = { now: 0, timers: new Map(), nextId: 1 };
globalThis.window = {
  setTimeout: (fn, ms) => {
    const id = clock.nextId++;
    clock.timers.set(id, { fn, due: clock.now + ms });
    return id;
  },
  clearTimeout: (id) => { clock.timers.delete(id); },
};
const listeners = [];
globalThis.document = {
  hidden: false,
  addEventListener: (_name, fn) => listeners.push(fn),
  removeEventListener: (_name, fn) => {
    const i = listeners.indexOf(fn);
    if (i >= 0) listeners.splice(i, 1);
  },
};
const settle = () => new Promise((resolve) => setImmediate(resolve));
async function advance(ms) {
  const end = clock.now + ms;
  for (;;) {
    const due = [...clock.timers.entries()]
      .filter(([, t]) => t.due <= end)
      .sort((a, b) => a[1].due - b[1].due)[0];
    if (!due) break;
    clock.timers.delete(due[0]);
    clock.now = due[1].due;
    due[1].fn();
    await settle();
  }
  clock.now = end;
}
async function setHidden(value) {
  document.hidden = value;
  listeners.slice().forEach((fn) => fn());
  await settle();
}
const { startPoll, singleFlight } = await import(__MODULE__);
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
      document.hidden = true;
      let calls = 0;
      startPoll(async () => { calls += 1; }, 15000);
      await advance(10 * 60 * 1000);
      out.hiddenAtStart = calls;
      await setHidden(false);
      out.afterReturn = calls;
      await advance(15000);
      out.nextTick = calls;
      await setHidden(true);
      await advance(10 * 60 * 1000);
      out.hiddenAgain = calls;
    """)
    assert out == {"hiddenAtStart": 0, "afterReturn": 1, "nextTick": 2, "hiddenAgain": 2}


def test_a_visible_page_polls_on_the_interval() -> None:
    out = _run_node("""
      let calls = 0;
      startPoll(async () => { calls += 1; }, 15000);
      await advance(4 * 15000);
      out.ticks = calls;
    """)
    assert out == {"ticks": 4}


def test_a_slow_read_stretches_the_gap_instead_of_stacking() -> None:
    out = _run_node("""
      let calls = 0;
      let release;
      const slow = () => { calls += 1; return new Promise((resolve) => { release = resolve; }); };
      startPoll(slow, 1500);
      await advance(1500 * 5);
      out.whileInFlight = calls;
      release();
      await settle();
      await advance(1499);
      out.justBeforeTheGap = calls;
      await advance(1);
      out.afterTheGap = calls;
    """)
    assert out == {"whileInFlight": 1, "justBeforeTheGap": 1, "afterTheGap": 2}


def test_a_failed_read_does_not_stop_the_poll() -> None:
    out = _run_node("""
      let calls = 0;
      startPoll(async () => { calls += 1; throw new Error('down'); }, 1500);
      await advance(1500 * 2);
      out.calls = calls;
    """)
    assert out == {"calls": 2}


def test_a_busy_only_poll_arms_nothing_until_kicked_and_stops_by_itself() -> None:
    # The run poll's shape: fast while a run is in flight, nothing at all otherwise.
    out = _run_node("""
      let calls = 0;
      let active = false;
      const poll = startPoll(async () => { calls += 1; }, 1500, { activeWhile: () => active });
      await advance(10 * 60 * 1000);
      out.whileIdle = calls;
      out.timersWhileIdle = clock.timers.size;
      active = true;
      poll.kick();
      await settle();
      out.onKick = calls;
      await advance(1500 * 2);
      out.whileActive = calls;
      active = false;
      await advance(1500);
      out.lastTick = calls;
      await advance(10 * 60 * 1000);
      out.afterTheRun = calls;
      out.timersAfterTheRun = clock.timers.size;
    """)
    assert out == {
        "whileIdle": 0, "timersWhileIdle": 0, "onKick": 1, "whileActive": 3,
        "lastTick": 3, "afterTheRun": 3, "timersAfterTheRun": 0,
    }


def test_single_flight_shares_a_read_and_fresh_waits_for_the_next() -> None:
    # A kick beside a tab tap's read shares it; a caller that just wrote never
    # takes a read that predates its write.
    out = _run_node("""
      let calls = 0;
      const releases = [];
      const slow = () => { calls += 1; return new Promise((resolve) => releases.push(resolve)); };
      const read = singleFlight(slow);
      const a = read();
      const b = read();
      out.sameRead = a === b;
      out.afterTwoCallers = calls;
      const c = read.fresh();
      await settle();
      out.freshWaits = calls;
      releases[0]();
      await a;
      await settle();
      out.freshStarted = calls;
      releases[1]();
      await c;
      out.total = calls;
    """)
    assert out == {
        "sameRead": True, "afterTwoCallers": 1, "freshWaits": 1, "freshStarted": 2, "total": 2,
    }
