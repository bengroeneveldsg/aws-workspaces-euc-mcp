# WorkSpaces EUC MCP Server

An [MCP](https://modelcontextprotocol.io) server that gives administrators AI-assisted
**inventory, troubleshooting, and cost/utilization optimization** across the Amazon WorkSpaces
End User Computing (EUC) portfolio:

- **Amazon WorkSpaces Personal** — persistent virtual desktops
- **Amazon WorkSpaces Pools** — non-persistent pooled desktops
- **Amazon WorkSpaces Applications** — application streaming (formerly AppStream 2.0)
- **Amazon WorkSpaces Secure Browser** — managed browser (formerly WorkSpaces Web)
- **Amazon WorkSpaces Core** — partner / bring-your-own VDI integration

**Legacy names are accepted.** Ask using former names and the tools still route correctly, while
output always uses the current name: **AppStream / AppStream 2.0 → Amazon WorkSpaces Applications**
(same service, the `appstream` API), and **WorkSpaces Web → Amazon WorkSpaces Secure Browser**.

> Built for the **administrator** persona, following the
> [official AWS MCP design conventions](https://github.com/awslabs/mcp) (Python, FastMCP,
> Pydantic, boto3). It is **read-only by default** and **security-first**: write/lifecycle tools
> are opt-in, gated behind explicit flags and matching IAM permissions.

## Why this exists

Generic AWS MCP servers can already call EUC APIs one-to-one. This server is different: its tools
are **cross-service workflows** that synthesize a result (an inventory rollup, a connectivity
diagnosis, a right-sizing recommendation) instead of returning raw API output. See
[`DESIGN.md`](DESIGN.md) for the full tool inventory and roadmap.

## Status

**Phase 1 (in progress)** — read-only inventory and troubleshooting tools:

| Tool | Description |
|------|-------------|
| `get_euc_inventory_summary` | Cross-service inventory for a region: per-service counts by state, grand total, and any per-service collection errors. |
| `diagnose_workspace_connectivity` | Why a WorkSpaces Personal desktop may be unreachable — correlates WorkSpace state, connection status, directory health, and CloudWatch connection metrics into a ranked diagnosis. |
| `diagnose_application_fleet` | A WorkSpaces Applications fleet's health and capacity — fleet state, fleet errors, compute capacity, auto-scaling activity, and insufficient-capacity errors. |
| `get_application_fleet_usage` | A WorkSpaces Applications fleet's **usage history** — AWS/AppStream capacity/utilization time-series over a window, with a plain-language summary (e.g. idle running capacity) (Tier 0). |
| `check_directory_health` | Registration state and AWS Directory Service stage for one or all WorkSpaces-registered directories. |
| `analyze_workspace_utilization` | Classifies WorkSpaces Personal desktops as unused / idle / active from the `UserConnected` metric (Tier 1). |
| `recommend_running_mode` | Flags AlwaysOn desktops with low usage as AutoStop candidates (Tier 1). |
| `get_workspace_performance` | Native CPU / memory / disk / GPU / latency / uptime metrics per desktop from `AWS/WorkSpaces` — no CloudWatch agent (Tier 0). |
| `recommend_bundle_rightsizing` | Suggests smaller/larger compute types from CPU & memory headroom (general families; graphics excluded) (Tier 0). |
| `get_euc_cost_summary` | EUC spend by service over a window via Cost Explorer, account-wide (Tier 1). |
| `generate_inventory_report` | Detailed per-resource inventory (desktops, pools, fleets, **stacks + their associated fleets**, portals) with key attributes (Tier 0). |
| `audit_security_posture` | Flags WorkSpace volumes without encryption and directories without IP access control groups (Tier 0). |
| `list_unused_resources` | Unused WorkSpaces desktops and stopped/zero-capacity fleets worth reclaiming (Tier 0). |

Cost/utilization tools need the **Tier 1** IAM policy ([`iam/tier1-cost.json`](iam/tier1-cost.json));
everything else above is **Tier 0** ([`iam/tier0-diagnostics.json`](iam/tier0-diagnostics.json)).

### Write tools — opt-in, guarded (Phase 2, Tier 2)

Registered **only** when launched with `--enable-writes`, and need the **Tier 2** policy
([`iam/tier2-lifecycle.json`](iam/tier2-lifecycle.json)). Every mutation is **dry-run by default**
(returns a plan, changes nothing) unless called with `confirm=true`, and confirmed bulk actions are
**refused above `--max-bulk-targets`**.

| Tool | Description |
|------|-------------|
| `start_workspaces` / `stop_workspaces` / `reboot_workspaces` | Power operations on WorkSpaces Personal desktops (batch, capped). |
| `modify_workspace_running_mode` | Switch a desktop between `AUTO_STOP` and `ALWAYS_ON`. |
| `start_workspaces_pool` / `stop_workspaces_pool` | Power a WorkSpaces Pool on/off. |
| `update_workspaces_pool_capacity` | Set a Pool's desired user-session capacity. |
| `start_application_fleet` / `stop_application_fleet` | Power a WorkSpaces Applications fleet on/off. |
| `update_application_fleet_capacity` | Set a fleet's desired instance capacity. |

### Destructive tools — double opt-in, typed acknowledgement (Phase 3, Tier 3)

Registered **only** with both `--enable-writes` **and** `--enable-destructive`, and need the
**Tier 3** policy ([`iam/tier3-destructive.json`](iam/tier3-destructive.json)). On top of the
dry-run default and blast-radius cap, each execution requires an **exact typed acknowledgement**.

| Tool | Description | Acknowledge |
|------|-------------|-------------|
| `terminate_workspaces` | **Permanently delete** desktops (irreversible). | `"TERMINATE"` |
| `rebuild_workspaces` | Reset root volume to bundle; user volume from last snapshot. | `"REBUILD"` |
| `restore_workspace` | Restore a desktop from its last snapshot. | `"RESTORE"` |

Example: `terminate_workspaces(workspace_ids=[...], confirm=true, acknowledge="TERMINATE")`.
Without the exact phrase the call is **refused** and nothing changes. See [`DESIGN.md`](DESIGN.md).

## Requirements

- Python 3.11+
- AWS credentials available via the standard chain (`AWS_PROFILE`, `AWS_REGION`, SSO, or an
  assumed role).
- An IAM identity with the **Tier 0** policy in [`iam/tier0-diagnostics.json`](iam/tier0-diagnostics.json).

## Credentials & data handling

This server is built to be redistributed and run by many parties, so it **never stores or embeds
any user-specific data**:

- **Credentials** come only from the standard AWS chain at runtime — they are never read into,
  logged by, or persisted by the server.
- **No state on disk.** There is no config/cache/state file; boto3 clients live in memory only.
- **No account-specific data in the code** — no account IDs, ARNs, profile names, or regions are
  hardcoded. Provide them at runtime via flags/env. Documentation uses placeholders only.

Bring your own credentials and region; the server holds nothing.

## Install

With [`uv`](https://docs.astral.sh/uv/) (recommended once published):

```bash
uvx workspaces-euc-mcp-server@latest
```

From source:

```bash
python -m venv .venv
# Windows: .venv\Scripts\Activate.ps1   |   macOS/Linux: source .venv/bin/activate
pip install -e ".[dev]"
```

## Configure your MCP client

```json
{
  "mcpServers": {
    "workspaces-euc": {
      "command": "uvx",
      "args": ["workspaces-euc-mcp-server@latest"],
      "env": {
        "AWS_PROFILE": "your-euc-admin-profile",
        "AWS_REGION": "us-east-1"
      }
    }
  }
}
```

Running from a source checkout instead of `uvx`:

```json
{
  "mcpServers": {
    "workspaces-euc": {
      "command": "python",
      "args": ["-m", "workspaces_euc_mcp_server.server", "--region", "us-east-1"],
      "env": { "AWS_PROFILE": "your-euc-admin-profile" }
    }
  }
}
```

## Command-line flags

| Flag | Default | Purpose |
|------|---------|---------|
| `--region` | session/profile region | Target AWS region. |
| `--profile` | default chain | AWS named profile. |
| `--enable-writes` | off | Register Phase 2 lifecycle (write) tools. |
| `--enable-destructive` | off | Allow terminate/rebuild/restore (requires `--enable-writes`). |
| `--max-bulk-targets` | 25 | Blast-radius cap for bulk mutations (Phase 2). |

The server starts **read-only**; mutating tools require both the launch flag **and** the matching
IAM tier.

## IAM

Attach [`iam/tier0-diagnostics.json`](iam/tier0-diagnostics.json) to the identity the server runs
as. Tiers are additive and documented in [`iam/README.md`](iam/README.md). All actions are
captured by AWS CloudTrail.

## Example admin questions

Once the server is connected to an MCP client, you can ask in natural language and the client will
pick the right tool. A few examples:

| You ask… | Tool the client uses |
|----------|----------------------|
| "What WorkSpaces resources do I have in us-east-1?" | `get_euc_inventory_summary` |
| "User X can't connect to ws-abc123 — why?" | `diagnose_workspace_connectivity` |
| "Is the marketing AppStream fleet healthy / out of capacity?" | `diagnose_application_fleet` |
| "Which desktops are idle or unused this fortnight?" | `analyze_workspace_utilization` / `list_unused_resources` |
| "Where can I cut WorkSpaces cost?" | `recommend_running_mode` + `get_euc_cost_summary` |
| "Any desktops without volume encryption or IP restrictions?" | `audit_security_posture` |
| "Switch ws-abc123 to AutoStop." | `modify_workspace_running_mode` (dry-run first) |
| "Reboot these three stuck desktops." | `reboot_workspaces` (dry-run, then `confirm=true`) |

**Write/destructive tools are off unless enabled.** Mutations show a dry-run plan first; to execute
you pass `confirm=true` (and, for destructive ops, the exact acknowledge phrase). For example, a
full destructive run looks like:

```text
terminate_workspaces(workspace_ids=["ws-abc123"], confirm=true, acknowledge="TERMINATE")
```

Launch with writes/destructive enabled:

```bash
workspaces-euc-mcp-server --enable-writes                      # Tier 2 power/capacity tools
workspaces-euc-mcp-server --enable-writes --enable-destructive # + Tier 3 terminate/rebuild/restore
```

## Development

```bash
pip install -e ".[dev]"
pytest            # run tests (deterministic, no AWS account needed)
ruff check .      # lint
ruff format .     # format
pyright           # type check
```

## License

Apache-2.0. See [`LICENSE`](LICENSE).
