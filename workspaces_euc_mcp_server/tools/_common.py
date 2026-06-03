# Copyright bengroeneveldsg. Licensed under the Apache License, Version 2.0 (the "License").
# You may not use this file except in compliance with the License.
# A copy of the License is located at http://www.apache.org/licenses/LICENSE-2.0
"""Shared best-effort AWS-call helpers used by the tool modules."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError
from loguru import logger
from mcp.types import ToolAnnotations

from ..models import ServiceError
from ..sso import SsoAutoLogin, looks_like_sso_token_error

# Optional process-wide SSO auto-login handler, installed by the server when --sso-auto-login is on.
_SSO_HANDLER: SsoAutoLogin | None = None


def register_sso_handler(handler: SsoAutoLogin | None) -> None:
    """Install the process-wide SSO auto-login handler (called once at server start)."""
    global _SSO_HANDLER
    _SSO_HANDLER = handler


def read_only(title: str) -> ToolAnnotations:
    """Annotations for a read-only tool (closed-domain: the configured AWS account)."""
    return ToolAnnotations(
        title=title,
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )


def writes(title: str, *, idempotent: bool = False, destructive: bool = False) -> ToolAnnotations:
    """Annotations for a mutating tool (lifecycle or destructive)."""
    return ToolAnnotations(
        title=title,
        readOnlyHint=False,
        destructiveHint=destructive,
        idempotentHint=idempotent,
        openWorldHint=False,
    )


def try_call(
    errors: list[ServiceError],
    service: str,
    operation: str,
    fn: Callable[[], Any],
    default: Any = None,
) -> Any:
    """Run an AWS call, recording (not raising) errors so collection can continue."""
    try:
        return fn()
    except (ClientError, BotoCoreError) as exc:
        logger.warning("AWS call failed: {} {} -> {}", service, operation, exc)
        message = str(exc)
        if looks_like_sso_token_error(exc):
            message += _sso_hint()
        errors.append(ServiceError(service=service, operation=operation, message=message))
        return default


def _sso_hint() -> str:
    """Append an actionable hint to SSO-token errors, auto-launching sign-in if enabled."""
    handler = _SSO_HANDLER
    if handler is not None and handler.enabled:
        status = handler.maybe_login()
        if status:
            return f" [SSO session expired — {status}]"
        return (
            " [SSO session expired — a browser sign-in is already in progress; approve it, "
            "then retry]"
        )
    return (
        " [SSO session expired — run `aws sso login --profile <your-profile>` to re-authenticate "
        "(automatic sign-in is disabled via --no-sso-auto-login). "
        "Note: signing into the AWS Console does NOT refresh the CLI/SSO token.]"
    )


def paginate(
    operation: Callable[..., dict[str, Any]],
    list_key: str,
    pagination_in: str = "NextToken",
    pagination_out: str = "NextToken",
    **kwargs: Any,
) -> list[dict[str, Any]]:
    """Drain a paginated AWS list/describe operation into a flat list.

    ``pagination_in`` / ``pagination_out`` are the request and response field names AWS uses for
    the continuation marker (e.g. ``NextToken``, or ``nextToken`` for camelCase services).
    """
    items: list[dict[str, Any]] = []
    marker: str | None = None
    while True:
        params = dict(kwargs)
        if marker:
            params[pagination_in] = marker
        response = operation(**params)
        items.extend(response.get(list_key, []))
        marker = response.get(pagination_out)
        if not marker:
            return items


def count_by(items: list[dict[str, Any]], state_key: str) -> dict[str, int]:
    """Count items grouped by a state-like field (missing values count as UNKNOWN)."""
    counts: dict[str, int] = {}
    for item in items:
        state = item.get(state_key, "UNKNOWN")
        counts[state] = counts.get(state, 0) + 1
    return counts
