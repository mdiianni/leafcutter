# Skill: Amazon ECS — Cost-Saving Opportunities

---

## 🧪 Real-World Learnings (validated 2026-03-23, eu-central-1, account `<YOUR_ACCOUNT_ID>`)

These corrections come from running `analyse_ecs.py` against a live AWS account and comparing
results with actual AWS Console data and Cost Explorer.

### 1. CPU Max > 100% — Normal Fargate Bursting
**Observed:** Many services showed `CpuMax > 100%` (up to 117%) in CloudWatch despite being
provisioned at e.g. 2 vCPU. This is expected — Fargate allows CPU bursting above the allocated
reservation for brief spikes, similar to EC2 burstable instances. **Do not flag these as errors.**
The average (1–2%) being well below p95 is the meaningful signal for right-sizing.

### 2. Memory is Severely Overprovisioned (common pattern)
**Observed:** 46 out of 48 services had avg CPU < 2% and avg memory < 25%, yet all were provisioned
at 2 vCPU / 4 GB (or higher). Standard JVM-based microservices with `-Xmx` limits rarely use more
than 20–30% of allocated memory in dev. Memory overprovisioning accounts for more total cost than
CPU overprovisioning per service.

### 3. Auto-Scaling Locked at min==max Is Very Common
**Observed:** 31 of 47 services had Application Auto Scaling registered but `min==max==1`.
This is a common IaC default — Terraform ECS module sets min/max from the same variable.
When flagging this, recommend setting `min=1, max=3` with CPUUtilization target tracking (60%
target) as the minimal useful change. These services will save zero without this fix first.

### 4. Zombie Service Definition
**Validated:** A zombie service is `status=ACTIVE AND desired > 0 AND running == 0`.
A service with `desired=0` is intentionally stopped and should be listed separately (not as a cost
opportunity, since Fargate cost is zero when desired=0). The analyser `continue`s past zombies to
skip right-sizing analysis on non-running services.

### 5. CloudWatch Metrics Exist for Zombie Services (Historical)
**Observed:** `ctpa-subscr-zinvoice-dev-eu-central-1` had `desired=1, running=0` but showed
CpuAvg=45%, CpuMax=107% in CloudWatch — these are **historical** metrics from when it was running.
This is not an issue in the analyser. If a service is a zombie, skip metric-based right-sizing.

### 6. Org Savings Plans Cover Fargate (Compute SP)
**Confirmed:** AWS Compute Savings Plans cover Fargate, EC2, and Lambda. In this account, 100%
coverage with 51.3% effective discount. Call `ce.get_cost_and_usage(GroupBy=RECORD_TYPE)` and look
for `SavingsPlanCoveredUsage` — same pattern as the EC2 analyser. The `collect_sp_data()` function
from `analyse_ec2.py` is reusable. The effective cost after SP is ~$2,428/month vs $4,983 gross.

### 7. Auto-Scaling May Show 0 Scalable Targets for Services with desired==1
**Observed:** `ctpa-utils-springconfig-dev-eu-central-1` had `desired=2/running=2` but no
auto-scaling — this is correctly detected as `ecs-no-autoscaling`. However, the `min==max` detection
must only trigger when auto-scaling IS registered (i.e., `describe_scalable_targets` returns a result).

### 8. Valid Fargate CPU/Memory Combinations
**Important:** When suggesting smaller task sizes, only suggest valid Fargate CPU/memory combos.
See `FARGATE_VALID` dict in `analyse_ecs.py`. Invalid combos will cause task launch failures.
Most commonly: `256 CPU` only supports up to `2048 MB` memory; `512 CPU` up to `4096 MB`.

### Validated Fargate Pricing (eu-central-1, 2026-03):
| Architecture | vCPU/hr  | GB/hr    | Source |
|---|---|---|---|
| x86_64       | $0.04868 | $0.00532 | AWS Fargate Pricing page |
| ARM64        | $0.03874 | $0.00423 | AWS Fargate Pricing page (~20% less) |

### Validated Analysis Results (eu-central-1, 2026-03-23):
- **48 services** across 6 clusters, all Fargate, all X86_64, all Container Insights enabled
- **Gross Fargate cost:** $4,983/month; **Effective after Org SP:** ~$2,428/month
- **Additional savings opportunities identified:** $2,935/month ($35,224/year)
- **Top opportunity:** CPU overprovisioning — 46 services, $1,848/month
- **Graviton migration:** 47 services, $1,000/month (⚠️ ARM64 build required)
- **1 zombie service:** `ctpa-subscr-zinvoice-dev-eu-central-1` ($87/month)
- **31 services locked** with auto-scaling min==max (no cost saving until unlocked)

---

## Service Overview

Amazon ECS runs containerised workloads either on **Fargate** (serverless compute) or **EC2
launch type**. Cost opportunities arise from overprovisioning of CPU/memory, inefficient launch
types, and lack of Graviton/Spot usage.

---

## Data Collection

### AWS API Calls

```python
ecs = session.client("ecs", region_name=region)
cw  = session.client("cloudwatch", region_name=region)
app_autoscaling = session.client("application-autoscaling", region_name=region)

# List all clusters
ecs.list_clusters()

# Describe all services per cluster
ecs.list_services(cluster=cluster_arn)
ecs.describe_services(cluster=cluster_arn, services=[...])  # up to 10 per call

# Describe task definitions for each unique task def ARN
ecs.describe_task_definition(taskDefinition=task_def_arn)

# Auto-scaling configuration
app_autoscaling.describe_scalable_targets(
    ServiceNamespace="ecs",
    ResourceIds=[f"service/{cluster_name}/{service_name}"]
)

# CloudWatch CPU utilisation per service (last 15 days)
cw.get_metric_statistics(
    Namespace="AWS/ECS",
    MetricName="CPUUtilization",
    Dimensions=[
        {"Name": "ClusterName", "Value": cluster_name},
        {"Name": "ServiceName", "Value": service_name},
    ],
    StartTime=now - timedelta(days=15),
    EndTime=now,
    Period=3600,
    Statistics=["Average", "Maximum", "p95"]
)

# CloudWatch Memory utilisation per service (last 15 days)
cw.get_metric_statistics(
    Namespace="AWS/ECS",
    MetricName="MemoryUtilization",
    ...  # same pattern as above
)
```

### Cache TTL
- ECS service + task definition metadata: **24 hours**
- CloudWatch CPU/MEM metrics: **6 hours**

> 💡 CloudWatch ECS metrics are only available if the ECS cluster has **Container Insights** enabled.
> If metrics are unavailable, downgrade to a `LOW` severity and note "Enable Container Insights
> for detailed utilisation analysis."

---

## Opportunity 1 — Overprovisioned CPU

**Severity:** 🟠 HIGH  
**Opportunity type:** `ecs-cpu-overprovision`

**Detection logic:**
Using CloudWatch `CPUUtilization` (Average and p95) over 15 days:

| Condition | Action |
|---|---|
| Average CPU < 10% AND p95 CPU < 25% | Suggest reducing CPU reservation by 50% |
| Average CPU < 20% AND p95 CPU < 40% | Suggest reducing CPU reservation by 25% |
| Average CPU < 30% AND p95 CPU < 60% | Flag as informational (LOW) |

CPU reservation is defined in the task definition `cpu` field (in CPU units; 1024 = 1 vCPU).

**Calculation (Fargate):**
```
current_vcpu      = task_def_cpu / 1024
suggested_vcpu    = max(0.25, current_vcpu * reduction_factor)
# Round to nearest valid Fargate CPU value: 0.25, 0.5, 1, 2, 4, 8, 16
price_per_vcpu_hr = pricing["fargate"]["cpu_per_vcpu_hour"]
task_count        = service_desired_count
monthly_saving    = (current_vcpu - suggested_vcpu) * price_per_vcpu_hr * 730 * task_count
```

**Recommendation:**
> "ECS service '{service}' in cluster '{cluster}' has average CPU of {avg_cpu}% (p95: {p95_cpu}%)
> over 15 days, but is provisioned for {current_vcpu} vCPU. Reducing to {suggested_vcpu} vCPU
> could save ~${saving}/month across {task_count} tasks."

---

## Opportunity 2 — Overprovisioned Memory

**Severity:** 🟠 HIGH  
**Opportunity type:** `ecs-memory-overprovision`

**Detection logic:**
Using CloudWatch `MemoryUtilization` (Average and Maximum) over 15 days:

| Condition | Action |
|---|---|
| Average MEM < 20% AND max < 40% | Suggest reducing memory reservation by 40% |
| Average MEM < 30% AND max < 50% | Suggest reducing memory reservation by 25% |

Memory reservation is defined in `memory` (in MB).

> ⚠️ **Caution:** Always retain headroom above the maximum observed. Recommended floor:
> `max_observed * 1.3` (30% headroom).

**Calculation (Fargate):**
```
current_gb        = task_def_memory / 1024
suggested_gb      = max(0.5, max_observed_pct/100 * current_gb * 1.3)
# Round up to nearest valid Fargate memory value
price_per_gb_hr   = pricing["fargate"]["mem_per_gb_hour"]
monthly_saving    = (current_gb - suggested_gb) * price_per_gb_hr * 730 * task_count
```

**Recommendation:**
> "ECS service '{service}' has average memory of {avg_mem}% (max: {max_mem}%) over 15 days,
> but is provisioned for {current_gb} GB. Reducing to {suggested_gb} GB (30% headroom above max)
> could save ~${saving}/month."

---

## Opportunity 3 — No Auto-Scaling Configured

**Severity:** 🟡 MEDIUM  
**Opportunity type:** `ecs-no-autoscaling`

**Detection logic:**
- `describe_scalable_targets` returns **no targets** for the service.
- Service `desired_count > 1`.
- Without auto-scaling, the service runs at peak capacity 24/7.

**Recommendation:**
> "ECS service '{service}' has {desired_count} tasks but no Application Auto Scaling configured.
> Adding a target tracking policy on CPUUtilization (target 60%) could reduce average task count
> during off-peak hours, saving on Fargate compute."

---

## Opportunity 3b — Auto-Scaling Locked (min == max)

**Severity:** 🟡 MEDIUM  
**Opportunity type:** `ecs-autoscaling-locked`

**Detection logic:**
- `describe_scalable_targets` returns a target for the service (auto-scaling IS registered).
- `MinCapacity == MaxCapacity` → tasks cannot scale in or out.
- This is a very common pattern when IaC tools (Terraform ECS module) set min/max from the same variable.

> 📌 **Real-world observation:** 31/48 services (65%) in a live account had this pattern.
> It effectively nullifies the auto-scaling registration.

**Recommendation:**
> "ECS service '{service}' has auto-scaling registered but min={min} == max={max}. This is
> effectively static scheduling. Set min=1, max=3 with a CPUUtilization target tracking policy
> (target 60%) to enable scale-in during off-peak hours."

---

## Opportunity 4 — Fargate: Move to Graviton (ARM64)

**Severity:** ⚠️ DEPENDENCY  
**Opportunity type:** `ecs-graviton-fargate`

**Detection logic:**
- Service uses Fargate launch type.
- Task definition `runtimePlatform.cpuArchitecture` is `X86_64` (or not set, which defaults to x86_64).
- Fargate Graviton (`ARM64`) is **~20% cheaper** than x86_64 for the same CPU/memory allocation.

> ⚠️ **Dependency warning:** Migrating to Fargate Graviton requires Docker images to be built
> for `linux/arm64`. Container images must be available as ARM64 manifests (multi-arch or arm64-only).
> All native dependencies, Lambda layers, and sidecars must also support ARM64.

**Calculation:**
```
fargate_x86_cpu_hr   = pricing["fargate"]["x86"]["cpu_per_vcpu_hour"]
fargate_arm_cpu_hr   = pricing["fargate"]["arm64"]["cpu_per_vcpu_hour"]
fargate_x86_mem_hr   = pricing["fargate"]["x86"]["mem_per_gb_hour"]
fargate_arm_mem_hr   = pricing["fargate"]["arm64"]["mem_per_gb_hour"]

current_monthly  = (vcpu * fargate_x86_cpu_hr + gb * fargate_x86_mem_hr) * 730 * task_count
arm_monthly      = (vcpu * fargate_arm_cpu_hr + gb * fargate_arm_mem_hr) * 730 * task_count
monthly_saving   = current_monthly - arm_monthly
```

**Recommendation:**
> "ECS service '{service}' runs {task_count} Fargate x86_64 tasks. Migrating to Fargate Graviton
> (ARM64) would save ~${saving}/month (~20%). ⚠️ Docker images must be rebuilt for linux/arm64."

---

## Opportunity 5 — EC2 Launch Type: Idle / Over-allocated Cluster Capacity

**Severity:** 🟠 HIGH  
**Opportunity type:** `ecs-ec2-cluster-idle`

**Detection logic:**
- Service uses EC2 launch type.
- Fetch `ClusterArn` → describe EC2 Container Instances.
- Compare registered CPU/memory vs currently reserved by tasks.
- If cluster CPU reservation < 30% across all container instances → flag idle capacity.

**Recommendation:**
> "ECS EC2 cluster '{cluster}' has {idle_pct}% of CPU capacity unused. Consider enabling
> ECS Cluster Auto Scaling (Capacity Providers) to scale in underutilised instances."

---

## Opportunity 6 — Zombie Service (desired > 0, running == 0)

**Severity:** 🟠 HIGH  
**Opportunity type:** `ecs-zombie-service`

**Detection logic:**
- `status = ACTIVE` AND `desiredCount > 0` AND `runningCount == 0`.
- The service is configured to run tasks but none are healthy. This wastes CPU/memory allocation
  on any associated load balancer target groups and may incur ECS scheduling costs.
- **Do NOT confuse** with intentionally stopped services (`desiredCount = 0`) — those are fine.
- **Do NOT continue to right-size** zombie services — skip metric analysis for these.

> 📌 **Real-world observation:** CloudWatch may still return historical metrics for a zombie service
> (from when it was last running). Do not use these metrics for sizing analysis.

**Recommendation:**
> "Service '{service}' has desiredCount={desired} but 0 running tasks. Investigate stopped task
> events in the ECS console. If no longer needed, set desiredCount=0 or delete the service to
> eliminate scheduling overhead and ALB target group costs."

---

## Opportunity 7 — Container Insights Not Enabled

**Severity:** 🟢 LOW  
**Opportunity type:** `ecs-no-container-insights`

**Detection logic:**
- `describe_clusters` → `settings` does not include `{"name": "containerInsights", "value": "enabled"}`.

**Recommendation:**
> "ECS cluster '{cluster}' does not have Container Insights enabled. Without it, CPU and memory
> utilisation metrics per service are unavailable, making right-sizing analysis impossible.
> Enable Container Insights to unlock cost optimisation data."

> Note: Container Insights has a small cost (~$0.50/million metrics). Include this in the note.

---

## Pricing Sources

Fargate pricing (fetch from AWS Pricing API or documentation for target region):
- `ServiceCode = "AmazonECS"`
- Fargate CPU: per-vCPU-hour
- Fargate Memory: per-GB-hour
- ARM64 rates: approximately 20% less than x86_64

---

## Notes

- Only analyse services with `status = "ACTIVE"` and `desiredCount > 0`.
- Services with `desiredCount > 0` but `runningCount == 0` → **zombie** (flag as HIGH, skip right-sizing).
- Services with `desiredCount = 0` → intentionally stopped, Fargate cost = $0, skip entirely.
- Container Insights metrics have a **~5-minute delay**; use 1-hour periods for trend analysis.
- **CPU Max > 100% is expected** — Fargate bursting above allocation for brief spikes is normal. Do not flag.
- **Org Savings Plans cover Fargate** (Compute SP) — reuse `collect_sp_data()` from `analyse_ec2.py` to show
  gross cost vs effective post-SP cost in the report header.
- Always validate suggested task sizes against `FARGATE_VALID` — only valid CPU/memory combos are safe to suggest.
- Tag-based filtering: skip services with tag `cost-analyser:ignore = true`.
