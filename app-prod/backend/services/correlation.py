"""
NetGravity — Request correlation
================================
Ties one browser request to the orchestrator executions it starts.

Why this exists
---------------
The loading screen is a picture of the orchestration runtime, and until now it
could only be drawn from two places: the boundaries of the client's own HTTP
calls, and the execution trace fetched *after* a call returned. Both are true,
and neither says anything while the call is in flight — which is precisely the
twenty to forty seconds a cold solve takes and the whole reason the screen is
on screen at all.

The orchestrator already knows. `ExecutionContext` carries `current_state`,
`capability_status`, `completed_steps`, `failed_steps` and `blocked_steps`, and
they are updated as the run proceeds. What was missing was a way for the client
to ask *which execution is mine* before its own request came back.

`ExecutionStateStore` already indexes contexts by `request_id` — it is the
idempotency key, and `find_by_request_id` is how a duplicated request resolves
to the original execution instead of running twice. The browser already sends
`X-Request-ID` on every call. This module joins the two: the id the browser
generated becomes the prefix of the `request_id` the orchestrator files the
execution under, so `GET /orchestrator/executions/live?correlation_id=…`
can find it while it is still running.

Why a prefix and not the id itself
----------------------------------
One HTTP request may start more than one execution, and `request_id` is an
idempotency key: two executions filed under the same one would make the second
resolve to the first and silently return the wrong answer. Every call site
therefore states its own purpose, and the id is ``<correlation>:<purpose>``.
Unique per execution, still traceable back to the browser request that caused
it, and the deduplication semantics are unchanged.

Nothing here is required for correctness. A caller that sends no header, or a
context with no Flask request at all, gets a fresh UUID and behaves exactly as
it did before.
"""

from __future__ import annotations

import re
import uuid

# The header the frontend's `ApiClient` has always sent. Read here rather than
# invented, so there is one id for a request rather than two.
CORRELATION_HEADER = "X-Request-ID"

# Generated ids look like `req_k3f8a91z2mlq9x1`. Anything else is ignored
# rather than trusted: this value is echoed back to clients by the live
# executions route and used as a dictionary key, so it is bounded and
# restricted to characters that cannot be mistaken for structure.
_SAFE_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,96}$")


def client_correlation_id() -> str:
    """
    The browser's own id for the request being served, or "".

    Empty — never a substitute — when there is no request context, no header,
    or a header that does not look like an id. The caller then falls back to a
    fresh UUID, which is what happened before this existed.
    """
    try:
        from flask import has_request_context, request
    except Exception:  # pragma: no cover — Flask is a hard dependency
        return ""
    if not has_request_context():
        return ""
    raw = (request.headers.get(CORRELATION_HEADER) or "").strip()
    if not raw or not _SAFE_ID.match(raw):
        return ""
    # A colon is legal in the pattern above (ids from other systems may carry
    # one) but it is our own separator, so it cannot appear in a prefix.
    return raw.split(":", 1)[0]


def orchestrator_request_id(purpose: str) -> str:
    """
    An idempotency key for one execution, traceable to the browser request.

    `purpose` distinguishes executions started by the same HTTP request and
    must be stable for a given call site — it is part of an idempotency key,
    so a value that changed per call would defeat deduplication.
    """
    correlation = client_correlation_id()
    if not correlation:
        return str(uuid.uuid4())
    return f"{correlation}:{purpose}"


def matches_correlation(request_id: str, correlation: str) -> bool:
    """Is this execution one that `correlation` started?"""
    if not request_id or not correlation:
        return False
    return request_id == correlation or request_id.startswith(f"{correlation}:")
