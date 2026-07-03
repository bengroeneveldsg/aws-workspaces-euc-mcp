# Copyright bengroeneveldsg. Licensed under the Apache License, Version 2.0 (the "License").
# You may not use this file except in compliance with the License.
# A copy of the License is located at http://www.apache.org/licenses/LICENSE-2.0
"""Tests for large-output handling (caps/CSV export), policy values, and the health report."""

from __future__ import annotations

import csv
import types
from pathlib import Path

from workspaces_euc_mcp_server import consts
from workspaces_euc_mcp_server.tools import health, reporting, secure_browser


class FakeFactory:
    region = "us-east-1"

    def __init__(self, clients: dict[str, object]) -> None:
        self._clients = clients

    def client(self, service_name: str, region: str | None = None):
        if service_name not in self._clients:
            raise AssertionError(f"unexpected client requested: {service_name}")
        return self._clients[service_name]


def _big_estate_factory(n_desktops: int) -> FakeFactory:
    workspaces = types.SimpleNamespace(
        describe_workspaces=lambda **_: {
            "Workspaces": [
                {"WorkspaceId": f"ws-{i:06d}", "State": "AVAILABLE", "BundleId": "wsb-1"}
                for i in range(n_desktops)
            ]
        },
        describe_workspace_bundles=lambda **kw: {
            "Bundles": [{"BundleId": b, "Name": "Std"} for b in kw.get("BundleIds", [])]
        },
        describe_workspaces_pools=lambda **_: {"WorkspacesPools": []},
    )
    appstream = types.SimpleNamespace(
        describe_fleets=lambda **_: {"Fleets": []},
        describe_stacks=lambda **_: {"Stacks": []},
    )
    web = types.SimpleNamespace(list_portals=lambda **_: {"portals": []})
    instances = types.SimpleNamespace(
        list_workspace_instances=lambda **_: {"WorkspaceInstances": []}
    )
    return FakeFactory(
        {
            consts.WORKSPACES_API: workspaces,
            consts.APPSTREAM_API: appstream,
            consts.SECURE_BROWSER_API: web,
            consts.WORKSPACES_INSTANCES_API: instances,
        }
    )


def test_inventory_report_caps_large_sections_and_flags_truncation():
    factory = _big_estate_factory(250)

    report = reporting.generate_inventory_report_core(
        factory, "us-east-1", max_resources_per_service=100
    )

    personal = report.sections[0]
    assert personal.total_count == 250  # real total preserved
    assert len(personal.resources) == 100  # capped
    assert personal.truncated is True
    assert report.total_resources == 250
    assert any("export_inventory_report_csv" in n for n in report.notes)


def test_inventory_report_service_filter_limits_sections():
    factory = _big_estate_factory(3)

    report = reporting.generate_inventory_report_core(factory, "us-east-1", services="personal")

    assert [s.resource_type for s in report.sections] == ["WorkSpace"]


def test_csv_export_writes_full_inventory(tmp_path: Path):
    factory = _big_estate_factory(250)
    out = tmp_path / "inv.csv"

    result = reporting.export_inventory_report_csv_core(factory, "us-east-1", path=str(out))

    assert result.path == str(out)
    assert result.rows == 250  # FULL list, not capped
    with out.open(encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 250
    assert rows[0]["id"] == "ws-000000"
    assert rows[0]["bundle_name"] == "Std"


def test_browser_policy_includes_size_guarded_values():
    summary = secure_browser._summarize_browser_policy(
        '{"chromePolicies": {'
        '"ProxyServer": {"value": "proxy.corp.example:8080"}, '
        '"ExtensionInstallForcelist": {"value": ["'
        + '", "'.join(f"ext-{i}" for i in range(30))
        + '"]}}}'
    )

    assert summary["policies"]["ProxyServer"] == "proxy.corp.example:8080"
    forcelist = summary["policies"]["ExtensionInstallForcelist"]
    assert len(forcelist) == 21  # 20 items + truncation marker
    assert forcelist[-1] == "... (+10 more)"


def test_health_report_composes_sections_and_markdown(monkeypatch):
    from workspaces_euc_mcp_server.models import (
        ActiveAlarm,
        ActiveAlarmsReport,
        ApplicationImageAuditReport,
        AuditReport,
        CostForecast,
        CostLineItem,
        CostSummary,
        EucInventorySummary,
        Finding,
        Recommendation,
        RecommendationReport,
        ServiceInventory,
        ServiceQuotaReport,
        WorkspaceImageAuditReport,
    )

    monkeypatch.setattr(
        health,
        "collect_inventory",
        lambda f, r: EucInventorySummary(
            region=r,
            total_resources=12,
            services=[
                ServiceInventory(
                    service="Amazon WorkSpaces Personal",
                    resource_type="WorkSpace",
                    count=12,
                    by_state={"AVAILABLE": 3, "STOPPED": 9},
                )
            ],
        ),
    )
    monkeypatch.setattr(
        health,
        "get_euc_active_alarms_core",
        lambda f, r: ActiveAlarmsReport(
            region=r,
            total_account_alarms_in_alarm=2,
            euc_alarms_in_alarm=2,
            alarms=[
                ActiveAlarm(name="real-alarm", service="Amazon WorkSpaces"),
                ActiveAlarm(name="scale-in", service="Amazon WorkSpaces", likely_autoscaling=True),
            ],
        ),
    )
    monkeypatch.setattr(
        health,
        "get_euc_cost_summary_core",
        lambda f, lookback_days: CostSummary(
            start="s",
            end="e",
            granularity="MONTHLY",
            total=1921.12,
            by_service=[CostLineItem(service="Amazon WorkSpaces", amount=720.44)],
            workspaces_breakdown={"WorkSpaces Personal": 550.0},
        ),
    )
    monkeypatch.setattr(
        health,
        "get_euc_cost_forecast_core",
        lambda f, days_ahead: CostForecast(
            start="s", end="e", granularity="MONTHLY", forecast_total=1881.01
        ),
    )
    monkeypatch.setattr(
        health,
        "recommend_running_mode_core",
        lambda f, r, d: RecommendationReport(
            region=r,
            lookback_days=d,
            recommendations=[
                Recommendation(
                    target_id="ws-idle",
                    kind="running_mode",
                    current="ALWAYS_ON",
                    recommended="AUTO_STOP",
                    rationale="idle",
                    estimated_monthly_savings_usd=63.0,
                    confidence="high",
                )
            ],
        ),
    )
    monkeypatch.setattr(
        health,
        "audit_security_posture_core",
        lambda f, r: AuditReport(
            region=r,
            findings=[
                Finding(severity="warning", title="WorkSpace volumes not encrypted", detail="d")
            ],
            severity_counts={"warning": 1},
        ),
    )
    monkeypatch.setattr(
        health,
        "audit_application_images_core",
        lambda f, r: ApplicationImageAuditReport(region=r, running_image_builders=1),
    )
    monkeypatch.setattr(
        health,
        "audit_workspace_images_core",
        lambda f, r: WorkspaceImageAuditReport(region=r),
    )
    monkeypatch.setattr(
        health,
        "get_euc_service_quotas_core",
        lambda f, r: ServiceQuotaReport(region=r),
    )

    report = health.generate_euc_health_report_core(None, "us-east-1")

    assert report.total_resources == 12
    assert report.alarms_firing == 1  # autoscaling alarm excluded
    assert report.audit_warnings == 1
    assert report.monthly_spend == 1921.12
    assert report.forecast_next_30d == 1881.01
    assert report.estimated_monthly_savings == 63.0
    md = report.markdown
    assert "# EUC Health Report" in md
    assert "Amazon WorkSpaces Personal" in md
    assert "real-alarm" in md
    assert "ws-idle" in md
    assert "$1,921.12" in md


def test_appstream_pricing_maps_platform_to_license(monkeypatch):
    from workspaces_euc_mcp_server.tools import pricing as pr

    captured = []

    class FakePricing:
        def get_products(self, **kw):
            captured.append(kw["Filters"])
            return {
                "PriceList": [
                    '{"product": {"attributes": {}}, "terms": {"OnDemand": {"a": '
                    '{"priceDimensions": {"b": {"unit": "hour", '
                    '"pricePerUnit": {"USD": "0.217"}}}}}}}'
                ]
            }

    class F:
        region = "ap-southeast-1"

        def client(self, name, region=None):
            return FakePricing()

    pr._generic_cache.clear()
    rate = pr.appstream_hourly_price(
        F(), "ap-southeast-1", "stream.standard.large", "ImageBuilder", "WINDOWS_11"
    )
    assert rate == 0.217
    os_filter = [f for f in captured[0] if f["Field"] == "operatingSystem"][0]
    assert os_filter["Value"] == "Windows BYOL"  # Windows 11 => BYOL SKU

    pr._generic_cache.clear()
    captured.clear()
    pr.appstream_hourly_price(
        F(), "ap-southeast-1", "stream.memory.xlarge", "ImageBuilder", "WINDOWS_SERVER_2025"
    )
    os_filter = [f for f in captured[0] if f["Field"] == "operatingSystem"][0]
    assert os_filter["Value"] == "Windows"  # Server OS => included license


def test_secure_browser_mau_tier_mapping():
    from workspaces_euc_mcp_server.tools import pricing as pr

    class FakePricing:
        def get_products(self, **kw):
            def sku(ut, usd):
                import json as _json

                return _json.dumps(
                    {
                        "product": {"attributes": {"usagetype": ut}},
                        "terms": {
                            "OnDemand": {
                                "a": {
                                    "priceDimensions": {
                                        "b": {"unit": "MAU", "pricePerUnit": {"USD": usd}}
                                    }
                                }
                            }
                        },
                    }
                )

            return {
                "PriceList": [
                    sku("APS1-WORKSPACES-WEB-ST", "8"),
                    sku("APS1-WORKSPACES-WEB-ST-LARGE", "23"),
                    sku("APS1-WORKSPACES-WEB-ST-XLARGE", "40"),
                ]
            }

    class F:
        region = "ap-southeast-1"

        def client(self, name, region=None):
            return FakePricing()

    pr._generic_cache.clear()
    tiers = pr.secure_browser_mau_prices(F(), "ap-southeast-1")
    assert tiers == {
        "standard.regular": 8.0,
        "standard.large": 23.0,
        "standard.xlarge": 40.0,
    }
