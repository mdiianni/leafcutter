# Skill: AWS Lambda — Cost-Saving Opportunities

## Service Overview

Lambda costs are based on **number of invocations** and **GB-seconds of compute** (memory × duration).
Cost savings come from right-sizing memory, reducing unnecessary invocations, migrating to Graviton,
and reviewing timeout configurations.

---

## Data Collection

### AWS API Calls

```python
lambda_client = session.client("lambda", region_name=region)
cw            = session.client("cloudwatch", region_name=region)

# List all functions
lambda_client.list_functions()   # paginate with Marker

# Get concurrency per function
lambda_client.get_function_concurrency(FunctionName=fn_name)

# CloudWatch: Invocations, Duration, Errors, Throttles (last 15 days)
cw.get_metric_statistics(
    Namespace="AWS/Lambda",
    MetricName="Invocations",
    Dimensions=[{"Name": "FunctionName", "Value": fn_name}],
    StartTime=now - timedelta(days=15),
    EndTime=now,
    Period=86400,
    Statistics=["Sum"]
)

cw.get_metric_statistics(
    Namespace="AWS/Lambda",
    MetricName="Duration",
    Dimensions=[{"Name": "FunctionName", "Value": fn_name}],
    StartTime=now - timedelta(days=15),
    EndTime=now,
    Period=86400,
    Statistics=["Average", "p95", "Maximum"]
)

# Throttles
cw.get_metric_statistics(
    Namespace="AWS/Lambda",
    MetricName="Throttles",
    ...
)
```

### Derived metrics

```python
# Per function:
memory_mb          = fn["MemorySize"]
timeout_sec        = fn["Timeout"]
runtime            = fn["Runtime"]           # e.g. "python3.11", "nodejs18.x"
architecture       = fn["Architectures"][0]  # "x86_64" or "arm64"
last_modified      = fn["LastModified"]
code_size_bytes    = fn["CodeSize"]

avg_duration_ms    = from CloudWatch Duration Average
p95_duration_ms    = from CloudWatch Duration p95
daily_invocations  = from CloudWatch Invocations Sum (last 15 days)
monthly_invocations= daily_invocations * 2   # extrapolate
```

### Cache TTL
- Function metadata: **24 hours**
- CloudWatch metrics: **6 hours**

---

## Opportunity 1 — Migrate to Graviton (ARM64) Architecture

**Severity:** ⚠️ DEPENDENCY  
**Opportunity type:** `lambda-graviton-migration`

**Detection logic:**
- `architecture = "x86_64"` (Lambda ARM64 uses Graviton2).
- Lambda ARM64 is ~**20% cheaper** per GB-second than x86_64 AND often faster for
  interpreted runtimes (Python, Node.js, Java on JVM with ARM optimisations).

> ⚠️ **Dependency warning:** Migrating Lambda to ARM64 requires:
> - Any native extensions (C/C++ compiled code, `cffi`, `cryptography`, etc.) must be available
>   as ARM64 wheels/binaries.
> - Lambda Layers must be rebuilt for ARM64.
> - Container image-based Lambdas must use an ARM64 base image and be rebuilt.
> - For ZIP-based Lambdas using pure Python/Node/Ruby — this is generally a safe switch.

**Calculation:**
```
gb_seconds_per_month = (memory_mb / 1024) * (avg_duration_ms / 1000) * monthly_invocations
price_x86_gb_sec     = pricing["lambda"]["x86"]["gb_second"]   # e.g. $0.0000166667
price_arm_gb_sec     = pricing["lambda"]["arm64"]["gb_second"]  # e.g. $0.0000133334
monthly_saving       = gb_seconds_per_month * (price_x86_gb_sec - price_arm_gb_sec)
```

**Recommendation:**
> "Lambda function '{fn}' runs on x86_64. Migrating to ARM64 (Graviton2) saves ~${saving}/month
> (~20%). For pure Python/Node.js functions without native extensions, this is usually a
> straightforward configuration change."

---

## Opportunity 2 — Memory Over-provisioned (Duration Well Below Timeout)

**Severity:** 🟠 HIGH  
**Opportunity type:** `lambda-memory-overprovisioned`

**Detection logic:**
- `p95_duration_ms < timeout_sec * 1000 * 0.50` → function rarely uses more than 50% of its timeout.
- `memory_mb > 512` AND the function's workload type (inferred from name/tags) suggests
  it's not memory-intensive.

> Note: Lambda bills on **memory × duration**. More memory = faster execution (more CPU),
> which may reduce duration. The optimal memory is not always the minimum. Flag for review
> rather than prescribing a specific value.

**Recommendation:**
> "Lambda function '{fn}' is provisioned with {memory_mb} MB and times out at {timeout_sec}s,
> but average duration is {avg_duration_ms:.0f}ms (p95: {p95_ms:.0f}ms). Consider using the
> AWS Lambda Power Tuning tool (open-source Step Functions state machine) to find the
> optimal memory setting for cost and performance."

**Attach link:** https://github.com/alexcasalboni/aws-lambda-power-tuning

---

## Opportunity 3 — No Invocations in Last 15 Days (Unused Function)

**Severity:** 🟡 MEDIUM  
**Opportunity type:** `lambda-unused-function`

**Detection logic:**
- `Invocations Sum = 0` over the last 15 days.
- Function was last modified > 90 days ago.

**Recommendation:**
> "Lambda function '{fn}' has not been invoked in the last 15 days and was last modified
> {days} days ago. If it is no longer needed, consider deleting it to reduce attack surface
> and maintenance overhead. (Lambda itself has no idle cost, but it may be triggering other
> billable services.)"

---

## Opportunity 4 — Timeout Excessively High Relative to Duration

**Severity:** 🟢 LOW  
**Opportunity type:** `lambda-timeout-too-high`

**Detection logic:**
- `timeout_sec > 60` AND `maximum_duration_ms < timeout_sec * 1000 * 0.20`.
- An excessively high timeout means failures (bugs, downstream timeouts) will run at full
  memory allocation until the timeout expires, inflating costs.

**Recommendation:**
> "Lambda function '{fn}' has a {timeout_sec}s timeout but maximum observed duration is
> {max_duration_ms:.0f}ms. Reducing the timeout to {suggested_timeout}s would limit
> runaway invocations and reduce worst-case costs."

---

## Opportunity 5 — Deprecated Runtime

**Severity:** 🟠 HIGH  
**Opportunity type:** `lambda-deprecated-runtime`

**Detection logic:**
Match `runtime` against the list of deprecated/end-of-life Lambda runtimes:

| Runtime | Status |
|---|---|
| python3.7, python3.8 | Deprecated / End of Support |
| nodejs12.x, nodejs14.x, nodejs16.x | Deprecated |
| java8 (non-AL2) | Deprecated |
| dotnet5.0, dotnet6 | Deprecated |
| ruby2.7 | Deprecated |
| go1.x (provided.al1) | Deprecated |

Deprecated runtimes stop receiving security patches, creating a cost of risk. Flag as HIGH.

**Recommendation:**
> "Lambda function '{fn}' uses runtime '{runtime}' which is deprecated / end-of-life. Upgrade
> to a supported runtime to maintain security patches and access to latest Lambda features
> (including ARM64 support for newer runtimes)."

---

## Opportunity 6 — Reserved Concurrency Blocking Scaling

**Severity:** 🟡 MEDIUM  
**Opportunity type:** `lambda-reserved-concurrency-review`

**Detection logic:**
- `get_function_concurrency` returns a low `ReservedConcurrentExecutions` value (e.g., 1–5).
- `Throttles > 0` over the last 15 days.
- Throttled invocations may be retrying and inflating invocation counts.

**Recommendation:**
> "Lambda function '{fn}' has {reserved_concurrency} reserved concurrent executions and
> {throttle_count} throttle events in the last 15 days. Throttled invocations drive retry
> costs. Review whether the reserved concurrency limit is appropriate."

---

## Pricing Sources

| Item | x86_64 Price | ARM64 Price |
|---|---|---|
| Requests | $0.20 per 1M invocations | $0.20 per 1M invocations |
| Duration | $0.0000166667 per GB-second | $0.0000133334 per GB-second |
| Free tier | 1M requests + 400,000 GB-seconds/month | Same |

Fetch exact pricing: `pricing.get_products(ServiceCode="AWSLambda", ...)`

---

## Notes

- Lambda has a generous free tier (1M invocations/month). Only flag functions where the
  monthly cost exceeds $1 to reduce noise.
- For Lambda@Edge and CloudFront Functions, note they are billed differently and are not
  analysed by this skill.
- Tag-based filtering: skip functions with tag `cost-analyser:ignore = true`.
