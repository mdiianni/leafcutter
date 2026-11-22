# Skill: Amazon DynamoDB — Cost-Saving Opportunities

## Service Overview

Amazon DynamoDB charges for **read/write capacity** (on-demand or provisioned), **storage**,
**backup**, **streams**, and **global tables replication**. The biggest cost levers are matching
the capacity mode to usage patterns, enabling TTL to reduce storage, and using the right
table class.

---

## Data Collection

### AWS API Calls

```python
ddb = session.client("dynamodb", region_name=region)
cw  = session.client("cloudwatch", region_name=region)
app_autoscaling = session.client("application-autoscaling", region_name=region)

# List all tables
ddb.list_tables()   # paginate

# Describe each table
ddb.describe_table(TableName=table_name)

# List backups for each table
ddb.list_backups(TableName=table_name)

# CloudWatch: ConsumedReadCapacityUnits (last 15 days)
cw.get_metric_statistics(
    Namespace="AWS/DynamoDB",
    MetricName="ConsumedReadCapacityUnits",
    Dimensions=[{"Name": "TableName", "Value": table_name}],
    StartTime=now - timedelta(days=15),
    EndTime=now,
    Period=3600,
    Statistics=["Sum", "Maximum"]
)

# CloudWatch: ConsumedWriteCapacityUnits
cw.get_metric_statistics(
    Namespace="AWS/DynamoDB",
    MetricName="ConsumedWriteCapacityUnits",
    ...
)

# CloudWatch: SuccessfulRequestLatency (to detect unused tables)
# CloudWatch: UserErrors, SystemErrors

# Auto-scaling targets
app_autoscaling.describe_scalable_targets(
    ServiceNamespace="dynamodb",
    ResourceIds=[f"table/{table_name}"]
)
```

### Derived metrics

```python
# Per table:
billing_mode        = table["BillingModeSummary"]["BillingMode"]  # PAY_PER_REQUEST or PROVISIONED
table_class         = table.get("TableClassSummary", {}).get("TableClass", "STANDARD")
provisioned_rcu     = table["ProvisionedThroughput"]["ReadCapacityUnits"]
provisioned_wcu     = table["ProvisionedThroughput"]["WriteCapacityUnits"]
size_bytes          = table["TableSizeBytes"]
item_count          = table["ItemCount"]
ttl_enabled         = table.get("TimeToLiveDescription", {}).get("TimeToLiveStatus") == "ENABLED"
point_in_time_recovery = table.get("ContinuousBackupsDescription", {}).get("ContinuousBackupsStatus")
```

### Cache TTL
- Table metadata: **24 hours**
- CloudWatch metrics: **6 hours**

---

## Opportunity 1 — Provisioned Mode With Low Utilisation → Switch to On-Demand

**Severity:** 🟠 HIGH  
**Opportunity type:** `dynamodb-on-demand-switch`

**Detection logic:**
- `BillingMode = "PROVISIONED"`.
- `ConsumedRCU / (ProvisionedRCU * 3600)` (utilisation ratio) < 20% average over 15 days.
- Tables with unpredictable or very low traffic are cheaper on-demand.

**Calculation:**
```
# On-demand: $0.25 per million WCU + $0.05 per million RCU (us-east-1)
# Provisioned: $0.00065/RCU-hr + $0.00013/WCU-hr (+ auto-scaling min capacity)

consumed_rcu_monthly = (sum_consumed_rcu_15d / 15) * 30
consumed_wcu_monthly = (sum_consumed_wcu_15d / 15) * 30

on_demand_cost   = (consumed_rcu_monthly / 1e6 * price_rcu_od) + (consumed_wcu_monthly / 1e6 * price_wcu_od)
provisioned_cost = (provisioned_rcu * 730 * price_rcu_hr) + (provisioned_wcu * 730 * price_wcu_hr)

monthly_saving   = max(0, provisioned_cost - on_demand_cost)
```

**Recommendation:**
> "DynamoDB table '{table}' is in provisioned mode with {util_pct}% average capacity utilisation
> over 15 days. Switching to on-demand billing would save ~${saving}/month based on current
> usage patterns. On-demand is more cost-effective for tables with utilisation < 20–25%."

---

## Opportunity 2 — On-Demand Mode With Consistently High Traffic → Switch to Provisioned

**Severity:** 🟠 HIGH  
**Opportunity type:** `dynamodb-provisioned-switch`

**Detection logic:**
- `BillingMode = "PAY_PER_REQUEST"` (on-demand).
- Consistent high traffic: consumed RCU + WCU is relatively stable day-over-day (low coefficient
  of variation).
- Provisioned with auto-scaling is cheaper for predictable high-throughput tables.

**Recommendation:**
> "DynamoDB table '{table}' is on on-demand billing with consistently high, predictable traffic.
> Switching to provisioned capacity with auto-scaling could save ~${saving}/month. On-demand
> has a ~5× premium over provisioned for sustained traffic."

---

## Opportunity 3 — No TTL Enabled on Tables With Large Item Count

**Severity:** 🟠 HIGH  
**Opportunity type:** `dynamodb-ttl-not-enabled`

**Detection logic:**
- `TimeToLiveStatus != "ENABLED"`.
- `ItemCount > 100,000` OR `TableSizeBytes > 1 GB`.
- Tables storing session data, logs, events, or temporary records are candidates for TTL.
- TTL deletes expired items at no cost.

**Recommendation:**
> "DynamoDB table '{table}' has {item_count:,} items ({size_gb:.1f} GB) and TTL is not enabled.
> If this table stores time-bound data (sessions, events, logs, tokens), enabling TTL will
> automatically expire and delete old items, reducing storage costs over time."

---

## Opportunity 4 — Standard Table Class → Standard-Infrequent Access (Standard-IA)

**Severity:** 🟡 MEDIUM  
**Opportunity type:** `dynamodb-standard-ia`

**Detection logic:**
- `TableClass = "STANDARD"` (or not set).
- Table has large storage but very low RCU/WCU consumption:
  - `size_bytes > 10 GB`
  - `consumed_rcu_per_day < 100` average
  - `consumed_wcu_per_day < 100` average
- Standard-IA storage is **60% cheaper** than Standard, but reads/writes cost more.
  Break-even: tables accessed < 10% of the time benefit from Standard-IA.

**Calculation:**
```
size_gb          = size_bytes / 1e9
price_std_gb_mo  = pricing["dynamodb"]["storage_standard"]     # e.g. $0.25/GB-mo
price_ia_gb_mo   = pricing["dynamodb"]["storage_ia"]           # e.g. $0.10/GB-mo
storage_saving   = (price_std_gb_mo - price_ia_gb_mo) * size_gb

# However: Standard-IA costs more per RCU/WCU — calculate break-even
ia_rcu_premium   = pricing["dynamodb"]["rcu_ia"] - pricing["dynamodb"]["rcu_std"]
ia_wcu_premium   = pricing["dynamodb"]["wcu_ia"] - pricing["dynamodb"]["wcu_std"]
access_overhead  = (consumed_rcu_monthly * ia_rcu_premium) + (consumed_wcu_monthly * ia_wcu_premium)

net_monthly_saving = storage_saving - access_overhead
```

**Recommendation:**
> "DynamoDB table '{table}' has {size_gb:.1f} GB of data with very low access frequency
> ({rcu_day:.0f} RCU/day, {wcu_day:.0f} WCU/day). Switching to the Standard-IA table class
> could save ~${saving}/month on storage costs."

---

## Opportunity 5 — Point-in-Time Recovery Enabled on Non-Production Tables

**Severity:** 🟢 LOW  
**Opportunity type:** `dynamodb-pitr-cost`

**Detection logic:**
- `ContinuousBackupsStatus = "ENABLED"` (PITR enabled).
- Table tags suggest non-production (`dev`, `test`, `staging`).
- PITR costs ~$0.20/GB-month (additional storage charge).

**Recommendation:**
> "DynamoDB table '{table}' (tagged as '{env}') has Point-in-Time Recovery enabled.
> For non-production tables, disabling PITR saves ~${saving}/month. PITR is recommended
> for production tables only."

---

## Opportunity 6 — Tables With No Traffic (Zombie Tables)

**Severity:** 🔴 CRITICAL  
**Opportunity type:** `dynamodb-zombie-table`

**Detection logic:**
- `ConsumedReadCapacityUnits + ConsumedWriteCapacityUnits = 0` over the last 15 days.
- Table is in ACTIVE state.
- Provisioned tables with no traffic still charge for provisioned capacity.

**Recommendation:**
> "DynamoDB table '{table}' has had zero reads and writes over the last 15 days. If the table
> is no longer in use, delete it. If it needs to be retained, switch to on-demand billing to
> reduce the idle cost from ~${provisioned_cost}/month to ~$0/month."

---

## Pricing Sources

- `pricing.get_products(ServiceCode="AmazonDynamoDB", ...)`
- Key attributes: `group` (read/write/storage), `usagetype`

---

## Notes

- DynamoDB Global Tables replicate data across regions — each replicated region is billed
  separately. If global tables are in use, the total cost is multiplied by the number of regions.
- DynamoDB Streams incur additional charges if enabled. Check `StreamSpecification` —
  flag if streams are enabled but no consumer is attached (unused stream cost).
- Tag-based filtering: skip tables with tag `cost-analyser:ignore = true`.
