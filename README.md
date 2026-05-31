# WorkSpaces EUC MCP Server

An [MCP](https://modelcontextprotocol.io) server that gives administrators AI-assisted
**inventory, troubleshooting, and cost/utilization optimization** across the Amazon WorkSpaces
End User Computing (EUC) portfolio:

- **Amazon WorkSpaces Personal** — persistent virtual desktops
- **Amazon WorkSpaces Pools** — non-persistent pooled desktops
- **Amazon WorkSpaces Applications** — application streaming (formerly AppStream 2.0)
- **Amazon WorkSpaces Secure Browser** — managed browser (formerly WorkSpaces Web)
- **Amazon WorkSpaces Core** — partner / bring-your-own VDI integration

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

**Phase 0 (scaffold)** — a working read-only vertical slice with one end-to-end tool:

| Tool | Description |
|------|-------------|
| `get_euc_inventory_summary` | Cross-service inventory for a region: per-service counts by state, grand total, and any per-service collection errors. |

Phase 1 adds the full diagnostics and cost tools; Phase 2+ add guarded lifecycle operations. See
[`DESIGN.md`](DESIGN.md).

## Requirements

- Python 3.11+
- AWS credentials available via the standard chain (`AWS_PROFILE`, `AWS_REGION`, SSO, or an
  assumed role). **No credentials are stored by the server.**
- An IAM identity with the **Tier 0** policy in [`iam/tier0-diagnostics.json`](iam/tier0-diagnostics.json).

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
