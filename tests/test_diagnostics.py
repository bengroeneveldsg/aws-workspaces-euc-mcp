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


def test_directory_health_skips_ds_for_workspaces_managed_directory():
    # WorkSpaces Pools directories use wsd-... ids that AWS Directory Service rejects; the tool
    # must NOT call ds:DescribeDirectories for them. The factory below has no ds client, so any
    # such call would raise.
    workspaces = types.SimpleNamespace(
        describe_workspace_directories=lambda **_: {
            "Directories": [{"DirectoryId": "wsd-f9bt3329t", "State": "REGISTERED"}]
        },
    )
    factory = FakeFactory({consts.WORKSPACES_API: workspaces})

    report = diagnostics.check_directory_health_core(factory, "wsd-f9bt3329t", "us-east-1")

    assert len(report.directories) == 1
    d = report.directories[0]
    assert d.status == "healthy"
    assert d.errors == []
    assert any("WorkSpaces-managed" in f.title for f in d.findings)


def test_directory_health_surfaces_registration_ou_and_properties():
    # The registration OU (WorkspaceCreationProperties.DefaultOu) and related properties must be
    # exposed in the diagnosis signals.
    workspaces = types.SimpleNamespace(
        describe_workspace_directories=lambda **_: {
            "Directories": [
                {
                    "DirectoryId": "d-0123456789",
                    "State": "REGISTERED",
                    "DirectoryType": "AD_CONNECTOR",
                    "WorkspaceType": "PERSONAL",
                    "WorkspaceCreationProperties": {
                        "DefaultOu": "OU=AmazonWorkspaces,OU=Singapore,DC=bg,DC=local",
                        "CustomSecurityGroupId": "sg-0abc",
                        "UserEnabledAsLocalAdministrator": True,
                        "EnableInternetAccess": False,
                        "EnableMaintenanceMode": True,
                    },
                }
            ]
        },
    )
    ds = types.SimpleNamespace(
        describe_directories=lambda **_: {
            "DirectoryDescriptions": [{"DirectoryId": "d-0123456789", "Stage": "Active"}]
        },
    )
    factory = FakeFactory({consts.WORKSPACES_API: workspaces, consts.DIRECTORY_API: ds})

    report = diagnostics.check_directory_health_core(factory, "d-0123456789", "us-east-1")

    sig = report.directories[0].signals
    assert sig["default_ou"] == "OU=AmazonWorkspaces,OU=Singapore,DC=bg,DC=local"
    assert sig["directory_type"] == "AD_CONNECTOR"
    assert sig["custom_security_group_id"] == "sg-0abc"
    assert sig["user_enabled_as_local_administrator"] is True
    assert sig["enable_maintenance_mode"] is True


def test_directory_health_flags_impaired_stage():
    workspaces = types.SimpleNamespace(
        describe_workspace_directories=lambda **_: {
            "Directories": [{"DirectoryId": "d-0123456789", "State": "REGISTERED"}]
        },
    )
    ds = types.SimpleNamespace(
        describe_directories=lambda **_: {
            "DirectoryDescriptions": [{"DirectoryId": "d-0123456789", "Stage": "Impaired"}]
        },
    )
    factory = FakeFactory({consts.WORKSPACES_API: workspaces, consts.DIRECTORY_API: ds})

    report = diagnostics.check_directory_health_core(factory, "d-0123456789", "us-east-1")

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


def test_pool_diagnosis_healthy():
    ws_dir, ds = _healthy_directory_clients()
    workspaces = types.SimpleNamespace(
        describe_workspaces_pools=lambda **_: {
            "WorkspacesPools": [
                {
                    "PoolId": "wspool-1",
                    "State": "RUNNING",
                    "DirectoryId": "d-123",
                    "CapacityStatus": {
                        "DesiredUserSessions": 2,
                        "ActualUserSessions": 2,
                        "ActiveUserSessions": 0,
                        "AvailableUserSessions": 2,
                    },
                }
            ]
        },
        describe_workspace_directories=ws_dir.describe_workspace_directories,
    )
    factory = FakeFactory(
        {
            consts.WORKSPACES_API: workspaces,
            consts.DIRECTORY_API: ds,
            consts.CLOUDWATCH_API: _fake_cloudwatch({"UserSessionsCapacityUtilization": 0.0}),
        }
    )

    diag = diagnostics.diagnose_pool_core(factory, "wspool-1", "us-east-1")

    assert diag.status == "healthy"
    assert diag.signals["state"] == "RUNNING"
    assert diag.signals["peak_utilization_percent"] == 0.0


def test_pool_diagnosis_capacity_exhausted_and_errors():
    workspaces = types.SimpleNamespace(
        describe_workspaces_pools=lambda **_: {
            "WorkspacesPools": [
                {
                    "PoolId": "wspool-2",
                    "State": "RUNNING",
                    "Errors": [{"ErrorCode": "DIRECTORY_FAILURE", "ErrorMessage": "dir gone"}],
                    "CapacityStatus": {
                        "DesiredUserSessions": 5,
                        "ActualUserSessions": 5,
                        "ActiveUserSessions": 5,
                        "AvailableUserSessions": 0,
                    },
                }
            ]
        },
    )
    factory = FakeFactory(
        {
            consts.WORKSPACES_API: workspaces,
            consts.CLOUDWATCH_API: _fake_cloudwatch({}),
        }
    )

    diag = diagnostics.diagnose_pool_core(factory, "wspool-2", "us-east-1")

    assert diag.status == "unhealthy"
    titles = " ".join(f.title for f in diag.findings)
    assert "capacity is exhausted" in titles
    assert "DIRECTORY_FAILURE" in titles


def test_pool_diagnosis_not_found():
    workspaces = types.SimpleNamespace(
        describe_workspaces_pools=lambda **_: {"WorkspacesPools": []},
    )
    factory = FakeFactory({consts.WORKSPACES_API: workspaces})

    diag = diagnostics.diagnose_pool_core(factory, "wspool-missing", "us-east-1")

    assert diag.status == "not_found"


def test_application_fleet_not_found():
    appstream = types.SimpleNamespace(describe_fleets=lambda **_: {"Fleets": []})
    factory = FakeFactory({consts.APPSTREAM_API: appstream})

    diag = diagnostics.diagnose_application_fleet_core(factory, "nope", "us-east-1")

    assert diag.status == "not_found"
