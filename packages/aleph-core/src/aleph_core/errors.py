"""Aleph error hierarchy.

These exceptions cross package boundaries. Service methods raise them; the API
layer converts them into RFC 7807 problem details (see apps/api).
"""

from __future__ import annotations


class AlephError(Exception):
    """Base for all Aleph-domain errors."""

    code: str = "aleph_error"
    http_status: int = 500

    def __init__(self, message: str, *, detail: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail or {}


class NotFound(AlephError):
    code = "not_found"
    http_status = 404


class PermissionDenied(AlephError):
    """Raised when authorization fails. The API maps this to 404 for resources
    where existence itself should not leak across project scopes; routes that
    are intentionally 403-on-known-resource use a distinct response (rare)."""

    code = "permission_denied"
    http_status = 403


class Conflict(AlephError):
    """The request is well-formed and permitted, but the target's state forbids it.

    Distinct from `NotFound` (which hides existence) and `PermissionDenied`
    (which is about the caller): here the caller may legitimately reach the
    resource and the resource simply is not in a state that accepts the
    operation — writing to an archived or deleted project, say. Saying so is the
    point, because the remedy is to change that state, and a 404 would hide the
    very thing the user needs to find in order to fix it.
    """

    code = "conflict"
    http_status = 409


class ValidationFailed(AlephError):
    code = "validation_failed"
    http_status = 422


class GatewayUnavailable(AlephError):
    """LiteLLM gateway is unreachable or returned a non-recoverable error."""

    code = "gateway_unavailable"
    http_status = 503
