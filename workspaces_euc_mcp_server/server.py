# Copyright bengroeneveldsg. Licensed under the Apache License, Version 2.0 (the "License").
# You may not use this file except in compliance with the License.
# A copy of the License is located at http://www.apache.org/licenses/LICENSE-2.0
"""FastMCP application entry point for the WorkSpaces EUC MCP server."""

from __future__ import annotations

import argparse
import os
import sys

from loguru import logger
from mcp.server.fastmcp import FastMCP

from . import consts
from .clients import ClientFactory
from .sso import SsoAutoLogin
from .tools import (
    _common,
    access,
    cost,
    destructive,
    diagnostics,
    governance,
    health,
    images,
    inventory,
    lifecycle,
    performance,
    pricing,
    reporting,
    secure_browser,
)


def create_server(
    *,
    region: str | None = None,
    profile: str | None = None,
    role_arn: str | None = None,
    external_id: str | None = None,
    enable_writes: bool = False,
    enable_destructive: bool = False,
    max_bulk_targets: int = consts.DEFAULT_MAX_BULK_TARGETS,
    sso_auto_login: bool = True,
) -> FastMCP:
    """Build the FastMCP server, registering tools according to the safety flags."""
    factory = ClientFactory(
        region=region, profile=profile, role_arn=role_arn, external_id=external_id
    )

    # On by default: when an AWS call fails with an expired SSO token, auto-launch `aws sso login`
    # (opens the browser) so the user never has to use a terminal. Disable for headless/CI.
    _common.register_sso_handler(
        SsoAutoLogin(profile=profile, enabled=sso_auto_login) if sso_auto_login else None
    )
    logger.info(
        "SSO auto-login {}: expired tokens {} trigger `aws sso login`.",
        "enabled" if sso_auto_login else "disabled",
        "will" if sso_auto_login else "will NOT",
    )

    mcp = FastMCP(consts.SERVER_NAME, instructions=consts.SERVER_INSTRUCTIONS)

    # Phase 1 read-only tools are always registered.
    inventory.register(mcp, factory)
    diagnostics.register(mcp, factory)
    cost.register(mcp, factory)
    reporting.register(mcp, factory)
    performance.register(mcp, factory)
    secure_browser.register(mcp, factory)
    images.register(mcp, factory)
    governance.register(mcp, factory)
    access.register(mcp, factory)
    health.register(mcp, factory)
    pricing.register(mcp, factory)

    if enable_writes:
        logger.info(
            "Write tools enabled (Tier 2). Mutations are dry-run unless confirm=true and are "
            "capped at {} targets per bulk action.",
            max_bulk_targets,
        )
        lifecycle.register(
            mcp,
            factory,
            max_bulk_targets=max_bulk_targets,
            enable_destructive=enable_destructive,
        )

        if enable_destructive:
            logger.warning(
                "Destructive tools enabled (Tier 3): terminate/rebuild/restore. These require "
                "confirm=true AND an exact acknowledgement phrase to execute."
            )
            destructive.register(mcp, factory, max_bulk_targets=max_bulk_targets)

    return mcp


def main() -> None:
    parser = argparse.ArgumentParser(
        prog=consts.SERVER_NAME,
        description="Admin MCP server for the Amazon WorkSpaces EUC portfolio (read-only default).",
    )
    parser.add_argument("--region", help="AWS region (overrides AWS_REGION / profile default).")
    parser.add_argument("--profile", help="AWS named profile to use.")
    parser.add_argument(
        "--assume-role-arn",
        help="Cross-account IAM role ARN to assume (multi-account / MSP). The caller needs "
        "sts:AssumeRole on it; the role needs the matching tier permissions.",
    )
    parser.add_argument(
        "--external-id",
        help="ExternalId to pass when assuming --assume-role-arn (if the role requires one).",
    )
    parser.add_argument(
        "--enable-writes",
        action="store_true",
        help="Register Phase 2 lifecycle (write) tools. Off by default.",
    )
    parser.add_argument(
        "--enable-destructive",
        action="store_true",
        help="Allow destructive ops (terminate/rebuild/restore). Requires --enable-writes.",
    )
    parser.add_argument(
        "--max-bulk-targets",
        type=int,
        default=consts.DEFAULT_MAX_BULK_TARGETS,
        help="Blast-radius cap for bulk mutations (Phase 2).",
    )
    parser.add_argument(
        "--sso-auto-login",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="On an expired SSO token, auto-launch `aws sso login` (opens your browser) so you "
        "re-authenticate without a terminal. ON by default; disable with --no-sso-auto-login "
        "(or WORKSPACES_EUC_SSO_AUTO_LOGIN=0) for headless/CI environments.",
    )
    args = parser.parse_args()

    if args.enable_destructive and not args.enable_writes:
        parser.error("--enable-destructive requires --enable-writes.")

    # On by default; an explicit env var (if set) overrides the flag, so it can force-disable too.
    sso_auto_login = args.sso_auto_login
    _sso_env = os.environ.get("WORKSPACES_EUC_SSO_AUTO_LOGIN")
    if _sso_env is not None:
        sso_auto_login = _sso_env.strip().lower() in ("1", "true", "yes", "on")

    logger.remove()
    logger.add(sys.stderr, level=os.environ.get("FASTMCP_LOG_LEVEL", "INFO").upper())

    mcp = create_server(
        region=args.region,
        profile=args.profile,
        role_arn=args.assume_role_arn,
        external_id=args.external_id,
        enable_writes=args.enable_writes,
        enable_destructive=args.enable_destructive,
        max_bulk_targets=args.max_bulk_targets,
        sso_auto_login=sso_auto_login,
    )
    mcp.run()


if __name__ == "__main__":
    main()
