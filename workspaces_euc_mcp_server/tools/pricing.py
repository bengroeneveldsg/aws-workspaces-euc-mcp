# Copyright bengroeneveldsg. Licensed under the Apache License, Version 2.0 (the "License").
# You may not use this file except in compliance with the License.
# A copy of the License is located at http://www.apache.org/licenses/LICENSE-2.0
"""Best-effort WorkSpaces price lookups (AWS Price List API) for savings estimates.

The Price List API is messy, so this is deliberately conservative: it matches the canonical
Included-license hardware SKU for a given region / OS / compute type / volume sizes and returns the
AlwaysOn monthly price plus the AutoStop monthly-base and hourly prices. Anything it cannot match
cleanly returns None, so callers degrade to "no estimate" rather than a wrong number.

Needs ``pricing:GetProducts`` (IAM Tier 1). The pricing client is global (queried from us-east-1).
"""

from __future__ import annotations

import json
from typing import NamedTuple

from loguru import logger

from ..clients import ClientFactory

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
