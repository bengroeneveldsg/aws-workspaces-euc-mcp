# Copyright bengroeneveldsg. Licensed under the Apache License, Version 2.0 (the "License").
# You may not use this file except in compliance with the License.
# A copy of the License is located at http://www.apache.org/licenses/LICENSE-2.0
"""Tests for the best-effort WorkSpaces price lookups and savings math."""

from __future__ import annotations

import json
import types

from workspaces_euc_mcp_server.tools import pricing


class FakeFactory:
    region = "ap-southeast-1"

    def __init__(self, clients):
        self._clients = clients

    def client(self, service_name, region=None):
        if service_name not in self._clients:
            raise AssertionError(f"unexpected client requested: {service_name}")
        return self._clients[service_name]


def _product(bundle, storage, running_mode, unit, usd, usagetype):
    return json.dumps(
        {
            "product": {
                "attributes": {
                    "bundle": bundle,
                    "storage": storage,
                    "runningMode": running_mode,
                    "usagetype": usagetype,
                }
            },
            "terms": {
                "OnDemand": {
                    "t": {"priceDimensions": {"d": {"unit": unit, "pricePerUnit": {"USD": usd}}}}
                }
            },
        }
    )


def _fake_pricing(storage="Root:175 GB,User:100 GB"):
    def get_products(**kwargs):
        rm = next(f["Value"] for f in kwargs["Filters"] if f["Field"] == "runningMode")
        if rm == "AlwaysOn":
            return {
                "PriceList": [
                    _product("Power", storage, "AlwaysOn", "Month", "0", "SW"),  # ignored ($0)
                    _product("Power", storage, "AlwaysOn", "Month", "124.0", "AW-HW-8"),
                ]
            }
        return {
            "PriceList": [
                _product("Power", storage, "AutoStop", "Hour", "0.99", "AW-HW-8-AutoStop-Usage"),
                _product("Power", storage, "AutoStop", "Month", "26.0", "AW-HW-8-AutoStop-User"),
            ]
        }

    return types.SimpleNamespace(get_products=get_products)


def setup_function(_):
    pricing._cache.clear()
    pricing._generic_cache.clear()


def test_get_workspace_prices_parses_alwayson_and_autostop():
    factory = FakeFactory({"pricing": _fake_pricing()})
    p = pricing.get_workspace_prices(
        factory, "ap-southeast-1", "WINDOWS_SERVER_2025", "POWER", 175, 100
    )
    assert p is not None
    assert p.alwayson_monthly == 124.0
    assert p.autostop_monthly_base == 26.0
    assert p.autostop_hourly == 0.99


def test_get_workspace_prices_none_when_unmatched_region():
    factory = FakeFactory({"pricing": _fake_pricing()})
    # Unknown region -> no location -> None, without even calling pricing.
    assert pricing.get_workspace_prices(factory, "mars-1", "Windows", "POWER", 175, 100) is None


def test_savings_for_unused_alwayson_desktop():
    # Power: AlwaysOn 124/mo; AutoStop base 26 + 0.99/h. Unused (0 active days) -> ~124-26 = 98.
    prices = pricing.WorkspacePrices(
        alwayson_monthly=124.0, autostop_monthly_base=26.0, autostop_hourly=0.99
    )
    saving = pricing.estimate_alwayson_to_autostop_savings(prices, active_days=0, lookback_days=14)
    assert saving == 98.0


def test_savings_none_when_no_prices():
    assert pricing.estimate_alwayson_to_autostop_savings(None, 0, 14) is None


def test_bundle_sku_listing_groups_by_storage():
    # The Price List fake only knows Root:175/User:100 SKUs; the listing should surface that
    # pairing with all three price points, regardless of what storage the caller wanted.
    factory = FakeFactory({"pricing": _fake_pricing()})
    skus = pricing.list_workspace_bundle_skus(factory, "ap-southeast-1", "WINDOWS_11", "POWER")

    assert len(skus) == 1
    assert skus[0]["storage"] == "Root:175 GB,User:100 GB"
    assert skus[0]["alwayson_monthly_usd"] == 124.0
    assert skus[0]["autostop_monthly_base_usd"] == 26.0
    assert skus[0]["autostop_hourly_usd"] == 0.99


def test_bundle_sku_listing_empty_for_unknown_region():
    factory = FakeFactory({"pricing": _fake_pricing()})
    assert pricing.list_workspace_bundle_skus(factory, "mars-1", "WINDOWS_11", "POWER") == []


def test_get_workspace_prices_all_none_when_storage_unmatched():
    # Query succeeds but no SKU carries the requested pairing -> truthy tuple of Nones,
    # which is the case the tool-level near-miss fallback keys off.
    factory = FakeFactory({"pricing": _fake_pricing()})
    p = pricing.get_workspace_prices(factory, "ap-southeast-1", "WINDOWS_11", "POWER", 80, 50)
    assert p is not None
    assert all(v is None for v in p)
