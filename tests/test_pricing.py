# Copyright bengroeneveldsg. Licensed under the Apache License, Version 2.0 (the "License").
# You may not use this file except in compliance with the License.
# A copy of the License is located at http://www.apache.org/licenses/LICENSE-2.0
"""Tests for the best-effort EUC price lookups and savings math."""

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


def setup_function(_):
    pricing._cache.clear()
    pricing._generic_cache.clear()


def _row_product(bundle, os_value, lic, rm, storage, unit, usd):
    return json.dumps(
        {
            "product": {
                "productFamily": "Enterprise Applications",
                "attributes": {
                    "bundle": bundle,
                    "operatingSystem": os_value,
                    "license": lic,
                    "runningMode": rm,
                    "storage": storage,
                },
            },
            "terms": {
                "OnDemand": {
                    "t": {"priceDimensions": {"d": {"unit": unit, "pricePerUnit": {"USD": usd}}}}
                }
            },
        }
    )


# Mirrors the REAL Singapore Power-family SKU layout: the base bundle carries the 175/100
# monthly fees plus the compute-level AutoStop hourly (one per OS/license — storage variants
# do NOT get their own hourly); the "-0" variant bundle carries the 80/10 monthly fees.
_POWER_FAMILY = {
    "Power": [
        ("Windows", "Included", "AlwaysOn", "Root:175 GB,User:100 GB", "Month", "124.0"),
        ("Windows", "Included", "AutoStop", "Root:175 GB,User:100 GB", "Hour", "0.99"),
        ("Windows", "Included", "AutoStop", "Root:175 GB,User:100 GB", "Month", "26.0"),
        ("Amazon Linux", "None", "AutoStop", "Root:175 GB,User:100 GB", "Hour", "0.95"),
        (
            "Windows",
            "Bring Your Own License",
            "AlwaysOn",
            "Root:175 GB,User:100 GB",
            "Month",
            "120.0",
        ),
        ("Any", "Bring Your Own License", "AutoStop", "Root:175 GB,User:100 GB", "Hour", "0.95"),
        (
            "Windows",
            "Bring Your Own License",
            "AutoStop",
            "Root:175 GB,User:100 GB",
            "Month",
            "26.0",
        ),
        ("Windows", "Included", "AlwaysOn", "Root:175 GB,User:100 GB", "Month", "0"),  # $0 SW row
    ],
    "Power-0": [
        ("Windows", "Included", "AlwaysOn", "Root:80 GB,User:10 GB", "Month", "116.0"),
        ("Windows", "Included", "AutoStop", "Root:80 GB,User:10 GB", "Month", "10.0"),
        ("Amazon Linux", "None", "AlwaysOn", "Root:80 GB,User:10 GB", "Month", "112.0"),
        ("Amazon Linux", "None", "AutoStop", "Root:80 GB,User:10 GB", "Month", "10.0"),
    ],
}


def _fake_family_pricing(captured=None):
    def get_products(**kwargs):
        if captured is not None:
            captured.append(kwargs["Filters"])
        by_field = {f["Field"]: f["Value"] for f in kwargs["Filters"]}
        rows = _POWER_FAMILY.get(by_field.get("bundle", ""), [])
        return {"PriceList": [_row_product(by_field["bundle"], *r) for r in rows]}

    return types.SimpleNamespace(get_products=get_products)


def test_get_workspace_prices_base_bundle_storage():
    factory = FakeFactory({"pricing": _fake_family_pricing()})
    p = pricing.get_workspace_prices(
        factory, "ap-southeast-1", "WINDOWS_SERVER_2025", "POWER", 175, 100
    )
    assert p == pricing.WorkspacePrices(124.0, 26.0, 0.99)


def test_get_workspace_prices_variant_bundle_storage():
    # Regression: 80/10 monthly fees live under the "Power-0" VARIANT bundle; the hourly is
    # compute-level from the base bundle — v0.1.21 wrongly concluded AWS doesn't publish them.
    factory = FakeFactory({"pricing": _fake_family_pricing()})
    p = pricing.get_workspace_prices(
        factory, "ap-southeast-1", "WINDOWS_SERVER_2025", "POWER", 80, 10
    )
    assert p == pricing.WorkspacePrices(116.0, 10.0, 0.99)  # hourly inherited from the family


def test_get_workspace_prices_windows11_selects_byol_rows():
    # BYOL rows carry os Windows OR Any; selection is client-side, never server-filtered.
    factory = FakeFactory({"pricing": _fake_family_pricing()})
    p = pricing.get_workspace_prices(factory, "ap-southeast-1", "WINDOWS_11", "POWER", 175, 100)
    assert p == pricing.WorkspacePrices(120.0, 26.0, 0.95)  # not the Included 124/0.99


def test_get_workspace_prices_linux_uses_license_none():
    # Amazon Linux SKUs carry license="None" (not "Included") and a real OS value (not
    # "Linux") — both were wrong before the ground-truth rework.
    factory = FakeFactory({"pricing": _fake_family_pricing()})
    p = pricing.get_workspace_prices(factory, "ap-southeast-1", "AMAZON_LINUX_2", "POWER", 80, 10)
    assert p == pricing.WorkspacePrices(112.0, 10.0, 0.95)  # AL hourly from the family rows


def test_family_query_filters_product_family_not_os_or_license():
    captured: list[list[dict]] = []
    factory = FakeFactory({"pricing": _fake_family_pricing(captured)})
    pricing.get_workspace_prices(factory, "ap-southeast-1", "WINDOWS_11", "POWER", 175, 100)

    bundles = set()
    for filters in captured:
        by_field = {f["Field"]: f["Value"] for f in filters}
        # productFamily is load-bearing: the same bundle names exist under WorkSpaces Core.
        assert by_field["productFamily"] == "Enterprise Applications"
        assert "operatingSystem" not in by_field
        assert "license" not in by_field
        bundles.add(by_field["bundle"])
    assert {"Power", "Power-0", "Power-1", "Power-2", "Power-3"} <= bundles


def test_get_workspace_prices_none_when_unmatched_region():
    factory = FakeFactory({"pricing": _fake_family_pricing()})
    assert pricing.get_workspace_prices(factory, "mars-1", "Windows", "POWER", 175, 100) is None


def test_get_workspace_prices_all_none_when_storage_unmatched():
    factory = FakeFactory({"pricing": _fake_family_pricing()})
    p = pricing.get_workspace_prices(factory, "ap-southeast-1", "WINDOWS_11", "POWER", 999, 999)
    assert p is not None
    assert all(v is None for v in p)


def test_bundle_sku_listing_spans_variant_bundles():
    factory = FakeFactory({"pricing": _fake_family_pricing()})
    skus = pricing.list_workspace_bundle_skus(
        factory, "ap-southeast-1", "WINDOWS_SERVER_2025", "POWER"
    )
    by_storage = {s["storage"]: s for s in skus}
    assert by_storage["Root:175 GB,User:100 GB"]["alwayson_monthly_usd"] == 124.0
    # The 80/10 pairing is COMPLETE now: monthly fees from Power-0, hourly inherited from
    # the family (the hourly rate is compute-level, not per-storage).
    assert by_storage["Root:80 GB,User:10 GB"] == {
        "storage": "Root:80 GB,User:10 GB",
        "alwayson_monthly_usd": 116.0,
        "autostop_monthly_base_usd": 10.0,
        "autostop_hourly_usd": 0.99,
    }


def test_bundle_sku_listing_empty_for_unknown_region():
    factory = FakeFactory({"pricing": _fake_family_pricing()})
    assert pricing.list_workspace_bundle_skus(factory, "mars-1", "WINDOWS_11", "POWER") == []


def test_personal_license_model_detection():
    assert pricing.personal_license_model("WINDOWS_11") == "Bring Your Own License"
    assert pricing.personal_license_model("BYOL_Windows1123H2") == "Bring Your Own License"
    assert pricing.personal_license_model("WINDOWS_SERVER_2025") == "Included"
    assert pricing.personal_license_model("RHEL_8") == "Included"
    assert pricing.personal_license_model("AMAZON_LINUX_2") == "None"
    assert pricing.personal_license_model("UBUNTU_2204") == "None"
    assert pricing.personal_license_model(None) == "Included"


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
    prices = pricing.WorkspacePrices(
        alwayson_monthly=124.0, autostop_monthly_base=26.0, autostop_hourly=0.99
    )
    assert pricing.autostop_breakeven_hours(prices) == 99.0


def test_autostop_breakeven_none_when_rates_missing():
    assert pricing.autostop_breakeven_hours(None) is None
    assert pricing.autostop_breakeven_hours(pricing.WorkspacePrices(124.0, 26.0, None)) is None
    assert pricing.autostop_breakeven_hours(pricing.WorkspacePrices(124.0, 26.0, 0.0)) is None
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
    partial = pricing.WorkspacePrices(None, None, 0.95)
    empty = pricing.WorkspacePrices(None, None, None)
    assert pricing.classify_price_completeness(full) == "complete"
    assert pricing.classify_price_completeness(partial) == "partial"
    assert pricing.classify_price_completeness(empty) == "none"
    assert pricing.classify_price_completeness(None) == "none"


def test_appstream_platform_to_os_mapping():
    f = pricing._appstream_os_for_platform
    assert f("WINDOWS_SERVER_2025") == "Windows"
    assert f("WINDOWS_11") == "Windows BYOL"
    assert f("WINDOWS_10") == "Windows BYOL"
    assert f("UBUNTU_PRO_22_04") == "Ubuntu Pro"
    assert f("AMAZON_LINUX2") == "Amazon Linux"
    assert f("AMAZON_LINUX2023") == "Amazon Linux"  # substring match survives enum revisions
    assert f("RHEL8") == "Red Hat Enterprise Linux"
    assert f("ROCKY_LINUX8") == "Rocky Linux"
    assert f(None) == "Windows"


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
    assert pricing.appstream_stopped_instance_fee(factory, "mars-1") is None


def test_appstream_user_fees_by_usagetype():
    def _fee_product(usagetype, unit, usd):
        return json.dumps(
            {
                "product": {"attributes": {"usagetype": usagetype}},
                "terms": {
                    "OnDemand": {
                        "t": {
                            "priceDimensions": {"d": {"unit": unit, "pricePerUnit": {"USD": usd}}}
                        }
                    }
                },
            }
        )

    def get_products(**kwargs):
        by_field = {f["Field"]: f["Value"] for f in kwargs["Filters"]}
        assert by_field["productFamily"] == "User Fees"
        return {
            "PriceList": [
                _fee_product("APS1-Win-User", "users", "4.19"),
                _fee_product("APS1-Win-User-Multi-Session", "Month", "6.42"),
                _fee_product("APS1-Win-User-Multi-Session-Additional", "Month", "2.23"),
            ]
        }

    factory = FakeFactory({"pricing": types.SimpleNamespace(get_products=get_products)})
    fees = pricing.appstream_user_fees(factory, "ap-southeast-1")
    assert fees == {
        "single_session_user_monthly_usd": 4.19,
        "multi_session_user_monthly_usd": 6.42,
        "multi_session_additional_user_monthly_usd": 2.23,
    }


def test_workspaces_pool_rates():
    def _pool_product(bundle, lic, rm, unit, usd):
        return json.dumps(
            {
                "product": {"attributes": {"bundle": bundle, "license": lic, "runningMode": rm}},
                "terms": {
                    "OnDemand": {
                        "t": {
                            "priceDimensions": {"d": {"unit": unit, "pricePerUnit": {"USD": usd}}}
                        }
                    }
                },
            }
        )

    def get_products(**kwargs):
        by_field = {f["Field"]: f["Value"] for f in kwargs["Filters"]}
        if by_field.get("runningMode") == "Pool":
            assert by_field["bundle"] == "Power"
            return {
                "PriceList": [
                    _pool_product("Power", "Included", "Pool", "hour", "0.48"),
                    _pool_product("Power", "Bring Your Own License", "Pool", "hour", "0.417"),
                ]
            }
        if by_field.get("bundle") == "Stopped Instance":
            return {
                "PriceList": [
                    _pool_product(
                        "Stopped Instance", "Included", "Not Applicable Pools", "hour", "0.025"
                    )
                ]
            }
        if by_field.get("bundle") == "User Fee":
            return {
                "PriceList": [
                    _pool_product("User Fee", "Included", "Not Applicable Pools", "Month", "4.19")
                ]
            }
        return {"PriceList": []}

    factory = FakeFactory({"pricing": types.SimpleNamespace(get_products=get_products)})
    rates = pricing.workspaces_pool_rates(factory, "ap-southeast-1", "POWER")
    assert rates == {
        "streaming_hourly_included_usd": 0.48,
        "streaming_hourly_byol_usd": 0.417,
        "stopped_instance_hourly_usd": 0.025,
        "user_fee_monthly_usd": 4.19,
    }


def test_resolve_fleet_pricing_inputs_gets_platform_from_image():
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
    assert resolved["platform"] == "WINDOWS_11"
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
    assert resolved["platform"] == "WINDOWS_SERVER_2022"
    assert resolved["instance_function"] == "ElasticFleet"
