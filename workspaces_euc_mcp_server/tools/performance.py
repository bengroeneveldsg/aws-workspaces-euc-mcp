# Copyright bengroeneveldsg. Licensed under the Apache License, Version 2.0 (the "License").
# You may not use this file except in compliance with the License.
# A copy of the License is located at http://www.apache.org/licenses/LICENSE-2.0
"""WorkSpaces Personal performance metrics & bundle right-sizing (read-only, IAM Tier 0).

The AWS/WorkSpaces namespace publishes per-desktop resource metrics (CPUUsage, MemoryUsage,
GPUUsage, disk usage, latency, uptime, …) natively, keyed by WorkspaceId — no CloudWatch agent
required. ``get_workspace_performance`` surfaces them; ``recommend_bundle_rightsizing`` uses CPU and
memory headroom to suggest a smaller/larger compute type.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from .. import consts
from ..clients import ClientFactory
from ..models import (
    FleetMetricSeries,
    FleetUsage,
    LiveSession,
    MetricStat,
    PerformanceReport,
    Recommendation,
    RecommendationReport,
    ServiceError,
    UsageHistory,
    UsagePoint,
    WorkspacePerformance,
)
from . import pricing
from ._common import LookbackDays, LookbackHours, PeriodHours, paginate, read_only, try_call

# Right-sizing thresholds on window peak (%). Conservative: only suggest a downsize when there is
# clear headroom, and an upsize when the desktop is genuinely pressured.
_DOWNSIZE_PEAK_CPU = 30.0
_DOWNSIZE_PEAK_MEM = 40.0
_UPSIZE_PEAK_CPU = 85.0
_UPSIZE_PEAK_MEM = 85.0
_MIN_DATAPOINTS = 6  # need at least this many hourly points to judge


def _fetch_metrics(
    cloudwatch: Any,
    workspace_id: str,
    metric_names: list[tuple[str, str]],
    lookback_hours: int,
    period: int = 300,
) -> dict[str, MetricStat]:
    """Fetch Average + Maximum series for each metric and reduce to latest/average/peak."""
    end = datetime.now(UTC)
    start = end - timedelta(hours=lookback_hours)
    stat_suffix = {"Average": "avg", "Maximum": "max"}
    queries: list[dict[str, Any]] = []
    for i, (name, _unit) in enumerate(metric_names):
        for stat in ("Average", "Maximum"):
            queries.append(
                {
                    "Id": f"m{i}_{stat_suffix[stat]}",
                    "MetricStat": {
                        "Metric": {
                            "Namespace": "AWS/WorkSpaces",
                            "MetricName": name,
                            "Dimensions": [{"Name": "WorkspaceId", "Value": workspace_id}],
                        },
                        "Period": period,
                        "Stat": stat,
                    },
                    "ReturnData": True,
                }
            )
    response = cloudwatch.get_metric_data(
        MetricDataQueries=queries,
        StartTime=start,
        EndTime=end,
        ScanBy="TimestampAscending",
    )
    by_id = {r.get("Id"): r.get("Values", []) for r in response.get("MetricDataResults", [])}

    out: dict[str, MetricStat] = {}
    for i, (name, unit) in enumerate(metric_names):
        avg_vals = by_id.get(f"m{i}_avg", [])
        max_vals = by_id.get(f"m{i}_max", [])
        if not avg_vals and not max_vals:
            continue
        out[name] = MetricStat(
            latest=avg_vals[-1] if avg_vals else None,
            average=(sum(avg_vals) / len(avg_vals)) if avg_vals else None,
            peak=max(max_vals) if max_vals else None,
            unit=unit,
        )
    return out


def get_workspace_performance_core(
    factory: ClientFactory,
    workspace_ids: list[str],
    region: str | None,
    lookback_hours: LookbackHours = 3,
) -> PerformanceReport:
    errors: list[ServiceError] = []
    cloudwatch = factory.client(consts.CLOUDWATCH_API, region=region)
    results: list[WorkspacePerformance] = []
    for wid in workspace_ids:
        metrics = try_call(
            errors,
            "Amazon CloudWatch",
            "GetMetricData",
            lambda wid=wid: _fetch_metrics(
                cloudwatch, wid, consts.WORKSPACES_PERFORMANCE_METRICS, lookback_hours
            ),
            default={},
        )
        results.append(
            WorkspacePerformance(
                workspace_id=wid,
                lookback_hours=lookback_hours,
                metrics=metrics or {},
                note=None if metrics else "No performance datapoints (desktop may be stopped).",
            )
        )
    return PerformanceReport(
        region=region,
        lookback_hours=lookback_hours,
        workspaces=results,
        errors=errors,
        notes=[
            "Metrics come from the native AWS/WorkSpaces namespace; CPUUsage/MemoryUsage are "
            "percentages. Stopped desktops report no datapoints."
        ],
    )


def _rightsizing_recommendation(
    workspace_id: str,
    compute_type: str | None,
    cpu: MetricStat | None,
    mem: MetricStat | None,
    lookback_days: int,
) -> Recommendation | None:
    if not compute_type:
        return None
    order = consts.WORKSPACES_COMPUTE_ORDER
    if compute_type not in order:
        return None  # graphics / unknown family — not safe to auto-suggest
    idx = order.index(compute_type)
    if cpu is None or mem is None or cpu.peak is None or mem.peak is None:
        return None  # insufficient data
    cpu_peak = cpu.peak
    mem_peak = mem.peak

    def stats() -> str:
        return (
            f"over {lookback_days}d peak CPU {cpu_peak:.0f}% / peak memory {mem_peak:.0f}% "
            f"(avg CPU {cpu.average:.0f}% / mem {mem.average:.0f}%)"
        )

    if cpu_peak < _DOWNSIZE_PEAK_CPU and mem_peak < _DOWNSIZE_PEAK_MEM and idx > 0:
        return Recommendation(
            target_id=workspace_id,
            kind="bundle_rightsizing",
            current=compute_type,
            recommended=order[idx - 1],
            rationale=f"Consistently low utilization ({stats()}); a smaller compute type should "
            "cope and cost less.",
            confidence="high" if cpu_peak < _DOWNSIZE_PEAK_CPU / 2 else "medium",
        )
    if (cpu_peak > _UPSIZE_PEAK_CPU or mem_peak > _UPSIZE_PEAK_MEM) and idx < len(order) - 1:
        return Recommendation(
            target_id=workspace_id,
            kind="bundle_rightsizing",
            current=compute_type,
            recommended=order[idx + 1],
            rationale=f"Resource pressure ({stats()}); a larger compute type would improve the "
            "user experience.",
            confidence="high" if (cpu_peak > 95 or mem_peak > 95) else "medium",
        )
    return None


def recommend_bundle_rightsizing_core(
    factory: ClientFactory, region: str | None, lookback_days: LookbackDays = 7
) -> RecommendationReport:
    errors: list[ServiceError] = []
    workspaces_client = factory.client(consts.WORKSPACES_API, region=region)
    cloudwatch = factory.client(consts.CLOUDWATCH_API, region=region)

    workspaces = try_call(
        errors,
        consts.PRODUCT_WORKSPACES_PERSONAL,
        "DescribeWorkspaces",
        lambda: paginate(workspaces_client.describe_workspaces, "Workspaces"),
        default=[],
    )

    lookback_hours = lookback_days * 24
    recommendations: list[Recommendation] = []
    skipped_no_data = 0
    for ws in workspaces or []:
        wid = ws.get("WorkspaceId", "")
        props = ws.get("WorkspaceProperties", {})
        compute_type = props.get("ComputeTypeName")
        metrics = try_call(
            errors,
            "Amazon CloudWatch",
            "GetMetricData",
            lambda wid=wid: _fetch_metrics(
                cloudwatch,
                wid,
                [("CPUUsage", "Percent"), ("MemoryUsage", "Percent")],
                lookback_hours,
                period=3600,
            ),
            default={},
        )
        cpu = (metrics or {}).get("CPUUsage")
        mem = (metrics or {}).get("MemoryUsage")
        datapoints_ok = cpu is not None and mem is not None
        if not datapoints_ok:
            skipped_no_data += 1
            continue
        rec = _rightsizing_recommendation(wid, compute_type, cpu, mem, lookback_days)
        if rec:
            # Best-effort $ estimate for AlwaysOn desktops: monthly compute-tier difference.
            if props.get("RunningMode") == "ALWAYS_ON":
                cur = pricing.get_workspace_prices(
                    factory,
                    region,
                    props.get("OperatingSystemName"),
                    rec.current,
                    props.get("RootVolumeSizeGib"),
                    props.get("UserVolumeSizeGib"),
                )
                new = pricing.get_workspace_prices(
                    factory,
                    region,
                    props.get("OperatingSystemName"),
                    rec.recommended,
                    props.get("RootVolumeSizeGib"),
                    props.get("UserVolumeSizeGib"),
                )
                if cur and new and cur.alwayson_monthly and new.alwayson_monthly:
                    diff = round(cur.alwayson_monthly - new.alwayson_monthly, 2)
                    rec.estimated_monthly_savings_usd = diff if diff > 0 else None
            recommendations.append(rec)

    notes = [
        "Based on native AWS/WorkSpaces CPUUsage/MemoryUsage (window peak). "
        "estimated_monthly_savings_usd is a best-effort AlwaysOn monthly compute-tier difference "
        "(Price List, Included license); null for AutoStop desktops or unmatched prices.",
        "Graphics compute families are excluded from automatic suggestions.",
    ]
    if skipped_no_data:
        notes.append(
            f"{skipped_no_data} desktop(s) skipped for insufficient metrics (likely stopped "
            "for the whole window)."
        )
    return RecommendationReport(
        region=region,
        lookback_days=lookback_days,
        recommendations=recommendations,
        errors=errors,
        notes=notes,
    )


def _fetch_metric_series(
    cloudwatch: Any,
    namespace: str,
    dimension_name: str,
    dimension_value: str,
    metric_specs: list[tuple[str, str]],
    lookback_days: int,
    period_hours: int,
) -> dict[str, FleetMetricSeries]:
    """Fetch Average + Maximum time-series for each metric and reduce to aggregates + series."""
    end = datetime.now(UTC)
    start = end - timedelta(days=lookback_days)
    period = period_hours * 3600
    stat_suffix = {"Average": "avg", "Maximum": "max"}
    queries: list[dict[str, Any]] = []
    for i, (name, _unit) in enumerate(metric_specs):
        for stat in ("Average", "Maximum"):
            queries.append(
                {
                    "Id": f"s{i}_{stat_suffix[stat]}",
                    "MetricStat": {
                        "Metric": {
                            "Namespace": namespace,
                            "MetricName": name,
                            "Dimensions": [{"Name": dimension_name, "Value": dimension_value}],
                        },
                        "Period": period,
                        "Stat": stat,
                    },
                    "ReturnData": True,
                }
            )
    response = cloudwatch.get_metric_data(
        MetricDataQueries=queries,
        StartTime=start,
        EndTime=end,
        ScanBy="TimestampAscending",
    )
    by_id = {r.get("Id"): r for r in response.get("MetricDataResults", [])}

    out: dict[str, FleetMetricSeries] = {}
    for i, (name, unit) in enumerate(metric_specs):
        avg = by_id.get(f"s{i}_avg", {})
        mx = by_id.get(f"s{i}_max", {})
        avg_vals = avg.get("Values", [])
        avg_ts = avg.get("Timestamps", [])
        max_vals = mx.get("Values", [])
        if not avg_vals and not max_vals:
            continue
        series = [
            UsagePoint(
                timestamp=str(avg_ts[j]) if j < len(avg_ts) else str(j),
                average=avg_vals[j] if j < len(avg_vals) else None,
                peak=max_vals[j] if j < len(max_vals) else None,
            )
            for j in range(max(len(avg_vals), len(max_vals)))
        ]
        out[name] = FleetMetricSeries(
            unit=unit,
            latest=avg_vals[-1] if avg_vals else None,
            average=(sum(avg_vals) / len(avg_vals)) if avg_vals else None,
            peak=max(max_vals) if max_vals else None,
            series=series,
        )
    return out


def _fetch_fleet_usage(
    cloudwatch: Any, fleet_name: str, lookback_days: int, period_hours: int
) -> dict[str, FleetMetricSeries]:
    return _fetch_metric_series(
        cloudwatch,
        "AWS/AppStream",
        "Fleet",
        fleet_name,
        consts.APPSTREAM_FLEET_METRICS,
        lookback_days,
        period_hours,
    )


def _summarize_fleet_usage(metrics: dict[str, FleetMetricSeries], lookback_days: int) -> str | None:
    if not metrics:
        return "No usage datapoints — the fleet was likely stopped for the whole window."
    in_use = metrics.get("InUseCapacity")
    running = metrics.get("RunningCapacity") or metrics.get("ActualCapacity")
    util = metrics.get("CapacityUtilization")
    if in_use and (in_use.peak or 0) == 0 and running and (running.peak or 0) > 0:
        return (
            f"Zero sessions in use across {lookback_days}d, yet up to {running.peak:.0f} "
            "instance(s) were kept running — idle running capacity (cost with no usage). Consider "
            "stopping the fleet or lowering desired capacity."
        )
    if in_use and in_use.peak is not None:
        util_txt = f"; peak utilization {util.peak:.0f}%" if util and util.peak is not None else ""
        return f"Peak {in_use.peak:.0f} instance(s) in use over {lookback_days}d{util_txt}."
    return None


def get_application_fleet_usage_core(
    factory: ClientFactory,
    fleet_name: str,
    region: str | None,
    lookback_days: LookbackDays = 7,
    period_hours: PeriodHours = 24,
) -> FleetUsage:
    errors: list[ServiceError] = []
    # LIVE: who is streaming right now (DescribeSessions via associated stacks).
    appstream = factory.client(consts.APPSTREAM_API, region=region)
    live = fleet_live_sessions(appstream, fleet_name, errors)

    cloudwatch = factory.client(consts.CLOUDWATCH_API, region=region)
    metrics = try_call(
        errors,
        "Amazon CloudWatch",
        "GetMetricData",
        lambda: _fetch_fleet_usage(cloudwatch, fleet_name, lookback_days, period_hours),
        default={},
    )
    history = _summarize_fleet_usage(metrics or {}, lookback_days)
    summary = f"{len(live)} live session(s) right now. {history or ''}".strip()
    return FleetUsage(
        fleet_name=fleet_name,
        lookback_days=lookback_days,
        period_hours=period_hours,
        active_session_count=len(live),
        active_sessions=live,
        metrics=metrics or {},
        summary=summary,
        errors=errors,
    )


def _count_active_buckets(series: list[UsagePoint]) -> int:
    return sum(1 for p in series if (p.peak or 0) >= 1)


def _summarize_connection_history(metrics: dict[str, FleetMetricSeries], lookback_days: int) -> str:
    connected = metrics.get("UserConnected")
    if not connected or not connected.series:
        return f"No connection datapoints in {lookback_days}d (desktop may be stopped/unused)."
    active = _count_active_buckets(connected.series)
    total = len(connected.series)
    if active == 0:
        return f"No user connections in any of the {total} buckets over {lookback_days}d (unused)."
    failures = metrics.get("ConnectionFailure")
    fail_txt = ""
    if failures and (failures.peak or 0) > 0:
        fail_txt = " Connection failures were recorded — check connectivity."
    return f"Connected in {active} of {total} buckets over {lookback_days}d.{fail_txt}"


def get_workspace_connection_history_core(
    factory: ClientFactory,
    workspace_id: str,
    region: str | None,
    lookback_days: LookbackDays = 7,
    period_hours: PeriodHours = 24,
) -> UsageHistory:
    errors: list[ServiceError] = []
    cloudwatch = factory.client(consts.CLOUDWATCH_API, region=region)
    metrics = try_call(
        errors,
        "Amazon CloudWatch",
        "GetMetricData",
        lambda: _fetch_metric_series(
            cloudwatch,
            "AWS/WorkSpaces",
            "WorkspaceId",
            workspace_id,
            consts.WORKSPACES_CONNECTION_METRICS,
            lookback_days,
            period_hours,
        ),
        default={},
    )
    return UsageHistory(
        target_type=consts.PRODUCT_WORKSPACES_PERSONAL,
        target_id=workspace_id,
        lookback_days=lookback_days,
        period_hours=period_hours,
        metrics=metrics or {},
        summary=_summarize_connection_history(metrics or {}, lookback_days),
        errors=errors,
    )


def _summarize_pool_session_history(
    metrics: dict[str, FleetMetricSeries], lookback_days: int
) -> str | None:
    if not metrics:
        return f"No session datapoints in {lookback_days}d (pool may be stopped)."
    active = metrics.get("ActiveUserSessionCapacity")
    util = metrics.get("UserSessionsCapacityUtilization")
    actual = metrics.get("ActualUserSessionCapacity")
    if active and (active.peak or 0) == 0 and actual and (actual.peak or 0) > 0:
        return (
            f"Zero active sessions across {lookback_days}d, yet up to {actual.peak:.0f} session "
            "slot(s) were kept available — idle pool capacity (cost with no usage)."
        )
    if active and active.peak is not None:
        util_txt = f"; peak utilization {util.peak:.0f}%" if util and util.peak is not None else ""
        return f"Peak {active.peak:.0f} active session(s) over {lookback_days}d{util_txt}."
    return None


def _iso(value: Any) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else None


def pool_live_sessions(
    workspaces: Any, pool_id: str, errors: list[ServiceError]
) -> list[LiveSession]:
    """Sessions on a Pool RIGHT NOW, live from DescribeWorkspacesPoolSessions (like the console)."""
    raw = (
        try_call(
            errors,
            consts.PRODUCT_WORKSPACES_POOLS,
            "DescribeWorkspacesPoolSessions",
            lambda: paginate(
                workspaces.describe_workspaces_pool_sessions, "Sessions", PoolId=pool_id
            ),
            default=[],
        )
        or []
    )
    return [
        LiveSession(
            session_id=s.get("SessionId", ""),
            user=s.get("UserId"),
            connection_state=s.get("ConnectionState"),
            start_time=_iso(s.get("StartTime")),
        )
        for s in raw
    ]


def fleet_live_sessions(
    appstream: Any, fleet_name: str, errors: list[ServiceError]
) -> list[LiveSession]:
    """Sessions streaming from a fleet RIGHT NOW, via its associated stacks + DescribeSessions."""
    stacks = (
        try_call(
            errors,
            consts.PRODUCT_WORKSPACES_APPLICATIONS,
            "ListAssociatedStacks",
            lambda: paginate(appstream.list_associated_stacks, "Names", FleetName=fleet_name),
            default=[],
        )
        or []
    )
    sessions: list[LiveSession] = []
    for stack in stacks:
        raw = (
            try_call(
                errors,
                consts.PRODUCT_WORKSPACES_APPLICATIONS,
                "DescribeSessions",
                lambda stack=stack: paginate(
                    appstream.describe_sessions,
                    "Sessions",
                    StackName=stack,
                    FleetName=fleet_name,
                ),
                default=[],
            )
            or []
        )
        sessions.extend(
            LiveSession(
                session_id=s.get("Id", ""),
                user=s.get("UserId"),
                state=s.get("State"),
                connection_state=s.get("ConnectionState"),
                start_time=_iso(s.get("StartTime")),
                stack_name=stack,
            )
            for s in raw
        )
    return sessions


def get_pool_session_history_core(
    factory: ClientFactory,
    pool_id: str,
    region: str | None,
    lookback_days: LookbackDays = 7,
    period_hours: PeriodHours = 24,
) -> UsageHistory:
    errors: list[ServiceError] = []
    # LIVE: current sessions from the real-time API; CloudWatch below is historic-only.
    workspaces = factory.client(consts.WORKSPACES_API, region=region)
    live = pool_live_sessions(workspaces, pool_id, errors)

    cloudwatch = factory.client(consts.CLOUDWATCH_API, region=region)
    metrics = try_call(
        errors,
        "Amazon CloudWatch",
        "GetMetricData",
        lambda: _fetch_metric_series(
            cloudwatch,
            "AWS/WorkSpaces",
            consts.WORKSPACES_POOL_DIMENSION,
            pool_id,
            consts.WORKSPACES_POOL_SESSION_METRICS,
            lookback_days,
            period_hours,
        ),
        default={},
    )
    history = _summarize_pool_session_history(metrics or {}, lookback_days)
    summary = f"{len(live)} live session(s) right now. {history or ''}".strip()
    return UsageHistory(
        target_type=consts.PRODUCT_WORKSPACES_POOLS,
        target_id=pool_id,
        lookback_days=lookback_days,
        period_hours=period_hours,
        active_session_count=len(live),
        active_sessions=live,
        metrics=metrics or {},
        summary=summary,
        errors=errors,
    )


def register(mcp: Any, factory: ClientFactory) -> None:
    """Register performance & right-sizing tools on the FastMCP app."""

    async def get_workspace_performance(
        workspace_ids: list[str], region: str | None = None, lookback_hours: LookbackHours = 3
    ) -> dict[str, Any]:
        """Get native CPU/memory/disk/GPU/latency performance metrics for WorkSpaces Personal.

        Reads the AWS/WorkSpaces namespace (no CloudWatch agent needed) and returns latest, window
        average, and window peak for each metric per desktop. Stopped desktops report no data.

        Args:
            workspace_ids: One or more WorkSpace IDs.
            region: AWS region. Defaults to the server's configured region.
            lookback_hours: Window for the metrics (default 3).
        """
        report = get_workspace_performance_core(
            factory, workspace_ids, region or factory.region, lookback_hours
        )
        return report.model_dump()

    async def recommend_bundle_rightsizing(
        region: str | None = None, lookback_days: LookbackDays = 7
    ) -> dict[str, Any]:
        """Recommend smaller/larger WorkSpace compute types from CPU & memory headroom.

        Uses native AWS/WorkSpaces CPUUsage/MemoryUsage over the window: flags desktops with ample
        headroom to downsize and pressured ones to upsize (general compute families only;
        graphics excluded). Read-only.

        Args:
            region: AWS region. Defaults to the server's configured region.
            lookback_days: Window for the analysis (default 7).
        """
        report = recommend_bundle_rightsizing_core(factory, region or factory.region, lookback_days)
        return report.model_dump()

    async def get_application_fleet_usage(
        fleet_name: str,
        region: str | None = None,
        lookback_days: LookbackDays = 7,
        period_hours: PeriodHours = 24,
    ) -> dict[str, Any]:
        """Get a WorkSpaces Applications (formerly AppStream 2.0) fleet's usage history.

        Returns the AWS/AppStream capacity time-series for the fleet (InUseCapacity,
        CapacityUtilization, Running/Available/Actual/Desired/PendingCapacity) over the window, as
        per-bucket points plus latest/average/peak, with a plain-language summary (e.g. flags idle
        running capacity). For a stack, resolve its fleet first (see generate_inventory_report).
        Read-only.

        Args:
            fleet_name: The fleet name (the fleet behind the stack, not the stack name).
            region: AWS region. Defaults to the server's configured region.
            lookback_days: Window length (default 7).
            period_hours: Time-bucket size in hours (default 24 = daily; use 1 for hourly).
        """
        usage = get_application_fleet_usage_core(
            factory, fleet_name, region or factory.region, lookback_days, period_hours
        )
        return usage.model_dump()

    async def get_workspace_connection_history(
        workspace_id: str,
        region: str | None = None,
        lookback_days: LookbackDays = 7,
        period_hours: PeriodHours = 24,
    ) -> dict[str, Any]:
        """Get a WorkSpaces Personal desktop's connection/session history.

        Returns the AWS/WorkSpaces connection time-series for the desktop (UserConnected, and —
        when present — ConnectionAttempt/Success/Failure, SessionLaunchTime, InSessionLatency) as
        per-bucket points plus latest/average/peak, with a plain-language summary. Read-only.

        Args:
            workspace_id: The WorkSpace ID (ws-...).
            region: AWS region. Defaults to the server's configured region.
            lookback_days: Window length (default 7).
            period_hours: Bucket size in hours (default 24 = daily; use 1 for hourly).
        """
        usage = get_workspace_connection_history_core(
            factory, workspace_id, region or factory.region, lookback_days, period_hours
        )
        return usage.model_dump()

    async def get_pool_session_history(
        pool_id: str,
        region: str | None = None,
        lookback_days: LookbackDays = 7,
        period_hours: PeriodHours = 24,
    ) -> dict[str, Any]:
        """Get a WorkSpaces Pool's user-session history.

        Returns the AWS/WorkSpaces session-capacity time-series for the pool
        (Active/Actual/Available/Desired/PendingUserSessionCapacity and
        UserSessionsCapacityUtilization) as per-bucket points plus latest/average/peak, with a
        plain-language summary that flags idle pool capacity. Read-only.

        Args:
            pool_id: The WorkSpaces Pool ID (wspool-...).
            region: AWS region. Defaults to the server's configured region.
            lookback_days: Window length (default 7).
            period_hours: Bucket size in hours (default 24 = daily; use 1 for hourly).
        """
        usage = get_pool_session_history_core(
            factory, pool_id, region or factory.region, lookback_days, period_hours
        )
        return usage.model_dump()

    mcp.add_tool(get_workspace_performance, annotations=read_only("WorkSpace performance"))
    mcp.add_tool(
        recommend_bundle_rightsizing, annotations=read_only("Recommend bundle right-sizing")
    )
    mcp.add_tool(
        get_application_fleet_usage, annotations=read_only("Applications fleet usage history")
    )
    mcp.add_tool(
        get_workspace_connection_history,
        annotations=read_only("WorkSpace connection history"),
    )
    mcp.add_tool(get_pool_session_history, annotations=read_only("Pool session history"))
