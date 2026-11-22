# Skill: Amazon ElastiCache — Cost-Saving Opportunities

## Service Overview

Amazon ElastiCache (Redis and Memcached) charges for **node hours**, **backup storage**, and
**data transfer**. The main savings levers are Graviton node migration, reserved nodes,
right-sizing from CloudWatch metrics, and reviewing idle clusters.

---

## Data Collection

### AWS API Calls

```python
ec_client = session.client("elasticache", region_name=region)
cw        = session.client("cloudwatch", region_name=region)

# List all cache clusters (includes replication groups expanded)
ec_client.describe_cache_clusters(ShowCacheNodeInfo=True)

# List all replication groups (Redis clusters)
ec_client.describe_replication_groups()

# Reserved nodes (active)
ec_client.describe_reserved_cache_nodes(Filters=[{"Name": "state", "Values": ["active"]}])

# CloudWatch: CPU utilisation per cache cluster (last 15 days)
cw.get_metric_statistics(
    Namespace="AWS/ElastiCache",
    MetricName="CPUUtilization",
    Dimensions=[{"Name": "CacheClusterId", "Value": cluster_id}],
    StartTime=now - timedelta(days=15),
    EndTime=now,
    Period=3600,
    Statistics=["Average", "Maximum"]
)

# CloudWatch: EngineCPUUtilization (Redis-specific, more accurate for Redis)
cw.get_metric_statistics(
    Namespace="AWS/ElastiCache",
    MetricName="EngineCPUUtilization",
    ...
)

# CloudWatch: DatabaseMemoryUsagePercentage (Redis)
cw.get_metric_statistics(
    Namespace="AWS/ElastiCache",
    MetricName="DatabaseMemoryUsagePercentage",
    ...
)

# CloudWatch: CurrConnections (detect idle clusters)
cw.get_metric_statistics(
    Namespace="AWS/ElastiCache",
    MetricName="CurrConnections",
    ...
)
```

### Cache TTL
- Cluster + replication group metadata: **24 hours**
- CloudWatch metrics: **6 hours**

---

## Opportunity 1 — Migrate to Graviton Node Types (Highly Recommended)

**Severity:** 🟠 HIGH  
**Opportunity type:** `elasticache-graviton-migration`

**Detection logic:**
- Node type is **not** Graviton-based (Graviton node types end in `g`:
  `cache.m6g.*`, `cache.r6g.*`, `cache.t4g.*`).
- x86 node types to flag: `cache.m5.*`, `cache.m6i.*`, `cache.r5.*`, `cache.r6i.*`, `cache.t3.*`

Graviton-based ElastiCache nodes offer:
- **Up to 20% lower cost** than equivalent x86 nodes
- Better CPU performance for Redis/Memcached

> ✅ **Safe & Highly Recommended** — ElastiCache handles the node replacement with a rolling
> update in the maintenance window. Redis clients reconnect automatically with minimal disruption.

**Graviton mapping:**

| Current type | Graviton equivalent | Saving (approx.) |
|---|---|---|
| cache.t3.micro | cache.t4g.micro | ~15–20% |
| cache.t3.small | cache.t4g.small | ~15–20% |
| cache.t3.medium | cache.t4g.medium | ~15–20% |
| cache.m5.large | cache.m6g.large | ~15–20% |
| cache.r5.large | cache.r6g.large | ~15–20% |
| cache.r5.xlarge | cache.r6g.xlarge | ~15–20% |

**Calculation:**
```
node_count       = cluster_node_count (or shard_count * replicas_per_shard for Redis)
current_hourly   = pricing["elasticache"][current_node_type]["on_demand"]
graviton_hourly  = pricing["elasticache"][graviton_node_type]["on_demand"]
monthly_saving   = (current_hourly - graviton_hourly) * 730 * node_count
```

**Recommendation:**
> "ElastiCache cluster '{cluster}' has {node_count} nodes on {current_type}. Migrating to
> {graviton_type} saves ~${saving}/month (~20%). This is a safe rolling update with no client
> changes required."

---

## Opportunity 2 — No Reserved Nodes (Clusters Running > 30 Days)

**Severity:** 🔴 CRITICAL  
**Opportunity type:** `elasticache-reserved-nodes`

**Detection logic:**
- Cluster was created > 30 days ago (`CacheClusterCreateTime`).
- No matching reserved node found (match on `CacheNodeType`, `ProductDescription`).
- 1-year reserved nodes save **30–40%** over On-Demand.

**Calculation:**
```
on_demand_hourly = pricing["elasticache"][node_type]["on_demand"]
reserved_hourly  = pricing["elasticache"][node_type]["reserved_1yr_no_upfront"]
monthly_saving   = (on_demand_hourly - reserved_hourly) * 730 * node_count
```

**Recommendation:**
> "ElastiCache cluster '{cluster}' has {node_count} {node_type} nodes running On-Demand for
> {days} days. Purchasing 1-year reserved nodes saves ~${saving}/month ({pct}%)."

---

## Opportunity 3 — Under-utilised Cluster (Low CPU + Low Memory)

**Severity:** 🟠 HIGH  
**Opportunity type:** `elasticache-right-sizing`

**Detection logic:**
Using CloudWatch over 15 days:
- `EngineCPUUtilization` average < 10% AND `DatabaseMemoryUsagePercentage` average < 20%.
- Node is candidate for downsizing to the next smaller type.

**Calculation:**
```
smaller_type     = one_size_down(current_node_type)
monthly_saving   = (pricing[current_type] - pricing[smaller_type]) * 730 * node_count
```

**Recommendation:**
> "ElastiCache cluster '{cluster}' has average CPU of {avg_cpu}% and memory usage of {avg_mem}%
> over 15 days. Downsizing from {current_type} to {smaller_type} would save ~${saving}/month.
> Validate peak memory usage before downsizing Redis clusters."

---

## Opportunity 4 — Idle Cluster (No Connections)

**Severity:** 🔴 CRITICAL  
**Opportunity type:** `elasticache-idle-cluster`

**Detection logic:**
- `CurrConnections` average = 0 over the last 15 days.
- Cluster is incurring compute charges with no active clients.

**Recommendation:**
> "ElastiCache cluster '{cluster}' has had 0 connections over the last 15 days. This cluster
> appears idle and is costing ~${monthly_cost}/month. Delete it if no longer required."

---

## Opportunity 5 — Single-Node Redis Cluster in Production

**Severity:** 🟡 MEDIUM  
**Opportunity type:** `elasticache-no-replication`

**Detection logic:**
- Replication group has `NumCacheClusters = 1` (no replicas).
- VPC / tag suggests production workload.
- Single-node Redis has no failover — if the node fails, cache is unavailable.
- While not directly a cost saving, adding a replica doubles cost; flag as informational.

**Recommendation:**
> "Redis replication group '{group_id}' has no read replicas (single node). For production
> workloads, add at least one replica for availability. Note this will increase cost by
> ~${replica_cost}/month but reduces the risk of full cache unavailability."

---

## Opportunity 6 — Redis Backup / Snapshot Retention Too Long

**Severity:** 🟢 LOW  
**Opportunity type:** `elasticache-backup-cost`

**Detection logic:**
- `SnapshotRetentionLimit > 7` days for a Redis cluster with a small dataset (infer from
  node type — t3/t4g nodes typically store small datasets).
- Snapshots stored in S3 accumulate at $0.085/GB-month.

**Recommendation:**
> "Redis cluster '{cluster}' retains snapshots for {retention} days. For development or
> non-critical clusters, reducing retention to 1–7 days can lower backup storage costs."

---

## Pricing Sources

- `pricing.get_products(ServiceCode="AmazonElastiCache", ...)`
  - Filter by `instanceType`, `cacheEngine` (`Redis`, `Memcached`)
- Reserved pricing: `ec_client.describe_reserved_cache_nodes_offerings(...)`

---

## Notes

- For Redis clusters using cluster mode (multiple shards), multiply savings by shard count × replica count.
- Graviton migration opportunities should be labelled **"Safe & Highly Recommended"** in the dashboard.
- Tag-based filtering: skip clusters with tag `cost-analyser:ignore = true`.
