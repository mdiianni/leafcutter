# Skill: Amazon EFS — Cost-Saving Opportunities

## Service Overview

Amazon EFS costs come from **storage tier** (Standard vs IA vs One Zone), **throughput mode**
(Provisioned vs Bursting), and **data transfer**. The biggest savings opportunities are enabling
lifecycle policies, switching unused provisioned throughput to Bursting, eliminating unused
filesystems, and choosing the right storage class for workload patterns.

---

## Data Collection

### AWS API Calls

```python
efs = session.client("efs", region_name=region)
cw  = session.client("cloudwatch", region_name=region)

# All EFS file systems
efs.describe_file_systems()

# Mount targets per filesystem (detect unmounted / orphaned)
efs.describe_mount_targets(FileSystemId=fs_id)

# Lifecycle configuration (detect missing IA policy)
efs.describe_lifecycle_configuration(FileSystemId=fs_id)

# CloudWatch: client connections and I/O (last 14 days, 1-hour period)
cw.get_metric_statistics(
    Namespace="AWS/EFS",
    MetricName="ClientConnections",
    Dimensions=[{"Name": "FileSystemId", "Value": fs_id}],
    StartTime=now - timedelta(days=14),
    EndTime=now,
    Period=3600,
    Statistics=["Sum"],
)

# Additional metrics: DataReadIOBytes, DataWriteIOBytes, PermittedThroughput, MeteredIOBytes
```

### Key Fields from `describe_file_systems`

| Field | Description |
|---|---|
| `FileSystemId` | Unique filesystem ID (e.g. `fs-abc123`) |
| `Name` | Optional name tag |
| `SizeInBytes["Value"]` | Current total storage in **bytes** → divide by `1024³` for GB |
| `ThroughputMode` | `"bursting"` or `"provisioned"` |
| `ProvisionedThroughputInMibps` | Only present when ThroughputMode is `"provisioned"` |
| `LifeCycleState` | `"available"`, `"creating"`, etc. |
| `AvailabilityZoneName` | Set for One Zone filesystems; absent for Multi-AZ |

### Cache TTL
- Filesystem metadata + mount targets + lifecycle config: **24 hours**
- CloudWatch metrics: **6 hours**

---

## Pricing — eu-central-1 (Frankfurt)

| Storage Class | Price per GB-month |
|---|---|
| EFS Standard (Multi-AZ) | $0.36 |
| EFS Standard-IA | $0.0272 |
| EFS One Zone | $0.192 |
| EFS One Zone-IA | $0.0163 |

| Throughput Mode | Price |
|---|---|
| Provisioned Throughput | $6.00 / MB/s-month |
| Bursting Throughput | Free (included with storage) |

---

## Opportunity 1 — No Lifecycle Policy (IA Tier Not Enabled)

**Severity:** 🟠 HIGH
**Opportunity type:** `efs-no-lifecycle-policy`

**Detection logic:**
- `describe_lifecycle_configuration` returns no rules, **or** no rule with
  `TransitionToIA` set.
- Filesystem is in `"available"` state with `SizeInBytes["Value"] > 0`.

EFS Standard tier costs **$0.36/GB-month**. Standard-IA costs only **$0.0272/GB-month** — a
**92% reduction** for infrequently accessed data. Without a lifecycle policy, every byte stays
in Standard forever. Enabling `TransitionToIA = AFTER_30_DAYS` is a zero-downtime change that
takes effect immediately and requires no application modifications.

**Calculation:**
```
size_gb        = fs["SizeInBytes"]["Value"] / (1024 ** 3)
current_mo     = size_gb * STANDARD_GB_MONTH           # $0.36
# Assume 80% of data becomes IA after 30 days (conservative)
ia_fraction    = 0.80
after_mo       = (size_gb * (1 - ia_fraction) * STANDARD_GB_MONTH
                + size_gb * ia_fraction       * STANDARD_IA_GB_MONTH)
saving_mo      = current_mo - after_mo
```

**Recommendation:**
> "EFS filesystem '{id}' ({size_gb:.1f} GB) has no lifecycle policy — all data remains in
> Standard tier at $0.36/GB-month. Enabling AFTER_30_DAYS transition to IA could save
> ~${saving}/month (assuming 80% of data is infrequently accessed). Zero application changes
> needed — enable via EFS console or `put-lifecycle-configuration`."

---

## Opportunity 2 — Provisioned Throughput Under-Utilised

**Severity:** 🟡 MEDIUM
**Opportunity type:** `efs-provisioned-throughput`

**Detection logic:**
- `ThroughputMode = "provisioned"`.
- 14-day average `MeteredIOBytes` throughput is **< 50%** of `ProvisionedThroughputInMibps`.
- Provisioned throughput is billed regardless of whether it is used.

Provisioned throughput costs **$6.00/MB/s-month**. Bursting throughput is free and scales
automatically with storage size (100 MB/s baseline per TB). Switching to Bursting mode takes
effect immediately and is fully reversible.

**Calculation:**
```
provisioned_mbps   = fs["ProvisionedThroughputInMibps"]
# MeteredIOBytes sum over 14 days → convert to average MB/s
metered_bytes_sum  = sum(datapoints["Sum"])          # bytes over 14 days
metered_mbps_avg   = metered_bytes_sum / (14 * 86400) / (1024 * 1024)
utilisation_pct    = (metered_mbps_avg / provisioned_mbps) * 100

current_mo   = provisioned_mbps * PROVISIONED_THROUGHPUT_PER_MBPS_MONTH  # $6.00
# Saving = full provisioned cost (Bursting is free)
saving_mo    = current_mo
```

**Recommendation:**
> "EFS filesystem '{id}' has {provisioned_mbps} MB/s provisioned throughput but actual average
> throughput is only {metered_mbps_avg:.2f} MB/s ({utilisation_pct:.0f}% utilisation) over the
> last 14 days. Switching to Bursting mode saves ~${saving}/month. Bursting throughput scales
> automatically and is free — recommended unless you have strict latency SLAs."

---

## Opportunity 3 — Unused Filesystem (No Mount Targets or Zero Connections)

**Severity:** 🔴 CRITICAL
**Opportunity type:** `efs-unused-filesystem`

**Detection logic (either condition triggers):**
- `describe_mount_targets` returns **zero** mount targets for the filesystem.
- **Or** `ClientConnections` sum over 14 days is **0** (filesystem mounted but never accessed).

A filesystem with no mount targets cannot be accessed by any compute. A filesystem with zero
connections has had no clients connected — it may be an orphaned resource from a deleted workload.

**Calculation:**
```
size_gb    = fs["SizeInBytes"]["Value"] / (1024 ** 3)
current_mo = size_gb * STANDARD_GB_MONTH   # $0.36/GB
# Plus provisioned throughput if applicable
if fs["ThroughputMode"] == "provisioned":
    current_mo += fs["ProvisionedThroughputInMibps"] * PROVISIONED_THROUGHPUT_PER_MBPS_MONTH
saving_mo = current_mo  # full cost is recoverable
```

**Recommendation:**
> "EFS filesystem '{id}' ({size_gb:.1f} GB) has had 0 client connections over the last 14 days
> [and/or has no mount targets]. It appears unused and is costing ~${current_mo:.2f}/month.
> Verify it is not required, then delete it to stop incurring charges."

---

## Opportunity 4 — Multi-AZ on Non-Production (One Zone Opportunity)

**Severity:** 🟡 MEDIUM
**Opportunity type:** `efs-single-az-opportunity`

**Detection logic:**
- `AvailabilityZoneName` is **absent** (Multi-AZ Standard filesystem).
- Instance or filesystem tags suggest a non-production environment:
  - `Environment` / `env` / `stage` tag contains `dev`, `test`, `staging`, `qa`, `uat`, `sandbox`

EFS One Zone stores data in a single AZ, which is perfectly adequate for non-production
workloads. One Zone costs **$0.192/GB-month vs $0.36/GB-month** for Standard — **47% cheaper**.

**Calculation:**
```
size_gb    = fs["SizeInBytes"]["Value"] / (1024 ** 3)
current_mo = size_gb * STANDARD_GB_MONTH      # $0.36
target_mo  = size_gb * ONE_ZONE_GB_MONTH      # $0.192
saving_mo  = current_mo - target_mo
```

**Recommendation:**
> "EFS filesystem '{id}' is Multi-AZ Standard but appears to be a non-production filesystem
> (env='{env_tag}'). One Zone storage costs $0.192/GB vs $0.36/GB — 47% cheaper. Migrating to
> One Zone (same AZ as your compute) would save ~${saving}/month. Use `aws efs create-file-system
> --availability-zone-name` and perform a DataSync copy."

---

## Opportunity 5 — Intelligent-Tiering Savings Estimate

**Severity:** 🟢 LOW
**Opportunity type:** `efs-intelligent-tiering-savings`

**Detection logic:**
- Filesystem has a lifecycle policy (IA transitions enabled) but is currently in Standard
  storage class (i.e., has non-trivial `SizeInBytes`).
- **Or** provide a general estimate for any filesystem with a lifecycle policy already enabled,
  showing the potential IA savings if usage patterns shift over time.
- This is an informational / advisory opportunity — no action may be needed if lifecycle is
  already configured.

Estimate what fraction of data *could* move to IA based on total size and actual I/O patterns.
If `DataReadIOBytes + DataWriteIOBytes` is low relative to `SizeInBytes`, much of the data
is likely cold and eligible for IA.

**Calculation:**
```
size_gb           = fs["SizeInBytes"]["Value"] / (1024 ** 3)
io_bytes_14d      = sum(read_datapoints["Sum"]) + sum(write_datapoints["Sum"])
# Rough cold fraction: if < 1% of data is accessed per day, call it 70% cold
bytes_per_day     = io_bytes_14d / 14
daily_access_pct  = bytes_per_day / fs["SizeInBytes"]["Value"] * 100
cold_fraction     = 0.70 if daily_access_pct < 1 else 0.40

current_mo  = size_gb * STANDARD_GB_MONTH
ia_mo       = (size_gb * (1 - cold_fraction) * STANDARD_GB_MONTH
             + size_gb * cold_fraction        * STANDARD_IA_GB_MONTH)
saving_mo   = current_mo - ia_mo
```

**Recommendation:**
> "EFS filesystem '{id}' ({size_gb:.1f} GB) is accessing ~{daily_access_pct:.2f}% of its data
> per day — suggesting ~{cold_fraction*100:.0f}% is cold. If Intelligent-Tiering were enabled,
> estimated savings would be ~${saving}/month (${saving*12:.0f}/year). Enable a lifecycle policy
> via `put-lifecycle-configuration` with `TransitionToIA = AFTER_30_DAYS`."

---

## Notes

- `SizeInBytes["Value"]` is in **bytes** — always divide by `1024³` to convert to GB.
- CloudWatch metrics may return no datapoints for empty or brand-new filesystems — handle
  gracefully (treat as 0 connections, no I/O).
- Tag-based filtering: skip filesystems with tag `cost-analyser:ignore = true`.
- One Zone filesystems are identified by the presence of `AvailabilityZoneName` in the
  `describe_file_systems` response.
- Provisioned throughput under-utilisation check requires both `PermittedThroughput` and
  `MeteredIOBytes` metrics. If `PermittedThroughput` is unavailable, fall back to the
  `ProvisionedThroughputInMibps` field from the API response.
