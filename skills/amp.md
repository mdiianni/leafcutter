# Skill: Amazon Managed Service for Prometheus (AMP) — Cost-Saving Opportunities

---

## AMP Pricing Model

AMP pricing has **two independent cost dimensions**:

### 1. Metric Sample Ingestion
Every Prometheus `remote_write` push sends metric samples to AMP. Each sample = one time-series
data point at one timestamp.

| Tier | Price |
|---|---|
| First 2B samples/month | $0.90 per million samples |
| Next 8B samples/month | $0.60 per million samples |
| Over 10B samples/month | $0.30 per million samples |

**Formula:**
```
samples_per_month = active_series × scrape_interval_per_day × 30
                  = active_series × (86400 / scrape_interval_seconds) × 30
ingestion_cost    = samples_per_month / 1_000_000 × $0.90
```
**Example:** 100,000 active series, 15s scrape interval →
100,000 × 5,760 × 30 = 17.28B samples/month → $11,520/month (first 2B at $0.90, rest cheaper).

### 2. Metric Sample Storage
AMP stores all ingested samples for the retention period. Storage cost applies per million
metric-samples stored per month.

| Item | Price |
|---|---|
| Storage | $0.03 per million metric-samples/month |

**Default retention:** 150 days  
**Formula:**
```
samples_stored = active_series × scrape_interval_per_day × retention_days
storage_cost   = samples_stored / 1_000_000 × $0.03
```

### 3. Query Cost
AMP does **not** charge for PromQL queries. Queries are free. This differs from Grafana Cloud
and other managed Prometheus services that charge per query.

### Key Insight: Ingestion >> Storage
At default 150-day retention with a 15s scrape interval:
- Ingestion cost for 1M series ≈ $1,728/month
- Storage cost for 1M series ≈ $864/month (150 days × $0.03/M samples/day)

Both are significant. Reducing retention primarily cuts storage, not ingestion.

---

## Cost Driver 1 — Retention Period (Default 150 Days)

**Why it matters:**  
AMP's default retention is 150 days. This is generous — most operational use cases only need
15–30 days for alerting and dashboards. Long-term trend analysis is better served by exporting
to S3 + Athena or using AWS Cost Explorer for billing trends.

**Impact of retention on storage cost:**
| Retention | Storage cost (1M series, 15s scrape) |
|---|---|
| 150 days (default) | ~$864/month |
| 90 days | ~$518/month |
| 30 days | ~$173/month |
| 15 days | ~$86/month |

**Reduction from 150 → 30 days: ~80% savings on storage.**

**How to change:**
```bash
aws amp update-workspace-configuration \
  --workspace-id <ws-id> \
  --retention-period-in-days 30
```

**Dependency warning:**  
Alert rules and dashboards using long-range PromQL queries will break if retention is reduced
below their look-back window. Common patterns to audit:
- `rate(metric[90d])` — requires 90-day retention minimum
- `avg_over_time(metric[60d:1d])` — subquery over 60 days
- Grafana panels with "Time range: Last 60 days" — data will be missing

**Recommendation:** Set retention to `max(dashboard_max_range, alert_max_range) + 20%` buffer.
For most environments, 30–45 days covers all operational use cases.

---

## Cost Driver 2 — High Metric Cardinality

**What is cardinality?**  
Each unique combination of label values creates a new time series. A metric with labels
`{job, pod_name, namespace, request_id}` where `request_id` has 10,000 unique values
creates 10,000 series — even if the metric name is the same.

**Common high-cardinality labels:**
| Label | Why it's dangerous |
|---|---|
| `pod_name` | New pod per deployment, rolling restart creates new series |
| `request_id` | Unique per HTTP request — explodes to millions |
| `trace_id` | Same as request_id |
| `container_id` | Changes on every container restart |
| `user_id` | Grows with user base |
| `session_id` | Unbounded |

**Detection:**  
Use the CloudWatch `AWS/Prometheus` metric `ActiveSeries` per `WorkspaceId` dimension.
> Note: There is no native AWS billing breakdown by workspace — use ActiveSeries as a proxy.

**Fix — metric relabeling:**
```yaml
# In Prometheus scrape config (applied before remote_write)
metric_relabel_configs:
  - source_labels: [request_id]
    action: labeldrop
    regex: request_id
  - source_labels: [pod_name]
    action: labelmap
    # Keep pod prefix, drop suffix (hash) to reduce cardinality
    regex: (.*)-[a-z0-9]{5}-[a-z0-9]{5}
    replacement: $1
```

**Alternative: recording rules**  
Pre-aggregate high-cardinality metrics into lower-cardinality summaries:
```yaml
# Instead of storing per-pod metrics, store per-service aggregates
- record: service:http_requests_total:rate5m
  expr: sum by (service, namespace) (rate(http_requests_total[5m]))
```
This removes the pod-level label dimension from storage while keeping the aggregate useful.

**Cost impact:**  
Each 1M series reduction saves ~$0.90M × 5,760/day × 30 days / 1M = ~$1,555/month in ingestion alone.

---

## Cost Driver 3 — Unused Workspaces

**Detection:**  
- CloudWatch `ActiveSeries` metric returns 0 for the workspace for 7+ consecutive days.
- Or no CloudWatch data at all (workspace was created but never configured).

**Why this happens:**
- Proof-of-concept workspaces left running after evaluation.
- CI/CD pipelines that created a workspace but never deployed the Prometheus remote_write config.
- Environment shutdown (dev cluster deleted) but AMP workspace not cleaned up.
- Blue/green deployment where old workspace was not removed after cutover.

**Cost of an idle workspace:**  
No active series → no ingestion cost. However, the workspace still contributes to management
overhead and may have residual storage from a prior active period that hasn't expired yet.

**How to delete:**
```bash
aws amp delete-workspace --workspace-id <ws-id>
```

**Important:** Deletion is immediate and permanent. All stored data is deleted. Verify that:
1. No Prometheus remote_write configs reference this workspace endpoint.
2. No Grafana datasources point to this workspace.
3. No scheduled CI jobs write to this workspace.

---

## Cost Driver 4 — Multiple Workspaces Without Environment Separation

**When multiple workspaces make sense:**
- One per environment (dev, staging, prod) — clear cost/blast-radius separation.
- One per business unit / team — for IAM isolation with workspace-level resource policies.
- One per region — AMP is regional; cross-region data requires separate workspaces.

**When consolidation helps:**
- Multiple workspaces in the same region for the same environment without clear separation.
- Workspaces created ad hoc (e.g., one per microservice) leading to 10-20 workspaces.
- No `env` or `cluster` external_labels to distinguish data sources after consolidation.

**How to consolidate:**
1. Add `external_labels` to each Prometheus remote_write config to tag the source:
```yaml
global:
  external_labels:
    env: production
    cluster: eks-eu-central-1
```
2. Update all remote_write endpoints to point to a single workspace.
3. Update Grafana datasources to use the single workspace.
4. Delete the old workspaces after validating data flow.

**Per-workspace overhead:**  
While AMP does not publish a per-workspace fixed monthly fee, each workspace has an independent
query engine and management plane. Consolidation reduces API call overhead, simplifies IAM policy
management, and centralises Grafana datasource configuration.

---

## Comparison: AMP vs Self-Hosted Prometheus

| Factor | AMP | Self-Hosted (EKS/EC2) |
|---|---|---|
| Infrastructure cost | None | EC2 + EBS for Prometheus pods |
| Operational overhead | None (managed) | High (PVC sizing, compaction, WAL recovery) |
| High-availability | Built-in | Requires federation or Thanos/Cortex |
| Retention flexibility | API-controlled | Bounded by PVC size |
| Cost at 100K series (30d ret.) | ~$166/month | ~$50-80/month (EC2 + EBS) |
| Cost at 1M series (30d ret.) | ~$1,660/month | ~$400-600/month (large EC2 + gp3 EBS) |
| Break-even point | ~200K series | N/A (always cheaper if ops cost excluded) |

**Rule of thumb:** AMP is cost-efficient under ~200K active series when including ops engineering
cost. Above that, self-hosted Prometheus with Thanos compaction becomes cheaper at scale — but
only if the team has the expertise to operate it reliably.

---

## Relevant AWS APIs

```python
amp = session.client("amp", region_name=region)

# List all workspaces (paginated)
paginator = amp.get_paginator("list_workspaces")
for page in paginator.paginate():
    workspaces = page["workspaces"]

# Workspace detail
ws = amp.describe_workspace(workspaceId=workspace_id)
alias      = ws["workspace"]["alias"]
status     = ws["workspace"]["status"]["statusCode"]
created_at = ws["workspace"]["createdAt"]

# Rule groups namespaces (check if workspace is configured with alert rules)
paginator = amp.get_paginator("list_rule_groups_namespaces")
for page in paginator.paginate(workspaceId=workspace_id):
    namespaces = page["ruleGroupsNamespaces"]

# Update retention
amp.update_workspace_configuration(
    workspaceId=workspace_id,
    retentionPeriodInDays=30,
)

# Delete workspace
amp.delete_workspace(workspaceId=workspace_id)
```

### CloudWatch Metrics for AMP

AMP publishes metrics under the `AWS/Prometheus` namespace:

| Metric | Dimension | Description |
|---|---|---|
| `ActiveSeries` | WorkspaceId | Current active time series count |
| `SamplesIngested` | WorkspaceId | Samples ingested per period |
| `IngestErrors` | WorkspaceId | Failed ingestion attempts |

```python
cw = session.client("cloudwatch", region_name=region)
cw.get_metric_statistics(
    Namespace="AWS/Prometheus",
    MetricName="ActiveSeries",
    Dimensions=[{"Name": "WorkspaceId", "Value": workspace_id}],
    StartTime=now - timedelta(days=7),
    EndTime=now,
    Period=86400,
    Statistics=["Maximum"],
)
```

> **Note:** CloudWatch metrics for AMP have a reporting delay of up to 15 minutes.
> Use a 7-day lookback window to reliably detect idle workspaces.

---

## Pricing Sources

- [AMP Pricing (eu-central-1)](https://aws.amazon.com/prometheus/pricing/)
- Ingestion: $0.90 per million samples (first 2B/month)
- Storage: $0.03 per million metric-samples stored/month
- Query: free (no per-query charge)
