"""Shared installed-app OAuth bootstrap for the fleet's portable Google packages.

``gmail_readonly/``, ``calendar_readonly/``, and ``calendar_write/`` each mint
their own independent OAuth grant — separate credential/scope boundaries, no
shared state at runtime — but they run the identical installed-app consent
flow, explicit-path CLI shape, and token load/refresh/persist routine. This
package holds that shared shape so it exists once instead of three times.

Like its three siblings it has no imports from ``src``, ``app``, or
``scripts``, so it is itself portable: a consumer lifting one of the other
three packages into another repo copies this package alongside it. See
``docs/gmail-reuse.md`` for the copy list.
"""

from __future__ import annotations
