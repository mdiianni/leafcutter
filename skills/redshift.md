# Skill: Amazon Redshift — Cost-Saving Opportunities

## Service Overview

Amazon Redshift charges for **node hours** (provisioned clusters), **Redshift Serverless
RPU-hours**, **backup/snapshot storage** (S3), and **data transfer**. The main savings levers
are pausing idle clusters, purchasing Reserved Nodes, migrating to Graviton (ra3), right-sizing
via CloudWatch, adopting Serverless for bursty workloads, and trimming snapshot retention.

---

## Pricing Model

### Provisioned Clusters (On-Demand, eu-central-1)

| Node Type     | $/node/hr | $/node/month |
|---------------|-----------|--------------|
| dc2.large     | $0.282    | ~$206        |
| dc2.8xlarge   | $2.256    | ~$1,647      |
| ra3.xlplus    | $0.454    | ~$331        |
| ra3.4xlarge   | $1.086    | ~$793        |
| ra3.16xlarge  | $4.342    | ~$3,170      |
| ds2.xlarge    | $1.069    | ~$780        |
| ds2.8xlarge   | $8.554    | ~$6,244      |

- **Managed storage** (ra3/ds2): billed separately at ~$0.024/GB-month (ra3) or included (ds2).
- **Automated snapshots**: stored in S3 at ~$0.085/GB-month beyond free tier.
- **Paused clusters**: compute charges stop; you pay only for managed storage.

### Reserved Nodes

| Term  | Discount vs On-Demand |
|-------|-----------------------|
| 1-year No Upfront | ~40% |
| 3-year No Upfront | ~75% |

Reserved Nodes are purchased at the node-type level and apply automatically to matching
provisioned clusters in the same region.

### Redshift Serverless

- Billed in **Redshift Processing Units (RPUs)** per second during query execution.
- No charge when idle — eliminates idle node cost for bursty workloads.
- Base RPU capacity configurable from 8 to 512 RPUs.
- Typical saving vs always-on provisioned cluster: **30–50%** for intermittent workloads.

---

## Data Collection

### AWS API Calls

```python
rs = session.client("redshift", region_name=region)
cw = session.client("cloudwatch", region_name=region)

# List all provisioned clusters (paginated)
rs.get_paginator("describe_clusters").paginate()

# List active reserved nodes
rs.get_paginator("describe_reserved_nodes").paginate()

# CloudWatch metrics (namespace: AWS/Redshift, dimension: ClusterIdentifier)
cw.get_metric_statistics(
    Namespace="AWS/Redshift",
    MetricName="CPUUtilization",           # % — detect idle clusters
    Dimensions=[{"Name": "ClusterIdentifier", "Value": cluster_id}],
    StartTime=now - timedelta(days=15),
    EndTime=now,
    Period=3600,
    Statistics=["Average", "Maximum"]
)
# Also fetch: DatabaseConnections, PercentageDiskSpaceUsed
```

### Cache TTL
- Cluster + reserved node metadata: **24 hours** (standard `cache_utils` TTL)
- CloudWatch metrics: fetched fresh each run (parallel, 10 workers)

---

## Opportunity 1 — Paused / Idle Cluster

**Severity:** 🔴 CRITICAL  
**Opportunity type:** `redshift-paused-idle-cluster`

**Detection logic (either condition):**
- `CPUUtilization` average < 5% over 15 days, **or**
- `DatabaseConnections` = 0 over 15 days.

**Action:** Pause the cluster. Compute charges stop immediately; you pay only for managed
storage (~25% of total cost).

**Saving:** ~75% of current node cost.

**Resume time:** ~3 minutes via console or CLI:
```bash
aws redshift pause-cluster --cluster-identifier <id>
aws redshift resume-cluster --cluster-identifier <id>
```

---

## Opportunity 2 — Reserved Node Opportunity

**Severity:** 🔴 CRITICAL  
**Opportunity type:** `redshift-reserved-node-opportunity`

**Detection logic:**
- Cluster running On-Demand for > 30 days.
- Node type has fewer active Reserved Nodes than cluster node count.

**Saving:**
- 1-year RI: **40%** of uncovered node cost.
- 3-year RI: **75%** of uncovered node cost.

**Action:** Purchase Reserved Nodes via the Redshift console → **Reserved Nodes** tab.
Match node type exactly (e.g., `ra3.xlplus`). RIs apply automatically to running clusters.

---

## Opportunity 3 — Graviton Migration (dc2/ds2 → ra3)

**Severity:** 🟠 HIGH  
**Opportunity type:** `redshift-graviton-migration`

**Detection logic:**
- Cluster node family is `dc2` or `ds2` (x86 architecture).

**ra3 Graviton advantages:**
- ~15% cheaper than equivalent x86 On-Demand pricing.
- Managed storage separates compute from storage — scale each independently.
- Better price/performance for standard SQL workloads.
- No breaking SQL changes required.

**Migration path:**
1. **Classic resize** (minimal downtime, cluster remains available in read-only mode).
2. **Snapshot-and-restore** (zero downtime via blue/green swap).

**Saving:** ~15% of current monthly node cost.

---

## Opportunity 4 — Cluster Right-Sizing

**Severity:** 🟡 MEDIUM  
**Opportunity type:** `redshift-cluster-right-sizing`

**Detection logic (all conditions over 15 days):**
- `PercentageDiskSpaceUsed` average < 30%.
- `CPUUtilization` average < 20%.
- `NumberOfNodes` > 1 (multi-node cluster).

**Options:**
- **Elastic resize** — reduce node count in minutes with minimal disruption.
- **Classic resize** — change node type (longer, cluster read-only during resize).

**Saving:** ~25% from reducing node count by approximately one quarter.

---

## Opportunity 5 — Redshift Serverless Migration

**Severity:** 🟡 MEDIUM  
**Opportunity type:** `redshift-serverless-opportunity`

**Detection logic:**
- `DatabaseConnections` average < 5 **and** `CPUUtilization` average < 10% — consistent
  with a bursty or batch-only workload that is idle most of the day.

**How Serverless works:**
- Provision a **namespace** (data) and **workgroup** (compute, 8–512 RPUs).
- Pay per RPU-second during query execution only.
- Auto-scaling within the RPU range — no manual resize.

**Dependency warning:** Connection string changes are required. Update all applications,
BI tools (Tableau, Quicksight, Grafana), and ETL pipelines to the new Serverless endpoint.

**Saving:** ~35% for workloads idle > 50% of the time.

---

## Opportunity 6 — Automated Snapshot Retention Too High

**Severity:** 🟢 LOW  
**Opportunity type:** `redshift-snapshot-retention`

**Detection logic:**
- `AutomatedSnapshotRetentionPeriod` > 35 days.

**Cost driver:** Automated snapshots are stored in S3 at ~$0.085/GB-month. Long retention
periods accumulate significant storage, especially for large ra3 or ds2 clusters.

**Recommendation:** Reduce retention to 14 days for most workloads (7 for dev/test):
```bash
aws redshift modify-cluster \
  --cluster-identifier <id> \
  --automated-snapshot-retention-period 14
```

---

## CloudWatch Metrics Reference

| Metric                    | Namespace      | Dimension         | Use Case                    |
|---------------------------|----------------|-------------------|-----------------------------|
| CPUUtilization            | AWS/Redshift   | ClusterIdentifier | Detect idle / overloaded    |
| DatabaseConnections       | AWS/Redshift   | ClusterIdentifier | Detect unused clusters      |
| PercentageDiskSpaceUsed   | AWS/Redshift   | ClusterIdentifier | Right-sizing disk headroom  |
| ReadIOPS / WriteIOPS      | AWS/Redshift   | ClusterIdentifier | I/O profile analysis        |
| NetworkReceiveThroughput  | AWS/Redshift   | ClusterIdentifier | Data ingestion volume       |

---

## Notes

- **Pause/Resume** is only available for single-node or multi-node **ra3** clusters, not dc2.
  For dc2 idle clusters, recommend migration to ra3 first, then pause.
- **Reserved Nodes** are non-refundable — verify workload stability before committing.
- **Tag-based exclusion:** Skip clusters with tag `cost-analyser:ignore = true`.
- **Concurrency Scaling:** Additional per-second charges when Concurrency Scaling is enabled
  and active — review `ConcurrencyScalingActivated` metric.
