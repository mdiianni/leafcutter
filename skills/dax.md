# Skill: Amazon DAX (DynamoDB Accelerator) — Cost-Saving Opportunities

## Service Overview

Amazon DAX is an in-memory write-through cache for DynamoDB. It charges for **node hours** only
(no separate storage or request fees). There are no Free Tier hours for DAX. Because every DAX
cluster costs money regardless of cache effectiveness, the primary analysis questions are:

1. Is the cache actually being hit (cache hit rate)?
2. Is the cluster idle entirely?
3. Are cheaper Graviton node types available?
4. Is the node oversized for the workload?
5. Is the cluster properly configured for availability?

---

## Pricing Model (eu-central-1, on-demand per node-hour)

| Node Type        | $/hr   | $/month (730 h) |
|------------------|--------|-----------------|
| dax.t3.small     | 0.059  | 43.07           |
| dax.t3.medium    | 0.116  | 84.68           |
| dax.t4g.small    | 0.052  | 37.96           |
| dax.t4g.medium   | 0.104  | 75.92           |
| dax.r5.large     | 0.223  | 162.79          |
| dax.r5.xlarge    | 0.446  | 325.58          |
| dax.r5.2xlarge   | 0.892  | 651.16          |
| dax.r6g.large    | 0.193  | 140.89          |
| dax.r6g.xlarge   | 0.386  | 281.78          |
| dax.r6g.2xlarge  | 0.772  | 563.56          |

**Total cluster cost = node_count × node_hourly_price × 730**

Prices vary by region; the values above are for `eu-central-1`.
Use `pricing.get_products(ServiceCode="AmazonDynamoDB")` filtered by `cacheNodeType` to get
region-accurate pricing programmatically.

---

## When DAX Makes Sense vs When to Remove It

### Keep DAX when:
- Item cache hit rate is consistently **> 60 %** over a multi-day window.
- Applications perform **read-heavy, repetitive GetItem/BatchGetItem** calls on a hot key set.
- Read latency requirements are **< 1 ms** (DAX achieves microsecond reads from cache).
- DynamoDB read costs are high and DAX reduces them via cache hits.

### Remove DAX when:
- Cache hit rate is **< 20 %**: the majority of reads still go to DynamoDB, so the only effect
  is added latency (cache miss path adds ~1 ms overhead) and the full DAX node cost.
- The cluster is **completely idle**: zero hits for 7+ days means no application traffic.
- The application performs mostly **write-heavy** workloads — DAX caches reads, not writes.
- The application already implements **client-side caching** (e.g. in-process LRU with TTL).
- DynamoDB table uses **on-demand capacity mode** with very irregular access — cost savings from
  caching are negligible compared to DAX's fixed node cost.

### Alternatives to DAX:
- **Application-level LRU cache** (e.g. `cachetools` in Python, Caffeine in Java): zero infra cost,
  sub-millisecond for in-process reads, no network hop.
- **ElastiCache (Redis)**: more flexible eviction policies, richer data structures, supports
  non-DynamoDB data; worthwhile if you already have an ElastiCache cluster.
- **DynamoDB DAX-less + read replicas**: for global tables or cross-region reads.

---

## Cache Hit Rate Importance

DAX tracks two types of cache:

| Metric            | Description                                         |
|-------------------|-----------------------------------------------------|
| `ItemCacheHits`   | GetItem / BatchGetItem served from DAX item cache   |
| `ItemCacheMisses` | GetItem / BatchGetItem that fell through to DynamoDB|
| `QueryCacheHits`  | Query/Scan results served from DAX query cache      |
| `QueryCacheMisses`| Query/Scan that fell through to DynamoDB            |

**Hit rate formula:**
```
item_hit_rate = ItemCacheHits / (ItemCacheHits + ItemCacheMisses) * 100
```

**Thresholds:**
- `>= 80 %` — DAX is highly effective; strong case for keeping it.
- `40–80 %` — Acceptable; consider whether write/miss overhead is tolerable.
- `20–40 %` — Marginal value; review access patterns and TTL settings.
- `< 20 %`  — DAX is costing more than it saves. Remove or fix access patterns.
- `= 0 %`   — Cluster is completely idle; delete immediately.

**Root causes of low hit rates:**
- Items have very short TTL (default item cache TTL is 5 minutes).
- Access pattern is uniformly distributed (no hot keys → low temporal locality).
- Writes invalidate cache entries faster than reads can benefit.
- DAX cluster endpoint is configured but application still calls DynamoDB directly.

---

## Graviton Node Types

DAX supports AWS Graviton2 (`t4g`, `r6g` families). Graviton nodes offer:
- **~15–20% lower cost** than equivalent Intel/x86 nodes.
- **No application code changes required** — the DAX client SDK is architecture-agnostic.
- The node replacement is **rolling** (DAX performs it in a maintenance window).

**Migration mapping:**

| Current (x86) | Graviton equivalent | Approx. saving |
|---------------|---------------------|----------------|
| dax.t3.small  | dax.t4g.small       | ~12 %          |
| dax.t3.medium | dax.t4g.medium      | ~10 %          |
| dax.r5.large  | dax.r6g.large       | ~13 %          |
| dax.r5.xlarge | dax.r6g.xlarge      | ~13 %          |
| dax.r5.2xlarge| dax.r6g.2xlarge     | ~13 %          |

**How to migrate:**
```bash
aws dax update-cluster \
  --cluster-name my-dax-cluster \
  --node-type dax.r6g.large
```
DAX handles the rolling replacement; the cluster remains available during the update.

---

## Right-Sizing DAX Nodes

DAX exposes `CPUUtilization` (percentage) via CloudWatch namespace `AWS/DAX`.

**Detection logic:**
- If `CPUUtilization` average < 10 % over 15 days → candidate for one size down.
- Memory is not directly exposed; infer from node type's known RAM:

| Node Type       | vCPU | RAM    |
|-----------------|------|--------|
| dax.t3.small    | 2    | 1.5 GB |
| dax.t3.medium   | 2    | 3 GB   |
| dax.t4g.small   | 2    | 1.5 GB |
| dax.t4g.medium  | 2    | 3 GB   |
| dax.r5.large    | 2    | 16 GB  |
| dax.r5.xlarge   | 4    | 32 GB  |
| dax.r5.2xlarge  | 8    | 64 GB  |
| dax.r6g.large   | 2    | 16 GB  |
| dax.r6g.xlarge  | 4    | 32 GB  |
| dax.r6g.2xlarge | 8    | 64 GB  |

**Key note:** For `r`-family nodes, DAX uses node RAM to hold the item cache. Before downsizing,
check whether the working set fits in the smaller node's RAM. If cache evictions increase after
downsizing, hit rates will drop and the saving will be offset by DynamoDB read costs.

---

## Availability: Single-Node Clusters

A single-node DAX cluster has **no replica**. On node failure:
- DAX becomes unavailable for the duration of the automatic replacement (minutes to tens of minutes).
- Application reads fall through to DynamoDB at full DynamoDB latency.
- For **latency-sensitive production workloads**, this is a significant availability risk.

**Minimum recommended cluster sizes:**
- Development / testing: 1 node (acceptable).
- Production with latency SLA: 3 nodes (1 primary + 2 replicas) for Multi-AZ.
- Production without strict latency SLA: 2 nodes (1 primary + 1 replica).

Adding a replica node costs an additional `node_hourly_price × 730` per month.

---

## AWS API Calls

```python
dax_client = session.client("dax", region_name=region)

# List all clusters (paginated)
paginator = dax_client.get_paginator("describe_clusters")
for page in paginator.paginate():
    clusters = page["Clusters"]

# Each cluster contains:
#   ClusterName, NodeType, Status, TotalNodes, ActiveNodes,
#   ClusterEndpoint, ParameterGroup, SubnetGroup, ...
```

### CloudWatch metrics (namespace: `AWS/DAX`)

| Metric              | Dimension     | Recommended statistic | Window     |
|---------------------|---------------|-----------------------|------------|
| `ItemCacheHits`     | ClusterName   | Sum                   | 15 days    |
| `ItemCacheMisses`   | ClusterName   | Sum                   | 15 days    |
| `QueryCacheHits`    | ClusterName   | Sum                   | 15 days    |
| `QueryCacheMisses`  | ClusterName   | Sum                   | 15 days    |
| `CPUUtilization`    | ClusterName   | Average (1-hr period) | 15 days    |

```python
cw.get_metric_statistics(
    Namespace="AWS/DAX",
    MetricName="ItemCacheHits",
    Dimensions=[{"Name": "ClusterName", "Value": cluster_name}],
    StartTime=now - timedelta(days=15),
    EndTime=now,
    Period=1296000,   # 15 days as a single bucket for Sum
    Statistics=["Sum"],
)
```

---

## Opportunity Summary

| # | Opportunity             | Severity  | Detection                                           | Saving             |
|---|-------------------------|-----------|-----------------------------------------------------|--------------------|
| 1 | Idle cluster            | CRITICAL  | ItemCacheHits + QueryCacheHits = 0 over 7 days      | Full cluster cost  |
| 2 | Low cache hit rate      | HIGH      | ItemCacheHits / total < 20% over 15 days            | Full cluster cost  |
| 3 | Graviton migration      | HIGH      | Node family in {t3, r5}                             | 10–15 %            |
| 4 | Oversized node          | MEDIUM    | CPUUtilization avg < 10 % over 15 days              | Price delta        |
| 5 | Single-node cluster     | MEDIUM    | TotalNodes == 1                                     | Availability risk  |

---

## Notes

- DAX does **not** support ElastiCache-style reserved nodes. There are no Reserved Instance
  savings available; the only pricing lever is node type selection.
- DAX clusters are **VPC-only**. Endpoint DNS is cluster-scoped; changing node type does not
  change the endpoint.
- Tag-based filtering: skip clusters with tag `cost-analyser:ignore = true`.
- DAX is not available in all AWS regions. Check regional availability before recommending
  cross-region migration.
