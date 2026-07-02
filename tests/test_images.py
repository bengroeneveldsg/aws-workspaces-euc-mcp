# Copyright bengroeneveldsg. Licensed under the Apache License, Version 2.0 (the "License").
# You may not use this file except in compliance with the License.
# A copy of the License is located at http://www.apache.org/licenses/LICENSE-2.0
"""Tests for the WorkSpaces Applications image-audit tool, using fake boto3 clients."""

from __future__ import annotations

import types
from datetime import UTC, datetime, timedelta

from workspaces_euc_mcp_server import consts
from workspaces_euc_mcp_server.tools import images


class FakeFactory:
    region = "ap-southeast-1"

    def __init__(self, client: object) -> None:
        self._client = client

    def client(self, service_name: str, region: str | None = None):
        assert service_name == consts.APPSTREAM_API
        return self._client


def _appstream(images_by_type: dict[str, list[dict]], builders: list[dict]):
    def describe_images(**kwargs):
        return {"Images": images_by_type.get(kwargs.get("Type"), [])}

    def describe_image_builders(**_):
        return {"ImageBuilders": builders}

    return types.SimpleNamespace(
        describe_images=describe_images,
        describe_image_builders=describe_image_builders,
        describe_app_block_builders=lambda **_: {"AppBlockBuilders": []},
    )


def test_audit_flags_stale_base_pinned_agent_and_running_builder():
    old_base = datetime.now(UTC) - timedelta(days=400)
    fresh_base = datetime.now(UTC) - timedelta(days=10)
    client = _appstream(
        {
            "PRIVATE": [
                {
                    "Name": "StaleImage",
                    "Visibility": "PRIVATE",
                    "Platform": "WINDOWS_SERVER_2022",
                    "State": "AVAILABLE",
                    "AppstreamAgentVersion": "10-02-2025",  # pinned, not LATEST
                    "Applications": [{"Name": "chrome", "Enabled": True}],
                    "PublicBaseImageReleasedDate": old_base,
                    "CreatedTime": old_base,
                },
                {
                    "Name": "GoodImage",
                    "Visibility": "PRIVATE",
                    "Platform": "WINDOWS_SERVER_2022",
                    "State": "AVAILABLE",
                    "AppstreamAgentVersion": "LATEST",
                    "Applications": [{"Name": "vscode", "Enabled": True}],
                    "PublicBaseImageReleasedDate": fresh_base,
                    "CreatedTime": fresh_base,
                },
            ],
            "SHARED": [],
        },
        builders=[
            {"Name": "Builder-Idle", "State": "STOPPED", "Platform": "WINDOWS_SERVER_2022"},
            {"Name": "Builder-Live", "State": "RUNNING", "Platform": "WINDOWS_SERVER_2025"},
        ],
    )
    report = images.audit_application_images_core(FakeFactory(client), "ap-southeast-1")

    assert report.image_count == 2
    assert report.image_builder_count == 2
    assert report.running_image_builders == 1

    issues = {(f.target, f.issue) for f in report.findings}
    # Stale base + pinned agent on StaleImage; running builder flagged; GoodImage clean.
    assert any(t == "StaleImage" and "base image released" in i for t, i in issues)
    assert any(t == "StaleImage" and "pinned" in i for t, i in issues)
    assert any(t == "Builder-Live" and "RUNNING" in i for t, i in issues)
    assert not any(t == "GoodImage" for t, _ in issues)


def test_audit_flags_shared_visibility_and_records_errors():
    from botocore.exceptions import ClientError

    def describe_images(**kwargs):
        if kwargs.get("Type") == "SHARED":
            return {
                "Images": [
                    {
                        "Name": "SharedIn",
                        "Visibility": "SHARED",
                        "State": "AVAILABLE",
                        "AppstreamAgentVersion": "LATEST",
                    }
                ]
            }
        return {"Images": []}

    def describe_image_builders(**_):
        raise ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "no"}},
            "DescribeImageBuilders",
        )

    client = types.SimpleNamespace(
        describe_images=describe_images,
        describe_image_builders=describe_image_builders,
        describe_app_block_builders=lambda **_: {"AppBlockBuilders": []},
    )
    report = images.audit_application_images_core(FakeFactory(client), "ap-southeast-1")

    assert any(f.target == "SharedIn" and "SHARED" in f.issue for f in report.findings)
    assert any(e.operation == "DescribeImageBuilders" for e in report.errors)


def test_audit_flags_running_app_block_builder():
    client = types.SimpleNamespace(
        describe_images=lambda **_: {"Images": []},
        describe_image_builders=lambda **_: {"ImageBuilders": []},
        describe_app_block_builders=lambda **_: {
            "AppBlockBuilders": [
                {"Name": "abb-live", "State": "RUNNING", "Platform": "WINDOWS_SERVER_2022"},
                {"Name": "abb-idle", "State": "STOPPED"},
            ]
        },
    )
    report = images.audit_application_images_core(FakeFactory(client), "ap-southeast-1")

    assert report.app_block_builder_count == 2
    assert report.running_app_block_builders == 1
    assert any(
        f.target == "abb-live" and "App block builder is RUNNING" in f.issue
        for f in report.findings
    )


def test_audit_workspace_images_flags_error_and_sharing():
    workspaces = types.SimpleNamespace(
        describe_workspace_images=lambda **_: {
            "Images": [
                {"ImageId": "wsi-err", "Name": "Broken", "State": "ERROR"},
                {"ImageId": "wsi-shared", "Name": "Golden", "State": "AVAILABLE"},
            ]
        },
        describe_workspace_image_permissions=lambda **kw: {
            "ImagePermissions": [{"SharedAccountId": "111122223333"}]
            if kw.get("ImageId") == "wsi-shared"
            else []
        },
    )

    class WsFactory:
        region = "ap-southeast-1"

        def client(self, service_name, region=None):
            assert service_name == consts.WORKSPACES_API
            return workspaces

    report = images.audit_workspace_images_core(WsFactory(), "ap-southeast-1")

    assert report.image_count == 2
    assert any(f.target == "Broken" and "ERROR" in f.issue for f in report.findings)
    assert any(f.target == "Golden" and "111122223333" in f.issue for f in report.findings)
