# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added
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
