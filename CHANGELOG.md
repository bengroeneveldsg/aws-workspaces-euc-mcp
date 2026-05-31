# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added
- Phase 0 scaffold: FastMCP server, region/profile-aware boto3 client factory, read-only safety
  defaults, and the `get_euc_inventory_summary` tool spanning WorkSpaces Personal, Pools,
  Applications, and Secure Browser.
- Tier 0 (read-only) IAM policy and per-tier IAM documentation.
- Test suite (fake-client based) and tooling config (ruff, pyright, bandit, pre-commit).
- `DESIGN.md` with the full tool inventory and phased roadmap.
