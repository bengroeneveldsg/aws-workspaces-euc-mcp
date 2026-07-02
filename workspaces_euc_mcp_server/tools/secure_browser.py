# Copyright bengroeneveldsg. Licensed under the Apache License, Version 2.0 (the "License").
# You may not use this file except in compliance with the License.
# A copy of the License is located at http://www.apache.org/licenses/LICENSE-2.0
"""WorkSpaces Secure Browser (formerly WorkSpaces Web) tools (read-only, IAM Tier 0).

Brings Secure Browser to parity with the other services:
- ``get_secure_browser_portal_details`` resolves a portal's user/browser/network/data-protection
  settings — the clipboard/print/download data-egress controls that matter for security. Available
  and validatable now.
- ``get_secure_browser_portal_usage`` returns session metrics from AWS/WorkSpacesWeb. Unlike the
  WorkSpaces capacity metrics, Secure Browser only emits these when sessions occur, so it is empty
  for idle portals; the metric names follow AWS docs and are best-effort until there is activity.
"""

from __future__ import annotations

from typing import Any

from .. import consts
from ..clients import ClientFactory
from ..models import (
    SecureBrowserPortalDetails,
    SecureBrowserPortalUsage,
    SecureBrowserSession,
    ServiceError,
)
from ._common import LookbackDays, PeriodHours, paginate, read_only, try_call
from .performance import _fetch_metric_series


def get_secure_browser_portal_details_core(
    factory: ClientFactory, portal_arn: str, region: str | None
) -> SecureBrowserPortalDetails:
    errors: list[ServiceError] = []
    web = factory.client(consts.SECURE_BROWSER_API, region=region)

    portal = try_call(
        errors,
        consts.PRODUCT_SECURE_BROWSER,
        "GetPortal",
        lambda: web.get_portal(portalArn=portal_arn).get("portal", {}),
        default={},
    )

    user_settings: dict[str, object] = {}
    if portal.get("userSettingsArn"):
        us = try_call(
            errors,
            consts.PRODUCT_SECURE_BROWSER,
            "GetUserSettings",
            lambda: web.get_user_settings(userSettingsArn=portal["userSettingsArn"]).get(
                "userSettings", {}
            ),
            default={},
        )
        # Keep the policy-relevant flags; drop bulky/identifying fields.
        for key in (
            "copyAllowed",
            "pasteAllowed",
            "downloadAllowed",
            "uploadAllowed",
            "printAllowed",
            "deepLinkAllowed",
            "webAuthnAllowed",
            "disconnectTimeoutInMinutes",
            "idleDisconnectTimeoutInMinutes",
        ):
            if key in (us or {}):
                user_settings[key] = us[key]

    network: dict[str, object] = {}
    if portal.get("networkSettingsArn"):
        ns = try_call(
            errors,
            consts.PRODUCT_SECURE_BROWSER,
            "GetNetworkSettings",
            lambda: web.get_network_settings(networkSettingsArn=portal["networkSettingsArn"]).get(
                "networkSettings", {}
            ),
            default={},
        )
        for key in ("vpcId", "subnetIds", "securityGroupIds"):
            if key in (ns or {}):
                network[key] = ns[key]

    data_protection: dict[str, object] = {}
    if portal.get("dataProtectionSettingsArn"):
        dps = try_call(
            errors,
            consts.PRODUCT_SECURE_BROWSER,
            "GetDataProtectionSettings",
            lambda: web.get_data_protection_settings(
                dataProtectionSettingsArn=portal["dataProtectionSettingsArn"]
            ).get("dataProtectionSettings", {}),
            default={},
        )
        data_protection = _summarize_data_protection(dps or {})

    return SecureBrowserPortalDetails(
        portal_arn=portal_arn,
        display_name=portal.get("displayName"),
        authentication_type=portal.get("authenticationType"),
        status=portal.get("portalStatus"),
        user_settings=user_settings,
        network=network,
        has_browser_policy=bool(portal.get("browserSettingsArn")),
        has_data_protection=bool(portal.get("dataProtectionSettingsArn")),
        data_protection=data_protection,
        errors=errors,
    )


def _summarize_data_protection(dps: dict[str, Any]) -> dict[str, Any]:
    """Reduce a data-protection settings object to the policy-relevant redaction configuration."""
    inline: dict[str, Any] = dps.get("inlineRedactionConfiguration") or {}
    patterns: list[dict[str, Any]] = inline.get("inlineRedactionPatterns") or []
    builtin: list[str] = []
    custom: list[dict[str, Any]] = []
    for p in patterns:
        if p.get("builtInPatternId"):
            builtin.append(p["builtInPatternId"])
        elif p.get("customPattern"):
            cp = p["customPattern"]
            custom.append(
                {
                    "name": cp.get("patternName"),
                    "description": cp.get("patternDescription"),
                    "keyword_regex": cp.get("keywordRegex"),
                }
            )
    return {
        "display_name": dps.get("displayName"),
        "redacted_pattern_count": len(patterns),
        "builtin_patterns": builtin,
        "custom_patterns": custom,
        "global_confidence_level": inline.get("globalConfidenceLevel"),
        "global_enforced_urls": inline.get("globalEnforcedUrls"),
        "global_exempt_urls": inline.get("globalExemptUrls"),
    }


def _portal_id(portal: str) -> str:
    """Accept a portal ARN or id; the CloudWatch dimension uses the id (last ARN segment)."""
    return portal.rsplit("/", 1)[-1] if "/" in portal else portal


def _list_active_sessions(
    web: Any, portal_id: str, errors: list[ServiceError]
) -> list[SecureBrowserSession]:
    """Current Active sessions for a portal, live from ListSessions (what the console shows)."""
    raw = try_call(
        errors,
        consts.PRODUCT_SECURE_BROWSER,
        "ListSessions",
        lambda: paginate(
            web.list_sessions,
            "sessions",
            pagination_in="nextToken",
            pagination_out="nextToken",
            portalId=portal_id,
            status="Active",
        ),
        default=[],
    )
    sessions: list[SecureBrowserSession] = []
    for s in raw or []:
        start = s.get("startTime")
        sessions.append(
            SecureBrowserSession(
                session_id=s.get("sessionId", ""),
                username=s.get("username"),
                status=s.get("status"),
                start_time=start.isoformat() if hasattr(start, "isoformat") else None,
            )
        )
    return sessions


def get_secure_browser_portal_usage_core(
    factory: ClientFactory,
    portal: str,
    region: str | None,
    lookback_days: LookbackDays = 7,
    period_hours: PeriodHours = 24,
) -> SecureBrowserPortalUsage:
    errors: list[ServiceError] = []
    portal_id = _portal_id(portal)
    web = factory.client(consts.SECURE_BROWSER_API, region=region)

    # LIVE: current active sessions come from ListSessions (the real-time source the console uses),
    # NOT from CloudWatch.
    active = _list_active_sessions(web, portal_id, errors)

    # HISTORIC: CloudWatch (AWS/WorkSpacesWeb) over the window — only meaningful for past activity.
    cloudwatch = factory.client(consts.CLOUDWATCH_API, region=region)
    metrics = try_call(
        errors,
        "Amazon CloudWatch",
        "GetMetricData",
        lambda: _fetch_metric_series(
            cloudwatch,
            consts.SECURE_BROWSER_NAMESPACE,
            consts.SECURE_BROWSER_PORTAL_DIMENSION,
            portal_id,
            consts.SECURE_BROWSER_SESSION_METRICS,
            lookback_days,
            period_hours,
        ),
        default={},
    )

    hist = (
        f"historic metrics over {lookback_days}d available (see historic_metrics)"
        if metrics
        else f"no historic session metrics in the last {lookback_days}d (idle portals publish "
        "none to CloudWatch; enable the portal's Session Logger for detail)"
    )
    summary = (
        f"{len(active)} active session(s) right now (live via ListSessions). Historic: {hist}."
    )
    return SecureBrowserPortalUsage(
        portal=portal,
        active_session_count=len(active),
        active_sessions=active,
        lookback_days=lookback_days,
        period_hours=period_hours,
        historic_metrics=metrics or {},
        summary=summary,
        errors=errors,
    )


def register(mcp: Any, factory: ClientFactory) -> None:
    """Register Secure Browser tools on the FastMCP app."""

    async def get_secure_browser_portal_details(
        portal_arn: str, region: str | None = None
    ) -> dict[str, Any]:
        """Resolve a WorkSpaces Secure Browser portal's settings (security-relevant).

        Returns the portal's user settings (clipboard copy/paste, file download/upload, print
        controls + timeouts), network (VPC/subnets/security groups), whether a browser policy is
        attached, and — when data protection is configured — the resolved inline-redaction config
        (which built-in/custom patterns are redacted, global confidence, enforced/exempt URLs).
        Read-only.

        Args:
            portal_arn: The portal ARN (from get_euc_inventory_summary / generate_inventory_report).
            region: AWS region. Defaults to the server's configured region.
        """
        details = get_secure_browser_portal_details_core(
            factory, portal_arn, region or factory.region
        )
        return details.model_dump()

    async def get_secure_browser_portal_usage(
        portal: str,
        region: str | None = None,
        lookback_days: LookbackDays = 7,
        period_hours: PeriodHours = 24,
    ) -> dict[str, Any]:
        """Get a Secure Browser portal's CURRENT active sessions plus historic session metrics.

        Active sessions are retrieved **live** from ListSessions (the same source as the console's
        active-sessions view). Historic usage comes from CloudWatch (AWS/WorkSpacesWeb) over the
        window — CloudWatch is only meaningful for *past* activity, and idle portals publish none.
        Read-only.

        Args:
            portal: The portal id or ARN.
            region: AWS region. Defaults to the server's configured region.
            lookback_days: Window length (default 7).
            period_hours: Bucket size in hours (default 24).
        """
        usage = get_secure_browser_portal_usage_core(
            factory, portal, region or factory.region, lookback_days, period_hours
        )
        return usage.model_dump()

    mcp.add_tool(
        get_secure_browser_portal_details,
        annotations=read_only("Secure Browser portal details"),
    )
    mcp.add_tool(
        get_secure_browser_portal_usage, annotations=read_only("Secure Browser portal usage")
    )
