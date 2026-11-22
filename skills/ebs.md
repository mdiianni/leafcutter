# Skill: Amazon EBS — Cost-Saving Opportunities

## Service Overview

Amazon EBS costs accrue from **provisioned storage**, **provisioned IOPS** (for io1/io2),
**snapshot storage**, and **data transfer**. The most impactful opportunities are volume type
upgrades (gp2 → gp3), eliminating orphaned volumes, and pruning old snapshots.

---

## Data Collection

### AWS API Calls

```python
ec2 = session.client("ec2", region_name=region)
cw  = session.client("cloudwatch", region_name=region)

# All EBS volumes
ec2.describe_volumes()   # paginate with NextToken

# All EBS snapshots owned by the account
ec2.describe_snapshots(OwnerIds=["self"])   # paginate

# CloudWatch: volume read/write ops (detect idle volumes)
cw.get_metric_statistics(
    Namespace="AWS/EBS",
    MetricName="VolumeReadOps",
    Dimensions=[{"Name": "VolumeId", "Value": volume_id}],
    StartTime=now - timedelta(days=15),
    EndTime=now,
    Period=86400,
    Statistics=["Sum"]
)
cw.get_metric_statistics(
    Namespace="AWS/EBS",
    MetricName="VolumeWriteOps",
    ...  # same pattern
)
```

### Derived metrics

```python
# Per volume:
is_attached    = len(volume["Attachments"]) > 0
volume_age_days= (now - volume["CreateTime"]).days
state          = volume["State"]   # available = unattached

# Per snapshot:
snapshot_age_days = (now - snapshot["StartTime"]).days
snapshot_size_gb  = snapshot["VolumeSize"]
```

### Cache TTL
- Volume + snapshot metadata: **24 hours**
- CloudWatch I/O metrics: **6 hours**

---

## Opportunity 1 — gp2 Volumes: Migrate to gp3 (Highly Recommended)

**Severity:** 🔴 CRITICAL  
**Opportunity type:** `ebs-gp2-to-gp3`

**Detection logic:**
- `VolumeType = "gp2"` in `describe_volumes`.

gp3 is the newer generation of General Purpose SSDs:
- **20% cheaper** than gp2 at the same size.
- Provides **3,000 IOPS and 125 MB/s throughput baseline** at no extra cost (gp2 requires
  a large volume to burst to the same IOPS level).
- Migration is done in-place with zero downtime — no detach required.

> ✅ **Safe & Highly Recommended** — this is one of the easiest quick wins in AWS.

**Calculation:**
```
price_gp2_per_gb = pricing["ebs"]["gp2"]   # e.g. $0.10/GB-month
price_gp3_per_gb = pricing["ebs"]["gp3"]   # e.g. $0.08/GB-month
size_gb          = volume["Size"]
monthly_saving   = (price_gp2_per_gb - price_gp3_per_gb) * size_gb
```

**Recommendation:**
> "EBS volume '{vol_id}' ({size} GB, gp2) can be migrated to gp3, saving ~${saving}/month with
> no downtime. gp3 also delivers consistent 3,000 IOPS baseline vs gp2's burst model."

---

## Opportunity 2 — io1 Volumes: Migrate to io2

**Severity:** 🟠 HIGH  
**Opportunity type:** `ebs-io1-to-io2`

**Detection logic:**
- `VolumeType = "io1"` in `describe_volumes`.

io2 provides:
- **99.999% durability** (vs 99.9% for io1)
- **Same price as io1** for the first 32,000 IOPS, then **higher IOPS/GB ratio** allowed
- Generally cost-neutral or better for most workloads

**Recommendation:**
> "EBS volume '{vol_id}' is io1. Migrating to io2 provides higher durability (99.999%) and
> allows up to 64,000 IOPS per volume at no extra cost. This is a recommended in-place upgrade."

---

## Opportunity 3 — Orphaned (Unattached) Volumes

**Severity:** 🔴 CRITICAL  
**Opportunity type:** `ebs-orphaned-volume`

**Detection logic:**
- `State = "available"` (not attached to any instance).
- `Volume age > 7 days` in available state.
- Unattached volumes incur full storage charges with no business value.

**Calculation:**
```
monthly_cost = pricing["ebs"][volume_type] * size_gb
```

**Recommendation:**
> "EBS volume '{vol_id}' ({size} GB, {type}) has been unattached for {days} days, costing
> ~${cost}/month. If the data is needed, create a snapshot (~${snapshot_cost}/month) and delete
> the volume. If not needed, delete it."

---

## Opportunity 4 — Snapshots Older Than 90 Days Without Retention Policy

**Severity:** 🟠 HIGH  
**Opportunity type:** `ebs-old-snapshots`

**Detection logic:**
- Snapshot `StartTime` > 90 days ago.
- No AWS Backup plan or DLM (Data Lifecycle Manager) policy managing the snapshot.
- Snapshots older than a typical retention window are likely redundant.

**Aggregated calculation (per volume):**
```
old_snapshots    = [s for s in snapshots if age > 90 and source_vol = vol_id]
old_size_gb      = sum(s["VolumeSize"] for s in old_snapshots)  # conservative; EBS snapshots are incremental
snapshot_price   = pricing["ebs"]["snapshot"]   # e.g. $0.05/GB-month
monthly_saving   = old_size_gb * snapshot_price
```

> Note: EBS snapshots are incremental — the `VolumeSize` field shows the original volume size,
> not the actual snapshot size. Report the saving as a conservative upper bound and note this.

**Recommendation:**
> "Found {count} EBS snapshots older than 90 days (total volume size: {size_gb} GB, estimated
> ~${cost}/month). Consider using AWS Data Lifecycle Manager (DLM) to automate snapshot
> retention and delete snapshots beyond your retention window."

---

## Opportunity 5 — Provisioned IOPS Significantly Over-provisioned

**Severity:** 🟠 HIGH  
**Opportunity type:** `ebs-iops-overprovision`

**Detection logic:**
Using CloudWatch `VolumeReadOps + VolumeWriteOps` over 15 days:
- Calculate the peak IOPS consumed: `max_daily_ops / 86400` (ops/sec).
- If `provisioned_iops / peak_observed_iops > 3.0` → flag as over-provisioned.

**Calculation:**
```
provisioned_iops = volume["Iops"]
peak_iops        = max(daily_read_ops + daily_write_ops) / 86400
overprovision_x  = provisioned_iops / max(peak_iops, 1)
suggested_iops   = ceil(peak_iops * 1.5)  # 50% headroom
price_per_iops   = pricing["ebs"]["io1_iops"]  # e.g. $0.065/provisioned IOPS-month
monthly_saving   = (provisioned_iops - suggested_iops) * price_per_iops
```

**Recommendation:**
> "EBS volume '{vol_id}' (io1, {provisioned_iops} IOPS provisioned) peak observed IOPS was
> {peak_iops} over 15 days. Reducing provisioned IOPS from {provisioned_iops} to {suggested_iops}
> (50% headroom above peak) would save ~${saving}/month."

---

## Opportunity 6 — Snapshots With No Associated Volume (Volume Deleted)

**Severity:** 🟡 MEDIUM  
**Opportunity type:** `ebs-orphaned-snapshot`

**Detection logic:**
- Snapshot `VolumeId` is not found in `describe_volumes`.
- The originating volume has been deleted, but its snapshots remain.

**Recommendation:**
> "EBS snapshot '{snap_id}' (created {date}) belongs to volume '{vol_id}' which no longer
> exists. If this snapshot is no longer needed for recovery, deleting it would save
> ~${cost}/month."

---

## Pricing Sources

| Volume Type | Price Example | Notes |
|---|---|---|
| gp2 | ~$0.10/GB-month | General Purpose SSD v1 |
| gp3 | ~$0.08/GB-month | General Purpose SSD v2 (20% cheaper) |
| io1 | ~$0.125/GB-month + $0.065/IOPS-month | Provisioned IOPS v1 |
| io2 | ~$0.125/GB-month + $0.065/IOPS-month (≤32k IOPS) | Provisioned IOPS v2 |
| st1 | ~$0.045/GB-month | Throughput Optimised HDD |
| sc1 | ~$0.025/GB-month | Cold HDD |
| Snapshots | ~$0.05/GB-month | Incremental |

Fetch exact pricing: `pricing.get_products(ServiceCode="AmazonEC2", filters=[volumeType, ...])`

---

## Notes

- gp2→gp3 is labelled **"Safe & Highly Recommended"** in the dashboard — same as RDS gp2→gp3.
- Snapshots created by AWS Backup or DLM can be identified by their tags (`aws:backup:source-resource-arn` or `dlm:policy-id`). Consider these managed and lower priority.
- Tag-based filtering: skip volumes/snapshots with tag `cost-analyser:ignore = true`.
