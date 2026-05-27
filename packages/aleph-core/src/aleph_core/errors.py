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


class ValidationFailed(AlephError):
    code = "validation_failed"
    http_status = 422


class BudgetExceeded(AlephError):
    code = "budget_exceeded"
    http_status = 429


class GatewayUnavailable(AlephError):
    """LiteLLM gateway is unreachable or returned a non-recoverable error."""

    code = "gateway_unavailable"
    http_status = 503
