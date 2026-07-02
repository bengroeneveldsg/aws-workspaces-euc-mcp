# Copyright bengroeneveldsg. Licensed under the Apache License, Version 2.0 (the "License").
# You may not use this file except in compliance with the License.
# A copy of the License is located at http://www.apache.org/licenses/LICENSE-2.0
"""Tests for the destructive (Tier 3) tools and their gating."""

from __future__ import annotations

import asyncio
import types

from workspaces_euc_mcp_server import consts
from workspaces_euc_mcp_server.server import create_server
from workspaces_euc_mcp_server.tools import destructive


class FakeFactory:
    region = "us-east-1"

    def __init__(self, clients: dict[str, object]) -> None:
        self._clients = clients

    def client(self, service_name: str, region: str | None = None):
        if service_name not in self._clients:
            raise AssertionError(f"unexpected client requested: {service_name}")
        return self._clients[service_name]


def _recording_workspaces(failed=None):
    failed = failed or []
    calls: list[tuple[str, dict]] = []

    def _record(name, returns):
        def fn(**kwargs):
            calls.append((name, kwargs))
            return returns

        return fn

    client = types.SimpleNamespace(
        terminate_workspaces=_record("terminate_workspaces", {"FailedRequests": failed}),
        rebuild_workspaces=_record("rebuild_workspaces", {"FailedRequests": failed}),
        restore_workspace=_record("restore_workspace", {}),
    )
    return client, calls


def test_terminate_dry_run_makes_no_calls():
    client, calls = _recording_workspaces()
    factory = FakeFactory({consts.WORKSPACES_API: client})

    outcome = destructive.batch_destructive_core(
        factory,
        "us-east-1",
        "terminate",
        ["ws-1"],
        confirm=False,
        acknowledge="",
        max_bulk_targets=25,
    )

    assert outcome.dry_run is True
    assert calls == []
    # The impact warning is surfaced in the dry-run notes.
    assert any("IRREVERSIBLE" in n for n in outcome.notes)


def test_terminate_confirmed_without_acknowledgement_is_refused():
    client, calls = _recording_workspaces()
    factory = FakeFactory({consts.WORKSPACES_API: client})

    outcome = destructive.batch_destructive_core(
        factory,
        "us-east-1",
        "terminate",
        ["ws-1"],
        confirm=True,
        acknowledge="",
        max_bulk_targets=25,
    )

    assert outcome.dry_run is False
    assert outcome.acknowledgement_required == "TERMINATE"
    assert calls == []  # refused, nothing deleted


def test_terminate_wrong_acknowledgement_is_refused():
    client, calls = _recording_workspaces()
    factory = FakeFactory({consts.WORKSPACES_API: client})

    outcome = destructive.batch_destructive_core(
        factory,
        "us-east-1",
        "terminate",
        ["ws-1"],
        confirm=True,
        acknowledge="terminate",  # case-sensitive; must be exactly TERMINATE
        max_bulk_targets=25,
    )

    assert outcome.acknowledgement_required == "TERMINATE"
    assert calls == []


def test_terminate_executes_with_correct_acknowledgement():
    client, calls = _recording_workspaces()
    factory = FakeFactory({consts.WORKSPACES_API: client})

    outcome = destructive.batch_destructive_core(
        factory,
        "us-east-1",
        "terminate",
        ["ws-1", "ws-2"],
        confirm=True,
        acknowledge="TERMINATE",
        max_bulk_targets=25,
    )

    assert outcome.dry_run is False
    assert calls[0][0] == "terminate_workspaces"
    assert calls[0][1]["TerminateWorkspaceRequests"] == [
        {"WorkspaceId": "ws-1"},
        {"WorkspaceId": "ws-2"},
    ]
    assert all(r.status == "ok" for r in outcome.results)


def test_terminate_refused_over_blast_radius_before_ack_check():
    client, calls = _recording_workspaces()
    factory = FakeFactory({consts.WORKSPACES_API: client})

    outcome = destructive.batch_destructive_core(
        factory,
        "us-east-1",
        "terminate",
        ["a", "b", "c"],
        confirm=True,
        acknowledge="TERMINATE",
        max_bulk_targets=2,
    )

    assert outcome.blast_radius_ok is False
    assert calls == []


def test_rebuild_executes_with_acknowledgement():
    client, calls = _recording_workspaces()
    factory = FakeFactory({consts.WORKSPACES_API: client})

    outcome = destructive.batch_destructive_core(
        factory,
        "us-east-1",
        "rebuild",
        ["ws-1"],
        confirm=True,
        acknowledge="REBUILD",
        max_bulk_targets=25,
    )

    assert calls[0][0] == "rebuild_workspaces"
    assert outcome.results[0].status == "ok"


def test_restore_requires_acknowledgement_then_executes():
    client, calls = _recording_workspaces()
    factory = FakeFactory({consts.WORKSPACES_API: client})

    refused = destructive.restore_workspace_core(
        factory, "us-east-1", "ws-1", confirm=True, acknowledge="", max_bulk_targets=25
    )
    assert refused.acknowledgement_required == "RESTORE"
    assert calls == []

    done = destructive.restore_workspace_core(
        factory, "us-east-1", "ws-1", confirm=True, acknowledge="RESTORE", max_bulk_targets=25
    )
    assert calls[0] == ("restore_workspace", {"WorkspaceId": "ws-1"})
    assert done.results[0].status == "ok"


def test_destructive_tools_gated_by_flags():
    writes_only = create_server(region="us-east-1", enable_writes=True)
    destructive_on = create_server(region="us-east-1", enable_writes=True, enable_destructive=True)

    writes_names = {t.name for t in asyncio.run(writes_only.list_tools())}
    destr_names = {t.name for t in asyncio.run(destructive_on.list_tools())}

    destructive_tools = {"terminate_workspaces", "rebuild_workspaces", "restore_workspace"}
    assert destructive_tools.isdisjoint(writes_names)  # absent with writes only
    assert destructive_tools <= destr_names  # present when destructive enabled


def test_rebuild_dry_run_includes_last_snapshot_time():
    client = types.SimpleNamespace(
        describe_workspace_snapshots=lambda **_: {
            "RebuildSnapshots": [{"SnapshotTime": "2026-07-02T05:19:24"}],
            "RestoreSnapshots": [],
        }
    )
    factory = FakeFactory({consts.WORKSPACES_API: client})

    outcome = destructive.batch_destructive_core(
        factory,
        "us-east-1",
        "rebuild",
        ["ws-abc"],
        confirm=False,
        acknowledge="",
        max_bulk_targets=25,
    )

    assert outcome.dry_run is True
    assert any("last rebuild snapshot 2026-07-02T05:19:24" in n for n in outcome.notes)
    assert any("data after this time will be lost" in n for n in outcome.notes)
