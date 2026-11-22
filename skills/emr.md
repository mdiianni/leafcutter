# Skill: Amazon EMR — Cost-Saving Opportunities

## Service Overview

Amazon EMR (Elastic MapReduce) charges for **EC2 instance hours** (master, core, and task
nodes) plus an **EMR service fee** per instance-hour on top of EC2 pricing. There is no
charge for the control plane itself — all cost is driven by the running EC2 fleet.

Key cost characteristics:
- **Master node** runs 24/7 for the cluster lifetime regardless of whether steps are executing
- **Core nodes** hold HDFS data and must be sized for peak storage + compute requirements
- **Task nodes** are optional, stateless compute — ideal for Spot and elastic scaling
- Persistent clusters accrue cost continuously; transient (job-scoped) clusters only bill
  while provisioned

Primary savings levers: terminate idle clusters, migrate task nodes to Spot, adopt Graviton
instance types, right-size core nodes based on YARN memory headroom, and evaluate transient
or EMR Serverless patterns for batch workloads.

---

## Data Collection

### AWS API Calls

```python
emr = session.client("emr", region_name=region)
cw  = session.client("cloudwatch", region_name=region)

# List all active clusters
emr.list_clusters(ClusterStates=["WAITING", "RUNNING"])  # paginated

# Full cluster detail (status timeline, configuration)
emr.describe_cluster(ClusterId=cluster_id)

# Instance groups per cluster (roles: MASTER, CORE, TASK; market: ON_DEMAND/SPOT)
emr.list_instance_groups(ClusterId=cluster_id)  # paginated

# Active steps per cluster
emr.list_steps(ClusterId=cluster_id, StepStates=["RUNNING", "PENDING"])

# CloudWatch: workload indicator (15 days, hourly)
cw.get_metric_statistics(
    Namespace="AWS/ElastiMapReduce",
    MetricName="TotalLoad",
    Dimensions=[{"Name": "JobFlowId", "Value": cluster_id}],
    StartTime=now - timedelta(days=15),
    EndTime=now,
    Period=3600,
    Statistics=["Average", "Maximum"]
)

# CloudWatch: YARN memory availability (15 days, hourly)
cw.get_metric_statistics(
    Namespace="AWS/ElastiMapReduce",
    MetricName="YARNMemoryAvailablePercentage",
    ...
)
```

### Cache TTL
- Cluster metadata + instance groups + active steps: **24 hours**
- CloudWatch metrics: **6 hours**

---

## Opportunity 1 — Idle Clusters

**Severity:** 🔴 CRITICAL
**Opportunity type:** `emr-idle-cluster`

**Detection logic:**
- `TotalLoad` average < 0.1 over the **last 7 days** (CloudWatch `AWS/ElastiMapReduce`)
- AND `list_steps` returns 0 RUNNING or PENDING steps

`TotalLoad` reflects the number of active YARN containers across all nodes. A value < 0.1
over a full week means no meaningful work has run. The cluster is incurring full On-Demand
EC2 cost 24/7 for zero productive output.

**Saving:** Full cluster monthly cost (master + core + task nodes).

**Calculation:**
```python
IDLE_THRESHOLD     = 0.1
window_days        = 7
total_load_7d_avg  = cw_avg("TotalLoad", days=window_days)
saving_mo          = cluster_monthly_cost   # 100% saving on termination
```

**Recommendation:**
> "Cluster '{name}' has TotalLoad avg {load:.3f} over 7 days with no active steps.
> Terminate the cluster immediately. Annual waste at current rate: ~${annual}."

---

## Opportunity 2 — On-Demand Task Nodes

**Severity:** 🟡 MEDIUM
**Opportunity type:** `emr-spot-task-nodes`

**Detection logic:**
- Instance group with `InstanceGroupType = "TASK"` and `Market = "ON_DEMAND"`
- `RunningInstanceCount > 0`

EMR task nodes are stateless (no HDFS data) and can be interrupted and re-scheduled by YARN.
They are the ideal Spot target within an EMR cluster. Spot task nodes save 60–80% over
On-Demand for the same instance type.

> ⚠️ **Dependency warning:** Spot interruption during a Step causes YARN to re-schedule
> affected containers. If the application does not tolerate partial container failure
> (e.g. no checkpointing, non-idempotent tasks), step failure is possible. Configure
> EMR step retry and use diversified instance types to mitigate.

**Calculation:**
```python
SPOT_DISCOUNT  = 0.70   # mid-point of 60–80% saving
task_cost_mo   = sum(ig_monthly_cost for ig in task_groups if ig["market"] == "ON_DEMAND")
saving_mo      = task_cost_mo * SPOT_DISCOUNT
```

**Recommendation:**
> "Cluster '{name}' has {n}×{type} On-Demand TASK nodes costing ${task_cost}/month.
> Switching to Spot saves ~70% (~${saving}/month). Master and Core nodes remain
> On-Demand. Use an instance fleet with diversified types to reduce interruption risk."

---

## Opportunity 3 — Graviton Migration

**Severity:** 🟠 HIGH (MASTER/CORE), 🟡 MEDIUM (TASK)
**Opportunity type:** `emr-graviton-migration`

**Detection logic:**
- Instance type family matches an x86 type with a Graviton equivalent:

| x86 family | Graviton equivalent | Saving |
|---|---|---|
| m5 / m6i | m6g | ~15% |
| r5 / r6i | r6g | ~15% |
| c5 / c6i | c6g | ~15% |

- Severity is **HIGH** for MASTER and CORE groups (dominant cost), **MEDIUM** for TASK groups.
- No dependency warning: EMR runs the same JVM/Python runtime on Graviton — no application
  or library changes are required.

**Calculation:**
```python
GRAVITON_DISCOUNT = 0.15
saving_mo         = ig_monthly_cost * GRAVITON_DISCOUNT
```

**Recommendation:**
> "Instance group '{group}' ({role}) runs {n}×{type}. Migrating to {graviton_type}
> saves ~15% (~${saving}/month). No application changes required — same JVM/Python runtime.
> Clone the cluster with updated instance types to apply."

---

## Opportunity 4 — Oversized Cluster

**Severity:** 🟡 MEDIUM
**Opportunity type:** `emr-oversized-cluster`

**Detection logic (requires active steps to be meaningful):**
- `YARNMemoryAvailablePercentage` average > 60% over **15 days** (CloudWatch)
- AND cluster has active steps (`active_step_count > 0`)

When more than 60% of YARN memory is consistently available, the cluster has been
over-provisioned for its actual workload. Downsizing core nodes one instance tier or
reducing the core node count releases that headroom cost.

**Calculation:**
```python
YARN_OVERSIZED_THRESHOLD = 60.0   # %
saving_mo = cluster_monthly_cost * 0.25   # ~25% saving by reducing cluster ~one tier
```

**Recommendation:**
> "Cluster '{name}' has average YARNMemoryAvailablePercentage of {pct:.1f}% over 15 days.
> Downsize Core nodes from {current_type} → {smaller_type} (one tier) or reduce Core node
> count. Validate memory headroom at peak step execution before resizing."

---

## Opportunity 5 — Persistent Cluster vs Transient

**Severity:** 🟠 HIGH
**Opportunity type:** `emr-persistent-to-transient`

**Detection logic:**
- Cluster `ReadyDateTime` age > **30 days**
- AND `active_step_count == 0` (currently idle between jobs)
- AND not already flagged as idle (Opp 1 is more severe for fully idle clusters)

A persistent cluster that has been running for over a month with no current activity is
paying for master and core nodes continuously, even during the gaps between batch jobs.

**Transient pattern:**
Provision a new cluster per job submission → run step(s) → terminate on completion.
Bootstrap scripts and EMR configurations can be parameterised via Step Functions or
AWS Batch with EMR plugin.

**Saving estimate:** ~40% of monthly cost (assuming 40% idle time between jobs).

**Calculation:**
```python
IDLE_FRACTION = 0.40
saving_mo     = cluster_monthly_cost * IDLE_FRACTION
```

**Recommendation:**
> "Cluster '{name}' has been running for {days} days with no active steps. A persistent
> cluster bills 24/7. Switching to transient clusters (provision-on-submit,
> terminate-on-complete) eliminates idle master/core costs. Consider EMR Serverless
> for serverless batch execution without cluster lifecycle management."

---

## Opportunity 6 — EMR Serverless Opportunity

**Severity:** 🟡 MEDIUM
**Opportunity type:** `emr-serverless-opportunity`

**Detection logic:**
- Cluster status is `WAITING` (between jobs, not actively running steps)
- Cluster uptime > **7 days**
- `TotalLoad` average over 15 days < **5.0** (low sustained utilisation)
- Not already flagged as idle (Opp 1) or persistent (Opp 5)

A WAITING cluster with chronically low TotalLoad indicates short batch jobs with long idle
gaps. The master node runs 24/7 but is only utilised for job duration.

**EMR Serverless advantages for this pattern:**
- Bills per-vCPU-second and per-GB-second **only during job execution**
- No master node or cluster lifecycle to manage
- Pre-initialised capacity available within seconds
- Supports Spark, Hive, and other frameworks natively

**Saving estimate:** ~80% of master node monthly cost (master is idle most of the time).

**Calculation:**
```python
master_mo = sum(ig["monthly_cost"] for ig in cluster["instance_groups"]
                if ig["instance_role"] == "MASTER")
saving_mo = master_mo * 0.80
```

**Recommendation:**
> "Cluster '{name}' is WAITING with TotalLoad avg {load:.2f} — short batch jobs with long
> idle gaps. Master nodes cost ${master_mo}/month while idle. EMR Serverless eliminates
> persistent master cost by billing only during job execution. Estimated saving: ${saving}/month."

---

## Pricing References (eu-central-1, On-Demand)

| Instance type | $/hr (EC2+EMR est.) | $/month (730 hr) |
|---|---|---|
| m5.xlarge     | $0.222 | $162  |
| m5.2xlarge    | $0.444 | $324  |
| m5.4xlarge    | $0.888 | $648  |
| m6g.xlarge    | $0.192 | $140  |
| m6g.2xlarge   | $0.384 | $280  |
| m6g.4xlarge   | $0.768 | $561  |
| r5.xlarge     | $0.296 | $216  |
| r5.2xlarge    | $0.592 | $432  |
| r5.4xlarge    | $1.184 | $864  |
| r6g.xlarge    | $0.252 | $184  |
| r6g.2xlarge   | $0.504 | $368  |
| r6g.4xlarge   | $1.008 | $736  |
| c5.xlarge     | $0.192 | $140  |
| c5.2xlarge    | $0.384 | $280  |
| c6g.xlarge    | $0.163 | $119  |
| c6g.2xlarge   | $0.326 | $238  |

Spot pricing varies by availability zone and time — typical savings are 60–80% vs On-Demand.
Fetch live Spot prices via `ec2.describe_spot_price_history(InstanceTypes=[...])`.

---

## Transient vs Persistent Cluster Strategy

### Persistent cluster (always-on)
- **Pros:** Fast job start (no provisioning delay), HDFS data retained between jobs,
  useful for interactive workloads (Hue, notebooks, Spark Shell)
- **Cons:** Master + Core nodes billed 24/7 regardless of step activity; accumulates
  cost during nights, weekends, and gaps between jobs

### Transient cluster (job-scoped)
- **Pros:** Zero cost when no job is running; natural cost boundary per job run
- **Cons:** 3–10 minute provisioning delay per job; HDFS data must be externalised
  to S3 (use `s3://` paths throughout); bootstrap actions re-run every launch
- **Best for:** Scheduled batch jobs, ETL pipelines, infrequent large-scale processing

### Decision guide

| Signal | Recommendation |
|--------|---------------|
| Cluster idle > 30 days, no active steps | Switch to transient |
| TotalLoad avg < 5.0 over 15 days | Evaluate EMR Serverless |
| Interactive notebooks / long-lived Spark sessions | Keep persistent, right-size |
| Nightly batch with predictable schedule | Transient via Step Functions or AWS Batch |
| Ad-hoc queries, data exploration | EMR Serverless (on-demand, no cluster mgmt) |

---

## EMR Serverless as Alternative for Batch Workloads

EMR Serverless (`emr-serverless`) is the recommended migration path for:
1. **Short batch jobs** that run infrequently (< 2 hr/day utilisation on average)
2. **Scheduled ETL pipelines** currently running on persistent clusters
3. **Teams that want to avoid cluster lifecycle management** entirely

### Pricing model (eu-central-1)
- vCPU: ~$0.052624 / vCPU-hour during job execution
- Memory: ~$0.005782 / GB-hour during job execution
- Storage: ~$0.000111 / GB-hour (ephemeral shuffle storage)
- **No charge when idle** — the application exists but costs nothing between runs

### Migration checklist
- [ ] Replace HDFS paths with `s3://` in all Spark/Hive job configs
- [ ] Package custom dependencies as a `.tar.gz` and upload to S3
- [ ] Create an EMR Serverless Application (`emr-serverless create-application`)
- [ ] Submit jobs via `emr-serverless start-job-run` (SDK, CLI, or Step Functions)
- [ ] Configure pre-initialised capacity for latency-sensitive jobs

---

## Notes

- EMR clusters in WAITING state still bill for all running EC2 instances.
- Tag-based filtering: skip clusters with tag `cost-analyser:ignore = true`.
- The EMR service surcharge (on top of EC2 pricing) is ~25–30% of EC2 cost. The
  `INSTANCE_PRICE_HR` constants in `analyse_emr.py` include an approximate blended rate.
- For reserved instance savings on long-lived persistent clusters: cross-reference the
  EC2 skill — EMR uses standard EC2 Reserved Instances, not an EMR-specific RI model.
- HDFS replication factor (default 3) means Core node storage is consumed at 3× the
  logical dataset size; factor this into right-sizing core node disk capacity.
