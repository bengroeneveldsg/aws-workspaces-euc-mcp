# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

## [0.1.22] - 2026-07-03

From live validation: a Windows Server 2025 vs Windows 11 price comparison came back "identical"
— because the Personal pricing path hardcoded license=Included, silently returning Windows Server
rates for Windows 11 queries. Windows 10/11 on WorkSpaces Personal is ALWAYS BYOL with separate
hardware-only SKUs. No new tools; 30 unchanged.

### Fixed
- **Personal pricing is license-aware.** `get_workspace_prices` / `list_workspace_bundle_skus`
  resolve Windows 10/11 (and BYOL-named bundles) against `license="Bring Your Own License"` SKUs
  — dropping the operatingSystem filter there, since BYOL SKUs carry os "Windows" OR "Any".
  Verified live (Power, Singapore): BYOL \$120/mo AlwaysOn + \$26/\$0.95 AutoStop vs Included
  \$124/\$26/\$0.99; the 80/10 BYOL hourly is \$0.76 vs \$0.95 Included (20% apart). This also
  makes `recommend_running_mode` savings estimates accurate for Windows 11 desktops.

### Added
- `get_euc_service_prices` (personal) returns `license_model` plus a LICENSE MODEL note: BYOL
  responses call out the bring-your-own-licenses + dedicated-tenancy requirements; Included
  responses remind the assistant to re-query with WINDOWS_11 if the user means the client OS.
- SERVER_INSTRUCTIONS: CRITICAL block — Windows 10/11 Personal is always BYOL with different
  rates; query both license models before comparing, never claim they cost the same.

## [0.1.21] - 2026-07-03

Pricing hardening, informed by a review of the awslabs aws-pricing-mcp-server patterns and a live
failure (Power Root:80/User:10 in Singapore has an AutoStop hourly SKU but NO published monthly
fees — the unexplained nulls sent the assistant to web search). No new tools; 30 unchanged.

### Added
- **"Never guess values" discovery on every miss** (awslabs pattern): partial Price List data now
  gets an explicit PARTIAL PRICE LIST DATA note ("null = unpublished, not \$0"), and
  `available_storage_configurations` attaches on partial matches too, not only total misses.
  Applications no-match responses now include `available_operating_systems` — the OS/license
  variants AWS does price for that instance type + function.
- **Multi-region comparison in one call** — `get_euc_service_prices(regions=[...])` returns a
  `by_region` map, so "is X cheaper in Sydney?" is a single tool call.
- **Explicit assumptions** on every pricing response: public LIST prices (private pricing /
  EDP/PPA discounts / credits NOT reflected — Cost Explorer actuals are the discounted truth);
  730-hour month for monthly derivations.
- README: advisory disclaimer for pricing estimates (adapted from the awslabs server's).

### Fixed
- All Price List queries now **paginate** (`_iter_price_list`, up to 5 pages) — single-page
  queries silently truncated at 100 products.

## [0.1.20] - 2026-07-03

From live validation: a sizing question ("10 WorkSpaces on Server 2025, 6 used 30 h/mo, 4 used
100 h/mo") was answered with Applications fleet pricing instead of Personal bundles, with no
clarifying questions and no running-mode tipping point. No new tools; 30 unchanged.

### Added
- `get_euc_service_prices` (personal) returns **`autostop_breakeven_hours_per_month`** — the
  connected hours/month where AUTO_STOP (base + hourly) and ALWAYS_ON (flat) cost the same —
  plus a RUNNING-MODE TIPPING POINT note. Pre-computed because models mis-derive tipping points
  from raw rates. (Power/SIN example: ~99 h/mo — a 100 h/mo user is a coin-flip, not a clear
  ALWAYS_ON.)

### Changed
- SERVER_INSTRUCTIONS: sizing/provisioning questions phrased per-user ("N WorkSpaces for users
  with X hours/month") mean WorkSpaces **Personal** unless fleets/pools are explicitly named;
  the assistant should ask for bundle/OS/storage (or state assumptions) before quoting, and use
  the break-even to assign running modes — flagging near-tipping-point users as a coin-flip.
- Personal pricing docstring: ask for bundle/OS/storage rather than assuming.

## [0.1.19] - 2026-07-03

Polish from the v0.1.18 validation round. No new tools; 30 unchanged.

### Added
- `get_euc_service_prices` accepts **`fleet_name`** (applications): resolves instance type, fleet
  type, desired capacity, and — the important part — the **platform from the fleet's image**,
  because DescribeFleets omits Platform for non-Elastic fleets. Fixes Windows 10/11 BYOL fleets
  being priced at the included-license Windows Server rate (observed: \$0.24/hr quoted for a
  \$0.217/hr BYOL fleet). Adds `idle_monthly_usd` (ON_DEMAND: stopped fee x 730 h x desired) or
  `provisioned_monthly_usd` (ALWAYS_ON) estimates. Uses existing Tier 0 appstream Describe
  permissions.

### Changed
- Applications pricing notes now state the On-Demand stopped-instance fee is **one flat SKU per
  region** (it does not vary by instance type) — stops assistants hedging.
- CloudTrail `LookupEvents` pacing is now a **module-level 2 TPS pacer shared across tool
  calls** — back-to-back per-service audits (the assistant's natural pattern for "who changed
  what everywhere") no longer throttle each other's first pages.

## [0.1.18] - 2026-07-03

Two confident-wrong-answer bugs found in live validation, both fixed. No new tools; 30 unchanged.

### Fixed
- **Audit trail false negative** — `get_euc_audit_trail` reported "no AppStream changes in 14
  days" while CloudTrail held a `CreateFleet`/`CreateStack` from 5 days earlier. The mutations
  sweep (account-wide `ReadOnly=false`) stopped after `max_events` RAW events, which in a noisy
  account (constant SSM/EC2 writes, ~1,800/day observed) covered barely an hour before filtering
  to EUC sources. Now:
  - Single-service queries additionally look up **~2 dozen curated mutation event names**
    (CreateFleet, UpdateStack, TerminateWorkspaces, DeletePortal, ...) directly — each EventName
    lookup spans the full window regardless of account noise; results merged and deduped.
  - Every response reports **honest coverage**: `window_fully_covered` and `scanned_back_to`,
    plus an explicit PARTIAL WINDOW COVERAGE note telling the assistant not to conclude "no
    changes" from an incomplete scan.
  - The sweep caps against EUC events found (not raw), with a page budget and 2 TPS pacing;
    the tool now runs off the event loop (`asyncio.to_thread`).
  Live re-test of the failing query: all 10 real changes returned in ~26 s, including the
  buried creates.
- **On-Demand fleet costs overstated ~8-10x** — an assistant estimated an idle 2-instance
  On-Demand fleet at ~$317/mo using `monthly_usd_24x7`; actual CE charges were ~$0.05/hr
  (2 x $0.025 stopped-instance fee), ~$38/mo. `get_euc_service_prices` (applications) now
  returns `on_demand_stopped_hourly_usd` (Price List `instanceFunction=StoppedFleetInstance`)
  and a billing-model note per fleet type: ALWAYS_ON bills 24/7; ON_DEMAND bills streaming
  hours + stopped fee while idle; ELASTIC bills streaming hours only; builders bill full rate
  while RUNNING. SERVER_INSTRUCTIONS gained a matching CRITICAL block (including "check the
  creation date before attributing a full month of cost").

## [0.1.17] - 2026-07-03

Polish from the v0.1.16 live-validation round (no new tools; 30 tools unchanged).

### Added
- `get_euc_cost_forecast` now returns **run-rate context** — `recent_7d_daily_avg` and
  `trailing_30d_daily_avg` from actuals — and appends a warning note when the last 7 days run
  well above the 30-day baseline, since Cost Explorer's forecast extrapolates current usage and
  transient resources (e.g. RUNNING image builders) can inflate it. Observed live: a \$1,921/30d
  estate forecast at \$5,182 during a builder-heavy week.
- `get_euc_service_prices` (personal): when the exact storage pairing has no list SKU, the
  response now includes `available_storage_configurations` — the pairings AWS **does** price for
  that region/OS/compute with their AlwaysOn/AutoStop rates — explicitly labelled as near-miss
  listings, never as the price for the requested sizes.

### Fixed
- `get_euc_service_prices` (personal): the "no clean bundle match" note now also fires when the
  Price List query succeeds but no SKU matches the storage pairing (previously it only fired on
  invalid inputs, leaving unexplained nulls).

### Changed
- `get_euc_service_prices` (core): docstring and response note now state that fee SKUs are listed
  **one per billing option (hourly vs monthly)** and the account bills whichever option it is
  configured for — assistants must not assume the monthly fee applies.

## [0.1.16] - 2026-07-03

### Added
- **Pricing now covers the whole EUC portfolio** (was WorkSpaces-bundles-only). New
  `get_euc_service_prices` tool returns authoritative AWS Price List rates per region:
  - **Applications**: $/hour by instance type × function (Fleet/ImageBuilder/AppBlockBuilder/
    Elastic/MultiSession) × **license model** — Windows Server = included license, Windows 10/11 =
    BYOL (cheaper SKU) — plus derived $/day and $/month.
  - **Secure Browser**: $/monthly-active-user by portal tier (standard.regular/large/xlarge).
  - **Core Managed Instances**: management-fee SKUs (monthly/hourly variants) per instance type.
  - **Personal**: existing bundle rates (AlwaysOn monthly, AutoStop base+hourly), now exposed
    directly.
  Found via live validation: an assistant estimated a running builder at \$0.38/hr from model
  memory when the real Singapore BYOL rate is \$0.217/hr (75% overstated) — this tool exists so
  cost answers never come from memory.
- `audit_application_images` RUNNING image/app-block builder findings now include the **actual
  regional list rate** ("\$0.680/hr, ~\$496/mo if left running") — best-effort, requires Tier 1
  pricing permission.

### Fixed
- `generate_inventory_report` / CSV export no longer emit an empty `tags` column when
  `include_tags` is off.

## [0.1.15] - 2026-07-03

### Fixed
- **Spreadsheet-ready inventory attributes** — validated against a real export, several fields came
  out blank because values were nested or missing; all are now flat, populated columns:
  - Pools: `CapacityStatus` flattened to `desired/actual/active/available_user_sessions`, plus the
    pool's **bundle name / compute type / OS** resolved via `DescribeWorkspaceBundles`.
  - Applications fleets: `ComputeCapacityStatus` flattened to
    `desired/running/in_use/available_capacity`, plus `platform` (Elastic fleets), `stream_view`,
    and `image_arn`.
  - Stacks: `embed_host_domains`, `redirect_url`, `feedback_url` added.
  - Personal: `root/user_volume_encrypted` are now explicit booleans — AWS omits the flag when a
    volume is NOT encrypted, which previously exported as blank/"unknown" instead of False.

## [0.1.14] - 2026-07-03

### Added
- **`generate_euc_health_report`** — the one-call estate report: inventory, firing alarms
  (auto-scaling alarms excluded), 30-day cost + forecast + AlwaysOn→AutoStop savings, security
  posture, image/builder findings, and quota headroom, collected **concurrently** and returned as
  structured sections plus **ready-to-send markdown** — the building block for scheduled/weekly
  estate reports. Live: full report in ~8s.
- **`export_inventory_report_csv`** — writes the FULL per-resource inventory (no cap) to a local
  CSV and returns the path/row count, so estates with hundreds or thousands of devices get a
  spreadsheet instead of an oversized inline answer. Reads AWS only; the only write is the local
  file.
- **Large-estate handling** on `generate_inventory_report`: per-section cap
  (`max_resources_per_service`, default 100) with `total_count` + `truncated` markers, a
  `services` filter to narrow scope, and guidance steering assistants to summarize first / filter /
  export on big estates (also added to the server instructions).
- `get_secure_browser_portal_details` browser policy now includes **per-policy values**
  (size-guarded: long lists/strings truncated with counts) — e.g. force-installed extensions and
  proxy configuration, not just policy names.

### Changed
- Health-report savings line distinguishes "no candidates" from "candidates whose bundles have no
  Price List match" so a null estimate is never read as "no opportunity".

## [0.1.13] - 2026-07-03

API-coverage expansion: a systematic audit of all four EUC service APIs (we used 16 of 83 read
operations) turned into six themes of new capability, all validated live.

### Added
- **Live sessions everywhere**: `get_pool_session_history` and `diagnose_pool` now show who is
  connected to a Pool RIGHT NOW (`DescribeWorkspacesPoolSessions`); `get_application_fleet_usage`
  shows who is streaming from a fleet right now (`DescribeSessions` via associated stacks) — the
  same live-vs-historic split Secure Browser already had.
- **`audit_workspace_images`** — WorkSpaces Personal custom-image audit: ERROR states, aging
  images, and cross-account sharing (via `DescribeWorkspaceImages`/`DescribeWorkspaceImagePermissions`).
- **`review_application_access`** — who has access to WorkSpaces Applications: user-pool users and
  per-stack user assignments (`DescribeUsers`/`DescribeUserStackAssociations`).
- **`get_euc_account_posture`** — dedicated tenancy (BYOL) status + management CIDR, recent account
  modifications, per-directory client properties, and cross-region connection aliases.
- `generate_inventory_report`: desktops now include the **bundle name** (`DescribeWorkspaceBundles`)
  and an optional **`include_tags`** parameter adds resource tags across desktops/pools/fleets/
  stacks/portals.
- `audit_application_images`: **app-block builders** are now audited too — RUNNING ones bill hourly
  like image builders.

### Changed
- `audit_security_posture` goes deeper: flags **0.0.0.0/0 rules inside IP access control groups**,
  Secure Browser portals **without IP restrictions** or **without session logging**, and
  WorkSpaces Applications **usage reporting disabled**.
- `get_secure_browser_portal_details` now resolves the **Chrome browser policy** (names + URL
  allow/block lists), **IP access ranges**, **identity providers**, and **session-logging status**.
- `rebuild_workspaces`/`restore_workspace` dry-runs now state **the last snapshot time per target**
  (`DescribeWorkspaceSnapshots`), making the data-loss window concrete;
  `diagnose_workspace_connectivity` surfaces the same snapshot recency.

### IAM
New read-only actions added to every tier: `workspaces:DescribeIpGroups`/`DescribeWorkspaceSnapshots`/
`DescribeWorkspaceImages`/`DescribeWorkspaceImagePermissions`/`DescribeAccount`/
`DescribeAccountModifications`/`DescribeClientProperties`/`DescribeConnectionAliases`;
`appstream:DescribeUsageReportSubscriptions`/`DescribeAppBlockBuilders`/`DescribeUsers`/
`DescribeUserStackAssociations`/`ListTagsForResource`; `workspaces-web:GetBrowserSettings`/
`GetIpAccessSettings`/`ListIdentityProviders`/`ListTagsForResource`.

## [0.1.12] - 2026-07-03

### Fixed
- Assistants could conflate **user connections with power state** and wrongly report running
  (AVAILABLE) desktops as stopped — e.g. answering "what WorkSpaces are running?" from
  `analyze_workspace_utilization` (which classifies by logons over a window, not lifecycle state).
  The server instructions and the inventory/utilization/report tool descriptions now state
  explicitly: power state comes from `get_euc_inventory_summary` (`by_state`) or
  `generate_inventory_report` (per-desktop `State`); utilization classifications must never be
  used to claim a desktop is stopped. No API/data changes — the returned data was always correct.

## [0.1.11] - 2026-07-03

Alignment pass against the awslabs MCP catalog and DESIGN_GUIDELINES, plus features adapted from
the AWS Billing/Cost-Management and CloudWatch MCP servers (EUC-scoped, not raw mirrors).

### Added
- `get_euc_cost_forecast` — forecast upcoming EUC spend via Cost Explorer's model (mean total +
  per-period values with an 80% prediction interval), filtered to the EUC services discovered from
  recent actual spend. Adds `ce:GetCostForecast` to Tiers 1–3.
- `compare_euc_costs` — "why did my cost change?": compares two windows (baseline defaults to the
  preceding equal-length window) and returns totals, per-service deltas, and the top
  **usage-type-level drivers**, with WorkSpaces usage bucketed into Personal/Pools/Core.
- `get_euc_active_alarms` — CloudWatch alarms currently in ALARM whose metrics live in EUC
  namespaces (AWS/WorkSpaces, AWS/AppStream, AWS/WorkSpacesWeb), with auto-scaling policy alarms
  flagged as expected (idle scale-in alarms are not incidents). Adds `cloudwatch:DescribeAlarms`
  to every IAM tier.
- `NOTICE` file (Apache-2.0 convention).

### Changed
- **Cross-service tools now collect concurrently** (inventory summary, inventory report, security
  posture audit, unused resources): per-service collectors fan out on a thread pool, cutting
  wall-clock time to roughly the slowest single service; heavy tools also moved off the MCP event
  loop (`asyncio.to_thread`). The boto3 client factory is now thread-safe.
- **Tool parameters are bounds-validated** with pydantic `Field` constraints (e.g. `lookback_days`
  1–90, dates must be `YYYY-MM-DD`), so bad inputs fail fast with a clear message instead of
  triggering slow or oversized AWS queries.

## [0.1.10] - 2026-06-03

### Added
- `get_euc_cost_summary` now returns **`workspaces_breakdown`** — the single "Amazon WorkSpaces"
  Cost Explorer line split into **Personal / Pools / Core** via `USAGE_TYPE` (which the SERVICE
  dimension cannot do; pool charges carry `Pools`, Core carries `ManagedInstances`, the rest is
  Personal). This answers "is this WorkSpaces figure Personal-only or does it include Pools/Core?".
  Controlled by `split_workspaces` (default true). A note also explains that AlwaysOn monthly bundle
  charges post on the 1st, so day-1 of a DAILY series spikes legitimately.

## [0.1.9] - 2026-06-03

### Changed
- **SSO auto-login is now ON by default** (previously opt-in via `--sso-auto-login`). When an AWS
  call fails with an expired SSO token, the server automatically runs `aws sso login` (opens the
  browser) and reports in the result that the token expired and that sign-in was launched — no flag
  needed. Disable with **`--no-sso-auto-login`** or `WORKSPACES_EUC_SSO_AUTO_LOGIN=0` (e.g.
  headless/CI). The browser approval is still required and credentials are still never stored.

## [0.1.8] - 2026-06-03

### Added
- **`--sso-auto-login`** (opt-in; also `WORKSPACES_EUC_SSO_AUTO_LOGIN=1`): when an AWS call fails
  with an expired SSO token, the server automatically runs `aws sso login` — opening the browser to
  the approval screen — so the user re-authenticates without opening a terminal. Debounced so a
  burst of failing calls opens sign-in only once. Off by default; never stores credentials (it only
  invokes the AWS CLI). Expired-token errors also now carry a clearer hint, including that signing
  into the AWS Console does **not** refresh the CLI/SSO token.

## [0.1.7] - 2026-06-02

### Fixed
- `get_secure_browser_portal_usage` now reports **current active sessions live via
  `workspaces-web:ListSessions`** (the same source as the console's active-sessions view) instead
  of inferring "active" from CloudWatch. CloudWatch (`AWS/WorkSpacesWeb`) is now used **only for
  historic** metrics, and the summary clearly separates live vs historic. Adds
  `workspaces-web:ListSessions` to every IAM tier. The tool now returns a `SecureBrowserPortalUsage`
  result (`active_session_count`, `active_sessions`, `historic_metrics`).

## [0.1.6] - 2026-06-02

### Added
- `check_directory_health` now surfaces each directory's **registration properties** in its
  signals — notably the target **OU** (`WorkspaceCreationProperties.DefaultOu`), plus custom
  security group, local-admin / internet-access / maintenance-mode flags, and directory/workspace
  type. (AD-backed directories carry an OU; WorkSpaces-managed ones return none.)
- `get_secure_browser_portal_details` now resolves the portal's **data-protection configuration**
  when attached: the redacted built-in/custom inline-redaction patterns, global confidence level,
  and enforced/exempt URLs — not just whether data protection is on. Adds
  `workspaces-web:GetDataProtectionSettings` to every IAM tier.

## [0.1.5] - 2026-06-02

### Added
- `get_euc_audit_trail` — a new read-only (Tier 0) tool that reports recent EUC management activity
  from CloudTrail (always-on `LookupEvents`, 90-day window, no trail required) across WorkSpaces
  Personal/Pools/Core, WorkSpaces Applications, Secure Browser, and Core Managed Instances.
  Mutations-only by default ("who created/modified/terminated what"); flags destructive actions and
  errors (e.g. AccessDenied). Adds `cloudtrail:LookupEvents` to every IAM tier.
- `get_euc_service_quotas` — a new read-only (Tier 0) tool that reports Service Quotas limits per
  EUC service and, where AWS publishes a linked usage metric (`AWS/Usage` `ResourceCount`), the
  current usage and utilisation %, flagging quotas approaching their limit (capacity planning).
  Adds `servicequotas:ListServiceQuotas` / `GetServiceQuota` to every IAM tier.
- `audit_application_images` — a new read-only (Tier 0) tool that audits WorkSpaces Applications
  (AppStream 2.0) **images and image builders**: lists your PRIVATE/SHARED images (skipping PUBLIC
  base images) and flags stale base images (likely unpatched OS), pinned/old AppStream agents,
  non-AVAILABLE or errored images, SHARED cross-account visibility, and image builders left
  **RUNNING** (per-hour cost + interactive admin surface). Adds `appstream:DescribeImages` and
  `appstream:DescribeImageBuilders` to every IAM tier.

### Docs
- README and DESIGN.md reconciled to the shipped state: 21 read-only tools (Tiers 0–1) + 10 write
  (Tier 2) + 3 destructive (Tier 3); `tools/` layout, §5 tool catalog (image audit + governance),
  and the Tier 0 IAM action list all updated.

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
