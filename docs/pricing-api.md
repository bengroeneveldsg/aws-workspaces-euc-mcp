# The AWS Price List API for EUC services — a grounded reference

How the AWS Price List API (`pricing:GetProducts`, queried from `us-east-1`) actually models the
WorkSpaces EUC portfolio. Everything below was discovered empirically against the live API
(2026-07) and is what `workspaces_euc_mcp_server/tools/pricing.py` encodes. When the tool and
this document disagree with the API, the API wins — re-verify with
`aws pricing get-attribute-values` before changing either.

General API facts:

- Filters are `TERM_MATCH` (exact) only. There is no contains/any-of at this API layer, so
  anything enumerable must be enumerated and anything variable must be selected client-side.
- Results paginate at 100 products; single-page queries silently truncate.
- All rates are **public list prices** — private pricing, EDP/PPA discounts, and credits are not
  reflected. Cost Explorer actuals are the discounted truth.

## AmazonWorkSpaces (Personal + Pools + a WorkSpaces Core twin)

The most intricate of the four service codes. Key attributes: `bundle`, `operatingSystem`,
`license`, `runningMode`, `storage`, `productFamily`, `group`.

### productFamily is load-bearing

`Enterprise Applications` = WorkSpaces Personal/Pools. `WorkSpaces Core` = a **parallel SKU set
with the same bundle names but different (cheaper) prices** (e.g. Power-0 Windows AlwaysOn:
$116/mo Personal vs $112/mo Core in Singapore). Never query bundles without pinning
productFamily or the two families cross-contaminate.

### Storage variants are separate bundles

A compute type is a bundle FAMILY: the base name (`Power`) carries one canonical storage pairing
(Root:175/User:100) and suffixed variants carry the others — `Power-0` = 80/10, `Power-1` =
80/50, `Power-2` = 80/100 (suffix-to-storage mapping varies per family; match on the `storage`
attribute, never on the suffix). `"... Plus"` bundles (`Power Plus`) are different products
(software-bundled, e.g. Microsoft 365 components) with many per-component monthly rows — exclude
them from hardware pricing.

### Rate shape: monthly fees are per-storage, the hourly is per-compute

- AlwaysOn monthly and AutoStop monthly base: published per storage pairing (on the variant
  bundle that owns the pairing).
- AutoStop hourly: published ONCE per bundle family × OS × license, on the base bundle's rows —
  storage variants inherit it.
- $0 "software" rows exist alongside hardware rows; skip zero-priced dimensions.

### operatingSystem and license travel together

| Requested OS | `operatingSystem` value | `license` value |
|---|---|---|
| Windows Server (any) | `Windows` | `Included` |
| Windows 10 / 11 | `Windows` **or** `Any` (varies by SKU) | `Bring Your Own License` |
| Amazon Linux | `Amazon Linux` | `None` |
| Ubuntu | `Ubuntu Linux` | `None` |
| RHEL | `Red Hat Enterprise Linux` | `Included` |
| Rocky | `Rocky Linux` | `Included` |

There is **no** `operatingSystem=Linux` value, and Amazon Linux/Ubuntu are `license=None`, not
`Included`. Windows 10/11 is always BYOL (own licenses + dedicated tenancy) and its rates DIFFER
from Windows Server (SIN Power 175/100: $120 vs $124 AlwaysOn). Because BYOL SKUs carry two
possible OS values, select client-side rather than filtering the API on OS.

### WorkSpaces Pools lives here too

- Streaming rates: `runningMode=Pool`, per bundle, Included vs BYOL license (SIN Power: $0.48 vs
  $0.417/hr), `storage` fixed at Root:200/User:0.
- `bundle="Stopped Instance"` (runningMode `Not Applicable Pools`): $0.025/hr provisioned-idle.
- `bundle="User Fee"`: $4.19/user/month.
- Pools bill: streaming hourly while serving + stopped fee while idle + monthly user fee.

## AmazonAppStream (WorkSpaces Applications)

Key attributes: `instanceType`, `instanceFunction`, `operatingSystem`, `productFamily`,
`usagetype`, `multiSession`.

- `instanceFunction`: `Fleet`, `MultiSessionFleet`, `ElasticFleet`, `ImageBuilder`,
  `AppBlockBuilder`, and `StoppedFleetInstance` (the On-Demand idle fee — **one flat SKU per
  region**, $0.025/hr, instance-type-agnostic).
- `operatingSystem`: `Windows`, `Windows BYOL`, `Amazon Linux`, `Red Hat Enterprise Linux`,
  `Rocky Linux`, `Ubuntu Pro`. Windows 10/11 platforms = `Windows BYOL` (cheaper); Windows
  Server = `Windows` (included license). DescribeFleets omits Platform for non-Elastic fleets —
  resolve it from the fleet's IMAGE.
- Billing by fleet type: ALWAYS_ON bills the instance rate 24/7 per provisioned instance;
  ON_DEMAND bills the rate only while streaming + the stopped fee while provisioned idle;
  ELASTIC bills streaming hours only; builders bill the full rate while RUNNING.
- `productFamily="User Fees"`: Microsoft RDS SAL charged per unique user per month **on top of
  instance hours** for Included-license Windows fleets — SIN: $4.19 single-session, $6.42
  multi-session (first) + $2.23 each additional (`usagetype` distinguishes them). BYOL fleets
  do not incur these.

## AmazonWorkSpacesWeb (Secure Browser)

Simple: `bundle=Standard`, `size` ∈ Regular/Large/XLarge, billed $/monthly-active-user. The
`usagetype` suffix (`WEB-ST`, `WEB-ST-LARGE`, `WEB-ST-XLARGE`) maps to the portal instanceType
(`standard.regular/large/xlarge`).

## AmazonWorkSpacesInstances (Core Managed Instances)

Management-fee SKUs per `instanceType` with `billingoption` ∈ Hourly/Monthly (the account bills
whichever it is configured for — never assume Monthly) and `tenancy` ∈ Dedicated/Shared. The
underlying EC2/EBS costs bill separately on EC2. Note the AmazonWorkSpaces service code ALSO
carries a `WorkSpaces Core` product family (bundle-shaped Core SKUs) — the two views coexist.

## Design rules this repository derives from the above

1. **Select client-side** where the vocabulary is irregular (OS/license pairs, storage
   variants); filter server-side only on stable dimensions (location, bundle family,
   productFamily, instanceFunction).
2. **Null over wrong** — an unmatched configuration returns no price, plus a listing of the
   configurations AWS *does* price (never guess values).
3. **Paginate everything**; single pages truncate silently.
4. **State the license model and assumptions** (list prices, 730-h month) on every response.
