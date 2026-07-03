# Copyright bengroeneveldsg. Licensed under the Apache License, Version 2.0 (the "License").
# You may not use this file except in compliance with the License.
# A copy of the License is located at http://www.apache.org/licenses/LICENSE-2.0
"""Tests for the governance tools (audit trail + service quotas)."""

from __future__ import annotations

import json
import types
from datetime import UTC, datetime

from workspaces_euc_mcp_server import consts
from workspaces_euc_mcp_server.tools import governance


class FakeFactory:
    region = "ap-southeast-1"

    def __init__(self, clients: dict[str, object]) -> None:
        self._clients = clients

    def client(self, service_name: str, region: str | None = None):
        assert service_name in self._clients, f"unexpected client: {service_name}"
        return self._clients[service_name]


def _event(name, source, user, *, read_only=False, error=None, resource=None):
    detail = {"sourceIPAddress": "203.0.113.5", "awsRegion": "ap-southeast-1"}
    if error:
        detail["errorCode"] = error
    return {
        "EventName": name,
        "EventSource": source,
        "Username": user,
        "ReadOnly": read_only,
        "EventTime": datetime(2026, 5, 30, 9, 0, tzinfo=UTC),
        "Resources": [{"ResourceName": resource}] if resource else [],
        "CloudTrailEvent": json.dumps(detail),
    }


def test_audit_trail_filters_euc_and_flags_destructive():
    events = [
        _event("TerminateWorkspaces", "workspaces.amazonaws.com", "admin", resource="ws-abc"),
        _event("CreateFleet", "appstream.amazonaws.com", "ops"),
        _event("RunInstances", "ec2.amazonaws.com", "someone"),  # non-EUC, must be dropped
    ]
    trail = types.SimpleNamespace(lookup_events=lambda **_: {"Events": events})
    factory = FakeFactory({consts.CLOUDTRAIL_API: trail})

    report = governance.get_euc_audit_trail_core(factory, "ap-southeast-1", lookback_days=7)

    names = {e.event_name for e in report.events}
    assert names == {"TerminateWorkspaces", "CreateFleet"}  # EC2 dropped
    assert report.total_events == 2
    assert any(
        "Destructive" in f.issue and "TerminateWorkspaces" in f.issue for f in report.findings
    )
    # Service label resolved from the event source.
    assert any(e.service == "Amazon WorkSpaces" for e in report.events)


def test_audit_trail_flags_access_denied():
    events = [_event("DeletePortal", "workspaces-web.amazonaws.com", "x", error="AccessDenied")]
    trail = types.SimpleNamespace(lookup_events=lambda **_: {"Events": events})
    factory = FakeFactory({consts.CLOUDTRAIL_API: trail})

    report = governance.get_euc_audit_trail_core(factory, "ap-southeast-1")

    assert any("AccessDenied" in f.issue and f.severity == "warning" for f in report.findings)


def _trail_event(name, source, eid, ts):
    return {
        "EventName": name,
        "EventSource": source,
        "Username": "ops",
        "ReadOnly": False,
        "EventTime": ts,
        "EventId": eid,
        "Resources": [],
        "CloudTrailEvent": "{}",
    }


def test_audit_trail_targeted_lookup_rescues_buried_mutations(monkeypatch):
    # Regression: account write-noise (constant SSM/EC2 events) exhausted the sweep's page
    # budget within ~1h of history, so a CreateFleet from 5 days earlier was reported as
    # "no changes". The curated event-name lookups must find it across the full window.
    monkeypatch.setattr(governance, "_LOOKUP_PACING_SECONDS", 0)
    noise_ts = datetime(2026, 7, 3, 12, 0, tzinfo=UTC)
    create_ts = datetime(2026, 6, 28, 4, 48, tzinfo=UTC)

    def lookup_events(**kwargs):
        attr = kwargs["LookupAttributes"][0]
        if attr["AttributeKey"] == "ReadOnly":
            page = int(kwargs.get("NextToken", 0))
            events = [
                _trail_event(
                    "UpdateInstanceInformation", "ssm.amazonaws.com", f"n{page}-{i}", noise_ts
                )
                for i in range(50)
            ]
            return {"Events": events, "NextToken": str(page + 1)}  # endless noise
        if attr["AttributeKey"] == "EventName" and attr["AttributeValue"] == "CreateFleet":
            return {
                "Events": [
                    _trail_event("CreateFleet", "appstream.amazonaws.com", "e-1", create_ts),
                    # Same event name from another service must be dropped by source filter.
                    _trail_event("CreateFleet", "gamelift.amazonaws.com", "e-2", create_ts),
                ]
            }
        return {"Events": []}

    trail = types.SimpleNamespace(lookup_events=lookup_events)
    factory = FakeFactory({consts.CLOUDTRAIL_API: trail})

    report = governance.get_euc_audit_trail_core(
        factory, "ap-southeast-1", lookback_days=14, service="applications"
    )

    assert [e.event_name for e in report.events] == ["CreateFleet"]
    assert report.window_fully_covered is False
    assert report.scanned_back_to is not None
    assert any("PARTIAL WINDOW COVERAGE" in n for n in report.notes)


def test_audit_trail_dedupes_sweep_and_targeted(monkeypatch):
    monkeypatch.setattr(governance, "_LOOKUP_PACING_SECONDS", 0)
    ts = datetime(2026, 6, 28, 4, 48, tzinfo=UTC)
    fleet_event = _trail_event("CreateFleet", "appstream.amazonaws.com", "same-id", ts)

    def lookup_events(**kwargs):
        attr = kwargs["LookupAttributes"][0]
        if attr["AttributeKey"] == "ReadOnly":
            return {"Events": [fleet_event]}  # no NextToken -> window fully covered
        if attr["AttributeKey"] == "EventName" and attr["AttributeValue"] == "CreateFleet":
            return {"Events": [fleet_event]}  # same event found again by the targeted path
        return {"Events": []}

    trail = types.SimpleNamespace(lookup_events=lookup_events)
    factory = FakeFactory({consts.CLOUDTRAIL_API: trail})

    report = governance.get_euc_audit_trail_core(
        factory, "ap-southeast-1", lookback_days=7, service="applications"
    )

    assert report.total_events == 1
    assert report.window_fully_covered is True


def test_audit_trail_service_all_skips_targeted_lookups():
    seen_attrs: list[str] = []

    def lookup_events(**kwargs):
        seen_attrs.append(kwargs["LookupAttributes"][0]["AttributeKey"])
        return {"Events": []}

    trail = types.SimpleNamespace(lookup_events=lookup_events)
    factory = FakeFactory({consts.CLOUDTRAIL_API: trail})

    governance.get_euc_audit_trail_core(factory, "ap-southeast-1", service="all")

    assert seen_attrs == ["ReadOnly"]  # sweep only; no per-event-name calls for "all"


def test_audit_trail_lookback_capped_at_90():
    trail = types.SimpleNamespace(lookup_events=lambda **_: {"Events": []})
    factory = FakeFactory({consts.CLOUDTRAIL_API: trail})
    report = governance.get_euc_audit_trail_core(factory, "ap-southeast-1", lookback_days=365)
    assert report.lookback_days == 90


def test_service_quotas_computes_headroom_and_flags():
    quotas = {
        "Quotas": [
            {
                "QuotaName": "WorkSpaces",
                "QuotaCode": "L-1",
                "Value": 200.0,
                "Adjustable": True,
                "UsageMetric": {
                    "MetricNamespace": "AWS/Usage",
                    "MetricName": "ResourceCount",
                    "MetricDimensions": {"Service": "WorkSpaces", "Resource": "WorkSpace"},
                    "MetricStatisticRecommendation": "Maximum",
                },
            },
            {
                "QuotaName": "WorkSpaces Pools",
                "QuotaCode": "L-2",
                "Value": 10.0,
                "Adjustable": False,
                "UsageMetric": {
                    "MetricNamespace": "AWS/Usage",
                    "MetricName": "ResourceCount",
                    "MetricDimensions": {"Service": "WorkSpaces", "Resource": "Pool"},
                    "MetricStatisticRecommendation": "Maximum",
                },
            },
            {"QuotaName": "Zero thing", "QuotaCode": "L-3", "Value": 0.0, "Adjustable": True},
        ]
    }
    sq = types.SimpleNamespace(list_service_quotas=lambda **_: quotas)
    # u0 -> WorkSpaces (14/200 = 7%), u1 -> Pools (9/10 = 90% -> flagged, not adjustable)
    cw = types.SimpleNamespace(
        get_metric_data=lambda **_: {
            "MetricDataResults": [
                {"Id": "u0", "Values": [14.0]},
                {"Id": "u1", "Values": [9.0]},
            ]
        }
    )
    factory = FakeFactory({consts.SERVICE_QUOTAS_API: sq, consts.CLOUDWATCH_API: cw})

    report = governance.get_euc_service_quotas_core(
        factory, "ap-southeast-1", service="workspaces", approaching_pct=80.0
    )

    by_name = {q.quota_name: q for q in report.quotas}
    assert "Zero thing" not in by_name  # zero-limit hidden by default
    assert by_name["WorkSpaces"].utilization_pct == 7.0
    assert by_name["WorkSpaces Pools"].utilization_pct == 90.0
    # Only the Pools quota (90% >= 80) is flagged, and noted as non-adjustable.
    assert len(report.findings) == 1
    assert "WorkSpaces Pools" in report.findings[0].target
    assert "NOT adjustable" in report.findings[0].issue
