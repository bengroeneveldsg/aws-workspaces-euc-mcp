# Copyright bengroeneveldsg. Licensed under the Apache License, Version 2.0 (the "License").
# You may not use this file except in compliance with the License.
# A copy of the License is located at http://www.apache.org/licenses/LICENSE-2.0
"""Tests for the diagnostics tools, using duck-typed fake boto3 clients."""

from __future__ import annotations

import types

from workspaces_euc_mcp_server import consts
from workspaces_euc_mcp_server.tools import diagnostics


class FakeFactory:
    region = "us-east-1"

    def __init__(self, clients: dict[str, object]) -> None:
        self._clients = clients

    def client(self, service_name: str, region: str | None = None):
        if service_name not in self._clients:
            raise AssertionError(f"unexpected client requested: {service_name}")
        return self._clients[service_name]


def _fake_cloudwatch(values_by_metric: dict[str, float]):
    def get_metric_data(**kwargs):
        name = kwargs["MetricDataQueries"][0]["MetricStat"]["Metric"]["MetricName"]
        return {"MetricDataResults": [{"Values": [values_by_metric.get(name, 0.0)]}]}

    return types.SimpleNamespace(get_metric_data=get_metric_data)


def _healthy_directory_clients():
    workspaces = types.SimpleNamespace(
        describe_workspace_directories=lambda **_: {
            "Directories": [{"DirectoryId": "d-123", "State": "REGISTERED"}]
        },
    )
    ds = types.SimpleNamespace(
        describe_directories=lambda **_: {
            "DirectoryDescriptions": [{"DirectoryId": "d-123", "Stage": "Active"}]
        },
    )
    return workspaces, ds


def test_workspace_connectivity_healthy():
    ws_dir, ds = _healthy_directory_clients()
    workspaces = types.SimpleNamespace(
        describe_workspaces=lambda **_: {
            "Workspaces": [
                {
                    "WorkspaceId": "ws-1",
                    "State": "AVAILABLE",
                    "DirectoryId": "d-123",
                    "WorkspaceProperties": {
                        "ComputeTypeName": "STANDARD",
                        "RunningMode": "AUTO_STOP",
                    },
                }
            ]
        },
        describe_workspaces_connection_status=lambda **_: {
            "WorkspacesConnectionStatus": [{"ConnectionState": "CONNECTED"}]
        },
        describe_workspace_directories=ws_dir.describe_workspace_directories,
    )
    factory = FakeFactory(
        {
            consts.WORKSPACES_API: workspaces,
            consts.DIRECTORY_API: ds,
            consts.CLOUDWATCH_API: _fake_cloudwatch({"ConnectionFailure": 0.0}),
        }
    )

    diag = diagnostics.diagnose_workspace_connectivity_core(factory, "ws-1", "us-east-1")

    assert diag.status == "healthy"
    assert diag.signals["state"] == "AVAILABLE"
    assert not any(f.severity == "critical" for f in diag.findings)


def test_workspace_connectivity_unhealthy_state():
    ws_dir, ds = _healthy_directory_clients()
    workspaces = types.SimpleNamespace(
        describe_workspaces=lambda **_: {
            "Workspaces": [{"WorkspaceId": "ws-2", "State": "UNHEALTHY", "DirectoryId": "d-123"}]
        },
        describe_workspaces_connection_status=lambda **_: {"WorkspacesConnectionStatus": []},
        describe_workspace_directories=ws_dir.describe_workspace_directories,
    )
    factory = FakeFactory(
        {
            consts.WORKSPACES_API: workspaces,
            consts.DIRECTORY_API: ds,
            consts.CLOUDWATCH_API: _fake_cloudwatch({}),
        }
    )

    diag = diagnostics.diagnose_workspace_connectivity_core(factory, "ws-2", "us-east-1")

    assert diag.status == "unhealthy"
    assert any(f.severity == "critical" and "UNHEALTHY" in f.title for f in diag.findings)


def test_workspace_connectivity_not_found():
    workspaces = types.SimpleNamespace(
        describe_workspaces=lambda **_: {"Workspaces": []},
    )
    factory = FakeFactory({consts.WORKSPACES_API: workspaces})

    diag = diagnostics.diagnose_workspace_connectivity_core(factory, "ws-missing", "us-east-1")

    assert diag.status == "not_found"


def test_workspace_connectivity_flags_connection_failures():
    ws_dir, ds = _healthy_directory_clients()
    workspaces = types.SimpleNamespace(
        describe_workspaces=lambda **_: {
            "Workspaces": [{"WorkspaceId": "ws-3", "State": "AVAILABLE", "DirectoryId": "d-123"}]
        },
        describe_workspaces_connection_status=lambda **_: {"WorkspacesConnectionStatus": []},
        describe_workspace_directories=ws_dir.describe_workspace_directories,
    )
    factory = FakeFactory(
        {
            consts.WORKSPACES_API: workspaces,
            consts.DIRECTORY_API: ds,
            consts.CLOUDWATCH_API: _fake_cloudwatch(
                {"ConnectionFailure": 5.0, "ConnectionAttempt": 8.0}
            ),
        }
    )

    diag = diagnostics.diagnose_workspace_connectivity_core(factory, "ws-3", "us-east-1")

    assert diag.signals["connection_failures"] == 5.0
    assert any("connection failures" in f.title for f in diag.findings)
    assert diag.status == "degraded"


def test_directory_health_flags_impaired_stage():
    workspaces = types.SimpleNamespace(
        describe_workspace_directories=lambda **_: {
            "Directories": [{"DirectoryId": "d-9", "State": "REGISTERED"}]
        },
    )
    ds = types.SimpleNamespace(
        describe_directories=lambda **_: {
            "DirectoryDescriptions": [{"DirectoryId": "d-9", "Stage": "Impaired"}]
        },
    )
    factory = FakeFactory({consts.WORKSPACES_API: workspaces, consts.DIRECTORY_API: ds})

    report = diagnostics.check_directory_health_core(factory, "d-9", "us-east-1")

    assert len(report.directories) == 1
    d = report.directories[0]
    assert d.status == "unhealthy"
    assert any("Impaired" in f.title for f in d.findings)


def test_application_fleet_capacity_exhausted():
    appstream = types.SimpleNamespace(
        describe_fleets=lambda **_: {
            "Fleets": [
                {
                    "Name": "f1",
                    "State": "RUNNING",
                    "FleetErrors": [],
                    "ComputeCapacityStatus": {
                        "Desired": 5,
                        "Running": 5,
                        "InUse": 5,
                        "Available": 0,
                    },
                }
            ]
        },
    )
    autoscaling = types.SimpleNamespace(
        describe_scaling_activities=lambda **_: {"ScalingActivities": []},
    )
    factory = FakeFactory(
        {
            consts.APPSTREAM_API: appstream,
            "application-autoscaling": autoscaling,
            consts.CLOUDWATCH_API: _fake_cloudwatch({"InsufficientCapacityError": 0.0}),
        }
    )

    diag = diagnostics.diagnose_application_fleet_core(factory, "f1", "us-east-1")

    assert diag.status == "unhealthy"
    assert any("capacity is exhausted" in f.title for f in diag.findings)


def test_application_fleet_surfaces_fleet_errors():
    appstream = types.SimpleNamespace(
        describe_fleets=lambda **_: {
            "Fleets": [
                {
                    "Name": "f2",
                    "State": "RUNNING",
                    "FleetErrors": [
                        {"ErrorCode": "IAM_SERVICE_ROLE_MISSING", "ErrorMessage": "role gone"}
                    ],
                    "ComputeCapacityStatus": {
                        "Desired": 2,
                        "Running": 2,
                        "InUse": 0,
                        "Available": 2,
                    },
                }
            ]
        },
    )
    autoscaling = types.SimpleNamespace(
        describe_scaling_activities=lambda **_: {"ScalingActivities": []},
    )
    factory = FakeFactory(
        {
            consts.APPSTREAM_API: appstream,
            "application-autoscaling": autoscaling,
            consts.CLOUDWATCH_API: _fake_cloudwatch({}),
        }
    )

    diag = diagnostics.diagnose_application_fleet_core(factory, "f2", "us-east-1")

    assert diag.status == "unhealthy"
    assert any("IAM_SERVICE_ROLE_MISSING" in f.title for f in diag.findings)


def test_application_fleet_not_found():
    appstream = types.SimpleNamespace(describe_fleets=lambda **_: {"Fleets": []})
    factory = FakeFactory({consts.APPSTREAM_API: appstream})

    diag = diagnostics.diagnose_application_fleet_core(factory, "nope", "us-east-1")

    assert diag.status == "not_found"
