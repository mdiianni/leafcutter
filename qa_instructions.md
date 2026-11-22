# Data QA & Sanity Check Instructions

## Purpose

Before distributing or acting on report findings, the data must pass a set of sanity checks
to ensure that each account's recommendations contain **only** information relevant to:

1. **Its own environment** — a DEV account must not contain STG or PRD resource references
2. **Its own group/project** — an account must not contain resources belonging to another group

Cross-contamination typically arises from:
- Shared EKS clusters or ECR registries that span environments
- Org-level recommendations (Savings Plans, billing) that are intentionally cross-account
- Misconfigured analysis runs where the wrong AWS profile/role was assumed

---

## Account Registry

The account registry is stored in `accounts.json` (gitignored — never committed).
See `accounts.example.json` for the expected format:

```json
{
  "123456789012": {"name": "my-app", "env": "dev", "group": "my-app"},
  "234567890123": {"name": "my-app", "env": "prd", "group": "my-app"}
}
```

`generate_dashboard_data.py` reads this file at runtime.
If `accounts.json` is absent, the tool auto-discovers account IDs from `cache/`.

---

## Check 1 — Environment Isolation

Each account should only contain recommendations for resources that belong to its own
environment. Scan `resource_name`, `resource_id`, and `recommendation` fields.

### Environment keyword patterns

| Env | Patterns that indicate the resource belongs to that env |
|---|---|
| dev | `-dev-`, `-dev.`, `_dev_`, `env='dev'`, `env=dev`, `/dev/` |
| stg | `-stg-`, `-stg.`, `_stg_`, `env='stg'`, `env=stg`, `/stg/` |
| prd | `-prd-`, `-prd.`, `_prd_`, `env='prd'`, `env=prd`, `/prd/` |

A DEV account with a row containing `-prd-` or `-stg-` in the resource name/recommendation
is **suspicious** and must be reviewed.

### Exempt opportunity types

The following opportunity types are intentionally org-wide or cross-account and are **exempt**
from environment isolation checks:

- `savings-plan-gap`
- `compute-savings-plan`
- `ecr-broad-replication`
- `billing-*` (any billing service type)
- `reserved-instance` rows where the resource is `ACCOUNT`

---

## Check 2 — Group/Project Isolation

Each account should only contain recommendations for resources belonging to its own group.
Use resource names, EKS cluster names, ECS service names, S3 bucket names, and tag values.

### Group identifier patterns

Populate this table based on your own `accounts.json` groups and naming conventions:

| Group | Name patterns to look for |
|---|---|
| my-app | `my-app`, `myapp` |
| platform | `platform`, `plat` |
| core | `core-infra`, `core` |

A row in an account belonging to group `my-app` that references a resource with another
group's name pattern is **suspicious**.

---

## How to Run the QA Checks

```bash
python qa_check.py
```

The script reads the latest CSV files for each account and produces a report of:
- Total recommendations per account
- Suspicious rows (wrong env or wrong group)
- Suggested action for each finding

### Output format

```
══════════════════════════════════════
GROUP / ENV          ACCOUNT
══════════════════════════════════════
Total recommendations : NNN
Suspicious rows       : N

  [ENV-CROSS]  resource_name | opportunity_type
               → Contains '-prd-' in resource_name; account is DEV
               Recommendation (truncated): ...

  [GRP-CROSS]  resource_name | opportunity_type
               → Contains pattern from another group; account group is my-app
               Recommendation (truncated): ...
```

Accounts with zero findings are omitted.

---

## Interpreting Findings

| Finding type | Likely cause | Likely action |
|---|---|---|
| DEV account has PRD-named resources | Wrong AWS profile used during collection; or shared account hosts both envs | Re-collect with correct profile; or document shared-account exception |
| PRD account references a DEV resource | Shared infrastructure (e.g. shared ECR, shared VPC) | Document as shared-infra; suppress the row if it's a false recommendation |
| Account references another group's resources | Cross-account access; shared services (ECR, Route53) | Investigate if resource truly belongs to this account; suppress or re-scope |
| Org-level recommendation present in multiple accounts | Expected — Savings Plan gap applies at org level | No action needed; these are flagged as exempt |

---

## Known Suppressions

The following findings are **intentional and should not be flagged** by future QA runs.
Add your own suppressions here after reviewing them:

| Account(s) | Pattern | Reason | Confirmed by |
|---|---|---|---|
| *(example)* my-app (dev/stg/prd) | `shared-service-lambda-*` Lambda | This Lambda lives in all accounts by design | Review YYYY-MM-DD |

When updating the QA scan script (`qa_check.py`), add these as row-level suppression rules checked before flagging.

---

## Incident Log

### Example — DEV account contaminated with PRD data

**Root cause:** The `analyse_ecs.py`, `analyse_elb.py`, and `analyse_cloudwatch.py` scripts were
run with the wrong AWS profile (PRD account `<PRD_ACCOUNT_ID>` instead of DEV account
`<DEV_ACCOUNT_ID>`). This caused PRD ECS services, ELBs, and CloudWatch resources to
be written into the DEV account's cache and CSV reports.

**Affected files (example):**
- `cache/<DEV_ACCOUNT_ID>/eu-central-1/ecs.json` — contaminated record at index [1]
- `cache/<DEV_ACCOUNT_ID>/eu-central-1/elb.json` — contaminated record at index [1]
- `cache/<DEV_ACCOUNT_ID>/eu-central-1/cloudwatch.json` — contaminated record at index [1]
- `output/20260324_130411_ecs_report.csv`
- `output/20260324_130411_elb_report.csv`
- `output/20260324_130411_cloudwatch_report.csv`

**Resolution:**
- Contaminated cache records removed; clean records retained (backed up as `*.json.bak`)
- Contaminated CSV files moved to `output/quarantine/`
- `dashboard_data.json` regenerated from clean data
- `output/AWS_Cost_Dashboard.html` rebuilt

**Action required:** Re-run affected analysers with the correct DEV profile:
```bash
# Set correct DEV profile before re-running
export AWS_PROFILE=my-dev-profile
python analyse_ecs.py
python analyse_elb.py
python analyse_cloudwatch.py
# Then regenerate:
python generate_dashboard_data.py
cd dashboard && BUILD_MODE=singlefile npm run build
```

---

## Fixing Confirmed Issues

Once the findings have been reviewed and confirmed:

1. **Wrong data collected** (wrong profile/role): delete the stale cache file and re-collect:
   ```bash
   rm cache/{account_id}/{region}/{service}.json
   python analyse_{service}.py  # re-collect with correct profile
   ```

2. **Shared resource that legitimately appears in multiple accounts**: document the exception
   in the relevant `skills/*.md` under a `## Shared Resources` section.

3. **False recommendation based on cross-account visibility**: add a suppression rule in the
   relevant `analyse_{service}.py` skill's `analyse()` function.

4. After fixes, regenerate:
   ```bash
   python generate_dashboard_data.py
   cd dashboard && BUILD_MODE=singlefile npm run build
   cp dashboard/dist/index.html output/AWS_Cost_Dashboard.html
   ```
   Bump `REPORT_VERSION` (PATCH if data fix only, MINOR if scope/logic changed).
