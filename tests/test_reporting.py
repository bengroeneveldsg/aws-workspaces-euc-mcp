# Copyright bengroeneveldsg. Licensed under the Apache License, Version 2.0 (the "License").
# You may not use this file except in compliance with the License.
# A copy of the License is located at http://www.apache.org/licenses/LICENSE-2.0
"""Tests for the reporting & audit tools, using duck-typed fake boto3 clients."""

from __future__ import annotations

import types

from workspaces_euc_mcp_server import consts
from workspaces_euc_mcp_server.tools import reporting


class FakeFactory:
    region = "us-east-1"

    def __init__(self, clients: dict[str, object]) -> None:
        self._clients = clients

    def client(self, service_name: str, region: str | None = None):
        if service_name not in self._clients:
            raise AssertionError(f"unexpected client requested: {service_name}")
        return self._clients[service_name]


def _cloudwatch_by_workspace(values_by_id: dict[str, list[float]]):
    def get_metric_data(**kwargs):
        wid = kwargs["MetricDataQueries"][0]["MetricStat"]["Metric"]["Dimensions"][0]["Value"]
        return {"MetricDataResults": [{"Values": values_by_id.get(wid, [])}]}

    return types.SimpleNamespace(get_metric_data=get_metric_data)


def test_generate_inventory_report_sections():
    workspaces = types.SimpleNamespace(
        describe_workspaces=lambda **_: {
            "Workspaces": [
                {
                    "WorkspaceId": "ws-1",
                    "State": "AVAILABLE",
                    "DirectoryId": "d-1",
                    "BundleId": "wsb-1",
                    "WorkspaceProperties": {
                        "ComputeTypeName": "STANDARD",
                        "RunningMode": "AUTO_STOP",
                    },
                }
            ]
        },
        describe_workspaces_pools=lambda **_: {
            "WorkspacesPools": [{"PoolId": "wsp-1", "PoolName": "pool", "State": "RUNNING"}]
        },
    )
    appstream = types.SimpleNamespace(
        describe_fleets=lambda **_: {
            "Fleets": [{"Name": "f1", "State": "RUNNING", "InstanceType": "stream.standard.medium"}]
        },
        describe_stacks=lambda **_: {"Stacks": [{"Name": "stack-1", "DisplayName": "Stack One"}]},
        list_associated_fleets=lambda **_: {"Names": ["f1"]},
    )
    secure_browser = types.SimpleNamespace(
        list_portals=lambda **_: {
            "portals": [{"portalArn": "arn:portal/1", "displayName": "p", "portalStatus": "Active"}]
        },
    )
    factory = FakeFactory(
        {
            consts.WORKSPACES_API: workspaces,
            consts.APPSTREAM_API: appstream,
            consts.SECURE_BROWSER_API: secure_browser,
        }
    )

    report = reporting.generate_inventory_report_core(factory, "us-east-1")

    assert report.total_resources == 5  # personal, pool, fleet, stack, portal
    by_service = {s.service: s for s in report.sections}
    personal = by_service[consts.PRODUCT_WORKSPACES_PERSONAL].resources[0]
    assert personal.id == "ws-1"
    assert personal.attributes["bundle_id"] == "wsb-1"
    assert by_service[consts.PRODUCT_SECURE_BROWSER].resources[0].id == "arn:portal/1"
    # The Applications stack section lists its associated fleets.
    stack_sections = [s for s in report.sections if s.resource_type == "Stack"]
    assert stack_sections and stack_sections[0].resources[0].id == "stack-1"
    assert stack_sections[0].resources[0].attributes["associated_fleets"] == ["f1"]


def test_audit_flags_unencrypted_and_missing_ip_groups():
    workspaces = types.SimpleNamespace(
        describe_workspaces=lambda **_: {
            "Workspaces": [
                {
                    "WorkspaceId": "ws-enc",
                    "RootVolumeEncryptionEnabled": True,
                    "UserVolumeEncryptionEnabled": True,
                },
                {
                    "WorkspaceId": "ws-plain",
                    "RootVolumeEncryptionEnabled": False,
                    "UserVolumeEncryptionEnabled": False,
                },
            ]
        },
        describe_workspace_directories=lambda **_: {
            "Directories": [
                {"DirectoryId": "d-open", "ipGroupIds": []},
                {"DirectoryId": "d-locked", "ipGroupIds": ["wsipg-1"]},
            ]
        },
    )
    factory = FakeFactory({consts.WORKSPACES_API: workspaces})

    report = reporting.audit_security_posture_core(factory, "us-east-1")

    titles = [f.title for f in report.findings]
    assert any("not encrypted" in t for t in titles)
    assert any("no IP access control groups" in t for t in titles)
    # The encrypted workspace and locked directory produce no findings.
    flagged_ids = {f.resource_id for f in report.findings}
    assert "ws-plain" in flagged_ids
    assert "d-open" in flagged_ids
    assert "ws-enc" not in flagged_ids
    assert report.resources_checked == {"workspaces": 2, "directories": 2}


def test_audit_clean_account_reports_info():
    workspaces = types.SimpleNamespace(
        describe_workspaces=lambda **_: {
            "Workspaces": [
                {
                    "WorkspaceId": "ws-ok",
                    "RootVolumeEncryptionEnabled": True,
                    "UserVolumeEncryptionEnabled": True,
                }
            ]
        },
        describe_workspace_directories=lambda **_: {
            "Directories": [{"DirectoryId": "d-ok", "ipGroupIds": ["wsipg-1"]}]
        },
    )
    factory = FakeFactory({consts.WORKSPACES_API: workspaces})

    report = reporting.audit_security_posture_core(factory, "us-east-1")

    assert report.severity_counts == {"info": 1}


def test_list_unused_resources_combines_sources():
    workspaces = types.SimpleNamespace(
        describe_workspaces=lambda **_: {
            "Workspaces": [
                {"WorkspaceId": "ws-unused", "WorkspaceProperties": {"RunningMode": "ALWAYS_ON"}},
                {"WorkspaceId": "ws-active", "WorkspaceProperties": {"RunningMode": "AUTO_STOP"}},
            ]
        },
    )
    appstream = types.SimpleNamespace(
        describe_fleets=lambda **_: {
            "Fleets": [
                {"Name": "f-stopped", "State": "STOPPED", "ComputeCapacityStatus": {"Desired": 2}},
                {"Name": "f-running", "State": "RUNNING", "ComputeCapacityStatus": {"Desired": 2}},
            ]
        },
    )
    factory = FakeFactory(
        {
            consts.WORKSPACES_API: workspaces,
            consts.APPSTREAM_API: appstream,
            consts.CLOUDWATCH_API: _cloudwatch_by_workspace(
                {"ws-unused": [], "ws-active": [1.0] * 7}
            ),
        }
    )

    report = reporting.list_unused_resources_core(factory, "us-east-1", lookback_days=14)

    ids = {item.id for item in report.items}
    assert ids == {"ws-unused", "f-stopped"}
