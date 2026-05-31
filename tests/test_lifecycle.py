# Copyright bengroeneveldsg. Licensed under the Apache License, Version 2.0 (the "License").
# You may not use this file except in compliance with the License.
# A copy of the License is located at http://www.apache.org/licenses/LICENSE-2.0
"""Tests for the guarded lifecycle (write) tools."""

from __future__ import annotations

import asyncio
import types

from workspaces_euc_mcp_server import consts
from workspaces_euc_mcp_server.server import create_server
from workspaces_euc_mcp_server.tools import lifecycle


class FakeFactory:
    region = "us-east-1"

    def __init__(self, clients: dict[str, object]) -> None:
        self._clients = clients
        self.calls: list[tuple[str, dict]] = []

    def client(self, service_name: str, region: str | None = None):
        if service_name not in self._clients:
            raise AssertionError(f"unexpected client requested: {service_name}")
        return self._clients[service_name]


def _recording_workspaces(failed=None):
    failed = failed or []
    calls: list[tuple[str, dict]] = []

    def start_workspaces(**kwargs):
        calls.append(("start_workspaces", kwargs))
        return {"FailedRequests": failed}

    def stop_workspaces(**kwargs):
        calls.append(("stop_workspaces", kwargs))
        return {"FailedRequests": failed}

    def reboot_workspaces(**kwargs):
        calls.append(("reboot_workspaces", kwargs))
        return {"FailedRequests": failed}

    def modify_workspace_properties(**kwargs):
        calls.append(("modify_workspace_properties", kwargs))
        return {}

    client = types.SimpleNamespace(
        start_workspaces=start_workspaces,
        stop_workspaces=stop_workspaces,
        reboot_workspaces=reboot_workspaces,
        modify_workspace_properties=modify_workspace_properties,
    )
    return client, calls


def test_power_action_dry_run_makes_no_calls():
    client, calls = _recording_workspaces()
    factory = FakeFactory({consts.WORKSPACES_API: client})

    outcome = lifecycle.batch_power_action_core(
        factory, "us-east-1", "start", ["ws-1", "ws-2"], confirm=False, max_bulk_targets=25
    )

    assert outcome.dry_run is True
    assert outcome.confirmed is False
    assert calls == []  # nothing executed
    assert all(r.status == "skipped" for r in outcome.results)


def test_power_action_confirmed_executes():
    client, calls = _recording_workspaces()
    factory = FakeFactory({consts.WORKSPACES_API: client})

    outcome = lifecycle.batch_power_action_core(
        factory, "us-east-1", "stop", ["ws-1"], confirm=True, max_bulk_targets=25
    )

    assert outcome.dry_run is False
    assert len(calls) == 1
    assert calls[0][0] == "stop_workspaces"
    assert calls[0][1]["StopWorkspaceRequests"] == [{"WorkspaceId": "ws-1"}]
    assert outcome.results[0].status == "ok"


def test_power_action_refused_over_blast_radius():
    client, calls = _recording_workspaces()
    factory = FakeFactory({consts.WORKSPACES_API: client})

    outcome = lifecycle.batch_power_action_core(
        factory, "us-east-1", "reboot", ["a", "b", "c"], confirm=True, max_bulk_targets=2
    )

    assert outcome.blast_radius_ok is False
    assert outcome.results == []
    assert calls == []  # refused before executing


def test_power_action_reports_failed_requests():
    client, _ = _recording_workspaces(
        failed=[{"WorkspaceId": "ws-bad", "ErrorCode": "X", "ErrorMessage": "nope"}]
    )
    factory = FakeFactory({consts.WORKSPACES_API: client})

    outcome = lifecycle.batch_power_action_core(
        factory, "us-east-1", "start", ["ws-ok", "ws-bad"], confirm=True, max_bulk_targets=25
    )

    by_id = {r.target_id: r for r in outcome.results}
    assert by_id["ws-ok"].status == "ok"
    assert by_id["ws-bad"].status == "error"
    assert by_id["ws-bad"].message == "nope"


def test_modify_running_mode_validates_input():
    client, calls = _recording_workspaces()
    factory = FakeFactory({consts.WORKSPACES_API: client})

    outcome = lifecycle.modify_running_mode_core(
        factory, "us-east-1", "ws-1", "TURBO", confirm=True, max_bulk_targets=25
    )

    assert outcome.blast_radius_ok is True
    assert "Invalid running mode" in outcome.plan
    assert calls == []  # invalid input never executes


def test_modify_running_mode_executes_when_confirmed():
    client, calls = _recording_workspaces()
    factory = FakeFactory({consts.WORKSPACES_API: client})

    outcome = lifecycle.modify_running_mode_core(
        factory, "us-east-1", "ws-1", "auto_stop", confirm=True, max_bulk_targets=25
    )

    assert outcome.dry_run is False
    assert calls[0][0] == "modify_workspace_properties"
    assert calls[0][1]["WorkspaceProperties"] == {"RunningMode": "AUTO_STOP"}
    assert outcome.results[0].status == "ok"


def test_write_tools_absent_unless_enabled():
    read_only = create_server(region="us-east-1")
    with_writes = create_server(region="us-east-1", enable_writes=True)

    ro_names = {t.name for t in asyncio.run(read_only.list_tools())}
    w_names = {t.name for t in asyncio.run(with_writes.list_tools())}

    assert "start_workspaces" not in ro_names
    assert {
        "start_workspaces",
        "stop_workspaces",
        "reboot_workspaces",
        "modify_workspace_running_mode",
    } <= w_names
