# Copyright bengroeneveldsg. Licensed under the Apache License, Version 2.0 (the "License").
# You may not use this file except in compliance with the License.
# A copy of the License is located at http://www.apache.org/licenses/LICENSE-2.0
"""Pydantic models for tool inputs and synthesized outputs."""

from __future__ import annotations

from pydantic import BaseModel, Field


class ServiceInventory(BaseModel):
    """Per-service rollup within an inventory summary."""

    service: str = Field(description="Current official AWS product name.")
    resource_type: str = Field(description="The kind of resource counted (e.g. WorkSpace, Fleet).")
    count: int = Field(description="Number of resources of this type found in the region.")
    by_state: dict[str, int] = Field(
        default_factory=dict,
        description="Breakdown of resources by their lifecycle state.",
    )


class ServiceError(BaseModel):
    """A non-fatal error encountered while calling one AWS service.

    Collection/diagnosis is best-effort: a permission gap or unsupported region for one service is
    recorded here rather than failing the whole result.
    """

    service: str
    operation: str = Field(description="The AWS API operation that failed.")
    message: str


# Backwards-compatible alias (the inventory tool predates the generic name).
InventoryError = ServiceError


class EucInventorySummary(BaseModel):
    """Cross-service inventory of the Amazon WorkSpaces EUC portfolio in one region."""

    region: str | None = Field(description="AWS region the summary covers.")
    account_scope: str = Field(default="single-account")
    total_resources: int = Field(description="Sum of counts across all services collected.")
    services: list[ServiceInventory] = Field(default_factory=list)
    errors: list[ServiceError] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class Finding(BaseModel):
    """A single observation from a diagnostic or audit tool."""

    severity: str = Field(description="One of: info, warning, critical.")
    title: str = Field(description="Short human-readable headline.")
    detail: str = Field(description="What was observed and why it matters.")
    recommendation: str | None = Field(default=None, description="Suggested next action, if any.")
    resource_id: str | None = Field(
        default=None, description="Resource the finding applies to, when relevant."
    )


class Diagnosis(BaseModel):
    """Synthesized diagnosis for a single EUC resource."""

    target_type: str = Field(description="Kind of resource diagnosed (official product name).")
    target_id: str = Field(description="Identifier of the resource diagnosed.")
    region: str | None = None
    status: str = Field(
        description="Overall verdict: healthy, degraded, unhealthy, unknown, or not_found."
    )
    summary: str = Field(description="One-line plain-language verdict.")
    signals: dict[str, object] = Field(
        default_factory=dict,
        description="Key raw signals observed (state, capacity, connection status, etc.).",
    )
    findings: list[Finding] = Field(default_factory=list)
    errors: list[ServiceError] = Field(default_factory=list)


class DirectoryHealthReport(BaseModel):
    """Health of one or more WorkSpaces-registered directories in a region."""

    region: str | None = None
    directories: list[Diagnosis] = Field(default_factory=list)
    errors: list[ServiceError] = Field(default_factory=list)


class MetricStat(BaseModel):
    """Aggregated values for one CloudWatch metric over the window."""

    latest: float | None = None
    average: float | None = None
    peak: float | None = None
    unit: str


class WorkspacePerformance(BaseModel):
    """Native AWS/WorkSpaces performance metrics for one desktop."""

    workspace_id: str
    lookback_hours: int
    metrics: dict[str, MetricStat] = Field(default_factory=dict)
    note: str | None = None


class PerformanceReport(BaseModel):
    """Performance metrics across one or more WorkSpaces Personal desktops."""

    region: str | None = None
    lookback_hours: int
    workspaces: list[WorkspacePerformance] = Field(default_factory=list)
    errors: list[ServiceError] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class UsagePoint(BaseModel):
    """One time-bucket in a usage time-series."""

    timestamp: str
    average: float | None = None
    peak: float | None = None


class FleetMetricSeries(BaseModel):
    """Aggregates plus the per-bucket time-series for one fleet metric."""

    unit: str
    latest: float | None = None
    average: float | None = None
    peak: float | None = None
    series: list[UsagePoint] = Field(default_factory=list)


class FleetUsage(BaseModel):
    """Usage history for a WorkSpaces Applications fleet over a window."""

    fleet_name: str
    lookback_days: int
    period_hours: int
    metrics: dict[str, FleetMetricSeries] = Field(default_factory=dict)
    summary: str | None = None
    errors: list[ServiceError] = Field(default_factory=list)


class SecureBrowserPortalDetails(BaseModel):
    """Resolved settings for a WorkSpaces Secure Browser portal."""

    portal_arn: str
    display_name: str | None = None
    authentication_type: str | None = None
    status: str | None = None
    user_settings: dict[str, object] = Field(
        default_factory=dict,
        description="Clipboard/print/download/upload controls and timeouts.",
    )
    network: dict[str, object] = Field(default_factory=dict)
    has_browser_policy: bool = False
    has_data_protection: bool = False
    errors: list[ServiceError] = Field(default_factory=list)


class UsageHistory(BaseModel):
    """Generic metric time-series history for a single EUC resource over a window."""

    target_type: str = Field(description="Resource kind, e.g. the current official product name.")
    target_id: str
    lookback_days: int
    period_hours: int
    metrics: dict[str, FleetMetricSeries] = Field(default_factory=dict)
    summary: str | None = None
    errors: list[ServiceError] = Field(default_factory=list)


class WorkspaceUtilization(BaseModel):
    """Utilization classification for a single WorkSpaces Personal desktop."""

    workspace_id: str
    running_mode: str | None = None
    lookback_days: int
    active_days: int | None = Field(
        default=None, description="Days with at least one user connection in the window."
    )
    classification: str = Field(description="unused, idle, active, or unknown.")
    # Inputs for pricing/savings estimates (not always populated).
    compute_type: str | None = None
    operating_system: str | None = None
    root_volume_gib: int | None = None
    user_volume_gib: int | None = None


class UtilizationReport(BaseModel):
    """Utilization rollup across WorkSpaces Personal desktops in a region."""

    region: str | None = None
    lookback_days: int
    total: int
    counts: dict[str, int] = Field(
        default_factory=dict, description="Count of desktops per classification."
    )
    workspaces: list[WorkspaceUtilization] = Field(default_factory=list)
    errors: list[ServiceError] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class Recommendation(BaseModel):
    """A single cost/utilization optimization recommendation."""

    target_id: str
    kind: str = Field(description="The recommendation type, e.g. running_mode.")
    current: str | None = None
    recommended: str | None = None
    rationale: str
    estimated_monthly_savings_usd: float | None = Field(
        default=None, description="Estimated saving, when it can be computed; otherwise null."
    )
    confidence: str = Field(description="low, medium, or high.")


class RecommendationReport(BaseModel):
    """Set of optimization recommendations for a region."""

    region: str | None = None
    lookback_days: int
    recommendations: list[Recommendation] = Field(default_factory=list)
    errors: list[ServiceError] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class CostLineItem(BaseModel):
    service: str
    amount: float


class CostPeriod(BaseModel):
    """One time bucket (a day or a month, per the requested granularity)."""

    start: str
    end: str
    total: float
    by_service: list[CostLineItem] = Field(default_factory=list)


class CostSummary(BaseModel):
    """Cost rollup for the EUC portfolio over a time window (account-wide)."""

    scope: str = Field(
        default="account-wide",
        description="Cost Explorer is not region-scoped; figures are account-wide.",
    )
    start: str
    end: str
    granularity: str
    currency: str = "USD"
    total: float
    by_service: list[CostLineItem] = Field(default_factory=list)
    by_period: list[CostPeriod] = Field(
        default_factory=list,
        description=(
            "Per-bucket time series (one entry per day for DAILY, per month for MONTHLY). "
            "Use this for charts / trend analysis."
        ),
    )
    errors: list[ServiceError] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class ResourceRecord(BaseModel):
    """A single resource row in an inventory report."""

    id: str
    name: str | None = None
    state: str | None = None
    attributes: dict[str, object] = Field(default_factory=dict)


class InventoryReportSection(BaseModel):
    service: str
    resource_type: str
    resources: list[ResourceRecord] = Field(default_factory=list)


class InventoryReport(BaseModel):
    """Detailed per-resource inventory across the EUC portfolio in a region."""

    region: str | None = None
    total_resources: int = 0
    sections: list[InventoryReportSection] = Field(default_factory=list)
    errors: list[ServiceError] = Field(default_factory=list)


class AuditReport(BaseModel):
    """Security-posture findings across the EUC portfolio in a region."""

    region: str | None = None
    findings: list[Finding] = Field(default_factory=list)
    severity_counts: dict[str, int] = Field(default_factory=dict)
    resources_checked: dict[str, int] = Field(default_factory=dict)
    errors: list[ServiceError] = Field(default_factory=list)


class UnusedResource(BaseModel):
    service: str
    resource_type: str
    id: str
    reason: str


class UnusedResourcesReport(BaseModel):
    """Candidate idle/unused resources worth reclaiming."""

    region: str | None = None
    lookback_days: int
    items: list[UnusedResource] = Field(default_factory=list)
    errors: list[ServiceError] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class TargetResult(BaseModel):
    """Outcome of a write action against a single target."""

    target_id: str
    status: str = Field(description="ok, error, or skipped.")
    message: str | None = None


class WriteOutcome(BaseModel):
    """Result of a guarded write/lifecycle action.

    Mutations are dry-run by default: unless ``confirmed`` is true the action only reports the plan
    and changes nothing. Bulk actions are refused when the target count exceeds the blast-radius
    cap.
    """

    action: str = Field(description="The lifecycle action requested.")
    dry_run: bool = Field(description="True when nothing was changed (plan only).")
    confirmed: bool = Field(description="Whether the caller explicitly confirmed execution.")
    requested_targets: list[str] = Field(default_factory=list)
    max_bulk_targets: int = Field(description="Configured blast-radius cap.")
    blast_radius_ok: bool = Field(
        description="False when the action was refused for being too large."
    )
    plan: str = Field(description="Human-readable description of what would happen / happened.")
    acknowledgement_required: str | None = Field(
        default=None,
        description="For destructive actions: the exact phrase the caller must pass to proceed.",
    )
    results: list[TargetResult] = Field(default_factory=list)
    errors: list[ServiceError] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class GovernanceFinding(BaseModel):
    """A governance audit observation (target + severity + human-readable issue)."""

    target: str
    severity: str = Field(description='"info" or "warning".')
    issue: str


class ApplicationImageFinding(BaseModel):
    """An audit observation about a WorkSpaces Applications image or image builder."""

    target: str
    severity: str = Field(description='"info" or "warning".')
    issue: str


class AuditEvent(BaseModel):
    """One EUC management event from CloudTrail."""

    time: str
    service: str
    event_name: str
    username: str | None = None
    source_ip: str | None = None
    aws_region: str | None = None
    resources: list[str] = Field(default_factory=list)
    error_code: str | None = None
    read_only: bool = False


class AuditTrailReport(BaseModel):
    """CloudTrail-derived audit of recent EUC management activity (account-wide, 90-day max)."""

    region: str | None = None
    lookback_days: int = 7
    include_read_only: bool = False
    total_events: int = 0
    by_event_name: dict[str, int] = Field(default_factory=dict)
    by_user: dict[str, int] = Field(default_factory=dict)
    events: list[AuditEvent] = Field(default_factory=list)
    findings: list[GovernanceFinding] = Field(default_factory=list)
    errors: list[ServiceError] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class QuotaItem(BaseModel):
    service: str
    quota_name: str
    quota_code: str
    limit: float
    adjustable: bool = False
    usage: float | None = None
    utilization_pct: float | None = None


class ServiceQuotaReport(BaseModel):
    """Service Quotas limits (and usage headroom where AWS publishes a usage metric) for EUC."""

    region: str | None = None
    approaching_pct: float = 80.0
    quotas: list[QuotaItem] = Field(default_factory=list)
    findings: list[GovernanceFinding] = Field(default_factory=list)
    errors: list[ServiceError] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class ApplicationImageInfo(BaseModel):
    name: str
    visibility: str | None = None
    platform: str | None = None
    state: str | None = None
    agent_version: str | None = None
    application_count: int = 0
    applications: list[str] = Field(default_factory=list)
    error_count: int = 0
    created: str | None = None
    base_image_released: str | None = None
    base_image_age_days: int | None = None
    allow_fleet: bool | None = None
    allow_image_builder: bool | None = None


class ApplicationImageBuilderInfo(BaseModel):
    name: str
    state: str | None = None
    platform: str | None = None
    instance_type: str | None = None
    agent_version: str | None = None
    created: str | None = None


class ApplicationImageAuditReport(BaseModel):
    """Audit of WorkSpaces Applications (AppStream 2.0) images and image builders."""

    region: str | None = None
    image_count: int = 0
    image_builder_count: int = 0
    running_image_builders: int = 0
    images: list[ApplicationImageInfo] = Field(default_factory=list)
    image_builders: list[ApplicationImageBuilderInfo] = Field(default_factory=list)
    findings: list[ApplicationImageFinding] = Field(default_factory=list)
    errors: list[ServiceError] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
