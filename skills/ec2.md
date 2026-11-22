<!-- last-updated: 2026-03-25 -->
<!-- change: Fixed Spot detection for EC2 Fleet-launched instances (EKS managed node groups); added SpotInstanceRequestId as authoritative fallback alongside InstanceLifecycle -->

# Skill: Amazon EC2 — Cost-Saving Opportunities

## Service Overview

Amazon EC2 is typically the largest cost driver in an AWS account. Cost savings come from four
distinct angles: **commitment discounts**, **right-sizing**, **architecture modernisation**, and
**eliminating waste**.

---

## Real-World Learnings (from live account runs)

> These notes capture corrections and improvements discovered during actual account analysis.
> They take precedence over any original assumptions below.

### ✅ Spot Instance Detection — Use InstanceLifecycle AND SpotInstanceRequestId

**Do NOT** use `tags.get("Lifecycle")` or any other tag to determine if an instance is Spot.
The tag value varies by team convention (e.g. `"Managed by Terraform"` is a common `Lifecycle`
tag value that has nothing to do with instance pricing).

There are **two authoritative AWS fields** to check, and both are needed:

| Field | When it is set | Notes |
|---|---|---|
| `InstanceLifecycle = "spot"` | Direct Spot requests | Always reliable for directly-requested Spot instances |
| `InstanceLifecycle = "fleet"` | EC2 Fleet instances | Could be Spot OR On-Demand capacity within the fleet |
| `SpotInstanceRequestId` | Any Spot instance | Set by AWS for **all** Spot instances, including Fleet-launched ones |

**Correct detection logic:**

```python
# InstanceLifecycle = "spot"  → direct Spot request
# InstanceLifecycle = "fleet" → EC2 Fleet; Spot status determined by SpotInstanceRequestId
# SpotInstanceRequestId set   → authoritative: AWS only populates this for Spot instances
is_spot = (instance.get("InstanceLifecycle", "") == "spot"
           or bool(instance.get("SpotInstanceRequestId")))
```

**Why `SpotInstanceRequestId` matters for EKS:**
EKS managed node groups with Spot capacity launch instances via **EC2 Fleet**. In this case,
`InstanceLifecycle` is `"fleet"` or may be absent entirely — **not** `"spot"` — even though
the instance IS running on Spot. The `SpotInstanceRequestId` field is always populated by AWS
for any Spot instance, regardless of how it was launched.

**Impact discovered (live production account, 2026-03-25):** 19/25 EKS fleet-launched Spot instances
had `InstanceLifecycle` absent. Checking only `InstanceLifecycle == "spot"` misidentified them
as On-Demand, generating 19 false "migrate to Spot" recommendations and overstating cost by ~3×.

---

### ✅ Savings Plans in AWS Organisations — Use Cost Explorer, NOT savingsplans API

In accounts that are **members of an AWS Organisation**, Savings Plans are typically purchased
at the **management account** level and shared to child accounts. The child account's
`savingsplans.describe_savings_plans()` returns **an empty list**, even when SP benefits
are actively being applied.

**The correct detection approach is Cost Explorer (CE):**

```python
ce = session.client("ce", region_name="us-east-1")

# 1. Check RECORD_TYPE breakdown for SavingsPlanCoveredUsage / SavingsPlanNegation
ce.get_cost_and_usage(
    TimePeriod={"Start": month_start, "End": today},
    Granularity="MONTHLY",
    Metrics=["UnblendedCost", "AmortizedCost"],
    GroupBy=[{"Type": "DIMENSION", "Key": "RECORD_TYPE"}],
)
# Key record types to look for:
#   SavingsPlanCoveredUsage  → compute billed at on-demand rate before SP discount
#   SavingsPlanNegation      → negative amount = the SP discount applied
#   Usage                    → on-demand spend NOT covered by any SP

# 2. Get coverage percentage
ce.get_savings_plans_coverage(
    TimePeriod={"Start": month_start, "End": today},
    Granularity="MONTHLY",
)
# Returns CoveragePercentage, OnDemandCost (uncovered), SpendCoveredBySavingsPlans
```

**How to calculate the actual SP saving from CE data:**
```python
od_equiv   = record_types["SavingsPlanCoveredUsage"]["unblended"]  # what it would cost at OD
amort_cost = record_types["SavingsPlanCoveredUsage"]["amortized"]  # what it costs with SP
sp_savings = od_equiv - amort_cost
effective_discount_pct = sp_savings / od_equiv * 100
# e.g.  $3,680 OD equiv - $1,793 amortized = $1,887 saved (51.3% discount)
```

> ⚠️ `savingsplans.get_savings_plans_utilization()` raises `DataUnavailableException` on child
> accounts — do not call it. Use CE record types instead.

**Impact:** A real child account showed `describe_savings_plans()` returning 0 plans, while CE
showed 99.9% SP coverage and $1,887/month in savings being applied from the org SP.

---

### ✅ Use Live Spot Prices — Not On-Demand Rates for Cost Estimates

For instances where `is_spot = True`, always fetch the **current Spot market price** rather
than using On-Demand pricing. Spot is typically 55–75% cheaper than On-Demand.

```python
ec2.describe_spot_price_history(
    InstanceTypes=[itype],
    ProductDescriptions=["Linux/UNIX"],
    StartTime=now - timedelta(hours=6),  # get most recent price
)
# Example savings vs OD (eu-central-1):
#   m5a.large  Spot: $0.0568/hr  OD: $0.104/hr  → 45% of OD price
#   t3a.large  Spot: $0.0387/hr  OD: $0.0752/hr → 51% of OD price
#   t3.large   Spot: $0.0401/hr  OD: $0.0832/hr → 48% of OD price
#   c5.large   Spot: $0.0375/hr  OD: $0.096/hr  → 39% of OD price
```

---

### ✅ Graviton Savings Apply to Spot Instances Too

When an instance is already Spot, the Graviton saving is calculated proportionally:

```python
if is_spot and od_price > 0:
    spot_discount = 1 - (spot_price / od_price)         # e.g. 45% off
    graviton_spot_est = graviton_od_price * (1 - spot_discount)  # apply same % off
    saving = (spot_price - graviton_spot_est) * 730
else:
    saving = (od_price - graviton_od_price) * 730
```

Label Spot-adjusted savings as **"Spot-adjusted estimate"** in the recommendation — the actual
Graviton Spot market price fluctuates and is not directly queryable without the exact instance
type (which would change post-migration).

---

## Data Collection

### AWS API Calls

```python
ec2 = session.client("ec2", region_name=region)
ce  = session.client("ce",  region_name="us-east-1")   # CE is always us-east-1

# All running AND stopped instances
ec2.describe_instances(Filters=[
    {"Name": "instance-state-name", "Values": ["running", "stopped"]}
])

# CORRECT Spot detection: read InstanceLifecycle from describe_instances response
# instance["InstanceLifecycle"] == "spot"  → Spot
# instance["InstanceLifecycle"] absent     → On-Demand

# Live Spot prices for all Spot instance types in use
spot_types = {i["InstanceType"] for i in instances if i.get("InstanceLifecycle") == "spot"}
ec2.describe_spot_price_history(
    InstanceTypes=list(spot_types),
    ProductDescriptions=["Linux/UNIX"],
    StartTime=now - timedelta(hours=6),
)

# Reserved Instances (active only)
ec2.describe_reserved_instances(Filters=[{"Name": "state", "Values": ["active"]}])

# Org-level Savings Plans via Cost Explorer (NOT savingsplans.describe_savings_plans)
month_start = now.replace(day=1).strftime("%Y-%m-%d")
today       = now.strftime("%Y-%m-%d")
ce.get_cost_and_usage(
    TimePeriod={"Start": month_start, "End": today},
    Granularity="MONTHLY",
    Metrics=["UnblendedCost", "AmortizedCost"],
    GroupBy=[{"Type": "DIMENSION", "Key": "RECORD_TYPE"}],
)
ce.get_savings_plans_coverage(
    TimePeriod={"Start": month_start, "End": today},
    Granularity="MONTHLY",
)

# CloudWatch: CPU utilisation per instance (last 15 days, 1-hour resolution)
cloudwatch.get_metric_statistics(
    Namespace="AWS/EC2",
    MetricName="CPUUtilization",
    Dimensions=[{"Name": "InstanceId", "Value": instance_id}],
    StartTime=now - timedelta(days=15),
    EndTime=now,
    Period=3600,
    Statistics=["Average", "Maximum"]
)
# Derive p95 from hourly averages (sort + index at 95th percentile)
```

### Enriched Instance Record

Each instance object should carry:

```python
{
    "id":          instance_id,
    "type":        instance_type,
    "state":       "running" | "stopped",
    "is_spot":     bool,           # from InstanceLifecycle == "spot"
    "price_hr":    float,          # Spot price if is_spot, else OD price
    "od_price_hr": float,          # always the OD rate (for saving calculations)
    "age_days":    int,            # days since launch
    "eks_cluster": str,            # from tag aws:eks:cluster-name (or "")
    "env":         str,            # from Environment tag
    "cpu_avg":     float | None,
    "cpu_max":     float | None,
    "cpu_p95":     float | None,
}
```

### Cache TTL
- Instance metadata + Spot prices: **24 hours**
- CloudWatch CPU metrics: **6 hours**
- CE Savings Plan data: **6 hours** (changes intra-day)
- Pricing data: **7 days**

---

## Opportunity 1 — Savings Plan: Coverage Check (Org-Aware)

**Severity:** 🔴 CRITICAL (no coverage) | 🟠 HIGH (gap) | ℹ️ INFO (well-covered)
**Opportunity type:** `compute-savings-plan` | `savings-plan-gap` | `savings-plan-active`

**Detection logic (three distinct outcomes):**

### Case A — No SP coverage at all
- CE `RECORD_TYPE` breakdown contains **no** `SavingsPlanCoveredUsage` or `SavingsPlanNegation`.
- `savingsplans.describe_savings_plans()` also returns empty.
- → Severity: 🔴 CRITICAL. Flag all On-Demand instances.

### Case B — Org SP active, but gap in coverage
- CE shows `SavingsPlanCoveredUsage` AND a non-trivial `Usage` record type (uncovered OD spend).
- Coverage percentage < 95%.
- → Severity: 🟠 HIGH. Flag the uncovered OD spend amount.
- Recommend escalating to the org management account to review SP commitment size.

### Case C — Org SP active, coverage ≥ 95%
- CE shows `SavingsPlanNegation` and `coverage_pct ≥ 95%`.
- → Severity: ℹ️ INFO. Show as a positive acknowledgement, not a problem.
- Report: coverage %, effective discount %, MTD savings, annualised savings estimate.

> **Important:** `savingsplans.describe_savings_plans()` only returns SP *owned by this account*.
> Org-level SP from the management account is **invisible** to this API on child accounts.
> Always use Cost Explorer as the authoritative source.

**Calculation:**
```python
od_equiv        = record_types["SavingsPlanCoveredUsage"]["unblended"]  # OD-rate equivalent
amort_cost      = record_types["SavingsPlanCoveredUsage"]["amortized"]  # effective SP cost
sp_savings_mtd  = od_equiv - amort_cost
sp_savings_mo   = sp_savings_mtd / days_elapsed * 30       # extrapolate to full month
discount_pct    = sp_savings_mtd / od_equiv * 100
uncovered_od    = record_types.get("Usage", {}).get("unblended", 0.0)
gap_saving_mo   = uncovered_od / days_elapsed * 30 * 0.38  # 38% if SP were extended
```

**Recommendation (Case B — gap):**
> "Org-level Savings Plans cover {coverage_pct:.1f}% of eligible compute. ${uncovered_od:.2f} of
> on-demand spend (MTD) remains uncovered. Increasing the org SP commitment could save a further
> ~${gap_saving_mo:.2f}/month. Escalate to the AWS Organizations management account."

**Recommendation (Case C — active, well-covered):**
> "Org-level Savings Plans are active. Coverage: {coverage_pct:.1f}%. Effective discount:
> {discount_pct:.1f}% on covered usage. SP savings applied MTD: ${sp_savings_mtd:.2f}
> (est. ${sp_savings_mo:.0f}/month, ${sp_savings_mo*12:.0f}/year)."

---

## Opportunity 2 — Unmatched On-Demand Instances (Reserved Instance Gap)

**Severity:** 🔴 CRITICAL (> 90 days OD) | 🟠 HIGH (30–90 days OD)
**Opportunity type:** `reserved-instance`

**Detection logic:**
- **Only applies to On-Demand instances** (`is_spot = False`). Spot instances are already
  discounted and cannot be matched to RIs — skip them entirely.
- Instance has been running for > 30 days (infer from `LaunchTime`).
- No matching active RI found by `(instance_type, scope/region)`.
- If org SP coverage is ≥ 95%: note that RI is an alternative to SP, not additive.
  Recommend checking SP commitment expansion rather than purchasing RIs.

**Calculation:**
```
on_demand_hourly = pricing[instance_type]["on_demand"]
ri_1yr_hourly    = pricing[instance_type]["reserved_1yr_no_upfront"]
monthly_saving   = (on_demand_hourly - ri_1yr_hourly) * 730
saving_pct       = (on_demand_hourly - ri_1yr_hourly) / on_demand_hourly * 100
```

**Recommendation:**
> "Instance {id} ({type}) has been running On-Demand for {days} days. Purchasing a 1-year
> no-upfront Reserved Instance would save approximately ${saving}/month ({pct}%)."

---

## Opportunity 3 — Current Gen AMD64: Move to Newer Families

**Severity:** 🟠 HIGH  
**Opportunity type:** `instance-family-upgrade`

**Detection logic:**
Instances running on **previous-generation families** that have a clearly better price/performance
successor:

| Current family | Recommended family | Typical saving |
|---|---|---|
| m4, m5 | m7i / m7a | 10–20% |
| c4, c5 | c7i / c7a | 15–25% |
| r4, r5 | r7i / r7a | 10–20% |
| t2 | t3 / t4g | 10–20% |
| i3 | i4i | 15–30% |

Flag any instance whose family prefix is in the "Current family" list above.

**Calculation:**
```
current_hourly    = pricing[current_instance_type]["on_demand"]
recommended_type  = map_to_recommended(current_instance_type)
recommended_hourly= pricing[recommended_type]["on_demand"]
monthly_saving    = (current_hourly - recommended_hourly) * 730
```

**Recommendation:**
> "Instance {id} is running on {type}. Migrating to {recommended_type} offers similar or better
> performance at ${saving}/month less. This is a drop-in replacement for most workloads."

---

## Opportunity 4 — ARM64 / Graviton Migration

**Severity:** ⚠️ DEPENDENCY  
**Opportunity type:** `graviton-migration`

**Detection logic:**
- Any x86_64 instance type that has a direct Graviton equivalent (e.g., `m5` → `m7g`, `c5` → `c7g`, `r5` → `r7g`).
- Graviton3 instances offer **up to 40% better price/performance** than comparable x86 instances.

> ⚠️ **Dependency warning:** Migrating to ARM64/Graviton requires the application binary to be
> compiled for `arm64`. Container images must be rebuilt as `linux/arm64` or `linux/arm64/v8`.
> Validate all dependencies (native extensions, third-party libraries) support ARM64 before migrating.

**Graviton mapping table:**

| x86 family | Graviton equivalent | Saving (approx.) |
|---|---|---|
| m5 / m6i | m7g | 20–40% |
| c5 / c6i | c7g | 20–40% |
| r5 / r6i | r7g | 20–40% |
| t3 / t3a | t4g | 20–40% |
| x1e | x2gd | 30–40% |

**Recommendation:**
> "Instance {id} ({type}) could migrate to {graviton_type} saving ~${saving}/month (~{pct}%).
> ⚠️ Application must be recompiled or Docker images rebuilt for linux/arm64."

---

## Opportunity 5 — Oversized / Under-utilised Instances

**Severity:** 🟠 HIGH  
**Opportunity type:** `right-sizing`

**Detection logic:**
Using CloudWatch CPU Average over the last 15 days:
- **< 10% average CPU** → suggest downsizing 1 size (e.g., `xlarge` → `large`)
- **< 5% average CPU** → suggest downsizing 2 sizes or moving to Burstable (`t3`/`t4g`)
- **Maximum CPU < 50%** over 15 days → also flag

For memory-intensive workloads (r-family): cross-reference with CloudWatch Memory (if CloudWatch
agent is installed) or flag as "verify memory usage" before downsizing.

**Calculation:**
```
downsize_type    = one_size_smaller(current_type)
current_hourly   = pricing[current_type]["on_demand"]
downsize_hourly  = pricing[downsize_type]["on_demand"]
monthly_saving   = (current_hourly - downsize_hourly) * 730
```

**Recommendation:**
> "Instance {id} ({type}) has had average CPU of {avg_cpu}% and max CPU of {max_cpu}% over the
> last 15 days. Consider downsizing to {downsize_type}, saving ~${saving}/month."

---

## Opportunity 6 — Stopped Instances Still Incurring Charges

**Severity:** 🟡 MEDIUM  
**Opportunity type:** `stopped-instance`

**Detection logic:**
- Instances in `stopped` state with EBS volumes still attached.
- Stopped instances don't incur compute charges, but attached EBS volumes do.
- Flag instance if stopped for **> 7 days**.

**Recommendation:**
> "Instance {id} has been stopped since {date}. EBS volume {vol_id} ({size} GB {type}) is still
> incurring ${ebs_cost}/month. Consider terminating and snapshotting if no longer needed."

---

## Opportunity 7 — Spot Instance Candidates (On-Demand Non-Production Only)

**Severity:** 🟢 LOW  
**Opportunity type:** `spot-instance`

**Detection logic:**
- **Only applies to On-Demand instances** (`is_spot = False`). Never recommend Spot for an
  instance that is already Spot — always check `InstanceLifecycle` AWS field first.
- Instance has `Environment` tag = `dev`, `test`, `staging`, `qa`, or `uat`.
- Instance is not part of an EKS Managed Node Group (EKS node groups have their own Spot
  configuration; flag at the node group level via the EKS skill instead).
- Stateless or fault-tolerant workloads are candidates for Spot pricing (savings of 60–80%).

**Correct Spot check:**
```python
# CORRECT — use AWS metadata
is_spot = instance.get("InstanceLifecycle", "") == "spot"

# WRONG — tags are unreliable
is_spot = instance["tags"].get("Lifecycle", "").lower() == "spot"
```

**Recommendation:**
> "Instance {id} ({type}, env='{env}') is On-Demand. Using Spot for this non-production workload
> can save ~70% (~${saving}/month). Ensure the workload tolerates interruption."

---

## Opportunity 8 — EKS Worker Node Scheduled Overnight Scale-Down (Non-Production)

**Severity:** 🟠 HIGH  
**Opportunity type:** `eks-scheduled-scale-down`

**Rationale:** Non-production EKS worker nodes (dev, qa, stg, acc, etc.) are almost never
needed between **20:00 and 05:00 UTC** (9 hours/night). Scaling node groups to 0 or to a
minimal warm-standby size overnight saves ~37% of monthly compute cost with zero risk to
production workloads.

> ⛔ **Never flag production environments** (env tags: `prod`, `production`, `prd`). Only apply
> to environments that explicitly signal non-production (`dev`, `development`, `qa`, `test`,
> `testing`, `stg`, `staging`, `acc`, `acceptance`, `uat`).

**Detection logic:**

```python
NON_PROD_ENVS = {"dev", "development", "qa", "test", "testing",
                 "stg", "staging", "acc", "acceptance", "uat"}
PROD_ENVS     = {"prod", "production", "prd"}

def is_eks_worker(instance):
    tags = {t["Key"]: t["Value"] for t in instance.get("Tags", [])}
    return "aws:eks:cluster-name" in tags

def get_env(instance):
    tags = {t["Key"]: t["Value"] for t in instance.get("Tags", [])}
    return tags.get("Environment", tags.get("environment", tags.get("env", ""))).lower()

def has_scheduled_scaling(instance, autoscaling_client):
    """Check if the underlying ASG already has a scheduled scale-down action."""
    tags    = {t["Key"]: t["Value"] for t in instance.get("Tags", [])}
    asg_name = tags.get("aws:autoscaling:groupName", "")
    if not asg_name:
        return False
    actions = autoscaling_client.describe_scheduled_actions(
        AutoScalingGroupName=asg_name
    ).get("ScheduledUpdateGroupActions", [])
    # Check for any action that sets desired/min to 0 between 20:00 and 05:00
    for a in actions:
        recurrence = a.get("Recurrence", "")
        desired    = a.get("DesiredCapacity", -1)
        if desired == 0 and recurrence:
            return True
    return False

# Flag candidate instances
for instance in running_instances:
    env = get_env(instance)
    if not is_eks_worker(instance):
        continue
    if env in PROD_ENVS or env not in NON_PROD_ENVS:
        continue                                    # skip prod or untagged
    if has_scheduled_scaling(instance, asg_client):
        continue                                    # already configured
    # → Flag this instance as a scale-down candidate
```

**Calculation:**

```python
# 9 hrs/night × 30 days = 270 hrs/month scaled down (out of 730 total)
SCALE_DOWN_HOURS_PER_MONTH = 9 * 30          # = 270
TOTAL_HOURS_PER_MONTH      = 730
SCALE_DOWN_FRACTION        = SCALE_DOWN_HOURS_PER_MONTH / TOTAL_HOURS_PER_MONTH  # ≈ 0.370

# Per instance saving
hourly_rate   = spot_price_hr if is_spot else od_price_hr
monthly_cost  = hourly_rate * TOTAL_HOURS_PER_MONTH
monthly_saving = hourly_rate * SCALE_DOWN_HOURS_PER_MONTH  # ~37% of monthly cost

# Aggregate by EKS cluster (all nodes in the same non-prod node group)
cluster_saving = sum(monthly_saving for node in cluster_nodes if qualifies)
```

**Implementation approach (for the recommendation text):**

AWS Auto Scaling **Scheduled Actions** on the underlying Auto Scaling Group:

```
# Scale down at 20:00 UTC — set desired to 0 (or 1 for warm standby)
aws autoscaling put-scheduled-update-group-action \
  --auto-scaling-group-name <ASG_NAME> \
  --scheduled-action-name scale-down-overnight \
  --recurrence "0 20 * * *" \
  --desired-capacity 0 --min-size 0

# Scale up at 05:00 UTC — restore original desired count
aws autoscaling put-scheduled-update-group-action \
  --auto-scaling-group-name <ASG_NAME> \
  --scheduled-action-name scale-up-morning \
  --recurrence "0 5 * * *" \
  --desired-capacity <ORIGINAL_DESIRED> --min-size <ORIGINAL_MIN>
```

Alternatively, use an **EventBridge Scheduler** rule + Lambda to call
`update_nodegroup_config` on the EKS node group directly (preferred for managed node groups).

**Recommendation template:**

> "EKS worker node {id} ({instance_type}, env='{env}', cluster='{cluster}') has no overnight
> scale-down schedule. Scaling this node group to 0 between 20:00–05:00 UTC (9 hrs/night) would
> save ~${monthly_saving:.2f}/month (~37% of compute cost). Implement via Auto Scaling Scheduled
> Actions or EventBridge + Lambda on the EKS node group. See EKS skill for full implementation
> guidance."

---

## Pricing Sources

- **On-Demand:** `pricing.get_products(ServiceCode="AmazonEC2", ...)` — or use a hardcoded
  lookup table seeded from the AWS pricing page per region (faster, no API call needed for
  the most common instance types).
- **Reserved (1yr, no-upfront):** `ec2.describe_reserved_instances_offerings(...)`
- **Savings Plans:** Use CE `SavingsPlanCoveredUsage` amortized vs unblended (not the
  `savingsplans` API — unreliable in org child accounts).
- **Spot (current market):** `ec2.describe_spot_price_history(StartTime=now-6h)` — always
  fetch fresh for Spot instances; do not use cached OD prices as a proxy.

---

## Notes

- Always sort opportunities by `estimated_saving_usd_monthly` descending.
- Tag-based filtering: skip instances with tag `cost-analyser:ignore = true`.
- Graviton and family-upgrade opportunities should appear together on the same resource if both
  apply, with the higher-saving one listed first.
- **EKS worker nodes:** identified by tag `aws:eks:cluster-name`. Graviton migration for EKS
  nodes requires creating a new node group with `amiType: AL2_ARM_64` — do not suggest a simple
  stop/start. Reference the EKS skill for full node group migration guidance.
- **Spot + Graviton:** Always show Graviton migration opportunities for Spot instances too — the
  saving is proportionally the same or better. Label as "Spot-adjusted estimate" since the
  actual Graviton Spot price is not queried directly.
- **Cost accuracy:** For accounts with org Savings Plans, the effective per-instance cost is
  lower than On-Demand. Report both the On-Demand equivalent rate (for comparison) and the
  effective Spot/amortized rate (for accurate current cost). Never mix the two in saving
  calculations.
