# Copyright bengroeneveldsg. Licensed under the Apache License, Version 2.0 (the "License").
# You may not use this file except in compliance with the License.
# A copy of the License is located at http://www.apache.org/licenses/LICENSE-2.0
"""Reporting & audit tools (read-only).

`generate_inventory_report` and `list_unused_resources` are Tier 0; `audit_security_posture` is
Tier 0 too. All synthesize across services and degrade gracefully when a signal is unavailable.
"""

from __future__ import annotations

from typing import Any

from .. import consts
from ..clients import ClientFactory
from ..models import (
    AuditReport,
    Finding,
    InventoryReport,
    InventoryReportSection,
    ResourceRecord,
    ServiceError,
    UnusedResource,
    UnusedResourcesReport,
)
from . import cost
from ._common import paginate, try_call


def generate_inventory_report_core(factory: ClientFactory, region: str | None) -> InventoryReport:
    errors: list[ServiceError] = []
    sections: list[InventoryReportSection] = []

    workspaces = factory.client(consts.WORKSPACES_API, region=region)
    appstream = factory.client(consts.APPSTREAM_API, region=region)
    secure_browser = factory.client(consts.SECURE_BROWSER_API, region=region)

    personal = try_call(
        errors,
        consts.PRODUCT_WORKSPACES_PERSONAL,
        "DescribeWorkspaces",
        lambda: paginate(workspaces.describe_workspaces, "Workspaces"),
        default=[],
    )
    sections.append(
        InventoryReportSection(
            service=consts.PRODUCT_WORKSPACES_PERSONAL,
            resource_type="WorkSpace",
            resources=[
                ResourceRecord(
                    id=w.get("WorkspaceId", ""),
                    state=w.get("State"),
                    attributes={
                        "directory_id": w.get("DirectoryId"),
                        "bundle_id": w.get("BundleId"),
                        "compute_type": w.get("WorkspaceProperties", {}).get("ComputeTypeName"),
                        "running_mode": w.get("WorkspaceProperties", {}).get("RunningMode"),
                    },
                )
                for w in (personal or [])
            ],
        )
    )

    pools = try_call(
        errors,
        consts.PRODUCT_WORKSPACES_POOLS,
        "DescribeWorkspacesPools",
        lambda: paginate(workspaces.describe_workspaces_pools, "WorkspacesPools"),
        default=[],
    )
    sections.append(
        InventoryReportSection(
            service=consts.PRODUCT_WORKSPACES_POOLS,
            resource_type="WorkSpacesPool",
            resources=[
                ResourceRecord(
                    id=p.get("PoolId", p.get("PoolName", "")),
                    name=p.get("PoolName"),
                    state=p.get("State"),
                    attributes={"capacity": p.get("Capacity")},
                )
                for p in (pools or [])
            ],
        )
    )

    fleets = try_call(
        errors,
        consts.PRODUCT_WORKSPACES_APPLICATIONS,
        "DescribeFleets",
        lambda: paginate(appstream.describe_fleets, "Fleets"),
        default=[],
    )
    sections.append(
        InventoryReportSection(
            service=consts.PRODUCT_WORKSPACES_APPLICATIONS,
            resource_type="Fleet",
            resources=[
                ResourceRecord(
                    id=f.get("Name", ""),
                    name=f.get("DisplayName"),
                    state=f.get("State"),
                    attributes={
                        "instance_type": f.get("InstanceType"),
                        "fleet_type": f.get("FleetType"),
                        "capacity": f.get("ComputeCapacityStatus"),
                    },
                )
                for f in (fleets or [])
            ],
        )
    )

    stacks = try_call(
        errors,
        consts.PRODUCT_WORKSPACES_APPLICATIONS,
        "DescribeStacks",
        lambda: paginate(appstream.describe_stacks, "Stacks"),
        default=[],
    )
    stack_records: list[ResourceRecord] = []
    for s in stacks or []:
        stack_name = s.get("Name", "")
        associated = try_call(
            errors,
            consts.PRODUCT_WORKSPACES_APPLICATIONS,
            "ListAssociatedFleets",
            lambda stack_name=stack_name: paginate(
                appstream.list_associated_fleets, "Names", StackName=stack_name
            ),
            default=[],
        )
        stack_records.append(
            ResourceRecord(
                id=stack_name,
                name=s.get("DisplayName"),
                attributes={
                    "description": s.get("Description"),
                    "associated_fleets": associated or [],
                },
            )
        )
    sections.append(
        InventoryReportSection(
            service=consts.PRODUCT_WORKSPACES_APPLICATIONS,
            resource_type="Stack",
            resources=stack_records,
        )
    )

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
        default=[],
    )
    sections.append(
        InventoryReportSection(
            service=consts.PRODUCT_SECURE_BROWSER,
            resource_type="Portal",
            resources=[
                ResourceRecord(
                    id=p.get("portalArn", p.get("portalId", "")),
                    name=p.get("displayName"),
                    state=p.get("portalStatus"),
                    attributes={"browser_type": p.get("browserType")},
                )
                for p in (portals or [])
            ],
        )
    )

    total = sum(len(s.resources) for s in sections)
    return InventoryReport(region=region, total_resources=total, sections=sections, errors=errors)


def audit_security_posture_core(factory: ClientFactory, region: str | None) -> AuditReport:
    errors: list[ServiceError] = []
    findings: list[Finding] = []
    resources_checked: dict[str, int] = {}

    workspaces = factory.client(consts.WORKSPACES_API, region=region)

    personal = try_call(
        errors,
        consts.PRODUCT_WORKSPACES_PERSONAL,
        "DescribeWorkspaces",
        lambda: paginate(workspaces.describe_workspaces, "Workspaces"),
        default=[],
    )
    resources_checked["workspaces"] = len(personal or [])
    for w in personal or []:
        wid = w.get("WorkspaceId", "")
        root_enc = w.get("RootVolumeEncryptionEnabled")
        user_enc = w.get("UserVolumeEncryptionEnabled")
        unencrypted = [
            name
            for name, enabled in (("root", root_enc), ("user", user_enc))
            if enabled is not True
        ]
        if unencrypted:
            findings.append(
                Finding(
                    severity="warning",
                    title=f"WorkSpace volumes not encrypted: {', '.join(unencrypted)}",
                    detail=f"WorkSpace {wid} has unencrypted {', '.join(unencrypted)} volume(s); "
                    "encryption can only be set at creation time.",
                    recommendation="Recreate the WorkSpace with root/user volume encryption.",
                    resource_id=wid,
                )
            )

    directories = try_call(
        errors,
        consts.PRODUCT_WORKSPACES_PERSONAL,
        "DescribeWorkspaceDirectories",
        lambda: paginate(workspaces.describe_workspace_directories, "Directories"),
        default=[],
    )
    resources_checked["directories"] = len(directories or [])
    for d in directories or []:
        did = d.get("DirectoryId", "")
        ip_groups = d.get("ipGroupIds") or []
        if not ip_groups:
            findings.append(
                Finding(
                    severity="warning",
                    title="Directory has no IP access control groups",
                    detail=f"Directory {did} has no IP access control groups, so WorkSpaces "
                    "connections are not restricted by source IP.",
                    recommendation="Attach an IP access control group to restrict trusted ranges.",
                    resource_id=did,
                )
            )

    if not findings and (
        resources_checked.get("workspaces") or resources_checked.get("directories")
    ):
        findings.append(
            Finding(
                severity="info",
                title="No posture issues found in the checks performed",
                detail="Checked WorkSpace volume encryption and directory IP access groups.",
            )
        )

    severity_counts: dict[str, int] = {}
    for f in findings:
        severity_counts[f.severity] = severity_counts.get(f.severity, 0) + 1

    return AuditReport(
        region=region,
        findings=findings,
        severity_counts=severity_counts,
        resources_checked=resources_checked,
        errors=errors,
    )


def list_unused_resources_core(
    factory: ClientFactory, region: str | None, lookback_days: int = 14
) -> UnusedResourcesReport:
    errors: list[ServiceError] = []
    items: list[UnusedResource] = []

    utilization = cost.analyze_workspace_utilization_core(factory, region, lookback_days)
    errors.extend(utilization.errors)
    for w in utilization.workspaces:
        if w.classification == "unused":
            items.append(
                UnusedResource(
                    service=consts.PRODUCT_WORKSPACES_PERSONAL,
                    resource_type="WorkSpace",
                    id=w.workspace_id,
                    reason=f"No user connections in the last {lookback_days} days.",
                )
            )

    appstream = factory.client(consts.APPSTREAM_API, region=region)
    fleets = try_call(
        errors,
        consts.PRODUCT_WORKSPACES_APPLICATIONS,
        "DescribeFleets",
        lambda: paginate(appstream.describe_fleets, "Fleets"),
        default=[],
    )
    for f in fleets or []:
        capacity = f.get("ComputeCapacityStatus", {})
        if f.get("State") == "STOPPED" or capacity.get("Desired") == 0:
            items.append(
                UnusedResource(
                    service=consts.PRODUCT_WORKSPACES_APPLICATIONS,
                    resource_type="Fleet",
                    id=f.get("Name", ""),
                    reason="Fleet is stopped or has zero desired capacity.",
                )
            )

    return UnusedResourcesReport(
        region=region,
        lookback_days=lookback_days,
        items=items,
        errors=errors,
        notes=[
            "WorkSpace usage is from the AWS/WorkSpaces UserConnected metric; review before any "
            "termination — recently provisioned desktops can look unused."
        ],
    )


def register(mcp: Any, factory: ClientFactory) -> None:
    """Register reporting & audit tools on the FastMCP app."""

    async def generate_inventory_report(region: str | None = None) -> dict[str, Any]:
        """Produce a detailed per-resource inventory across the EUC portfolio.

        Lists WorkSpaces Personal desktops, WorkSpaces Pools, WorkSpaces Applications (formerly
        AppStream 2.0) fleets, and WorkSpaces Secure Browser (formerly WorkSpaces Web) portals with
        key attributes per resource. Read-only.

        Args:
            region: AWS region. Defaults to the server's configured region.
        """
        report = generate_inventory_report_core(factory, region or factory.region)
        return report.model_dump()

    async def audit_security_posture(region: str | None = None) -> dict[str, Any]:
        """Audit EUC security posture against common best practices.

        Checks WorkSpaces Personal root/user volume encryption and whether directories have IP
        access control groups, returning severity-ranked findings. Read-only.

        Args:
            region: AWS region. Defaults to the server's configured region.
        """
        report = audit_security_posture_core(factory, region or factory.region)
        return report.model_dump()

    async def list_unused_resources(
        region: str | None = None, lookback_days: int = 14
    ) -> dict[str, Any]:
        """List candidate idle/unused EUC resources worth reclaiming.

        Surfaces unused WorkSpaces Personal desktops (no connections in the window) and stopped or
        zero-capacity WorkSpaces Applications fleets. Read-only.

        Args:
            region: AWS region. Defaults to the server's configured region.
            lookback_days: Window for WorkSpace usage (default 14).
        """
        report = list_unused_resources_core(factory, region or factory.region, lookback_days)
        return report.model_dump()

    mcp.add_tool(generate_inventory_report)
    mcp.add_tool(audit_security_posture)
    mcp.add_tool(list_unused_resources)
