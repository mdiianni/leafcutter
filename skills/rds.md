# Skill: Amazon RDS — Cost-Saving Opportunities

## Service Overview

Amazon RDS and Aurora costs come from **instance compute**, **storage**, **I/O** (Aurora),
**backup storage**, and **data transfer**. The biggest savings opportunities are commitment
discounts, Graviton migration, right-sizing, and storage optimisation.

---

## Data Collection

### AWS API Calls

```python
rds = session.client("rds", region_name=region)
cw  = session.client("cloudwatch", region_name=region)

# All RDS instances
rds.describe_db_instances()

# All Aurora clusters
rds.describe_db_clusters()

# Reserved instances (active)
rds.describe_reserved_db_instances(Filters=[{"Name": "state", "Values": ["active"]}])

# CloudWatch: CPU utilisation (last 15 days, 1-hour period)
cw.get_metric_statistics(
    Namespace="AWS/RDS",
    MetricName="CPUUtilization",
    Dimensions=[{"Name": "DBInstanceIdentifier", "Value": db_id}],
    StartTime=now - timedelta(days=15),
    EndTime=now,
    Period=3600,
    Statistics=["Average", "Maximum"]
)

# CloudWatch: DatabaseConnections (detect idle databases)
cw.get_metric_statistics(
    Namespace="AWS/RDS",
    MetricName="DatabaseConnections",
    ...  # same pattern
)

# CloudWatch: FreeStorageSpace (detect storage pressure)
cw.get_metric_statistics(
    Namespace="AWS/RDS",
    MetricName="FreeStorageSpace",
    ...
)
```

### Cache TTL
- Instance/cluster metadata + reserved instances: **24 hours**
- CloudWatch metrics: **6 hours**

---

## Opportunity 1 — Migrate to Graviton (Highly Recommended)

**Severity:** 🟠 HIGH  
**Opportunity type:** `rds-graviton-migration`

**Detection logic:**
- Instance class is **not** a Graviton-based instance (i.e., does not end in `g`):
  - x86 families: `db.m5`, `db.m6i`, `db.r5`, `db.r6i`, `db.t3`, `db.t4g` (t4g is already Graviton ✅)
  - Graviton families: `db.m6g`, `db.m7g`, `db.r6g`, `db.r7g`, `db.t4g`, `db.x2g`

Graviton-based RDS instances offer **up to 35% better price/performance** vs equivalent x86
instances. Migration is safe for all RDS engines that support Graviton:
- MySQL 8.0+
- PostgreSQL 12.9+, 13.5+, 14.2+, 15+
- MariaDB 10.4+
- Aurora MySQL 3.x
- Aurora PostgreSQL 13.3+

> ✅ **This is a safe migration** — no application changes required. RDS handles the instance
> class change with a maintenance window (reboot required). Multi-AZ deployments minimise downtime.

**Graviton mapping:**

| Current class | Graviton equivalent | Saving (approx.) |
|---|---|---|
| db.m5.large | db.m6g.large | 15–20% |
| db.m5.xlarge | db.m6g.xlarge | 15–20% |
| db.m6i.* | db.m7g.* | 10–15% |
| db.r5.* | db.r6g.* | 15–20% |
| db.r6i.* | db.r7g.* | 10–15% |
| db.t3.* | db.t4g.* | 10–20% |

**Calculation:**
```
current_hourly   = pricing["rds"][engine][current_class]["on_demand"]
graviton_hourly  = pricing["rds"][engine][graviton_class]["on_demand"]
monthly_saving   = (current_hourly - graviton_hourly) * 730
```

**Recommendation:**
> "RDS instance '{id}' ({class}, {engine}) is running on x86. Migrating to {graviton_class}
> offers equivalent performance at ~${saving}/month less. This is a safe, drop-in replacement —
> requires only a maintenance window reboot."

---

## Opportunity 2 — No Reserved Instance (On-Demand Running > 30 Days)

**Severity:** 🔴 CRITICAL  
**Opportunity type:** `rds-reserved-instance`

**Detection logic:**
- Instance has been running for > 30 days (infer from `InstanceCreateTime`).
- No matching reserved instance found (match on: `DBInstanceClass`, `MultiAZ`, `Engine`).
- 1-year no-upfront RIs save ~30–40% over On-Demand.

**Calculation:**
```
on_demand_hourly = pricing["rds"][engine][class]["on_demand"]
ri_1yr_hourly    = pricing["rds"][engine][class]["reserved_1yr_no_upfront"]
monthly_saving   = (on_demand_hourly - ri_1yr_hourly) * 730
```

**Recommendation:**
> "RDS instance '{id}' has been running On-Demand for {days} days. Purchasing a 1-year
> no-upfront Reserved Instance saves ~${saving}/month ({pct}%). If you plan to keep this
> database, commit to an RI."

---

## Opportunity 3 — Storage Type: gp2 → gp3

**Severity:** 🟠 HIGH  
**Opportunity type:** `rds-gp2-to-gp3`

**Detection logic:**
- `StorageType = "gp2"` in `describe_db_instances`.
- gp3 storage is **20% cheaper** than gp2 at the same volume size AND provides baseline 3,000
  IOPS and 125 MB/s throughput for free (vs gp2's burst-only model).

**Calculation:**
```
allocated_gb     = db_instance["AllocatedStorage"]
price_gp2_gb_mo  = pricing["rds"]["storage"]["gp2"]   # e.g. $0.115/GB-mo
price_gp3_gb_mo  = pricing["rds"]["storage"]["gp3"]   # e.g. $0.092/GB-mo
monthly_saving   = (price_gp2_gb_mo - price_gp3_gb_mo) * allocated_gb
```

**Recommendation:**
> "RDS instance '{id}' uses gp2 storage ({allocated_gb} GB). Migrating to gp3 saves
> ~${saving}/month and provides better baseline IOPS performance. This change can be applied
> without downtime using the Modify DB instance option."

---

## Opportunity 4 — Idle Database (No Connections)

**Severity:** 🔴 CRITICAL  
**Opportunity type:** `rds-idle-instance`

**Detection logic:**
- `DatabaseConnections` average over 15 days **= 0** or consistently < 1.
- Instance is a potential zombie — still running with no active users.

**Recommendation:**
> "RDS instance '{id}' ({class}) has had 0 database connections over the last 15 days.
> This instance appears idle and is costing ~${monthly_cost}/month. Consider stopping or
> terminating it if no longer required."

---

## Opportunity 5 — Multi-AZ on Non-Production Databases

**Severity:** 🟡 MEDIUM  
**Opportunity type:** `rds-multiaz-dev`

**Detection logic:**
- `MultiAZ = True` AND instance tags suggest non-production:
  - `Environment` tag contains `dev`, `test`, `staging`, `qa`, `uat`

Multi-AZ doubles the instance cost (standby replica in another AZ).

**Recommendation:**
> "RDS instance '{id}' appears to be a {env} database with Multi-AZ enabled. Non-production
> databases typically do not require Multi-AZ. Disabling it would save ~${saving}/month (50%
> of instance cost)."

---

## Opportunity 6 — Over-allocated Storage with Auto-Scaling Enabled

**Severity:** 🟢 LOW  
**Opportunity type:** `rds-storage-oversized`

**Detection logic:**
- `MaxAllocatedStorage` is set (storage autoscaling enabled).
- `AllocatedStorage` (current size) is significantly larger than `FreeStorageSpace` suggests
  is needed (i.e., used storage < 40% of allocated).

**Recommendation:**
> "RDS instance '{id}' has {allocated_gb} GB allocated but only {used_gb} GB in use ({pct}%).
> Consider reducing the allocated storage size. Note: RDS does not automatically shrink storage;
> this requires a snapshot + restore or engine-level dump/restore."

---

## Opportunity 7 — Aurora Serverless v1 → Aurora Serverless v2 or Provisioned

**Severity:** 🟡 MEDIUM  
**Opportunity type:** `rds-aurora-serverless-v1`

**Detection logic:**
- Cluster `EngineMode = "serverless"` (this indicates Aurora Serverless v1).
- Aurora Serverless v1 is deprecated and priced at a premium ACU rate with coarser scaling.
- Aurora Serverless v2 is more responsive and better priced.

**Recommendation:**
> "Aurora cluster '{id}' uses Serverless v1 (deprecated). Migrating to Aurora Serverless v2
> offers finer-grained autoscaling (0.5 ACU increments), lower cost per ACU, and broader feature
> support."

---

## Pricing Sources

- `pricing.get_products(ServiceCode="AmazonRDS")`
- Filter by: `instanceType`, `databaseEngine`, `deploymentOption` (Multi-AZ / Single-AZ), `region`
- Storage: filter by `volumeType` (`General Purpose-GP2`, `General Purpose-GP3`)

---

## Notes

- Graviton migration opportunities should be labelled **"Safe & Highly Recommended"** in the dashboard.
- Always verify engine version compatibility before recommending Graviton migration.
- Tag-based filtering: skip instances with tag `cost-analyser:ignore = true`.
- Aurora clusters: cost is per ACU-hour or per provisioned instance-hour; analyse both cluster
  and instance levels.
