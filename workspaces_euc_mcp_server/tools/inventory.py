# Copyright bengroeneveldsg. Licensed under the Apache License, Version 2.0 (the "License").
# You may not use this file except in compliance with the License.
# A copy of the License is located at http://www.apache.org/licenses/LICENSE-2.0
"""Inventory & discovery tools (read-only, IAM Tier 0).

These are *workflow* tools: each one fans out across several EUC services and synthesizes a single
result, rather than mirroring one API call. Collection is best-effort — a failure for one service
(missing permission, unsupported region) is captured in ``errors`` and does not abort the summary.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError
from loguru import logger

from .. import consts
from ..clients import ClientFactory
from ..models import EucInventorySummary, InventoryError, ServiceInventory


def _paginate(
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


def _count_by(items: list[dict[str, Any]], state_key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        state = item.get(state_key, "UNKNOWN")
        counts[state] = counts.get(state, 0) + 1
    return counts


def _safe(
    errors: list[InventoryError],
    service: str,
    operation: str,
    fn: Callable[[], list[dict[str, Any]]],
) -> list[dict[str, Any]] | None:
    """Run a collection step, recording (not raising) AWS errors."""
    try:
        return fn()
    except (ClientError, BotoCoreError) as exc:
        logger.warning("Inventory step failed: {} {} -> {}", service, operation, exc)
        errors.append(InventoryError(service=service, operation=operation, message=str(exc)))
        return None


def collect_inventory(factory: ClientFactory, region: str | None) -> EucInventorySummary:
    """Collect a cross-service EUC inventory for one region. Pure/testable core."""
    errors: list[InventoryError] = []
    services: list[ServiceInventory] = []

    workspaces = factory.client(consts.WORKSPACES_API, region=region)

    personal = _safe(
        errors,
        consts.PRODUCT_WORKSPACES_PERSONAL,
        "DescribeWorkspaces",
        lambda: _paginate(workspaces.describe_workspaces, "Workspaces"),
    )
    if personal is not None:
        services.append(
            ServiceInventory(
                service=consts.PRODUCT_WORKSPACES_PERSONAL,
                resource_type="WorkSpace",
                count=len(personal),
                by_state=_count_by(personal, "State"),
            )
        )

    pools = _safe(
        errors,
        consts.PRODUCT_WORKSPACES_POOLS,
        "DescribeWorkspacesPools",
        lambda: _paginate(workspaces.describe_workspaces_pools, "WorkspacesPools"),
    )
    if pools is not None:
        services.append(
            ServiceInventory(
                service=consts.PRODUCT_WORKSPACES_POOLS,
                resource_type="WorkSpacesPool",
                count=len(pools),
                by_state=_count_by(pools, "State"),
            )
        )

    appstream = factory.client(consts.APPSTREAM_API, region=region)
    fleets = _safe(
        errors,
        consts.PRODUCT_WORKSPACES_APPLICATIONS,
        "DescribeFleets",
        lambda: _paginate(appstream.describe_fleets, "Fleets"),
    )
    if fleets is not None:
        services.append(
            ServiceInventory(
                service=consts.PRODUCT_WORKSPACES_APPLICATIONS,
                resource_type="Fleet",
                count=len(fleets),
                by_state=_count_by(fleets, "State"),
            )
        )

    secure_browser = factory.client(consts.SECURE_BROWSER_API, region=region)
    portals = _safe(
        errors,
        consts.PRODUCT_SECURE_BROWSER,
        "ListPortals",
        lambda: _paginate(
            secure_browser.list_portals,
            "portals",
            pagination_in="nextToken",
            pagination_out="nextToken",
        ),
    )
    if portals is not None:
        services.append(
            ServiceInventory(
                service=consts.PRODUCT_SECURE_BROWSER,
                resource_type="Portal",
                count=len(portals),
                by_state=_count_by(portals, "portalStatus"),
            )
        )

    total = sum(s.count for s in services)
    return EucInventorySummary(
        region=region,
        total_resources=total,
        services=services,
        errors=errors,
    )


def register(mcp: Any, factory: ClientFactory) -> None:
    """Register inventory tools on the FastMCP app."""

    async def get_euc_inventory_summary(region: str | None = None) -> dict[str, Any]:
        """Summarize all Amazon WorkSpaces EUC resources in an AWS region.

        Fans out across WorkSpaces Personal, WorkSpaces Pools, WorkSpaces Applications, and
        WorkSpaces Secure Browser, returning per-service counts broken down by state, the grand
        total, and any per-service collection errors (e.g. missing permissions). Read-only.

        Args:
            region: AWS region to inventory. Defaults to the server's configured region.
        """
        target_region = region or factory.region
        summary = collect_inventory(factory, target_region)
        return summary.model_dump()

    mcp.add_tool(get_euc_inventory_summary)
