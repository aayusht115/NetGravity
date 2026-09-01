"""
NetGravity — Application-Layer Error Taxonomy
=============================================
Mirrors `netgravity.orchestrator.exceptions` in shape so the two layers
serialize identically to the client.

Every layer must fail explicitly (brief §24). Nothing here ever degrades into a
plausible business value; an application error is always surfaced as an error.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, Optional


class AppErrorCode(str, Enum):
    """Application-layer failure vocabulary."""

    VALIDATION_ERROR = "VALIDATION_ERROR"
    UNAUTHENTICATED = "UNAUTHENTICATED"
    FORBIDDEN = "FORBIDDEN"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    NO_NETWORK_BOUND = "NO_NETWORK_BOUND"
    INGESTION_ERROR = "INGESTION_ERROR"
    ENGINE_UNAVAILABLE = "ENGINE_UNAVAILABLE"


class ApplicationError(Exception):
    """
    Base application error.

    `http_status` is carried on the exception rather than decided at the call
    site so that one handler can serialize every application failure
    consistently.
    """

    code: AppErrorCode = AppErrorCode.VALIDATION_ERROR
    http_status: int = 400

    def __init__(
        self,
        message: str,
        *,
        context: Optional[Dict[str, Any]] = None,
        http_status: Optional[int] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.context: Dict[str, Any] = dict(context or {})
        if http_status is not None:
            self.http_status = http_status

    def to_payload(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "error": {
                "code": self.code.value,
                "message": self.message,
            }
        }
        if self.context:
            payload["error"]["context"] = self.context
        return payload


class ValidationError(ApplicationError):
    code = AppErrorCode.VALIDATION_ERROR
    http_status = 400


class UnauthenticatedError(ApplicationError):
    code = AppErrorCode.UNAUTHENTICATED
    http_status = 401


class ForbiddenError(ApplicationError):
    """
    Raised when an authenticated actor asks for a resource they do not own.

    Deliberately distinct from NotFound: the project-isolation boundary should
    be observable in logs and tests, not silently collapsed into a 404.
    """

    code = AppErrorCode.FORBIDDEN
    http_status = 403


class NotFoundError(ApplicationError):
    code = AppErrorCode.NOT_FOUND
    http_status = 404


class ConflictError(ApplicationError):
    code = AppErrorCode.CONFLICT
    http_status = 409


class NoNetworkBoundError(ApplicationError):
    """
    The project exists but has no network snapshot bound to it yet.

    This is the honest answer for a project whose data has not been ingested.
    It is NOT an internal failure, and it must never be answered with a
    fabricated or borrowed network — hence its own code, which the frontend
    renders as an empty state rather than an error toast.
    """

    code = AppErrorCode.NO_NETWORK_BOUND
    http_status = 409


class EngineUnavailableError(ApplicationError):
    code = AppErrorCode.ENGINE_UNAVAILABLE
    http_status = 503
