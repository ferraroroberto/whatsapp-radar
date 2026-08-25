"""The traffic-check per-leg status vocabulary, in one importable place (#283).

A commute leg in a ``traffic-check`` payload's ``checked`` list either got a
Routes answer or it did not, and the two statuses that mean *it did not* —
:data:`STATUS_ANCHOR_IN_THE_PAST` and :data:`STATUS_ERROR` — are the whole
reason this module exists. Every surface that counts legs has to know them, or
it reports a coverage number that silently counts non-coverage: "3 routes
checked" for a run in which nothing was priced is real, plausible and wrong, and
that is how you end up trusting a check that has not been running (#283).

**Why a module of its own rather than a constant in**
:mod:`src.family.traffic_check`. Three of the four Python readers live in
``app/webapp/``, and importing ``traffic_check`` there would drag the Google
client libraries into webapp startup through its
:mod:`src.family.calendar_source` import. :mod:`app.webapp.routers.dashboard`
worked around that by spelling the statuses as literals with a comment
explaining why; #283 adds three more readers, and a vocabulary copied five times
is a vocabulary that will eventually disagree with itself.

So this module imports **nothing but the standard library**, and
``src/family/__init__.py`` is (and must stay) import-free, which is what makes
``from src.family.leg_status import …`` cost the webapp nothing. That is not a
convention to be taken on trust: ``tests/test_leg_status.py`` imports this
module in a clean subprocess and asserts no ``google*`` package landed in
``sys.modules``.

The JavaScript surfaces cannot import Python, so they carry the one unavoidable
second spelling — also in exactly one place, ``legPricing()`` in
``app/webapp/static/format.js``, which the Execution funnel and the Audit
drill-down both call.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

#: The drive is over — never priced, and no Routes call was spent on it, because
#: the API rejects a past ``departureTime`` outright (#270). Mirrors
#: ``src.family.traffic_check.STATUS_ANCHOR_IN_THE_PAST``, which is now defined
#: from this one.
STATUS_ANCHOR_IN_THE_PAST = "anchor_in_the_past"

#: The Routes call was made and failed. A leg that established nothing about the
#: road, for a different reason than the one above — the distinction is kept
#: because a transport fault and a schedule fact are genuinely different things
#: to report, even though both count as "not priced".
STATUS_ERROR = "error"

#: Every status that establishes *nothing* about the road. Neither may be folded
#: into a count of routes checked, nor into "no significant delay": a check that
#: did not happen must never read as an all-clear, at any granularity.
UNPRICED_LEG_STATUSES = frozenset({STATUS_ANCHOR_IN_THE_PAST, STATUS_ERROR})


def count_legs(legs: Iterable[Any] | None) -> tuple[int, int]:
    """``(priced, unpriced)`` over a traffic-check payload's ``checked`` list.

    Tolerant of whatever the persisted payload actually holds, because these
    counts are rendered from run rows written by older versions: a non-mapping
    entry, or one with no ``status`` at all, counts as **priced**. That is the
    conservative direction for a legacy row — a payload from before #270 has no
    ``status`` field and every one of its legs genuinely was priced, so calling
    those unpriced would invent a coverage gap that never existed.
    """
    priced = unpriced = 0
    for leg in legs or ():
        if isinstance(leg, dict) and leg.get("status") in UNPRICED_LEG_STATUSES:
            unpriced += 1
        else:
            priced += 1
    return priced, unpriced
