# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

## [0.1.4] - 2026-06-02

### Added
- `get_euc_cost_summary` now returns `by_period` — a per-bucket time series (one entry per day for
  `DAILY`, per month for `MONTHLY`), each with its own per-service split. Previously the tool
  collapsed all periods into per-service totals, so a `DAILY` request lost the daily breakdown and
  clients had to query each day individually to chart trends.

## [0.1.3] - 2026-06-02

### Fixed
- `get_euc_cost_summary` no longer silently drops spend for services whose Cost Explorer `SERVICE`
  name isn't an exact match to a hardcoded list. In real accounts this **hid all WorkSpaces
  Applications spend**, which bills under the name `Amazon WorkSpaces Applications` (the AppStream
  rebrand) rather than `Amazon AppStream`. EUC services are now selected by keyword
  (`workspaces` / `appstream`) against every service in the period, with `Amazon WorkSpaces Thin
  Client` (out of scope) explicitly excluded, and results are paginated.

### Added
- `get_euc_cost_summary` accepts optional `start_date` / `end_date` (YYYY-MM-DD, end exclusive) to
  total an exact calendar month instead of only a rolling `lookback_days` window.

### Docs
- README: a "not an official AWS product" disclaimer at the top; an **Amazon Quick (Desktop)**
  client example; an **AWS authentication** section (SSO login + auto-refresh; console sign-in does
  not produce the on-disk token); and an explicit four-gate **write/destructive safety** section
  noting that the launch flag grants no AWS access (IAM is still required).
- DESIGN.md reconciled with the shipped code.

## [0.1.2] - 2026-06-01

### Added
- Docker support: a `Dockerfile` (slim, non-root) and a GHCR publish workflow
  (`ghcr.io/bengroeneveldsg/aws-workspaces-euc-mcp`, pushed on release). CI validates the image
  builds. Matches the awslabs MCP distribution pattern (PyPI/uvx + container).

### Fixed
- Corrected a stale distribution note in `DESIGN.md` that referenced an unused `awslabs.`
  namespace; documented the actual channels (PyPI via OIDC trusted publishing + GHCR).

## [0.1.1] - 2026-06-01

Best-practice alignment pass (audited against the awslabs MCP design guidelines and the MCP
tool-annotations spec).

### Added
- MCP **tool annotations** on every tool (`readOnlyHint` / `destructiveHint` / `idempotentHint` /
  `openWorldHint` / `title`), so clients can show appropriate consent UX — read-only tools are
  marked read-only, lifecycle writes non-destructive (start/stop/modify idempotent), and
  terminate/rebuild/restore destructive. Aligns with the awslabs MCP design guidelines and the
  MCP tool-annotations spec.
- `FASTMCP_LOG_LEVEL` env var to control the server log level (default `INFO`).
- `Literal` types on fixed-value parameters (`running_mode`, cost `granularity`) for clearer
  client-side validation.

## [0.1.0] - 2026-06-01

First tagged release. Admin-focused MCP server for the Amazon WorkSpaces EUC portfolio —
18 read-only tools (inventory, diagnostics, performance/usage history, cost & right-sizing with
$ estimates, audit, reporting) and 10 guarded write/destructive tools, across WorkSpaces Personal,
Pools, Applications, Secure Browser, and Core Managed Instances. Four additive IAM tiers,
multi-account/MSP via assume-role, no embedded tenant data, CI (ruff/format/pyright/bandit/pytest
on Py 3.11–3.13).

### Added
- CI now also runs `pyright` (basic type-checking); the codebase type-checks clean.
- Multi-account / MSP support: `--assume-role-arn` (+ optional `--external-id`) transparently
  `sts:AssumeRole`s into another account, with auto-refreshing credentials and no tool-code
  changes. The launching identity needs `sts:AssumeRole` on the target role; the role needs the
  matching tier policy.
- Estimated monthly $ savings on recommendations, via the AWS Price List API (new
  `tools/pricing.py`, needs `pricing:GetProducts` / Tier 1): `recommend_running_mode` now fills
  `estimated_monthly_savings_usd` for AlwaysOn→AutoStop candidates (AlwaysOn monthly − AutoStop
  base − hourly×estimated-usage), and `recommend_bundle_rightsizing` fills the AlwaysOn
  compute-tier monthly difference. Best-effort and conservative: matches the canonical
  Included-license SKU on region/OS/compute/volume sizes and returns **null** (never a wrong
  number) when it can't match. Validated live (Standard 80/50 → $35/mo, Power 175/100 → $98/mo).
- `diagnose_pool` (Tier 0): a WorkSpaces Pool health diagnostic correlating pool state, pool
  errors, user-session capacity, backing directory health, and CloudWatch session utilization —
  bringing Pools to parity with the WorkSpace and fleet diagnostics.
- WorkSpaces Core Managed Instances coverage (the `workspaces-instances` API, previously not
  covered at all): `get_euc_inventory_summary` and `generate_inventory_report` now include managed
  instances, enriched with the backing EC2 instance's type / state / launch time / private IP /
  platform (via `ec2:DescribeInstances`). Adds the
  `workspaces-instances:ListWorkspaceInstances`/`GetWorkspaceInstance`/`ListInstanceTypes`/`ListRegions`
  and `ec2:DescribeInstances` IAM actions to all tiers.
- WorkSpaces Secure Browser parity: `get_secure_browser_portal_details` (resolves user/network
  settings — clipboard/print/download controls) and `get_secure_browser_portal_usage`
  (`AWS/WorkSpacesWeb` session metrics; note Secure Browser only emits these during sessions, so
  idle portals return nothing). Adds `workspaces-web:GetUserSettings`/`GetNetworkSettings` to the
  IAM tiers.
- `audit_security_posture` is now **cross-service**: in addition to WorkSpace volume encryption and
  directory IP access control groups, it flags **Secure Browser portals** and **Applications
  stacks** that permit data egress (clipboard-to-local / download / print).

### Fixed
- `generate_inventory_report` Pools capacity was always `null` — it read the wrong key (`Capacity`
  instead of `CapacityStatus`). Now populated with the real session capacity.

### Added
- WorkSpaces Personal **user mapping** + many previously-dropped fields in `generate_inventory_report`
  (found by auditing the raw API responses): each desktop now exposes assigned `UserName` (as the
  record label), `ComputerName`, `IpAddress`, plus `OperatingSystemName`, `Protocols`, root/user
  volume sizes, AutoStop timeout, root/user encryption flags, and subnet. `diagnose_workspace_connectivity`
  signals now include `user_name`/`computer_name`. Pools also expose bundle/directory/running-mode/
  description; Applications fleets expose image name and session/idle/disconnect timeouts; stacks
  expose `UserSettings` (clipboard/print/file-transfer controls) and storage connectors; Secure
  Browser portals expose authentication type, max concurrent sessions, instance and renderer type.
- `get_workspace_connection_history` and `get_pool_session_history` (Tier 0): time-series usage
  history for WorkSpaces Personal desktops (UserConnected + connection attempts/failures) and
  WorkSpaces Pools (Active/Actual/Available/Desired/Pending user-session capacity +
  utilization), each with a plain-language summary that flags unused desktops / idle pool
  capacity. Generalized the CloudWatch series fetch and added a generic `UsageHistory` model.
  Note: the Pools CloudWatch dimension is literally `"WorkSpaces pool ID"` (with spaces).
- `get_application_fleet_usage` (Tier 0): time-series **usage history** for a WorkSpaces
  Applications fleet from the `AWS/AppStream` namespace (InUseCapacity, CapacityUtilization,
  Running/Available/Actual/Desired/Pending capacity) — per-bucket points plus latest/average/peak
  and a plain-language summary that flags idle running capacity.
- WorkSpaces Applications **stacks**: `get_euc_inventory_summary` now counts stacks (alongside
  fleets) and `generate_inventory_report` lists each stack with its associated fleets
  (`ListAssociatedFleets`). IAM policies updated to use `appstream:ListAssociatedFleets` /
  `ListAssociatedStacks` (replacing a non-existent `DescribeFleetAssociations`).
- Legacy service-name acceptance: the server instructions and application-fleet tool descriptions
  now teach the model that "AppStream" / "AppStream 2.0" means Amazon WorkSpaces Applications (and
  "WorkSpaces Web" means Secure Browser), so queries using former names route correctly while
  output keeps the current name. Added a `LEGACY_NAME_ALIASES` map and guardrail tests.
- `get_workspace_performance` (Tier 0): native per-desktop CPU/memory/GPU/FPS/disk/latency/uptime
  metrics from the `AWS/WorkSpaces` namespace (latest/average/peak), no CloudWatch agent required.
- `recommend_bundle_rightsizing` (Tier 0): now implemented on the native CPU/memory metrics —
  suggests smaller/larger compute types from window-peak headroom (general families; graphics
  excluded). This corrects an earlier wrong assumption that these metrics required the CloudWatch
  agent; verified against a live account.

### Changed
- Internal: consolidated the per-module `try_call` / `paginate` / `count_by` helpers onto the
  shared `tools/_common.py` (no behaviour change). Refreshed `DESIGN.md` to reflect shipped state
  and added an "Example admin questions" section to the README.

### Added
- Phase 3 destructive tools (Tier 3), registered only with both `--enable-writes` and
  `--enable-destructive`: `terminate_workspaces`, `rebuild_workspaces`, and `restore_workspace`.
  On top of dry-run + blast-radius cap, each execution requires an exact typed acknowledgement
  phrase (`"TERMINATE"` / `"REBUILD"` / `"RESTORE"`). Added the Tier 3 IAM policy
  (`iam/tier3-destructive.json`) and an optional `acknowledgement_required` field on `WriteOutcome`.
- Phase 2 guarded lifecycle tools for WorkSpaces Pools and Applications (writes, Tier 2):
  `start_workspaces_pool`, `stop_workspaces_pool`, `update_workspaces_pool_capacity`,
  `start_application_fleet`, `stop_application_fleet`, and `update_application_fleet_capacity`.
  Same safety model (dry-run default, opt-in registration); Tier 2 policy extended with the Pools
  and AppStream start/stop/update actions.
- Phase 2 guarded lifecycle tools (writes, Tier 2), registered only with `--enable-writes`:
  `start_workspaces`, `stop_workspaces`, `reboot_workspaces`, and `modify_workspace_running_mode`.
  Mutations are dry-run unless `confirm=true`, and confirmed bulk actions are refused above
  `--max-bulk-targets`. Added the Tier 2 IAM policy (`iam/tier2-lifecycle.json`) and
  `WriteOutcome`/`TargetResult` models.
- Phase 1 reporting & audit tools (read-only, Tier 0): `generate_inventory_report`,
  `audit_security_posture` (volume encryption + directory IP access control groups), and
  `list_unused_resources`. Added a shared `tools/_common.py` (best-effort call + pagination
  helpers) and inventory/audit/unused-resource models; `Finding` gained an optional `resource_id`.
- Phase 1 cost & utilization tools (read-only, Tier 1): `analyze_workspace_utilization`,
  `recommend_running_mode`, and `get_euc_cost_summary`, plus the Tier 1 IAM policy
  (`iam/tier1-cost.json`) adding Cost Explorer and Pricing access.
- Utilization/recommendation/cost models (`WorkspaceUtilization`, `UtilizationReport`,
  `Recommendation`, `RecommendationReport`, `CostSummary`).
- Phase 1 diagnostics tools (read-only, Tier 0): `diagnose_workspace_connectivity`,
  `diagnose_application_fleet`, and `check_directory_health` — each correlates resource state,
  directory health, CloudWatch telemetry, and auto-scaling activity into a severity-ranked
  diagnosis with recommendations.
- Generic `ServiceError`, `Finding`, `Diagnosis`, and `DirectoryHealthReport` models.
- Phase 0 scaffold: FastMCP server, region/profile-aware boto3 client factory, read-only safety
  defaults, and the `get_euc_inventory_summary` tool spanning WorkSpaces Personal, Pools,
  Applications, and Secure Browser.
- Tier 0 (read-only) IAM policy and per-tier IAM documentation.
- Test suite (fake-client based) and tooling config (ruff, pyright, bandit, pre-commit).
- `DESIGN.md` with the full tool inventory and phased roadmap.
