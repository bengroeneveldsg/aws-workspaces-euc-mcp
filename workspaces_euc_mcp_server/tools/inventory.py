# Copyright bengroeneveldsg. Licensed under the Apache License, Version 2.0 (the "License").
# You may not use this file except in compliance with the License.
# A copy of the License is located at http://www.apache.org/licenses/LICENSE-2.0
"""Inventory & discovery tools (read-only, IAM Tier 0).

These are *workflow* tools: each one fans out across several EUC services and synthesizes a single
result, rather than mirroring one API call. Collection is best-effort — a failure for one service
(missing permission, unsupported region) is captured in ``errors`` and does not abort the summary.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from .. import consts
from ..clients import ClientFactory
from ..models import EucInventorySummary, InventoryError, ServiceInventory
from ._common import count_by, gather_concurrently, paginate, read_only, try_call


def collect_inventory(factory: ClientFactory, region: str | None) -> EucInventorySummary:
    """Collect a cross-service EUC inventory for one region. Pure/testable core.

    The per-service collectors run concurrently (see ``gather_concurrently``); results and errors
    are merged in a fixed service order so output is deterministic.
    """
    # Clients are created up front on this thread; only their (thread-safe) methods run in jobs.
    workspaces = factory.client(consts.WORKSPACES_API, region=region)
    appstream = factory.client(consts.APPSTREAM_API, region=region)
    secure_browser = factory.client(consts.SECURE_BROWSER_API, region=region)
    instances_client = factory.client(consts.WORKSPACES_INSTANCES_API, region=region)

    # (product, resource_type, operation, fetch, state_key)
    specs: list[tuple[str, str, str, Callable[[], list[dict[str, Any]]], str | None]] = [
        (
            consts.PRODUCT_WORKSPACES_PERSONAL,
            "WorkSpace",
            "DescribeWorkspaces",
            lambda: paginate(workspaces.describe_workspaces, "Workspaces"),
            "State",
        ),
        (
            consts.PRODUCT_WORKSPACES_POOLS,
            "WorkSpacesPool",
            "DescribeWorkspacesPools",
            lambda: paginate(workspaces.describe_workspaces_pools, "WorkspacesPools"),
            "State",
        ),
        (
            consts.PRODUCT_WORKSPACES_APPLICATIONS,
            "Fleet",
            "DescribeFleets",
            lambda: paginate(appstream.describe_fleets, "Fleets"),
            "State",
        ),
        (
            consts.PRODUCT_WORKSPACES_APPLICATIONS,
            "Stack",
            "DescribeStacks",
            lambda: paginate(appstream.describe_stacks, "Stacks"),
            None,
        ),
        (
            consts.PRODUCT_SECURE_BROWSER,
            "Portal",
            "ListPortals",
            lambda: paginate(
                secure_browser.list_portals,
                "portals",
                pagination_in="nextToken",
                pagination_out="nextToken",
            ),
            "portalStatus",
        ),
        (
            consts.PRODUCT_WORKSPACES_CORE_INSTANCES,
            "ManagedInstance",
            "ListWorkspaceInstances",
            lambda: paginate(instances_client.list_workspace_instances, "WorkspaceInstances"),
            "ProvisionState",
        ),
    ]

    def _collect(
        product: str,
        resource_type: str,
        operation: str,
        fetch: Callable[[], list[dict[str, Any]]],
        state_key: str | None,
    ) -> tuple[ServiceInventory | None, list[InventoryError]]:
        errors: list[InventoryError] = []
        items = try_call(errors, product, operation, fetch)
        if items is None:
            return None, errors
        return (
            ServiceInventory(
                service=product,
                resource_type=resource_type,
                count=len(items),
                by_state=count_by(items, state_key) if state_key else {},
            ),
            errors,
        )

    results = gather_concurrently(*[lambda spec=spec: _collect(*spec) for spec in specs])

    services = [inventory for inventory, _ in results if inventory is not None]
    errors = [error for _, job_errors in results for error in job_errors]
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

        Fans out across WorkSpaces Personal, WorkSpaces Pools, WorkSpaces Applications (formerly
        AppStream 2.0), and WorkSpaces Secure Browser (formerly WorkSpaces Web), returning
        per-service counts broken down by state, the grand total, and any per-service collection
        errors (e.g. missing permissions). Read-only.

        by_state is the lifecycle/power state (e.g. AVAILABLE = running, STOPPED) — use it for
        "what is running right now?" questions; use generate_inventory_report for the per-resource
        list with each desktop's State.

        Args:
            region: AWS region to inventory. Defaults to the server's configured region.
        """
        target_region = region or factory.region
        # to_thread keeps the (blocking, multi-call) collection off the MCP event loop.
        summary = await asyncio.to_thread(collect_inventory, factory, target_region)
        return summary.model_dump()

    mcp.add_tool(get_euc_inventory_summary, annotations=read_only("EUC inventory summary"))
