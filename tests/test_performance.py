# Copyright bengroeneveldsg. Licensed under the Apache License, Version 2.0 (the "License").
# You may not use this file except in compliance with the License.
# A copy of the License is located at http://www.apache.org/licenses/LICENSE-2.0
"""Tests for the performance metrics and bundle right-sizing tools."""

from __future__ import annotations

import types

from workspaces_euc_mcp_server import consts
from workspaces_euc_mcp_server.tools import performance


class FakeFactory:
    region = "us-east-1"

    def __init__(self, clients: dict[str, object]) -> None:
        self._clients = clients

    def client(self, service_name: str, region: str | None = None):
        if service_name not in self._clients:
            raise AssertionError(f"unexpected client requested: {service_name}")
        return self._clients[service_name]


def _cloudwatch(series_by_metric: dict[str, dict[str, list[float]]]):
    """series_by_metric: {MetricName: {'Average': [...], 'Maximum': [...]}}."""

    def get_metric_data(**kwargs):
        results = []
        for q in kwargs["MetricDataQueries"]:
            name = q["MetricStat"]["Metric"]["MetricName"]
            stat = q["MetricStat"]["Stat"]
            vals = series_by_metric.get(name, {}).get(stat, [])
            results.append({"Id": q["Id"], "Values": vals})
        return {"MetricDataResults": results}

    return types.SimpleNamespace(get_metric_data=get_metric_data)


def _workspaces_client(workspaces: list[dict]):
    return types.SimpleNamespace(describe_workspaces=lambda **_: {"Workspaces": workspaces})


def test_get_workspace_performance_reduces_series():
    cw = _cloudwatch(
        {
            "CPUUsage": {"Average": [10.0, 20.0, 30.0], "Maximum": [15.0, 25.0, 40.0]},
            "MemoryUsage": {"Average": [40.0, 44.0, 48.0], "Maximum": [50.0, 55.0, 60.0]},
        }
    )
    factory = FakeFactory({consts.CLOUDWATCH_API: cw})

    report = performance.get_workspace_performance_core(factory, ["ws-1"], "us-east-1", 3)

    wp = report.workspaces[0]
    cpu = wp.metrics["CPUUsage"]
    assert cpu.latest == 30.0  # ascending scan -> last value
    assert cpu.average == 20.0
    assert cpu.peak == 40.0
    assert cpu.unit == "Percent"
    assert wp.metrics["MemoryUsage"].peak == 60.0


def test_get_workspace_performance_handles_stopped_desktop():
    cw = _cloudwatch({})  # no datapoints for any metric
    factory = FakeFactory({consts.CLOUDWATCH_API: cw})

    report = performance.get_workspace_performance_core(factory, ["ws-stopped"], "us-east-1", 3)

    wp = report.workspaces[0]
    assert wp.metrics == {}
    assert "no performance datapoints" in (wp.note or "").lower()


def test_rightsizing_recommends_downsize_for_low_usage():
    workspaces = _workspaces_client(
        [{"WorkspaceId": "ws-big", "WorkspaceProperties": {"ComputeTypeName": "POWER"}}]
    )
    cw = _cloudwatch(
        {
            "CPUUsage": {"Average": [3.0] * 8, "Maximum": [8.0] * 8},
            "MemoryUsage": {"Average": [12.0] * 8, "Maximum": [20.0] * 8},
        }
    )
    factory = FakeFactory({consts.WORKSPACES_API: workspaces, consts.CLOUDWATCH_API: cw})

    report = performance.recommend_bundle_rightsizing_core(factory, "us-east-1", lookback_days=7)

    assert len(report.recommendations) == 1
    rec = report.recommendations[0]
    assert rec.current == "POWER"
    assert rec.recommended == "PERFORMANCE"  # one step down
    assert rec.kind == "bundle_rightsizing"


def test_rightsizing_recommends_upsize_for_pressure():
    workspaces = _workspaces_client(
        [{"WorkspaceId": "ws-small", "WorkspaceProperties": {"ComputeTypeName": "STANDARD"}}]
    )
    cw = _cloudwatch(
        {
            "CPUUsage": {"Average": [80.0] * 8, "Maximum": [97.0] * 8},
            "MemoryUsage": {"Average": [70.0] * 8, "Maximum": [80.0] * 8},
        }
    )
    factory = FakeFactory({consts.WORKSPACES_API: workspaces, consts.CLOUDWATCH_API: cw})

    report = performance.recommend_bundle_rightsizing_core(factory, "us-east-1", lookback_days=7)

    rec = report.recommendations[0]
    assert rec.current == "STANDARD"
    assert rec.recommended == "PERFORMANCE"  # one step up
    assert rec.confidence == "high"  # peak CPU > 95


def test_rightsizing_skips_graphics_and_no_data():
    workspaces = _workspaces_client(
        [
            {"WorkspaceId": "ws-gfx", "WorkspaceProperties": {"ComputeTypeName": "GRAPHICS_G4DN"}},
            {"WorkspaceId": "ws-none", "WorkspaceProperties": {"ComputeTypeName": "POWER"}},
        ]
    )
    cw = _cloudwatch(
        {
            # ws-gfx would be low usage, but graphics families are excluded; ws-none has no data.
            "CPUUsage": {"Average": [], "Maximum": []},
            "MemoryUsage": {"Average": [], "Maximum": []},
        }
    )
    factory = FakeFactory({consts.WORKSPACES_API: workspaces, consts.CLOUDWATCH_API: cw})

    report = performance.recommend_bundle_rightsizing_core(factory, "us-east-1", lookback_days=7)

    assert report.recommendations == []
    assert any("skipped" in n for n in report.notes)
