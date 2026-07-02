# Copyright bengroeneveldsg. Licensed under the Apache License, Version 2.0 (the "License").
# You may not use this file except in compliance with the License.
# A copy of the License is located at http://www.apache.org/licenses/LICENSE-2.0
"""Tests for the cross-service inventory collection.

These use lightweight duck-typed fake clients (via SimpleNamespace) instead of moto, because
several in-scope EUC operations (WorkSpaces Pools, Secure Browser portals) are not covered by
moto. Faking the boto3 clients keeps the test deterministic and version-independent while
exercising the real synthesis logic.
"""

from __future__ import annotations

import types

from botocore.exceptions import ClientError

from workspaces_euc_mcp_server import consts
from workspaces_euc_mcp_server.tools import inventory


class FakeFactory:
    """Returns pre-built fake clients keyed by API identifier."""

    region = "us-east-1"

    def __init__(self, clients: dict[str, object]) -> None:
        self._clients = clients

    def client(self, service_name: str, region: str | None = None):
        if service_name not in self._clients:
            raise AssertionError(f"unexpected client requested: {service_name}")
        return self._clients[service_name]


def _access_denied(operation: str) -> ClientError:
    return ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "not authorized"}},
        operation,
    )


def test_collect_inventory_counts_and_states():
    workspaces = types.SimpleNamespace(
        describe_workspaces=lambda **_: {
            "Workspaces": [
                {"State": "AVAILABLE"},
                {"State": "AVAILABLE"},
                {"State": "STOPPED"},
            ]
        },
        describe_workspaces_pools=lambda **_: {"WorkspacesPools": [{"State": "RUNNING"}]},
    )
    appstream = types.SimpleNamespace(
        describe_fleets=lambda **_: {"Fleets": [{"State": "RUNNING"}, {"State": "STOPPED"}]},
        describe_stacks=lambda **_: {"Stacks": [{"Name": "stack-a"}, {"Name": "stack-b"}]},
    )
    secure_browser = types.SimpleNamespace(
        list_portals=lambda **_: {"portals": [{"portalStatus": "Active"}]},
    )
    instances = types.SimpleNamespace(
        list_workspace_instances=lambda **_: {
            "WorkspaceInstances": [
                {"WorkspaceInstanceId": "wsinst-1", "ProvisionState": "ALLOCATED"},
                {"WorkspaceInstanceId": "wsinst-2", "ProvisionState": "ALLOCATED"},
            ]
        },
    )

    factory = FakeFactory(
        {
            consts.WORKSPACES_API: workspaces,
            consts.APPSTREAM_API: appstream,
            consts.SECURE_BROWSER_API: secure_browser,
            consts.WORKSPACES_INSTANCES_API: instances,
        }
    )

    summary = inventory.collect_inventory(factory, "us-east-1")

    assert summary.region == "us-east-1"
    # personal/pools/fleets/stacks/portals/managed-instances
    assert summary.total_resources == 3 + 1 + 2 + 2 + 1 + 2
    assert summary.errors == []

    by_service = {s.service: s for s in summary.services}
    personal = by_service[consts.PRODUCT_WORKSPACES_PERSONAL]
    assert personal.count == 3
    assert personal.by_state == {"AVAILABLE": 2, "STOPPED": 1}
    assert by_service[consts.PRODUCT_WORKSPACES_POOLS].count == 1
    # WorkSpaces Applications now contributes two rows: Fleet and Stack.
    apps = {
        s.resource_type: s.count
        for s in summary.services
        if s.service == consts.PRODUCT_WORKSPACES_APPLICATIONS
    }
    assert apps == {"Fleet": 2, "Stack": 2}
    assert by_service[consts.PRODUCT_SECURE_BROWSER].count == 1
    core = by_service[consts.PRODUCT_WORKSPACES_CORE_INSTANCES]
    assert core.count == 2
    assert core.by_state == {"ALLOCATED": 2}


def test_collect_inventory_records_per_service_errors():
    def raise_fleets(**_):
        raise _access_denied("DescribeFleets")

    def raise_stacks(**_):
        raise _access_denied("DescribeStacks")

    workspaces = types.SimpleNamespace(
        describe_workspaces=lambda **_: {"Workspaces": [{"State": "AVAILABLE"}]},
        describe_workspaces_pools=lambda **_: {"WorkspacesPools": []},
    )
    appstream = types.SimpleNamespace(describe_fleets=raise_fleets, describe_stacks=raise_stacks)
    secure_browser = types.SimpleNamespace(list_portals=lambda **_: {"portals": []})
    instances = types.SimpleNamespace(
        list_workspace_instances=lambda **_: {"WorkspaceInstances": []}
    )

    factory = FakeFactory(
        {
            consts.WORKSPACES_API: workspaces,
            consts.APPSTREAM_API: appstream,
            consts.SECURE_BROWSER_API: secure_browser,
            consts.WORKSPACES_INSTANCES_API: instances,
        }
    )

    summary = inventory.collect_inventory(factory, "us-east-1")

    # The Applications fleet AND stack calls failed, but the rest still collected.
    assert summary.total_resources == 1
    assert len(summary.errors) == 2
    err = summary.errors[0]
    assert err.service == consts.PRODUCT_WORKSPACES_APPLICATIONS
    assert err.operation == "DescribeFleets"
    assert {e.operation for e in summary.errors} == {"DescribeFleets", "DescribeStacks"}
    services = {s.service for s in summary.services}
    assert consts.PRODUCT_WORKSPACES_APPLICATIONS not in services
    assert consts.PRODUCT_WORKSPACES_PERSONAL in services


def test_paginate_follows_tokens():
    pages = [
        {"Workspaces": [{"State": "AVAILABLE"}], "NextToken": "t1"},
        {"Workspaces": [{"State": "STOPPED"}]},
    ]
    calls = {"n": 0}

    def op(**_):
        page = pages[calls["n"]]
        calls["n"] += 1
        return page

    from workspaces_euc_mcp_server.tools._common import paginate

    items = paginate(op, "Workspaces")
    assert len(items) == 2
    assert calls["n"] == 2


def test_gather_concurrently_preserves_order_and_runs_parallel():
    """Jobs run concurrently (barrier only releases if all run at once) and keep order."""
    import threading

    from workspaces_euc_mcp_server.tools import _common

    barrier = threading.Barrier(3, timeout=5)

    def make_job(value):
        def job():
            barrier.wait()  # deadlocks (then Barrier timeout) unless all 3 run concurrently
            return value

        return job

    results = _common.gather_concurrently(make_job("a"), make_job("b"), make_job("c"))
    assert results == ["a", "b", "c"]


def test_gather_concurrently_propagates_exceptions():
    from workspaces_euc_mcp_server.tools import _common

    def boom():
        raise ValueError("job failed")

    try:
        _common.gather_concurrently(lambda: 1, boom)
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "job failed" in str(exc)
