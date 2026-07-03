# Copyright bengroeneveldsg. Licensed under the Apache License, Version 2.0 (the "License").
# You may not use this file except in compliance with the License.
# A copy of the License is located at http://www.apache.org/licenses/LICENSE-2.0
"""Cost & utilization optimization tools (read-only, IAM Tier 1).

These add Cost Explorer and Pricing access on top of Tier 0. They identify under-used WorkSpaces
Personal desktops, recommend running-mode changes, and summarize EUC spend — returning opinionated
findings rather than raw billing/metric data.

Utilization is derived from the standard ``AWS/WorkSpaces`` ``UserConnected`` metric (1 when a user
is connected during a period); no CloudWatch agent is required.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal

from .. import consts
from ..clients import ClientFactory
from ..models import (
    CostComparison,
    CostDriver,
    CostForecast,
    CostLineItem,
    CostPeriod,
    CostSummary,
    ForecastPeriod,
    Recommendation,
    RecommendationReport,
    ServiceError,
    UtilizationReport,
    WorkspaceUtilization,
)
from . import pricing
from ._common import (
    CostLookbackDays,
    DateString,
    ForecastDays,
    LookbackDays,
    gather_concurrently,
    paginate,
    read_only,
    try_call,
)


def _daily_connected_values(cloudwatch: Any, workspace_id: str, lookback_days: int) -> list[float]:
    """Per-day maximum of UserConnected (1 if the desktop had any connection that day)."""
    end = datetime.now(UTC)
    start = end - timedelta(days=lookback_days)
    response = cloudwatch.get_metric_data(
        MetricDataQueries=[
            {
                "Id": "uc",
                "MetricStat": {
                    "Metric": {
                        "Namespace": "AWS/WorkSpaces",
                        "MetricName": "UserConnected",
                        "Dimensions": [{"Name": "WorkspaceId", "Value": workspace_id}],
                    },
                    "Period": 86400,
                    "Stat": "Maximum",
                },
                "ReturnData": True,
            }
        ],
        StartTime=start,
        EndTime=end,
    )
    return response.get("MetricDataResults", [{}])[0].get("Values", [])


def _classify(active_days: int | None, lookback_days: int, idle_threshold: int) -> str:
    if active_days is None:
        return "unknown"
    if active_days == 0:
        return "unused"
    if active_days <= idle_threshold:
        return "idle"
    return "active"


def _collect_utilization(
    factory: ClientFactory, region: str | None, lookback_days: int
) -> tuple[list[WorkspaceUtilization], list[ServiceError]]:
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

    idle_threshold = max(1, lookback_days // 7)
    items: list[WorkspaceUtilization] = []
    for ws in workspaces or []:
        wid = ws.get("WorkspaceId", "")
        props = ws.get("WorkspaceProperties", {})
        values = try_call(
            errors,
            "Amazon CloudWatch",
            "GetMetricData",
            lambda wid=wid: _daily_connected_values(cloudwatch, wid, lookback_days),
        )
        active_days = sum(1 for v in values if v >= 1) if values is not None else None
        items.append(
            WorkspaceUtilization(
                workspace_id=wid,
                running_mode=props.get("RunningMode"),
                lookback_days=lookback_days,
                active_days=active_days,
                classification=_classify(active_days, lookback_days, idle_threshold),
                compute_type=props.get("ComputeTypeName"),
                operating_system=props.get("OperatingSystemName"),
                root_volume_gib=props.get("RootVolumeSizeGib"),
                user_volume_gib=props.get("UserVolumeSizeGib"),
            )
        )
    return items, errors


def analyze_workspace_utilization_core(
    factory: ClientFactory, region: str | None, lookback_days: LookbackDays = 14
) -> UtilizationReport:
    items, errors = _collect_utilization(factory, region, lookback_days)
    counts: dict[str, int] = {}
    for item in items:
        counts[item.classification] = counts.get(item.classification, 0) + 1
    return UtilizationReport(
        region=region,
        lookback_days=lookback_days,
        total=len(items),
        counts=counts,
        workspaces=items,
        errors=errors,
        notes=[
            "Utilization is based on the AWS/WorkSpaces UserConnected metric; a desktop with no "
            "datapoints over the window is reported as unused."
        ],
    )


def recommend_running_mode_core(
    factory: ClientFactory, region: str | None, lookback_days: LookbackDays = 14
) -> RecommendationReport:
    items, errors = _collect_utilization(factory, region, lookback_days)
    recommendations: list[Recommendation] = []
    for item in items:
        if item.running_mode == "ALWAYS_ON" and item.classification in {"unused", "idle"}:
            prices = pricing.get_workspace_prices(
                factory,
                region,
                item.operating_system,
                item.compute_type,
                item.root_volume_gib,
                item.user_volume_gib,
            )
            savings = pricing.estimate_alwayson_to_autostop_savings(
                prices, item.active_days, lookback_days
            )
            recommendations.append(
                Recommendation(
                    target_id=item.workspace_id,
                    kind="running_mode",
                    current="ALWAYS_ON",
                    recommended="AUTO_STOP",
                    rationale=(
                        f"Connected on {item.active_days} of the last {lookback_days} days "
                        f"({item.classification}); AlwaysOn bills the full month regardless of "
                        "use, so AutoStop typically costs less at low usage."
                    ),
                    estimated_monthly_savings_usd=savings,
                    confidence="high" if item.classification == "unused" else "medium",
                )
            )
        elif (
            item.running_mode == "AUTO_STOP"
            and item.active_days is not None
            and item.active_days >= lookback_days
        ):
            recommendations.append(
                Recommendation(
                    target_id=item.workspace_id,
                    kind="running_mode",
                    current="AUTO_STOP",
                    recommended="evaluate ALWAYS_ON",
                    rationale=(
                        f"Connected every day in the last {lookback_days} days; if daily usage is "
                        "also long-duration, AlwaysOn may be cheaper than per-hour AutoStop."
                    ),
                    confidence="low",
                )
            )
    return RecommendationReport(
        region=region,
        lookback_days=lookback_days,
        recommendations=recommendations,
        errors=errors,
        notes=[
            "estimated_monthly_savings_usd is a best-effort Price List estimate (Included license, "
            "on-demand, matched on compute type / OS / volume sizes); it is null when prices can't "
            "be matched or pricing:GetProducts is not permitted. Assumes ~8h per active day."
        ],
    )


def _is_euc_service(service_name: str) -> bool:
    """True if a Cost Explorer SERVICE value belongs to the EUC portfolio.

    Matches by keyword rather than exact name, so account/era naming variants
    (e.g. "Amazon AppStream 2.0") are never silently excluded.
    """
    name = service_name.lower()
    if any(token in name for token in consts.EUC_COST_EXPLORER_EXCLUDE_TOKENS):
        return False
    return any(token in name for token in consts.EUC_COST_EXPLORER_SERVICE_TOKENS)


def _is_core_workspaces_service(name: str) -> bool:
    """True for the 'Amazon WorkSpaces' SERVICE line (Personal/Pools/Core), not Applications/Web."""
    n = name.lower()
    if "workspaces" not in n:
        return False
    return not any(t in n for t in ("applications", "secure browser", "web", "thin client"))


def _classify_workspaces_usage_type(usage_type: str) -> str:
    """Map a WorkSpaces Cost Explorer USAGE_TYPE to Personal / Pools / Core."""
    u = usage_type.lower()
    for token, label in consts.WORKSPACES_USAGE_TYPE_CLASSES:
        if token in u:
            return label
    return consts.WORKSPACES_USAGE_TYPE_DEFAULT_CLASS


def _fetch_usage_type_totals(
    cost_explorer: Any,
    start: str,
    end: str,
    granularity: str,
    service_names: list[str],
    errors: list[ServiceError],
) -> dict[str, float]:
    """Raw USAGE_TYPE -> amount totals for the given (exact) SERVICE names over a window."""
    out: dict[str, float] = {}
    next_token: str | None = None
    while True:
        kwargs: dict[str, Any] = {
            "TimePeriod": {"Start": start, "End": end},
            "Granularity": granularity,
            "Metrics": ["UnblendedCost"],
            # Exact names taken from prior SERVICE results — safe to filter on (no guessing).
            "Filter": {"Dimensions": {"Key": "SERVICE", "Values": service_names}},
            "GroupBy": [{"Type": "DIMENSION", "Key": "USAGE_TYPE"}],
        }
        if next_token:
            kwargs["NextPageToken"] = next_token
        resp = try_call(
            errors,
            "AWS Cost Explorer",
            "GetCostAndUsage",
            lambda kwargs=kwargs: cost_explorer.get_cost_and_usage(**kwargs),
            default={},
        )
        if not resp:
            break
        for period in resp.get("ResultsByTime", []):
            for group in period.get("Groups", []):
                usage_type = (group.get("Keys") or [""])[0]
                amount = float(group.get("Metrics", {}).get("UnblendedCost", {}).get("Amount", 0.0))
                out[usage_type] = out.get(usage_type, 0.0) + amount
        next_token = resp.get("NextPageToken")
        if not next_token:
            break
    return out


def _fetch_workspaces_breakdown(
    cost_explorer: Any,
    start: str,
    end: str,
    granularity: str,
    service_names: list[str],
    errors: list[ServiceError],
) -> dict[str, float]:
    """Split the 'Amazon WorkSpaces' line into Personal/Pools/Core via a USAGE_TYPE query."""
    raw = _fetch_usage_type_totals(cost_explorer, start, end, granularity, service_names, errors)
    out: dict[str, float] = {}
    for usage_type, amount in raw.items():
        label = _classify_workspaces_usage_type(usage_type)
        out[label] = out.get(label, 0.0) + amount
    return {k: round(v, 2) for k, v in out.items() if round(v, 2) != 0.0}


def _discover_euc_service_names(
    cost_explorer: Any, errors: list[ServiceError], lookback_days: int = 30
) -> list[str]:
    """Exact Cost Explorer SERVICE names with recent EUC spend (for filters that need them)."""
    end_d = datetime.now(UTC).date()
    start_d = end_d - timedelta(days=lookback_days)
    names: list[str] = []
    next_token: str | None = None
    while True:
        kwargs: dict[str, Any] = {
            "TimePeriod": {"Start": start_d.isoformat(), "End": end_d.isoformat()},
            "Granularity": "MONTHLY",
            "Metrics": ["UnblendedCost"],
            "GroupBy": [{"Type": "DIMENSION", "Key": "SERVICE"}],
        }
        if next_token:
            kwargs["NextPageToken"] = next_token
        resp = try_call(
            errors,
            "AWS Cost Explorer",
            "GetCostAndUsage",
            lambda kwargs=kwargs: cost_explorer.get_cost_and_usage(**kwargs),
            default={},
        )
        if not resp:
            break
        for period in resp.get("ResultsByTime", []):
            for group in period.get("Groups", []):
                service = (group.get("Keys") or [""])[0]
                if service and _is_euc_service(service) and service not in names:
                    names.append(service)
        next_token = resp.get("NextPageToken")
        if not next_token:
            break
    return names


def get_euc_cost_summary_core(
    factory: ClientFactory,
    lookback_days: CostLookbackDays = 30,
    granularity: str = "MONTHLY",
    start_date: DateString | None = None,
    end_date: DateString | None = None,
    split_workspaces: bool = True,
) -> CostSummary:
    errors: list[ServiceError] = []
    # Cost Explorer is a global endpoint served from us-east-1, regardless of working region.
    cost_explorer = factory.client(consts.COST_EXPLORER_API, region=consts.COST_EXPLORER_REGION)

    if start_date and end_date:
        start, end = start_date, end_date
    else:
        end_d = datetime.now(UTC).date()
        start_d = end_d - timedelta(days=lookback_days)
        start, end = start_d.isoformat(), end_d.isoformat()

    # Group by SERVICE across ALL spend and select EUC services in code (see _is_euc_service).
    # A server-side exact-name SERVICE filter would silently drop any naming variant — the very
    # bug that hid AppStream / WorkSpaces Applications spend. Page through all results, keeping both
    # the overall per-service totals and the per-period (daily/monthly) breakdown for charts.
    totals_by_service: dict[str, float] = {}
    per_period: dict[tuple[str, str], dict[str, float]] = {}
    currency = "USD"
    next_token: str | None = None
    while True:
        kwargs: dict[str, Any] = {
            "TimePeriod": {"Start": start, "End": end},
            "Granularity": granularity,
            "Metrics": ["UnblendedCost"],
            "GroupBy": [{"Type": "DIMENSION", "Key": "SERVICE"}],
        }
        if next_token:
            kwargs["NextPageToken"] = next_token
        response = try_call(
            errors,
            "AWS Cost Explorer",
            "GetCostAndUsage",
            lambda kwargs=kwargs: cost_explorer.get_cost_and_usage(**kwargs),
            default={},
        )
        if not response:
            break
        for period in response.get("ResultsByTime", []):
            tp = period.get("TimePeriod", {})
            bucket = per_period.setdefault((tp.get("Start", ""), tp.get("End", "")), {})
            for group in period.get("Groups", []):
                service = (group.get("Keys") or ["Unknown"])[0]
                if not _is_euc_service(service):
                    continue
                metric = group.get("Metrics", {}).get("UnblendedCost", {})
                amount = float(metric.get("Amount", 0.0))
                currency = metric.get("Unit", currency)
                totals_by_service[service] = totals_by_service.get(service, 0.0) + amount
                bucket[service] = bucket.get(service, 0.0) + amount
        next_token = response.get("NextPageToken")
        if not next_token:
            break

    by_service = [
        CostLineItem(service=s, amount=round(a, 2))
        for s, a in sorted(totals_by_service.items(), key=lambda kv: kv[1], reverse=True)
    ]
    total = round(sum(item.amount for item in by_service), 2)

    # Split the single "Amazon WorkSpaces" line into Personal/Pools/Core via USAGE_TYPE.
    workspaces_breakdown: dict[str, float] = {}
    core_ws = [
        it.service for it in by_service if _is_core_workspaces_service(it.service) and it.amount
    ]
    if split_workspaces and core_ws:
        workspaces_breakdown = _fetch_workspaces_breakdown(
            cost_explorer, start, end, granularity, core_ws, errors
        )

    by_period = [
        CostPeriod(
            start=p_start,
            end=p_end,
            total=round(sum(svc.values()), 2),
            by_service=[
                CostLineItem(service=s, amount=round(a, 2))
                for s, a in sorted(svc.items(), key=lambda kv: kv[1], reverse=True)
            ],
        )
        for (p_start, p_end), svc in sorted(per_period.items())
    ]

    return CostSummary(
        start=start,
        end=end,
        granularity=granularity,
        currency=currency,
        total=total,
        by_service=by_service,
        workspaces_breakdown=workspaces_breakdown,
        by_period=by_period,
        errors=errors,
        notes=[
            "EUC services are selected by matching the Cost Explorer SERVICE name against the EUC "
            "keyword set (workspaces / appstream), so naming variants are not dropped.",
            "Cost Explorer bills WorkSpaces Personal, Pools, and Core under one 'Amazon "
            "WorkSpaces' SERVICE line; workspaces_breakdown splits them via USAGE_TYPE (Personal "
            "vs Pools vs Core) — a heuristic on usage-type names, so treat sub-totals as "
            "estimates.",
            "by_period gives the per-bucket time series (per day for DAILY, per month for MONTHLY) "
            "for charts/trends; by_service is the total across the whole window.",
            "On MONTHLY granularity, AlwaysOn monthly bundle charges post on the 1st of the month, "
            "so day-1 of a DAILY series legitimately spikes — it is not a Pools/Core artefact.",
        ],
    )


def _daily_run_rate(
    cost_explorer: Any, services: list[str], errors: list[ServiceError]
) -> tuple[float | None, float | None]:
    """(last-7-day, trailing-30-day) average daily EUC spend, for forecast sanity context."""
    end_d = datetime.now(UTC).date()
    start_d = end_d - timedelta(days=30)
    resp = try_call(
        errors,
        "AWS Cost Explorer",
        "GetCostAndUsage",
        lambda: cost_explorer.get_cost_and_usage(
            TimePeriod={"Start": start_d.isoformat(), "End": end_d.isoformat()},
            Granularity="DAILY",
            Metrics=["UnblendedCost"],
            Filter={"Dimensions": {"Key": "SERVICE", "Values": services}},
        ),
        default={},
    )
    daily = [
        float(p.get("Total", {}).get("UnblendedCost", {}).get("Amount", 0) or 0)
        for p in (resp or {}).get("ResultsByTime", [])
    ]
    if not daily:
        return None, None
    recent = daily[-7:]
    return round(sum(recent) / len(recent), 2), round(sum(daily) / len(daily), 2)


def get_euc_cost_forecast_core(
    factory: ClientFactory,
    days_ahead: int = 30,
    granularity: str = "MONTHLY",
) -> CostForecast:
    """Forecast EUC spend using Cost Explorer's GetCostForecast, filtered to EUC services."""
    errors: list[ServiceError] = []
    cost_explorer = factory.client(consts.COST_EXPLORER_API, region=consts.COST_EXPLORER_REGION)

    # Forecast windows must start in the future; tomorrow avoids same-day boundary rejections.
    start_d = datetime.now(UTC).date() + timedelta(days=1)
    end_d = start_d + timedelta(days=max(1, days_ahead))
    start, end = start_d.isoformat(), end_d.isoformat()

    # GetCostForecast needs a filter with exact SERVICE names; discover them from recent actuals
    # (keyword-matched) so naming variants are never guessed wrong.
    services = _discover_euc_service_names(cost_explorer, errors)
    if not services:
        return CostForecast(
            start=start,
            end=end,
            granularity=granularity,
            errors=errors,
            notes=[
                "No EUC spend found in the last 30 days, so there is no history to forecast from."
            ],
        )

    resp = try_call(
        errors,
        "AWS Cost Explorer",
        "GetCostForecast",
        lambda: cost_explorer.get_cost_forecast(
            TimePeriod={"Start": start, "End": end},
            Metric="UNBLENDED_COST",
            Granularity=granularity,
            Filter={"Dimensions": {"Key": "SERVICE", "Values": services}},
            PredictionIntervalLevel=80,
        ),
        default={},
    )

    recent_avg, trailing_avg = _daily_run_rate(cost_explorer, services, errors)
    notes = [
        "Forecast is Cost Explorer's model (80% prediction interval) over the discovered EUC "
        "services; it needs sufficient usage history and reflects current usage patterns.",
    ]
    if recent_avg is not None and trailing_avg and recent_avg > trailing_avg * 1.3:
        pct = (recent_avg / trailing_avg - 1) * 100
        notes.append(
            f"Recent run-rate is elevated: the last 7 days averaged ${recent_avg:,.2f}/day vs "
            f"${trailing_avg:,.2f}/day over the trailing 30 days (+{pct:.0f}%). The forecast "
            "extrapolates current usage, so transient resources (e.g. RUNNING image builders or "
            "temporarily busy fleets) can inflate it — re-check after stopping them."
        )

    total = (resp or {}).get("Total", {})
    by_period = [
        ForecastPeriod(
            start=p.get("TimePeriod", {}).get("Start", ""),
            end=p.get("TimePeriod", {}).get("End", ""),
            mean=round(float(p.get("MeanValue", 0.0)), 2),
            lower=round(float(p["PredictionIntervalLowerBound"]), 2)
            if p.get("PredictionIntervalLowerBound")
            else None,
            upper=round(float(p["PredictionIntervalUpperBound"]), 2)
            if p.get("PredictionIntervalUpperBound")
            else None,
        )
        for p in (resp or {}).get("ForecastResultsByTime", [])
    ]
    return CostForecast(
        start=start,
        end=end,
        granularity=granularity,
        currency=total.get("Unit", "USD"),
        forecast_total=round(float(total["Amount"]), 2) if total.get("Amount") else None,
        by_period=by_period,
        filtered_services=services,
        recent_7d_daily_avg=recent_avg,
        trailing_30d_daily_avg=trailing_avg,
        errors=errors,
        notes=notes,
    )


def _fetch_service_usage_totals(
    cost_explorer: Any,
    start: str,
    end: str,
    service_names: list[str],
    errors: list[ServiceError],
) -> dict[tuple[str, str], float]:
    """(service, usage_type) -> amount over a window, via a two-dimension grouping."""
    out: dict[tuple[str, str], float] = {}
    next_token: str | None = None
    while True:
        kwargs: dict[str, Any] = {
            "TimePeriod": {"Start": start, "End": end},
            "Granularity": "MONTHLY",
            "Metrics": ["UnblendedCost"],
            "Filter": {"Dimensions": {"Key": "SERVICE", "Values": service_names}},
            "GroupBy": [
                {"Type": "DIMENSION", "Key": "SERVICE"},
                {"Type": "DIMENSION", "Key": "USAGE_TYPE"},
            ],
        }
        if next_token:
            kwargs["NextPageToken"] = next_token
        resp = try_call(
            errors,
            "AWS Cost Explorer",
            "GetCostAndUsage",
            lambda kwargs=kwargs: cost_explorer.get_cost_and_usage(**kwargs),
            default={},
        )
        if not resp:
            break
        for period in resp.get("ResultsByTime", []):
            for group in period.get("Groups", []):
                keys = group.get("Keys") or ["", ""]
                service, usage_type = (keys + ["", ""])[:2]
                amount = float(group.get("Metrics", {}).get("UnblendedCost", {}).get("Amount", 0.0))
                out[(service, usage_type)] = out.get((service, usage_type), 0.0) + amount
        next_token = resp.get("NextPageToken")
        if not next_token:
            break
    return out


def _driver_category(service: str, usage_type: str) -> str:
    """Human bucket for a driver: WorkSpaces splits into Personal/Pools/Core, others keep name."""
    if _is_core_workspaces_service(service):
        return _classify_workspaces_usage_type(usage_type)
    return service


def compare_euc_costs_core(
    factory: ClientFactory,
    start_date: str | None = None,
    end_date: str | None = None,
    baseline_start: str | None = None,
    baseline_end: str | None = None,
    top_n: int = 10,
) -> CostComparison:
    """Compare two windows of EUC spend and rank the usage-type drivers of the change."""
    errors: list[ServiceError] = []
    cost_explorer = factory.client(consts.COST_EXPLORER_API, region=consts.COST_EXPLORER_REGION)

    if start_date and end_date:
        c_start_d, c_end_d = date.fromisoformat(start_date), date.fromisoformat(end_date)
    else:
        c_end_d = datetime.now(UTC).date()
        c_start_d = c_end_d - timedelta(days=30)
    if baseline_start and baseline_end:
        b_start_d, b_end_d = date.fromisoformat(baseline_start), date.fromisoformat(baseline_end)
    else:
        length = max(1, (c_end_d - c_start_d).days)
        b_end_d = c_start_d
        b_start_d = b_end_d - timedelta(days=length)

    def _window(w_start: str, w_end: str):
        w_errors: list[ServiceError] = []
        summary = get_euc_cost_summary_core(
            factory,
            granularity="MONTHLY",
            start_date=w_start,
            end_date=w_end,
            split_workspaces=False,
        )
        w_errors.extend(summary.errors)
        services = [li.service for li in summary.by_service if li.amount]
        usage = (
            _fetch_service_usage_totals(cost_explorer, w_start, w_end, services, w_errors)
            if services
            else {}
        )
        return summary, usage, w_errors

    (b_summary, b_usage, b_errors), (c_summary, c_usage, c_errors) = gather_concurrently(
        lambda: _window(b_start_d.isoformat(), b_end_d.isoformat()),
        lambda: _window(c_start_d.isoformat(), c_end_d.isoformat()),
    )
    errors.extend(b_errors)
    errors.extend(c_errors)

    b_by_service = {li.service: li.amount for li in b_summary.by_service}
    c_by_service = {li.service: li.amount for li in c_summary.by_service}
    by_service_delta = {
        svc: {
            "baseline": round(b_by_service.get(svc, 0.0), 2),
            "comparison": round(c_by_service.get(svc, 0.0), 2),
            "delta": round(c_by_service.get(svc, 0.0) - b_by_service.get(svc, 0.0), 2),
        }
        for svc in sorted(set(b_by_service) | set(c_by_service))
    }

    drivers = [
        CostDriver(
            usage_type=usage_type,
            category=_driver_category(service, usage_type),
            baseline=round(b_usage.get((service, usage_type), 0.0), 2),
            comparison=round(c_usage.get((service, usage_type), 0.0), 2),
            delta=round(
                c_usage.get((service, usage_type), 0.0) - b_usage.get((service, usage_type), 0.0),
                2,
            ),
        )
        for service, usage_type in set(b_usage) | set(c_usage)
    ]
    drivers = [d for d in drivers if abs(d.delta) >= 0.01]
    drivers.sort(key=lambda d: abs(d.delta), reverse=True)

    delta = round(c_summary.total - b_summary.total, 2)
    return CostComparison(
        baseline_start=b_start_d.isoformat(),
        baseline_end=b_end_d.isoformat(),
        comparison_start=c_start_d.isoformat(),
        comparison_end=c_end_d.isoformat(),
        currency=c_summary.currency,
        baseline_total=b_summary.total,
        comparison_total=c_summary.total,
        delta=delta,
        delta_pct=round(delta / b_summary.total * 100, 1) if b_summary.total else None,
        by_service_delta=by_service_delta,
        top_drivers=drivers[: max(1, top_n)],
        errors=errors,
        notes=[
            "Windows use Cost Explorer's exclusive end date. When only start/end are given, the "
            "baseline defaults to the preceding window of equal length.",
            "Drivers are usage-type-level changes; WorkSpaces usage types are bucketed into "
            "Personal / Pools / Core, other services keep their service name.",
        ],
    )


def register(mcp: Any, factory: ClientFactory) -> None:
    """Register cost & utilization tools on the FastMCP app."""

    async def analyze_workspace_utilization(
        region: str | None = None, lookback_days: LookbackDays = 14
    ) -> dict[str, Any]:
        """Classify WorkSpaces Personal desktops as unused / idle / active BY USER CONNECTIONS.

        Uses the AWS/WorkSpaces UserConnected metric over the window to count active days per
        desktop, returning per-desktop classifications and a rollup. Read-only.

        IMPORTANT: classifications reflect user connections over the lookback window, NOT power
        state — an AVAILABLE (running) desktop with no logons is "unused", not stopped. For
        "which WorkSpaces are running/powered on?" use get_euc_inventory_summary or
        generate_inventory_report (lifecycle State) instead.

        Args:
            region: AWS region. Defaults to the server's configured region.
            lookback_days: Window for the usage analysis (default 14).
        """
        report = analyze_workspace_utilization_core(
            factory, region or factory.region, lookback_days
        )
        return report.model_dump()

    async def recommend_running_mode(
        region: str | None = None, lookback_days: LookbackDays = 14
    ) -> dict[str, Any]:
        """Recommend AlwaysOn -> AutoStop running-mode changes for under-used desktops.

        Identifies AlwaysOn WorkSpaces Personal desktops with low usage that would typically cost
        less on AutoStop (and flags the reverse, low-confidence, for daily-used AutoStop desktops).
        Read-only.

        Args:
            region: AWS region. Defaults to the server's configured region.
            lookback_days: Window for the usage analysis (default 14).
        """
        report = recommend_running_mode_core(factory, region or factory.region, lookback_days)
        return report.model_dump()

    async def get_euc_cost_summary(
        lookback_days: CostLookbackDays = 30,
        granularity: Literal["MONTHLY", "DAILY"] = "MONTHLY",
        start_date: DateString | None = None,
        end_date: DateString | None = None,
        split_workspaces: bool = True,
    ) -> dict[str, Any]:
        """Summarize EUC spend by service over a window (account-wide via Cost Explorer).

        Returns unblended cost grouped by service for the EUC portfolio (WorkSpaces, including
        Personal/Pools/Core which Cost Explorer bills together as "Amazon WorkSpaces"; WorkSpaces
        Applications/AppStream; and Secure Browser). Services are matched by keyword, so naming
        variants are never dropped. Cost Explorer is not region-scoped, so figures are account-wide.
        Read-only.

        Returns `by_service` (totals per service), `by_period` (a per-bucket time series — one entry
        per day for DAILY, per month for MONTHLY — for charts), and `workspaces_breakdown`: the
        single "Amazon WorkSpaces" line split into **Personal / Pools / Core** via USAGE_TYPE
        (which the SERVICE dimension cannot do). Use this when you need to know whether a WorkSpaces
        figure is Personal-only or includes Pools/Core.

        Note: on MONTHLY granularity, AlwaysOn monthly bundle charges post on the 1st, so day-1 of a
        DAILY series spikes legitimately (not a Pools/Core artefact).

        For a specific calendar month, pass start_date/end_date instead of lookback_days — Cost
        Explorer's end is EXCLUSIVE, so for May 2026 use start_date="2026-05-01",
        end_date="2026-06-01".

        Args:
            lookback_days: How far back to total when start_date/end_date are omitted (default 30).
            granularity: Cost Explorer granularity: MONTHLY or DAILY (default MONTHLY).
            start_date: Optional inclusive start, "YYYY-MM-DD". Use with end_date; overrides
                lookback_days.
            end_date: Optional EXCLUSIVE end, "YYYY-MM-DD". Use with start_date.
            split_workspaces: Split the WorkSpaces line into Personal/Pools/Core (default True).
        """
        summary = get_euc_cost_summary_core(
            factory, lookback_days, granularity, start_date, end_date, split_workspaces
        )
        return summary.model_dump()

    async def get_euc_cost_forecast(
        days_ahead: ForecastDays = 30,
        granularity: Literal["MONTHLY", "DAILY"] = "MONTHLY",
    ) -> dict[str, Any]:
        """Forecast upcoming EUC spend (account-wide, via Cost Explorer's forecasting model).

        Answers "what will my WorkSpaces/EUC bill be?" — returns the mean forecast total for the
        window plus per-period values with an 80% prediction interval. The forecast is filtered to
        the EUC services discovered in the last 30 days of actual spend, so naming variants are
        never guessed. Needs sufficient usage history; a data-unavailable error is reported in the
        payload rather than raised. Read-only.

        Args:
            days_ahead: How far ahead to forecast, starting tomorrow (default 30).
            granularity: MONTHLY or DAILY forecast buckets (default MONTHLY).
        """
        forecast = await asyncio.to_thread(
            get_euc_cost_forecast_core, factory, days_ahead, granularity
        )
        return forecast.model_dump()

    async def compare_euc_costs(
        start_date: DateString | None = None,
        end_date: DateString | None = None,
        baseline_start: DateString | None = None,
        baseline_end: DateString | None = None,
        top_n: int = 10,
    ) -> dict[str, Any]:
        """Compare two windows of EUC spend and explain WHY the cost changed.

        Answers "why is this month higher than last month?" — returns totals for both windows, the
        delta (absolute and %), per-service deltas, and the top usage-type-level drivers of the
        change (WorkSpaces usage types bucketed into Personal / Pools / Core). Account-wide via
        Cost Explorer. Read-only.

        Defaults: with no dates, compares the last 30 days against the 30 days before. With only
        start_date/end_date, the baseline is the preceding window of equal length. End dates are
        EXCLUSIVE (for May vs June 2026: start_date="2026-06-01", end_date="2026-07-01").

        Args:
            start_date: Comparison window start (YYYY-MM-DD, inclusive).
            end_date: Comparison window end (YYYY-MM-DD, exclusive).
            baseline_start: Optional explicit baseline start; defaults to the preceding window.
            baseline_end: Optional explicit baseline end (exclusive).
            top_n: How many drivers to return (default 10).
        """
        comparison = await asyncio.to_thread(
            compare_euc_costs_core,
            factory,
            start_date,
            end_date,
            baseline_start,
            baseline_end,
            top_n,
        )
        return comparison.model_dump()

    mcp.add_tool(
        analyze_workspace_utilization, annotations=read_only("Analyze WorkSpace utilization")
    )
    mcp.add_tool(recommend_running_mode, annotations=read_only("Recommend running mode"))
    mcp.add_tool(get_euc_cost_summary, annotations=read_only("EUC cost summary"))
    mcp.add_tool(get_euc_cost_forecast, annotations=read_only("EUC cost forecast"))
    mcp.add_tool(compare_euc_costs, annotations=read_only("Compare EUC costs"))
