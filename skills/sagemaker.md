# Skill: Amazon SageMaker — Cost-Saving Opportunities

## Service Overview

Amazon SageMaker costs span several billing dimensions: **notebook instances** (per-hour
compute while InService), **real-time inference endpoints** (per-instance-hour regardless of
traffic), **training jobs** (per-instance-second of compute), **Studio kernel apps** (per-hour
per kernel gateway instance), and **storage** (EBS volumes attached to notebooks, S3 for
model artefacts). The dominant waste patterns are idle notebooks and endpoints that bill
continuously even when unused.

---

## Pricing Components

### Notebook Instances (eu-central-1, On-Demand per hour)

| Instance Type   | $/hr    | $/month (730 hr) |
|-----------------|---------|-----------------|
| ml.t3.medium    | $0.059  | $43.07          |
| ml.t3.large     | $0.118  | $86.14          |
| ml.t3.xlarge    | $0.236  | $172.28         |
| ml.m5.xlarge    | $0.269  | $196.37         |
| ml.m5.2xlarge   | $0.538  | $392.74         |
| ml.m5.4xlarge   | $1.075  | $785.25         |
| ml.m6g.xlarge   | $0.227  | $165.71         |
| ml.m6g.2xlarge  | $0.454  | $331.42         |
| ml.p3.2xlarge   | $4.234  | $3,090.82       |
| ml.p3.8xlarge   | $16.934 | $12,361.82      |

> ⚠️ Notebook instances bill from the moment they are **InService** until **Stopped** or
> **Deleted**, regardless of whether a browser tab is open or any kernel is running.

### Real-Time Inference Endpoints (eu-central-1, On-Demand per hour)

| Instance Type   | $/hr    | $/month (730 hr) |
|-----------------|---------|-----------------|
| ml.t2.medium    | $0.064  | $46.72          |
| ml.m5.large     | $0.134  | $97.82          |
| ml.m5.xlarge    | $0.269  | $196.37         |
| ml.c5.large     | $0.119  | $86.87          |
| ml.c5.xlarge    | $0.238  | $173.74         |
| ml.m6g.large    | $0.117  | $85.41          |
| ml.c6g.large    | $0.093  | $67.89          |

> Endpoints bill per-instance-hour for **every** instance in the production variant,
> even at 0 requests/second. Multi-variant endpoints (A/B test) bill all variants
> simultaneously.

### Training Jobs

Training billing is per-**instance-second** of actual compute time, from job start to job end.
Managed Spot Training discounts are 70–90% of the On-Demand rate. The Spot price fluctuates
but is bounded by `MaxSpotInstanceInterruptions` retries.

### SageMaker Studio Kernel Gateway Apps

Each `KernelGateway` app in Studio launches a dedicated EC2-backed instance billed at
notebook instance rates. Apps persist (and bill) until explicitly shut down — closing the
browser tab does **not** stop the kernel.

### Storage

- Notebook EBS volumes: standard EBS gp2/gp3 pricing (~$0.10/GB-month) regardless of status
- Model artefacts in S3: standard S3 pricing
- Feature Store: offline store (S3) + online store (in-memory) billed separately

---

## Data Collection

### AWS API Calls

```python
sm = session.client("sagemaker", region_name=region)
cw = session.client("cloudwatch", region_name=region)

# Notebook instances
sm.list_notebook_instances()                           # paginated
sm.describe_notebook_instance(NotebookInstanceName=n) # for EBS size, lifecycle config

# Endpoints
sm.list_endpoints()                                    # paginated
sm.describe_endpoint(EndpointName=ep_name)             # for instance type per variant
sm.describe_endpoint_config(EndpointConfigName=cfg)    # for ProductionVariants[].InstanceType

# Training jobs (last 30 days)
sm.list_training_jobs(
    StatusEquals="Completed",
    CreationTimeAfter=thirty_days_ago,
)                                                      # paginated
sm.describe_training_job(TrainingJobName=name)
# Key fields: EnableManagedSpotTraining, ResourceConfig.InstanceType, ResourceConfig.InstanceCount

# Studio domains
sm.list_domains()                                      # paginated
sm.list_apps(DomainIdEquals=domain_id)                 # paginated; returns AppType, Status,
                                                       # LastUserActivityTimestamp

# CloudWatch: CPU utilisation for notebook instances (15-day window)
cw.get_metric_statistics(
    Namespace="AWS/SageMaker",
    MetricName="CPUUtilization",
    Dimensions=[{"Name": "NotebookInstanceName", "Value": nb_name}],
    StartTime=now - timedelta(days=15),
    EndTime=now,
    Period=3600,
    Statistics=["Average", "Maximum"],
)

# CloudWatch: Invocation count for endpoints (7-day window)
cw.get_metric_statistics(
    Namespace="AWS/SageMaker",
    MetricName="Invocations",
    Dimensions=[{"Name": "EndpointName", "Value": ep_name}],
    StartTime=now - timedelta(days=7),
    EndTime=now,
    Period=3600,
    Statistics=["Sum"],
)
```

### Instance type on endpoints

The instance type is **not** on the `list_endpoints()` response — it requires:

```python
ep = sm.describe_endpoint(EndpointName=ep_name)
config_name = ep["EndpointConfigName"]
cfg = sm.describe_endpoint_config(EndpointConfigName=config_name)
instance_type = cfg["ProductionVariants"][0]["InstanceType"]
```

### Cache TTL
- Notebook / endpoint / domain metadata: **24 hours**
- CloudWatch metrics: **6 hours** (fetched in parallel with `ThreadPoolExecutor`)

---

## Opportunity 1 — Idle Notebook Instances (InService, Low CPU)

**Severity:** 🔴 CRITICAL  
**Opportunity type:** `sagemaker-idle-notebook`

**Detection logic:**
- `NotebookInstanceStatus == "InService"`
- CloudWatch `CPUUtilization` average < 5% over the last **15 days**

**Rationale:** Notebook instances charge per hour from `InService` to `Stopped`. An idle
notebook at `ml.m5.xlarge` costs $196/month with zero productive use.

**Saving calculation:**
```python
saving_mo = NOTEBOOK_PRICE_HR[instance_type] * 730  # full monthly cost is the saving
```

**Recommendation:**
> "Stop or delete notebook '{name}' ({instance_type}, ${price_hr}/hr). It has been InService
> with {cpu_avg}% avg CPU for 15 days — cost is ${monthly_cost}/month with no utilisation.
> For interactive development, migrate to SageMaker Studio which charges only while a kernel
> is actively running."

**Implementation:**
```python
sm.stop_notebook_instance(NotebookInstanceName=name)
# or for permanent removal:
sm.delete_notebook_instance(NotebookInstanceName=name)
# (instance must be Stopped before delete)
```

---

## Opportunity 2 — Idle Real-Time Endpoints (Zero Invocations)

**Severity:** 🔴 CRITICAL  
**Opportunity type:** `sagemaker-idle-endpoint`

**Detection logic:**
- `EndpointStatus == "InService"`
- CloudWatch `Invocations` **sum** over 7 days == 0

**Rationale:** Real-time endpoints bill per-instance-hour. An endpoint with 0 traffic still
costs the full instance rate. Delete it and re-deploy on demand.

**Alternatives to always-on endpoints:**

| Option | Best for | Cost model |
|--------|----------|-----------|
| Real-time endpoint | Sustained high-frequency traffic | Per instance-hour |
| Serverless Inference | Sporadic, latency-tolerant | Per request + per GB processed |
| Async Inference | Large payloads, minutes acceptable | Per instance-hour only while processing |
| Batch Transform | Offline scoring on S3 datasets | Per instance-second of job |

**Saving calculation:**
```python
saving_mo = ENDPOINT_PRICE_HR[instance_type] * 730
```

---

## Opportunity 3 — On-Demand Training (Spot Not Enabled)

**Severity:** 🟡 MEDIUM  
**Opportunity type:** `sagemaker-ondemand-training`

**Detection logic:**
- `list_training_jobs(StatusEquals="Completed", CreationTimeAfter=30_days_ago)`
- `describe_training_job()["EnableManagedSpotTraining"] == False`

**Rationale:** SageMaker Managed Spot Training uses EC2 Spot capacity for training jobs,
saving 70–90% of compute cost. The trade-off is potential interruption — mitigated by
checkpointing.

**Enabling Spot Training:**
```python
sm.create_training_job(
    ...
    EnableManagedSpotTraining=True,
    StoppingCondition={
        "MaxRuntimeInSeconds":    7200,   # max actual training time
        "MaxWaitTimeInSeconds":  14400,   # max wall-clock time including interruptions
    },
    CheckpointConfig={
        "S3Uri": "s3://bucket/checkpoints/job-name/",
    },
)
```

**Checkpointing in framework estimators (Python SDK):**
```python
from sagemaker.pytorch import PyTorch

estimator = PyTorch(
    entry_point="train.py",
    use_spot_instances=True,
    max_run=7200,
    max_wait=14400,
    checkpoint_s3_uri="s3://bucket/checkpoints/",
    checkpoint_local_path="/opt/ml/checkpoints",
    ...
)
```

**In training script — save checkpoint periodically:**
```python
import torch, os

CHECKPOINT_DIR = os.environ.get("SM_CHECKPOINT_DIR", "/opt/ml/checkpoints")

def save_checkpoint(model, optimizer, epoch):
    path = os.path.join(CHECKPOINT_DIR, f"checkpoint-epoch-{epoch}.pt")
    torch.save({"epoch": epoch, "model": model.state_dict(),
                "optimizer": optimizer.state_dict()}, path)

def load_latest_checkpoint(model, optimizer):
    files = sorted(os.listdir(CHECKPOINT_DIR))
    if files:
        ckpt = torch.load(os.path.join(CHECKPOINT_DIR, files[-1]))
        model.load_state_dict(ckpt["model"])
        optimizer.load_state_dict(ckpt["optimizer"])
        return ckpt["epoch"]
    return 0
```

**Saving calculation:**
```python
# Approximate: based on job count and average duration per instance type
saving_mo = num_ondemand_jobs * avg_hourly_rate * avg_duration_hrs * SPOT_DISCOUNT
# SPOT_DISCOUNT = 0.70  (conservative; actual savings can be 70–90%)
```

---

## Opportunity 4 — Graviton Inference Endpoints

**Severity:** 🟠 HIGH  
**Opportunity type:** `sagemaker-graviton-endpoint`

**Detection logic:**
- `EndpointStatus == "InService"`
- Instance type family is `ml.m5`, `ml.c5`, or `ml.r5`

**Graviton instance mapping:**

| Current (x86) | Graviton (ARM64) | Saving |
|---------------|-----------------|--------|
| ml.m5.large   | ml.m6g.large    | ~15%   |
| ml.m5.xlarge  | ml.m6g.xlarge   | ~15%   |
| ml.m5.2xlarge | ml.m6g.2xlarge  | ~15%   |
| ml.c5.large   | ml.c6g.large    | ~15%   |
| ml.c5.xlarge  | ml.c6g.xlarge   | ~15%   |
| ml.r5.large   | ml.r6g.large    | ~15%   |
| ml.r5.xlarge  | ml.r6g.xlarge   | ~15%   |

**Validation checklist before migrating:**
- [ ] Serving container available as multi-arch (linux/amd64 + linux/arm64) or ARM64-only image
- [ ] AWS DLC (Deep Learning Container) used? TF/PyTorch DLCs are available for ARM64
- [ ] Custom C extensions or native dependencies compiled for x86? Rebuild for ARM64
- [ ] Deploy a shadow/canary Graviton endpoint alongside existing endpoint for A/B validation
- [ ] Monitor `ModelLatency` and `OverheadLatency` CW metrics for regression

**Saving calculation:**
```python
current_hr   = ENDPOINT_PRICE_HR[instance_type]
graviton_hr  = current_hr * (1 - GRAVITON_DISCOUNT)   # GRAVITON_DISCOUNT = 0.15
saving_mo    = (current_hr - graviton_hr) * 730
```

---

## Opportunity 5 — Studio Domain Idle Kernel Apps

**Severity:** 🟡 MEDIUM  
**Opportunity type:** `sagemaker-studio-idle-kernels`

**Detection logic:**
- `list_domains()` returns at least one domain
- `list_apps(DomainIdEquals=domain_id)` returns `KernelGateway` or `JupyterServer`
  apps with `Status == "InService"`
- `LastUserActivityTimestamp` on any app > 7 days ago (or absent)

**Rationale:** Each KernelGateway app in Studio runs a dedicated instance billed at notebook
instance rates. Closing a browser tab does **not** shut down the kernel — it persists billing
until explicitly terminated.

**Auto-shutdown lifecycle configuration:**
```python
# Create lifecycle config that auto-shuts idle kernels
lcc = sm.create_notebook_instance_lifecycle_config(
    NotebookInstanceLifecycleConfigName="auto-shutdown",
    OnStart=[{
        "Content": base64.b64encode(b"""
#!/bin/bash
# Install idle kernel auto-shutdown extension
pip install jupyter_contrib_nbextensions
jupyter contrib nbextension install --user
jupyter nbextension enable autosavetime/main
""").decode()
    }]
)

# For Studio: use the JupyterLab idle kernel shutdown setting
# Add to domain settings:
sm.update_domain(
    DomainId=domain_id,
    DefaultUserSettings={
        "JupyterServerAppSettings": {
            "LifecycleConfigArns": [lcc_arn],
        },
        "KernelGatewayAppSettings": {
            "LifecycleConfigArns": [lcc_arn],
        },
    },
)
```

**Manual shutdown via API:**
```python
sm.delete_app(
    DomainId=domain_id,
    UserProfileName=user_profile,
    AppType="KernelGateway",
    AppName=app_name,
)
```

**Saving calculation:**
```python
# Each active KernelGateway app runs at minimum an ml.t3.medium by default
app_cost_mo = active_kernel_apps * NOTEBOOK_PRICE_HR["ml.t3.medium"] * 730
saving_mo   = app_cost_mo  # full cost saved by shutting down idle apps
```

---

## Opportunity 6 — Oversized Notebook Instances (Low CPU)

**Severity:** 🟡 MEDIUM  
**Opportunity type:** `sagemaker-oversized-notebook`

**Detection logic:**
- `NotebookInstanceStatus == "InService"`
- Instance type is `ml.m5.xlarge` or larger (or `ml.m6g.xlarge`+, `ml.p3.*`)
- CloudWatch `CPUUtilization` average < 10% over 15 days

**Rationale:** `ml.t3.medium` (4 vCPU, 4 GB RAM, $0.059/hr) covers the vast majority of
interactive EDA and model experimentation workloads. Larger instances are only justified for
heavy data preprocessing, large model loading, or GPU training.

**Downsizing procedure:**
```bash
# 1. Stop the instance (files on EBS are preserved)
aws sagemaker stop-notebook-instance --notebook-instance-name my-notebook

# 2. Wait for Stopped status
aws sagemaker wait notebook-instance-stopped --notebook-instance-name my-notebook

# 3. Update the instance type
aws sagemaker update-notebook-instance \
  --notebook-instance-name my-notebook \
  --instance-type ml.t3.medium

# 4. Restart
aws sagemaker start-notebook-instance --notebook-instance-name my-notebook
```

**Saving calculation:**
```python
current_mo = NOTEBOOK_PRICE_HR[current_type] * 730
target_mo  = NOTEBOOK_PRICE_HR["ml.t3.medium"] * 730
saving_mo  = current_mo - target_mo
```

---

## SageMaker Studio vs Classic Notebooks — Cost Comparison

| Dimension            | Classic Notebooks          | SageMaker Studio              |
|----------------------|---------------------------|-------------------------------|
| **Billing model**    | Per-hour while InService   | Per-hour while kernel running |
| **Idle cost**        | Full instance rate         | Zero (no kernel running)      |
| **Startup latency**  | None (already running)     | 30–90 sec to start kernel     |
| **Storage**          | Attached EBS               | EFS (shared across profiles)  |
| **Multi-user**       | 1 notebook = 1 instance    | Multiple profiles, shared domain |
| **Git integration**  | Manual                    | Built-in CodeCommit/GitHub    |
| **Auto-shutdown**    | No native support          | Lifecycle config supported    |

**Cost scenario — developer who works 8 hr/day, 22 days/month:**

| Instance      | Classic (Always On)       | Studio (Kernel only when active)   |
|---------------|--------------------------|-------------------------------------|
| ml.t3.medium  | $0.059 × 730 = **$43/mo** | $0.059 × 176 = **$10/mo** (76% less) |
| ml.m5.xlarge  | $0.269 × 730 = **$196/mo**| $0.269 × 176 = **$47/mo** (76% less) |

> For teams running notebooks 24/7 due to scheduled jobs: use **SageMaker Processing Jobs**
> or **SageMaker Pipelines** for scheduled work instead of notebooks.

---

## Detection Caveats

### Endpoint instance type requires describe_endpoint_config

`list_endpoints()` does **not** return the instance type. To get it:

```python
ep_detail  = sm.describe_endpoint(EndpointName=name)
cfg_name   = ep_detail["EndpointConfigName"]
cfg_detail = sm.describe_endpoint_config(EndpointConfigName=cfg_name)
itype      = cfg_detail["ProductionVariants"][0]["InstanceType"]
```

### CloudWatch Invocations metric granularity

The `Invocations` metric is available at 1-minute granularity but 1-hour periods are
sufficient for 7-day idle detection. Use `Statistics=["Sum"]` (not Average) to correctly
detect zero-traffic endpoints across the entire window.

### Studio LastUserActivityTimestamp

`LastUserActivityTimestamp` is per-app (not per-domain). An app may have `Status=InService`
but `LastUserActivityTimestamp` from weeks ago — this means the kernel is running and billing
but no user is active. Take the **most recent** timestamp across all apps in the domain to
determine overall domain activity.

### Training job cost accuracy

`list_training_jobs()` returns completed jobs but not the actual compute seconds billed.
For accurate cost, use Cost Explorer grouped by `sagemaker:training-job-name` resource tag,
or call `describe_training_job()` and compute:
```python
duration_sec  = (TrainingEndTime - TrainingStartTime).total_seconds()
billable_sec  = BillableTimeInSeconds  # field in describe_training_job response
hourly_rate   = NOTEBOOK_PRICE_HR.get(instance_type, 0.269)
actual_cost   = (billable_sec / 3600) * hourly_rate * instance_count
```

---

## Notes

- Skip resources tagged with `cost-analyser:ignore = true`.
- SageMaker Savings Plans exist (compute SP) and can cover notebook + endpoint compute.
  Check Cost Explorer before flagging On-Demand usage — SP coverage may already apply.
- Feature Store online store costs (in-memory) can be significant at scale; check
  `AWS/SageMaker/FeatureStore` CloudWatch namespace for `RecordAccessCount`.
- Multi-model endpoints (MME) and multi-container endpoints share instance cost across
  models — flag for consolidation if single-model endpoints could be merged.
