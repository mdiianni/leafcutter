# Skill: AWS X-Ray — Cost-Saving Opportunities

## Service Overview

AWS X-Ray provides distributed tracing for applications. Costs are driven by:
- **Traces recorded** — $5.00 per million segments after the first 100,000 free/month
- **Traces retrieved/accessed** — $0.50 per million after the first 1,000,000 free/month

X-Ray costs are proportional to **traffic volume × sampling rate × segments per trace**.
A single API call may generate 5–15 segments (Lambda, API GW, DynamoDB, downstream services).

---

## Real-World Learnings (from live account runs)

### ✅ X-Ray costs can spike dramatically then disappear

In a real PRD account, X-Ray cost was:
- January: $183/month
- February: **$562/month** (3× spike — ~93.5M segments recorded)
- March (from day 3 onwards): **~$0.01/day** (essentially zero)

The spike was caused by a specific service/deployment that generated extremely high
trace volumes, then was fixed or removed. **Always check the daily cost trend first**
before drawing conclusions — a high monthly cost may already be resolved.

### ✅ GetTraceSummaries is limited to 24-hour windows

```python
# WRONG — raises InvalidRequestException
xray.get_trace_summaries(
    StartTime=now - timedelta(days=7),  # >24 hours → error
    EndTime=now
)

# CORRECT — use 1-hour windows to estimate current rate
xray.get_trace_summaries(
    StartTime=now - timedelta(hours=1),
    EndTime=now,
    Sampling=False
)
# Extrapolate: traces_per_hour × 730 = monthly estimate
```

### ✅ get_trace_summary_statistics and get_sampling_statistics_summaries do not exist

These methods appear in some SDK documentation but are not available:
```python
# These raise AttributeError — do NOT call them:
xray.get_trace_summary_statistics(...)      # does not exist
xray.get_sampling_statistics_summaries()    # does not exist
```

Use `get_sampling_rules()` for rule config and Cost Explorer for volume estimates.

### ✅ Derive trace volume from CE cost — don't try to count traces

Cost Explorer gives precise `EUC1-XRay-TracesStored` and `EUC1-XRay-TracesAccessed`
cost breakdown. Use these to back-calculate segment volumes:

```python
# Pricing (after free tier: 100K traces/month free):
PRICE_PER_M_SEGMENTS_RECORDED  = 5.00   # $5 per million
PRICE_PER_M_TRACES_ACCESSED    = 0.50   # $0.50 per million

segments_recorded_M = cost_stored   / PRICE_PER_M_SEGMENTS_RECORDED
traces_accessed_M   = cost_accessed / PRICE_PER_M_TRACES_ACCESSED
```

### ✅ Check daily cost trend first — spikes may already be resolved

Always fetch 60 days of daily CE data before analysing. A high monthly cost may
be a historical spike that has already been fixed, in which case the opportunity
is informational ("already resolved") rather than actionable.

---

## Data Collection

### AWS API Calls

```python
xray = session.client("xray", region_name=region)
ce   = session.client("ce",   region_name="us-east-1")

# 1. Sampling rules (config, not volume)
xray.get_sampling_rules()

# 2. Groups (custom filter expressions)
xray.get_groups()

# 3. Encryption configuration
xray.get_encryption_config()

# 4. Recent trace volume estimate (1-hour window only)
xray.get_trace_summaries(
    StartTime=now - timedelta(hours=1),
    EndTime=now,
    Sampling=False
)

# 5. CE: last full month cost by usage type
ce.get_cost_and_usage(
    TimePeriod={...},
    Filter={"Dimensions": {"Key": "SERVICE", "Values": ["AWS X-Ray"]}},
    GroupBy=[{"Type": "DIMENSION", "Key": "USAGE_TYPE"}],
)

# 6. CE: daily cost trend last 60 days (spike detection)
ce.get_cost_and_usage(
    TimePeriod={"Start": 60_days_ago, "End": today},
    Granularity="DAILY",
    Metrics=["UnblendedCost"],
    Filter={"Dimensions": {"Key": "SERVICE", "Values": ["AWS X-Ray"]}},
)
```

### Derived Metrics

```python
xray_data = {
    "sampling_rules":         list,   # all rules with FixedRate, ReservoirSize
    "default_sampling_rate":  float,  # from "Default" rule
    "groups":                 list,
    "encryption_type":        str,    # "NONE" or "KMS"
    "last_month_cost":        float,  # from CE
    "cost_stored":            float,  # EUC1-XRay-TracesStored
    "cost_accessed":          float,  # EUC1-XRay-TracesAccessed
    "segments_recorded_M":    float,  # back-calculated from cost
    "traces_per_hour":        int,    # from get_trace_summaries (1h window)
    "top_services":           dict,   # service name → trace count (1h sample)
    "daily_costs":            list,   # [{date, cost}] for 60d trend
    "cost_spike_detected":    bool,
    "spike_already_resolved": bool,
    "mtd_daily_avg":          float,
    "lm_daily_avg":           float,
}
```

### Cache TTL
- Sampling rules + groups: **24 hours**
- CE monthly cost: **7 days** (closed month)
- CE daily trend: **24 hours**
- Trace summaries (1h sample): **1 hour**

---

## Opportunity 1 — Sampling Rate Too High

**Severity:** 🟠 HIGH (rate > 10% and cost > $50/mo) | 🟡 MEDIUM (rate > 5%)  
**Opportunity type:** `xray-sampling-rate-high`

**Detection logic:**

```python
for rule in sampling_rules:
    sr = rule["SamplingRule"]
    rate = sr["FixedRate"]
    if rate > 0.10 and monthly_cost > 50:
        # High sampling rate driving cost
        flag_opportunity()
    elif rate == 1.0:
        # 100% sampling — almost certainly unintentional in production
        flag_critical()
```

**Recommendation:**
> "X-Ray default sampling rate is {rate*100:.0f}%. For production traffic this is high.
> AWS recommends 5% (0.05) for production. Reducing to 5% would save approximately
> {saving_pct:.0f}% of tracing costs (~${saving:.2f}/month). Use reservoir sampling
> for critical paths to ensure low-traffic services are always sampled."

---

## Opportunity 2 — Cost Spike Detection (Historical)

**Severity:** 🟠 HIGH (spike > 3× baseline, still active) | ℹ️ INFO (already resolved)  
**Opportunity type:** `xray-cost-spike`

**Detection logic:**

```python
# Calculate 7-day rolling average for each day
for i, day in enumerate(daily_costs):
    if i < 7: continue
    baseline_avg = sum(daily_costs[i-7:i]) / 7
    if day["cost"] > baseline_avg * 3:
        spike_start = day["date"]

# Check if spike is still active
recent_7d_avg = sum(daily_costs[-7:]) / 7
spike_resolved = recent_7d_avg < (baseline_avg * 1.5)
```

**Recommendation (unresolved spike):**
> "X-Ray cost spiked {spike_multiplier:.0f}× on {spike_start}. Current daily average
> ${recent_avg:.2f} remains elevated vs pre-spike baseline ${baseline_avg:.2f}/day.
> Use `get_trace_summaries` (1-hour windows) to identify the top-traffic services
> and apply tighter sampling rules to those service names."

**Recommendation (resolved spike):**
> "✅ X-Ray cost spike detected in {spike_month} (${spike_cost:.2f}) has resolved.
> Current daily cost is ${current_daily:.4f}/day. The spike was likely caused by
> a service deployment with 100% sampling or a traffic surge. Review sampling rules
> to prevent recurrence."

---

## Opportunity 3 — X-Ray Disabled / No Cost (Service Not in Use)

**Severity:** ℹ️ INFO  
**Opportunity type:** `xray-not-in-use`

**Detection logic:**
```python
if last_month_cost < 1.0 and segments_recorded_M < 0.1:
    # X-Ray not meaningfully in use
    flag_info("X-Ray has negligible cost — no action needed.")
```

---

## Opportunity 4 — KMS Encryption Cost

**Severity:** 🟢 LOW  
**Opportunity type:** `xray-kms-encryption`

**Detection logic:**
```python
if encryption_config["Type"] == "KMS":
    # Each X-Ray API call with KMS adds a KMS Decrypt API call
    # High trace volume + KMS = additional KMS cost (~$0.03 per 10K API calls)
    flag_opportunity()
```

**Recommendation:**
> "X-Ray uses KMS customer-managed key encryption. At high trace volumes this adds
> KMS API costs. If regulatory compliance does not require CMK, switching to
> AWS-managed encryption (Type=NONE) eliminates the KMS overhead."

---

## Opportunity 5 — Granular Sampling Rules per Service

**Severity:** 🟡 MEDIUM  
**Opportunity type:** `xray-sampling-rules-optimise`

**Detection logic:**
```python
if len(sampling_rules) == 1 and sampling_rules[0]["RuleName"] == "Default":
    # Only default rule — no per-service tuning
    if segments_recorded_M > 10:  # > 10M segments/month
        flag_opportunity()
```

**Recommendation:**
> "Only the Default sampling rule is configured. With {segments_M:.0f}M segments/month,
> per-service sampling rules would allow high-value services to be fully sampled while
> reducing sampling on high-traffic, low-value paths (e.g. health checks, metrics endpoints).
> Example: set rule for service_name='*/health' with FixedRate=0.0 to skip health checks."

---

## Sampling Rate Reference

| Scenario | Recommended rate | Notes |
|----------|-----------------|-------|
| Production, high traffic (>1K req/s) | 0.01–0.05 | 1–5% sufficient for insights |
| Production, moderate traffic | 0.05–0.10 | 5–10% |
| Production, low traffic (<10 req/s) | 0.10–1.0 | Higher rate needed for coverage |
| Non-production | 0.05–0.10 | Cost is low, higher rate acceptable |
| Debug mode / incident investigation | 1.0 (temporary) | Revert after debugging |
| Health checks / probes | 0.0 | Never sample — pure noise |

**Reservoir sampling** ensures at least N traces/second are always captured regardless
of the percentage-based rate. Set `ReservoirSize=1` for low-traffic services so at
least 1 trace/second is always recorded even if the fixed rate would exclude it.

---

## Notes

- X-Ray has a **permanent free tier**: 100,000 traces recorded/month and 1,000,000
  traces retrieved/month. At low volumes, X-Ray is effectively free.
- `get_trace_summaries` only works on **windows ≤ 24 hours**. Use 1-hour windows to
  estimate current rate, then extrapolate.
- `get_sampling_rules()` returns all rules including the immutable Default rule.
- The `ServiceName` field in sampling rules supports wildcards (`*`) — use `*/health`
  pattern to target health check endpoints.
- Tag-based filtering: skip accounts/services with tag `cost-analyser:ignore=true`.
- **Daily cost trend analysis is the most valuable signal** — always fetch 60 days of
  daily CE data to identify spikes, resolutions, and trend direction.
