# AWS Static Cost Analyser — Main Instructions

## Overview

The AWS Static Cost Analyser is a CLI-driven tool that collects metrics from a target AWS account
and region, identifies cost-saving opportunities across all active managed services, persists
results locally, and feeds a React-based dashboard that surfaces trends over time.

The tool is **read-only**: it never modifies AWS resources. It uses only `Describe*`, `List*`,
`Get*`, `CloudWatch GetMetricStatistics`, and `CostExplorer Get*` API calls.

---

## Getting Started (First-Time Setup)

### Prerequisites

| Requirement | Minimum version | Check with |
|---|---|---|
| Python | 3.10+ | `python3 --version` |
| AWS CLI v2 | 2.x | `aws --version` |
| Node.js *(dashboard only)* | 18+ | `node --version` |
| npm *(dashboard only)* | 9+ | `npm --version` |

> **Note:** Node.js and npm are only required if you want to run the interactive dashboard
> locally or rebuild the self-contained HTML export (`--html`).

### Step 1 — Clone / download the project

```bash
git clone <repository-url> aws-cost-analyser
cd aws-cost-analyser
```

### Step 2 — Install Python dependencies

Dependencies are listed in `requirements.txt` and will be installed **automatically**
the first time you run `run_analysis.py`.  To install manually:

```bash
pip install -r requirements.txt
```

Packages required:

| Package | Purpose |
|---|---|
| `boto3` | AWS SDK — used by all 29 service analysers |
| `python-pptx` | PowerPoint report generation (`generate_presentation.py`) |

### Step 3 — Configure your AWS profiles

Ensure your `~/.aws/config` contains named profiles for the accounts you want to analyse.
The tool reads `AWS_PROFILE` environment variable or the `--profile` flag — it never stores
credentials itself.

```bash
# Verify a profile is working
aws sts get-caller-identity --profile my-aws-profile
```

### Step 4 — Discover account metadata (recommended, one-time)

Run the account discovery script to resolve your profiles to account IDs and infer
the name, environment, and group from the profile name automatically:

```bash
python3 discover_accounts.py --all --strip-prefix myorg-
```

This writes `cache/{account_id}/account_meta.json` locally (gitignored).
Re-run whenever you add new accounts.

### Step 5 — Install dashboard dependencies (optional)

```bash
cd dashboard
npm install
cd ..
```

### Step 6 — Run the analysis

```bash
# Analyse a single account (all 29 services)
python3 run_analysis.py --profile my-aws-profile

# Analyse multiple accounts
python3 run_analysis.py --profile dev-profile stg-profile prd-profile

# Analyse specific services only
python3 run_analysis.py --profile my-aws-profile --services ec2 rds s3

# Full run with version bump and self-contained HTML export
python3 run_analysis.py --profile my-aws-profile --bump --html
```

`run_analysis.py` will:
1. Check and install missing Python dependencies automatically
2. Run the selected service analysers sequentially with a 2 s rate-limit delay
3. Refresh `dashboard_data.json` after the run
4. Optionally bump the report version and rebuild the static HTML export

### Step 7 — View the dashboard

```bash
# Option A — Interactive dev server (requires Node.js)
cd dashboard && npm run dev

# Option B — Open the self-contained HTML file (no server needed)
open output/AWS_Cost_Dashboard.html
```

---

## Docker End-to-End Test

Use `test-container.sh` to verify the distributable ZIP works correctly on a clean
**linux/amd64** environment — before sharing it, after a new release, or when onboarding
a new team member.

### What the test does (automated flow)

| Step | Action |
|---|---|
| 1 | Pre-flight: checks Docker is running + ZIP exists + profile resolved in `~/.aws/config` |
| 2 | Builds a **linux/amd64** image (`Dockerfile.test`) with Python 3.11 + Node 20 |
| 3 | Starts a detached container; mounts `~/.aws` read-only; sets `AWS_PROFILE` |
| 4 | Copies the ZIP into the container and extracts it |
| 5 | Installs Python/Node deps inside the container (fresh, no host cache) |
| 6 | Runs `python3 run_analysis.py --profile <PROFILE> --html` |
| 7 | Serves `output/AWS_Cost_Dashboard.html` on port 8080 via `python3 -m http.server` |
| 8 | Opens `http://localhost:8080/AWS_Cost_Dashboard.html` in your local browser |

### Profile name resolution

The script supports **short profile names** — it will fuzzy-match against `~/.aws/config`
using suffix matching, so you don't need to type the full org-prefixed name:

```bash
# These are equivalent if "playground-1-dev" exists in ~/.aws/config:
bash test-container.sh --profile playground-1-dev
bash test-container.sh --profile playground-1-dev
```

Resolution order:
1. Exact match against `[profile <name>]` entries
2. Suffix match (e.g. `playground-1-dev` → `playground-1-dev`)
3. Substring match
4. Use as-is (with a warning) if nothing matches
| 8 | Opens `http://localhost:8080/AWS_Cost_Dashboard.html` in your local browser |

### Prerequisites

```bash
# Docker Desktop (or Docker Engine) must be running
docker info

# The distributable ZIP must exist (generate it first)
bash package.sh

# The target AWS profile must be configured (credentials or SSO cache live)
aws sts get-caller-identity --profile playground-1-dev
```

### Quick start

```bash
# Full automated test — runs analysis + opens dashboard in browser
bash test-container.sh --profile playground-1-dev

# With org prefix stripping so account name/group/env resolve correctly in the dashboard
# e.g. "myorg-app-dev" → name="app", env="dev", group="app"
bash test-container.sh --profile playground-1-dev --strip-prefix myorg-

# Interactive shell — explore the container manually (no analysis run)
bash test-container.sh --profile playground-1-dev --strip-prefix myorg- --shell

# Custom port; remove container + image when done
bash test-container.sh --profile playground-1-dev --strip-prefix myorg- --port 9090 --clean

# Test a specific ZIP archive
bash test-container.sh --profile my-profile --zip /tmp/aws-cost-analyser-2.0.0.zip
```

> **Tip:** If your profiles are named `myorg-app-dev`, pass `--strip-prefix myorg-` so the
> dashboard shows `app (dev)` rather than `myorg-app (dev)` under group/env filters.

### Interactive exploration

The test container stays running after the script completes, so you can connect at any time:

```bash
# Connect to the running container
docker exec -it aws-cost-analyser-test bash

# Inside the container — run a partial analysis
python3 run_analysis.py --profile playground-1-dev --services s3 ec2

# Serve the report manually
python3 -m http.server 8080 --directory output/
# Then open: http://localhost:8080/AWS_Cost_Dashboard.html
```

### Copy reports back to host

```bash
# Copy the entire output directory from the container to your local machine
docker cp aws-cost-analyser-test:/aws-cost-analyser/output ./output-from-container
```

### Cleanup

```bash
# Stop and remove the container
docker rm -f aws-cost-analyser-test

# Remove the test image (frees ~700 MB)
docker rmi aws-cost-analyser-test:latest

# Or use --clean flag to do both automatically on exit:
bash test-container.sh --profile playground-1-dev --clean
```

### AWS credential handling inside the container

The container mounts `~/.aws` as **read-only** — no credentials are copied or stored.
This covers:

| Auth method | Handled automatically |
|---|---|
| Long-term IAM keys (`~/.aws/credentials`) | ✓ |
| SSO cached tokens (`~/.aws/sso/cache/`) | ✓ |
| Assume-role chains (`~/.aws/config`) | ✓ |
| Instance profile / ECS task role | ✗ (local test only) |

> **Note:** If using AWS SSO, ensure you have a valid SSO session before running the test:
> `aws sso login --profile playground-1-dev`

### Dockerfile.test

The `Dockerfile.test` is intentionally minimal:

```
python:3.11-slim  (linux/amd64)
  └── apt: curl, unzip, gnupg, procps
  └── Node.js 20 LTS (via NodeSource)
  └── pip: upgraded baseline only
       └── requirements.txt installed at runtime by run_analysis.py
           └── dashboard/node_modules installed at runtime by npm install
```

No pre-baked credentials, no hardcoded profiles, no sensitive data.

### Continuous integration

To run the test non-interactively in a CI pipeline:

```yaml
# Example GitHub Actions snippet
- name: Test distributable package
  run: |
    bash package.sh
    bash test-container.sh \
      --profile ${{ secrets.AWS_PROFILE }} \
      --no-browser \
      --clean
  env:
    AWS_PROFILE: ${{ secrets.AWS_PROFILE }}
```

---

## Open-Source Policy — Keeping the Codebase Shareable

This tooling is designed to be **organisation-agnostic** so it can be shared with other teams,
entities, or open-sourced without leaking sensitive information.

### What must never be committed to version control

| Category | Examples | Where it lives instead |
|---|---|---|
| AWS account IDs | `123456789012` | `accounts.json` (gitignored) |
| AWS profile names specific to your org | `my-company-prod` | Environment variable `AWS_PROFILE` |
| Company or brand names | Any company, project, or team names | `accounts.json` labels only |
| Domain names | `mycompany.example.com` | Not stored in code |
| Resource names that identify your org | MSK cluster names, ECR repos, etc. | Only appear in local `cache/` and `output/` |
| Real cost figures or invoice amounts | `$348,858/mo` | `dashboard_data.json` (gitignored) |

### Gitignored paths (see `.gitignore`)

```
accounts.json          # Your account registry — contains real account IDs
dashboard_data.json    # Generated output — contains cost figures
cache/                 # Raw API responses — contains real resource names
output/                # CSV reports and generated HTML/PPTX
```

### Rules for contributors and skill files

1. **No hardcoded account IDs** — use `<YOUR_ACCOUNT_ID>` as placeholder in docs/examples.
2. **No hardcoded profile names** — all scripts must read from `os.environ.get("AWS_PROFILE", "my-aws-profile")`.
3. **No company-specific resource name patterns** — skill files must describe generic patterns, not company-specific naming conventions.
4. **Validated examples in skill files** — use the placeholder `account <YOUR_ACCOUNT_ID>` to indicate where a real-world observation occurred without revealing the actual ID.
5. **Default profile** in all `analyse_*.py` must be `"my-aws-profile"`.

### Setting up for a new organisation

```bash
# 1. Copy the example account config
cp accounts.example.json accounts.json

# 2. Edit accounts.json with your real account IDs and project names
#    (this file is gitignored and will never be committed)

# 3. Run all checks and refresh the dashboard
#    (dependencies are installed automatically on first run)
python3 run_analysis.py --profile my-aws-profile

# 4. Or run specific services only
python3 run_analysis.py --profile my-aws-profile --services ec2 rds s3

# 5. Run multiple profiles, bump version, and rebuild the HTML export
python3 run_analysis.py --profile dev-profile stg-profile prd-profile --bump --html
```

### Wrapper reference

```
python3 run_analysis.py --help
```

| Flag | Default | Description |
|---|---|---|
| `--profile / -p` | required | One or more AWS CLI profile names |
| `--services / -s` | all (29) | Limit to specific service slugs |
| `--region / -r` | eu-central-1 | AWS region |
| `--delay / -d` | 2 s | Pause between analysers (rate-limit guard) |
| `--bump [patch\|minor\|major]` | off | Auto-bump REPORT_VERSION after run |
| `--no-dashboard` | off | Skip dashboard_data.json refresh |
| `--html` | off | Rebuild self-contained HTML export via Vite |
| `--list-services` | — | Print all available service slugs and exit |
| `--dry-run` | — | Preview execution plan without running |

---

## AWS Organisations Awareness

> This tool is designed to work correctly in **both standalone accounts and AWS Organisations
> child accounts**. Several AWS APIs behave differently in org child accounts — the sections
> below document the known differences and the correct approach for each.

### Savings Plans

In AWS Organisations, Savings Plans are typically purchased at the **management account** level
and shared to child accounts. The key behaviours:

| API | Standalone Account | Org Child Account |
|---|---|---|
| `savingsplans.describe_savings_plans()` | Returns owned SPs ✅ | Returns **empty list** even when SP benefits ARE applied ❌ |
| `ce.get_cost_and_usage(GroupBy=RECORD_TYPE)` | Shows SP record types ✅ | Shows SP record types ✅ |
| `ce.get_savings_plans_coverage()` | Returns coverage ✅ | Returns coverage ✅ |
| `ce.get_savings_plans_utilization()` | Works ✅ | Raises `DataUnavailableException` ❌ |

**Rule:** Always use Cost Explorer record types (`SavingsPlanCoveredUsage`, `SavingsPlanNegation`)
as the authoritative source for SP coverage. Never rely on `describe_savings_plans()` alone.

The relevant CE record types:
- `SavingsPlanCoveredUsage` — compute charged at OD rate before SP discount
  - **Unblended** = what it would cost at On-Demand rates
  - **Amortized** = effective cost after SP discount
  - Saving = unblended − amortized
- `SavingsPlanNegation` — the credit applied (always negative unblended amount)
- `Usage` — on-demand compute spend **not** covered by any SP (the gap to fix)

### Spot Instances

The `InstanceLifecycle` field returned by `ec2.describe_instances` is the **only reliable source**
for determining if an EC2 instance is Spot. User-defined tags (e.g., `Lifecycle`, `PricingModel`)
are set by convention and cannot be trusted — their values vary widely by team.

```python
is_spot = instance.get("InstanceLifecycle", "") == "spot"
```

All skills that analyse EC2 instances (EC2, ECS EC2 launch type, EKS node groups) must use this
field. Any skill that recommends "migrate to Spot" must first confirm `is_spot = False`.

---

## Prerequisites

| Requirement | Minimum version | Notes |
|---|---|---|
| Python | 3.11 | Tool backend is written in Python |
| AWS CLI | v2.x | Must be configured and working |
| boto3 | 1.34+ | `pip install boto3` |
| Node.js | 20 LTS | For the React frontend |
| npm / pnpm | latest | Package manager |
| SQLite | 3.x | Bundled with Python |

### AWS Permissions Required

The executing IAM principal (user or role) needs at minimum:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "CostAnalyserReadOnly",
      "Effect": "Allow",
      "Action": [
        "ec2:Describe*",
        "s3:ListAllMyBuckets",
        "s3:GetBucketLifecycleConfiguration",
        "s3:GetBucketMetricsConfiguration",
        "s3:GetBucketVersioning",
        "s3:GetBucketLocation",
        "s3:GetBucketTagging",
        "ecs:ListClusters",
        "ecs:ListServices",
        "ecs:DescribeServices",
        "ecs:DescribeTaskDefinition",
        "ecr:DescribeRepositories",
        "ecr:DescribeImages",
        "ecr:GetLifecyclePolicy",
        "rds:DescribeDBInstances",
        "rds:DescribeDBClusters",
        "rds:DescribeReservedDBInstances",
        "elasticloadbalancing:DescribeLoadBalancers",
        "elasticloadbalancing:DescribeTargetGroups",
        "elasticloadbalancing:DescribeTargetHealth",
        "lambda:ListFunctions",
        "lambda:GetFunctionConcurrency",
        "dynamodb:ListTables",
        "dynamodb:DescribeTable",
        "elasticache:DescribeCacheClusters",
        "elasticache:DescribeReservedCacheNodes",
        "kafka:ListClusters",
        "kafka:DescribeCluster",
        "eks:ListClusters",
        "eks:DescribeCluster",
        "eks:ListNodegroups",
        "eks:DescribeNodegroup",
        "cloudwatch:GetMetricStatistics",
        "cloudwatch:ListMetrics",
        "logs:DescribeLogGroups",
        "pricing:GetProducts",
        "ce:GetCostAndUsage",
        "ce:GetSavingsPlansCoverage",
        "ce:GetSavingsPlansUtilization",
        "savingsplans:DescribeSavingsPlans",
        "ec2:DescribeReservedInstances"
      ],
      "Resource": "*"
    }
  ]
}
```

---

## Repository Structure

```
aws_cost_analyser/
├── instructions.md                  ← You are here
├── infographics_instructions.md     ← Frontend / visualisation spec
├── datastore_instructions.md        ← Data model & persistence spec
├── html_report_instructions.md      ← Static HTML report versioning & rebuild workflow
├── skills/                          ← One file per AWS service
│   ├── ec2.md
│   ├── s3.md
│   ├── ecs.md
│   ├── ecr.md
│   ├── rds.md
│   ├── ebs.md
│   ├── lambda.md
│   ├── msk.md
│   ├── nat-gateway.md
│   ├── elb.md
│   ├── elasticache.md
│   ├── dynamodb.md
│   ├── cloudwatch.md
│   └── eks.md
├── analyser/                        ← Python backend
│   ├── main.py                      ← Entry point
│   ├── config.py                    ← Region, profile, paths
│   ├── cache.py                     ← Local metric caching logic
│   ├── pricing.py                   ← AWS Pricing API helpers
│   ├── db.py                        ← SQLite read/write helpers
│   ├── reporter.py                  ← CSV + SQLite snapshot writer
│   └── skills/                      ← One module per service skill
│       ├── ec2.py
│       ├── s3.py
│       ├── ecs.py
│       └── ...
├── frontend/                        ← React dashboard
│   ├── package.json
│   └── src/
│       └── ...
├── cache/                           ← Auto-created; gitignored
│   └── {account_id}/{region}/       ← Raw JSON API responses
├── data/
│   └── cost_analysis.db             ← SQLite snapshot history
└── output/
    └── {timestamp}_report.csv       ← Per-run CSV export
```

---

## Running the Tool

### 1. First-time setup

```bash
# Clone / navigate to the repo
cd aws_cost_analyser

# Install Python dependencies
pip install -r requirements.txt

# Install and build the frontend
cd frontend && npm install && npm run build && cd ..
```

### 2. Run an analysis

```bash
# Using the default AWS_PROFILE
export AWS_PROFILE=my-profile
python analyser/main.py --region eu-west-1

# Or pass profile and region explicitly
python analyser/main.py --profile my-profile --region us-east-1

# Force-refresh all cached data (ignore cache TTL)
python analyser/main.py --profile my-profile --region eu-west-1 --refresh-cache

# Analyse a specific subset of services only
python analyser/main.py --services ec2,rds,ebs --region eu-west-1
```

### 3. Open the dashboard

```bash
cd frontend
npm run dev
# Opens http://localhost:5173
```

---

## How the Tool Works — Step by Step

```
┌──────────────────────────────────────────────────────────────┐
│  1. Resolve account ID and region                            │
│  2. For each enabled service skill:                          │
│     a. Check cache/{account}/{region}/{service}.json         │
│        → If fresh (< TTL): use cached data                   │
│        → If stale / missing: call AWS APIs, save to cache    │
│     b. Run the service's analyse() function                  │
│        → Returns list of Opportunity objects                 │
│  3. Fetch current pricing for affected resource types        │
│     (also cached in cache/pricing/{region}/)                 │
│  4. Persist snapshot to SQLite (data/cost_analysis.db)       │
│  5. Export CSV to output/{timestamp}_report.csv              │
│  6. Print summary table to stdout                            │
└──────────────────────────────────────────────────────────────┘
```

---

## Cache Strategy

### TTL and Format

| Cache type | Default TTL | Location |
|---|---|---|
| Service resource metadata | **30 days** | `cache/{account}/{region}/{service}.json` |
| CloudWatch metrics | 30 days | (embedded in service cache file) |
| Pricing data | 7 days | `cache/pricing/{region}/{service}.json` |

Cache files are plain JSON, keyed on `(account_id, region, service)`.

> ⚠️ The `cache/` directory **must be gitignored** as it may contain account-specific metadata.

### Append-Only Cache — Historical Records

**Cache files are append-only. Existing records are NEVER deleted or overwritten.**

Each time a refresh is triggered (`--refresh-cache` flag), a new snapshot record is appended to
the cache file. All previous snapshots are preserved. This enables historical trend analysis:
comparing costs and opportunities across multiple runs over time.

**Cache file structure (v2):**
```json
{
  "account_id": "123456789012",
  "region":     "eu-central-1",
  "service":    "ecs",
  "records": [
    {
      "collected_at": "2026-03-23T10:36:54+00:00",
      "expires_at":   "2026-04-22T10:36:54+00:00",
      "data": { ... }
    },
    {
      "collected_at": "2026-04-15T09:00:00+00:00",
      "expires_at":   "2026-05-15T09:00:00+00:00",
      "data": { ... }
    }
  ]
}
```

Shared cache utilities live in `cache_utils.py`:
- `load_cache(path)` — returns the most recent non-expired record's `data`, or `None`
- `save_cache(path, data, account_id, region)` — appends a new record (never overwrites)
- `load_all_records(path)` — returns all records sorted oldest-first (for trend charts)
- `CACHE_TTL_DAYS = 30`

### Cache-First Policy (default behaviour)

**When running an analysis or requesting a summary for a service, the tool ALWAYS reads from
cache first.** It will NOT call AWS APIs unless:

1. No cache file exists for that service, OR
2. The most recent record in the cache has expired (past its 30-day TTL), OR
3. `--refresh-cache` is explicitly passed on the command line.

This means running `python analyser/main.py --services s3` multiple times within 30 days will
produce the same results instantly from cache — no AWS API calls, no costs, no waiting.

**To force a fresh pull and add a new historical snapshot for a specific service:**
```bash
python analyser/main.py --services s3 --refresh-cache
```

**To force a fresh pull for all services:**
```bash
python analyser/main.py --refresh-cache
```

> 📌 **Rule for this tool:** Treat `--refresh-cache` as an explicit opt-in. Never silently
> invalidate or skip a valid cache entry. If the cache is fresh, use it — always.

### Old v1 Format Migration

Cache files written before v2 (single `_meta` + `data` at root) are automatically migrated
to v2 format on first read. The old record is preserved inside the new `records` array and
marked as expired so the next `--refresh-cache` will append a fresh snapshot alongside it.

---

## Opportunity Severity Levels

Each identified cost-saving opportunity is tagged with a severity:

| Level | Icon | Meaning |
|---|---|---|
| `CRITICAL` | 🔴 | Immediate savings available; no risk |
| `HIGH` | 🟠 | High savings potential; low risk |
| `MEDIUM` | 🟡 | Moderate savings; some validation needed |
| `LOW` | 🟢 | Minor savings or informational |
| `DEPENDENCY` | ⚠️ | Savings available but requires application changes |

---

## Opportunity Output Schema

Each opportunity emitted by a skill module has the following fields:

| Field | Type | Description |
|---|---|---|
| `run_id` | UUID | Unique per analysis run |
| `account_id` | string | AWS account ID |
| `region` | string | AWS region |
| `service` | string | AWS service name (e.g. `ec2`, `rds`) |
| `resource_id` | string | ARN or resource identifier |
| `resource_name` | string | Human-readable name / tag |
| `opportunity_type` | string | Category (e.g. `right-sizing`, `reserved-instance`) |
| `severity` | string | CRITICAL / HIGH / MEDIUM / LOW / DEPENDENCY |
| `current_cost_usd_monthly` | float | Estimated current monthly cost |
| `estimated_saving_usd_monthly` | float | Estimated monthly saving |
| `saving_percentage` | float | Saving as percentage of current cost |
| `recommendation` | string | Human-readable recommendation |
| `dependency_warning` | string | Description of required dependencies (if any) |
| `details` | JSON string | Raw supporting evidence (metrics, config) |
| `captured_at` | ISO timestamp | When this opportunity was recorded |

---

## Adding a New Service Skill

1. Create `skills/{service}.md` following the skill template below.
2. Create `analyser/skills/{service}.py` implementing:
   - `collect(session, region, cache) -> dict` — fetches and caches raw data
   - `analyse(raw_data, pricing) -> list[Opportunity]` — pure function; returns opportunities
3. Register the skill in `analyser/config.py` under `ENABLED_SKILLS`.

### Skill Module Template

```python
# analyser/skills/my_service.py

SKILL_NAME = "my_service"
# Cache TTL is managed centrally in cache_utils.py (CACHE_TTL_DAYS = 30)

def collect(session, region: str, cache) -> dict:
    """Fetch raw data from AWS and persist to cache."""
    client = session.client("my-service", region_name=region)
    data = {}
    # ... AWS API calls ...
    cache.save(SKILL_NAME, data)
    return data

def analyse(raw_data: dict, pricing: dict) -> list:
    """Pure analysis — no AWS calls here."""
    opportunities = []
    # ... analysis logic ...
    return opportunities
```

---

## Continuous Improvement Workflow

1. **Run the analysis** against the target account and region.
2. **Review the dashboard** — identify the highest-value opportunities.
3. **Implement fixes** in AWS (e.g., add lifecycle policies, migrate to gp3).
4. **Re-run the tool** — it will create a new snapshot and the dashboard will show before/after.
5. **Update the relevant `skills/*.md`** file if new opportunity patterns are discovered.
6. **Commit** the updated skill file so the knowledge is preserved for the next account/run.

---

## Skill Update → Data Freshness Check

Whenever a service skill (`skills/*.md` or `analyser/skills/*.py`) is updated, the following
rule applies:

### Rule

> **If a skill is updated, assess whether the cached analysis data for that service is still
> valid. If the skill change affects the logic used to detect opportunities (new checks, revised
> thresholds, corrected false-positive filters, changed severity levels), the existing cached
> data for that service is considered stale — even if it has not yet expired by TTL.**

### What to check

After updating a skill, compare:

| What | Where |
|---|---|
| Skill last modified | `skills/{service}.md` — check the `<!-- last-updated: YYYY-MM-DD -->` header, or the git commit date |
| Data last refreshed | `cache/{account_id}/{region}/{service}.json` → the most recent record's `collected_at` timestamp |

### Warning message

If the cache data for an affected service predates the skill update, emit the following warning
when presenting recommendations for that service/account:

```
⚠️  DATA FRESHNESS WARNING
    Service skill : {service}  (updated {skill_updated_date})
    Last data refresh : {collected_at}  (account {account_id}, {region})
    The skill has been updated since the last data collection for this account.
    Recommendations shown may not reflect the latest analysis logic.
    To refresh:  python analyser/main.py --services {service} --refresh-cache
```

### How to record the skill update date

Add or update a metadata comment at the top of every skill markdown file when editing it:

```markdown
<!-- last-updated: YYYY-MM-DD -->
<!-- change: <one-line summary of what changed> -->
```

Example:

```markdown
<!-- last-updated: 2026-03-25 -->
<!-- change: Added ecs-zombie-service check; revised cpu-overprovision threshold from 20% to 15% -->
```

### Scope

This check applies to **every account in scope**. If a skill is updated and account A was
refreshed yesterday but account B was last refreshed 3 weeks ago (before the skill update),
the warning must be shown for account B's data — not just the most recently analysed account.

---

## Versioning & Snapshots

Each run produces an immutable snapshot stored in SQLite with:
- A unique `run_id` (UUID)
- `captured_at` timestamp
- All opportunities found in that run

The dashboard compares snapshots over time, showing:
- Total estimated savings trend
- Opportunities resolved vs. new
- Per-service cost deltas

---

## Gitignore Recommendations

```gitignore
cache/
data/cost_analysis.db
output/
frontend/node_modules/
frontend/dist/
**/__pycache__/
*.pyc
.env
```

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `AWS_PROFILE` | — | AWS named profile (alternative to `--profile`) |
| `AWS_DEFAULT_REGION` | — | Fallback region |
| `ANALYSER_CACHE_DIR` | `./cache` | Override cache location |
| `ANALYSER_DB_PATH` | `./data/cost_analysis.db` | Override SQLite path |
| `ANALYSER_OUTPUT_DIR` | `./output` | Override CSV output location |
| `PRICING_CACHE_TTL_DAYS` | `7` | Override pricing cache TTL |

---

## Known API Quirks & Field-Validated Learnings

> This section is a running log of corrections and gotchas discovered during real account
> analysis runs. Update it whenever a new edge case is found.

### EC2 — Spot Instance Detection (updated 2026-03-25)

**Problem (original, 2026-03-23):** Using `tags.get("Lifecycle")` to detect Spot instances is unreliable. In practice,
the `Lifecycle` tag is often set to values like `"Managed by Terraform"` by IaC tooling, making
it useless for pricing detection.

**Problem (updated, 2026-03-25):** Using `InstanceLifecycle == "spot"` alone is also insufficient.
EKS managed node groups launch Spot instances via **EC2 Fleet**. For these instances,
`InstanceLifecycle` is `"fleet"` or absent — **not** `"spot"` — even though the instance is
running on Spot capacity. This caused 19/25 fleet-launched Spot instances in a dev account to
be misidentified as On-Demand.

**Fix:** Check both fields:

```python
is_spot = (instance.get("InstanceLifecycle", "") == "spot"
           or bool(instance.get("SpotInstanceRequestId")))
```

`SpotInstanceRequestId` is set by AWS for **all** Spot instances (direct requests and
Fleet-launched), and is never present on On-Demand instances. It is the most reliable
single indicator of Spot pricing.

---

### EC2 — Savings Plans in AWS Organisations (validated 2026-03-23)

**Problem:** `savingsplans.describe_savings_plans()` returns an empty list on org child accounts,
even when org-level Savings Plans are actively discounting ~99.9% of compute spend.
Additionally, `ce.get_savings_plans_utilization()` raises `DataUnavailableException` on child
accounts.

**Fix:** Use `ce.get_cost_and_usage(GroupBy=RECORD_TYPE)` and look for:
- `SavingsPlanCoveredUsage` → org SP is active. Calculate savings as `unblended − amortized`.
- `Usage` → on-demand spend not covered by SP — this is the gap to address.
- `ce.get_savings_plans_coverage()` for the coverage percentage.

**Real-world data (eu-central-1 child account):**
- `describe_savings_plans()` → 0 plans
- CE `SavingsPlanCoveredUsage` → $3,680 unblended / $1,793 amortized → **$1,887 MTD saving**
- CE `get_savings_plans_coverage()` → **99.9% coverage**, 51.3% effective discount
- CE `Usage` → **$4,060 uncovered spend MTD** (the gap to address at org level)

---

### EC2 — Spot Pricing for Cost Estimates (validated 2026-03-23)

**Problem:** Using On-Demand prices to estimate cost of Spot instances doubles the actual cost.

**Fix:** Fetch live Spot prices via `ec2.describe_spot_price_history(StartTime=now-6h)` for
all Spot instance types in use. Example discounts observed in eu-central-1:

| Instance type | OD price | Spot price | Saving |
|---|---|---|---|
| m5a.large | $0.104/hr | $0.057/hr | 45% off |
| t3a.large | $0.0752/hr | $0.039/hr | 48% off |
| t3.large | $0.0832/hr | $0.040/hr | 52% off |
| c5.large | $0.096/hr | $0.038/hr | 61% off |

Cache Spot prices with a short TTL (6 hours) — they fluctuate with market demand.

---

### General — Severity Level for Informational Findings

Added `ℹ️ INFO` severity level for findings that are positive confirmations (e.g., "org SP is
active and well-configured") rather than problems. INFO items are displayed in a separate panel
in the report and excluded from the total opportunity count and saving sum.

---

### General — Terminal Box Alignment with Emoji / Wide Characters

**Problem:** Python's f-string `:<N>` format spec counts *characters*, not terminal *display columns*. Emoji such as 💸 💰 🔍 ✅ are typically rendered as **2 display columns** by terminals, while Python counts them as 1. Variation-selector emoji (e.g. ⚠️ = U+26A0 + U+FE0F) also render as 2 columns but Python sees 2 "narrow" characters. This causes the right `║` border to drift out of alignment.

**Rule:** Never use `:<N>` trailing padding on any f-string line that contains emoji or other Unicode wide characters.

**Fix (permanent, v1.5.2+):** All terminal box drawing now uses `box_utils.py`:

```python
from box_utils import _bl, BORDER_TOP, BORDER_MID, BORDER_BOT

print(BORDER_TOP)
print(_bl("  My header"))
print(BORDER_MID)
print(_bl(f"  💸 Cost : ${cost:>8.2f} / month"))  # _bl handles alignment
print(_bl(f"  🔍 Opps : {n}"))
print(BORDER_BOT)
```

`_bl(content)` measures the **true display width** via `_dw()` (using `unicodedata.east_asian_width` + variation-selector detection), then pads with the exact number of trailing spaces so the right border always lands in the same column. Content that exceeds `BOX_W` (76 cols) is truncated with `…`.

**For future scripts:** Always use `_bl()` for any `║`-bordered content line — never hardcode `:<N>` padding. Build content freely and let `_bl` handle alignment.

---

## Cost Estimation Accuracy — Empirical Factors

**Rule:** Never use provisioned/nominal size as a proxy for actual billed consumption when the two can diverge significantly. Always apply an empirical factor and document it as a named constant.

### The Problem Pattern (v1.5.5 fix)

EBS snapshot savings (Opportunities 4 & 5) were calculated as:
```python
saving_mo = total_vol_gb * PRICE["snapshot"]   # ❌ 16–31× too high
```
`VolumeSize` from `describe_snapshots` is the **original volume size**, not the incremental storage actually billed. AWS snapshots store changed blocks only; actual usage is typically 5–15% of volume size.

### The Fix Pattern

```python
# Named constant with rationale — tune in skills/<service>.md if data changes
SNAPSHOT_INCREMENTAL_FACTOR = 0.10   # 10%: conservative upper bound (actual ~5–15%)

est_stored_gb = total_vol_gb * SNAPSHOT_INCREMENTAL_FACTOR
saving_mo     = est_stored_gb * PRICE["snapshot"]   # ✅ ~2× actual at worst
```

Mark estimated rows in CSV output:
```python
"saving_basis": "estimated"   # 'actual' | 'estimated' | 'upper_bound'
```

### Decision Table

| Metric used            | Service billed on          | Safe to use directly? |
|------------------------|----------------------------|-----------------------|
| EBS `VolumeSize`       | Provisioned GB             | ✅ Yes (volumes)      |
| EBS `VolumeSize`       | Incremental changed blocks | ❌ No (snapshots)     |
| RDS `AllocatedStorage` | Provisioned GB             | ✅ Yes                |
| ECR `imageSizeInBytes` | Actual stored bytes        | ✅ Yes                |
| S3 CloudWatch bytes    | Actual stored bytes        | ✅ Yes                |
| EFS CloudWatch bytes   | Actual stored bytes        | ✅ Yes                |

### Sanity Guard (generate_dashboard_data.py)

A per-row warning fires if any single opportunity's `saving_mo` exceeds 30% of the account's monthly invoice (`SINGLE_OPP_WARN_PCT = 0.30`). This catches estimation errors early — output goes to stderr so it does not pollute the JSON.

The 80% account-level savings cap (`CAP_PCT = 0.80`) remains as a secondary catch-all.

### For Future Analysers

Before shipping a `saving_mo` calculation:
1. Check: is the size metric **what AWS bills on** or just a proxy?
2. If it's a proxy, introduce an `*_FACTOR` constant with a comment explaining the rationale.
3. Set `"saving_basis": "estimated"` in the opportunity dict.
4. Document the factor in the relevant `skills/<service>.md`.

---

## Release Notes Policy

### Rule

> **Every patch, minor, or major version bump MUST produce a release notes file.**

Create `releases/RELEASE_NOTES_v{MAJOR}.{MINOR}.{PATCH}.md` before (or immediately after) bumping `REPORT_VERSION`. The file is committed alongside the version bump.

### Release Notes Template

```markdown
# Release Notes — v{X}.{Y}.{Z}

**Release date**: YYYY-MM-DD
**Type**: Patch | Minor | Major — <one-line summary>

---

## Summary

<2-4 sentence summary of what changed and why.>

---

## Bug Fixes

### <Bug Title>
- **File(s)**: `file.py`
- **Root cause**: ...
- **Fix**: ...
- **Impact**: ...

---

## New Features

### <Feature Title>
- Description
- Usage example if relevant

---

## Improvements

- Item 1
- Item 2

---

## Files Changed

| File | Change |
|---|---|
| `file.py` | Description |
```

### Location

```
releases/
  RELEASE_NOTES_v1.5.5.md
  RELEASE_NOTES_v1.6.0.md
  ...
```

The `releases/` directory IS committed to version control (unlike `accounts/`, `cache/`, `output/`).

---

## Multi-Region Pricing Cache

### Problem

All `analyse_*.py` modules previously used hardcoded `PRICE = {...}` dicts commented as "eu-central-1". Running analysis in other regions (e.g. `ap-northeast-1`) used incorrect prices silently.

### Solution

`pricing_cache.py` provides a centralised `load_prices(service, region)` function:

1. **First**: checks `cache/pricing/{region}/{service}.json` for a fresh (< 7 day) cached price set
2. **Fallback**: returns hardcoded eu-central-1 defaults with a `⚠️ PRICING WARNING` stderr message

### Updating the Pricing Cache

```bash
# Fetch prices for a single region
python3 update_pricing.py --region eu-central-1

# Fetch for your analysis region (always do this after first setup)
python3 update_pricing.py --region ap-northeast-1 --services ebs s3

# Refresh all regions (background job, takes ~5 min)
python3 update_pricing.py --all-regions

# Show what's cached and when it expires
python3 update_pricing.py --list-cached

# Force-refresh even if cache is still fresh
python3 update_pricing.py --region eu-central-1 --force
```

### Pricing Cache TTL

Pricing cache files expire after **7 days** (prices rarely change intra-month). Service analysis cache TTL remains **30 days**.

### Adding a New Service to Pricing Cache

1. Add a `_FALLBACKS["myservice"]` dict in `pricing_cache.py` with eu-central-1 defaults.
2. Add a `fetch_myservice_prices()` function in `update_pricing.py` using `pricing.get_products(ServiceCode="...")`.
3. Register it in `FETCHERS` dict in `update_pricing.py`.
4. Call `PRICE = load_prices("myservice", REGION)` at the top of `analyse_myservice.py`.

### Rule for All New Analysers

> **Never hardcode a `PRICE = {...}` dict in `analyse_*.py`.** Always use `from pricing_cache import load_prices` and call `PRICE = load_prices("service", REGION)` at module level.

---

## Cache Controls

Three new runtime controls are available in `run_analysis.py` and `refresh_all.py`:

| Flag | Effect |
|---|---|
| `--force-refresh` | Ignores all caches; fetches live AWS data. Sets `AWS_COST_ANALYSER_FORCE_REFRESH=1` env var. |
| `--cache-ttl-days N` | New cache records expire after N days instead of the default 30. |
| `--clear-cache [service...]` | Marks the latest cache record for each service as expired (historical data preserved). |

Examples:
```bash
# Ignore all caches for this run
python3 run_analysis.py --profile my-profile --force-refresh

# Re-run with a 7-day TTL (cache refreshes more often)
python3 run_analysis.py --profile my-profile --cache-ttl-days 7

# Expire S3 and EC2 caches so next run fetches fresh data
python3 run_analysis.py --profile my-profile --clear-cache s3 ec2

# Expire all service caches
python3 run_analysis.py --profile my-profile --clear-cache
```

The `--force-refresh` and `--cache-ttl-days` flags are also supported in `refresh_all.py`:
```bash
python3 refresh_all.py --force-refresh
python3 refresh_all.py --cache-ttl-days 7 --group my-app
```

---

## Global Services Analysis

### Scope

Some AWS services are **globally-scoped** (no region): Route 53, CloudFront, IAM, WAF (global). Others are **regionally billed but globally visible**: S3 (each bucket has a region but the service API is global).

### `analyse_global.py`

Dedicated analyser for global services. Registered as service slug `"global"`.

**Checks performed:**
- **Route 53**: empty hosted zones (SOA/NS only), disabled health checks, public zones without any health checks
- **CloudFront**: distributions without compression, without WAF, using HTTP/1.1
- **S3 cross-region**: CE cost breakdown by region (informational — shows which region drives S3 cost)

**When to run:**
- Include `global` in `--services` for any profile/region: `--services global billing`
- Or run all services (default) — `analyse_global.py` is in `ALL_SERVICES`
- Route 53 and CloudFront are truly global, so one run is sufficient (results are identical from any region)
- For a multi-region org, run once from `us-east-1` per account for the global check

### S3 Special Case

S3 appears in both `analyse_s3.py` (per-region, per-bucket analysis) and `analyse_global.py` (cross-region cost aggregation via Cost Explorer). These are complementary:

| Analyser | Scope | What it checks |
|---|---|---|
| `analyse_s3.py` | Per-region | Lifecycle policies, versioning, storage class, Intelligent-Tiering |
| `analyse_global.py` | Cross-region | Which region drives the most S3 cost (informational) |

---

## Dashboard Region Filter

The dashboard header now includes a **Region dropdown** (`All Regions` + all AWS regions) alongside the existing Group and Environment filters.

- Selecting a region filters the account cards and all charts to show only accounts analysed in that region
- The `region` field is populated from the most recently analysed region per account (read from the billing cache path)
- `All Regions` (default) shows all accounts regardless of region

### Adding Region to New Analysers

Every `analyse_*.py` script must emit `region` in its CSV rows so it flows through to `generate_dashboard_data.py`. The standard boilerplate is:

```python
REGION = os.environ.get("AWS_DEFAULT_REGION", "eu-central-1")
# ...
# In CSV writer:
writer.writerow({
    "account_id": ACCOUNT_ID,
    "region":     REGION,
    ...
})
```

---

## EC2 Instance Families — Current Reference

### Graviton (ARM64) — Safe migration, no recompile for managed services

| Generation | Families | vs. Previous | Notes |
|---|---|---|---|
| Graviton2 | `m6g`, `c6g`, `r6g` | ~40% cheaper than m5/c5/r5 | Older; prefer G3/G4 for new deployments |
| Graviton3 | `m7g`, `c7g`, `r7g` | ~20% cheaper than G2 | Widely available |
| **Graviton4** | **`m8g`, `c8g`, `r8g`** | **~30% better than G3** | **Recommended target (2024)** |

**Application requirement**: All container images must be built for `linux/arm64`. This is a `DEPENDENCY` severity recommendation.

### x86/AMD — Safe same-arch migrations (no recompile)

| Source family | Recommended upgrade | Saving |
|---|---|---|
| `m5`, `m5a`, `m6i` | `m7a` (AMD EPYC Genoa) | ~12% vs m6a |
| `c5`, `c5a`, `c6i` | `c7a` (AMD EPYC Genoa) | ~15% |
| `r5`, `r5a`, `r6i` | `r7a` (AMD EPYC Genoa) | ~12% |
| `m7i`, `c7i` | `m7i-flex` / `c7i-flex` | Lightly-utilised only (<30% avg CPU) |

### Migration Mapping (GRAVITON_MAP in analyse_ec2.py)

```
m5/m5a/m6i/m6a → m8g (Graviton4)   m7g/m6g → m8g
c5/c5a/c6i/c6a → c8g (Graviton4)   c7g/c6g → c8g
r5/r5a/r6i     → r8g (Graviton4)   r7g/r6g → r8g
t3/t3a         → t4g (Graviton2)
```

### Recommending ARM64 — Dependency Warning Rule

> **Any recommendation to migrate to Graviton (ARM64) MUST carry `severity = DEPENDENCY` and include the warning**: "All container images must be built for linux/arm64 — requires CI/CD pipeline changes."


---

## Expiring Cache by Date Range (`expire_cache_range.py`)

### Problem

Standard `--clear-cache` only marks the **latest** record as expired. When a bug causes incorrect data collection over a multi-week period, you need to invalidate **all records within that window** without losing the records outside it (before the bug started or after it was fixed).

### Solution

`expire_cache_range.py` scans the entire `cache/` tree and marks any record whose `collected_at` falls within the given range as expired — without deleting historical data.

### Usage

```bash
# Expire ALL cache records collected between Apr 11 and May 29 (all accounts, all services)
python3 expire_cache_range.py --start 2026-04-11 --end 2026-05-29

# Dry-run first — shows what WOULD be expired without touching files
python3 expire_cache_range.py --start 2026-04-11 --end 2026-05-29 --dry-run

# Inspect: list all records in the range with their current expiry status
python3 expire_cache_range.py --start 2026-04-11 --end 2026-05-29 --list-only

# Limit to specific accounts
python3 expire_cache_range.py --start 2026-04-11 --end 2026-05-29 \
    --accounts 123456789012 234567890123

# Limit to specific region and services
python3 expire_cache_range.py --start 2026-04-11 --end 2026-05-29 \
    --region eu-central-1 --services ebs ec2

# Expire and immediately rebuild dashboard data
python3 expire_cache_range.py --start 2026-04-11 --end 2026-05-29 \
    --rebuild-dashboard

# Full remediation flow for a known-bad period:
# 1. Inspect what's in the range
python3 expire_cache_range.py --start 2026-04-11 --end 2026-05-29 --list-only

# 2. Dry-run to confirm scope
python3 expire_cache_range.py --start 2026-04-11 --end 2026-05-29 --dry-run

# 3. Apply expiry
python3 expire_cache_range.py --start 2026-04-11 --end 2026-05-29

# 4. Re-collect fresh data (parallel refresh)
python3 refresh_all.py --force-refresh

# 5. Rebuild dashboard
python3 generate_dashboard_data.py
```

### How It Works

- Scans `cache/{account_id}/{region}/{service}.json` for all records
- For each record where `start <= collected_at <= end`, sets `expires_at = start − 1 second`
- The record remains in the file (audit trail preserved)
- The next analysis run finds these records expired and fetches fresh data from AWS

### Filtering Options

| Flag | Default | Description |
|---|---|---|
| `--start` | required | Start of bad period (inclusive) `YYYY-MM-DD` |
| `--end` | required | End of bad period (inclusive) `YYYY-MM-DD` |
| `--accounts` | all | AWS account IDs to filter (space-separated) |
| `--region` | all | AWS regions to filter (space-separated) |
| `--services` | all | Service slugs to filter (e.g. `ebs ec2 s3`) |
| `--dry-run` | off | Preview changes without modifying files |
| `--list-only` | off | Inspect records in range without any changes |
| `--rebuild-dashboard` | off | Rebuild dashboard_data.json + HTML after expiry |

### When to Use

| Scenario | Command |
|---|---|
| Bug caused double-aggregation for 6 weeks | `--start ... --end ... --dry-run` then without `--dry-run` |
| Wrong region used for 2 accounts | `--accounts 111 222 --region wrong-region ...` |
| One service analyser had a calculation error | `--services ebs --start ... --end ...` |
| Verify scope before acting | `--list-only` |

