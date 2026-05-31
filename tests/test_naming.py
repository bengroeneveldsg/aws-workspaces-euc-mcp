# Copyright bengroeneveldsg. Licensed under the Apache License, Version 2.0 (the "License").
# You may not use this file except in compliance with the License.
# A copy of the License is located at http://www.apache.org/licenses/LICENSE-2.0
"""Guardrails for legacy-name acceptance and current-name output.

Users keep saying "AppStream"; the server must accept that term (so the client model routes to the
WorkSpaces Applications tools) while always presenting the current official name.
"""

from __future__ import annotations

import asyncio

from workspaces_euc_mcp_server import consts
from workspaces_euc_mcp_server.server import create_server


def test_legacy_aliases_map_to_current_names():
    assert consts.LEGACY_NAME_ALIASES["appstream"] == consts.PRODUCT_WORKSPACES_APPLICATIONS
    assert consts.LEGACY_NAME_ALIASES["appstream 2.0"] == consts.PRODUCT_WORKSPACES_APPLICATIONS
    assert consts.LEGACY_NAME_ALIASES["workspaces web"] == consts.PRODUCT_SECURE_BROWSER


def test_server_instructions_teach_the_appstream_mapping():
    text = consts.SERVER_INSTRUCTIONS.lower()
    assert "appstream" in text
    assert "workspaces applications" in text


def test_application_fleet_tool_advertises_appstream_alias():
    mcp = create_server(region="us-east-1")
    tools = {t.name: t for t in asyncio.run(mcp.list_tools())}
    desc = (tools["diagnose_application_fleet"].description or "").lower()
    assert "appstream" in desc  # so "AppStream" queries route here
    assert "workspaces applications" in desc  # and the current name is present


def test_output_uses_current_product_names_only():
    # The product constants used in tool output must be the current official names.
    assert consts.PRODUCT_WORKSPACES_APPLICATIONS == "Amazon WorkSpaces Applications"
    assert consts.PRODUCT_SECURE_BROWSER == "Amazon WorkSpaces Secure Browser"
    assert "AppStream" not in consts.PRODUCT_WORKSPACES_APPLICATIONS
