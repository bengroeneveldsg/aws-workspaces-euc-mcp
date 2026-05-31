# IAM policy tiers

The server ships one least-privilege policy per capability tier. Tiers are **additive** and the
default install needs only **Tier 0**. Grant the smallest tier that covers the tools you enable.

| Tier | File | Covers | Mutates? |
|------|------|--------|----------|
| 0 | [`tier0-diagnostics.json`](tier0-diagnostics.json) | Inventory, troubleshooting, diagnostics (read-only) | No |
| 1 | [`tier1-cost.json`](tier1-cost.json) | Cost & utilization optimization (adds Cost Explorer / Pricing) | No |
| 2 | [`tier2-lifecycle.json`](tier2-lifecycle.json) | Lifecycle ops: start/stop/reboot + modify running mode | Yes |
| 3 | _planned_ | Destructive ops (terminate/rebuild/restore) | Yes (irreversible) |

> Tier 2 only grants effect when the server is also launched with `--enable-writes`. Even then,
> mutations are dry-run unless the caller passes `confirm=true`, and bulk actions are capped by
> `--max-bulk-targets`.

## Notes

- Many EUC `Describe*` / `List*` actions do **not** support resource-level permissions, so the
  policies use `"Resource": "*"`. Where a service supports tag or directory conditions, scope
  further with an IAM `Condition` block.
- Attach the policy to the role/identity whose credentials the server uses (via `AWS_PROFILE`,
  SSO, or an assumed role). For a stronger posture, use **IAM context keys to distinguish the
  agent identity from the human operator**, mirroring the AWS MCP Server pattern.
- All underlying API calls are captured by **AWS CloudTrail** for audit.
