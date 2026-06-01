# Copyright bengroeneveldsg. Licensed under the Apache License, Version 2.0 (the "License").
# You may not use this file except in compliance with the License.
# A copy of the License is located at http://www.apache.org/licenses/LICENSE-2.0
"""Tests for the WorkSpaces Secure Browser tools."""

from __future__ import annotations

import types

from workspaces_euc_mcp_server import consts
from workspaces_euc_mcp_server.tools import secure_browser


class FakeFactory:
    region = "us-east-1"

    def __init__(self, clients: dict[str, object]) -> None:
        self._clients = clients

    def client(self, service_name: str, region: str | None = None):
        if service_name not in self._clients:
            raise AssertionError(f"unexpected client requested: {service_name}")
        return self._clients[service_name]


def test_portal_details_resolves_settings():
    web = types.SimpleNamespace(
        get_portal=lambda **_: {
            "portal": {
                "portalArn": "arn:portal/1",
                "displayName": "P1",
                "authenticationType": "Standard",
                "portalStatus": "Active",
                "userSettingsArn": "arn:us/1",
                "networkSettingsArn": "arn:ns/1",
                "browserSettingsArn": "arn:bs/1",
            }
        },
        get_user_settings=lambda **_: {
            "userSettings": {
                "copyAllowed": "Disabled",
                "downloadAllowed": "Enabled",
                "printAllowed": "Disabled",
                "idleDisconnectTimeoutInMinutes": 15,
                "associatedPortalArns": ["arn:portal/1"],  # should be dropped
            }
        },
        get_network_settings=lambda **_: {
            "networkSettings": {
                "vpcId": "vpc-1",
                "subnetIds": ["s-1"],
                "securityGroupIds": ["sg-1"],
            }
        },
    )
    factory = FakeFactory({consts.SECURE_BROWSER_API: web})

    d = secure_browser.get_secure_browser_portal_details_core(factory, "arn:portal/1", "us-east-1")

    assert d.display_name == "P1"
    assert d.user_settings["downloadAllowed"] == "Enabled"
    assert "associatedPortalArns" not in d.user_settings  # bulky/identifying field dropped
    assert d.network["vpcId"] == "vpc-1"
    assert d.has_browser_policy is True
    assert d.has_data_protection is False


def test_portal_usage_empty_explains_session_model():
    cw = types.SimpleNamespace(
        get_metric_data=lambda **_: {"MetricDataResults": []}  # no data
    )
    factory = FakeFactory({consts.CLOUDWATCH_API: cw})

    usage = secure_browser.get_secure_browser_portal_usage_core(
        factory, "arn:aws:workspaces-web:r:a:portal/abc123", "us-east-1"
    )

    assert usage.target_type == consts.PRODUCT_SECURE_BROWSER
    assert usage.target_id.endswith("portal/abc123")
    assert "Session Logger" in (usage.summary or "")


def test_portal_id_extracted_from_arn():
    assert secure_browser._portal_id("arn:aws:workspaces-web:r:a:portal/abc123") == "abc123"
    assert secure_browser._portal_id("abc123") == "abc123"
