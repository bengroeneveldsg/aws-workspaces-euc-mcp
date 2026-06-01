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
from ..models import SecureBrowserPortalDetails, ServiceError, UsageHistory
from ._common import try_call
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

    return SecureBrowserPortalDetails(
        portal_arn=portal_arn,
        display_name=portal.get("displayName"),
        authentication_type=portal.get("authenticationType"),
        status=portal.get("portalStatus"),
        user_settings=user_settings,
        network=network,
        has_browser_policy=bool(portal.get("browserSettingsArn")),
        has_data_protection=bool(portal.get("dataProtectionSettingsArn")),
        errors=errors,
    )


def _portal_id(portal: str) -> str:
    """Accept a portal ARN or id; the CloudWatch dimension uses the id (last ARN segment)."""
    return portal.rsplit("/", 1)[-1] if "/" in portal else portal


def get_secure_browser_portal_usage_core(
    factory: ClientFactory,
    portal: str,
    region: str | None,
    lookback_days: int = 7,
    period_hours: int = 24,
) -> UsageHistory:
    errors: list[ServiceError] = []
    cloudwatch = factory.client(consts.CLOUDWATCH_API, region=region)
    metrics = try_call(
        errors,
        "Amazon CloudWatch",
        "GetMetricData",
        lambda: _fetch_metric_series(
            cloudwatch,
            consts.SECURE_BROWSER_NAMESPACE,
            consts.SECURE_BROWSER_PORTAL_DIMENSION,
            _portal_id(portal),
            consts.SECURE_BROWSER_SESSION_METRICS,
            lookback_days,
            period_hours,
        ),
        default={},
    )
    summary = (
        "No session metrics in the window. Secure Browser only emits CloudWatch metrics when "
        "sessions occur (idle portals publish nothing); for detailed usage enable the portal's "
        "Session Logger."
        if not metrics
        else f"Session metrics over {lookback_days}d: see series."
    )
    return UsageHistory(
        target_type=consts.PRODUCT_SECURE_BROWSER,
        target_id=portal,
        lookback_days=lookback_days,
        period_hours=period_hours,
        metrics=metrics or {},
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
        controls + timeouts), network (VPC/subnets/security groups), and whether a browser policy
        and data-protection settings are attached. Read-only.

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
        lookback_days: int = 7,
        period_hours: int = 24,
    ) -> dict[str, Any]:
        """Get a Secure Browser portal's session metrics (AWS/WorkSpacesWeb) over a window.

        NOTE: Secure Browser only emits these metrics when sessions occur, so idle portals return
        nothing; richer per-session data is available via the portal's Session Logger. Read-only.

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

    mcp.add_tool(get_secure_browser_portal_details)
    mcp.add_tool(get_secure_browser_portal_usage)
