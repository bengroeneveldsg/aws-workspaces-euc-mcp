# Amazon WorkSpaces EUC Admin MCP Server — Design & Build Plan

> Status: **Phases 0–3 shipped** (2026-05-31). An MCP server giving administrators AI-assisted
> inventory, troubleshooting, cost/utilization optimization, and guarded lifecycle management
> across the Amazon WorkSpaces family of End User Computing services.
>
> **Shipped:** 21 read-only tools (Tier 0/1) + 10 guarded write tools (Tier 2) + 3 destructive
> tools (Tier 3), all behind opt-in flags with dry-run/confirm/blast-radius/typed-acknowledgement
> guards. Read-only coverage now includes image auditing and governance (CloudTrail audit trail +
> Service Quotas headroom). All four IAM policy tiers ship in `iam/`. CI
> (ruff/format/pyright/bandit/pytest on Py 3.11–3.13) is green; published to PyPI and GHCR.
> **`README.md` is the source of truth for the live tool catalog and exact tool names** — §5 below
> records the original design intent and is kept reconciled with what shipped. See `CHANGELOG.md`
> for per-version detail.

## 1. Principles

1. **Admin persona only.** Operators managing fleets — not end users.
2. **Domain intelligence over API mirroring.** Generic servers (AWS MCP Server, Cloud Control,
   AWS API) already call EUC APIs raw. Our value is cross-service diagnosis, fleet-level
   optimization, and EUC-aware guardrails — not 1:1 API wrappers.
3. **Security-first, least privilege.** Read-only by default. Writes are opt-in, separately
   permissioned, dry-run-able, confirmation-gated, and blast-radius-limited.
4. **No embedded tenant data — ever.** This is redistributable software run by many parties.
   Credentials, account IDs, ARNs, profile names, regions, and any other consumer-specific data
   are supplied **only at runtime** (AWS credential chain, CLI flags, env vars) and are **never**
   hardcoded, persisted to disk, or committed. The codebase ships with zero account-specific data.
5. **Official AWS naming everywhere a human reads.** Legacy identifiers only where the SDK/API
   literally requires them (and labelled as such).
6. **Build on awslabs conventions** so we match patterns customers already trust.

## 2. Services in scope (official naming → API identifier)

| Product name | Underlying API (boto3 client) | Notes |
|---|---|---|
| Amazon WorkSpaces Personal | `workspaces` | Persistent desktops |
| Amazon WorkSpaces Pools | `workspaces` (Pools operations) | Non-persistent pooled |
| Amazon WorkSpaces Applications | `appstream` | App streaming (formerly AppStream 2.0) |
| Amazon WorkSpaces Secure Browser | `workspaces-web` | Managed browser (formerly WorkSpaces Web) |
| Amazon WorkSpaces Core | `workspaces` | Partner/BYO-VDI integration |

**Excluded:** Amazon WorkSpaces Thin Client.

## 3. Architecture & stack (per awslabs DESIGN_GUIDELINES)

- **Language/framework:** Python 3.11+, FastMCP, Pydantic models, boto3, async/await.
- **Auth:** standard boto3 credential chain via `AWS_PROFILE` / `AWS_REGION`; no secrets stored
  in the server. Assumed-role / Identity Center friendly. **Single-account v1**, with a client
  factory designed to add cross-account `sts:AssumeRole` later without touching tool code.
- **Transport:** local `uvx`/stdio first; tool logic kept transport-agnostic so a remote
  (AgentCore Runtime) deployment can be added later.
- **Distribution (shipped):** published to PyPI as `workspaces-euc-mcp-server` (run via
  `uvx workspaces-euc-mcp-server@latest` or `pip`), and a container image on GHCR
  (`ghcr.io/bengroeneveldsg/aws-workspaces-euc-mcp`). Independent — not under the `awslabs.`
  namespace. Both publish from GitHub Releases via OIDC trusted publishing (PyPI) and `GITHUB_TOKEN`
  (GHCR).
- **Observability:** Loguru with env-controlled log level (`FASTMCP_LOG_LEVEL`). Per-signal errors
  are returned in the structured result payload (deliberate: each tool makes many AWS calls and
  synthesizes one result), rather than `ctx.error`.
- **Repo layout** (independent package — **not** under an `awslabs/` namespace):
  ```
  workspaces_euc_mcp_server/
    __init__.py        # version
    server.py          # FastMCP app + tool registration + main()/argparse
    consts.py          # service/API constants, region→pricing-location maps
    models.py          # Pydantic request/response models
    clients.py         # boto3 client factory (region/profile/assume-role aware)
    tools/
      _common.py       # read_only/writes annotation helpers, try_call, paginate
      inventory.py
      diagnostics.py
      cost.py
      performance.py   # perf + usage/history tools (native CloudWatch metrics)
      reporting.py
      secure_browser.py
      pricing.py       # AWS Price List lookups for $ estimates
      images.py        # WorkSpaces Applications image / image-builder audit
      governance.py    # CloudTrail audit trail + Service Quotas headroom
      lifecycle.py     # Tier 2 (guarded writes)
      destructive.py   # Tier 3 (terminate/rebuild/restore)
  iam/                 # shippable least-privilege policy docs per tier (0–3)
  tests/               # pytest + pytest-asyncio (no live AWS; calls are stubbed/monkeypatched)
  Dockerfile, .dockerignore
  pyproject.toml, .pre-commit-config.yaml, README.md, CHANGELOG.md, LICENSE
  ```
- **Quality gates:** ruff (format/lint), pyright (basic type-checking), bandit (security),
  pre-commit (incl. `detect-private-key`), Apache-2.0 headers. Tests stub the boto3 layer rather
  than hitting AWS.

## 4. Configuration / safety flags

- `--readonly` (default **on**): only Describe/Get/List tools are registered.
- `--enable-writes`: registers Phase-2 lifecycle tools (still dry-run/confirm gated).
- `--enable-destructive`: separately gates terminate/rebuild/restore.
- `--max-bulk-targets N`: blast-radius cap for any bulk mutation.
- `AWS_REGION` / `AWS_PROFILE`: standard.

## 5. Tool inventory

Each tool lists the IAM actions it needs. Tools are *workflows* that compose several API calls
and return a synthesized result, not raw API passthroughs.

### Phase 1 — Read / Diagnose / Optimize (read-only, Tiers 0–1)

> Tool names below are the **shipped** names. Where the original plan used a different working
> name, the shipped name is what appears here.

**Inventory & discovery**
| Tool | Purpose | IAM actions |
|---|---|---|
| `get_euc_inventory_summary` | Cross-service rollup (counts, states, regions) across Personal, Pools, Applications, Secure Browser, Core | union of the in-scope describes |
| `generate_inventory_report` | Structured per-resource inventory across all in-scope services | Phase-1 describes |

**Troubleshooting & triage** (the flagship value)
| Tool | Purpose | IAM actions |
|---|---|---|
| `diagnose_workspace_connectivity` | Correlate a Personal WorkSpace's state + connection status + directory health + CloudWatch into a root-cause narrative | `workspaces:Describe*`, `ds:DescribeDirectories`, `cloudwatch:GetMetricData` |
| `diagnose_pool` | Why a Pool is unhealthy/queued — capacity status, sessions, errors, scaling | `workspaces:DescribeWorkspacesPool*`, `cloudwatch:GetMetricData` |
| `diagnose_application_fleet` | Fleet state, capacity, scaling activity, fleet errors | `appstream:DescribeFleets`, `appstream:ListAssociatedStacks`, `cloudwatch:GetMetricData`, `application-autoscaling:DescribeScalingActivities` |
| `check_directory_health` | Shared dependency: directory reachability/registration + registration properties (target OU, security group, flags); skips `ds` for WorkSpaces-managed `wsd-` directories | `ds:DescribeDirectories`, `workspaces:DescribeWorkspaceDirectories` |

**Cost, utilization & performance**
| Tool | Purpose | IAM actions |
|---|---|---|
| `analyze_workspace_utilization` | Find idle/unused Personal WorkSpaces from connection metrics | `workspaces:DescribeWorkspaces*`, `cloudwatch:GetMetricData` |
| `recommend_running_mode` | AlwaysOn → AutoStop candidates with a $ estimate | `workspaces:DescribeWorkspaces`, `cloudwatch:GetMetricData`, `pricing:GetProducts` |
| `recommend_bundle_rightsizing` | Over/under-sized bundles from native CPU/mem metrics (see §10) | `workspaces:DescribeWorkspaces`, `workspaces:DescribeWorkspaceBundles`, `cloudwatch:GetMetricData` |
| `get_workspace_performance` | Per-WorkSpace CPU/mem/GPU/FPS/latency from native `AWS/WorkSpaces` metrics | `workspaces:DescribeWorkspaces`, `cloudwatch:GetMetricData` |
| `get_workspace_connection_history` | Per-WorkSpace connection timeline | `workspaces:DescribeWorkspaces*`, `cloudwatch:GetMetricData` |
| `get_pool_session_history` | Pool session/capacity history | `workspaces:DescribeWorkspacesPool*`, `cloudwatch:GetMetricData` |
| `get_application_fleet_usage` | Applications fleet utilization vs capacity | `appstream:DescribeFleets`, `cloudwatch:GetMetricData` |
| `get_euc_cost_summary` | Spend rollup filtered to EUC services | `ce:GetCostAndUsage`, `ce:GetDimensionValues` |

**Secure Browser**
| Tool | Purpose | IAM actions |
|---|---|---|
| `get_secure_browser_portal_details` | Portal config + associated settings (browser/network/user/IP-access) + resolved data-protection redaction config | `workspaces-web:GetPortal`, `workspaces-web:List*`, `workspaces-web:Get*Settings`, `workspaces-web:GetDataProtectionSettings` |
| `get_secure_browser_portal_usage` | Current active sessions (live, `ListSessions`) + historic CloudWatch metrics | `workspaces-web:ListPortals`, `workspaces-web:ListSessions`, `cloudwatch:GetMetricData` |

**Reporting & audit**
| Tool | Purpose | IAM actions |
|---|---|---|
| `audit_security_posture` | Encryption at rest, IP access control groups, directory config, portal policies | `workspaces:Describe*`, `workspaces-web:Get*/List*`, `appstream:Describe*` |
| `audit_application_images` | WorkSpaces Applications image/image-builder audit — stale base, pinned agent, errored/SHARED images, RUNNING builders | `appstream:DescribeImages`, `appstream:DescribeImageBuilders` |
| `list_unused_resources` | Idle desktops / empty fleets / orphaned resources | describes + `cloudwatch:GetMetricData` |

**Governance** (cross-service)
| Tool | Purpose | IAM actions |
|---|---|---|
| `get_euc_audit_trail` | "Who changed what" — recent EUC management events (mutations by default), 90-day CloudTrail history | `cloudtrail:LookupEvents` |
| `get_euc_service_quotas` | Quota limits + usage headroom per EUC service (capacity planning) | `servicequotas:ListServiceQuotas`, `servicequotas:GetServiceQuota`, `cloudwatch:GetMetricData` |

### Phase 2 — Guarded lifecycle (writes; Tier 2, `--enable-writes`)
All support `dry_run`, return a plan + blast-radius before acting, and honor `--max-bulk-targets`.

| Tool | Purpose | IAM actions |
|---|---|---|
| `start_workspaces` / `stop_workspaces` / `reboot_workspaces` | Personal power ops | `workspaces:Start/Stop/RebootWorkspaces` |
| `modify_workspace_running_mode` | Apply AutoStop/AlwaysOn recommendation | `workspaces:ModifyWorkspaceProperties` |
| `start_workspaces_pool` / `stop_workspaces_pool` / `update_workspaces_pool_capacity` | Pool power + resize | `workspaces:Start/Stop/UpdateWorkspacesPool` |
| `start_application_fleet` / `stop_application_fleet` / `update_application_fleet_capacity` | Applications fleet ops | `appstream:Start/Stop/UpdateFleet` |

> Note: bundle right-sizing is **recommend-only** (`recommend_bundle_rightsizing`); there is no
> `modify_workspace_compute_type` write tool — applying a compute change is left to the operator.

### Phase 3 — Destructive (Tier 3, `--enable-destructive`, hardest gating)
| Tool | Purpose | IAM actions |
|---|---|---|
| `rebuild_workspaces` / `restore_workspace` | Recover a desktop (data impact) | `workspaces:Rebuild/RestoreWorkspace` |
| `terminate_workspaces` | Decommission (irreversible) | `workspaces:TerminateWorkspaces` |

## 6. IAM policy tiers (shipped with the server)

We ship a managed policy document per tier so customers grant exactly what they enable.

- **Tier 0 — Diagnostics (read-only):** `workspaces:Describe*`, `appstream:Describe*` (incl.
  `DescribeImages`/`DescribeImageBuilders`), `workspaces-web:Get*`/`List*`,
  `workspaces-instances:List*`/`Get*`, `ds:DescribeDirectories`,
  `cloudwatch:GetMetricData`/`ListMetrics`, `application-autoscaling:DescribeScalingActivities`,
  `ec2:DescribeInstances`, `cloudtrail:LookupEvents`,
  `servicequotas:ListServiceQuotas`/`GetServiceQuota`.
- **Tier 1 — Cost/optimization (read-only):** Tier 0 + `ce:GetCostAndUsage`,
  `ce:GetDimensionValues`, `pricing:GetProducts`.
- **Tier 2 — Lifecycle (writes):** Tier 1 + `workspaces:Start/Stop/Reboot/ModifyWorkspace*`,
  `workspaces:UpdateWorkspacesPool`, `appstream:Start/Stop/UpdateFleet`.
- **Tier 3 — Destructive:** Tier 2 + `workspaces:Rebuild/Restore/TerminateWorkspaces`.

Each tier is additive; default install = Tier 0. Recommend scoping by resource tag / directory
where the API supports it, and using **IAM context keys to separate the agent identity from the
human operator** (the pattern the AWS MCP Server uses).

## 7. Security model

- Read-only default; writes require an explicit launch flag *and* the matching IAM tier.
- Every mutation: `dry_run` plan → explicit confirmation → blast-radius cap.
- **No credentials or tenant data in the server.** Credentials are resolved solely from the
  standard AWS chain (`AWS_PROFILE` / `AWS_REGION` / SSO / assumed role) at runtime. The server
  stores nothing to disk — boto3 clients are cached in memory only; there is no config/state file,
  no credential cache, no account ID, ARN, or profile name baked into source.
- **Redistributable-safe repo:** nothing account-specific is ever committed. `.gitignore` blocks
  `.aws/`, `.env`, `*.pem`; pre-commit runs `detect-private-key`; CI greps for hardcoded
  account IDs/ARNs/secrets. Examples in docs use placeholders only.
- Full auditability via CloudTrail (every underlying API call is attributable).
- Optional `audit_security_posture` self-check against EUC best practices.
- Input validation with Pydantic; bandit + AST checks in CI.

## 8. Phased roadmap

- **Phase 0 — Scaffold:** repo per awslabs layout, client factory, auth, `--readonly` flag,
  one end-to-end tool (`get_euc_inventory_summary`), tests (stubbed boto3), CI/pre-commit. Ship to
  internal users.
- **Phase 1 — Read/Diagnose/Optimize:** full inventory + diagnostics + cost tools (Tiers 0–1).
  This is the demonstrable-value milestone.
- **Phase 2 — Guarded lifecycle:** writes behind `--enable-writes`, dry-run + confirm + caps.
- **Phase 3 — Destructive ops:** terminate/rebuild/restore behind `--enable-destructive`.
- **Phase 4 — Polish:** packaging (uvx/Docker), docs, optional remote/AgentCore deployment.

## 9. Decisions

1. **Distribution:** standalone **public repo on the author's personal GitHub**, consumable and
   self-deployable by others, with full documentation (README, install, IAM setup, examples).
   Follows awslabs conventions so it feels familiar, but lives independently (not in `awslabs/mcp`).
2. **Account model:** **single-account first** (standard boto3 credential chain). Architect the
   client factory so cross-account role assumption (MSP/multi-account) can be added later without
   refactoring tools.
3. **Deployment:** **local `uvx` first**, but design transport/config so the same server can be
   deployed remotely (e.g. Bedrock AgentCore Runtime) later — keep tool logic transport-agnostic.

## 10. Still open (defer)

- **CORRECTED & SHIPPED:** `recommend_bundle_rightsizing` was previously deferred on the
  (incorrect) assumption that `AWS/WorkSpaces` doesn't publish CPU/memory. It does — the namespace
  natively emits `CPUUsage`, `MemoryUsage`, `GPUUsage`, `FramesPerSecond`, disk usage, latency,
  uptime, etc., keyed by `WorkspaceId`, with **no CloudWatch agent required** (verified against a
  live account). `get_workspace_performance` and `recommend_bundle_rightsizing` now ship on these
  native metrics (Tier 0).
- Cost tools: Cost Explorer (`ce`) only for v1, or add CUR for finer granularity later?
- Resource-tag / directory-scoped IAM conditions — which services support them well enough to
  recommend by default.
