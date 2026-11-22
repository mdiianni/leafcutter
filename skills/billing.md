# Skill: AWS Billing & Cost Explorer — Cost Ground Truth + Service Discovery

## Service Overview

AWS Billing / Cost Explorer is the **authoritative source of truth** for all actual costs
in an AWS account. Analyser scripts that collect per-resource metrics (ECS, EC2, ECR, etc.)
derive **on-demand equivalent estimates** which may differ substantially from what is actually
billed, due to:

- **Org Savings Plans** — negate large chunks of compute cost (visible only as `SavingsPlanNegation`)
- **Reserved Instances** — upfront/partial commitments change per-instance effective rates
- **EDP / Enterprise Discounts** — org-wide pricing agreements
- **Per-opportunity double-counting** — multiple CSV rows for the same resource each carrying full cost
- **Missing services** — our per-service analysers may not cover every billed service

> ⚠️ **CRITICAL RULE:** Always use Cost Explorer as the **ground truth for costs** in overall
> summaries and trend reports. Per-analyser CSV `current_cost_usd_monthly` values are useful
> for **relative opportunity sizing only** — never sum them to produce a total account cost.

---

## Real-World Learnings (from live account runs)

### ✅ CE vs CSV Discrepancy — Root Causes Identified

In a real STG account, the CSV-aggregated total was **$23,817/month** while CE reported
**$10,878.93 actual** (last full month, unblended). Key causes:

| Cause | Example | Inflation |
|-------|---------|-----------|
| **SP negation not applied** | ECS: $12,667 CSV vs $54 unblended actual | 235× |
| **Multi-opportunity per resource** | EC2: 4 instances had 2–3 rows each | 2–3× |
| **Per-repo cost + account total** | ECR: $865 account + per-repo sub-totals = $1,936 vs $65 actual | 30× |
| **OD-rate used not SP-adjusted rate** | ECS compute billed at SP rate, not OD | ~67% off |

### ✅ "EC2 - Other" Is Mostly EBS + Cross-AZ Traffic

CE groups EBS volumes, snapshots, throughput provisioning, and cross-AZ data transfer
all under `"EC2 - Other"`. In a real account this line was **$2,431/month**:

| Usage type | Cost |
|---|---|
| EBS gp3 Throughput provisioning | $1,211 |
| EBS gp3 Volume usage | $921 |
| Cross-AZ data transfer | $190 |
| EBS gp3 IOPS provisioning | $55 |
| EBS Snapshots | $55 |

> These costs are missed when the EBS analyser reports "all gp3, optimised" — the actual
> EBS spend is real and should be reflected in the overall cost table using CE data.

### ✅ Savings Plans: Unblended vs Amortized

| Metric | Meaning |
|--------|---------|
| **Unblended cost** | What appears on the monthly invoice (SP negation applied) |
| **Amortized cost** | Effective cost including SP commitment spread across usage |
| `SavingsPlanCoveredUsage` | Compute billed at OD rate before SP discount |
| `SavingsPlanNegation` | Negative amount = the SP discount applied to the bill |

In a real STG account: $8,392 was covered by Savings Plans, with $4,444 discount applied
(effective ~53% off covered usage). The unblended bill was $10,879 but amortized was $14,827.

### ✅ Services In Billing Not Covered by Our Analysers

Always cross-reference the billing service list against our analyser coverage. Common gaps:

| Service in billing | Suggested action |
|--------------------|-----------------|
| `EC2 - Other` | Break down into EBS, Data Transfer, Snapshots |
| `AWS End User Messaging` | Check SMS volumes by country — often hidden Pinpoint/EUM spend |
| `Amazon EKS` | Add `analyse_eks.py` — control plane cost per cluster is fixed $0.10/hr |
| `AWS Key Management Service` | Add `analyse_kms.py` — each CMK costs $1/month |
| `Amazon Route 53` | Add `analyse_route53.py` — hosted zones + health checks |
| `Amazon DynamoDB` | Add `analyse_dynamodb.py` — on-demand vs provisioned capacity |
| `Amazon CloudFront` | Add `analyse_cloudfront.py` — data transfer + requests |
| `Amazon RDS` | Add `analyse_rds.py` — instances, storage, multi-AZ |
| `AWS CodeCommit` | Repositories > free tier threshold |
| `AWS X-Ray` | Trace recording costs — evaluate sampling rate |
| `AWS Payment Cryptography` | Specialist service — validate usage justification |

---

## Data Collection

### AWS API Calls

```python
ce = session.client("ce", region_name="us-east-1")   # CE is always us-east-1

# 1. Total cost by SERVICE (last full calendar month)
ce.get_cost_and_usage(
    TimePeriod={"Start": last_month_start, "End": last_month_end},
    Granularity="MONTHLY",
    Metrics=["UnblendedCost", "AmortizedCost"],
    GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
)

# 2. Record type breakdown (shows SP coverage)
ce.get_cost_and_usage(
    TimePeriod={"Start": last_month_start, "End": last_month_end},
    Granularity="MONTHLY",
    Metrics=["UnblendedCost", "AmortizedCost"],
    GroupBy=[{"Type": "DIMENSION", "Key": "RECORD_TYPE"}],
)

# 3. Drill into "EC2 - Other" by usage type (EBS, data transfer, etc.)
ce.get_cost_and_usage(
    TimePeriod={"Start": last_month_start, "End": last_month_end},
    Granularity="MONTHLY",
    Metrics=["UnblendedCost"],
    Filter={"Dimensions": {"Key": "SERVICE", "Values": ["EC2 - Other"]}},
    GroupBy=[{"Type": "DIMENSION", "Key": "USAGE_TYPE"}],
)

# 4. MTD cost by SERVICE (current month, for current state analysis)
ce.get_cost_and_usage(
    TimePeriod={"Start": mtd_start, "End": today},
    Granularity="MONTHLY",
    Metrics=["UnblendedCost", "AmortizedCost"],
    GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
)

# 5. Daily cost trend (last 90 days) — for trend charts
ce.get_cost_and_usage(
    TimePeriod={"Start": ninety_days_ago, "End": today},
    Granularity="DAILY",
    Metrics=["UnblendedCost"],
)

# 6. Savings Plans coverage
ce.get_savings_plans_coverage(
    TimePeriod={"Start": last_month_start, "End": last_month_end},
    Granularity="MONTHLY",
)
```

### Derived Metrics

```python
billing_data = {
    "period":            f"{last_month_start} to {last_month_end}",
    "total_unblended":   float,      # actual invoice amount
    "total_amortized":   float,      # including SP amortisation
    "total_tax":         float,      # tax line item
    "sp_covered_usage":  float,      # OD-equivalent of SP-covered compute
    "sp_negation":       float,      # actual SP discount applied (negative)
    "sp_effective_rate": float,      # discount % on covered usage
    "by_service":        dict,       # {service_name: {unblended, amortized}}
    "ec2_other_detail":  dict,       # {usage_type: cost}
    "services_in_bill":  list,       # all services with cost > $0
    "services_not_analysed": list,   # services in bill but not in our analyser coverage
}
```

### Cache TTL
- Last full month billing: **7 days** (immutable once month closes)
- MTD / current month: **6 hours** (changes daily)
- Daily trend (90d): **24 hours**

---

## Opportunity 1 — Services In Billing Not Covered by Analysis

**Severity:** 🟡 MEDIUM  
**Opportunity type:** `billing-coverage-gap`

**Detection logic:**

```python
ANALYSED_SERVICES = {
    "Amazon Simple Storage Service",
    "Amazon Elastic Compute Cloud - Compute",
    "EC2 - Other",
    "Amazon Elastic Container Service",
    "Amazon EC2 Container Registry (ECR)",
    "AmazonCloudWatch",
    "Amazon Elastic Block Store",
    "Amazon Elastic Load Balancing",
    "Amazon Virtual Private Cloud",
    "Amazon Managed Streaming for Apache Kafka",
    "Amazon ElastiCache",
    "Amazon Kinesis",
    "Amazon OpenSearch Service",
    "AWS WAF",
    "AWS Lambda",
    "Amazon API Gateway",
    "AWS Secrets Manager",
    "Amazon GuardDuty",
    "Amazon Simple Email Service",
    "AWS Systems Manager",
    "Amazon Simple Notification Service",
    "Amazon Simple Queue Service",
    "AWS End User Messaging",
}

for service, cost in billing_by_service.items():
    if service in ("Tax",) or cost < 1.0:
        continue
    if service not in ANALYSED_SERVICES:
        # Flag as coverage gap
        severity = "HIGH" if cost > 100 else "MEDIUM" if cost > 10 else "LOW"
        yield opportunity(service, cost, severity)
```

**Recommendation:**
> "Service '{service}' cost ${cost:.2f} last month but is not covered by this cost analyser.
> Consider adding an analyser for '{service}' to identify optimisation opportunities."

---

## Opportunity 2 — Cross-AZ Data Transfer Cost

**Severity:** 🟠 HIGH (> $100/mo) | 🟡 MEDIUM ($10–$100) | 🟢 LOW (< $10)  
**Opportunity type:** `cross-az-data-transfer`

**Detection logic:**

Within the `EC2 - Other` usage type breakdown, look for:
- `*-DataTransfer-Regional-Bytes` — cross-AZ + same-region data transfer
- `*-DataTransfer-Out-Bytes` — internet egress from EC2

Cross-AZ traffic costs **$0.01/GB in each direction = $0.02/GB round-trip**. Common causes:
- Services in different AZs making synchronous calls
- Load balancers routing across AZs
- Database replicas in different AZs
- EKS pods scheduled sub-optimally across AZs

```python
regional_transfer_cost = ec2_other_by_type.get("EUC1-DataTransfer-Regional-Bytes", 0)
# Estimate GB: cost / $0.01
estimated_gb = regional_transfer_cost / 0.01
```

**Recommendation:**
> "Cross-AZ / regional data transfer cost ${cost:.2f} last month (est. {gb:,.0f} GB).
> Common causes: services in different AZs, cross-AZ load balancer routing, EKS pod
> topology. Enable VPC Flow Logs and use AWS Cost Explorer resource-level tags to identify
> the top talkers. Consider Availability Zone affinity in service mesh / EKS topology
> spread constraints."

---

## Opportunity 3 — Savings Plan Coverage Gap

**Severity:** 🟡 MEDIUM (< 90% coverage) | 🟢 LOW (≥ 90%)  
**Opportunity type:** `sp-coverage-gap`

**Detection logic:**

```python
sp_covered    = record_types["SavingsPlanCoveredUsage"]["unblended"]
uncovered_od  = record_types.get("Usage", {})["unblended"]
total_compute = sp_covered + uncovered_od
coverage_pct  = sp_covered / total_compute * 100 if total_compute > 0 else 0

sp_negation   = abs(record_types["SavingsPlanNegation"]["unblended"])
effective_disc = sp_negation / sp_covered * 100 if sp_covered > 0 else 0
sp_savings_mo  = sp_negation
```

**Recommendation (gap):**
> "Savings Plans cover {coverage_pct:.1f}% of eligible compute. ${uncovered:.2f}/month of
> compute runs at full On-Demand rates. Increasing the Org SP commitment at the management
> account could save an additional ~${gap_saving:.2f}/month (est. 38% discount on uncovered
> usage)."

---

## Opportunity 4 — EBS Costs Hidden in "EC2 - Other"

**Severity:** 🟡 MEDIUM  
**Opportunity type:** `ebs-hidden-cost`

**Detection logic:**

```python
ebs_volume_cost     = ec2_other["EUC1-EBS:VolumeUsage.gp3"]
ebs_throughput_cost = ec2_other["EUC1-EBS:VolumeP-Throughput.gp3"]
ebs_iops_cost       = ec2_other["EUC1-EBS:VolumeP-IOPS.gp3"]
ebs_snapshot_cost   = ec2_other.get("EUC1-EBS:SnapshotUsage", 0)

# Flag if provisioned throughput/IOPS > baseline (gp3 baseline: 125 MB/s, 3000 IOPS free)
# Provisioned throughput/IOPS charges only apply above baseline
if ebs_throughput_cost > 10 or ebs_iops_cost > 10:
    # Check if provisioned values match actual workload
    flag_opportunity()
```

**Recommendation:**
> "EBS costs ${total_ebs:.2f}/month (throughput: ${tp:.2f}, volume: ${vol:.2f},
> IOPS: ${iops:.2f}, snapshots: ${snap:.2f}). Provisioned throughput/IOPS charges above
> gp3 baseline (125 MB/s, 3000 IOPS) indicate potentially over-provisioned volumes. Review
> actual I/O patterns in CloudWatch to right-size provisioned values."

---

## Opportunity 5 — Unexpectedly High Tax Line

**Severity:** ℹ️ INFO  
**Opportunity type:** `billing-tax-info`

Some AWS accounts accrue tax (VAT, GST) depending on the legal entity and country of registration.
Tax is not reducible but should be reported for full-cost transparency.

```python
tax_pct = tax_amount / (total_unblended - tax_amount) * 100
if tax_amount > 0:
    report_info(f"Tax: ${tax_amount:.2f} ({tax_pct:.1f}% of pre-tax spend)")
```

---

## Opportunity 6 — MTD Cost Anomaly (vs Last Month)

**Severity:** 🔴 CRITICAL (> 30% spike) | 🟠 HIGH (15–30%)  
**Opportunity type:** `billing-cost-spike`

**Detection logic:**

```python
last_month_daily_avg = total_last_month / days_in_last_month
mtd_daily_avg        = mtd_total / days_elapsed_this_month
pct_change           = (mtd_daily_avg - last_month_daily_avg) / last_month_daily_avg * 100

if pct_change > 30:
    # Find which service drove the increase
    for svc in services:
        svc_delta = mtd_by_service[svc]["daily_avg"] - lm_by_service.get(svc, {}).get("daily_avg", 0)
```

**Recommendation:**
> "Current month daily spend (${mtd_avg:.2f}/day) is {pct_change:+.1f}% vs last month
> (${lm_avg:.2f}/day). Top driver: '{top_service}' (+${delta:.2f}/day). Investigate recent
> deployments or configuration changes."

---

## How to Use Billing Data in the Overall Summary

The `analyse_billing.py` script should be the **first step** in any summary run. It produces
two key outputs:

1. **`billing_actuals`** — dict of `{service_label: actual_cost}` from CE for last month.
   Use these figures in the overall summary table instead of CSV-aggregated costs.

2. **`coverage_gaps`** — list of services in the bill with no corresponding analyser.
   Emit as opportunities in the billing CSV.

The overall summary script should:

```python
# CORRECT approach for overall cost table
billing = load_cache("billing") or run_billing_analysis()
for service in ALL_SERVICES:
    actual_cost = billing["by_service"].get(CE_SERVICE_MAP[service], 0)
    opp_saving  = sum_savings_from_csv(service)   # from per-service CSV
    print(f"{service}: actual=${actual_cost:.2f}  saving=${opp_saving:.2f}")

# WRONG approach — do not do this
total_cost = sum(csv_row["current_cost_usd_monthly"] for csv in all_csvs for csv_row in csv)
```

### CE Service Name → Analyser Mapping

| CE Service Name | Analyser |
|----------------|---------|
| `Amazon Elastic Compute Cloud - Compute` | `analyse_ec2.py` |
| `EC2 - Other` | `analyse_ebs.py` + cross-AZ (billing) |
| `Amazon Elastic Container Service` | `analyse_ecs.py` |
| `Amazon EC2 Container Registry (ECR)` | `analyse_ecr.py` |
| `AmazonCloudWatch` | `analyse_cloudwatch.py` |
| `Amazon Elastic Load Balancing` | `analyse_elb.py` |
| `Amazon Virtual Private Cloud` | `analyse_vpc.py` |
| `Amazon Managed Streaming for Apache Kafka` | `analyse_msk.py` |
| `Amazon ElastiCache` | `analyse_elasticache.py` |
| `Amazon Kinesis` | `analyse_kinesis.py` |
| `Amazon OpenSearch Service` | `analyse_opensearch.py` |
| `AWS WAF` | `analyse_waf.py` |
| `AWS Lambda` | `analyse_lambda.py` |
| `Amazon API Gateway` | `analyse_apigw.py` |
| `AWS Secrets Manager` | `analyse_secrets_manager.py` |
| `Amazon GuardDuty` | `analyse_guardduty.py` |
| `Amazon Simple Email Service` | `analyse_ses.py` |
| `AWS Systems Manager` | `analyse_ssm.py` |
| `Amazon Simple Queue Service` | `analyse_sqs.py` |
| `Amazon Simple Notification Service` | `analyse_sns.py` |
| `Amazon Simple Storage Service` | `analyse_s3.py` |
| `AWS End User Messaging` | `analyse_pinpoint.py` |
| `Amazon Elastic Container Service for Kubernetes` | *(gap — add analyse_eks.py)* |
| `AWS Key Management Service` | *(gap — add analyse_kms.py)* |
| `Amazon Route 53` | *(gap — add analyse_route53.py)* |
| `Amazon DynamoDB` | *(gap — add analyse_dynamodb.py)* |
| `Amazon CloudFront` | *(gap — add analyse_cloudfront.py)* |
| `Amazon Relational Database Service` | *(gap — add analyse_rds.py)* |

---

## Notes

- Always use `region_name="us-east-1"` for the CE client — it is a global API.
- Cost Explorer has a charge of **$0.01 per API request** (after 1M free). Batch calls
  accordingly and cache aggressively (7 days for closed months, 6 hours for MTD).
- `UnblendedCost` = what appears on the invoice. Use this for the "actual cost" column.
- `AmortizedCost` = actual cost including SP/RI upfront fees spread across usage. Use this
  for "true economic cost" comparisons.
- Tax appears as a separate line item in CE. Always strip it out when comparing service
  costs to avoid inflating the total.
- For cross-AZ traffic analysis, CE does not provide source/destination AZ granularity.
  Use VPC Flow Logs + Athena (or CloudWatch Logs Insights) to identify top cross-AZ talkers.
- The `analyse_billing.py` script should always write its CE actuals to the cache so other
  scripts and the summary aggregator can read it without re-querying CE.
