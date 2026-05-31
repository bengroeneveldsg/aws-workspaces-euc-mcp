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
    MetricStat,
    PerformanceReport,
    Recommendation,
    RecommendationReport,
    ServiceError,
    UsagePoint,
    WorkspacePerformance,
)
from ._common import paginate, try_call

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
    lookback_hours: int = 3,
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
    cpu_peak = cpu.peak if cpu else None
    mem_peak = mem.peak if mem else None
    if cpu_peak is None or mem_peak is None:
        return None  # insufficient data

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
    factory: ClientFactory, region: str | None, lookback_days: int = 7
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
        compute_type = ws.get("WorkspaceProperties", {}).get("ComputeTypeName")
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
            recommendations.append(rec)

    notes = [
        "Based on native AWS/WorkSpaces CPUUsage/MemoryUsage (window peak). Savings are not "
        "estimated (they need per-bundle pricing); this identifies candidates and direction.",
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


def _fetch_fleet_usage(
    cloudwatch: Any, fleet_name: str, lookback_days: int, period_hours: int
) -> dict[str, FleetMetricSeries]:
    """Fetch Average + Maximum time-series for each AWS/AppStream fleet capacity metric."""
    end = datetime.now(UTC)
    start = end - timedelta(days=lookback_days)
    period = period_hours * 3600
    stat_suffix = {"Average": "avg", "Maximum": "max"}
    queries: list[dict[str, Any]] = []
    for i, (name, _unit) in enumerate(consts.APPSTREAM_FLEET_METRICS):
        for stat in ("Average", "Maximum"):
            queries.append(
                {
                    "Id": f"f{i}_{stat_suffix[stat]}",
                    "MetricStat": {
                        "Metric": {
                            "Namespace": "AWS/AppStream",
                            "MetricName": name,
                            "Dimensions": [{"Name": "Fleet", "Value": fleet_name}],
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
    for i, (name, unit) in enumerate(consts.APPSTREAM_FLEET_METRICS):
        avg = by_id.get(f"f{i}_avg", {})
        mx = by_id.get(f"f{i}_max", {})
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


def _summarize_fleet_usage(metrics: dict[str, FleetMetricSeries], lookback_days: int) -> str:
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
    lookback_days: int = 7,
    period_hours: int = 24,
) -> FleetUsage:
    errors: list[ServiceError] = []
    cloudwatch = factory.client(consts.CLOUDWATCH_API, region=region)
    metrics = try_call(
        errors,
        "Amazon CloudWatch",
        "GetMetricData",
        lambda: _fetch_fleet_usage(cloudwatch, fleet_name, lookback_days, period_hours),
        default={},
    )
    return FleetUsage(
        fleet_name=fleet_name,
        lookback_days=lookback_days,
        period_hours=period_hours,
        metrics=metrics or {},
        summary=_summarize_fleet_usage(metrics or {}, lookback_days),
        errors=errors,
    )


def register(mcp: Any, factory: ClientFactory) -> None:
    """Register performance & right-sizing tools on the FastMCP app."""

    async def get_workspace_performance(
        workspace_ids: list[str], region: str | None = None, lookback_hours: int = 3
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
        region: str | None = None, lookback_days: int = 7
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
        lookback_days: int = 7,
        period_hours: int = 24,
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

    mcp.add_tool(get_workspace_performance)
    mcp.add_tool(recommend_bundle_rightsizing)
    mcp.add_tool(get_application_fleet_usage)
