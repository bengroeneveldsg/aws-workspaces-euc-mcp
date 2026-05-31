# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added
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
