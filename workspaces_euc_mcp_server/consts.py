# Copyright bengroeneveldsg. Licensed under the Apache License, Version 2.0 (the "License").
# You may not use this file except in compliance with the License.
# A copy of the License is located at http://www.apache.org/licenses/LICENSE-2.0
"""Constants for the WorkSpaces EUC MCP server.

Note the distinction between **product names** (current official AWS branding, used everywhere a
human reads) and **API identifiers** (the legacy boto3 client names the SDK still requires).
"""

from . import __version__

SERVER_NAME = "workspaces-euc-mcp-server"
SERVER_VERSION = __version__

# boto3 client names (API identifiers — NOT product names).
WORKSPACES_API = "workspaces"  # Amazon WorkSpaces Personal, Pools, and Core all use this client.
APPSTREAM_API = "appstream"  # Amazon WorkSpaces Applications (formerly AppStream 2.0).
SECURE_BROWSER_API = "workspaces-web"  # Amazon WorkSpaces Secure Browser (formerly WorkSpaces Web).
DIRECTORY_API = "ds"  # AWS Directory Service (shared dependency).
CLOUDWATCH_API = "cloudwatch"  # Telemetry for diagnostics/cost tools.

# Current official product names (used in all human-facing output).
PRODUCT_WORKSPACES_PERSONAL = "Amazon WorkSpaces Personal"
PRODUCT_WORKSPACES_POOLS = "Amazon WorkSpaces Pools"
PRODUCT_WORKSPACES_APPLICATIONS = "Amazon WorkSpaces Applications"
PRODUCT_SECURE_BROWSER = "Amazon WorkSpaces Secure Browser"

# Default blast-radius cap for any (future, Phase 2) bulk mutation.
DEFAULT_MAX_BULK_TARGETS = 25

SERVER_INSTRUCTIONS = """\
Administrator-focused MCP server for the Amazon WorkSpaces End User Computing portfolio:
WorkSpaces Personal, WorkSpaces Pools, WorkSpaces Applications, WorkSpaces Secure Browser, and
WorkSpaces Core. Tools are read-only by default and synthesize cross-service results for
inventory, troubleshooting, and cost/utilization optimization. Always refer to services by their
current official names. Write/lifecycle operations are not enabled unless the server was launched
with --enable-writes and the matching IAM permissions are present.
"""
