"""Bounded-timeout HTTP transport shared by the portable Google packages.

``googleapiclient.discovery.build(..., credentials=...)`` leaves the service on
httplib2's default transport, and httplib2's default is *no timeout at all*: a
dropped connection blocks a scheduled sync forever instead of failing after a
bounded wait. Gmail found and fixed that in #180; the two Calendar clients were
written afterwards and never inherited it (#298). The bound lives here so all
three builders share one definition instead of three drifting copies.
"""

from __future__ import annotations

from typing import Any

# Every request made through a client built by these packages gets this bound.
DEFAULT_REQUEST_TIMEOUT_S = 60


def bounded_authorized_http(credentials: Any, request_timeout_s: int) -> Any:
    """Authorized httplib2 transport with an explicit socket timeout.

    Pass the result as ``build(..., http=...)`` instead of ``credentials=``:
    the discovery client then uses this transport for every call, so a stalled
    connection fails after ``request_timeout_s`` rather than hanging the caller.
    """
    import httplib2  # type: ignore[import-untyped]
    from google_auth_httplib2 import AuthorizedHttp  # type: ignore[import-untyped]

    return AuthorizedHttp(credentials, http=httplib2.Http(timeout=request_timeout_s))
