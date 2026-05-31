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
COST_EXPLORER_API = "ce"  # Cost Explorer (global; account-wide, not region-scoped).
PRICING_API = "pricing"  # AWS Price List (global).

# Cost Explorer is a global endpoint served from us-east-1 regardless of the working region.
COST_EXPLORER_REGION = "us-east-1"

# Cost Explorer SERVICE dimension values that map to the EUC portfolio. Names can vary by
# account/era; the filter simply ignores values that produce no results.
EUC_COST_EXPLORER_SERVICES = [
    "Amazon WorkSpaces",
    "Amazon AppStream",
    "Amazon WorkSpaces Web",
    "Amazon WorkSpaces Secure Browser",
]

# Current official product names (used in all human-facing output).
PRODUCT_WORKSPACES_PERSONAL = "Amazon WorkSpaces Personal"
PRODUCT_WORKSPACES_POOLS = "Amazon WorkSpaces Pools"
PRODUCT_WORKSPACES_APPLICATIONS = "Amazon WorkSpaces Applications"
PRODUCT_SECURE_BROWSER = "Amazon WorkSpaces Secure Browser"

# Legacy / former product names mapped to their current official name. Accept these as INPUT
# (users will keep saying them) but always emit the current name in output. This is surfaced to the
# MCP client model via the server instructions and tool descriptions so a query about, say,
# "AppStream fleets" routes to the WorkSpaces Applications tools.
LEGACY_NAME_ALIASES = {
    "appstream": PRODUCT_WORKSPACES_APPLICATIONS,
    "appstream 2.0": PRODUCT_WORKSPACES_APPLICATIONS,
    "amazon appstream": PRODUCT_WORKSPACES_APPLICATIONS,
    "amazon appstream 2.0": PRODUCT_WORKSPACES_APPLICATIONS,
    "workspaces web": PRODUCT_SECURE_BROWSER,
    "amazon workspaces web": PRODUCT_SECURE_BROWSER,
}

# Default blast-radius cap for any (future, Phase 2) bulk mutation.
DEFAULT_MAX_BULK_TARGETS = 25

# WorkSpaces Personal general-purpose compute types, smallest -> largest. Used by bundle
# right-sizing to step a desktop up/down one size. Graphics families are intentionally excluded
# (different hardware/pricing; not safe to auto-suggest across).
WORKSPACES_COMPUTE_ORDER = ["VALUE", "STANDARD", "PERFORMANCE", "POWER", "POWERPRO"]

# Performance metrics published natively to the AWS/WorkSpaces namespace (keyed by WorkspaceId),
# with a best-effort unit label. No CloudWatch agent is required for these.
WORKSPACES_PERFORMANCE_METRICS = [
    ("CPUUsage", "Percent"),
    ("MemoryUsage", "Percent"),
    ("GPUUsage", "Percent"),
    ("FramesPerSecond", "Count"),
    ("RootVolumeDiskUsage", "Percent"),
    ("UserVolumeDiskUsage", "Percent"),
    ("InSessionLatency", "Milliseconds"),
    ("UpTime", "Seconds"),
    ("Bandwidth", "Bytes"),
    ("BandwidthInbound", "Bytes"),
    ("CPUQueueLength", "Count"),
    ("MemoryPageHardFaults", "Count"),
    ("RootVolumeDiskIOQueueLength", "Count"),
    ("UserVolumeDiskIOQueueLength", "Count"),
    ("TCPRetransmissionRate", "Percent"),
    ("UDPPacketLossRate", "Percent"),
]

# Capacity/utilization metrics published to AWS/AppStream for a fleet (dimension Fleet), used for
# WorkSpaces Applications fleet usage history. (ActiveSessions etc. only exist for elastic /
# multi-session fleets, so they are not in the base set.)
APPSTREAM_FLEET_METRICS = [
    ("InUseCapacity", "Count"),
    ("CapacityUtilization", "Percent"),
    ("ActualCapacity", "Count"),
    ("AvailableCapacity", "Count"),
    ("RunningCapacity", "Count"),
    ("DesiredCapacity", "Count"),
    ("PendingCapacity", "Count"),
]

# WorkSpaces Personal connection/session metrics (AWS/WorkSpaces, dimension WorkspaceId). Idle
# desktops typically publish only UserConnected; the rest emit when there are connection attempts.
WORKSPACES_CONNECTION_METRICS = [
    ("UserConnected", "Count"),
    ("ConnectionAttempt", "Count"),
    ("ConnectionSuccess", "Count"),
    ("ConnectionFailure", "Count"),
    ("SessionLaunchTime", "Milliseconds"),
    ("InSessionLatency", "Milliseconds"),
]

# WorkSpaces Pools user-session metrics. NOTE the dimension name is literally "WorkSpaces pool ID"
# (with spaces), not PoolId — verified against a live account.
WORKSPACES_POOL_DIMENSION = "WorkSpaces pool ID"
WORKSPACES_POOL_SESSION_METRICS = [
    ("ActiveUserSessionCapacity", "Count"),
    ("ActualUserSessionCapacity", "Count"),
    ("AvailableUserSessionCapacity", "Count"),
    ("DesiredUserSessionCapacity", "Count"),
    ("PendingUserSessionCapacity", "Count"),
    ("UserSessionsCapacityUtilization", "Percent"),
]

SERVER_INSTRUCTIONS = """\
Administrator-focused MCP server for the Amazon WorkSpaces End User Computing portfolio:
WorkSpaces Personal, WorkSpaces Pools, WorkSpaces Applications, WorkSpaces Secure Browser, and
WorkSpaces Core. Tools are read-only by default and synthesize cross-service results for
inventory, troubleshooting, and cost/utilization optimization. Write/lifecycle operations are not
enabled unless the server was launched with --enable-writes and the matching IAM permissions are
present.

Legacy/former service names are fully supported as input — accept them and treat them as the
current service, but ALWAYS use the current official name in your response:
- "AppStream", "AppStream 2.0", "Amazon AppStream 2.0"  ->  Amazon WorkSpaces Applications
- "WorkSpaces Web"                                       ->  Amazon WorkSpaces Secure Browser
Amazon WorkSpaces Applications IS the rebranded AppStream 2.0 (same service and API), so a request
about "AppStream fleets" or "AppStream stacks" is about WorkSpaces Applications and is handled by
the application-fleet tools — do not say AppStream is unsupported.
"""
