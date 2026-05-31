# Copyright bengr. Licensed under the Apache License, Version 2.0 (the "License").
# You may not use this file except in compliance with the License.
# A copy of the License is located at http://www.apache.org/licenses/LICENSE-2.0
"""FastMCP application entry point for the WorkSpaces EUC MCP server."""

from __future__ import annotations

import argparse
import sys

from loguru import logger
from mcp.server.fastmcp import FastMCP

from . import consts
from .clients import ClientFactory
from .tools import inventory


def create_server(
    *,
    region: str | None = None,
    profile: str | None = None,
    enable_writes: bool = False,
    enable_destructive: bool = False,
    max_bulk_targets: int = consts.DEFAULT_MAX_BULK_TARGETS,
) -> FastMCP:
    """Build the FastMCP server, registering tools according to the safety flags."""
    factory = ClientFactory(region=region, profile=profile)
    mcp = FastMCP(consts.SERVER_NAME, instructions=consts.SERVER_INSTRUCTIONS)

    # Phase 1 read-only tools are always registered.
    inventory.register(mcp, factory)

    if enable_writes:
        # Phase 2 lifecycle tools land here, gated by max_bulk_targets / enable_destructive.
        logger.warning(
            "--enable-writes was set, but write/lifecycle tools are not implemented yet "
            "(planned for Phase 2). Running read-only."
        )

    return mcp


def main() -> None:
    parser = argparse.ArgumentParser(
        prog=consts.SERVER_NAME,
        description="Admin MCP server for the Amazon WorkSpaces EUC portfolio (read-only default).",
    )
    parser.add_argument("--region", help="AWS region (overrides AWS_REGION / profile default).")
    parser.add_argument("--profile", help="AWS named profile to use.")
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
    args = parser.parse_args()

    if args.enable_destructive and not args.enable_writes:
        parser.error("--enable-destructive requires --enable-writes.")

    logger.remove()
    logger.add(sys.stderr, level="INFO")

    mcp = create_server(
        region=args.region,
        profile=args.profile,
        enable_writes=args.enable_writes,
        enable_destructive=args.enable_destructive,
        max_bulk_targets=args.max_bulk_targets,
    )
    mcp.run()


if __name__ == "__main__":
    main()
