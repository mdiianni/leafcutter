# Skill: Amazon MSK (Managed Streaming for Apache Kafka) — Cost-Saving Opportunities

## Service Overview

Amazon MSK costs come from **broker instance compute**, **EBS broker storage**, **data transfer**,
and optionally **MSK Connect** workers. The principal cost levers are broker right-sizing,
Graviton migration, reserved pricing, and enabling tiered storage for long-retention topics.

---

## Data Collection

### AWS API Calls

```python
kafka = session.client("kafka", region_name=region)
cw    = session.client("cloudwatch", region_name=region)

# List all MSK clusters (provisioned + serverless)
kafka.list_clusters_v2()   # includes both provisioned and serverless

# Describe each cluster
kafka.describe_cluster_v2(ClusterArn=cluster_arn)

# List nodes per cluster (to see instance types)
kafka.list_nodes(ClusterArn=cluster_arn)

# MSK Connect connectors (if in use)
kafka.list_connectors()

# CloudWatch: Broker-level metrics (last 15 days)
# CPU utilisation
cw.get_metric_statistics(
    Namespace="AWS/Kafka",
    MetricName="CpuUser",
    Dimensions=[
        {"Name": "Cluster Name", "Value": cluster_name},
        {"Name": "Broker ID",    "Value": str(broker_id)},
    ],
    StartTime=now - timedelta(days=15),
    EndTime=now,
    Period=3600,
    Statistics=["Average", "Maximum"]
)

# Disk usage
cw.get_metric_statistics(
    Namespace="AWS/Kafka",
    MetricName="KafkaDataLogsDiskUsed",
    ...
)

# Network throughput
cw.get_metric_statistics(
    Namespace="AWS/Kafka",
    MetricName="NetworkRxPackets",
    ...
)
cw.get_metric_statistics(
    Namespace="AWS/Kafka",
    MetricName="NetworkTxPackets",
    ...
)
```

### Cache TTL
- Cluster metadata + node list: **24 hours**
- CloudWatch metrics: **6 hours**

---

## Opportunity 1 — Migrate Brokers to Graviton (Highly Recommended)

**Severity:** 🟠 HIGH  
**Opportunity type:** `msk-graviton-migration`

**Detection logic:**
- Broker `InstanceType` is in the x86 family (e.g., `kafka.m5.large`, `kafka.m5.xlarge`).
- Graviton-based MSK broker types (e.g., `kafka.m7g.large`) are available and offer
  **up to 20% lower cost** with better price/performance.

Graviton-compatible MSK instance mapping:

| Current type | Graviton equivalent | Saving (approx.) |
|---|---|---|
| kafka.m5.large | kafka.m7g.large | ~15–20% |
| kafka.m5.xlarge | kafka.m7g.xlarge | ~15–20% |
| kafka.m5.2xlarge | kafka.m7g.2xlarge | ~15–20% |
| kafka.m5.4xlarge | kafka.m7g.4xlarge | ~15–20% |

> ✅ **Safe & Highly Recommended** — MSK handles broker instance migration with a rolling
> update, maintaining availability. Kafka clients are not affected (no code changes needed).

**Calculation:**
```
broker_count     = cluster["NumberOfBrokerNodes"]
current_hourly   = pricing["msk"][current_instance]["on_demand"]
graviton_hourly  = pricing["msk"][graviton_instance]["on_demand"]
monthly_saving   = (current_hourly - graviton_hourly) * 730 * broker_count
```

**Recommendation:**
> "MSK cluster '{cluster}' has {broker_count} brokers on {current_type}. Migrating to
> {graviton_type} would save ~${saving}/month. This is a safe rolling update — Kafka clients
> require no changes."

---

## Opportunity 2 — Broker Storage Type: gp2 → gp3

**Severity:** 🔴 CRITICAL  
**Opportunity type:** `msk-gp2-to-gp3`

**Detection logic:**
- Cluster `BrokerNodeGroupInfo.StorageInfo.EbsStorageInfo.VolumeType = "gp2"` (or default).

MSK brokers use EBS for log storage. Migrating to gp3 saves ~20% on storage costs at the
same size with better baseline throughput.

**Calculation:**
```
storage_per_broker_gb = cluster["BrokerNodeGroupInfo"]["StorageInfo"]["EbsStorageInfo"]["VolumeSizeGB"]
price_gp2             = pricing["ebs"]["gp2"]
price_gp3             = pricing["ebs"]["gp3"]
monthly_saving        = (price_gp2 - price_gp3) * storage_per_broker_gb * broker_count
```

**Recommendation:**
> "MSK cluster '{cluster}' uses gp2 EBS storage ({storage_gb} GB × {broker_count} brokers =
> {total_gb} GB). Migrating to gp3 saves ~${saving}/month with no operational impact."

---

## Opportunity 3 — No Reserved Pricing (Clusters Running > 30 Days)

**Severity:** 🔴 CRITICAL  
**Opportunity type:** `msk-reserved-pricing`

**Detection logic:**
- Cluster `CreationTime` > 30 days ago.
- MSK supports Reserved Instance-equivalent pricing via Savings Plans (Compute Savings Plans
  cover MSK broker hours).

**Recommendation:**
> "MSK cluster '{cluster}' has been running for {days} days. MSK broker compute hours are
> covered by Compute Savings Plans. A 1-year no-upfront Compute Savings Plan typically saves
> 40–60% on MSK compute costs."

---

## Opportunity 4 — Brokers Under-utilised (CPU < 15%)

**Severity:** 🟠 HIGH  
**Opportunity type:** `msk-broker-oversized`

**Detection logic:**
- `CpuUser` average < 15% AND maximum < 30% over 15 days across all brokers.
- Cluster is candidate for downsizing to the next smaller broker type.

**Calculation:**
```
smaller_type     = one_size_down(current_instance_type)
current_hourly   = pricing["msk"][current_type]["on_demand"]
smaller_hourly   = pricing["msk"][smaller_type]["on_demand"]
monthly_saving   = (current_hourly - smaller_hourly) * 730 * broker_count
```

**Recommendation:**
> "MSK cluster '{cluster}' brokers have average CPU of {avg_cpu}% (max: {max_cpu}%) over
> 15 days. Downsizing from {current_type} to {smaller_type} could save ~${saving}/month.
> Validate partition count and replication factor allow for the reduced network/storage throughput."

---

## Opportunity 5 — Enable MSK Tiered Storage for Long-Retention Topics

**Severity:** 🟡 MEDIUM  
**Opportunity type:** `msk-tiered-storage`

**Detection logic:**
- Cluster `StorageMode` is not `TIERED` (i.e., it's `LOCAL`).
- `KafkaDataLogsDiskUsed` is consistently > 60% over 15 days (storage pressure).
- Tiered storage offloads older Kafka segments to S3, allowing larger retention windows at
  a much lower cost per GB ($0.023/GB S3 vs ~$0.08–$0.10/GB EBS).

**Recommendation:**
> "MSK cluster '{cluster}' is at {disk_pct}% disk usage on local EBS storage. Enabling MSK
> Tiered Storage offloads older Kafka log segments to S3, dramatically reducing EBS costs
> while maintaining the same retention SLA. Requires MSK cluster version ≥ 2.8.1."

---

## Opportunity 6 — MSK Serverless vs Provisioned Review

**Severity:** 🟢 LOW  
**Opportunity type:** `msk-serverless-review`

**Detection logic:**
- Cluster is Provisioned type with `NumberOfBrokerNodes = 3` (minimum) AND
  `CpuUser` average < 5% — very low utilisation.
- MSK Serverless may be more cost-effective for sporadic workloads.

**Recommendation:**
> "MSK cluster '{cluster}' is lightly loaded ({avg_cpu}% average CPU). For sporadic or
> variable streaming workloads, MSK Serverless eliminates the per-broker charge and bills
> only for throughput consumed. Evaluate the trade-off based on your throughput patterns."

---

## Pricing Sources

- MSK broker pricing: `pricing.get_products(ServiceCode="AmazonMSK", ...)`
  - Filter by `instanceType`, `region`
- EBS storage: same as EBS skill
- MSK Tiered Storage: S3 pricing (see S3 skill)

---

## Notes

- MSK cluster modifications (instance type, storage) require a rolling broker restart.
  This is handled by the MSK control plane with no data loss.
- Always check `EnhancedMonitoring` level — `PER_BROKER` or `PER_TOPIC_PER_BROKER` incur
  additional CloudWatch metric costs. Downgrade to `DEFAULT` for low-traffic clusters.
- Tag-based filtering: skip clusters with tag `cost-analyser:ignore = true`.
