# Skill: Amazon GuardDuty — Cost-Saving Opportunities

## Service Overview

Amazon GuardDuty is a threat detection service that continuously monitors for malicious activity
and unauthorised behaviour. Costs accrue based on the **volume of data processed** per enabled
feature — not a flat rate. The three primary cost drivers are:

1. **CloudTrail management events** — per 1M events analysed (usually the largest cost)
2. **VPC Flow Logs** — per GB analysed
3. **DNS query logs** — per GB analysed

Optional paid add-ons (disabled by default):
- S3 data events, EKS audit logs, EBS malware protection, RDS login events,
  Lambda network logs, Runtime Monitoring

---

## Data Collection

### AWS API Calls

```python
gd = session.client("guardduty", region_name=region)

# Enumerate detectors (usually one per account per region)
gd.list_detectors()

# Full detector config incl. enabled features
gd.get_detector(DetectorId=detector_id)

# Month-to-date cost breakdown by feature
gd.get_usage_statistics(
    DetectorId=detector_id,
    UsageStatisticType="SUM_BY_FEATURES",
    UsageCriteria={"Features": ["CLOUD_TRAIL", "DNS_LOGS", "FLOW_LOGS",
                                "S3_DATA_EVENTS", "EKS_AUDIT_LOGS",
                                "EBS_MALWARE_PROTECTION", "RDS_LOGIN_EVENTS",
                                "LAMBDA_NETWORK_LOGS"]},
    Unit="USD"
)

# Findings count / types — for operational hygiene checks
gd.list_findings(DetectorId=id, FindingCriteria={"Criterion": {"severity": {"Gte": 4}}})
gd.get_findings(DetectorId=id, FindingIds=[...])   # batch up to 50

# Suppression filters
gd.list_filters(DetectorId=id)

# Publishing destinations (S3 export)
gd.list_publishing_destinations(DetectorId=id)

# Custom threat intel sets
gd.list_threat_intel_sets(DetectorId=id)
gd.list_ip_sets(DetectorId=id)
```

### Real-World API Quirks

1. **`UsageCriteria` not `UsageCriterion`** — the boto3 parameter is `UsageCriteria` (plural).
   Using `UsageCriterion` raises `Unknown parameter` error.
2. **Features array must be non-empty** — passing `Features: []` raises `BadRequestException:
   the provided features array was empty`. Must list explicit feature names.
3. **Unit must be `"USD"`** — `"By"` (bytes) raises `Invalid Unit, not a valid ISO 4217 currency`.
   `"GBP"` raises `Unsupported currency`. Only `"USD"` is accepted.
4. **`SUM_BY_DATA_SOURCE` type is deprecated** — use `SUM_BY_FEATURES` instead. Passing
   `DataSources` in `UsageCriteria` raises a bad request error.
5. **Usage stats are month-to-date** — the returned amounts cover the current billing period
   (not last 30 days). Extrapolate to full month: `amount / day_of_month * 30`.
6. **Findings severity filter must be `int` not `float`** — `{"Gte": 7}` works; `{"Gte": 7.0}`
   raises `Invalid type for parameter ... valid types: <class 'int'>`. Always cast severity
   thresholds to `int` in `FindingCriteria`.

### Cache TTL
- Detector config + usage stats: **30 days** (via `cache_utils.py`)

---

## Cost Model

GuardDuty pricing in `eu-central-1`:

| Feature | Pricing |
|---------|---------|
| CloudTrail management events | $4.60 / 1M events/month |
| VPC Flow Logs | $1.15 / GB analysed |
| DNS Logs | $1.15 / GB analysed |
| S3 data events (first 500M) | $0.80 / 1M events |
| EKS audit logs | $1.15 / 1M audit logs |
| EBS malware protection | $0.86 / GB scanned |
| RDS login events | $0.40 / 1M login attempts |
| Lambda network activity logs | $1.15 / 1M invocations |

> Note: CloudTrail is typically the dominant cost because every AWS API call generates
> a CloudTrail event. In active accounts with many ECS tasks, Lambda invocations, and
> auto-scaling events, CloudTrail volume can be very high.

---

## Opportunity 1 — Stale Unarchived Findings (Operational Hygiene)

**Severity:** 🟡 MEDIUM
**Opportunity type:** `guardduty-stale-findings`

**Detection logic:**
- Findings with `UpdatedAt` older than 30 days that have not been archived.
- No suppression filters (`list_filters` returns empty) to auto-archive recurring false positives.

Unarchived findings accumulate indefinitely. If findings are recurring false positives (e.g.
`IAMUser/MaliciousIPCaller` for a legitimate IP), they add noise and mask real threats. Suppression
rules auto-archive matching findings so analysts focus on genuine threats.

**Recommendation:**
> "Found {count} unarchived findings older than {days} days. {type_breakdown}. If these are
> known false positives (e.g. dev tooling from specific IPs), create a GuardDuty filter to
> auto-suppress them. Review and archive findings not acted upon."

---

## Opportunity 2 — No Finding Publishing Destination

**Severity:** 🟢 LOW
**Opportunity type:** `guardduty-no-export-destination`

**Detection logic:**
- `list_publishing_destinations` returns empty.

Exporting findings to S3 enables long-term retention, cross-account analysis, and integration
with SIEM tools. Without it, findings are limited to 90 days in GuardDuty console. No direct
cost impact — but affects security posture and audit capability.

**Recommendation:**
> "No GuardDuty publishing destination configured. Add an S3 destination to retain findings
> beyond 90 days and enable cross-account security analysis."

---

## Opportunity 3 — High CloudTrail Cost: Review API Activity Volume

**Severity:** 🟡 MEDIUM
**Opportunity type:** `guardduty-cloudtrail-high-cost`

**Detection logic:**
- `CLOUD_TRAIL` feature cost > $100/month (extrapolated from MTD usage).

CloudTrail cost is proportional to management API event volume. In ECS/EKS accounts with
frequent scaling events, service deployments, and Lambda invocations, this can be substantial.
Actions to reduce (in order of safety):

1. **Review if S3 data events are enabled** — if so, data events add significant volume
2. **Identify top API callers** via CloudTrail Insights or Athena queries
3. **Evaluate if any Lambda or ECS tasks are making excessive API calls** in tight loops
4. **In non-prod accounts**: consider reducing the GuardDuty finding publishing frequency
   (`FindingPublishingFrequency`) from `SIX_HOURS` to `FIFTEEN_MINUTES` — this does NOT
   affect the data analysed, only how often findings are surfaced

> Note: CloudTrail feature cannot be selectively scoped — all management events in the
> account are processed. Reducing the cost requires reducing API call volume in the account.

**Calculation:**
```
mtd_days     = today.day
cloud_trail_cost_mo = (mtd_amount / mtd_days) * 30
```

---

## Opportunity 4 — EKS Cluster Present but EKS Audit Logs Disabled

**Severity:** 🟡 MEDIUM (security, not cost)
**Opportunity type:** `guardduty-eks-audit-logs-disabled`

**Detection logic:**
- `EKS_AUDIT_LOGS` feature status is `DISABLED`
- Account has active EKS clusters (cross-reference `analyse_eks.py` or EC2 worker nodes)

EKS audit log monitoring detects threats like container escapes, privilege escalation in pods,
and suspicious kubectl commands. It is **free for the first 30 days** when enabled; after that
charged at $1.15 / 1M audit log events.

> ⚠️ Enabling this adds cost — include a dependency warning (🔗) in the report since
> it requires evaluating EKS cluster audit log volume first.

---

## Opportunity 5 — No Custom Threat Intelligence

**Severity:** 🟢 LOW
**Opportunity type:** `guardduty-no-threat-intel`

**Detection logic:**
- `list_threat_intel_sets` returns empty.
- `list_ip_sets` returns empty.

Custom threat intel sets allow feeding known malicious IPs or trusted IP allow-lists into
GuardDuty. Without them, GuardDuty relies solely on AWS threat intelligence feeds.
Adding a trusted IP set can reduce false positives from dev tooling or VPN exits.

---

## Opportunity 6 — Runtime Monitoring Disabled for ECS Fargate

**Severity:** 🟡 MEDIUM (security, not cost)
**Opportunity type:** `guardduty-ecs-runtime-monitoring-disabled`

**Detection logic:**
- `RUNTIME_MONITORING` feature is `DISABLED`
- `ECS_FARGATE_AGENT_MANAGEMENT` additional config is `DISABLED`
- Account has active ECS Fargate services

Runtime Monitoring provides process-level threat detection inside ECS Fargate tasks. It detects
container escapes, reverse shells, and cryptomining within running containers. Enabling it
deploys a lightweight security agent as a sidecar. Cost: based on vCPU-hours monitored.

> 🔗 **Dependency warning** — enabling Runtime Monitoring for ECS Fargate adds per-task overhead
> and requires ECS task definition updates. Cost impact should be evaluated before enabling.

---

## Notes

- GuardDuty is a security service — the primary recommendation lens is **risk reduction**,
  not cost reduction. Cost optimisation here means eliminating waste (stale findings, no export
  destination) rather than disabling features.
- **Never recommend disabling CloudTrail, DNS Logs, or VPC Flow Logs** for cost savings — these
  are the foundational threat detection data sources. Their benefit far exceeds their cost.
- The `findings_publishing_frequency` field only affects UI latency, not cost or data volume.
- In AWS Organisations, GuardDuty can be managed centrally via a delegated admin account.
  Child accounts show detectors but may not see full usage stats via CE.

---

## Real-World Learnings

### Account: `<YOUR_ACCOUNT_ID>`, Region: `eu-central-1`, March 2026

**Configuration found:**
- 1 detector, ENABLED since 2022-01-17
- Enabled features: `CLOUD_TRAIL`, `DNS_LOGS`, `FLOW_LOGS`
- Disabled: S3_DATA_EVENTS, EKS_AUDIT_LOGS, EBS_MALWARE_PROTECTION, RDS_LOGIN_EVENTS,
  EKS_RUNTIME_MONITORING, LAMBDA_NETWORK_LOGS, RUNTIME_MONITORING

**Month-to-date costs (23 days into March):**
| Feature | MTD Amount | Est. Full Month |
|---------|-----------|-----------------|
| CLOUD_TRAIL | $106.12 | **$138.42** |
| FLOW_LOGS | $39.72 | **$51.81** |
| DNS_LOGS | $1.01 | **$1.32** |
| **Total** | **$146.85** | **~$191.55** |

**Findings:**
- 0 high severity (≥7.0)
- 19 medium severity (5.0) — all from **Poland**, all IAMUser/MaliciousIPCaller or Recon
  - 14× `Recon:IAMUser/MaliciousIPCaller`
  - 5× `UnauthorizedAccess:IAMUser/MaliciousIPCaller`
  - Last updated: 2026-03-04 — stale, not being actioned
  - No suppression filters configured

**No publishing destinations, no threat intel sets, no suppression filters.**
