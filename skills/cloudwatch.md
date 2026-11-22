# Skill: Amazon CloudWatch — Cost-Saving Opportunities

---

## 🧪 Real-World Learnings (validated 2026-03-23, eu-central-1, account `<YOUR_ACCOUNT_ID>`)

### 1. Log Ingestion Dominates — Not Storage
**Observed:** Total stored = 47 GB ($1.41/month). Total ingestion = 504 GB/month ($258.88/month).
Ingestion costs 16× more than storage in this account. Always fetch `IncomingBytes` from
`AWS/Logs` namespace via CloudWatch metrics (last 30 days, 1 data point). This is the real cost
driver. Do NOT focus only on `storedBytes` from `describe_log_groups`.

### 2. VPC Flow Logs Are Typically the #1 Log Ingestion Cost
**Observed:** `/aws/VPCFlowLogs` — 322 GB/month = $161/month (CRITICAL). Default flow logs capture
ALL traffic. Two options: (a) redirect to S3 ($7/month vs $161/month, 22× cheaper), or (b) filter
to REJECT traffic only (reduces ~80%, saves ~$129/month, keeps CW Logs Insights capability).

### 3. EKS Control Plane Logs Are the #2 Log Ingestion Cost
**Observed:** `/aws/eks/aws-eks-ec1-co-dev/cluster` — 182 GB/month = $91/month. EKS enables 5
log types: `api`, `audit`, `authenticator`, `controllerManager`, `scheduler`. The last two are
highest volume and lowest operational value. Disabling them saves ~60% ($55/month). Keep `api`
and `audit` for security compliance.

### 4. Container Insights Is the #1 Custom Metrics Cost ($380/month)
**Observed:** 1,278 custom metrics from `ECS/ContainerInsights` namespace ($383/month). This is
18 metric types × ~71 series each. The full list: `CpuUtilized`, `MemoryUtilized`,
`EphemeralStorageUtilized`, `StorageWriteBytes`, `StorageReadBytes`, `MemoryReserved`,
`CpuReserved`, `EphemeralStorageReserved`, `NetworkTxBytes`, `NetworkRxBytes`,
`RunningTaskCount`, `PendingTaskCount`, `DeploymentCount`, `TaskSetCount`, `DesiredTaskCount`.
Essential for ECS right-sizing (CPU + MEM utilization). Non-essential: EphemeralStorage*,
Storage*Bytes. Reducing to 5 core types saves ~$277/month.

### 5. Metric Stream May Show 0 Updates — Check Destination Health
**Observed:** `datadog-metrics-stream` was running but reported 0 `MetricStreamMetricUpdateCount`
over 30 days. This suggests either (a) the Datadog Firehose destination is broken, or (b) the
metric was not populated for this period. Flag running streams with 0 updates as LOW severity.

### 6. Zombie Alarms Are Common — Three Distinct Patterns
**Observed:** 30/141 alarms (21%) had no actions. Three patterns:
- **ApplicationInsights auto-generated** (4 alarms): Created by Container Insights Application
  Insights integration. These are auto-managed — disable Application Insights to remove them.
- **RDS orphan alarms** (10 alarms): Auto-created for RDS instances but no SNS topic was ever
  configured. Pattern: `{name}-cpu-utilization`, `{name}-freeable-memory`.
- **ECS service alarms** (16 alarms): Created as part of ECS service setup (Terraform?) but SNS
  targets were never attached. Pattern: `ctpa-utils-*-cpu-utilization-high/low`.

### 7. Most Log Groups Already Have Short Retention (1 day) — A Good Sign
**Observed:** 128/177 log groups (72%) have 1-day retention. Mostly Lambda (`/aws/lambda/*`) and
ECS Container Insights. This is cost-efficient. The 1-day setting is common when teams set very
aggressive retention for dev accounts. Only 1 group had no retention (`msk-<project>-<env>-logs`).

### 8. Fetch IncomingBytes in Parallel for All Log Groups
**Pattern:** With 177 log groups, fetching ingestion serially takes too long. Use
`ThreadPoolExecutor(max_workers=20)` — completes in ~30 seconds vs ~3 minutes serially.

### Validated CloudWatch Pricing (eu-central-1, 2026-03):
| Item | Price | Notes |
|---|---|---|
| Log ingestion | $0.50/GB | Biggest cost driver |
| Log storage | $0.03/GB-month | Small vs ingestion |
| Custom metrics | $0.30/metric/month | First 10 free |
| Standard alarm | $0.10/alarm-month | |
| High-res alarm | $0.30/alarm-month | Period < 60s |
| Dashboard | $3.00/dashboard/month | |
| Metric stream | $0.003/1000 updates | |
| Logs Insights | $0.005/GB scanned | |

### Validated Cost Breakdown (eu-central-1, 2026-03-23):
| Item | Monthly Cost |
|---|---|
| Log ingestion (504 GB) | $258.88 |
| Custom metrics (1,278 series) | $380.40 |
| Alarms (141) | $14.10 |
| Log storage (47 GB) | $1.41 |
| **Total** | **$654.79** |
| **Savings identified** | **$873.86/month ($10,486/year)** |

---



Amazon CloudWatch costs come from **custom metrics**, **dashboards**, **logs ingestion and
storage**, **alarms**, **metric streams**, and **Contributor Insights**. Log storage is
frequently the largest CloudWatch cost driver, especially when log groups have no retention
policy and verbosity is high.

---

## Data Collection

### AWS API Calls

```python
logs = session.client("logs", region_name=region)
cw   = session.client("cloudwatch", region_name=region)

# List all log groups with metadata (size, retention)
logs.describe_log_groups()   # paginate with nextToken

# Custom metrics in use
cw.list_metrics(Namespace="CWAgent")       # CloudWatch agent custom metrics
cw.list_metrics(Namespace="Custom")        # Application custom metrics

# CloudWatch dashboards
cw.list_dashboards()

# Alarms
cw.describe_alarms()

# Metric streams (expensive if sending large volumes)
cw.list_metric_streams()

# Contributor Insights rules
logs.describe_query_definitions()
cw.list_dashboards()
```

### Derived metrics

```python
# Per log group:
log_group_name          = lg["logGroupName"]
retention_days          = lg.get("retentionInDays")   # None = never expire
stored_bytes            = lg.get("storedBytes", 0)
stored_gb               = stored_bytes / 1e9
has_retention           = retention_days is not None

# Total account-level:
total_log_groups        = count of all log groups
no_retention_log_groups = [lg for lg in log_groups if not has_retention(lg)]
total_stored_gb         = sum(lg["storedBytes"] for lg in log_groups) / 1e9
```

### Cache TTL
- Log group metadata: **24 hours**
- Alarm + dashboard list: **24 hours**

---

## Opportunity 1 — Log Groups Without Retention Policy (Never Expire)

**Severity:** 🔴 CRITICAL  
**Opportunity type:** `cloudwatch-logs-no-retention`

**Detection logic:**
- `retentionInDays` is `None` or not set → logs never expire.
- Any log group with `storedBytes > 0` and no retention is a cost risk.
- CloudWatch logs storage costs ~$0.03/GB-month (ingestion is $0.50/GB).

**Calculation:**
```
stored_gb        = lg["storedBytes"] / 1e9
price_per_gb_mo  = pricing["cloudwatch"]["log_storage"]   # e.g. $0.03/GB-month
monthly_cost     = stored_gb * price_per_gb_mo
# If retention is set to 30 days, assume 30/365 of current data survives = 8.2%
pct_reduction    = 1 - (30 / 365)
monthly_saving   = monthly_cost * pct_reduction
```

**Recommendation:**
> "Log group '{log_group}' has {stored_gb:.1f} GB stored with no expiry policy, costing
> ~${monthly_cost}/month. Setting a 30-day retention would reduce this to ~${after_cost}/month.
> Review and set appropriate retention for all log groups."

**Aggregate opportunity:** Report total across all log groups without retention:
> "Found {count} log groups ({total_gb:.0f} GB total) with no retention policy, estimated
> storage cost ~${total_cost}/month. Setting 30-day retention across all could save
> ~${total_saving}/month."

---

## Opportunity 2 — Lambda Log Groups With No Retention

**Severity:** 🟠 HIGH  
**Opportunity type:** `cloudwatch-lambda-log-retention`

**Detection logic:**
- Log group name matches `/aws/lambda/{function_name}`.
- `retentionInDays` is `None`.
- Lambda automatically creates log groups — developers often forget to set retention.

**Recommendation:**
> "{lambda_log_count} Lambda function log groups have no retention policy. Lambda functions
> generate logs continuously; without retention, these accumulate indefinitely. Set 7–30 day
> retention for development Lambda logs and 30–90 days for production."

---

## Opportunity 3 — ECS/EKS Container Log Groups With No Retention

**Severity:** 🟠 HIGH  
**Opportunity type:** `cloudwatch-container-log-retention`

**Detection logic:**
- Log group names match `/ecs/`, `/eks/`, or `/aws/containerinsights/`.
- `retentionInDays` is `None`.
- Container logs can be very high volume.

**Recommendation:**
> "{container_log_count} container log groups have no retention policy, consuming {total_gb:.0f}
> GB. Container logs are often high-volume. Set 14–30 day retention for these groups."

---

## Opportunity 4 — High-Frequency Custom Metrics

**Severity:** 🟡 MEDIUM  
**Opportunity type:** `cloudwatch-custom-metrics-cost`

**Detection logic:**
- `list_metrics` shows > 100 custom metrics (namespace `CWAgent` or application custom namespaces).
- CloudWatch charges $0.30/metric/month (standard resolution) for custom metrics beyond the
  free tier.
- First 10 custom metrics per region are free; $0.30/metric/month thereafter.

**Calculation:**
```
custom_metric_count = total from list_metrics excluding AWS/* namespaces
billable_metrics    = max(0, custom_metric_count - 10)
monthly_cost        = billable_metrics * pricing["cloudwatch"]["custom_metric"]  # $0.30
```

**Recommendation:**
> "Found {custom_metric_count} custom CloudWatch metrics (beyond free tier: {billable_metrics}),
> costing ~${monthly_cost}/month. Review whether all custom metrics are actively used by alarms,
> dashboards, or auto-scaling policies. Remove unused metrics from your instrumentation."

---

## Opportunity 5 — Dashboards Not Referenced / Unused

**Severity:** 🟢 LOW  
**Opportunity type:** `cloudwatch-unused-dashboards`

**Detection logic:**
- `list_dashboards` returns dashboards older than 90 days.
- Each dashboard costs $3/month.
- Flag if there are > 5 dashboards.

**Calculation:**
```
dashboard_count  = len(dashboards)
dashboard_cost   = dashboard_count * pricing["cloudwatch"]["dashboard"]  # $3/month
```

**Recommendation:**
> "Found {dashboard_count} CloudWatch dashboards costing ~${dashboard_cost}/month. Review
> and delete dashboards that are no longer actively used."

---

## Opportunity 6 — CloudWatch Alarms With No Actions (Zombie Alarms)

**Severity:** 🟢 LOW  
**Opportunity type:** `cloudwatch-zombie-alarms`

**Detection logic:**
- `describe_alarms` returns alarms with no `AlarmActions`, `OKActions`, or `InsufficientDataActions`.
- These alarms cost $0.10/alarm-month (standard resolution) with no operational benefit.

**Calculation:**
```
zombie_alarm_count = count of alarms with no actions
monthly_saving     = zombie_alarm_count * pricing["cloudwatch"]["alarm_standard"]  # $0.10
```

**Recommendation:**
> "Found {zombie_alarm_count} CloudWatch alarms with no configured actions. These alarms fire
> silently and serve no purpose. Delete them to save ~${monthly_saving}/month and reduce
> alarm noise."

---

## Opportunity 7 — CloudWatch Logs Insights Queries on Large Log Groups

**Severity:** 🟢 LOW  
**Opportunity type:** `cloudwatch-insights-cost`

**Detection logic:**
- Log groups > 10 GB with no retention policy (already flagged in Opportunity 1).
- **Informational:** CloudWatch Logs Insights charges $0.005/GB scanned. Large log groups
  make queries increasingly expensive over time.

**Recommendation:**
> "Large log groups without retention policies are also expensive to query with CloudWatch
> Logs Insights ($0.005/GB scanned). Setting retention policies on Opportunity 1 will
> reduce future query costs."

---

## Pricing Sources

| Item | Price |
|---|---|
| Log ingestion | $0.50/GB |
| Log storage | $0.03/GB-month |
| Custom metrics | $0.30/metric/month (first 10K), $0.09 next 240K |
| Dashboards | $3.00/dashboard/month |
| Standard alarms | $0.10/alarm-month |
| High-resolution alarms | $0.30/alarm-month |
| Logs Insights queries | $0.005/GB scanned |
| Metric streams | $0.003/1000 metric updates |

Fetch exact pricing: `pricing.get_products(ServiceCode="AmazonCloudWatch", ...)`

---

## Notes

- Log group `storedBytes` is updated approximately daily. Use it as an indicative measure.
- CloudWatch Container Insights has its own cost: ~$0.50 per 1M custom metrics. Include
  a note if Container Insights is enabled on many clusters.
- Tag-based filtering is not applicable to log groups (they can't be tagged in the same way),
  but a naming convention exclusion list (e.g., skip `/aws/codebuild/`) can be configured.
