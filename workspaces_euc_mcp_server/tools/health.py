# Copyright bengroeneveldsg. Licensed under the Apache License, Version 2.0 (the "License").
# You may not use this file except in compliance with the License.
# A copy of the License is located at http://www.apache.org/licenses/LICENSE-2.0
"""One-call EUC health report (read-only; needs Tier 1 for the cost sections).

``generate_euc_health_report`` orchestrates the inventory, alarm, cost, utilization, security,
image, and quota tools concurrently and returns BOTH structured data and a ready-to-send
``markdown`` report — the building block for scheduled/emailed estate reports. Every section is
size-guarded so the report stays digestible even on large estates; the underlying tools remain
available for full detail.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

from ..clients import ClientFactory
from ..models import ServiceError
from ._common import LookbackDays, gather_concurrently, read_only
from .cost import get_euc_cost_forecast_core, get_euc_cost_summary_core, recommend_running_mode_core
from .diagnostics import get_euc_active_alarms_core
from .governance import get_euc_service_quotas_core
from .images import audit_application_images_core, audit_workspace_images_core
from .inventory import collect_inventory
from .reporting import audit_security_posture_core

_MAX_FINDINGS = 25
_MAX_RECOMMENDATIONS = 10
_MAX_QUOTAS = 8


class HealthReport(BaseModel):
    """Structured estate health report plus a ready-to-send markdown rendering."""

    region: str | None = None
    generated_at: str
    lookback_days: int
    total_resources: int = 0
    alarms_firing: int = 0
    audit_warnings: int = 0
    monthly_spend: float | None = None
    forecast_next_30d: float | None = None
    estimated_monthly_savings: float | None = None
    inventory: dict[str, Any] = Field(default_factory=dict)
    cost: dict[str, Any] = Field(default_factory=dict)
    security: dict[str, Any] = Field(default_factory=dict)
    utilization: dict[str, Any] = Field(default_factory=dict)
    images: dict[str, Any] = Field(default_factory=dict)
    quotas: dict[str, Any] = Field(default_factory=dict)
    alarms: list[dict[str, Any]] = Field(default_factory=list)
    markdown: str = Field(description="Ready-to-send report (email/wiki/PDF source).")
    errors: list[ServiceError] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


def _money(value: float | None) -> str:
    return f"${value:,.2f}" if value is not None else "n/a"


def generate_euc_health_report_core(
    factory: ClientFactory, region: str | None, lookback_days: LookbackDays = 14
) -> HealthReport:
    """Collect all report sections concurrently and synthesize one health report."""
    (
        inventory,
        alarms,
        cost_summary,
        forecast,
        recommendations,
        audit,
        app_images,
        ws_images,
        quotas,
    ) = gather_concurrently(
        lambda: collect_inventory(factory, region),
        lambda: get_euc_active_alarms_core(factory, region),
        lambda: get_euc_cost_summary_core(factory, lookback_days=30),
        lambda: get_euc_cost_forecast_core(factory, days_ahead=30),
        lambda: recommend_running_mode_core(factory, region, lookback_days),
        lambda: audit_security_posture_core(factory, region),
        lambda: audit_application_images_core(factory, region),
        lambda: audit_workspace_images_core(factory, region),
        lambda: get_euc_service_quotas_core(factory, region),
    )

    errors: list[ServiceError] = [
        *inventory.errors,
        *alarms.errors,
        *cost_summary.errors,
        *forecast.errors,
        *recommendations.errors,
        *audit.errors,
        *app_images.errors,
        *ws_images.errors,
        *quotas.errors,
    ]

    real_alarms = [a for a in alarms.alarms if not a.likely_autoscaling]
    warnings = [f for f in audit.findings if f.severity in ("warning", "critical")]
    autostop_candidates = [
        r for r in recommendations.recommendations if r.recommended == "AUTO_STOP"
    ]
    savings = [r for r in autostop_candidates if r.estimated_monthly_savings_usd is not None]
    total_savings = round(sum(r.estimated_monthly_savings_usd or 0 for r in savings), 2) or None
    hot_quotas = [q for q in quotas.quotas if (q.utilization_pct or 0) >= 50][:_MAX_QUOTAS]
    image_findings = [*app_images.findings, *ws_images.findings]
    running_builders = app_images.running_image_builders + app_images.running_app_block_builders

    generated_at = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")

    # ------------------------------------------------------------------ markdown
    lines: list[str] = [
        f"# EUC Health Report — {region or 'default region'} — {generated_at}",
        "",
        "## Executive summary",
        f"- **{inventory.total_resources} resources** across "
        f"{len(inventory.services)} service categories",
        f"- **Alarms firing:** {len(real_alarms)}"
        + (
            f" ({len(alarms.alarms) - len(real_alarms)} auto-scaling alarms excluded as expected)"
            if len(alarms.alarms) != len(real_alarms)
            else ""
        ),
        f"- **Security findings:** {len(warnings)} warning(s) "
        f"({audit.severity_counts.get('info', 0)} informational)",
        f"- **Spend (last 30d):** {_money(cost_summary.total)}; "
        f"**forecast (next 30d):** {_money(forecast.forecast_total)}",
        f"- **AlwaysOn→AutoStop candidates:** {len(autostop_candidates)}"
        + (
            f" (~{_money(total_savings)}/mo estimated on the {len(savings)} with matchable pricing)"
            if total_savings
            else " (no Price List match for these bundles, so no $ estimate)"
            if autostop_candidates
            else ""
        ),
        f"- **Builders running (billing hourly):** {running_builders}",
        "",
        "## Inventory",
        "| Service | Type | Count | States |",
        "|---|---|---|---|",
    ]
    for svc in inventory.services:
        states = ", ".join(f"{k}: {v}" for k, v in sorted(svc.by_state.items())) or "-"
        lines.append(f"| {svc.service} | {svc.resource_type} | {svc.count} | {states} |")

    lines += ["", "## Cost", "| Service | Last 30 days |", "|---|---|"]
    for item in cost_summary.by_service:
        lines.append(f"| {item.service} | {_money(item.amount)} |")
    if cost_summary.workspaces_breakdown:
        lines.append("")
        lines.append(
            "WorkSpaces split: "
            + "; ".join(f"{k} {_money(v)}" for k, v in cost_summary.workspaces_breakdown.items())
        )
    if autostop_candidates:
        lines += ["", "### Savings opportunities (AlwaysOn → AutoStop)"]
        for r in autostop_candidates[:_MAX_RECOMMENDATIONS]:
            est = (
                f"save ~{_money(r.estimated_monthly_savings_usd)}/mo"
                if r.estimated_monthly_savings_usd is not None
                else "savings not priceable for this bundle"
            )
            lines.append(f"- `{r.target_id}` — {est} ({r.confidence} confidence)")

    lines += ["", "## Utilization (WorkSpaces Personal)"]
    lines.append(
        f"- {len(recommendations.recommendations)} running-mode recommendation(s) over the last "
        f"{lookback_days} day(s)"
    )

    if real_alarms:
        lines += ["", "## Alarms firing now"]
        for a in real_alarms:
            lines.append(f"- **{a.name}** [{a.service}] {a.metric_name} — {a.state_reason}")

    lines += ["", f"## Security posture ({len(warnings)} warning(s))"]
    for f in warnings[:_MAX_FINDINGS]:
        rid = f" (`{f.resource_id}`)" if f.resource_id else ""
        lines.append(f"- **{f.title}**{rid}")
    if len(warnings) > _MAX_FINDINGS:
        lines.append(f"- … and {len(warnings) - _MAX_FINDINGS} more (run audit_security_posture)")

    if image_findings:
        lines += ["", f"## Images & builders ({len(image_findings)} finding(s))"]
        for f in image_findings[:_MAX_FINDINGS]:
            lines.append(f"- {f.target}: {f.issue}")

    if hot_quotas:
        lines += [
            "",
            "## Service quotas ≥50% utilised",
            "| Quota | Usage | Limit | % |",
            "|---|---|---|---|",
        ]
        for q in hot_quotas:
            lines.append(
                f"| {q.service}: {q.quota_name} | {q.usage} | {q.limit:g} | {q.utilization_pct}% |"
            )

    markdown = "\n".join(lines)

    return HealthReport(
        region=region,
        generated_at=generated_at,
        lookback_days=lookback_days,
        total_resources=inventory.total_resources,
        alarms_firing=len(real_alarms),
        audit_warnings=len(warnings),
        monthly_spend=cost_summary.total,
        forecast_next_30d=forecast.forecast_total,
        estimated_monthly_savings=total_savings,
        inventory={
            "total": inventory.total_resources,
            "services": [s.model_dump() for s in inventory.services],
        },
        cost={
            "last_30d_total": cost_summary.total,
            "by_service": [i.model_dump() for i in cost_summary.by_service],
            "workspaces_breakdown": cost_summary.workspaces_breakdown,
            "forecast_next_30d": forecast.forecast_total,
        },
        security={
            "severity_counts": audit.severity_counts,
            "top_warnings": [f.model_dump() for f in warnings[:_MAX_FINDINGS]],
        },
        utilization={
            "recommendations": [
                r.model_dump() for r in recommendations.recommendations[:_MAX_RECOMMENDATIONS]
            ],
            "estimated_monthly_savings": total_savings,
        },
        images={
            "findings": [f.model_dump() for f in image_findings[:_MAX_FINDINGS]],
            "running_builders": running_builders,
        },
        quotas={"at_or_above_50pct": [q.model_dump() for q in hot_quotas]},
        alarms=[a.model_dump() for a in real_alarms],
        markdown=markdown,
        errors=errors,
        notes=[
            "Sections are size-guarded (top-N) so the report stays digestible; use the individual "
            "tools (audit_security_posture, generate_inventory_report, export_inventory_report_csv "
            "…) for full detail on large estates.",
            "Cost sections need Tier 1 IAM; without it they appear in errors and the rest of the "
            "report still renders.",
        ],
    )


def register(mcp: Any, factory: ClientFactory) -> None:
    """Register the health-report tool on the FastMCP app."""

    async def generate_euc_health_report(
        region: str | None = None, lookback_days: LookbackDays = 14
    ) -> dict[str, Any]:
        """Generate a full EUC estate health report in ONE call (inventory, cost, security, …).

        Orchestrates the inventory, active-alarm, cost (30d + forecast + savings), utilization,
        security-posture, image-audit, and quota tools concurrently, returning structured sections
        plus a ready-to-send `markdown` report — ideal for scheduled/weekly estate reports or
        emailing a summary. Sections are size-guarded (top-N) for large estates; use the individual
        tools for full detail. Cost sections need Tier 1 IAM. Read-only.

        Args:
            region: AWS region. Defaults to the server's configured region.
            lookback_days: Window for utilization/savings analysis (default 14).
        """
        report = await asyncio.to_thread(
            generate_euc_health_report_core, factory, region or factory.region, lookback_days
        )
        return report.model_dump()

    mcp.add_tool(generate_euc_health_report, annotations=read_only("EUC health report"))
