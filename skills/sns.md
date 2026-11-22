# Skill: Amazon SNS — Cost-Saving Opportunities

## Service Overview

Amazon SNS is effectively **free at dev-account volumes**. Key opportunities are **operational hygiene** (idle/test topics, no-subscriber topics) rather than direct cost savings.

---

## Data Collection

```python
sns = session.client("sns", region_name=region)
cw  = session.client("cloudwatch", region_name=region)

# Topics (paginated)
paginator = sns.get_paginator("list_topics")
# Per topic
sns.get_topic_attributes(TopicArn=arn)
# Key: SubscriptionsConfirmed, SubscriptionsPending, SubscriptionsDeleted,
#      FifoTopic ("true"/"false"), KmsMasterKeyId, Policy

# Subscriptions (paginated)
paginator = sns.get_paginator("list_subscriptions")
# Key: Protocol (sqs/lambda/http/email/sms), SubscriptionArn ("PendingConfirmation" = unconfirmed)

# CW 30d publish count — ThreadPoolExecutor for parallel fetches
cw.get_metric_statistics(Namespace="AWS/SNS", MetricName="NumberOfMessagesPublished",
    Dimensions=[{"Name":"TopicName","Value":name}], ...)
```

---

## Pricing (eu-central-1)

| Item | Price |
|------|-------|
| First 1M API requests/month | **FREE** |
| Standard topics (beyond free) | $0.50 / 1M |
| FIFO topics (beyond free) | $0.33 / 1M |
| SQS delivery (same region) | **FREE** |
| Lambda delivery | **FREE** |
| HTTP/S delivery | $0.60 / 1M |
| Email delivery | $2.00 / 1,000 (outside free tier) |

---

## Opportunities

### 1 — Test/POC Topics (`sns-test-poc-topics`) HIGH
Detection: topic name contains test/poc/demo/temp/TestTopic/poc. Zero-delivery orphans.

### 2 — Idle Topics (`sns-idle-topics`) MEDIUM
Detection: 0 messages published in 30 days. Likely orphaned.

### 3 — Topics With No Subscriptions (`sns-topics-no-subscriptions`) MEDIUM
Detection: `SubscriptionsConfirmed = 0`. Messages published are silently discarded — either the producer is misconfigured or the topic is orphaned.

### 4 — Duplicate Topic Names (`sns-duplicate-names`) LOW
Detection: two or more topics with near-identical names (e.g. `ctp-np-unreg-deviceids-deveu` vs `ctp-np-unregistred-deviceIds-dev-eu-central-1-sns`). Same functional purpose, two resources — consolidate.

### 5 — FIFO Topics Info (`sns-fifo-topics`) INFO
FIFO SNS → FIFO SQS fan-out is the dominant pattern. FIFO topics can only deliver to FIFO SQS queues. Verify fan-out targets are all FIFO SQS.

---

## Real-World Learnings

### Account: `<YOUR_ACCOUNT_ID>`, Region: `eu-central-1`, March 2026

- **27 topics**, **111 subscriptions** (110 SQS, 1 Lambda)
- **18 idle topics** (67%), **9 with 0 confirmed subscribers**
- **0 pending confirmations** ✅
- Total cost: **~$0** (all within free tier at ~30K messages/30d)

**Test/POC topics to delete:** `TestTopic.fifo`, `26dcm-sns-poc.fifo`
**Duplicate pair:** `ctp-np-unreg-deviceids-deveu` / `ctp-np-unregistred-deviceIds-dev-eu-central-1-sns` (same purpose, different spelling)
**Dead-end topic:** `ctpa-utils-dev-euc1-healthcheck` — 1 publish, 0 confirmed subscribers (healthcheck alerts going nowhere)
**Fan-out pattern:** `ctp-compliance-service-dev-eu.fifo` → 38 SQS subscribers — this is the likely source of the 2,867-message backlog pattern seen across SQS queues (single SNS fan-out failure affected all 38 subscribers simultaneously)
