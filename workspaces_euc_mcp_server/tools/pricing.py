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

# WorkSpaces compute type -> Price List "bundle" FAMILY name. Storage variants of a compute
# type are SEPARATE bundles suffixed -0..-3 (e.g. Power = Root:175/User:100, Power-0 = 80/10,
# Power-1 = 80/50, Power-2 = 80/100), so the family is queried as a whole and matched on the
# storage attribute. "... Plus" bundles are distinct software-bundled products — excluded.
_COMPUTE_BUNDLE = {
    "VALUE": "Value",
    "STANDARD": "Standard",
    "PERFORMANCE": "Performance",
    "POWER": "Power",
    "POWERPRO": "PowerPro",
    "GRAPHICS": "Graphics",
    "GRAPHICSPRO": "GraphicsPro",
    "GRAPHICS_G4DN": "Graphics.g4dn",
    "GRAPHICSPRO_G4DN": "GraphicsPro.g4dn",
    "GENERALPURPOSE_4XLARGE": "GeneralPurpose.4xlarge",
    "GENERALPURPOSE_8XLARGE": "GeneralPurpose.8xlarge",
}
_BUNDLE_VARIANT_SUFFIXES = ("", "-0", "-1", "-2", "-3")
# Personal SKUs live under this product family; the SAME bundle names exist again under
# "WorkSpaces Core" with different prices, so the filter is load-bearing.
_PERSONAL_PRODUCT_FAMILY = "Enterprise Applications"

_cache: dict[tuple, WorkspacePrices | None] = {}


class WorkspacePrices(NamedTuple):
    alwayson_monthly: float | None
    autostop_monthly_base: float | None
    autostop_hourly: float | None


# Client-OS markers: Windows 10/11 on WorkSpaces Personal is ALWAYS BYOL (customer-licensed,
# dedicated tenancy) and bills on separate hardware-only SKUs — cheaper than the Windows Server
# Included-license rates. Never present the two as identically priced.
_BYOL_OS_MARKERS = (
    "WINDOWS_10",
    "WINDOWS_11",
    "WINDOWS 10",
    "WINDOWS 11",
    "WIN10",
    "WIN11",
    "BYOL",
)


def _personal_os_license(operating_system: str | None) -> tuple[tuple[str, ...], str] | None:
    """(acceptable Price List operatingSystem values, license value) for a requested OS.

    Grounded in the actual Price List vocabulary: Windows Server bundles are os=Windows /
    license=Included; Windows 10/11 is BYOL (those SKUs carry os Windows OR Any); Amazon Linux
    and Ubuntu carry license=None (not Included); RHEL and Rocky are Included.
    """
    o = (operating_system or "").upper()
    if any(m in o for m in _BYOL_OS_MARKERS):
        return ("Windows", "Any"), "Bring Your Own License"
    if "AMAZON" in o:
        return ("Amazon Linux",), "None"
    if "UBUNTU" in o:
        return ("Ubuntu Linux",), "None"
    if "RHEL" in o or "RED HAT" in o or "RED_HAT" in o:
        return ("Red Hat Enterprise Linux",), "Included"
    if "ROCKY" in o:
        return ("Rocky Linux",), "Included"
    if "WIN" in o or not o:
        return ("Windows",), "Included"
    return None


def personal_license_model(operating_system: str | None) -> str:
    """Price List 'license' attribute value for a Personal OS name."""
    resolved = _personal_os_license(operating_system)
    return resolved[1] if resolved else "Included"


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
    """Resolve AlwaysOn monthly + AutoStop base/hourly prices for a desktop (None if unmatched).

    Fetches the whole bundle FAMILY (storage variants are separate suffixed bundles) and
    selects client-side on storage + OS + license — grounded in the real SKU vocabulary.
    """
    location = _REGION_LOCATIONS.get(region or "")
    resolved = _personal_os_license(operating_system)
    bundle = _COMPUTE_BUNDLE.get((compute_type or "").upper())
    storage = _storage_str(root_gib, user_gib)
    if not (location and resolved and bundle and storage):
        return None
    os_values, license_value = resolved

    key = (location, os_values, license_value, bundle, storage)
    if key in _cache:
        return _cache[key]

    # The AutoStop HOURLY rate is compute-level (published once per bundle+OS+license, on the
    # base bundle's rows); only the monthly fees vary by storage. Prefer a storage-exact hourly
    # if one ever appears, else use the family's.
    alwayson = autostop_base = hourly_exact = hourly_family = None
    for row in _personal_family_rows(factory, location, bundle):
        if row["operating_system"] not in os_values or row["license"] != license_value:
            continue
        if row["running_mode"] == "AutoStop" and row["unit"] == "hour":
            if row["storage"] == storage:
                hourly_exact = row["usd"]
            else:
                hourly_family = row["usd"]
            continue
        if row["storage"] != storage:
            continue
        if row["running_mode"] == "AlwaysOn" and row["unit"] == "month":
            alwayson = row["usd"]
        elif row["running_mode"] == "AutoStop" and row["unit"] == "month":
            autostop_base = row["usd"]

    hourly = hourly_exact
    if hourly is None and (alwayson is not None or autostop_base is not None):
        # Inherit the compute-level hourly only when the pairing actually exists — a fully
        # unmatched storage must stay all-None, not masquerade as a partial match.
        hourly = hourly_family
    prices = WorkspacePrices(alwayson, autostop_base, hourly)
    _cache[key] = prices
    return prices


def _personal_family_rows(factory: ClientFactory, location: str, bundle: str) -> list[dict]:
    """Every priced dimension for a bundle family (base bundle + its storage-variant bundles).

    Filtered to the Personal product family — the SAME bundle names exist under "WorkSpaces
    Core" with different prices, so productFamily is load-bearing.
    """
    key = ("personal-family", location, bundle)
    if key in _generic_cache:
        return list(_generic_cache[key])  # type: ignore[arg-type]
    rows: list[dict] = []
    try:
        pricing = factory.client("pricing", region=_PRICING_REGION)
        for suffix in _BUNDLE_VARIANT_SUFFIXES:
            filters = [
                {"Type": "TERM_MATCH", "Field": "location", "Value": location},
                {
                    "Type": "TERM_MATCH",
                    "Field": "productFamily",
                    "Value": _PERSONAL_PRODUCT_FAMILY,
                },
                {"Type": "TERM_MATCH", "Field": "bundle", "Value": bundle + suffix},
            ]
            for raw in _iter_price_list(pricing, "AmazonWorkSpaces", filters):
                product = json.loads(raw)
                a = product["product"]["attributes"]
                for term in product.get("terms", {}).get("OnDemand", {}).values():
                    for pd in term["priceDimensions"].values():
                        usd = float(pd.get("pricePerUnit", {}).get("USD", 0) or 0)
                        if usd <= 0:
                            continue
                        rows.append(
                            {
                                "bundle": a.get("bundle"),
                                "operating_system": a.get("operatingSystem"),
                                "license": a.get("license"),
                                "running_mode": a.get("runningMode"),
                                "storage": a.get("storage"),
                                "unit": (pd.get("unit") or "").lower(),
                                "usd": usd,
                            }
                        )
    except Exception as exc:  # pricing is best-effort; never fail the caller
        logger.warning("Bundle family fetch failed for {} {}: {}", location, bundle, exc)
        return []
    _generic_cache[key] = rows
    return rows


def list_workspace_bundle_skus(
    factory: ClientFactory,
    region: str | None,
    operating_system: str | None,
    compute_type: str | None,
) -> list[dict]:
    """Every storage pairing AWS prices for a region/OS/compute (near-miss fallback listing)."""
    location = _REGION_LOCATIONS.get(region or "")
    resolved = _personal_os_license(operating_system)
    bundle = _COMPUTE_BUNDLE.get((compute_type or "").upper())
    if not (location and resolved and bundle):
        return []
    os_values, license_value = resolved
    by_storage: dict[str, dict] = {}
    family_hourly: float | None = None
    for row in _personal_family_rows(factory, location, bundle):
        storage = row["storage"]
        if (
            not storage
            or row["operating_system"] not in os_values
            or row["license"] != license_value
        ):
            continue
        if row["running_mode"] == "AlwaysOn" and row["unit"] == "month":
            by_storage.setdefault(storage, {"storage": storage})["alwayson_monthly_usd"] = row[
                "usd"
            ]
        elif row["running_mode"] == "AutoStop" and row["unit"] == "hour":
            by_storage.setdefault(storage, {"storage": storage})["autostop_hourly_usd"] = row["usd"]
            family_hourly = row["usd"]
        elif row["running_mode"] == "AutoStop" and row["unit"] == "month":
            by_storage.setdefault(storage, {"storage": storage})["autostop_monthly_base_usd"] = row[
                "usd"
            ]
    # The hourly rate is compute-level; fill it in for pairings that only carry monthly fees.
    if family_hourly is not None:
        for entry in by_storage.values():
            entry.setdefault("autostop_hourly_usd", family_hourly)
    return sorted(by_storage.values(), key=lambda e: e["storage"])


def workspaces_pool_rates(
    factory: ClientFactory, region: str | None, compute_type: str | None
) -> dict:
    """WorkSpaces Pools rates for a compute type: streaming $/hr by license + shared fees.

    Pool SKUs live under runningMode=Pool per bundle (Included vs BYOL rates); the stopped-
    instance fee and the monthly per-user fee are their own bundles ("Stopped Instance",
    "User Fee") under runningMode "Not Applicable Pools".
    """
    location = _REGION_LOCATIONS.get(region or "")
    bundle = _COMPUTE_BUNDLE.get((compute_type or "").upper())
    if not (location and bundle):
        return {}
    key = ("pools", location, bundle)
    if key in _generic_cache:
        return dict(_generic_cache[key])  # type: ignore[arg-type]
    out: dict[str, float] = {}
    try:
        pricing = factory.client("pricing", region=_PRICING_REGION)
        filters = [
            {"Type": "TERM_MATCH", "Field": "location", "Value": location},
            {"Type": "TERM_MATCH", "Field": "runningMode", "Value": "Pool"},
            {"Type": "TERM_MATCH", "Field": "bundle", "Value": bundle},
        ]
        for raw in _iter_price_list(pricing, "AmazonWorkSpaces", filters):
            product = json.loads(raw)
            a = product["product"]["attributes"]
            for term in product.get("terms", {}).get("OnDemand", {}).values():
                for pd in term["priceDimensions"].values():
                    usd = float(pd.get("pricePerUnit", {}).get("USD", 0) or 0)
                    if usd <= 0 or not (pd.get("unit") or "").lower().startswith("hour"):
                        continue
                    if a.get("license") == "Bring Your Own License":
                        out["streaming_hourly_byol_usd"] = usd
                    elif a.get("license") == "Included":
                        out["streaming_hourly_included_usd"] = usd
        for fee_bundle, field, unit_prefix in (
            ("Stopped Instance", "stopped_instance_hourly_usd", "hour"),
            ("User Fee", "user_fee_monthly_usd", "month"),
        ):
            fee_filters = [
                {"Type": "TERM_MATCH", "Field": "location", "Value": location},
                {"Type": "TERM_MATCH", "Field": "bundle", "Value": fee_bundle},
            ]
            for raw in _iter_price_list(pricing, "AmazonWorkSpaces", fee_filters):
                product = json.loads(raw)
                for term in product.get("terms", {}).get("OnDemand", {}).values():
                    for pd in term["priceDimensions"].values():
                        usd = float(pd.get("pricePerUnit", {}).get("USD", 0) or 0)
                        if usd > 0 and (pd.get("unit") or "").lower().startswith(unit_prefix):
                            out[field] = usd
    except Exception as exc:
        logger.warning("Pools pricing lookup failed for {} {}: {}", location, bundle, exc)
        return {}
    _generic_cache[key] = out
    return out


def autostop_breakeven_hours(prices: WorkspacePrices | None) -> float | None:
    """Connected hours/month above which ALWAYS_ON beats AUTO_STOP for a bundle.

    AUTO_STOP costs base + hourly x hours; ALWAYS_ON is flat — they cross at
    (alwayson - base) / hourly. Handed to the assistant pre-computed because models
    reliably mis-derive tipping points from raw rates.
    """
    if (
        not prices
        or prices.alwayson_monthly is None
        or prices.autostop_monthly_base is None
        or not prices.autostop_hourly
    ):
        return None
    breakeven = (prices.alwayson_monthly - prices.autostop_monthly_base) / prices.autostop_hourly
    return round(breakeven, 1) if breakeven > 0 else None


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

# Price List operatingSystem values for AmazonAppStream (discovered): Windows, Windows BYOL,
# Amazon Linux, Red Hat Enterprise Linux, Rocky Linux, Ubuntu Pro. Windows 10/11 platforms are
# BYOL (customer-licensed) and bill at the cheaper BYOL SKU; matching is substring-based so new
# platform enum revisions (AMAZON_LINUX2023, RHEL9, ...) resolve instead of defaulting wrong.

# Secure Browser usagetype suffix -> portal instanceType.
_WEB_TIER_BY_SUFFIX = {
    "WEB-ST-XLARGE": "standard.xlarge",
    "WEB-ST-LARGE": "standard.large",
    "WEB-ST": "standard.regular",
}

_generic_cache: dict[tuple, object] = {}

_PRICE_LIST_MAX_PAGES = 5  # get_products caps at 100 products/page; 500 covers any EUC query


def _iter_price_list(pricing: Any, service_code: str, filters: list[dict]) -> Any:
    """Yield raw PriceList JSON strings across pages (single pages silently truncate)."""
    token: str | None = None
    for _ in range(_PRICE_LIST_MAX_PAGES):
        kwargs: dict[str, Any] = {
            "ServiceCode": service_code,
            "Filters": filters,
            "MaxResults": 100,
        }
        if token:
            kwargs["NextToken"] = token
        resp = pricing.get_products(**kwargs)
        yield from resp.get("PriceList", [])
        token = resp.get("NextToken")
        if not token:
            return


def classify_price_completeness(prices: WorkspacePrices | None) -> str:
    """'complete' | 'partial' | 'none' — AWS's Price List publishes some bundle/storage
    pairings with only a subset of rates (e.g. Power Root:80/User:10 in Singapore has an
    AutoStop hourly but no monthly fees), and unexplained nulls read as $0 or "unavailable"."""
    if prices is None or all(p is None for p in prices):
        return "none"
    if any(p is None for p in prices):
        return "partial"
    return "complete"


def _appstream_os_for_platform(platform: str | None) -> str:
    if not platform:
        return "Windows"
    p = platform.upper()
    if any(m in p for m in _BYOL_OS_MARKERS):
        return "Windows BYOL"
    if "UBUNTU" in p:
        return "Ubuntu Pro"
    if "AMAZON" in p:
        return "Amazon Linux"
    if "RHEL" in p or "RED_HAT" in p or "RED HAT" in p:
        return "Red Hat Enterprise Linux"
    if "ROCKY" in p:
        return "Rocky Linux"
    return "Windows"


def appstream_user_fees(factory: ClientFactory, region: str | None) -> dict[str, float]:
    """Microsoft user fees (RDS SAL/CAL) for Included-license Windows Applications fleets.

    Billed per unique user per month on top of instance hours: one rate for single-session
    fleets, first+additional rates for multi-session. BYOL Windows 10/11 fleets do not incur
    these.
    """
    location = _REGION_LOCATIONS.get(region or "")
    if not location:
        return {}
    key = ("appstream-user-fees", location)
    if key in _generic_cache:
        return dict(_generic_cache[key])  # type: ignore[arg-type]
    out: dict[str, float] = {}
    try:
        pricing = factory.client("pricing", region=_PRICING_REGION)
        filters = [
            {"Type": "TERM_MATCH", "Field": "location", "Value": location},
            {"Type": "TERM_MATCH", "Field": "productFamily", "Value": "User Fees"},
        ]
        for raw in _iter_price_list(pricing, "AmazonAppStream", filters):
            product = json.loads(raw)
            usage_type = product["product"]["attributes"].get("usagetype", "")
            for term in product.get("terms", {}).get("OnDemand", {}).values():
                for pd in term["priceDimensions"].values():
                    usd = float(pd.get("pricePerUnit", {}).get("USD", 0) or 0)
                    if usd <= 0:
                        continue
                    if "Multi-Session-Additional" in usage_type:
                        out["multi_session_additional_user_monthly_usd"] = usd
                    elif "Multi-Session" in usage_type:
                        out["multi_session_user_monthly_usd"] = usd
                    else:
                        out["single_session_user_monthly_usd"] = usd
    except Exception as exc:
        logger.warning("AppStream user-fee lookup failed for {}: {}", location, exc)
        return {}
    _generic_cache[key] = out
    return out


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
        filters = [
            {"Type": "TERM_MATCH", "Field": "location", "Value": location},
            {"Type": "TERM_MATCH", "Field": "instanceType", "Value": instance_type},
            {"Type": "TERM_MATCH", "Field": "instanceFunction", "Value": instance_function},
            {"Type": "TERM_MATCH", "Field": "operatingSystem", "Value": os_value},
        ]
        for raw in _iter_price_list(pricing, "AmazonAppStream", filters):
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


def resolve_fleet_pricing_inputs(
    factory: ClientFactory, region: str | None, fleet_name: str
) -> dict[str, Any] | None:
    """Resolve a fleet's pricing inputs from the live fleet definition.

    DescribeFleets omits Platform for non-Elastic fleets, so the platform is resolved from the
    fleet's IMAGE — that is what distinguishes BYOL Windows 10/11 rates from included-license
    Windows Server rates (~10% apart).
    """
    try:
        appstream = factory.client("appstream", region=region)
        fleets = appstream.describe_fleets(Names=[fleet_name]).get("Fleets", [])
        if not fleets:
            return None
        f = fleets[0]
        platform = f.get("Platform")
        if not platform:
            image_kwargs: dict[str, Any] = {}
            if f.get("ImageArn"):
                image_kwargs = {"Arns": [f["ImageArn"]]}
            elif f.get("ImageName"):
                image_kwargs = {"Names": [f["ImageName"]]}
            if image_kwargs:
                images = appstream.describe_images(**image_kwargs).get("Images", [])
                if images:
                    platform = images[0].get("Platform")
        fleet_type = f.get("FleetType")
        if fleet_type == "ELASTIC":
            function = "ElasticFleet"
        elif (f.get("MaxSessionsPerInstance") or 1) > 1:
            function = "MultiSessionFleet"
        else:
            function = "Fleet"
        return {
            "fleet_name": fleet_name,
            "fleet_type": fleet_type,
            "instance_type": f.get("InstanceType"),
            "platform": platform,
            "instance_function": function,
            "desired_capacity": (f.get("ComputeCapacityStatus") or {}).get("Desired"),
            "image": f.get("ImageName") or f.get("ImageArn"),
        }
    except Exception as exc:  # best-effort; never fail the caller
        logger.warning("Fleet pricing resolution failed for {}: {}", fleet_name, exc)
        return None


def appstream_stopped_instance_fee(factory: ClientFactory, region: str | None) -> float | None:
    """$/hour for a provisioned-but-idle On-Demand fleet instance (instance-type-agnostic SKU)."""
    location = _REGION_LOCATIONS.get(region or "")
    if not location:
        return None
    key = ("appstream-stopped", location)
    if key in _generic_cache:
        return _generic_cache[key]  # type: ignore[return-value]
    fee: float | None = None
    try:
        pricing = factory.client("pricing", region=_PRICING_REGION)
        filters = [
            {"Type": "TERM_MATCH", "Field": "location", "Value": location},
            {
                "Type": "TERM_MATCH",
                "Field": "instanceFunction",
                "Value": "StoppedFleetInstance",
            },
        ]
        for raw in _iter_price_list(pricing, "AmazonAppStream", filters):
            product = json.loads(raw)
            for term in product.get("terms", {}).get("OnDemand", {}).values():
                for pd in term["priceDimensions"].values():
                    usd = float(pd.get("pricePerUnit", {}).get("USD", 0) or 0)
                    if usd > 0 and (pd.get("unit") or "").lower().startswith("hour"):
                        fee = usd
    except Exception as exc:  # best-effort; never fail the caller
        logger.warning("Stopped-instance fee lookup failed for {}: {}", location, exc)
    _generic_cache[key] = fee
    return fee


def appstream_os_rate_options(
    factory: ClientFactory,
    region: str | None,
    instance_type: str | None,
    instance_function: str,
) -> dict[str, float]:
    """OS/license variant -> $/hour for an instance type + function (no-match discovery).

    When a requested platform has no SKU, listing what AWS *does* price lets the assistant pick
    the right variant instead of guessing (the awslabs "never guess values" pattern)."""
    location = _REGION_LOCATIONS.get(region or "")
    if not (location and instance_type):
        return {}
    key = ("appstream-os-options", location, instance_type, instance_function)
    if key in _generic_cache:
        return dict(_generic_cache[key])  # type: ignore[arg-type]
    out: dict[str, float] = {}
    try:
        pricing = factory.client("pricing", region=_PRICING_REGION)
        filters = [
            {"Type": "TERM_MATCH", "Field": "location", "Value": location},
            {"Type": "TERM_MATCH", "Field": "instanceType", "Value": instance_type},
            {"Type": "TERM_MATCH", "Field": "instanceFunction", "Value": instance_function},
        ]
        for raw in _iter_price_list(pricing, "AmazonAppStream", filters):
            product = json.loads(raw)
            os_value = product["product"]["attributes"].get("operatingSystem")
            if not os_value:
                continue
            for term in product.get("terms", {}).get("OnDemand", {}).values():
                for pd in term["priceDimensions"].values():
                    usd = float(pd.get("pricePerUnit", {}).get("USD", 0) or 0)
                    if usd > 0 and (pd.get("unit") or "").lower().startswith("hour"):
                        out[os_value] = usd
    except Exception as exc:
        logger.warning("OS-options lookup failed for {}: {}", key, exc)
    _generic_cache[key] = out
    return out


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
        filters = [{"Type": "TERM_MATCH", "Field": "location", "Value": location}]
        for raw in _iter_price_list(pricing, "AmazonWorkSpacesWeb", filters):
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
        filters = [
            {"Type": "TERM_MATCH", "Field": "location", "Value": location},
            {"Type": "TERM_MATCH", "Field": "instanceType", "Value": instance_type},
        ]
        for raw in _iter_price_list(pricing, "AmazonWorkSpacesInstances", filters):
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
        service: Literal["applications", "secure-browser", "core", "personal", "pools"],
        region: str | None = None,
        regions: list[str] | None = None,
        fleet_name: str | None = None,
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
        - applications: $/hour for a WorkSpaces Applications (AppStream) instance. PREFER passing
          fleet_name — the tool then resolves instance_type, fleet type, capacity, and the
          platform from the fleet's IMAGE (DescribeFleets omits Platform for non-Elastic fleets;
          WINDOWS_10/11 images = BYOL, cheaper than WINDOWS_SERVER_* included license) and adds
          idle_monthly_usd / provisioned_monthly_usd estimates. Otherwise pass instance_type,
          instance_function, and platform explicitly. Also returns derived $/day and $/month
          (730 h) and on_demand_stopped_hourly_usd. The 24x7 figures apply to ALWAYS_ON fleets
          and RUNNING builders only; ON_DEMAND fleets bill the full rate only while streaming
          plus the stopped-instance fee while provisioned idle.
        - pools: WorkSpaces Pools rates for a compute_type — streaming $/hr (Included vs BYOL
          image licensing), the pool stopped-instance $/hr, and the $/user/month user fee.
        - secure-browser: $/monthly-active-user per portal tier (standard.regular/large/xlarge).
        - core: managed-instance fee SKUs for an instance type — one SKU per billing option
          (hourly vs monthly). The account bills whichever billing option it is configured for;
          do NOT assume the monthly fee applies.
        - personal: AlwaysOn monthly + AutoStop base/hourly for a bundle — needs compute_type,
          operating_system, root_volume_gib, user_volume_gib. LICENSE-AWARE: Windows 10/11 =
          BYOL hardware-only SKUs; Windows Server/Linux = Included — the rates DIFFER, so never
          claim Win 11 and Win Server cost the same without querying both. Returns
          autostop_breakeven_hours_per_month: below it AUTO_STOP is cheaper, above it ALWAYS_ON —
          use it for running-mode recommendations and sizing questions ("N desktops used X
          hours/month"). ASK for bundle/OS/storage rather than assuming when the user hasn't
          said.
        Needs pricing:GetProducts (IAM Tier 1). Read-only.

        Args:
            service: Which EUC service to price.
            region: AWS region the resources run in. Defaults to the server's configured region.
            regions: Compare list prices across several regions in ONE call (returns by_region);
                overrides region. Use for "is X cheaper in Sydney?" questions.
            fleet_name: Applications fleet to price — auto-resolves instance type, platform
                (BYOL-aware, from the fleet's image), fleet type, and capacity estimates.
            instance_type: Streaming/managed instance type (applications, core).
            instance_function: Applications instance role (default Fleet).
            platform: Applications platform enum (e.g. WINDOWS_SERVER_2025, WINDOWS_11).
            compute_type: WorkSpaces Personal compute type (personal).
            operating_system: WorkSpaces Personal OS name (personal).
            root_volume_gib: Personal root volume GiB (personal).
            user_volume_gib: Personal user volume GiB (personal).
        """

        def _lookup_one(target_region: str | None) -> dict[str, Any]:
            location = _REGION_LOCATIONS.get(target_region or "")
            base: dict[str, Any] = {
                "service": service,
                "region": target_region,
                "pricing_location": location,
                "assumptions": [
                    "Rates are PUBLIC LIST prices from the AWS Price List API — private "
                    "pricing, EDP/PPA discounts, and credits are NOT reflected; "
                    "get_euc_cost_summary shows the account's discounted actuals.",
                    "Monthly derivations assume a 730-hour month.",
                ],
                "notes": [],
            }
            if not location:
                base["notes"].append(
                    f"Region {target_region} is not in the pricing location map; no prices."
                )
                return base
            if service == "applications":
                itype, ifunc, iplat = instance_type, instance_function, platform
                fleet_info: dict[str, Any] | None = None
                if fleet_name:
                    fleet_info = resolve_fleet_pricing_inputs(factory, target_region, fleet_name)
                    if fleet_info:
                        itype = itype or fleet_info["instance_type"]
                        iplat = iplat or fleet_info["platform"]
                        if instance_function == "Fleet":
                            ifunc = fleet_info["instance_function"]
                        base["fleet"] = fleet_info
                    else:
                        base["notes"].append(
                            f"Fleet '{fleet_name}' was not found in {target_region}; pricing "
                            "from the explicit parameters only."
                        )
                hourly = appstream_hourly_price(factory, target_region, itype, ifunc, iplat)
                stopped_fee = appstream_stopped_instance_fee(factory, target_region)
                base.update(
                    {
                        "instance_type": itype,
                        "instance_function": ifunc,
                        "operating_system": _appstream_os_for_platform(iplat),
                        "hourly_usd": hourly,
                        "daily_usd_24x7": round(hourly * 24, 2) if hourly else None,
                        "monthly_usd_24x7": round(hourly * 730, 2) if hourly else None,
                        "on_demand_stopped_hourly_usd": stopped_fee,
                    }
                )
                if fleet_info and hourly is not None:
                    desired = fleet_info.get("desired_capacity") or 0
                    if fleet_info.get("fleet_type") == "ALWAYS_ON" and desired:
                        base["provisioned_monthly_usd"] = round(hourly * 730 * desired, 2)
                        base["notes"].append(
                            f"provisioned_monthly_usd = {desired} ALWAYS_ON instance(s) x 730 h "
                            "x hourly_usd — this fleet bills 24/7 regardless of usage."
                        )
                    elif fleet_info.get("fleet_type") == "ON_DEMAND" and desired and stopped_fee:
                        base["idle_monthly_usd"] = round(stopped_fee * 730 * desired, 2)
                        base["notes"].append(
                            f"idle_monthly_usd = {desired} ON_DEMAND instance(s) x 730 h x the "
                            "stopped-instance fee — the cost floor with zero streaming; add "
                            "hourly_usd per actual streaming hour."
                        )
                base["notes"].append(
                    "BILLING MODEL BY FLEET TYPE — do not apply the 24x7 figures blindly: "
                    "ALWAYS_ON fleets bill hourly_usd 24/7 per provisioned instance "
                    "(monthly_usd_24x7 applies). ON_DEMAND fleets bill hourly_usd ONLY while a "
                    "user is streaming; provisioned-but-idle instances bill "
                    "on_demand_stopped_hourly_usd instead (~10x cheaper). ELASTIC fleets bill "
                    "streaming hours only. Image/app-block builders bill hourly_usd the whole "
                    "time they are RUNNING (24x7 figures apply). For actuals use "
                    "get_euc_cost_summary, and never extrapolate a full month for a resource "
                    "created mid-month."
                )
                base["notes"].append(
                    "The On-Demand stopped-instance fee is one flat SKU per region — it does "
                    "NOT vary by instance type."
                )
                if _appstream_os_for_platform(iplat) == "Windows":
                    fees = appstream_user_fees(factory, target_region)
                    if fees:
                        base["microsoft_user_fees_monthly_usd"] = fees
                        base["notes"].append(
                            "Included-license Windows fleets ALSO bill a Microsoft user fee "
                            "(RDS SAL) per unique user per month on top of instance hours — "
                            "single_session vs multi_session (first user) + additional-user "
                            "rates above. BYOL Windows 10/11 fleets do not incur these."
                        )
                if hourly is None:
                    base["notes"].append(
                        "No matching SKU — check instance_type/instance_function/platform."
                    )
                    os_options = appstream_os_rate_options(factory, target_region, itype, ifunc)
                    if os_options:
                        base["available_operating_systems"] = os_options
                        base["notes"].append(
                            "available_operating_systems lists every OS/license variant AWS "
                            "prices for this instance type + function in this region — pick "
                            "the matching one instead of guessing."
                        )
            elif service == "pools":
                if not compute_type:
                    base["notes"].append("compute_type is required for pools pricing.")
                else:
                    base["rates"] = workspaces_pool_rates(factory, target_region, compute_type)
                    base["notes"].append(
                        "WorkSpaces Pools bill the streaming rate per instance-hour while "
                        "serving sessions, the stopped-instance rate for provisioned idle "
                        "instances, PLUS the monthly user fee per unique user. Included vs "
                        "BYOL streaming rates differ with the image's license model."
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
                        "Core Managed Instances: one management-fee SKU per billing option "
                        "(hourly vs monthly) — the account bills whichever option it is "
                        "configured for, so do not assume the monthly fee. Compare actuals "
                        "(get_euc_cost_summary workspaces_breakdown) to see which applies. "
                        "Underlying EC2/EBS costs bill separately on EC2."
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
                breakeven = autostop_breakeven_hours(prices)
                license_model = personal_license_model(operating_system)
                base.update(
                    {
                        "license_model": license_model,
                        "alwayson_monthly_usd": prices.alwayson_monthly if prices else None,
                        "autostop_monthly_base_usd": prices.autostop_monthly_base
                        if prices
                        else None,
                        "autostop_hourly_usd": prices.autostop_hourly if prices else None,
                        "autostop_breakeven_hours_per_month": breakeven,
                    }
                )
                if license_model == "Bring Your Own License":
                    base["notes"].append(
                        "LICENSE MODEL: Windows 10/11 on WorkSpaces Personal is BYOL — these "
                        "are hardware-only rates that DIFFER from included-license Windows "
                        "Server rates (never present the two as identically priced). BYOL also "
                        "requires bringing eligible Windows licenses and dedicated-tenancy "
                        "enablement (check get_euc_account_posture)."
                    )
                else:
                    base["notes"].append(
                        "LICENSE MODEL: Included (Windows Server-based or Linux). If the user "
                        "means Windows 10/11, that is BYOL with different rates — re-query "
                        "with operating_system WINDOWS_11."
                    )
                if breakeven is not None:
                    base["notes"].append(
                        f"RUNNING-MODE TIPPING POINT: at ~{breakeven:.0f} connected hours/month "
                        "the two modes cost the same for this bundle. Users below it are "
                        "cheaper on AUTO_STOP (base + hourly); users above it are cheaper on "
                        "ALWAYS_ON (flat monthly). Compare each user's expected connected "
                        "hours/month against this number — near the tipping point the "
                        "difference is small either way."
                    )
                completeness = classify_price_completeness(prices)
                if completeness == "none":
                    base["notes"].append(
                        "No clean bundle match (needs compute_type, operating_system and both "
                        "volume sizes; Included-license SKUs only)."
                    )
                elif completeness == "partial":
                    base["notes"].append(
                        "PARTIAL PRICE LIST DATA: AWS publishes only some rates for this "
                        "storage pairing in this region — the null fields are UNPUBLISHED, "
                        "not $0 and not 'service unavailable'. Use a pairing from "
                        "available_storage_configurations for a complete quote, or state the "
                        "gap explicitly."
                    )
                if completeness != "complete":
                    nearby = list_workspace_bundle_skus(
                        factory, target_region, operating_system, compute_type
                    )
                    if nearby:
                        base["available_storage_configurations"] = nearby
                        base["notes"].append(
                            "available_storage_configurations lists every storage pairing AWS "
                            "prices for this compute/OS in this region with its published "
                            "rates — do NOT present one pairing's price as another's."
                        )
            return base

        def _lookup() -> dict[str, Any]:
            if regions:
                wanted = list(dict.fromkeys(regions))
                return {
                    "service": service,
                    "regions": wanted,
                    "by_region": {r: _lookup_one(r) for r in wanted},
                    "notes": [
                        "Cross-region LIST-price comparison in one call; each region entry "
                        "carries its own assumptions and notes."
                    ],
                }
            return _lookup_one(region or factory.region)

        return await asyncio.to_thread(_lookup)

    mcp.add_tool(get_euc_service_prices, annotations=read_only("EUC service prices"))
