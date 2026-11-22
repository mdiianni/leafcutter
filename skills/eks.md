# Skill: Amazon EKS — Cost-Saving Opportunities

## Service Overview

Amazon EKS charges a **flat control plane fee** per cluster per hour plus the underlying
EC2 or Fargate compute for nodes. Cost saving opportunities include eliminating idle clusters,
using Spot instances for non-critical node groups, migrating to Graviton node groups, using
Karpenter for right-sized provisioning, and right-sizing node groups based on pod utilisation.

---

## Data Collection

### AWS API Calls

```python
eks = session.client("eks", region_name=region)
ec2 = session.client("ec2", region_name=region)
cw  = session.client("cloudwatch", region_name=region)

# List all EKS clusters
eks.list_clusters()

# Describe each cluster
eks.describe_cluster(name=cluster_name)

# List node groups per cluster
eks.list_nodegroups(clusterName=cluster_name)

# Describe each node group
eks.describe_nodegroup(clusterName=cluster_name, nodegroupName=ng_name)

# EC2 instances tagged by EKS (node group instances)
ec2.describe_instances(
    Filters=[
        {"Name": "tag:eks:cluster-name", "Value": cluster_name},
        {"Name": "instance-state-name", "Values": ["running"]}
    ]
)

# CloudWatch: Node CPU utilisation (via Container Insights if enabled)
cw.get_metric_statistics(
    Namespace="ContainerInsights",
    MetricName="node_cpu_utilization",
    Dimensions=[
        {"Name": "ClusterName", "Value": cluster_name},
        {"Name": "NodeName",    "Value": node_name},
    ],
    StartTime=now - timedelta(days=15),
    EndTime=now,
    Period=3600,
    Statistics=["Average", "Maximum"]
)

# CloudWatch: node_memory_utilization (Container Insights)
# CloudWatch: pod_cpu_utilization
# CloudWatch: pod_memory_utilization
```

### Derived metrics

```python
# Per cluster:
control_plane_cost_mo  = pricing["eks"]["control_plane"] * 730  # $0.10/hr → $73/month
node_groups            = list of nodegroup descriptors
total_node_count       = sum(ng["scalingConfig"]["desiredSize"] for ng in nodegroups)
ami_type               = ng["amiType"]  # AL2_x86_64, AL2_ARM_64, BOTTLEROCKET_x86_64, etc.
capacity_type          = ng["capacityType"]  # ON_DEMAND or SPOT
```

### Cache TTL
- Cluster + node group metadata: **24 hours**
- CloudWatch Container Insights metrics: **6 hours**

---

## Opportunity 1 — EKS Cluster Control Plane: Idle / Unused Clusters

**Severity:** 🔴 CRITICAL  
**Opportunity type:** `eks-idle-cluster`

**Detection logic:**
- Cluster exists but all node groups have `desiredSize = 0`.
- OR: Container Insights `node_cpu_utilization` = 0 across all nodes over 15 days.

Control plane cost is $0.10/hour = **$73/month** regardless of whether nodes are running.

**Calculation:**
```
control_plane_monthly = pricing["eks"]["control_plane_hourly"] * 730
node_cost_monthly     = sum of all node EC2 costs
total_monthly         = control_plane_monthly + node_cost_monthly
```

**Recommendation:**
> "EKS cluster '{cluster}' appears idle (no running pods or nodes over 15 days). The cluster
> control plane alone costs ${control_plane_cost}/month. Consider deleting it if no longer in use."

---

## Opportunity 2 — Node Groups Using On-Demand for Non-Production Workloads

**Severity:** 🟠 HIGH  
**Opportunity type:** `eks-spot-instances`

**Detection logic:**
- Node group `capacityType = "ON_DEMAND"`.
- Cluster or namespace tags suggest non-production (`dev`, `test`, `staging`).
- Spot instances save 60–90% for interruptible workloads.

**Recommendation:**
> "EKS node group '{ng_name}' in cluster '{cluster}' uses On-Demand instances for a {env}
> workload. Switching to Spot instances saves ~{pct}% (~${saving}/month). Configure Pod
> Disruption Budgets and `tolerations` for Spot interruption handling."

---

## Opportunity 3 — Node Groups on x86 AMI: Migrate to Graviton (ARM64)

**Severity:** ⚠️ DEPENDENCY  
**Opportunity type:** `eks-graviton-node-group`

**Detection logic:**
- Node group `amiType` is `AL2_x86_64` or `BOTTLEROCKET_x86_64`.
- A Graviton-based node group (`AL2_ARM_64`, `BOTTLEROCKET_ARM_64`) with comparable instance
  types offers 20% lower cost.

> ⚠️ **Dependency warning:** Migrating EKS nodes to ARM64 (Graviton) requires:
> - All container images must be available as ARM64 or multi-arch manifests.
> - Any native extensions or language-specific compiled binaries must support ARM64.
> - Test on a separate ARM64 node group before migrating production workloads.
> - Karpenter or Cluster Autoscaler must be configured to schedule pods on ARM64 nodes.

**Graviton instance mapping for EKS:**

| Current type | Graviton equivalent | Saving |
|---|---|---|
| m5.large | m7g.large | ~20% |
| m5.xlarge | m7g.xlarge | ~20% |
| c5.large | c7g.large | ~20% |
| r5.large | r7g.large | ~20% |
| t3.medium | t4g.medium | ~20% |

**Recommendation:**
> "EKS node group '{ng_name}' runs {node_count} × {instance_type} (x86_64). Adding an ARM64
> Graviton node group with {graviton_type} instances saves ~${saving}/month. ⚠️ Container
> images must be rebuilt for linux/arm64 before migrating workloads."

---

## Opportunity 4 — Cluster Autoscaler Not Enabled (Over-provisioned Nodes)

**Severity:** 🟠 HIGH  
**Opportunity type:** `eks-no-autoscaler`

**Detection logic:**
- Node group `minSize < desiredSize = maxSize` (i.e., desired == max → no scale-in).
- OR: No `cluster-autoscaler` deployment found (infer from node group tags:
  `k8s.io/cluster-autoscaler/{cluster_name}` absent).
- Without Cluster Autoscaler or Karpenter, nodes run at peak provisioning 24/7.

**Recommendation:**
> "EKS cluster '{cluster}' does not appear to have Cluster Autoscaler or Karpenter configured.
> Without node auto-scaling, {node_count} nodes run continuously. Enabling scale-in on low
> utilisation periods could reduce costs significantly (~${saving}/month for {hours_idle}
> off-peak hours per day)."

---

## Opportunity 5 — Over-provisioned Node Groups (Low CPU + Memory)

**Severity:** 🟠 HIGH  
**Opportunity type:** `eks-node-right-sizing`

**Detection logic (requires Container Insights):**
- `node_cpu_utilization` average < 15% over 15 days.
- `node_memory_utilization` average < 30% over 15 days.
- Nodes are candidate for downsizing to smaller instance type.

**Recommendation:**
> "EKS cluster '{cluster}' nodes have average CPU of {avg_cpu}% and memory of {avg_mem}%
> over 15 days. Downsizing from {current_type} to {smaller_type} would save ~${saving}/month
> across {node_count} nodes. Validate pod resource requests/limits before downsizing."

---

## Opportunity 6 — Container Insights Not Enabled

**Severity:** 🟢 LOW  
**Opportunity type:** `eks-no-container-insights`

**Detection logic:**
- CloudWatch Container Insights metrics for the cluster return no data.
- Without Container Insights, node and pod level utilisation analysis is unavailable.

**Recommendation:**
> "EKS cluster '{cluster}' does not have Container Insights enabled. Without it, CPU and
> memory utilisation analysis at node and pod level is unavailable, making right-sizing
> impossible. Enable Container Insights to unlock cost optimisation data."

---

## Opportunity 7 — Multiple Small Dev/Test Clusters

**Severity:** 🟡 MEDIUM  
**Opportunity type:** `eks-cluster-consolidation`

**Detection logic:**
- > 3 clusters with tags suggesting non-production (`dev`, `test`, `staging`).
- Each cluster has a $73/month control plane charge regardless of size.

**Recommendation:**
> "Found {cluster_count} non-production EKS clusters. EKS control plane costs ${cp_total}/month.
> Consolidating dev/test workloads onto fewer clusters using namespaces and RBAC isolation
> could save ~${saving}/month in control plane fees."

---

## Opportunity 8 — Scheduled Overnight Scale-Down for Non-Production Node Groups

**Severity:** 🟠 HIGH  
**Opportunity type:** `eks-scheduled-scale-down`

**Rationale:** Non-production node groups (dev, qa, stg, acc, etc.) are rarely needed between
**20:00 and 05:00 UTC** (9 hours/night). Scaling node groups to 0 overnight eliminates ~37%
of monthly EC2 node compute cost with zero impact on production clusters.

> ⛔ **Never flag production environments** (`prod`, `production`, `prd`). Only apply to node
> groups or clusters whose `Environment`/`env` tag is one of: `dev`, `development`, `qa`,
> `test`, `testing`, `stg`, `staging`, `acc`, `acceptance`, `uat`.

---

### Detection Logic

```python
NON_PROD_ENVS = {"dev", "development", "qa", "test", "testing",
                 "stg", "staging", "acc", "acceptance", "uat"}
PROD_ENVS     = {"prod", "production", "prd"}

def get_cluster_env(cluster_tags: dict) -> str:
    """Resolve environment from cluster or node group tags."""
    for key in ("Environment", "environment", "env", "Env"):
        if key in cluster_tags:
            return cluster_tags[key].lower()
    return ""

def has_scheduled_scale_down(asg_name: str, autoscaling_client) -> bool:
    """
    Check whether the node group's Auto Scaling Group already has a scheduled
    action that scales desired capacity to 0 (or 1) during overnight hours.
    """
    actions = autoscaling_client.describe_scheduled_actions(
        AutoScalingGroupName=asg_name
    ).get("ScheduledUpdateGroupActions", [])
    for action in actions:
        desired = action.get("DesiredCapacity", -1)
        recurrence = action.get("Recurrence", "")
        # A cron expression starting with "0 20 *" or "0 19 *" targeting 0 nodes is a match
        if desired <= 1 and recurrence:
            return True
    return False

# Resolve ASG name from node group
def get_asg_name_for_nodegroup(nodegroup: dict) -> str:
    """
    The managed node group descriptor contains:
      nodegroup["resources"]["autoScalingGroups"][0]["name"]
    """
    asgs = nodegroup.get("resources", {}).get("autoScalingGroups", [])
    return asgs[0]["name"] if asgs else ""

# Main detection loop
for cluster_name in clusters:
    cluster_desc = eks.describe_cluster(name=cluster_name)["cluster"]
    cluster_tags = cluster_desc.get("tags", {})
    env = get_cluster_env(cluster_tags)

    if env in PROD_ENVS:
        continue                                 # skip production
    if env not in NON_PROD_ENVS:
        continue                                 # skip untagged / unknown

    for ng_name in eks.list_nodegroups(clusterName=cluster_name)["nodegroups"]:
        ng = eks.describe_nodegroup(clusterName=cluster_name, nodegroupName=ng_name)["nodegroup"]

        # Inherit env from node group tags if more specific than cluster
        ng_tags = ng.get("tags", {})
        ng_env  = get_cluster_env(ng_tags) or env
        if ng_env in PROD_ENVS:
            continue

        asg_name = get_asg_name_for_nodegroup(ng)
        if asg_name and has_scheduled_scale_down(asg_name, autoscaling_client):
            continue                             # already configured — skip

        desired = ng["scalingConfig"]["desiredSize"]
        if desired == 0:
            continue                             # already at 0 — no saving

        # → Flag this node group
```

---

### Saving Calculation

```python
# Constants
SCALE_DOWN_HOURS_PER_NIGHT  = 9        # 20:00 → 05:00 UTC
NIGHTS_PER_MONTH            = 30
SCALE_DOWN_HOURS_PER_MONTH  = SCALE_DOWN_HOURS_PER_NIGHT * NIGHTS_PER_MONTH  # 270 hrs
TOTAL_HOURS_PER_MONTH       = 730
SCALE_DOWN_FRACTION         = SCALE_DOWN_HOURS_PER_MONTH / TOTAL_HOURS_PER_MONTH  # ≈ 37%

# Per node group
instance_type   = ng["instanceTypes"][0]   # primary instance type
node_count      = ng["scalingConfig"]["desiredSize"]
capacity_type   = ng["capacityType"]       # ON_DEMAND or SPOT

if capacity_type == "SPOT":
    hourly_rate = current_spot_price(instance_type)   # from describe_spot_price_history
else:
    hourly_rate = on_demand_price(instance_type)      # from pricing lookup

monthly_node_cost   = hourly_rate * node_count * TOTAL_HOURS_PER_MONTH
monthly_saving      = hourly_rate * node_count * SCALE_DOWN_HOURS_PER_MONTH
saving_pct          = SCALE_DOWN_FRACTION * 100   # 37.0%
annual_saving       = monthly_saving * 12
```

---

### Implementation — Two Approaches

#### Option A: Auto Scaling Scheduled Actions (simplest, works today)

```bash
ASG_NAME="<node-group-asg-name>"
ORIGINAL_DESIRED=3   # current desired node count
ORIGINAL_MIN=1       # current min size

# Scale DOWN at 20:00 UTC every night
aws autoscaling put-scheduled-update-group-action \
  --auto-scaling-group-name "$ASG_NAME" \
  --scheduled-action-name eks-scale-down-overnight \
  --recurrence "0 20 * * *" \
  --desired-capacity 0 \
  --min-size 0

# Scale UP at 05:00 UTC every morning
aws autoscaling put-scheduled-update-group-action \
  --auto-scaling-group-name "$ASG_NAME" \
  --scheduled-action-name eks-scale-up-morning \
  --recurrence "0 5 * * *" \
  --desired-capacity "$ORIGINAL_DESIRED" \
  --min-size "$ORIGINAL_MIN"
```

> ⚠️ When desired=0, all pods on that node group will be evicted. Ensure other node groups
> or Cluster Autoscaler warm pool can absorb critical system pods (CoreDNS, kube-proxy, etc.).
> Consider setting desired=1 (not 0) if system pods must remain running overnight.

#### Option B: EventBridge Scheduler + Lambda (preferred for managed node groups)

Directly scales the EKS managed node group without touching the underlying ASG:

```python
# Lambda function body (triggered by EventBridge Scheduler)
import boto3

def handler(event, context):
    eks = boto3.client("eks")
    action = event["action"]          # "scale_down" or "scale_up"
    cluster = event["cluster"]
    nodegroup = event["nodegroup"]
    original_desired = event.get("original_desired", 3)
    original_min     = event.get("original_min", 1)

    if action == "scale_down":
        eks.update_nodegroup_config(
            clusterName=cluster,
            nodegroupName=nodegroup,
            scalingConfig={"minSize": 0, "desiredSize": 0}
        )
    elif action == "scale_up":
        eks.update_nodegroup_config(
            clusterName=cluster,
            nodegroupName=nodegroup,
            scalingConfig={"minSize": original_min, "desiredSize": original_desired}
        )
```

EventBridge Scheduler rules:
```json
// Scale down — 20:00 UTC daily
{ "ScheduleExpression": "cron(0 20 * * ? *)", "Input": {"action": "scale_down", ...} }

// Scale up — 05:00 UTC daily
{ "ScheduleExpression": "cron(0 5 * * ? *)", "Input": {"action": "scale_up",   ...} }
```

#### Option C: Terraform / IaC (for teams using infrastructure-as-code)

```hcl
resource "aws_autoscaling_schedule" "scale_down_overnight" {
  scheduled_action_name  = "eks-scale-down-overnight"
  autoscaling_group_name = module.eks.eks_managed_node_groups["dev"].node_group_autoscaling_group_names[0]
  recurrence             = "0 20 * * *"
  desired_capacity       = 0
  min_size               = 0
  max_size               = -1  # -1 = keep current max
}

resource "aws_autoscaling_schedule" "scale_up_morning" {
  scheduled_action_name  = "eks-scale-up-morning"
  autoscaling_group_name = module.eks.eks_managed_node_groups["dev"].node_group_autoscaling_group_names[0]
  recurrence             = "0 5 * * *"
  desired_capacity       = var.dev_node_count
  min_size               = var.dev_node_min
  max_size               = -1
}
```

---

### Recommendation Template

> "EKS node group **'{ng_name}'** in cluster **'{cluster}'** (env=`{env}`, {node_count}×
> {instance_type}, {capacity_type}) has no overnight scale-down schedule. Scaling to 0 nodes
> between **20:00–05:00 UTC** (9 hrs/night) would save ~**${monthly_saving:.2f}/month**
> (~37% of node compute, **${annual_saving:.0f}/year**). Implement via Auto Scaling Scheduled
> Actions or EventBridge + Lambda. See EKS skill for full implementation guidance."

---

### Additional Considerations

| Concern | Mitigation |
|---------|-----------|
| CoreDNS / kube-proxy eviction | Set `desiredSize=1` instead of 0 if system pods must stay running |
| PersistentVolume re-attachment | EBS volumes reattach automatically on scale-up; EFS is unaffected |
| CI/CD pipelines running overnight | Set scale-down recurrence to weekdays only: `0 20 * * 1-5` |
| Long-running jobs (e.g. nightly batch) | Add a `cost-analyser:no-scale-down=true` tag on the node group to suppress the opportunity |
| Weekend savings | For even more savings, scale down all weekend: `0 20 * * 5` (scale down Friday 20:00) + `0 5 * * 1` (scale up Monday 05:00) |

**Weekend-only scale-down additional saving:**

```python
# Weekend: Fri 20:00 → Mon 05:00 = 57 hours every weekend (~4.3 weekends/month)
WEEKEND_SCALE_DOWN_HOURS = 57 * 4.3   # ≈ 245 extra hrs/month
# Combined overnight + weekend saving ≈ 70% of monthly node cost
```

---

## Pricing Sources

| Item | Price |
|---|---|
| EKS control plane | $0.10/hour per cluster (~$73/month) |
| EC2 nodes | Standard EC2 On-Demand / Spot pricing |
| Fargate pods | Standard Fargate pricing |

Fetch exact pricing: `pricing.get_products(ServiceCode="AmazonEKS", ...)`

---

## Notes

- Each EKS cluster's underlying EC2 node costs are analysed by the EC2 skill. Cross-reference
  EKS node findings with EC2 right-sizing recommendations.
- EKS Fargate profiles: pods are billed per-vCPU-hour and per-GB-hour, same as ECS Fargate.
  Apply the same Graviton analysis as ECS Fargate (⚠️ dependency warning applies).
- Tag-based filtering: skip clusters with tag `cost-analyser:ignore = true`.
