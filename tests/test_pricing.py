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


def test_autostop_breakeven_hours():
    # Power SIN: AlwaysOn 124, AutoStop 26 + 0.99/h -> (124-26)/0.99 = 99.0 hrs/month.
    # A 100 hrs/month user sits right at the tipping point (the live-validation case).
    prices = pricing.WorkspacePrices(
        alwayson_monthly=124.0, autostop_monthly_base=26.0, autostop_hourly=0.99
    )
    assert pricing.autostop_breakeven_hours(prices) == 99.0


def test_autostop_breakeven_none_when_rates_missing():
    assert pricing.autostop_breakeven_hours(None) is None
    assert pricing.autostop_breakeven_hours(pricing.WorkspacePrices(124.0, 26.0, None)) is None
    # Zero hourly must not divide-by-zero.
    assert pricing.autostop_breakeven_hours(pricing.WorkspacePrices(124.0, 26.0, 0.0)) is None
    # Base above AlwaysOn (nonsense data) -> no breakeven rather than a negative number.
    assert pricing.autostop_breakeven_hours(pricing.WorkspacePrices(20.0, 26.0, 0.99)) is None


def test_iter_price_list_follows_pagination():
    pages = {
        None: {"PriceList": ["a", "b"], "NextToken": "t1"},
        "t1": {"PriceList": ["c"], "NextToken": "t2"},
        "t2": {"PriceList": ["d"]},  # no NextToken -> stop
    }

    def get_products(**kwargs):
        return pages[kwargs.get("NextToken")]

    client = types.SimpleNamespace(get_products=get_products)
    got = list(pricing._iter_price_list(client, "AmazonWorkSpaces", []))
    assert got == ["a", "b", "c", "d"]


def test_classify_price_completeness():
    full = pricing.WorkspacePrices(124.0, 26.0, 0.99)
    partial = pricing.WorkspacePrices(None, None, 0.95)  # the Power 80/10 SIN case
    empty = pricing.WorkspacePrices(None, None, None)
    assert pricing.classify_price_completeness(full) == "complete"
    assert pricing.classify_price_completeness(partial) == "partial"
    assert pricing.classify_price_completeness(empty) == "none"
    assert pricing.classify_price_completeness(None) == "none"


def test_appstream_os_rate_options_lists_variants():
    def _aps_product(os_value, usd):
        return json.dumps(
            {
                "product": {"attributes": {"operatingSystem": os_value}},
                "terms": {
                    "OnDemand": {
                        "t": {
                            "priceDimensions": {
                                "d": {"unit": "Hours", "pricePerUnit": {"USD": usd}}
                            }
                        }
                    }
                },
            }
        )

    def get_products(**kwargs):
        fields = {f["Field"] for f in kwargs["Filters"]}
        assert "operatingSystem" not in fields  # discovery query must NOT pin the OS
        return {
            "PriceList": [
                _aps_product("Windows", "0.24"),
                _aps_product("Windows BYOL", "0.217"),
                _aps_product("Amazon Linux", "0.217"),
            ]
        }

    factory = FakeFactory({"pricing": types.SimpleNamespace(get_products=get_products)})
    options = pricing.appstream_os_rate_options(
        factory, "ap-southeast-1", "stream.standard.large", "Fleet"
    )
    assert options == {"Windows": 0.24, "Windows BYOL": 0.217, "Amazon Linux": 0.217}


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


def test_appstream_stopped_instance_fee_parsed():
    stopped_product = json.dumps(
        {
            "product": {"attributes": {"instanceFunction": "StoppedFleetInstance"}},
            "terms": {
                "OnDemand": {
                    "t": {
                        "priceDimensions": {
                            "d": {"unit": "Hours", "pricePerUnit": {"USD": "0.025"}}
                        }
                    }
                }
            },
        }
    )

    def get_products(**kwargs):
        funcs = [f["Value"] for f in kwargs["Filters"] if f["Field"] == "instanceFunction"]
        assert funcs == ["StoppedFleetInstance"]
        return {"PriceList": [stopped_product]}

    factory = FakeFactory({"pricing": types.SimpleNamespace(get_products=get_products)})
    assert pricing.appstream_stopped_instance_fee(factory, "ap-southeast-1") == 0.025
    # Unknown region -> no location -> None without calling pricing.
    assert pricing.appstream_stopped_instance_fee(factory, "mars-1") is None


def test_resolve_fleet_pricing_inputs_gets_platform_from_image():
    # DescribeFleets omits Platform for non-Elastic fleets — the resolver must follow the
    # fleet's image to find WINDOWS_11 (BYOL rate), the regression from live validation.
    appstream = types.SimpleNamespace(
        describe_fleets=lambda **kw: {
            "Fleets": [
                {
                    "Name": kw["Names"][0],
                    "FleetType": "ON_DEMAND",
                    "InstanceType": "stream.standard.large",
                    "ImageArn": "arn:aws:appstream:ap-southeast-1:1:image/Windows_11_25H2",
                    "ComputeCapacityStatus": {"Desired": 2},
                }
            ]
        },
        describe_images=lambda **kw: {"Images": [{"Platform": "WINDOWS_11"}]},
    )
    factory = FakeFactory({"appstream": appstream})

    resolved = pricing.resolve_fleet_pricing_inputs(factory, "ap-southeast-1", "windows-11-fleet")

    assert resolved is not None
    assert resolved["platform"] == "WINDOWS_11"  # -> BYOL SKU, $0.217 not $0.24
    assert resolved["instance_type"] == "stream.standard.large"
    assert resolved["fleet_type"] == "ON_DEMAND"
    assert resolved["instance_function"] == "Fleet"
    assert resolved["desired_capacity"] == 2


def test_resolve_fleet_pricing_inputs_none_for_missing_fleet():
    appstream = types.SimpleNamespace(describe_fleets=lambda **kw: {"Fleets": []})
    factory = FakeFactory({"appstream": appstream})
    assert pricing.resolve_fleet_pricing_inputs(factory, "ap-southeast-1", "nope") is None


def test_resolve_fleet_elastic_uses_fleet_platform_and_function():
    appstream = types.SimpleNamespace(
        describe_fleets=lambda **kw: {
            "Fleets": [
                {
                    "Name": kw["Names"][0],
                    "FleetType": "ELASTIC",
                    "Platform": "WINDOWS_SERVER_2022",
                    "InstanceType": "stream.standard.large",
                }
            ]
        },
    )
    factory = FakeFactory({"appstream": appstream})

    resolved = pricing.resolve_fleet_pricing_inputs(factory, "ap-southeast-1", "el")

    assert resolved is not None
    assert resolved["platform"] == "WINDOWS_SERVER_2022"  # no image call needed
    assert resolved["instance_function"] == "ElasticFleet"


def test_get_workspace_prices_all_none_when_storage_unmatched():
    # Query succeeds but no SKU carries the requested pairing -> truthy tuple of Nones,
    # which is the case the tool-level near-miss fallback keys off.
    factory = FakeFactory({"pricing": _fake_pricing()})
    p = pricing.get_workspace_prices(factory, "ap-southeast-1", "WINDOWS_11", "POWER", 80, 50)
    assert p is not None
    assert all(v is None for v in p)
