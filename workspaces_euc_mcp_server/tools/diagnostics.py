# Copyright bengroeneveldsg. Licensed under the Apache License, Version 2.0 (the "License").
# You may not use this file except in compliance with the License.
# A copy of the License is located at http://www.apache.org/licenses/LICENSE-2.0
"""Troubleshooting & triage tools (read-only, IAM Tier 0).

Each tool correlates several AWS signals (resource state, directory health, CloudWatch telemetry,
auto-scaling activity) into a single synthesized diagnosis with severity-ranked findings and
recommendations — rather than returning raw API output. All collection is best-effort: a failing
signal is recorded and the diagnosis proceeds with what it could gather.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

from .. import consts
from ..clients import ClientFactory
from ..models import Diagnosis, DirectoryHealthReport, Finding, ServiceError
from ._common import try_call

# AWS Directory Service directory IDs look like d-xxxxxxxxxx. WorkSpaces Pools and other
# WorkSpaces-managed directories use other prefixes (e.g. wsd-...) that are NOT backed by AWS
# Directory Service, so ds:DescribeDirectories rejects them.
_AWS_DS_DIRECTORY_ID = re.compile(r"^d-[0-9a-f]{10}$")

# WorkSpace states that indicate the desktop itself is broken.
_UNHEALTHY_WORKSPACE_STATES = {"ERROR", "UNHEALTHY", "IMPAIRED"}
# States where the desktop is intentionally not running (AutoStop) but recoverable.
_STOPPED_WORKSPACE_STATES = {"STOPPED", "SUSPENDED"}

_SEVERITY_RANK = {"info": 0, "warning": 1, "critical": 2}
_RANK_STATUS = {0: "healthy", 1: "degraded", 2: "unhealthy"}


def _overall_status(findings: list[Finding]) -> str:
    if not findings:
        return "unknown"
    worst = max(_SEVERITY_RANK.get(f.severity, 0) for f in findings)
    return _RANK_STATUS[worst]


def _metric_stat(
    cloudwatch: Any,
    namespace: str,
    metric_name: str,
    dimensions: dict[str, str],
    lookback_hours: int,
    stat: str = "Sum",
) -> float | None:
    """Fetch a single aggregated CloudWatch metric value over the lookback window."""
    end = datetime.now(UTC)
    start = end - timedelta(hours=lookback_hours)
    response = cloudwatch.get_metric_data(
        MetricDataQueries=[
            {
                "Id": "m1",
                "MetricStat": {
                    "Metric": {
                        "Namespace": namespace,
                        "MetricName": metric_name,
                        "Dimensions": [{"Name": k, "Value": v} for k, v in dimensions.items()],
                    },
                    "Period": 3600,
                    "Stat": stat,
                },
                "ReturnData": True,
            }
        ],
        StartTime=start,
        EndTime=end,
    )
    values = response.get("MetricDataResults", [{}])[0].get("Values", [])
    if not values:
        return None
    if stat == "Sum":
        return float(sum(values))
    if stat == "Maximum":
        return float(max(values))
    if stat == "Minimum":
        return float(min(values))
    return float(sum(values) / len(values))


# --------------------------------------------------------------------------------------
# WorkSpaces Personal connectivity
# --------------------------------------------------------------------------------------


def diagnose_workspace_connectivity_core(
    factory: ClientFactory,
    workspace_id: str,
    region: str | None,
    lookback_hours: int = 24,
) -> Diagnosis:
    errors: list[ServiceError] = []
    findings: list[Finding] = []
    signals: dict[str, object] = {}

    workspaces = factory.client(consts.WORKSPACES_API, region=region)

    described = try_call(
        errors,
        consts.PRODUCT_WORKSPACES_PERSONAL,
        "DescribeWorkspaces",
        lambda: workspaces.describe_workspaces(WorkspaceIds=[workspace_id]),
        default={},
    )
    items = (described or {}).get("Workspaces", [])
    if not items:
        return Diagnosis(
            target_type=consts.PRODUCT_WORKSPACES_PERSONAL,
            target_id=workspace_id,
            region=region,
            status="not_found" if not errors else "unknown",
            summary=f"WorkSpace {workspace_id} was not found in {region or 'the region'}."
            if not errors
            else f"Could not retrieve WorkSpace {workspace_id}.",
            findings=findings,
            errors=errors,
        )

    ws = items[0]
    state = ws.get("State", "UNKNOWN")
    directory_id = ws.get("DirectoryId")
    signals["state"] = state
    signals["user_name"] = ws.get("UserName")
    signals["computer_name"] = ws.get("ComputerName")
    signals["directory_id"] = directory_id
    signals["compute_type"] = ws.get("WorkspaceProperties", {}).get("ComputeTypeName")
    signals["running_mode"] = ws.get("WorkspaceProperties", {}).get("RunningMode")

    if state in _UNHEALTHY_WORKSPACE_STATES:
        findings.append(
            Finding(
                severity="critical",
                title=f"WorkSpace is in {state} state",
                detail=f"The desktop reports {state}, so connections will fail.",
                recommendation="Reboot the WorkSpace; if it persists, rebuild or restore it.",
            )
        )
    elif state in _STOPPED_WORKSPACE_STATES:
        findings.append(
            Finding(
                severity="warning",
                title=f"WorkSpace is {state}",
                detail="An AutoStop WorkSpace is powered off and starts on connect; a failed start "
                "would present as an inability to connect.",
                recommendation="Confirm it resumes on connect; check start failures otherwise.",
            )
        )
    elif state == "AVAILABLE":
        findings.append(
            Finding(
                severity="info",
                title="WorkSpace state is AVAILABLE",
                detail="The desktop itself is healthy and reachable.",
            )
        )
    else:
        findings.append(
            Finding(
                severity="info",
                title=f"WorkSpace is in transitional state {state}",
                detail="The desktop is mid-transition; retry once it reaches AVAILABLE.",
            )
        )

    conn = try_call(
        errors,
        consts.PRODUCT_WORKSPACES_PERSONAL,
        "DescribeWorkspacesConnectionStatus",
        lambda: workspaces.describe_workspaces_connection_status(WorkspaceIds=[workspace_id]),
        default={},
    )
    conn_items = (conn or {}).get("WorkspacesConnectionStatus", [])
    if conn_items:
        conn_state = conn_items[0].get("ConnectionState", "UNKNOWN")
        signals["connection_state"] = conn_state
        signals["last_known_user_connection"] = str(
            conn_items[0].get("LastKnownUserConnectionTimestamp", "")
        )
        if conn_state == "CONNECTED":
            findings.append(
                Finding(
                    severity="info",
                    title="A user is currently connected",
                    detail="The WorkSpace shows an active connection right now.",
                )
            )

    if directory_id:
        _diagnose_directory_into(
            factory,
            region,
            directory_id,
            findings,
            errors,
            signals_prefix="directory_",
            signals=signals,
        )

    cloudwatch = factory.client(consts.CLOUDWATCH_API, region=region)
    failures = try_call(
        errors,
        "Amazon CloudWatch",
        "GetMetricData",
        lambda: _metric_stat(
            cloudwatch,
            "AWS/WorkSpaces",
            "ConnectionFailure",
            {"WorkspaceId": workspace_id},
            lookback_hours,
        ),
    )
    attempts = try_call(
        errors,
        "Amazon CloudWatch",
        "GetMetricData",
        lambda: _metric_stat(
            cloudwatch,
            "AWS/WorkSpaces",
            "ConnectionAttempt",
            {"WorkspaceId": workspace_id},
            lookback_hours,
        ),
    )
    if failures is not None:
        signals["connection_failures"] = failures
        signals["connection_attempts"] = attempts
        if failures > 0:
            ratio = f" ({failures:.0f}/{attempts:.0f} attempts)" if attempts else ""
            findings.append(
                Finding(
                    severity="warning",
                    title=f"{failures:.0f} connection failures in {lookback_hours}h{ratio}",
                    detail="Repeated connection failures suggest a client, network, or directory "
                    "problem rather than the desktop state.",
                    recommendation="Check the client/network path and directory health below.",
                )
            )

    status = _overall_status(findings)
    return Diagnosis(
        target_type=consts.PRODUCT_WORKSPACES_PERSONAL,
        target_id=workspace_id,
        region=region,
        status=status,
        summary=_summarize(status, f"WorkSpace {workspace_id}"),
        signals=signals,
        findings=findings,
        errors=errors,
    )


# --------------------------------------------------------------------------------------
# Directory health (shared dependency + standalone tool)
# --------------------------------------------------------------------------------------


def _diagnose_directory_into(
    factory: ClientFactory,
    region: str | None,
    directory_id: str,
    findings: list[Finding],
    errors: list[ServiceError],
    signals_prefix: str = "",
    signals: dict[str, object] | None = None,
) -> None:
    """Append directory-health findings (registration + Directory Service stage)."""
    workspaces = factory.client(consts.WORKSPACES_API, region=region)
    reg = try_call(
        errors,
        consts.PRODUCT_WORKSPACES_PERSONAL,
        "DescribeWorkspaceDirectories",
        lambda: workspaces.describe_workspace_directories(DirectoryIds=[directory_id]),
        default={},
    )
    dirs = (reg or {}).get("Directories", [])
    if dirs:
        reg_state = dirs[0].get("State", "UNKNOWN")
        if signals is not None:
            signals[f"{signals_prefix}registration_state"] = reg_state
        if reg_state != "REGISTERED":
            findings.append(
                Finding(
                    severity="critical",
                    title=f"Directory {directory_id} is {reg_state}, not REGISTERED",
                    detail="WorkSpaces cannot broker connections through a directory that is not "
                    "registered.",
                    recommendation="Re-register the directory with WorkSpaces.",
                )
            )

    if not _AWS_DS_DIRECTORY_ID.match(directory_id):
        # WorkSpaces-managed (e.g. Pools) directory — no AWS Directory Service stage to check.
        if signals is not None:
            signals[f"{signals_prefix}stage"] = "N/A (WorkSpaces-managed)"
        findings.append(
            Finding(
                severity="info",
                title=f"Directory {directory_id} is WorkSpaces-managed",
                detail="This directory is not backed by AWS Directory Service, so there is no "
                "Directory Service stage to evaluate; registration state is used instead.",
            )
        )
        return

    stage_resp = try_call(
        errors,
        "AWS Directory Service",
        "DescribeDirectories",
        lambda: factory.client(consts.DIRECTORY_API, region=region).describe_directories(
            DirectoryIds=[directory_id]
        ),
        default={},
    )
    desc = (stage_resp or {}).get("DirectoryDescriptions", [])
    if desc:
        stage = desc[0].get("Stage", "Unknown")
        if signals is not None:
            signals[f"{signals_prefix}stage"] = stage
        if stage != "Active":
            findings.append(
                Finding(
                    severity="critical",
                    title=f"Directory {directory_id} stage is {stage}",
                    detail=f"AWS Directory Service reports the directory as {stage}; this blocks "
                    "authentication and connections.",
                    recommendation="Investigate the directory in AWS Directory Service "
                    "(DNS, domain controllers, networking).",
                )
            )
        else:
            findings.append(
                Finding(
                    severity="info",
                    title=f"Directory {directory_id} is Active",
                    detail="The backing directory is healthy.",
                )
            )


def check_directory_health_core(
    factory: ClientFactory,
    directory_id: str | None,
    region: str | None,
) -> DirectoryHealthReport:
    errors: list[ServiceError] = []
    workspaces = factory.client(consts.WORKSPACES_API, region=region)

    if directory_id:
        directory_ids = [directory_id]
    else:
        listed = try_call(
            errors,
            consts.PRODUCT_WORKSPACES_PERSONAL,
            "DescribeWorkspaceDirectories",
            lambda: workspaces.describe_workspace_directories(),
            default={},
        )
        directory_ids = [d.get("DirectoryId") for d in (listed or {}).get("Directories", [])]
        directory_ids = [d for d in directory_ids if d]

    diagnoses: list[Diagnosis] = []
    for did in directory_ids:
        findings: list[Finding] = []
        signals: dict[str, object] = {}
        dir_errors: list[ServiceError] = []
        _diagnose_directory_into(factory, region, did, findings, dir_errors, signals=signals)
        status = _overall_status(findings)
        diagnoses.append(
            Diagnosis(
                target_type="WorkSpaces directory",
                target_id=did,
                region=region,
                status=status,
                summary=_summarize(status, f"Directory {did}"),
                signals=signals,
                findings=findings,
                errors=dir_errors,
            )
        )

    return DirectoryHealthReport(region=region, directories=diagnoses, errors=errors)


# --------------------------------------------------------------------------------------
# WorkSpaces Applications fleet
# --------------------------------------------------------------------------------------


def diagnose_application_fleet_core(
    factory: ClientFactory,
    fleet_name: str,
    region: str | None,
    lookback_hours: int = 24,
) -> Diagnosis:
    errors: list[ServiceError] = []
    findings: list[Finding] = []
    signals: dict[str, object] = {}

    appstream = factory.client(consts.APPSTREAM_API, region=region)
    described = try_call(
        errors,
        consts.PRODUCT_WORKSPACES_APPLICATIONS,
        "DescribeFleets",
        lambda: appstream.describe_fleets(Names=[fleet_name]),
        default={},
    )
    fleets = (described or {}).get("Fleets", [])
    if not fleets:
        return Diagnosis(
            target_type=consts.PRODUCT_WORKSPACES_APPLICATIONS,
            target_id=fleet_name,
            region=region,
            status="not_found" if not errors else "unknown",
            summary=f"Fleet {fleet_name} was not found in {region or 'the region'}."
            if not errors
            else f"Could not retrieve fleet {fleet_name}.",
            findings=findings,
            errors=errors,
        )

    fleet = fleets[0]
    state = fleet.get("State", "UNKNOWN")
    signals["state"] = state

    if state == "STOPPED":
        findings.append(
            Finding(
                severity="warning",
                title="Fleet is STOPPED",
                detail="A stopped fleet serves no sessions.",
                recommendation="Start the fleet if users need access.",
            )
        )
    elif state in {"STARTING", "STOPPING"}:
        findings.append(
            Finding(
                severity="info",
                title=f"Fleet is {state}",
                detail="The fleet is mid-transition.",
            )
        )
    elif state == "RUNNING":
        findings.append(
            Finding(
                severity="info",
                title="Fleet is RUNNING",
                detail="The fleet is active.",
            )
        )

    for err in fleet.get("FleetErrors", []) or []:
        findings.append(
            Finding(
                severity="critical",
                title=f"Fleet error: {err.get('ErrorCode', 'Unknown')}",
                detail=err.get("ErrorMessage", "No message provided."),
                recommendation="Resolve the underlying error (IAM role, image, or networking).",
            )
        )

    capacity = fleet.get("ComputeCapacityStatus", {})
    desired = capacity.get("Desired")
    running = capacity.get("Running")
    in_use = capacity.get("InUse")
    available = capacity.get("Available")
    signals["capacity"] = {
        "desired": desired,
        "running": running,
        "in_use": in_use,
        "available": available,
    }
    if available == 0 and running and in_use is not None and in_use >= running:
        findings.append(
            Finding(
                severity="critical",
                title="Fleet capacity is exhausted",
                detail=f"All {running} running instances are in use (0 available); new sessions "
                "will be rejected.",
                recommendation="Raise desired capacity or enable/extend auto scaling.",
            )
        )
    elif desired is not None and running is not None and running < desired:
        findings.append(
            Finding(
                severity="warning",
                title=f"Fleet is below desired capacity ({running}/{desired})",
                detail="Fewer instances are running than desired; users may queue while it scales.",
                recommendation="Check scaling activity and instance launch errors.",
            )
        )

    scaling = try_call(
        errors,
        "Application Auto Scaling",
        "DescribeScalingActivities",
        lambda: factory.client(
            "application-autoscaling", region=region
        ).describe_scaling_activities(
            ServiceNamespace="appstream", ResourceId=f"fleet/{fleet_name}"
        ),
        default={},
    )
    recent = (scaling or {}).get("ScalingActivities", [])
    failed_scaling = [a for a in recent if a.get("StatusCode") not in (None, "Successful")]
    if failed_scaling:
        signals["failed_scaling_activities"] = len(failed_scaling)
        latest = failed_scaling[0]
        findings.append(
            Finding(
                severity="warning",
                title=f"{len(failed_scaling)} recent scaling activities did not succeed",
                detail=f"Most recent: {latest.get('StatusCode')} — "
                f"{latest.get('StatusMessage', 'no message')}.",
                recommendation="Review service limits and the fleet's scaling policy.",
            )
        )

    cloudwatch = factory.client(consts.CLOUDWATCH_API, region=region)
    insufficient = try_call(
        errors,
        "Amazon CloudWatch",
        "GetMetricData",
        lambda: _metric_stat(
            cloudwatch,
            "AWS/AppStream",
            "InsufficientCapacityError",
            {"Fleet": fleet_name},
            lookback_hours,
        ),
    )
    if insufficient:
        signals["insufficient_capacity_errors"] = insufficient
        findings.append(
            Finding(
                severity="critical",
                title=f"{insufficient:.0f} insufficient-capacity errors in {lookback_hours}h",
                detail="Users were denied sessions because no capacity was available.",
                recommendation="Increase capacity or auto-scaling headroom.",
            )
        )

    status = _overall_status(findings)
    return Diagnosis(
        target_type=consts.PRODUCT_WORKSPACES_APPLICATIONS,
        target_id=fleet_name,
        region=region,
        status=status,
        summary=_summarize(status, f"Fleet {fleet_name}"),
        signals=signals,
        findings=findings,
        errors=errors,
    )


def _summarize(status: str, subject: str) -> str:
    return {
        "healthy": f"{subject} looks healthy.",
        "degraded": f"{subject} is degraded — see findings.",
        "unhealthy": f"{subject} is unhealthy — see critical findings.",
        "unknown": f"{subject} could not be fully assessed.",
        "not_found": f"{subject} was not found.",
    }.get(status, f"{subject}: {status}.")


# --------------------------------------------------------------------------------------
# WorkSpaces Pools
# --------------------------------------------------------------------------------------


def diagnose_pool_core(
    factory: ClientFactory,
    pool_id: str,
    region: str | None,
    lookback_hours: int = 24,
) -> Diagnosis:
    errors: list[ServiceError] = []
    findings: list[Finding] = []
    signals: dict[str, object] = {}

    workspaces = factory.client(consts.WORKSPACES_API, region=region)
    described = try_call(
        errors,
        consts.PRODUCT_WORKSPACES_POOLS,
        "DescribeWorkspacesPools",
        lambda: workspaces.describe_workspaces_pools(PoolIds=[pool_id]),
        default={},
    )
    pools = (described or {}).get("WorkspacesPools", [])
    if not pools:
        return Diagnosis(
            target_type=consts.PRODUCT_WORKSPACES_POOLS,
            target_id=pool_id,
            region=region,
            status="not_found" if not errors else "unknown",
            summary=f"Pool {pool_id} was not found in {region or 'the region'}."
            if not errors
            else f"Could not retrieve pool {pool_id}.",
            findings=findings,
            errors=errors,
        )

    pool = pools[0]
    state = pool.get("State", "UNKNOWN")
    directory_id = pool.get("DirectoryId")
    signals["state"] = state
    signals["running_mode"] = pool.get("RunningMode")
    signals["directory_id"] = directory_id

    if state == "STOPPED":
        findings.append(
            Finding(
                severity="warning",
                title="Pool is STOPPED",
                detail="A stopped pool serves no sessions.",
                recommendation="Start the pool if users need access.",
            )
        )
    elif state in {"STARTING", "STOPPING", "UPDATING"}:
        findings.append(
            Finding(severity="info", title=f"Pool is {state}", detail="The pool is mid-transition.")
        )
    elif state == "RUNNING":
        findings.append(
            Finding(severity="info", title="Pool is RUNNING", detail="The pool is active.")
        )

    for err in pool.get("Errors", []) or []:
        findings.append(
            Finding(
                severity="critical",
                title=f"Pool error: {err.get('ErrorCode', 'Unknown')}",
                detail=err.get("ErrorMessage", "No message provided."),
                recommendation="Resolve the underlying error (directory, networking, or bundle).",
            )
        )

    cap = pool.get("CapacityStatus", {})
    desired = cap.get("DesiredUserSessions")
    actual = cap.get("ActualUserSessions")
    active = cap.get("ActiveUserSessions")
    available = cap.get("AvailableUserSessions")
    signals["capacity"] = {
        "desired": desired,
        "actual": actual,
        "active": active,
        "available": available,
    }
    if available == 0 and active and actual is not None and active >= actual:
        findings.append(
            Finding(
                severity="critical",
                title="Pool session capacity is exhausted",
                detail=f"All {actual} session slots are in use (0 available); new sessions will "
                "be rejected.",
                recommendation="Raise desired capacity or enable/extend auto scaling.",
            )
        )
    elif desired is not None and actual is not None and actual < desired:
        findings.append(
            Finding(
                severity="warning",
                title=f"Pool is below desired capacity ({actual}/{desired})",
                detail="Fewer session slots are available than desired; users may queue while it "
                "scales.",
                recommendation="Check directory health and scaling activity.",
            )
        )

    if directory_id:
        _diagnose_directory_into(
            factory,
            region,
            directory_id,
            findings,
            errors,
            signals_prefix="directory_",
            signals=signals,
        )

    cloudwatch = factory.client(consts.CLOUDWATCH_API, region=region)
    util = try_call(
        errors,
        "Amazon CloudWatch",
        "GetMetricData",
        lambda: _metric_stat(
            cloudwatch,
            "AWS/WorkSpaces",
            "UserSessionsCapacityUtilization",
            {consts.WORKSPACES_POOL_DIMENSION: pool_id},
            lookback_hours,
            stat="Maximum",
        ),
    )
    if util is not None:
        signals["peak_utilization_percent"] = util

    status = _overall_status(findings)
    return Diagnosis(
        target_type=consts.PRODUCT_WORKSPACES_POOLS,
        target_id=pool_id,
        region=region,
        status=status,
        summary=_summarize(status, f"Pool {pool_id}"),
        signals=signals,
        findings=findings,
        errors=errors,
    )


# --------------------------------------------------------------------------------------
# Registration
# --------------------------------------------------------------------------------------


def register(mcp: Any, factory: ClientFactory) -> None:
    """Register diagnostics tools on the FastMCP app."""

    async def diagnose_workspace_connectivity(
        workspace_id: str, region: str | None = None, lookback_hours: int = 24
    ) -> dict[str, Any]:
        """Diagnose why a WorkSpaces Personal desktop may be unreachable.

        Correlates the WorkSpace state, live connection status, backing directory health, and
        recent CloudWatch connection metrics into a single verdict with severity-ranked findings
        and recommendations. Read-only.

        Args:
            workspace_id: The WorkSpace ID (e.g. ws-xxxxxxxxx).
            region: AWS region. Defaults to the server's configured region.
            lookback_hours: Window for CloudWatch connection metrics (default 24).
        """
        diag = diagnose_workspace_connectivity_core(
            factory, workspace_id, region or factory.region, lookback_hours
        )
        return diag.model_dump()

    async def diagnose_application_fleet(
        fleet_name: str, region: str | None = None, lookback_hours: int = 24
    ) -> dict[str, Any]:
        """Diagnose a WorkSpaces Applications (formerly AppStream 2.0) fleet's health and capacity.

        Use this for any "AppStream" fleet request — WorkSpaces Applications is the rebranded
        AppStream 2.0 (same service/API). Correlates fleet state, fleet errors, compute capacity,
        auto-scaling activity, and recent insufficient-capacity CloudWatch errors into a single
        verdict. Read-only.

        Args:
            fleet_name: The fleet name.
            region: AWS region. Defaults to the server's configured region.
            lookback_hours: Window for CloudWatch capacity metrics (default 24).
        """
        diag = diagnose_application_fleet_core(
            factory, fleet_name, region or factory.region, lookback_hours
        )
        return diag.model_dump()

    async def check_directory_health(
        directory_id: str | None = None, region: str | None = None
    ) -> dict[str, Any]:
        """Check the health of WorkSpaces-registered directories.

        Reports registration state and AWS Directory Service stage for one directory, or all
        WorkSpaces-registered directories in the region when no id is given. Read-only.

        Args:
            directory_id: A specific directory id, or omit to check all registered directories.
            region: AWS region. Defaults to the server's configured region.
        """
        report = check_directory_health_core(factory, directory_id, region or factory.region)
        return report.model_dump()

    async def diagnose_pool(
        pool_id: str, region: str | None = None, lookback_hours: int = 24
    ) -> dict[str, Any]:
        """Diagnose a WorkSpaces Pool's health and session capacity.

        Correlates pool state, pool errors, user-session capacity, backing directory health, and
        recent CloudWatch session-capacity utilization into a single verdict. Read-only.

        Args:
            pool_id: The WorkSpaces Pool ID (wspool-...).
            region: AWS region. Defaults to the server's configured region.
            lookback_hours: Window for CloudWatch utilization (default 24).
        """
        diag = diagnose_pool_core(factory, pool_id, region or factory.region, lookback_hours)
        return diag.model_dump()

    mcp.add_tool(diagnose_workspace_connectivity)
    mcp.add_tool(diagnose_application_fleet)
    mcp.add_tool(check_directory_health)
    mcp.add_tool(diagnose_pool)
