# Copyright bengroeneveldsg. Licensed under the Apache License, Version 2.0 (the "License").
# You may not use this file except in compliance with the License.
# A copy of the License is located at http://www.apache.org/licenses/LICENSE-2.0
"""Reporting & audit tools (read-only).

`generate_inventory_report` and `list_unused_resources` are Tier 0; `audit_security_posture` is
Tier 0 too. All synthesize across services and degrade gracefully when a signal is unavailable.
Per-service collection runs concurrently (``gather_concurrently``); results merge in fixed order.
"""

from __future__ import annotations

import asyncio
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
from ._common import LookbackDays, gather_concurrently, paginate, read_only, try_call


def _managed_instance_record(instance: dict, ec2_by_id: dict[str, dict]) -> ResourceRecord:
    ec2_id = (instance.get("EC2ManagedInstance") or {}).get("InstanceId")
    ec2 = ec2_by_id.get(ec2_id or "", {})
    return ResourceRecord(
        id=instance.get("WorkspaceInstanceId", ""),
        state=instance.get("ProvisionState"),
        attributes={
            "ec2_instance_id": ec2_id,
            "ec2_instance_type": ec2.get("InstanceType"),
            "ec2_state": (ec2.get("State") or {}).get("Name"),
            "ec2_launch_time": str(ec2.get("LaunchTime")) if ec2.get("LaunchTime") else None,
            "ec2_private_ip": ec2.get("PrivateIpAddress"),
            "ec2_platform": ec2.get("PlatformDetails"),
        },
    )


def generate_inventory_report_core(factory: ClientFactory, region: str | None) -> InventoryReport:
    """Detailed per-resource inventory; the six per-service sections collect concurrently."""
    # Clients are created up front on this thread; only their (thread-safe) methods run in jobs.
    workspaces = factory.client(consts.WORKSPACES_API, region=region)
    appstream = factory.client(consts.APPSTREAM_API, region=region)
    secure_browser = factory.client(consts.SECURE_BROWSER_API, region=region)
    instances_client = factory.client(consts.WORKSPACES_INSTANCES_API, region=region)

    def _personal_section() -> tuple[InventoryReportSection, list[ServiceError]]:
        errors: list[ServiceError] = []
        personal = try_call(
            errors,
            consts.PRODUCT_WORKSPACES_PERSONAL,
            "DescribeWorkspaces",
            lambda: paginate(workspaces.describe_workspaces, "Workspaces"),
            default=[],
        )
        section = InventoryReportSection(
            service=consts.PRODUCT_WORKSPACES_PERSONAL,
            resource_type="WorkSpace",
            resources=[
                ResourceRecord(
                    id=w.get("WorkspaceId", ""),
                    name=w.get("UserName"),
                    state=w.get("State"),
                    attributes={
                        "user_name": w.get("UserName"),
                        "computer_name": w.get("ComputerName"),
                        "ip_address": w.get("IpAddress"),
                        "directory_id": w.get("DirectoryId"),
                        "bundle_id": w.get("BundleId"),
                        "compute_type": w.get("WorkspaceProperties", {}).get("ComputeTypeName"),
                        "running_mode": w.get("WorkspaceProperties", {}).get("RunningMode"),
                        "operating_system": w.get("WorkspaceProperties", {}).get(
                            "OperatingSystemName"
                        ),
                        "protocols": w.get("WorkspaceProperties", {}).get("Protocols"),
                        "root_volume_gib": w.get("WorkspaceProperties", {}).get(
                            "RootVolumeSizeGib"
                        ),
                        "user_volume_gib": w.get("WorkspaceProperties", {}).get(
                            "UserVolumeSizeGib"
                        ),
                        "auto_stop_timeout_minutes": w.get("WorkspaceProperties", {}).get(
                            "RunningModeAutoStopTimeoutInMinutes"
                        ),
                        "root_volume_encrypted": w.get("RootVolumeEncryptionEnabled"),
                        "user_volume_encrypted": w.get("UserVolumeEncryptionEnabled"),
                        "subnet_id": w.get("SubnetId"),
                    },
                )
                for w in (personal or [])
            ],
        )
        return section, errors

    def _pools_section() -> tuple[InventoryReportSection, list[ServiceError]]:
        errors: list[ServiceError] = []
        pools = try_call(
            errors,
            consts.PRODUCT_WORKSPACES_POOLS,
            "DescribeWorkspacesPools",
            lambda: paginate(workspaces.describe_workspaces_pools, "WorkspacesPools"),
            default=[],
        )
        section = InventoryReportSection(
            service=consts.PRODUCT_WORKSPACES_POOLS,
            resource_type="WorkSpacesPool",
            resources=[
                ResourceRecord(
                    id=p.get("PoolId", p.get("PoolName", "")),
                    name=p.get("PoolName"),
                    state=p.get("State"),
                    attributes={
                        "capacity": p.get("CapacityStatus"),
                        "bundle_id": p.get("BundleId"),
                        "directory_id": p.get("DirectoryId"),
                        "running_mode": p.get("RunningMode"),
                        "description": p.get("Description"),
                        "created_at": str(p.get("CreatedAt")) if p.get("CreatedAt") else None,
                        "errors": p.get("Errors"),
                    },
                )
                for p in (pools or [])
            ],
        )
        return section, errors

    def _fleets_section() -> tuple[InventoryReportSection, list[ServiceError]]:
        errors: list[ServiceError] = []
        fleets = try_call(
            errors,
            consts.PRODUCT_WORKSPACES_APPLICATIONS,
            "DescribeFleets",
            lambda: paginate(appstream.describe_fleets, "Fleets"),
            default=[],
        )
        section = InventoryReportSection(
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
                        "image_name": f.get("ImageName"),
                        "max_user_duration_seconds": f.get("MaxUserDurationInSeconds"),
                        "idle_disconnect_timeout_seconds": f.get("IdleDisconnectTimeoutInSeconds"),
                        "disconnect_timeout_seconds": f.get("DisconnectTimeoutInSeconds"),
                        "max_sessions_per_instance": f.get("MaxSessionsPerInstance"),
                        "default_internet_access": f.get("EnableDefaultInternetAccess"),
                    },
                )
                for f in (fleets or [])
            ],
        )
        return section, errors

    def _stacks_section() -> tuple[InventoryReportSection, list[ServiceError]]:
        errors: list[ServiceError] = []
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
                        "user_settings": s.get("UserSettings"),
                        "storage_connectors": s.get("StorageConnectors"),
                        "application_settings": s.get("ApplicationSettings"),
                    },
                )
            )
        section = InventoryReportSection(
            service=consts.PRODUCT_WORKSPACES_APPLICATIONS,
            resource_type="Stack",
            resources=stack_records,
        )
        return section, errors

    def _portals_section() -> tuple[InventoryReportSection, list[ServiceError]]:
        errors: list[ServiceError] = []
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
        section = InventoryReportSection(
            service=consts.PRODUCT_SECURE_BROWSER,
            resource_type="Portal",
            resources=[
                ResourceRecord(
                    id=p.get("portalArn", p.get("portalId", "")),
                    name=p.get("displayName"),
                    state=p.get("portalStatus"),
                    attributes={
                        "browser_type": p.get("browserType"),
                        "authentication_type": p.get("authenticationType"),
                        "max_concurrent_sessions": p.get("maxConcurrentSessions"),
                        "instance_type": p.get("instanceType"),
                        "renderer_type": p.get("rendererType"),
                        "portal_endpoint": p.get("portalEndpoint"),
                    },
                )
                for p in (portals or [])
            ],
        )
        return section, errors

    def _instances_section() -> tuple[InventoryReportSection, list[ServiceError]]:
        errors: list[ServiceError] = []
        instances = try_call(
            errors,
            consts.PRODUCT_WORKSPACES_CORE_INSTANCES,
            "ListWorkspaceInstances",
            lambda: paginate(instances_client.list_workspace_instances, "WorkspaceInstances"),
            default=[],
        )
        # Enrich with EC2 details (type/state/launch/IP) for the backing instances.
        ec2_ids = [
            eid
            for i in (instances or [])
            if (eid := (i.get("EC2ManagedInstance") or {}).get("InstanceId"))
        ]
        ec2_by_id: dict[str, dict] = {}
        if ec2_ids:
            ec2 = factory.client(consts.EC2_API, region=region)
            reservations = try_call(
                errors,
                consts.PRODUCT_WORKSPACES_CORE_INSTANCES,
                "DescribeInstances",
                lambda: ec2.describe_instances(InstanceIds=ec2_ids).get("Reservations", []),
                default=[],
            )
            for res in reservations or []:
                for inst in res.get("Instances", []):
                    ec2_by_id[inst.get("InstanceId", "")] = inst
        section = InventoryReportSection(
            service=consts.PRODUCT_WORKSPACES_CORE_INSTANCES,
            resource_type="ManagedInstance",
            resources=[_managed_instance_record(i, ec2_by_id) for i in (instances or [])],
        )
        return section, errors

    results = gather_concurrently(
        _personal_section,
        _pools_section,
        _fleets_section,
        _stacks_section,
        _portals_section,
        _instances_section,
    )
    sections = [section for section, _ in results]
    errors = [error for _, job_errors in results for error in job_errors]
    total = sum(len(s.resources) for s in sections)
    return InventoryReport(region=region, total_resources=total, sections=sections, errors=errors)


def audit_security_posture_core(factory: ClientFactory, region: str | None) -> AuditReport:
    """Security-posture audit; the four per-service checks run concurrently."""
    workspaces = factory.client(consts.WORKSPACES_API, region=region)
    secure = factory.client(consts.SECURE_BROWSER_API, region=region)
    appstream = factory.client(consts.APPSTREAM_API, region=region)

    def _check_workspace_encryption() -> tuple[list[Finding], dict[str, int], list[ServiceError]]:
        errors: list[ServiceError] = []
        findings: list[Finding] = []
        personal = try_call(
            errors,
            consts.PRODUCT_WORKSPACES_PERSONAL,
            "DescribeWorkspaces",
            lambda: paginate(workspaces.describe_workspaces, "Workspaces"),
            default=[],
        )
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
                        detail=f"WorkSpace {wid} has unencrypted {', '.join(unencrypted)} "
                        "volume(s); encryption can only be set at creation time.",
                        recommendation="Recreate the WorkSpace with root/user volume encryption.",
                        resource_id=wid,
                    )
                )
        return findings, {"workspaces": len(personal or [])}, errors

    def _check_directories() -> tuple[list[Finding], dict[str, int], list[ServiceError]]:
        errors: list[ServiceError] = []
        findings: list[Finding] = []
        directories = try_call(
            errors,
            consts.PRODUCT_WORKSPACES_PERSONAL,
            "DescribeWorkspaceDirectories",
            lambda: paginate(workspaces.describe_workspace_directories, "Directories"),
            default=[],
        )
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
                        recommendation="Attach an IP access control group to restrict trusted "
                        "ranges.",
                        resource_id=did,
                    )
                )
        return findings, {"directories": len(directories or [])}, errors

    def _check_portals() -> tuple[list[Finding], dict[str, int], list[ServiceError]]:
        # Secure Browser portals: flag relaxed data-egress controls (download/copy/print enabled).
        errors: list[ServiceError] = []
        findings: list[Finding] = []
        portals = try_call(
            errors,
            consts.PRODUCT_SECURE_BROWSER,
            "ListPortals",
            lambda: paginate(
                secure.list_portals,
                "portals",
                pagination_in="nextToken",
                pagination_out="nextToken",
            ),
            default=[],
        )
        for p in portals or []:
            arn = p.get("portalArn", "")
            us_arn = p.get("userSettingsArn")
            if not us_arn:
                continue
            us = try_call(
                errors,
                consts.PRODUCT_SECURE_BROWSER,
                "GetUserSettings",
                lambda us_arn=us_arn: secure.get_user_settings(userSettingsArn=us_arn).get(
                    "userSettings", {}
                ),
                default={},
            )
            enabled = [
                flag
                for flag in consts.SECURE_BROWSER_EGRESS_FLAGS
                if (us or {}).get(flag) == "Enabled"
            ]
            if enabled:
                findings.append(
                    Finding(
                        severity="warning",
                        title=f"Secure Browser portal allows data egress: {', '.join(enabled)}",
                        detail=f"Portal {p.get('displayName') or arn} permits "
                        f"{', '.join(enabled)} — content can leave the managed browser session.",
                        recommendation="Disable unneeded download/copy/print in the user settings.",
                        resource_id=arn,
                    )
                )
        return findings, {"portals": len(portals or [])}, errors

    def _check_stacks() -> tuple[list[Finding], dict[str, int], list[ServiceError]]:
        # Applications stacks: flag relaxed UserSettings that permit local data egress.
        errors: list[ServiceError] = []
        findings: list[Finding] = []
        stacks = try_call(
            errors,
            consts.PRODUCT_WORKSPACES_APPLICATIONS,
            "DescribeStacks",
            lambda: paginate(appstream.describe_stacks, "Stacks"),
            default=[],
        )
        egress_actions = {
            "CLIPBOARD_COPY_TO_LOCAL_DEVICE",
            "FILE_DOWNLOAD",
            "PRINTING_TO_LOCAL_DEVICE",
        }
        for s in stacks or []:
            name = s.get("Name", "")
            enabled = [
                u.get("Action")
                for u in (s.get("UserSettings") or [])
                if u.get("Action") in egress_actions and u.get("Permission") == "ENABLED"
            ]
            if enabled:
                findings.append(
                    Finding(
                        severity="warning",
                        title=f"WorkSpaces Applications stack allows data egress: "
                        f"{', '.join(enabled)}",
                        detail=f"Stack {name} permits {', '.join(enabled)} to the local device.",
                        recommendation="Disable unneeded copy-to-local/file-download/"
                        "local-printing in the stack user settings.",
                        resource_id=name,
                    )
                )
        return findings, {"stacks": len(stacks or [])}, errors

    results = gather_concurrently(
        _check_workspace_encryption, _check_directories, _check_portals, _check_stacks
    )
    findings = [f for job_findings, _, _ in results for f in job_findings]
    resources_checked: dict[str, int] = {}
    for _, checked, _ in results:
        resources_checked.update(checked)
    errors = [e for _, _, job_errors in results for e in job_errors]

    if not findings and any(resources_checked.values()):
        findings.append(
            Finding(
                severity="info",
                title="No posture issues found in the checks performed",
                detail="Checked WorkSpace volume encryption, directory IP access groups, and "
                "Secure Browser / Applications data-egress controls.",
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
    factory: ClientFactory, region: str | None, lookback_days: LookbackDays = 14
) -> UnusedResourcesReport:
    # Pre-create the appstream client on this thread (the utilization job creates its own).
    appstream = factory.client(consts.APPSTREAM_API, region=region)

    def _unused_workspaces() -> tuple[list[UnusedResource], list[ServiceError]]:
        utilization = cost.analyze_workspace_utilization_core(factory, region, lookback_days)
        items = [
            UnusedResource(
                service=consts.PRODUCT_WORKSPACES_PERSONAL,
                resource_type="WorkSpace",
                id=w.workspace_id,
                reason=f"No user connections in the last {lookback_days} days.",
            )
            for w in utilization.workspaces
            if w.classification == "unused"
        ]
        return items, list(utilization.errors)

    def _idle_fleets() -> tuple[list[UnusedResource], list[ServiceError]]:
        errors: list[ServiceError] = []
        fleets = try_call(
            errors,
            consts.PRODUCT_WORKSPACES_APPLICATIONS,
            "DescribeFleets",
            lambda: paginate(appstream.describe_fleets, "Fleets"),
            default=[],
        )
        items = [
            UnusedResource(
                service=consts.PRODUCT_WORKSPACES_APPLICATIONS,
                resource_type="Fleet",
                id=f.get("Name", ""),
                reason="Fleet is stopped or has zero desired capacity.",
            )
            for f in fleets or []
            if f.get("State") == "STOPPED" or f.get("ComputeCapacityStatus", {}).get("Desired") == 0
        ]
        return items, errors

    results = gather_concurrently(_unused_workspaces, _idle_fleets)
    items = [item for job_items, _ in results for item in job_items]
    errors = [error for _, job_errors in results for error in job_errors]

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
        key attributes per resource — including each desktop's lifecycle State (AVAILABLE =
        running/powered on, STOPPED, ...), the authoritative answer to "which WorkSpaces are
        running right now?". Read-only.

        Args:
            region: AWS region. Defaults to the server's configured region.
        """
        report = await asyncio.to_thread(
            generate_inventory_report_core, factory, region or factory.region
        )
        return report.model_dump()

    async def audit_security_posture(region: str | None = None) -> dict[str, Any]:
        """Audit EUC security posture against common best practices.

        Checks WorkSpaces Personal root/user volume encryption and whether directories have IP
        access control groups, returning severity-ranked findings. Read-only.

        Args:
            region: AWS region. Defaults to the server's configured region.
        """
        report = await asyncio.to_thread(
            audit_security_posture_core, factory, region or factory.region
        )
        return report.model_dump()

    async def list_unused_resources(
        region: str | None = None, lookback_days: LookbackDays = 14
    ) -> dict[str, Any]:
        """List candidate idle/unused EUC resources worth reclaiming.

        Surfaces unused WorkSpaces Personal desktops (no connections in the window) and stopped or
        zero-capacity WorkSpaces Applications fleets. Read-only.

        Args:
            region: AWS region. Defaults to the server's configured region.
            lookback_days: Window for WorkSpace usage (default 14).
        """
        report = await asyncio.to_thread(
            list_unused_resources_core, factory, region or factory.region, lookback_days
        )
        return report.model_dump()

    mcp.add_tool(generate_inventory_report, annotations=read_only("Inventory report"))
    mcp.add_tool(audit_security_posture, annotations=read_only("Security posture audit"))
    mcp.add_tool(list_unused_resources, annotations=read_only("List unused resources"))
