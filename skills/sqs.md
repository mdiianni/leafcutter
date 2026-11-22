# Skill: Amazon SQS — Cost-Saving Opportunities

## Service Overview

Amazon SQS is effectively **free at dev-account volumes** (first 1M requests/month free tier). The primary opportunities are **operational risk reduction** (DLQ backlogs, missing DLQs) and **cost awareness for scale** (FIFO vs Standard pricing gap grows at higher volumes).

---

## Data Collection

```python
sqs = session.client("sqs", region_name=region)
cw  = session.client("cloudwatch", region_name=region)

# List all queues — paginate via NextToken
resp = sqs.list_queues(MaxResults=1000)
# if resp.get("NextToken"): continue paginating

# Per-queue attributes
attrs = sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["All"])["Attributes"]
# Key: ApproximateNumberOfMessages, ApproximateNumberOfMessagesNotVisible,
#      MessageRetentionPeriod, VisibilityTimeout, RedrivePolicy (JSON string)

# CW 30d message volume — use ThreadPoolExecutor(max_workers=20) for 300+ queues
cw.get_metric_statistics(Namespace="AWS/SQS", MetricName="NumberOfMessagesSent",
    Dimensions=[{"Name":"QueueName","Value":name}],
    StartTime=now-timedelta(days=30), EndTime=now, Period=2592000, Statistics=["Sum"])
```

---

## Pricing (eu-central-1)

| Type | First 1M/month | Beyond free tier |
|------|---------------|-----------------|
| Standard | **FREE** | $0.40 / 1M requests |
| FIFO | **FREE** | $0.50 / 1M requests |

---

## Opportunities

### 1 — DLQ Backlogs (`sqs-dlq-backlog`) CRITICAL
Detection: DLQ queue (name contains dlq/dead/deadletter) with `ApproximateNumberOfMessages > 100`.
Large DLQ backlogs indicate processing failures in the consuming service. Messages in DLQs are not being processed — they are expired or abandoned events that may represent data loss.

### 2 — Queues Without DLQ (`sqs-queue-no-dlq`) HIGH
Detection: non-DLQ queue with empty `RedrivePolicy`. Without a DLQ, messages that exceed `maxReceiveCount` are permanently deleted. Silent data loss on processing failure.

### 3 — Large Non-DLQ Backlog (`sqs-large-backlog-non-dlq`) HIGH
Detection: non-DLQ queue with `ApproximateNumberOfMessages > 500`. Large backlogs in main queues indicate the consumer is falling behind or has stopped processing.

### 4 — Idle Queues (`sqs-idle-queues`) MEDIUM
Detection: 0 messages sent in 30d AND 0 messages currently waiting. Likely orphaned from decommissioned services.

### 5 — FIFO vs Standard Evaluation (`sqs-fifo-vs-standard`) INFO
Detection: FIFO queues that don't obviously require ordering. FIFO costs 25% more per million. 🔗 Requires consumer code changes to migrate.

---

## Real-World Learnings

### Account: `<YOUR_ACCOUNT_ID>`, Region: `eu-central-1`, March 2026

- **334 queues** (93 Standard, 241 FIFO)
- **154 DLQ queues**; **26 queues without any DLQ**
- **254 idle queues** (76% of fleet is dormant)
- **Total cost: ~$0** (well within free tier at <200K messages/30d)

**🚨 Critical backlogs:**
- `ctpeu-compliance-service-dev-eu-central-1-dlq.fifo` — **38,003 messages stuck**
- `ctpeu-home-charging-service-customer-update-dev-eu-central-1.fifo` — 8,202 msgs
- **Pattern**: many queues with exactly **2,867 messages** — likely a common upstream SNS fan-out failure affecting all subscribers simultaneously

**API quirk**: `list_queues(MaxResults=1000)` paginates via `NextToken`. Always check for NextToken even with MaxResults=1000.

**Performance**: With 334 queues, sequential CW calls take 5+ minutes. Always use `ThreadPoolExecutor(max_workers=20)` for CloudWatch metric fetches.
