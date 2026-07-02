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


def test_cost_summary_daily_returns_per_period_time_series():
    # DAILY granularity must preserve the per-day breakdown in by_period (for charts), not just
    # collapse everything into by_service totals.
    ce = types.SimpleNamespace(
        get_cost_and_usage=lambda **_: {
            "ResultsByTime": [
                {
                    "TimePeriod": {"Start": "2026-05-01", "End": "2026-05-02"},
                    "Groups": [
                        {
                            "Keys": ["Amazon WorkSpaces Applications"],
                            "Metrics": {"UnblendedCost": {"Amount": "30.00", "Unit": "USD"}},
                        }
                    ],
                },
                {
                    "TimePeriod": {"Start": "2026-05-02", "End": "2026-05-03"},
                    "Groups": [
                        {
                            "Keys": ["Amazon WorkSpaces Applications"],
                            "Metrics": {"UnblendedCost": {"Amount": "45.00", "Unit": "USD"}},
                        }
                    ],
                },
            ]
        }
    )
    factory = FakeFactory({consts.COST_EXPLORER_API: ce})

    summary = cost.get_euc_cost_summary_core(
        factory, granularity="DAILY", start_date="2026-05-01", end_date="2026-05-03"
    )

    # Aggregate total preserved...
    assert summary.total == 75.0
    # ...and the daily series is available, ordered by date.
    assert [(p.start, p.total) for p in summary.by_period] == [
        ("2026-05-01", 30.0),
        ("2026-05-02", 45.0),
    ]
    assert summary.by_period[0].by_service[0].service == "Amazon WorkSpaces Applications"


def test_cost_summary_splits_workspaces_personal_pools_core():
    # First CE call = SERVICE grouping; second = USAGE_TYPE grouping (the breakdown).
    service_resp = {
        "ResultsByTime": [
            {
                "Groups": [
                    {
                        "Keys": ["Amazon WorkSpaces"],
                        "Metrics": {"UnblendedCost": {"Amount": "547.53", "Unit": "USD"}},
                    }
                ]
            }
        ]
    }
    usage_resp = {
        "ResultsByTime": [
            {
                "Groups": [
                    {
                        "Keys": ["APS1-AW-HWB5-0"],  # Personal AlwaysOn monthly
                        "Metrics": {"UnblendedCost": {"Amount": "386.25", "Unit": "USD"}},
                    },
                    {
                        "Keys": ["APS1-AW-HW-Pools-Stopped-Usage"],  # Pools
                        "Metrics": {"UnblendedCost": {"Amount": "146.68", "Unit": "USD"}},
                    },
                    {
                        "Keys": ["APS1-WH-ManagedInstances-Usage"],  # Core
                        "Metrics": {"UnblendedCost": {"Amount": "14.60", "Unit": "USD"}},
                    },
                ]
            }
        ]
    }
    calls = {"n": 0}

    def get_cost_and_usage(**kwargs):
        # The breakdown call carries a SERVICE filter + USAGE_TYPE grouping.
        is_breakdown = kwargs.get("GroupBy", [{}])[0].get("Key") == "USAGE_TYPE"
        calls["n"] += 1
        return usage_resp if is_breakdown else service_resp

    ce = types.SimpleNamespace(get_cost_and_usage=get_cost_and_usage)
    factory = FakeFactory({consts.COST_EXPLORER_API: ce})

    summary = cost.get_euc_cost_summary_core(factory, lookback_days=30)

    assert summary.workspaces_breakdown == {
        "WorkSpaces Personal": 386.25,
        "WorkSpaces Pools": 146.68,
        "WorkSpaces Core (Managed Instances)": 14.6,
    }
    assert calls["n"] == 2  # one SERVICE call + one USAGE_TYPE breakdown call


def test_cost_summary_classify_usage_type():
    assert (
        cost._classify_workspaces_usage_type("APS1-AW-HW-Pools-Stopped-Usage") == "WorkSpaces Pools"
    )
    assert (
        cost._classify_workspaces_usage_type("APS1-WH-ManagedInstances-Usage")
        == "WorkSpaces Core (Managed Instances)"
    )
    assert cost._classify_workspaces_usage_type("APS1-AW-HWB5-0") == "WorkSpaces Personal"


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


def test_cost_forecast_filters_to_discovered_euc_services():
    captured: dict = {}

    def get_cost_and_usage(**kwargs):
        # Discovery call: SERVICE grouping over recent actuals, incl. a non-EUC service.
        return {
            "ResultsByTime": [
                {
                    "Groups": [
                        {
                            "Keys": ["Amazon WorkSpaces"],
                            "Metrics": {"UnblendedCost": {"Amount": "500", "Unit": "USD"}},
                        },
                        {
                            "Keys": ["Amazon Elastic Compute Cloud - Compute"],
                            "Metrics": {"UnblendedCost": {"Amount": "999", "Unit": "USD"}},
                        },
                    ]
                }
            ]
        }

    def get_cost_forecast(**kwargs):
        captured.update(kwargs)
        return {
            "Total": {"Amount": "1234.564", "Unit": "USD"},
            "ForecastResultsByTime": [
                {
                    "TimePeriod": {"Start": "2026-07-04", "End": "2026-08-03"},
                    "MeanValue": "1234.564",
                    "PredictionIntervalLowerBound": "1000.0",
                    "PredictionIntervalUpperBound": "1500.0",
                }
            ],
        }

    ce = types.SimpleNamespace(
        get_cost_and_usage=get_cost_and_usage, get_cost_forecast=get_cost_forecast
    )
    factory = FakeFactory({consts.COST_EXPLORER_API: ce})

    forecast = cost.get_euc_cost_forecast_core(factory, days_ahead=30)

    # Non-EUC services must NOT leak into the forecast filter.
    assert captured["Filter"]["Dimensions"]["Values"] == ["Amazon WorkSpaces"]
    assert forecast.forecast_total == 1234.56
    assert forecast.by_period[0].lower == 1000.0
    assert forecast.by_period[0].upper == 1500.0
    assert forecast.filtered_services == ["Amazon WorkSpaces"]


def test_cost_forecast_without_history_returns_note_not_error():
    ce = types.SimpleNamespace(get_cost_and_usage=lambda **_: {"ResultsByTime": []})
    factory = FakeFactory({consts.COST_EXPLORER_API: ce})

    forecast = cost.get_euc_cost_forecast_core(factory)

    assert forecast.forecast_total is None
    assert any("no history" in n.lower() or "No EUC spend" in n for n in forecast.notes)


def test_compare_euc_costs_deltas_and_ranked_drivers():
    baseline_start = "2026-05-01"

    def get_cost_and_usage(**kwargs):
        window = kwargs["TimePeriod"]["Start"]
        two_dims = len(kwargs.get("GroupBy", [])) == 2
        if not two_dims:  # SERVICE totals per window
            if window == baseline_start:
                groups = [("Amazon WorkSpaces", "500.00")]
            else:
                groups = [
                    ("Amazon WorkSpaces", "700.00"),
                    ("Amazon WorkSpaces Applications", "100.00"),
                ]
            return {
                "ResultsByTime": [
                    {
                        "Groups": [
                            {
                                "Keys": [k],
                                "Metrics": {"UnblendedCost": {"Amount": v, "Unit": "USD"}},
                            }
                            for k, v in groups
                        ]
                    }
                ]
            }
        # SERVICE + USAGE_TYPE drivers per window
        if window == baseline_start:
            rows = [
                ("Amazon WorkSpaces", "APS1-AW-HWB5-0", "400.00"),
                ("Amazon WorkSpaces", "APS1-AW-HW-Pools-Stopped-Usage", "100.00"),
            ]
        else:
            rows = [
                ("Amazon WorkSpaces", "APS1-AW-HWB5-0", "450.00"),
                ("Amazon WorkSpaces", "APS1-AW-HW-Pools-Stopped-Usage", "250.00"),
                ("Amazon WorkSpaces Applications", "APS1-AppStreamHours", "100.00"),
            ]
        return {
            "ResultsByTime": [
                {
                    "Groups": [
                        {
                            "Keys": [svc, ut],
                            "Metrics": {"UnblendedCost": {"Amount": amt, "Unit": "USD"}},
                        }
                        for svc, ut, amt in rows
                    ]
                }
            ]
        }

    ce = types.SimpleNamespace(get_cost_and_usage=get_cost_and_usage)
    factory = FakeFactory({consts.COST_EXPLORER_API: ce})

    cmp = cost.compare_euc_costs_core(
        factory,
        start_date="2026-06-01",
        end_date="2026-07-01",
        baseline_start="2026-05-01",
        baseline_end="2026-06-01",
    )

    assert cmp.baseline_total == 500.0
    assert cmp.comparison_total == 800.0
    assert cmp.delta == 300.0
    assert cmp.delta_pct == 60.0
    assert cmp.by_service_delta["Amazon WorkSpaces"]["delta"] == 200.0
    assert cmp.by_service_delta["Amazon WorkSpaces Applications"]["delta"] == 100.0
    # Drivers ranked by |delta|: Pools +150, Applications +100, Personal +50.
    deltas = [(d.category, d.delta) for d in cmp.top_drivers]
    assert deltas[0] == ("WorkSpaces Pools", 150.0)
    assert deltas[1] == ("Amazon WorkSpaces Applications", 100.0)
    assert deltas[2] == ("WorkSpaces Personal", 50.0)


def test_compare_euc_costs_default_baseline_is_preceding_window():
    calls: list[str] = []

    def get_cost_and_usage(**kwargs):
        calls.append(kwargs["TimePeriod"]["Start"])
        return {"ResultsByTime": []}

    ce = types.SimpleNamespace(get_cost_and_usage=get_cost_and_usage)
    factory = FakeFactory({consts.COST_EXPLORER_API: ce})

    cmp = cost.compare_euc_costs_core(factory, start_date="2026-06-01", end_date="2026-07-01")

    assert cmp.baseline_start == "2026-05-02"  # preceding 30-day window
    assert cmp.baseline_end == "2026-06-01"
    assert cmp.comparison_start == "2026-06-01"
