# Copyright bengroeneveldsg. Licensed under the Apache License, Version 2.0 (the "License").
# You may not use this file except in compliance with the License.
# A copy of the License is located at http://www.apache.org/licenses/LICENSE-2.0
"""Governance tools (read-only, IAM Tier 0): audit trail + service-quota headroom.

- ``get_euc_audit_trail`` reads the always-on CloudTrail management-event history (LookupEvents,
  90-day window — no trail to configure) and reports recent EUC activity. By default it returns
  only **mutations** (ReadOnly=false), so the signal is "who created / modified / terminated what",
  not a log dump. Destructive actions and errors (e.g. AccessDenied) are flagged.
- ``get_euc_service_quotas`` lists Service Quotas limits for each EUC service and, where AWS
  publishes a linked usage metric (AWS/Usage ResourceCount), pairs it with current usage to compute
  **headroom** and flag quotas approaching their limit.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from .. import consts
from ..clients import ClientFactory
from ..models import (
    AuditEvent,
    AuditTrailReport,
    GovernanceFinding,
    QuotaItem,
    ServiceError,
    ServiceQuotaReport,
)
from ._common import LookbackDays, MaxEvents, Percentage, paginate, read_only, try_call

_CLOUDTRAIL_MAX_LOOKBACK = 90  # LookupEvents only retains 90 days.
_SWEEP_PAGE_BUDGET = 12  # account-wide sweep: up to 12 pages (600 events) before giving up
_LOOKUP_PACING_SECONDS = 0.51  # LookupEvents is throttled at 2 requests/second per region


# --------------------------------------------------------------------------- audit trail


def _parse_event(raw: dict[str, Any]) -> AuditEvent:
    source = str(raw.get("EventSource") or "")
    detail: dict[str, Any] = {}
    try:
        detail = json.loads(raw.get("CloudTrailEvent", "{}"))
    except (ValueError, TypeError):
        detail = {}
    resources = [
        r.get("ResourceName", "") for r in raw.get("Resources", []) if r.get("ResourceName")
    ]
    event_time = raw.get("EventTime")
    return AuditEvent(
        time=event_time.isoformat() if isinstance(event_time, datetime) else str(event_time),
        service=consts.EUC_AUDIT_SOURCES.get(source, source),
        event_name=raw.get("EventName", ""),
        username=raw.get("Username"),
        source_ip=detail.get("sourceIPAddress"),
        aws_region=detail.get("awsRegion"),
        resources=resources,
        error_code=detail.get("errorCode"),
        read_only=bool(raw.get("ReadOnly")),
    )


def _audit_findings(events: list[AuditEvent]) -> list[GovernanceFinding]:
    findings: list[GovernanceFinding] = []
    for ev in events:
        if ev.event_name.startswith(consts.AUDIT_DESTRUCTIVE_PREFIXES):
            findings.append(
                GovernanceFinding(
                    target=ev.event_name,
                    severity="warning",
                    issue=(
                        f"Destructive/high-impact action {ev.event_name} on {ev.service} by "
                        f"{ev.username or 'unknown'} at {ev.time}"
                        + (f" ({', '.join(ev.resources)})" if ev.resources else "")
                    ),
                )
            )
        if ev.error_code:
            sev = "warning" if "denied" in ev.error_code.lower() else "info"
            findings.append(
                GovernanceFinding(
                    target=ev.event_name,
                    severity=sev,
                    issue=(
                        f"{ev.event_name} on {ev.service} by {ev.username or 'unknown'} "
                        f"failed with {ev.error_code}"
                    ),
                )
            )
    return findings


def _event_sort_key(raw: dict[str, Any]) -> str:
    t = raw.get("EventTime")
    return t.isoformat() if isinstance(t, datetime) else str(t or "")


def _lookup_paged(
    trail: Any,
    attr: dict[str, str],
    start: datetime,
    end: datetime,
    sources: set[str],
    stop_after_euc: int,
    page_budget: int,
    errors: list[ServiceError],
) -> tuple[list[dict[str, Any]], datetime | None, bool]:
    """One paged LookupEvents scan for a single attribute (its single-attribute limit).

    Returns (matching raw events, oldest event time SEEN pre-filter, window fully covered).
    Pages are paced because LookupEvents throttles at 2 requests/second account-wide.
    """
    matched: list[dict[str, Any]] = []
    oldest: datetime | None = None
    covered = False
    page_token: str | None = None
    for page in range(page_budget):
        if page:
            time.sleep(_LOOKUP_PACING_SECONDS)
        kwargs: dict[str, Any] = {
            "LookupAttributes": [attr],
            "StartTime": start,
            "EndTime": end,
            "MaxResults": 50,
        }
        if page_token:
            kwargs["NextToken"] = page_token
        resp = try_call(
            errors,
            "AWS CloudTrail",
            "LookupEvents",
            lambda kwargs=kwargs: trail.lookup_events(**kwargs),
            default={},
        )
        if not resp:
            break
        for e in resp.get("Events", []):
            t = e.get("EventTime")
            if isinstance(t, datetime) and (oldest is None or t < oldest):
                oldest = t
            if e.get("EventSource", "") in sources:
                matched.append(e)
        page_token = resp.get("NextToken")
        if not page_token:
            covered = True  # CloudTrail exhausted the window — nothing older exists.
            break
        if len(matched) >= stop_after_euc:
            break
    return matched, oldest, covered


def get_euc_audit_trail_core(
    factory: ClientFactory,
    region: str | None,
    lookback_days: LookbackDays = 7,
    service: str = "all",
    include_read_only: bool = False,
    max_events: MaxEvents = 100,
) -> AuditTrailReport:
    errors: list[ServiceError] = []
    lookback_days = max(1, min(lookback_days, _CLOUDTRAIL_MAX_LOOKBACK))
    sources = set(
        consts.EUC_AUDIT_SERVICE_FILTER.get(service, consts.EUC_AUDIT_SERVICE_FILTER["all"])
    )
    trail = factory.client(consts.CLOUDTRAIL_API, region=region)
    end = datetime.now(UTC)
    start = end - timedelta(days=lookback_days)

    raw_events: list[dict[str, Any]] = []
    oldest_scanned: datetime | None = None
    fully_covered = True
    notes: list[str] = [
        "Source: CloudTrail LookupEvents — always-on management-event history, last 90 days "
        "max; no trail required. For longer retention use CloudTrail Lake or an S3 trail.",
    ]

    if include_read_only:
        # One sweep per EventSource (LookupEvents allows a single attribute per call).
        for s in sorted(sources):
            attr = {"AttributeKey": "EventSource", "AttributeValue": s}
            matched, oldest, covered = _lookup_paged(
                trail, attr, start, end, sources, max_events, _SWEEP_PAGE_BUDGET, errors
            )
            raw_events.extend(matched)
            fully_covered = fully_covered and covered
            if oldest is not None and (oldest_scanned is None or oldest < oldest_scanned):
                oldest_scanned = oldest
        notes.append(
            "include_read_only sweeps can be flooded by automated Describe/List noise (e.g. "
            "auto-scaling polls every ~40s), which limits how far back the page budget reaches."
        )
    else:
        # Mutations: an account-wide ReadOnly=false sweep first. In noisy accounts (constant
        # SSM/EC2 writes) this alone can cover only minutes — so for a single-service query the
        # curated mutation event names are ALSO looked up directly; each EventName lookup spans
        # the full window regardless of account noise.
        attr = {"AttributeKey": "ReadOnly", "AttributeValue": "false"}
        raw_events, oldest_scanned, fully_covered = _lookup_paged(
            trail, attr, start, end, sources, max_events, _SWEEP_PAGE_BUDGET, errors
        )
        notes.append(
            "By default only mutating events are returned (ReadOnly=false); pass "
            "include_read_only=true to also include Describe/List calls."
        )
        targeted_names = consts.EUC_AUDIT_MUTATION_EVENT_NAMES.get(service, [])
        if targeted_names:
            for name in targeted_names:
                time.sleep(_LOOKUP_PACING_SECONDS)
                name_attr = {"AttributeKey": "EventName", "AttributeValue": name}
                matched, _, _ = _lookup_paged(
                    trail, name_attr, start, end, sources, max_events, 3, errors
                )
                raw_events.extend(matched)
            notes.append(
                f"{len(targeted_names)} curated {service} mutation event names (Create/Update/"
                "Delete/Start/Stop of fleets, stacks, desktops, portals, builders, ...) were "
                "additionally looked up across the FULL window, so those changes are captured "
                "even where the account-wide sweep fell short."
            )
        elif service == "all" and not fully_covered:
            notes.append(
                "Re-run with a specific service (e.g. service='applications') to additionally "
                "scan that service's curated mutation event names across the full window."
            )

    # Merge sweep + targeted results, dropping duplicates seen by both query paths.
    seen_ids: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for e in raw_events:
        eid = str(e.get("EventId") or _event_sort_key(e) + e.get("EventName", ""))
        if eid in seen_ids:
            continue
        seen_ids.add(eid)
        deduped.append(e)
    deduped.sort(key=_event_sort_key, reverse=True)

    if not fully_covered:
        reached = oldest_scanned.isoformat() if oldest_scanned else "an unknown point"
        notes.append(
            f"PARTIAL WINDOW COVERAGE: the account-wide scan reached back only to {reached}, "
            f"not the full {lookback_days} days (page budget; account event volume). Do NOT "
            "conclude 'no changes in the window' from the scan alone — only the curated "
            "event-name lookups above (if run) cover the full window."
        )

    events = [_parse_event(e) for e in deduped][:max_events]

    by_event_name: dict[str, int] = {}
    by_user: dict[str, int] = {}
    for ev in events:
        by_event_name[ev.event_name] = by_event_name.get(ev.event_name, 0) + 1
        by_user[ev.username or "unknown"] = by_user.get(ev.username or "unknown", 0) + 1

    return AuditTrailReport(
        region=region,
        lookback_days=lookback_days,
        include_read_only=include_read_only,
        window_fully_covered=fully_covered,
        scanned_back_to=oldest_scanned.isoformat() if oldest_scanned else None,
        total_events=len(events),
        by_event_name=dict(sorted(by_event_name.items(), key=lambda kv: kv[1], reverse=True)),
        by_user=dict(sorted(by_user.items(), key=lambda kv: kv[1], reverse=True)),
        events=events,
        findings=_audit_findings(events),
        errors=errors,
        notes=notes,
    )


# --------------------------------------------------------------------------- service quotas


def _usage_query(index: int, usage_metric: dict[str, Any]) -> dict[str, Any] | None:
    dims = usage_metric.get("MetricDimensions") or {}
    if not dims:
        return None
    return {
        "Id": f"u{index}",
        "MetricStat": {
            "Metric": {
                "Namespace": usage_metric.get("MetricNamespace", "AWS/Usage"),
                "MetricName": usage_metric.get("MetricName", "ResourceCount"),
                "Dimensions": [{"Name": k, "Value": str(v)} for k, v in dims.items()],
            },
            "Period": 86400,
            "Stat": usage_metric.get("MetricStatisticRecommendation", "Maximum"),
        },
        "ReturnData": True,
    }


def _fetch_usage(
    factory: ClientFactory, region: str | None, queries: list[dict[str, Any]], errors: list
) -> dict[str, float]:
    """Batch-fetch current usage for quota usage metrics; returns {query_id: latest_value}."""
    if not queries:
        return {}
    cloudwatch = factory.client(consts.CLOUDWATCH_API, region=region)
    end = datetime.now(UTC)
    start = end - timedelta(days=2)
    out: dict[str, float] = {}
    # get_metric_data accepts up to 500 queries per call; chunk to be safe.
    for i in range(0, len(queries), 500):
        chunk = queries[i : i + 500]
        resp = try_call(
            errors,
            "Amazon CloudWatch",
            "GetMetricData",
            lambda chunk=chunk: cloudwatch.get_metric_data(
                MetricDataQueries=chunk, StartTime=start, EndTime=end
            ),
            default={},
        )
        for result in (resp or {}).get("MetricDataResults", []):
            values = result.get("Values") or []
            if values:
                out[result["Id"]] = float(values[0])
    return out


def get_euc_service_quotas_core(
    factory: ClientFactory,
    region: str | None,
    service: str = "all",
    approaching_pct: Percentage = 80.0,
    include_zero_limit: bool = False,
) -> ServiceQuotaReport:
    errors: list[ServiceError] = []
    codes = consts.EUC_QUOTA_SERVICE_FILTER.get(service, consts.EUC_QUOTA_SERVICE_FILTER["all"])
    sq = factory.client(consts.SERVICE_QUOTAS_API, region=region)

    items: list[QuotaItem] = []
    usage_queries: list[dict[str, Any]] = []
    query_target: dict[str, int] = {}  # query id -> index in items

    for code in codes:
        label = consts.EUC_QUOTA_SERVICE_CODES.get(code, code)
        quotas = try_call(
            errors,
            "AWS Service Quotas",
            "ListServiceQuotas",
            lambda code=code: paginate(sq.list_service_quotas, "Quotas", ServiceCode=code),
            default=[],
        )
        for q in quotas or []:
            limit = float(q.get("Value", 0.0))
            if not include_zero_limit and limit <= 0:
                continue
            item = QuotaItem(
                service=label,
                quota_name=q.get("QuotaName", ""),
                quota_code=q.get("QuotaCode", ""),
                limit=limit,
                adjustable=bool(q.get("Adjustable")),
            )
            query = _usage_query(len(items), q.get("UsageMetric") or {})
            if query:
                query_target[query["Id"]] = len(items)
                usage_queries.append(query)
            items.append(item)

    usage = _fetch_usage(factory, region, usage_queries, errors)
    for qid, idx in query_target.items():
        if qid in usage:
            items[idx].usage = round(usage[qid], 2)
            if items[idx].limit > 0:
                items[idx].utilization_pct = round(usage[qid] / items[idx].limit * 100, 1)

    findings: list[GovernanceFinding] = []
    for it in items:
        if it.utilization_pct is not None and it.utilization_pct >= approaching_pct:
            findings.append(
                GovernanceFinding(
                    target=f"{it.service}: {it.quota_name}",
                    severity="warning",
                    issue=(
                        f"At {it.utilization_pct}% of limit ({it.usage:g}/{it.limit:g})"
                        + ("" if it.adjustable else "; this quota is NOT adjustable")
                        + "."
                    ),
                )
            )

    items.sort(key=lambda i: (i.utilization_pct is None, -(i.utilization_pct or 0)))
    return ServiceQuotaReport(
        region=region,
        approaching_pct=approaching_pct,
        quotas=items,
        findings=findings,
        errors=errors,
        notes=[
            "Limits come from Service Quotas (account-applied values). Usage/headroom is shown "
            "only where AWS publishes a linked usage metric (AWS/Usage ResourceCount) — WorkSpaces "
            "and Secure Browser do; most WorkSpaces Applications per-instance-type quotas do not.",
            "Zero-limit quotas (e.g. unused instance types) are hidden unless "
            "include_zero_limit=true.",
        ],
    )


# --------------------------------------------------------------------------- account posture


def get_euc_account_posture_core(factory: ClientFactory, region: str | None) -> dict[str, Any]:
    """Account-level WorkSpaces configuration: tenancy, client properties, connection aliases."""
    errors: list[ServiceError] = []
    workspaces = factory.client(consts.WORKSPACES_API, region=region)

    account = try_call(
        errors,
        "Amazon WorkSpaces",
        "DescribeAccount",
        lambda: workspaces.describe_account(),
        default={},
    )

    modifications = (
        try_call(
            errors,
            "Amazon WorkSpaces",
            "DescribeAccountModifications",
            lambda: paginate(workspaces.describe_account_modifications, "AccountModifications"),
            default=[],
        )
        or []
    )

    directories = (
        try_call(
            errors,
            "Amazon WorkSpaces",
            "DescribeWorkspaceDirectories",
            lambda: paginate(workspaces.describe_workspace_directories, "Directories"),
            default=[],
        )
        or []
    )
    directory_ids = [d.get("DirectoryId", "") for d in directories if d.get("DirectoryId")]
    client_properties: dict[str, Any] = {}
    if directory_ids:
        props = (
            try_call(
                errors,
                "Amazon WorkSpaces",
                "DescribeClientProperties",
                lambda: workspaces.describe_client_properties(ResourceIds=directory_ids).get(
                    "ClientPropertiesList", []
                ),
                default=[],
            )
            or []
        )
        for entry in props:
            client_properties[entry.get("ResourceId", "")] = entry.get("ClientProperties", {})

    aliases = (
        try_call(
            errors,
            "Amazon WorkSpaces",
            "DescribeConnectionAliases",
            lambda: paginate(workspaces.describe_connection_aliases, "ConnectionAliases"),
            default=[],
        )
        or []
    )

    recent_mods = [
        {
            "state": m.get("ModificationState"),
            "tenancy": m.get("DedicatedTenancySupport"),
            "started": str(m.get("StartTime")) if m.get("StartTime") else None,
            "error": m.get("ErrorMessage"),
        }
        for m in modifications[:5]
    ]
    return {
        "region": region,
        "dedicated_tenancy_support": account.get("DedicatedTenancySupport"),
        "dedicated_tenancy_management_cidr": account.get("DedicatedTenancyManagementCidrRange"),
        "recent_account_modifications": recent_mods,
        "client_properties_by_directory": client_properties,
        "connection_aliases": [
            {
                "alias_id": a.get("AliasId"),
                "connection_string": a.get("ConnectionString"),
                "state": a.get("State"),
            }
            for a in aliases
        ],
        "errors": [e.model_dump() for e in errors],
        "notes": [
            "Dedicated tenancy (BYOL) requires account enablement; connection aliases support "
            "cross-region redirection; client properties control the WorkSpaces client "
            "experience (e.g. reconnect) per directory.",
        ],
    }


# --------------------------------------------------------------------------- registration


def register(mcp: Any, factory: ClientFactory) -> None:
    """Register the governance (audit trail + service quotas) tools on the FastMCP app."""

    async def get_euc_audit_trail(
        region: str | None = None,
        lookback_days: LookbackDays = 7,
        service: Literal["all", "workspaces", "applications", "secure-browser", "core"] = "all",
        include_read_only: bool = False,
        max_events: MaxEvents = 100,
    ) -> dict[str, Any]:
        """Audit recent EUC management activity from CloudTrail — "who changed what".

        Reads the always-on CloudTrail event history (last 90 days max; no trail required). By
        default returns only mutating events (creates/modifies/terminates) across WorkSpaces
        Personal/Pools/Core, WorkSpaces Applications, Secure Browser, and Core Managed Instances,
        with destructive actions and errors (e.g. AccessDenied) flagged. Read-only.

        IMPORTANT: when the user asks about ONE service (e.g. "recent AppStream changes"), pass
        that service — a single-service query additionally scans ~2 dozen curated mutation event
        names across the FULL window, immune to account noise. Check window_fully_covered in the
        response: when false, the account-wide scan stopped early (scanned_back_to says where) and
        an empty result does NOT mean nothing changed.

        Args:
            region: AWS region. Defaults to the server's configured region.
            lookback_days: Window in days (1-90, default 7).
            service: Limit to one service group, or "all". Prefer a specific service when the
                question is about one — it enables the full-window curated event-name scan.
            include_read_only: Also include Describe/List calls (default False = mutations only).
            max_events: Maximum events to return (default 100).
        """
        report = await asyncio.to_thread(
            get_euc_audit_trail_core,
            factory,
            region or factory.region,
            lookback_days,
            service,
            include_read_only,
            max_events,
        )
        return report.model_dump()

    async def get_euc_service_quotas(
        region: str | None = None,
        service: Literal["all", "workspaces", "applications", "secure-browser", "core"] = "all",
        approaching_pct: Percentage = 80.0,
        include_zero_limit: bool = False,
    ) -> dict[str, Any]:
        """Report EUC Service Quotas limits and usage headroom (capacity planning).

        Lists each EUC service's quotas (limit + adjustability) and, where AWS publishes a linked
        usage metric, the current usage and utilisation %, flagging any quota at/above the
        approaching threshold. WorkSpaces and Secure Browser expose usage; most WorkSpaces
        Applications per-instance-type quotas are limit-only. Read-only.

        Args:
            region: AWS region. Defaults to the server's configured region.
            service: Limit to one service group, or "all".
            approaching_pct: Utilisation %% at/above which a quota is flagged (default 80).
            include_zero_limit: Include quotas with a zero limit (default False).
        """
        report = get_euc_service_quotas_core(
            factory, region or factory.region, service, approaching_pct, include_zero_limit
        )
        return report.model_dump()

    async def get_euc_account_posture(region: str | None = None) -> dict[str, Any]:
        """Report account-level WorkSpaces configuration posture.

        Returns dedicated-tenancy (BYOL) status and management CIDR, recent account modifications,
        per-directory client properties (e.g. reconnect), and cross-region connection aliases.
        Read-only.

        Args:
            region: AWS region. Defaults to the server's configured region.
        """
        return await asyncio.to_thread(
            get_euc_account_posture_core, factory, region or factory.region
        )

    mcp.add_tool(get_euc_audit_trail, annotations=read_only("EUC audit trail (CloudTrail)"))
    mcp.add_tool(get_euc_account_posture, annotations=read_only("EUC account posture"))
    mcp.add_tool(get_euc_service_quotas, annotations=read_only("EUC service quotas / headroom"))
