# Copyright bengroeneveldsg. Licensed under the Apache License, Version 2.0 (the "License").
# You may not use this file except in compliance with the License.
# A copy of the License is located at http://www.apache.org/licenses/LICENSE-2.0
"""Best-effort EUC price lookups (AWS Price List API) across the whole portfolio.

The Price List API is messy, so this is deliberately conservative: it matches the canonical
Included-license hardware SKU for a given region / OS / compute type / volume sizes and returns the
AlwaysOn monthly price plus the AutoStop monthly-base and hourly prices. Anything it cannot match
cleanly returns None, so callers degrade to "no estimate" rather than a wrong number.

Needs ``pricing:GetProducts`` (IAM Tier 1). The pricing client is global (queried from us-east-1).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Literal, NamedTuple

from loguru import logger

from ..clients import ClientFactory
from ._common import read_only

_PRICING_REGION = "us-east-1"

# Region -> Price List "location" long name. Extend as needed; unknown regions -> no estimate.
_REGION_LOCATIONS = {
    "us-east-1": "US East (N. Virginia)",
    "us-west-2": "US West (Oregon)",
    "eu-west-1": "Europe (Ireland)",
    "eu-central-1": "Europe (Frankfurt)",
    "ap-southeast-1": "Asia Pacific (Singapore)",
    "ap-southeast-2": "Asia Pacific (Sydney)",
    "ap-northeast-1": "Asia Pacific (Tokyo)",
    "ap-south-1": "Asia Pacific (Mumbai)",
    "ca-central-1": "Canada (Central)",
}

# WorkSpaces compute type -> Price List "bundle" name.
_COMPUTE_BUNDLE = {
    "VALUE": "Value",
    "STANDARD": "Standard",
    "PERFORMANCE": "Performance",
    "POWER": "Power",
    "POWERPRO": "PowerPro",
}

_cache: dict[tuple, WorkspacePrices | None] = {}


class WorkspacePrices(NamedTuple):
    alwayson_monthly: float | None
    autostop_monthly_base: float | None
    autostop_hourly: float | None


def _os_filter(operating_system: str | None) -> str | None:
    if not operating_system:
        return "Windows"
    o = operating_system.upper()
    if "WIN" in o:
        return "Windows"
    if any(k in o for k in ("LINUX", "UBUNTU", "RHEL", "ROCKY", "AMAZON")):
        return "Linux"
    return None


def _storage_str(root_gib: int | None, user_gib: int | None) -> str | None:
    if root_gib is None or user_gib is None:
        return None
    return f"Root:{root_gib} GB,User:{user_gib} GB"


def get_workspace_prices(
    factory: ClientFactory,
    region: str | None,
    operating_system: str | None,
    compute_type: str | None,
    root_gib: int | None,
    user_gib: int | None,
) -> WorkspacePrices | None:
    """Resolve AlwaysOn monthly + AutoStop base/hourly prices for a desktop (None if unmatched)."""
    location = _REGION_LOCATIONS.get(region or "")
    os_value = _os_filter(operating_system)
    bundle = _COMPUTE_BUNDLE.get((compute_type or "").upper())
    storage = _storage_str(root_gib, user_gib)
    if not (location and os_value and bundle and storage):
        return None

    key = (location, os_value, bundle, storage)
    if key in _cache:
        return _cache[key]

    base_filters = [
        {"Type": "TERM_MATCH", "Field": "location", "Value": location},
        {"Type": "TERM_MATCH", "Field": "operatingSystem", "Value": os_value},
        {"Type": "TERM_MATCH", "Field": "license", "Value": "Included"},
        {"Type": "TERM_MATCH", "Field": "bundle", "Value": bundle},
    ]
    alwayson = autostop_base = hourly = None
    try:
        pricing = factory.client("pricing", region=_PRICING_REGION)
        for running_mode in ("AlwaysOn", "AutoStop"):
            filters = base_filters + [
                {"Type": "TERM_MATCH", "Field": "runningMode", "Value": running_mode}
            ]
            resp = pricing.get_products(
                ServiceCode="AmazonWorkSpaces", Filters=filters, MaxResults=100
            )
            for raw in resp.get("PriceList", []):
                product = json.loads(raw)
                attrs = product["product"]["attributes"]
                if attrs.get("storage") != storage:
                    continue
                for term in product.get("terms", {}).get("OnDemand", {}).values():
                    for pd in term["priceDimensions"].values():
                        unit = (pd.get("unit") or "").lower()
                        usd = float(pd.get("pricePerUnit", {}).get("USD", 0) or 0)
                        if usd <= 0:
                            continue
                        if running_mode == "AlwaysOn" and unit == "month":
                            alwayson = usd
                        elif running_mode == "AutoStop" and unit == "hour":
                            hourly = usd
                        elif running_mode == "AutoStop" and unit == "month":
                            autostop_base = usd
    except Exception as exc:  # pricing is best-effort; never fail the recommendation
        logger.warning("Pricing lookup failed for {}: {}", key, exc)
        _cache[key] = None
        return None

    prices = WorkspacePrices(alwayson, autostop_base, hourly)
    _cache[key] = prices
    return prices


def _estimated_monthly_hours(active_days: int | None, lookback_days: int) -> float:
    """Rough monthly connected-hours estimate from active days (assume ~8h per active day)."""
    if not active_days or lookback_days <= 0:
        return 0.0
    return (active_days / lookback_days) * 30.0 * 8.0


def estimate_alwayson_to_autostop_savings(
    prices: WorkspacePrices | None, active_days: int | None, lookback_days: int
) -> float | None:
    """Monthly saving from moving an AlwaysOn desktop to AutoStop, given its usage."""
    if not prices or prices.alwayson_monthly is None or prices.autostop_monthly_base is None:
        return None
    hours = _estimated_monthly_hours(active_days, lookback_days)
    autostop_cost = prices.autostop_monthly_base + (prices.autostop_hourly or 0.0) * hours
    saving = prices.alwayson_monthly - autostop_cost
    return round(saving, 2) if saving > 0 else None


# --------------------------------------------------------------------- portfolio-wide pricing

# AppStream Platform enum -> Price List operatingSystem attribute. Windows 10/11 on WorkSpaces
# Applications are BYOL (customer-licensed) and bill at the cheaper BYOL SKU.
_APPSTREAM_OS = {
    "AMAZON_LINUX2": "Amazon Linux",
    "RHEL8": "Red Hat Enterprise Linux",
    "ROCKY_LINUX8": "Rocky Linux",
    "WINDOWS_10": "Windows BYOL",
    "WINDOWS_11": "Windows BYOL",
}

# Secure Browser usagetype suffix -> portal instanceType.
_WEB_TIER_BY_SUFFIX = {
    "WEB-ST-XLARGE": "standard.xlarge",
    "WEB-ST-LARGE": "standard.large",
    "WEB-ST": "standard.regular",
}

_generic_cache: dict[tuple, object] = {}


def _appstream_os_for_platform(platform: str | None) -> str:
    if not platform:
        return "Windows"
    return _APPSTREAM_OS.get(platform.upper(), "Windows")


def appstream_hourly_price(
    factory: ClientFactory,
    region: str | None,
    instance_type: str | None,
    instance_function: str = "Fleet",
    platform: str | None = None,
) -> float | None:
    """$/hour for a WorkSpaces Applications instance (Fleet/ImageBuilder/AppBlockBuilder/...)."""
    location = _REGION_LOCATIONS.get(region or "")
    if not (location and instance_type):
        return None
    os_value = _appstream_os_for_platform(platform)
    key = ("appstream", location, instance_type, instance_function, os_value)
    if key in _generic_cache:
        return _generic_cache[key]  # type: ignore[return-value]
    price: float | None = None
    try:
        pricing = factory.client("pricing", region=_PRICING_REGION)
        resp = pricing.get_products(
            ServiceCode="AmazonAppStream",
            Filters=[
                {"Type": "TERM_MATCH", "Field": "location", "Value": location},
                {"Type": "TERM_MATCH", "Field": "instanceType", "Value": instance_type},
                {"Type": "TERM_MATCH", "Field": "instanceFunction", "Value": instance_function},
                {"Type": "TERM_MATCH", "Field": "operatingSystem", "Value": os_value},
            ],
            MaxResults=20,
        )
        for raw in resp.get("PriceList", []):
            product = json.loads(raw)
            for term in product.get("terms", {}).get("OnDemand", {}).values():
                for pd in term["priceDimensions"].values():
                    usd = float(pd.get("pricePerUnit", {}).get("USD", 0) or 0)
                    if usd > 0 and (pd.get("unit") or "").lower().startswith("hour"):
                        price = usd
    except Exception as exc:  # best-effort; never fail the caller
        logger.warning("AppStream pricing lookup failed for {}: {}", key, exc)
    _generic_cache[key] = price
    return price


def secure_browser_mau_prices(factory: ClientFactory, region: str | None) -> dict[str, float]:
    """Portal instanceType -> $/monthly-active-user for the region."""
    location = _REGION_LOCATIONS.get(region or "")
    if not location:
        return {}
    key = ("web", location)
    if key in _generic_cache:
        return dict(_generic_cache[key])  # type: ignore[arg-type]
    out: dict[str, float] = {}
    try:
        pricing = factory.client("pricing", region=_PRICING_REGION)
        resp = pricing.get_products(
            ServiceCode="AmazonWorkSpacesWeb",
            Filters=[{"Type": "TERM_MATCH", "Field": "location", "Value": location}],
            MaxResults=100,
        )
        for raw in resp.get("PriceList", []):
            product = json.loads(raw)
            usage_type = product["product"]["attributes"].get("usagetype", "")
            tier = next(
                (t for suffix, t in _WEB_TIER_BY_SUFFIX.items() if usage_type.endswith(suffix)),
                None,
            )
            if not tier:
                continue
            for term in product.get("terms", {}).get("OnDemand", {}).values():
                for pd in term["priceDimensions"].values():
                    usd = float(pd.get("pricePerUnit", {}).get("USD", 0) or 0)
                    if usd > 0:
                        out[tier] = usd
    except Exception as exc:
        logger.warning("Secure Browser pricing lookup failed for {}: {}", location, exc)
    _generic_cache[key] = out
    return out


def core_instance_prices(
    factory: ClientFactory, region: str | None, instance_type: str
) -> list[dict]:
    """Managed-instance SKUs (monthly fee / hourly variants) for a Core instance type."""
    location = _REGION_LOCATIONS.get(region or "")
    if not (location and instance_type):
        return []
    key = ("core", location, instance_type)
    if key in _generic_cache:
        return list(_generic_cache[key])  # type: ignore[arg-type]
    out: list[dict] = []
    try:
        pricing = factory.client("pricing", region=_PRICING_REGION)
        resp = pricing.get_products(
            ServiceCode="AmazonWorkSpacesInstances",
            Filters=[
                {"Type": "TERM_MATCH", "Field": "location", "Value": location},
                {"Type": "TERM_MATCH", "Field": "instanceType", "Value": instance_type},
            ],
            MaxResults=100,
        )
        for raw in resp.get("PriceList", []):
            product = json.loads(raw)
            attrs = product["product"]["attributes"]
            for term in product.get("terms", {}).get("OnDemand", {}).values():
                for pd in term["priceDimensions"].values():
                    usd = float(pd.get("pricePerUnit", {}).get("USD", 0) or 0)
                    if usd <= 0:
                        continue
                    out.append(
                        {
                            "unit": pd.get("unit"),
                            "usd": usd,
                            "billing_option": attrs.get("billingoption"),
                            "tenancy": attrs.get("tenancy"),
                            "description": (pd.get("description") or "")[:120],
                        }
                    )
    except Exception as exc:
        logger.warning("Core pricing lookup failed for {} {}: {}", location, instance_type, exc)
    _generic_cache[key] = out
    return out


def register(mcp, factory: ClientFactory) -> None:
    """Register the portfolio pricing tool on the FastMCP app."""

    async def get_euc_service_prices(
        service: Literal["applications", "secure-browser", "core", "personal"],
        region: str | None = None,
        instance_type: str | None = None,
        instance_function: Literal[
            "Fleet", "ImageBuilder", "AppBlockBuilder", "ElasticFleet", "MultiSessionFleet"
        ] = "Fleet",
        platform: str | None = None,
        compute_type: str | None = None,
        operating_system: str | None = None,
        root_volume_gib: int | None = None,
        user_volume_gib: int | None = None,
    ) -> dict[str, Any]:
        """Get authoritative AWS list prices for EUC resources in a region (AWS Price List API).

        ALWAYS use this instead of estimating prices from memory — rates vary by region and
        license model. Coverage:
        - applications: $/hour for a WorkSpaces Applications (AppStream) instance — needs
          instance_type (e.g. stream.standard.large), instance_function, and the platform
          (WINDOWS_SERVER_* = included license; WINDOWS_10/11 = BYOL, cheaper). Also returns
          derived $/day and $/month (730 h).
        - secure-browser: $/monthly-active-user per portal tier (standard.regular/large/xlarge).
        - core: managed-instance fee SKUs for an instance type (monthly/hourly variants by
          billing option).
        - personal: AlwaysOn monthly + AutoStop base/hourly for a bundle — needs compute_type,
          operating_system, root_volume_gib, user_volume_gib.
        Needs pricing:GetProducts (IAM Tier 1). Read-only.

        Args:
            service: Which EUC service to price.
            region: AWS region the resources run in. Defaults to the server's configured region.
            instance_type: Streaming/managed instance type (applications, core).
            instance_function: Applications instance role (default Fleet).
            platform: Applications platform enum (e.g. WINDOWS_SERVER_2025, WINDOWS_11).
            compute_type: WorkSpaces Personal compute type (personal).
            operating_system: WorkSpaces Personal OS name (personal).
            root_volume_gib: Personal root volume GiB (personal).
            user_volume_gib: Personal user volume GiB (personal).
        """

        def _lookup() -> dict[str, Any]:
            target_region = region or factory.region
            location = _REGION_LOCATIONS.get(target_region or "")
            base: dict[str, Any] = {
                "service": service,
                "region": target_region,
                "pricing_location": location,
                "notes": [],
            }
            if not location:
                base["notes"].append(
                    f"Region {target_region} is not in the pricing location map; no prices."
                )
                return base
            if service == "applications":
                hourly = appstream_hourly_price(
                    factory, target_region, instance_type, instance_function, platform
                )
                base.update(
                    {
                        "instance_type": instance_type,
                        "instance_function": instance_function,
                        "operating_system": _appstream_os_for_platform(platform),
                        "hourly_usd": hourly,
                        "daily_usd_24x7": round(hourly * 24, 2) if hourly else None,
                        "monthly_usd_24x7": round(hourly * 730, 2) if hourly else None,
                    }
                )
                if hourly is None:
                    base["notes"].append(
                        "No matching SKU — check instance_type/instance_function/platform."
                    )
            elif service == "secure-browser":
                base["monthly_active_user_usd_by_tier"] = secure_browser_mau_prices(
                    factory, target_region
                )
                base["notes"].append(
                    "Secure Browser bills per monthly active user by the portal's instance tier."
                )
            elif service == "core":
                if not instance_type:
                    base["notes"].append("instance_type is required for core pricing.")
                else:
                    base["skus"] = core_instance_prices(factory, target_region, instance_type)
                    base["notes"].append(
                        "Core Managed Instances: the management fee SKUs above; the underlying "
                        "EC2/EBS costs bill separately on EC2."
                    )
            else:  # personal
                prices = get_workspace_prices(
                    factory,
                    target_region,
                    operating_system,
                    compute_type,
                    root_volume_gib,
                    user_volume_gib,
                )
                base.update(
                    {
                        "alwayson_monthly_usd": prices.alwayson_monthly if prices else None,
                        "autostop_monthly_base_usd": prices.autostop_monthly_base
                        if prices
                        else None,
                        "autostop_hourly_usd": prices.autostop_hourly if prices else None,
                    }
                )
                if not prices:
                    base["notes"].append(
                        "No clean bundle match (needs compute_type, operating_system and both "
                        "volume sizes; Included-license SKUs only)."
                    )
            return base

        return await asyncio.to_thread(_lookup)

    mcp.add_tool(get_euc_service_prices, annotations=read_only("EUC service prices"))
