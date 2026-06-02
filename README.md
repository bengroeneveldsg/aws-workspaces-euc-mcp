# WorkSpaces EUC MCP Server

[![CI](https://github.com/bengroeneveldsg/aws-workspaces-euc-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/bengroeneveldsg/aws-workspaces-euc-mcp/actions/workflows/ci.yml)

> **Disclaimer:** This project is not an official AWS product, service, or solution, and is not
> affiliated with or endorsed by Amazon Web Services. It is an independent, community example
> implementation intended to demonstrate what is possible when combining the Amazon WorkSpaces End
> User Computing APIs with the Model Context Protocol. It is provided as a starting point to learn
> from, adapt, and iterate on — not as a supported or production-certified offering. Use it at your
> own discretion and always validate it against your organisation's security, compliance, and
> operational requirements before deploying to production.

An [MCP](https://modelcontextprotocol.io) server that gives administrators AI-assisted
**inventory, troubleshooting, and cost/utilization optimization** across the Amazon WorkSpaces
End User Computing (EUC) portfolio:

- **Amazon WorkSpaces Personal** — persistent virtual desktops
- **Amazon WorkSpaces Pools** — non-persistent pooled desktops
- **Amazon WorkSpaces Applications** — application streaming (formerly AppStream 2.0)
- **Amazon WorkSpaces Secure Browser** — managed browser (formerly WorkSpaces Web)
- **Amazon WorkSpaces Core** — partner / bring-your-own VDI integration, incl. **Core Managed
  Instances** (the `workspaces-instances` API). Plain Core partner desktops appear via the standard
  WorkSpaces API and are counted under WorkSpaces Personal.

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

**Shipped** (published to PyPI + GHCR) — **21 read-only tools** (Tiers 0–1) plus opt-in **10 write**
(Tier 2) and **3 destructive** (Tier 3) tools. The read-only inventory, troubleshooting, cost,
audit, and governance tools:

| Tool | Description |
|------|-------------|
| `get_euc_inventory_summary` | Cross-service inventory for a region (incl. **WorkSpaces Core Managed Instances**): per-service counts by state, grand total, and any per-service collection errors. |
| `diagnose_workspace_connectivity` | Why a WorkSpaces Personal desktop may be unreachable — correlates WorkSpace state, connection status, directory health, and CloudWatch connection metrics into a ranked diagnosis. |
| `diagnose_application_fleet` | A WorkSpaces Applications fleet's health and capacity — fleet state, fleet errors, compute capacity, auto-scaling activity, and insufficient-capacity errors. |
| `diagnose_pool` | A WorkSpaces Pool's health — state, pool errors, user-session capacity, backing directory health, and session-utilization. |
| `get_application_fleet_usage` | A WorkSpaces Applications fleet's **usage history** — AWS/AppStream capacity/utilization time-series over a window, with a plain-language summary (e.g. idle running capacity) (Tier 0). |
| `check_directory_health` | Registration state, AWS Directory Service stage, and **registration properties** (target **OU**, custom security group, local-admin / internet-access / maintenance-mode flags) for one or all WorkSpaces-registered directories. |
| `analyze_workspace_utilization` | Classifies WorkSpaces Personal desktops as unused / idle / active from the `UserConnected` metric (Tier 1). |
| `recommend_running_mode` | Flags AlwaysOn desktops with low usage as AutoStop candidates, with an **estimated $/mo saving** where the bundle price can be matched (Tier 1). |
| `get_workspace_performance` | Native CPU / memory / disk / GPU / latency / uptime metrics per desktop from `AWS/WorkSpaces` — no CloudWatch agent (Tier 0). |
| `get_workspace_connection_history` | A desktop's connection/session **history** (UserConnected + connection attempts/failures) over a window, with a summary (Tier 0). |
| `get_pool_session_history` | A WorkSpaces Pool's user-**session history** (active/available/utilization capacity time-series), flags idle pool capacity (Tier 0). |
| `recommend_bundle_rightsizing` | Suggests smaller/larger compute types from CPU & memory headroom (general families; graphics excluded) (Tier 0). |
| `get_euc_cost_summary` | EUC spend by service over a window (or an explicit `start_date`/`end_date` calendar month) via Cost Explorer, account-wide. Services are matched by keyword so naming variants (e.g. AppStream 2.0) aren't dropped; note Cost Explorer bills WorkSpaces Personal/Pools/Core together as one "Amazon WorkSpaces" line (Tier 1). |
| `generate_inventory_report` | Detailed per-resource inventory (desktops **with assigned user / computer name / IP**, pools, fleets, **stacks + their associated fleets**, portals) with key attributes (Tier 0). |
| `audit_security_posture` | Cross-service: flags unencrypted WorkSpace volumes, directories without IP access control groups, and **Secure Browser portals / Applications stacks that allow data egress** (clipboard/download/print) (Tier 0). |
| `audit_application_images` | Audits WorkSpaces Applications (AppStream 2.0) **images and image builders** — flags stale base images (unpatched OS), pinned/old AppStream agents, non-AVAILABLE/errored images, SHARED cross-account visibility, and **image builders left RUNNING** (cost + admin surface) (Tier 0). |
| `get_euc_audit_trail` | **"Who changed what"** — recent EUC management events from CloudTrail (last 90 days, no trail required) across all services; mutations-only by default, flags destructive actions and errors (e.g. AccessDenied) (Tier 0). |
| `get_euc_service_quotas` | **Service-quota limits + usage headroom** per EUC service; pairs limits with current usage (where AWS publishes a usage metric) to flag quotas approaching their limit — capacity planning (Tier 0). |
| `get_secure_browser_portal_details` | Resolves a Secure Browser portal's user settings (clipboard/print/download controls + timeouts), network, attached policies, and — when configured — the **data-protection redaction config** (which built-in/custom patterns are redacted, confidence level, enforced/exempt URLs) (Tier 0). |
| `get_secure_browser_portal_usage` | A Secure Browser portal's **current active sessions** (live, via `ListSessions` — same as the console) plus **historic** `AWS/WorkSpacesWeb` metrics over a window (CloudWatch is historic-only; idle portals publish none) (Tier 0). |
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

With [`uv`](https://docs.astral.sh/uv/) (recommended):

```bash
uvx workspaces-euc-mcp-server@latest
```

Or with pip:

```bash
pip install workspaces-euc-mcp-server
```

Or with Docker (published to GHCR; the server speaks MCP over stdio, so run with `-i`):

```bash
docker run -i --rm \
  -e AWS_PROFILE=your-euc-admin-profile -e AWS_REGION=us-east-1 \
  -v "$HOME/.aws:/home/mcp/.aws:ro" \
  ghcr.io/bengroeneveldsg/aws-workspaces-euc-mcp:latest --region us-east-1
```

From source (for development):

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

The two blocks above are the standard `mcpServers` shape used by most clients (Claude Desktop,
Cursor, etc.). Some clients use a **form** instead of raw JSON — see the example below.

### Example: Amazon Quick (Desktop)

Amazon Quick's **Capabilities → MCP → Add MCP** uses a form rather than JSON. Choose connection
type **Local** ("Run a command on your machine") and fill in:

| Field | Value |
|-------|-------|
| **Name** | `WorkSpaces EUC` |
| **Command** | `uvx` |
| **Arguments** | `workspaces-euc-mcp-server@latest --region us-east-1` |
| **Description** | `Admin tools for Amazon WorkSpaces EUC — inventory, troubleshooting, cost/utilization. Read-only.` |
| **Environment variables** | `AWS_PROFILE` = `your-euc-admin-profile`  ·  `AWS_REGION` = `us-east-1` |
| **Timeout (seconds)** | `60` |

Notes:
- Set `--region` (in Arguments) and `AWS_REGION` to where your WorkSpaces actually live; keep the
  two in sync.
- **Bump the timeout from the default 30 → 60–120 for the first run** — `uvx` downloads the package
  on first launch, which can exceed 30 s and look like a failed connection. Later starts are fast.
- If `uvx` isn't on your `PATH`, use Command `workspaces-euc-mcp-server` (after
  `pip install workspaces-euc-mcp-server`) with Arguments `--region us-east-1`, or Command `python`
  with Arguments `-m workspaces_euc_mcp_server.server --region us-east-1`.
- To enable writes/destructive, append the flags to **Arguments** (e.g.
  `… --enable-writes --max-bulk-targets 10`) — and attach the matching IAM tier (see
  [the safety gates](#enabling-write--destructive-tools--the-safety-gates)).
- A local MCP server has **no "Sign in" button** (that's a Quick *Connections* feature). It uses
  your AWS credential chain, so authenticate first — e.g. `aws sso login --profile your-profile` for
  SSO — then the server connects. See [AWS authentication](#aws-authentication).

## AWS authentication

The server uses the **standard AWS credential chain** — it does not handle sign-in itself. Provide
credentials however boto3 expects them:

- **IAM Identity Center (SSO):** configure an `sso-session` profile, then `aws sso login --profile
  <name>`. With the modern `sso-session` format, botocore **auto-refreshes** the token for the
  session window, so you log in once rather than every few hours. (Signing into the AWS Console or
  access portal in a browser does **not** create the on-disk token the server needs — only
  `aws sso login` does.)
- **Static / temporary keys:** a named profile in `~/.aws/credentials`, or `AWS_ACCESS_KEY_ID` /
  `AWS_SECRET_ACCESS_KEY` / `AWS_SESSION_TOKEN` env vars.
- **Assumed role / multi-account:** see [Multi-account / MSP](#multi-account--msp).

If calls fail with an expired-token / `UnauthorizedException` error, your session has lapsed —
re-authenticate (for SSO, `aws sso login --profile <name>`) and retry. Nothing in the MCP config
changes.

## Enabling write / destructive tools — the safety gates

The config above is **read-only**: the write and destructive tools are not even registered, so
they cannot be called no matter what the model or the IAM policy allows. Enabling them is a
deliberate act, and **IAM permissions alone are not enough**. A destructive call (e.g.
`terminate_workspaces`) only runs after clearing **all four** of these independent gates:

| # | Gate | Where it lives | What it does |
|---|------|----------------|--------------|
| 1 | **Launch flag** | The `args` in your MCP-client config | Destructive tools aren't registered unless the server was started with **both** `--enable-writes` **and** `--enable-destructive`. Default launch = read-only. |
| 2 | **IAM tier** | The AWS profile / assumed role | The credentials must carry the matching tier ([`iam/tier2-lifecycle.json`](iam/tier2-lifecycle.json) for writes, [`iam/tier3-destructive.json`](iam/tier3-destructive.json) for destructive). Without it, AWS denies the call. |
| 3 | **`confirm=true`** | The tool call | Every mutation is **dry-run by default** — it returns a plan and changes nothing. You must explicitly pass `confirm=true`. |
| 4 | **Typed acknowledgement + blast-radius cap** | The tool call | Destructive ops also require the **exact** phrase (`acknowledge="TERMINATE"` / `"REBUILD"` / `"RESTORE"`) and must stay within `--max-bulk-targets`. Wrong phrase or too many targets → refused, nothing changes. |

> **The launch flag grants no AWS permission.** Gate 1 (the `--enable-*` flag) and Gate 2 (IAM) are
> two *separate* switches and you need **both**. Turning on `--enable-destructive` only *exposes* the
> tool in the client — it does not give your AWS profile any access. If the flag is on but the
> profile/role is **missing the matching tier policy**, the tool is callable and its dry-run works,
> but the real `confirm=true` call **fails with an AWS `AccessDenied` error and nothing is deleted**.
> So enabling writes/destructive in your MCP client is necessary but **not** sufficient: you must
> *also* attach [`iam/tier2-lifecycle.json`](iam/tier2-lifecycle.json) /
> [`iam/tier3-destructive.json`](iam/tier3-destructive.json) to the AWS profile (or assumed role)
> the server runs as.
>
> Gates **1–2** are the real security boundary (config + AWS-enforced IAM). Gates **3–4** are
> in-tool guardrails against an over-eager agent or a fat-fingered single call — they are **not** a
> substitute for IAM. The genuine least-privilege control is: **don't grant Tier 2/3 and don't pass
> the flags unless you truly want those operations available.** For an extra backstop, scope the
> Tier 2/3 policy with resource tags / conditions so even an enabled server can only touch a bounded
> set of resources.

**MCP-client config — writes enabled (Tier 2):** add the flag to `args` and attach the Tier 2 policy.

```json
{
  "mcpServers": {
    "workspaces-euc": {
      "command": "uvx",
      "args": ["workspaces-euc-mcp-server@latest", "--enable-writes", "--max-bulk-targets", "10"],
      "env": {
        "AWS_PROFILE": "your-euc-admin-profile",
        "AWS_REGION": "us-east-1"
      }
    }
  }
}
```

**MCP-client config — destructive enabled (Tier 3):** both flags, and attach the Tier 3 policy. Use
a tightly-scoped profile.

```json
{
  "mcpServers": {
    "workspaces-euc": {
      "command": "uvx",
      "args": [
        "workspaces-euc-mcp-server@latest",
        "--enable-writes",
        "--enable-destructive",
        "--max-bulk-targets", "5"
      ],
      "env": {
        "AWS_PROFILE": "your-euc-admin-profile",
        "AWS_REGION": "us-east-1"
      }
    }
  }
}
```

Even with the destructive config above, a call still does nothing until gates 3–4 are satisfied:

```text
terminate_workspaces(workspace_ids=["ws-abc123"], confirm=true, acknowledge="TERMINATE")
```

Equivalent CLI launches (e.g. for the Docker entrypoint or a shell):

```bash
workspaces-euc-mcp-server --enable-writes --max-bulk-targets 10                      # Tier 2 only
workspaces-euc-mcp-server --enable-writes --enable-destructive --max-bulk-targets 5  # + Tier 3
```

## Command-line flags

| Flag | Default | Purpose |
|------|---------|---------|
| `--region` | session/profile region | Target AWS region. |
| `--profile` | default chain | AWS named profile. |
| `--assume-role-arn` | none | Cross-account role to assume (multi-account / MSP). |
| `--external-id` | none | ExternalId for the assumed role, if required. |
| `--enable-writes` | off | Register Phase 2 lifecycle (write) tools. |
| `--enable-destructive` | off | Allow terminate/rebuild/restore (requires `--enable-writes`). |
| `--max-bulk-targets` | 25 | Blast-radius cap for bulk mutations (Phase 2). |

The server starts **read-only**; mutating tools require both the launch flag **and** the matching
IAM tier.

## Multi-account / MSP

To manage a **different** account from the one your credentials live in, launch with
`--assume-role-arn` (and `--external-id` if the role requires it):

```bash
workspaces-euc-mcp-server --region ap-southeast-1 \
  --assume-role-arn arn:aws:iam::222222222222:role/EucReadOnly --external-id my-ext-id
```

The server transparently `sts:AssumeRole`s into the target account and auto-refreshes the
credentials. Requirements:
- The launching identity needs `sts:AssumeRole` on the target role.
- The **target role** needs the matching tier policy from [`iam/`](iam/) (Tier 0 for read-only).
- To manage many accounts, run one instance per target role (or point separate MCP-client entries
  at different `--assume-role-arn` values).

## IAM

Attach [`iam/tier0-diagnostics.json`](iam/tier0-diagnostics.json) to the identity the server runs
as (or to the role you assume with `--assume-role-arn`). Tiers are additive and documented in [`iam/README.md`](iam/README.md). All actions are
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
you pass `confirm=true` (and, for destructive ops, the exact acknowledge phrase). See
[Enabling write / destructive tools — the safety gates](#enabling-write--destructive-tools--the-safety-gates)
for how to turn them on (MCP-client config + IAM) and the four gates each call must clear.

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
