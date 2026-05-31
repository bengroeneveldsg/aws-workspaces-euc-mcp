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
from typing import Any

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
from ._common import paginate, try_call


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
        running_mode = ws.get("WorkspaceProperties", {}).get("RunningMode")
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
                running_mode=running_mode,
                lookback_days=lookback_days,
                active_days=active_days,
                classification=_classify(active_days, lookback_days, idle_threshold),
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
            "Savings are not estimated here because exact figures require the per-bundle AlwaysOn "
            "and AutoStop rates; this tool identifies candidates and direction only."
        ],
    )


def get_euc_cost_summary_core(
    factory: ClientFactory, lookback_days: int = 30, granularity: str = "MONTHLY"
) -> CostSummary:
    errors: list[ServiceError] = []
    # Cost Explorer is a global endpoint served from us-east-1, regardless of working region.
    cost_explorer = factory.client(consts.COST_EXPLORER_API, region=consts.COST_EXPLORER_REGION)

    end_date = datetime.now(UTC).date()
    start_date = end_date - timedelta(days=lookback_days)
    start, end = start_date.isoformat(), end_date.isoformat()

    response = try_call(
        errors,
        "AWS Cost Explorer",
        "GetCostAndUsage",
        lambda: cost_explorer.get_cost_and_usage(
            TimePeriod={"Start": start, "End": end},
            Granularity=granularity,
            Metrics=["UnblendedCost"],
            Filter={
                "Dimensions": {
                    "Key": "SERVICE",
                    "Values": consts.EUC_COST_EXPLORER_SERVICES,
                }
            },
            GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
        ),
        default={},
    )

    totals_by_service: dict[str, float] = {}
    currency = "USD"
    for period in (response or {}).get("ResultsByTime", []):
        for group in period.get("Groups", []):
            service = (group.get("Keys") or ["Unknown"])[0]
            metric = group.get("Metrics", {}).get("UnblendedCost", {})
            amount = float(metric.get("Amount", 0.0))
            currency = metric.get("Unit", currency)
            totals_by_service[service] = totals_by_service.get(service, 0.0) + amount

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
            "Filtered to EUC SERVICE dimension values; some products (e.g. Secure Browser) may "
            "bill under a different service name depending on the account."
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
        lookback_days: int = 30, granularity: str = "MONTHLY"
    ) -> dict[str, Any]:
        """Summarize EUC spend by service over a window (account-wide via Cost Explorer).

        Returns unblended cost grouped by service for the EUC portfolio. Cost Explorer is not
        region-scoped, so figures are account-wide. Read-only.

        Args:
            lookback_days: How far back to total (default 30).
            granularity: Cost Explorer granularity: MONTHLY or DAILY (default MONTHLY).
        """
        summary = get_euc_cost_summary_core(factory, lookback_days, granularity)
        return summary.model_dump()

    mcp.add_tool(analyze_workspace_utilization)
    mcp.add_tool(recommend_running_mode)
    mcp.add_tool(get_euc_cost_summary)
