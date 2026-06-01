# Copyright bengroeneveldsg. Licensed under the Apache License, Version 2.0 (the "License").
# You may not use this file except in compliance with the License.
# A copy of the License is located at http://www.apache.org/licenses/LICENSE-2.0
"""Tests for the boto3 client factory, including cross-account assume-role."""

from __future__ import annotations

import types
from datetime import UTC, datetime, timedelta

from workspaces_euc_mcp_server import clients


def test_factory_without_role_uses_base_session():
    f = clients.ClientFactory(region="us-east-1")
    # No role assumed: effective session is the base session.
    assert f._session is f._base_session
    assert f.region == "us-east-1"


def test_factory_assumes_role_when_arn_given(monkeypatch):
    calls = {}

    def fake_assume_role(**kwargs):
        calls.update(kwargs)
        return {
            "Credentials": {
                "AccessKeyId": "AKIAFAKE",
                "SecretAccessKey": "secret",
                "SessionToken": "token",
                "Expiration": datetime.now(UTC) + timedelta(hours=1),
            }
        }

    fake_sts = types.SimpleNamespace(assume_role=fake_assume_role)

    # Patch the base session's sts client to our fake, and the assumed boto3.Session build.
    real_session_init = clients.boto3.Session

    class FakeBaseSession:
        region_name = "ap-southeast-1"

        def client(self, name, **_):
            assert name == "sts"
            return fake_sts

    monkeypatch.setattr(clients.boto3, "Session", lambda *a, **k: FakeBaseSession())
    # The assumed-session build wraps a botocore session; just return a sentinel boto3 Session.
    monkeypatch.setattr(
        clients,
        "_botocore_get_session",
        lambda: types.SimpleNamespace(set_config_variable=lambda *a, **k: None),
    )
    sentinel = object()

    # boto3.Session(botocore_session=...) -> the assumed-role session; otherwise the base session.
    def session_factory(*a, **k):
        return sentinel if "botocore_session" in k else FakeBaseSession()

    monkeypatch.setattr(clients.boto3, "Session", session_factory)

    f = clients.ClientFactory(
        region="ap-southeast-1",
        role_arn="arn:aws:iam::222222222222:role/EucReadOnly",
        external_id="xyz",
    )

    assert calls["RoleArn"] == "arn:aws:iam::222222222222:role/EucReadOnly"
    assert calls["ExternalId"] == "xyz"
    assert calls["RoleSessionName"] == "workspaces-euc-mcp-server"
    assert f._session is sentinel  # clients come from the assumed-role session

    # restore (monkeypatch auto-undoes, but be explicit about not leaking)
    clients.boto3.Session = real_session_init
