# Skill: Amazon API Gateway — Cost-Saving Opportunities

## Service Overview

API Gateway costs come from **request volume** and **cache provisioning**. REST (v1) at $3.50/1M is 3.5× more expensive than HTTP (v2) at $1.00/1M. API cache is $0.020–$3.80/hr depending on size — significant fixed cost when enabled.

---

## Data Collection

```python
apigw  = session.client("apigateway",   region_name=region)  # REST v1
apigw2 = session.client("apigatewayv2", region_name=region)  # HTTP/WebSocket v2
cw     = session.client("cloudwatch",   region_name=region)

# REST v1
paginator = apigw.get_paginator("get_rest_apis")
apigw.get_stages(restApiId=id)
# Stage: cacheClusterEnabled, cacheClusterSize, cacheClusterStatus, accessLogSettings

# HTTP/WebSocket v2
apigw2.get_apis()       # no paginator for small counts
apigw2.get_stages(ApiId=id)

# Usage plans & keys
apigw.get_usage_plans()
apigw.get_api_keys(includeValues=False)

# CW request counts 30d
# REST: Namespace="AWS/ApiGateway", MetricName="Count"
#   Dims: ApiName + Stage
# HTTP/WS: same namespace, Dims: ApiId + Stage
# Cache: MetricName="CacheHitCount", "CacheMissCount"
```

---

## Pricing (eu-central-1)

| Item | Price |
|------|-------|
| REST API (v1) | **$3.50 / 1M requests** |
| HTTP API (v2) | **$1.00 / 1M requests** |
| WebSocket messages | $1.00 / 1M |
| WebSocket connection-minutes | $0.29 / 1M |
| Cache 0.5 GB | $0.020/hr = **$14.40/month** |
| Cache 1.6 GB | $0.038/hr = **$27.74/month** |
| Cache 6.1 GB | $0.200/hr = **$146/month** |

---

## Opportunities

### 1 — Idle REST APIs (`apigw-idle-apis`) HIGH
Detection: all stages of a REST API show 0 requests in 30d. Orphaned APIs still consume API keys, usage plans, and IAM policies. Delete to reduce attack surface.

### 2 — POC / Test APIs (`apigw-poc-api`) HIGH
Detection: API name contains poc/test/demo/temp. Always 0 requests.

### 3 — REST → HTTP API Migration (`apigw-rest-to-http-migration`) MEDIUM 🔗
Detection: REST (v1) API that doesn't use REST-exclusive features.
Saving: 71% reduction ($3.50 → $1.00 per 1M). Requires consumer + backend changes.
REST-exclusive features that block migration: request/response transforms, AWS service proxy integrations, API keys with usage plans on per-method level, custom GATEWAY_RESPONSE types.

### 4 — Duplicate Stages (`apigw-duplicate-stages`) MEDIUM
Detection: API with multiple stages where one stage has 0 requests and another is active.

### 5 — Active API Cache With Low Utilisation (`apigw-cache-waste`) HIGH
Detection: `cacheClusterEnabled=True` AND `cacheClusterStatus=AVAILABLE` AND either:
- Requests/30d < 1,000 (cache cost exceeds request cost significantly), OR
- `CacheHitCount/(CacheHitCount+CacheMissCount) < 10%` (cache not effective)
Saving: cache price/month.

### 6 — API Keys Audit (`apigw-api-keys-audit`) LOW
Detection: enabled API keys > 10. Each key is a potential attack vector if leaked.

### 7 — Missing Access Logs (`apigw-missing-access-logs`) LOW
Detection: stage has no `accessLogSettings.destinationArn`. Without access logs, debugging is limited to aggregated CW metrics.

---

## Real-World Learnings

### Account: `<YOUR_ACCOUNT_ID>`, Region: `eu-central-1`, March 2026

- **18 REST APIs** (v1), **1 WebSocket** (v2), **0 HTTP APIs** (v2)
- Total REST requests 30d: ~190,000 → **~$0.67/month**
- **No caches currently active** (cacheClusterEnabled=False everywhere) → **$0 cache cost** ✅
- **7+ REST APIs with 0 requests** (idle/orphaned)
- `26dcm-poc-vpc-private-api-gateway` — POC with 2 test stages, 0 requests → delete
- Top APIs: ctpa-oneapi (102,284 req), ctpeu-forgerock (81,868 req)
- **22 API keys all enabled** — needs audit for unused keys
- **15 usage plans** — complex throttle/quota setup

**REST→HTTP saving potential**: At 190K req/30d: current $0.67/mo → $0.19/mo = $0.48/mo ($5.76/yr). Small now but 71% reduction compounds with growth.

**Cache configured but disabled**: Several stages have `cacheClusterSize=0.5` configured but `cacheClusterEnabled=False`. No charge, but risk of accidentally enabling a $14.40/month cache. Remove configuration if caching is not planned.

**API `cacheClusterStatus` values:**
- `AVAILABLE` → cache IS running → you ARE being charged
- `NOT_AVAILABLE` → no cache cluster → $0
- Always check `cacheClusterStatus`, not just `cacheClusterEnabled`

**SNS→API Gateway integration**: `sns-email-delivery-status` topic has an SQS subscription — this feeds the `deveu-notifications` REST API which processes SES delivery events.
