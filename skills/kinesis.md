# Skill: Amazon Kinesis Data Streams — Cost-Saving Opportunities

## Service Overview

Amazon Kinesis Data Streams charges depend on the capacity mode. **Provisioned** streams charge
for **shard hours** and **PUT payload units**. **On-demand** streams charge per **record written**
and **GB retrieved**, with a per-hour minimum. Extended retention beyond the default 24 hours adds
a per-shard-hour surcharge. The main savings levers are choosing the right capacity mode for actual
traffic, removing orphaned or unused streams, and trimming unnecessary extended retention.

---

## Data Collection

### AWS API Calls

```python
kinesis = session.client("kinesis", region_name=region)
cw      = session.client("cloudwatch", region_name=region)

# List all stream names
kinesis.get_paginator("list_streams").paginate()

# Full stream metadata (mode, shard count, retention)
kinesis.describe_stream_summary(StreamName=name)
# Returns StreamDescriptionSummary:
#   OpenShardCount, RetentionPeriodHours, StreamModeDetails.StreamMode,
#   StreamARN, StreamStatus, EnhancedMonitoring

# CloudWatch: total records written in last 30 days
cw.get_metric_statistics(
    Namespace="AWS/Kinesis",
    MetricName="IncomingRecords",
    Dimensions=[{"Name": "StreamName", "Value": stream_name}],
    StartTime=now - timedelta(days=30),
    EndTime=now,
    Period=30 * 86400,
    Statistics=["Sum"],
)

# CloudWatch: total bytes written in last 30 days
cw.get_metric_statistics(
    Namespace="AWS/Kinesis",
    MetricName="IncomingBytes",
    ...
)

# CloudWatch: average consumer iterator age (detects lag / active consumers)
cw.get_metric_statistics(
    Namespace="AWS/Kinesis",
    MetricName="GetRecords.IteratorAgeMilliseconds",
    ...
    Statistics=["Average"],
)
```

### Cache TTL
- Stream metadata (describe_stream_summary): **24 hours**
- CloudWatch metrics: **24 hours** (shared `CACHE_TTL_DAYS` constant)

---

## Opportunity 1 — Evaluate On-Demand vs Provisioned Mode

**Severity:** 🟢 LOW  
**Opportunity type:** `kinesis-evaluate-on-demand`

**Detection logic:**
- Stream is in `PROVISIONED` mode.
- Fetch actual `IncomingRecords` (30-day sum) from CloudWatch.
- Calculate break-even: the record volume at which provisioned and on-demand cost the same.
- If actual traffic is **below break-even**, on-demand is cheaper (or the stream should be deleted).

**Break-even formula:**
```
# Cost of provisioned stream per month
prov_mo = shard_count × $0.016/hr × 730

# Number of records at which on-demand PUT cost equals provisioned cost
break_even_records = (prov_mo / $0.014) × 1_000_000
# e.g. 1 shard: ($11.68 / $0.014) × 1M ≈ 834M records/month

# On-demand cost for actual traffic
od_put_cost = (actual_records_30d / 1_000_000) × $0.04
```

> **Note:** On-demand streams also carry a minimum charge of **$0.08/hr ≈ $58.40/month** when
> the stream exists. For streams with truly near-zero traffic, deletion is often better than
> switching to on-demand.

**Calculation:**
```
saving = max(0, prov_mo - od_put_cost)   # only meaningful when records < break_even
```

**Recommendation:**
> "Stream '{name}' is PROVISIONED: {shard_count} shard(s) × $0.016/shard-hr =
> ${prov_mo:.2f}/month. Actual traffic in last 30 days: {records} records.
> Break-even for ON_DEMAND: {break_even} records/month.
> Current traffic is BELOW break-even — ON_DEMAND would cost ~${od_put_cost:.4f}/month vs
> ${prov_mo:.2f}/month provisioned. Potential saving: ~${saving:.2f}/month.
> Note: ON_DEMAND has a minimum charge of ~$58.40/month, so evaluate if near-zero traffic
> justifies deleting the stream instead."

---

## Opportunity 2 — Orphaned / Unused Stream Detection

**Severity:** ℹ️  INFO  
**Opportunity type:** `kinesis-stream-purpose`

**Detection logic:**
- Stream name matches patterns associated with abandoned or ephemeral usage:
  - `springcloudbus`, `spring-cloud-bus`, `springCloud` — Spring Cloud Bus streams used for
    distributed config refresh; commonly left behind when teams migrate to Kubernetes ConfigMap
    watchers or service-mesh config reloads.
  - Additional patterns to consider: `test`, `unused`, `temp`, `demo`.
- Zero or near-zero CloudWatch metrics (`IncomingRecords < 10,000` over 30 days) reinforce
  the orphan signal.

**Calculation:**
```
current_mo = provisioned_monthly_cost(shard_count, retention_h)   # if PROVISIONED
saving_mo  = 0   # informational; full saving realised only on deletion
```

**Recommendation:**
> "Stream '{name}' appears to be a Spring Cloud Bus stream, used for distributed config refresh
> propagation across microservices. Observed {records} records in last 30 days — very low traffic
> confirms pattern. Spring Cloud Bus is often replaced by service-mesh config reloads or
> k8s ConfigMap watchers in modern deployments. Verify: are all microservices still subscribing
> to this stream? If config refresh has moved to another mechanism, this stream is orphaned and
> can be deleted to save ${prov_mo:.2f}/month."

---

## Opportunity 3 — Unnecessary Extended Retention

**Severity:** 🟡 MEDIUM  
**Opportunity type:** `kinesis-extended-retention`

**Detection logic:**
- Stream `RetentionPeriodHours > 24` (the default free tier).
- Each additional 24-hour period beyond the first 24 hours costs **$0.024/shard-hour**.
- Flag streams where extended retention is enabled but cannot be justified by observable
  consumer behaviour (e.g. no elevated `GetRecords.IteratorAgeMilliseconds`).

**Calculation:**
```
ext_periods = (retention_h - 24) // 24          # number of extra 24h blocks
ext_mo      = shard_count × $0.024/shard-hr × ext_periods × 730
# e.g. 1 shard, retention 72h (2 extra periods): 1 × 0.024 × 2 × 730 = $35.04/month
```

**Recommendation:**
> "Stream '{name}' has extended retention of {retention_h}h (default is 24h). Extended retention
> costs $0.024/shard-hour per extra 24h period. This adds ~${ext_mo:.2f}/month for
> {shard_count} shard(s). Review if consumers actually replay data beyond 24h.
> If not, reduce retention to 24h to save ~${ext_mo:.2f}/month."

---

## Pricing Reference (eu-central-1)

| Component | Price |
|---|---|
| Provisioned shard | $0.016 / shard-hour |
| Provisioned PUT payload units | $0.014 / million PUT units |
| On-demand records written | $0.04 / million records |
| On-demand data retrieved | $0.08 / GB |
| On-demand minimum | $0.08 / hour (~$58.40/month) |
| Extended retention | $0.024 / shard-hour per extra 24h period |

> Source: https://aws.amazon.com/kinesis/data-streams/pricing/

---

## Pricing Sources

- Kinesis pricing is **hardcoded** as constants in `analyse_kinesis.py` (not retrieved via
  `pricing.get_products`). Update constants when AWS adjusts rates.

---

## Notes

- CloudWatch `IncomingRecords` uses `Sum` over 30 days as a proxy for PUT payload units.
  Actual provisioned PUT costs also depend on record size (each PUT unit = 25 KB);
  the analyser uses record count as a conservative approximation.
- For streams with no CloudWatch data (`IncomingRecords = None`), the analyser treats traffic
  as zero — this strengthens the orphan signal.
- Retrieve costs (for on-demand mode) depend on consumer behaviour and are omitted from the
  cost estimate to keep the calculation conservative.
- Tag-based filtering: skip streams with tag `cost-analyser:ignore = true`.
