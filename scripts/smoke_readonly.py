# Copyright bengroeneveldsg. Licensed under the Apache License, Version 2.0 (the "License").
# You may not use this file except in compliance with the License.
# A copy of the License is located at http://www.apache.org/licenses/LICENSE-2.0
"""Read-only smoke test against a real AWS account.

Exercises the Tier 0/1 core functions end-to-end to validate API field assumptions. It makes ONLY
Describe/Get/List + CloudWatch/Cost Explorer read calls — it never mutates anything.

Usage (after authenticating, e.g. AWS_PROFILE / SSO / env creds):

    python scripts/smoke_readonly.py --region us-east-1
    python scripts/smoke_readonly.py --region us-east-1 --profile my-euc-admin

Needs the Tier 0 policy (iam/tier0-diagnostics.json); add Tier 1 to include the cost summary.
"""

from __future__ import annotations

import argparse
import json

from workspaces_euc_mcp_server.clients import ClientFactory
from workspaces_euc_mcp_server.tools import cost, inventory, reporting


def _dump(title: str, model) -> None:
    print(f"\n=== {title} ===")
    print(json.dumps(model.model_dump(), indent=2, default=str))


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only smoke test (no mutations).")
    parser.add_argument("--region", required=True, help="AWS region to inspect.")
    parser.add_argument("--profile", help="AWS named profile (optional).")
    parser.add_argument(
        "--lookback-days", type=int, default=14, help="Window for utilization (default 14)."
    )
    parser.add_argument(
        "--with-cost",
        action="store_true",
        help="Also call Cost Explorer (needs Tier 1; account-wide).",
    )
    args = parser.parse_args()

    factory = ClientFactory(region=args.region, profile=args.profile)
    print(f"Region: {factory.region}  (read-only — no changes will be made)")

    _dump("Inventory summary", inventory.collect_inventory(factory, args.region))
    _dump(
        "Inventory report",
        reporting.generate_inventory_report_core(factory, args.region),
    )
    _dump(
        "Security posture audit",
        reporting.audit_security_posture_core(factory, args.region),
    )
    _dump(
        "Workspace utilization",
        cost.analyze_workspace_utilization_core(factory, args.region, args.lookback_days),
    )
    _dump(
        "Running-mode recommendations",
        cost.recommend_running_mode_core(factory, args.region, args.lookback_days),
    )
    _dump(
        "Unused resources",
        reporting.list_unused_resources_core(factory, args.region, args.lookback_days),
    )

    if args.with_cost:
        _dump("EUC cost summary", cost.get_euc_cost_summary_core(factory))

    print("\nSmoke test complete (read-only).")


if __name__ == "__main__":
    main()
