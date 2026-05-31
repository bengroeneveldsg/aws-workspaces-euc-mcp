# Copyright bengroeneveldsg. Licensed under the Apache License, Version 2.0 (the "License").
# You may not use this file except in compliance with the License.
# A copy of the License is located at http://www.apache.org/licenses/LICENSE-2.0
"""Inventory & discovery tools (read-only, IAM Tier 0).

These are *workflow* tools: each one fans out across several EUC services and synthesizes a single
result, rather than mirroring one API call. Collection is best-effort — a failure for one service
(missing permission, unsupported region) is captured in ``errors`` and does not abort the summary.
"""

from __future__ import annotations

from typing import Any

from .. import consts
from ..clients import ClientFactory
from ..models import EucInventorySummary, InventoryError, ServiceInventory
from ._common import count_by, paginate, try_call


def collect_inventory(factory: ClientFactory, region: str | None) -> EucInventorySummary:
    """Collect a cross-service EUC inventory for one region. Pure/testable core."""
    errors: list[InventoryError] = []
    services: list[ServiceInventory] = []

    workspaces = factory.client(consts.WORKSPACES_API, region=region)

    personal = try_call(
        errors,
        consts.PRODUCT_WORKSPACES_PERSONAL,
        "DescribeWorkspaces",
        lambda: paginate(workspaces.describe_workspaces, "Workspaces"),
    )
    if personal is not None:
        services.append(
            ServiceInventory(
                service=consts.PRODUCT_WORKSPACES_PERSONAL,
                resource_type="WorkSpace",
                count=len(personal),
                by_state=count_by(personal, "State"),
            )
        )

    pools = try_call(
        errors,
        consts.PRODUCT_WORKSPACES_POOLS,
        "DescribeWorkspacesPools",
        lambda: paginate(workspaces.describe_workspaces_pools, "WorkspacesPools"),
    )
    if pools is not None:
        services.append(
            ServiceInventory(
                service=consts.PRODUCT_WORKSPACES_POOLS,
                resource_type="WorkSpacesPool",
                count=len(pools),
                by_state=count_by(pools, "State"),
            )
        )

    appstream = factory.client(consts.APPSTREAM_API, region=region)
    fleets = try_call(
        errors,
        consts.PRODUCT_WORKSPACES_APPLICATIONS,
        "DescribeFleets",
        lambda: paginate(appstream.describe_fleets, "Fleets"),
    )
    if fleets is not None:
        services.append(
            ServiceInventory(
                service=consts.PRODUCT_WORKSPACES_APPLICATIONS,
                resource_type="Fleet",
                count=len(fleets),
                by_state=count_by(fleets, "State"),
            )
        )

    secure_browser = factory.client(consts.SECURE_BROWSER_API, region=region)
    portals = try_call(
        errors,
        consts.PRODUCT_SECURE_BROWSER,
        "ListPortals",
        lambda: paginate(
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
                by_state=count_by(portals, "portalStatus"),
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
