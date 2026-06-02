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

from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from .. import consts
from ..clients import ClientFactory
from ..models import (
    CostLineItem,
    CostSummary,
    Recommendation,
    RecommendationReport,
    ServiceError,
    UtilizationReport,
    WorkspaceUtilization,
)
from . import pricing
from ._common import paginate, read_only, try_call


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
    factory: ClientFactory, region: str | None, lookback_days: int = 14
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
    factory: ClientFactory, region: str | None, lookback_days: int = 14
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
    return any(token in name for token in consts.EUC_COST_EXPLORER_SERVICE_TOKENS)


def get_euc_cost_summary_core(
    factory: ClientFactory,
    lookback_days: int = 30,
    granularity: str = "MONTHLY",
    start_date: str | None = None,
    end_date: str | None = None,
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
    # bug that hid AppStream / WorkSpaces Applications spend. Page through all results.
    totals_by_service: dict[str, float] = {}
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
            for group in period.get("Groups", []):
                service = (group.get("Keys") or ["Unknown"])[0]
                if not _is_euc_service(service):
                    continue
                metric = group.get("Metrics", {}).get("UnblendedCost", {})
                amount = float(metric.get("Amount", 0.0))
                currency = metric.get("Unit", currency)
                totals_by_service[service] = totals_by_service.get(service, 0.0) + amount
        next_token = response.get("NextPageToken")
        if not next_token:
            break

    by_service = [
        CostLineItem(service=s, amount=round(a, 2))
        for s, a in sorted(totals_by_service.items(), key=lambda kv: kv[1], reverse=True)
    ]
    total = round(sum(item.amount for item in by_service), 2)

    return CostSummary(
        start=start,
        end=end,
        granularity=granularity,
        currency=currency,
        total=total,
        by_service=by_service,
        errors=errors,
        notes=[
            "EUC services are selected by matching the Cost Explorer SERVICE name against the EUC "
            "keyword set (workspaces / appstream), so naming variants are not dropped.",
            "Cost Explorer bills WorkSpaces Personal, Pools, and Core together under the single "
            "'Amazon WorkSpaces' service; they cannot be separated via the SERVICE dimension.",
        ],
    )


def register(mcp: Any, factory: ClientFactory) -> None:
    """Register cost & utilization tools on the FastMCP app."""

    async def analyze_workspace_utilization(
        region: str | None = None, lookback_days: int = 14
    ) -> dict[str, Any]:
        """Classify WorkSpaces Personal desktops as unused / idle / active.

        Uses the AWS/WorkSpaces UserConnected metric over the window to count active days per
        desktop, returning per-desktop classifications and a rollup. Read-only.

        Args:
            region: AWS region. Defaults to the server's configured region.
            lookback_days: Window for the usage analysis (default 14).
        """
        report = analyze_workspace_utilization_core(
            factory, region or factory.region, lookback_days
        )
        return report.model_dump()

    async def recommend_running_mode(
        region: str | None = None, lookback_days: int = 14
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
        lookback_days: int = 30,
        granularity: Literal["MONTHLY", "DAILY"] = "MONTHLY",
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> dict[str, Any]:
        """Summarize EUC spend by service over a window (account-wide via Cost Explorer).

        Returns unblended cost grouped by service for the EUC portfolio (WorkSpaces, including
        Personal/Pools/Core which Cost Explorer bills together as "Amazon WorkSpaces"; WorkSpaces
        Applications/AppStream; and Secure Browser). Services are matched by keyword, so naming
        variants are never dropped. Cost Explorer is not region-scoped, so figures are account-wide.
        Read-only.

        For a specific calendar month, pass start_date/end_date instead of lookback_days — Cost
        Explorer's end is EXCLUSIVE, so for May 2026 use start_date="2026-05-01",
        end_date="2026-06-01".

        Args:
            lookback_days: How far back to total when start_date/end_date are omitted (default 30).
            granularity: Cost Explorer granularity: MONTHLY or DAILY (default MONTHLY).
            start_date: Optional inclusive start, "YYYY-MM-DD". Use with end_date; overrides
                lookback_days.
            end_date: Optional EXCLUSIVE end, "YYYY-MM-DD". Use with start_date.
        """
        summary = get_euc_cost_summary_core(
            factory, lookback_days, granularity, start_date, end_date
        )
        return summary.model_dump()

    mcp.add_tool(
        analyze_workspace_utilization, annotations=read_only("Analyze WorkSpace utilization")
    )
    mcp.add_tool(recommend_running_mode, annotations=read_only("Recommend running mode"))
    mcp.add_tool(get_euc_cost_summary, annotations=read_only("EUC cost summary"))
