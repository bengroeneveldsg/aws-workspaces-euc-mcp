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
        errors.append(ServiceError(service=service, operation=operation, message=str(exc)))
        return default


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
