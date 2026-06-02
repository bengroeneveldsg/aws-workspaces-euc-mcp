# Copyright bengroeneveldsg. Licensed under the Apache License, Version 2.0 (the "License").
# You may not use this file except in compliance with the License.
# A copy of the License is located at http://www.apache.org/licenses/LICENSE-2.0
"""Tests for the cost & utilization tools, using duck-typed fake boto3 clients."""

from __future__ import annotations

import types

from workspaces_euc_mcp_server import consts
from workspaces_euc_mcp_server.tools import cost


class FakeFactory:
    region = "us-east-1"

    def __init__(self, clients: dict[str, object]) -> None:
        self._clients = clients

    def client(self, service_name: str, region: str | None = None):
        if service_name not in self._clients:
            raise AssertionError(f"unexpected client requested: {service_name}")
        return self._clients[service_name]


def _workspaces_client(workspaces: list[dict]):
    return types.SimpleNamespace(describe_workspaces=lambda **_: {"Workspaces": workspaces})


def _cloudwatch_by_workspace(values_by_id: dict[str, list[float]]):
    def get_metric_data(**kwargs):
        wid = kwargs["MetricDataQueries"][0]["MetricStat"]["Metric"]["Dimensions"][0]["Value"]
        return {"MetricDataResults": [{"Values": values_by_id.get(wid, [])}]}

    return types.SimpleNamespace(get_metric_data=get_metric_data)


def test_utilization_classifies_unused_idle_active():
    workspaces = [
        {"WorkspaceId": "ws-unused", "WorkspaceProperties": {"RunningMode": "ALWAYS_ON"}},
        {"WorkspaceId": "ws-idle", "WorkspaceProperties": {"RunningMode": "AUTO_STOP"}},
        {"WorkspaceId": "ws-active", "WorkspaceProperties": {"RunningMode": "AUTO_STOP"}},
    ]
    factory = FakeFactory(
        {
            consts.WORKSPACES_API: _workspaces_client(workspaces),
            consts.CLOUDWATCH_API: _cloudwatch_by_workspace(
                {
                    "ws-unused": [],
                    "ws-idle": [1.0],
                    "ws-active": [1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0],
                }
            ),
        }
    )

    report = cost.analyze_workspace_utilization_core(factory, "us-east-1", lookback_days=14)

    assert report.total == 3
    by_id = {w.workspace_id: w for w in report.workspaces}
    assert by_id["ws-unused"].classification == "unused"
    assert by_id["ws-idle"].classification == "idle"
    assert by_id["ws-active"].classification == "active"
    assert report.counts == {"unused": 1, "idle": 1, "active": 1}


def test_recommend_running_mode_flags_alwayson_idle():
    workspaces = [
        {"WorkspaceId": "ws-on-idle", "WorkspaceProperties": {"RunningMode": "ALWAYS_ON"}},
        {"WorkspaceId": "ws-on-busy", "WorkspaceProperties": {"RunningMode": "ALWAYS_ON"}},
    ]
    factory = FakeFactory(
        {
            consts.WORKSPACES_API: _workspaces_client(workspaces),
            consts.CLOUDWATCH_API: _cloudwatch_by_workspace(
                {
                    "ws-on-idle": [1.0],  # idle
                    "ws-on-busy": [1.0] * 14,  # active -> no recommendation
                }
            ),
        }
    )

    report = cost.recommend_running_mode_core(factory, "us-east-1", lookback_days=14)

    assert len(report.recommendations) == 1
    rec = report.recommendations[0]
    assert rec.target_id == "ws-on-idle"
    assert rec.current == "ALWAYS_ON"
    assert rec.recommended == "AUTO_STOP"


def test_cost_summary_aggregates_by_service():
    ce = types.SimpleNamespace(
        get_cost_and_usage=lambda **_: {
            "ResultsByTime": [
                {
                    "Groups": [
                        {
                            "Keys": ["Amazon WorkSpaces"],
                            "Metrics": {"UnblendedCost": {"Amount": "100.50", "Unit": "USD"}},
                        },
                        {
                            "Keys": ["Amazon AppStream"],
                            "Metrics": {"UnblendedCost": {"Amount": "40.00", "Unit": "USD"}},
                        },
                    ]
                },
                {
                    "Groups": [
                        {
                            "Keys": ["Amazon WorkSpaces"],
                            "Metrics": {"UnblendedCost": {"Amount": "9.50", "Unit": "USD"}},
                        },
                    ]
                },
            ]
        }
    )
    factory = FakeFactory({consts.COST_EXPLORER_API: ce})

    summary = cost.get_euc_cost_summary_core(factory, lookback_days=60)

    assert summary.total == 150.0
    assert summary.currency == "USD"
    # Sorted descending by amount: WorkSpaces (110.0) before AppStream (40.0).
    assert summary.by_service[0].service == "Amazon WorkSpaces"
    assert summary.by_service[0].amount == 110.0
    assert summary.by_service[1].amount == 40.0


def test_cost_summary_keyword_matches_variants_and_excludes_noneuc():
    # Real-world Cost Explorer SERVICE names: AppStream now bills as "Amazon WorkSpaces
    # Applications" (the rebrand that the old exact-match list missed) and must be captured;
    # "Amazon WorkSpaces Thin Client" contains "workspaces" but is OUT of scope and must be
    # excluded; non-EUC services (EC2) are excluded.
    ce = types.SimpleNamespace(
        get_cost_and_usage=lambda **_: {
            "ResultsByTime": [
                {
                    "Groups": [
                        {
                            "Keys": ["Amazon WorkSpaces Applications"],
                            "Metrics": {"UnblendedCost": {"Amount": "1006.85", "Unit": "USD"}},
                        },
                        {
                            "Keys": ["Amazon WorkSpaces"],
                            "Metrics": {"UnblendedCost": {"Amount": "694.97", "Unit": "USD"}},
                        },
                        {
                            "Keys": ["Amazon WorkSpaces Thin Client"],
                            "Metrics": {"UnblendedCost": {"Amount": "6.00", "Unit": "USD"}},
                        },
                        {
                            "Keys": ["Amazon Elastic Compute Cloud - Compute"],
                            "Metrics": {"UnblendedCost": {"Amount": "485.58", "Unit": "USD"}},
                        },
                    ]
                }
            ]
        }
    )
    factory = FakeFactory({consts.COST_EXPLORER_API: ce})

    summary = cost.get_euc_cost_summary_core(factory, lookback_days=30)

    # WorkSpaces Applications captured, Thin Client + EC2 excluded.
    assert {li.service for li in summary.by_service} == {
        "Amazon WorkSpaces Applications",
        "Amazon WorkSpaces",
    }
    assert summary.total == 1701.82


def test_cost_summary_explicit_date_range_overrides_lookback():
    captured: dict = {}

    def get_cost_and_usage(**kwargs):
        captured.update(kwargs)
        return {"ResultsByTime": []}

    ce = types.SimpleNamespace(get_cost_and_usage=get_cost_and_usage)
    factory = FakeFactory({consts.COST_EXPLORER_API: ce})

    summary = cost.get_euc_cost_summary_core(
        factory, start_date="2026-05-01", end_date="2026-06-01"
    )

    assert captured["TimePeriod"] == {"Start": "2026-05-01", "End": "2026-06-01"}
    assert summary.start == "2026-05-01"
    assert summary.end == "2026-06-01"


def test_cost_summary_follows_pagination():
    page1 = {
        "ResultsByTime": [
            {
                "Groups": [
                    {
                        "Keys": ["Amazon WorkSpaces"],
                        "Metrics": {"UnblendedCost": {"Amount": "10.00", "Unit": "USD"}},
                    }
                ]
            }
        ],
        "NextPageToken": "p2",
    }
    page2 = {
        "ResultsByTime": [
            {
                "Groups": [
                    {
                        "Keys": ["Amazon AppStream"],
                        "Metrics": {"UnblendedCost": {"Amount": "20.00", "Unit": "USD"}},
                    }
                ]
            }
        ]
    }

    def get_cost_and_usage(**kwargs):
        return page2 if kwargs.get("NextPageToken") == "p2" else page1

    ce = types.SimpleNamespace(get_cost_and_usage=get_cost_and_usage)
    factory = FakeFactory({consts.COST_EXPLORER_API: ce})

    summary = cost.get_euc_cost_summary_core(factory, lookback_days=30)

    assert summary.total == 30.0
    assert {li.service for li in summary.by_service} == {"Amazon WorkSpaces", "Amazon AppStream"}


def test_cost_summary_records_errors_gracefully():
    from botocore.exceptions import ClientError

    def boom(**_):
        raise ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "no ce"}}, "GetCostAndUsage"
        )

    ce = types.SimpleNamespace(get_cost_and_usage=boom)
    factory = FakeFactory({consts.COST_EXPLORER_API: ce})

    summary = cost.get_euc_cost_summary_core(factory, lookback_days=30)

    assert summary.total == 0.0
    assert len(summary.errors) == 1
    assert summary.errors[0].operation == "GetCostAndUsage"
