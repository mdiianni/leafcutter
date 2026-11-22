# Skill: Elastic Load Balancing (ALB / NLB / CLB) — Cost-Saving Opportunities

## Service Overview

Elastic Load Balancers charge for **Load Balancer Capacity Units (LCUs)** or **hours** depending
on type, plus a per-hour idle charge. The main cost opportunities are eliminating unused / idle
load balancers, consolidating target groups, and reviewing classic load balancers.

---

## Data Collection

### AWS API Calls

```python
elbv2 = session.client("elbv2", region_name=region)    # ALB + NLB
elb   = session.client("elb", region_name=region)      # Classic LBs (v1)
cw    = session.client("cloudwatch", region_name=region)

# ALB and NLB
elbv2.describe_load_balancers()
elbv2.describe_target_groups()
elbv2.describe_listeners(LoadBalancerArn=lb_arn)

# Target health (per target group)
elbv2.describe_target_health(TargetGroupArn=tg_arn)

# Classic Load Balancers
elb.describe_load_balancers()
elb.describe_instance_health(LoadBalancerName=lb_name)

# CloudWatch: RequestCount per ALB (last 15 days)
cw.get_metric_statistics(
    Namespace="AWS/ApplicationELB",
    MetricName="RequestCount",
    Dimensions=[{"Name": "LoadBalancer", "Value": lb_dimension_name}],
    StartTime=now - timedelta(days=15),
    EndTime=now,
    Period=86400,
    Statistics=["Sum"]
)

# CloudWatch: ActiveFlowCount per NLB
cw.get_metric_statistics(
    Namespace="AWS/NetworkELB",
    MetricName="ActiveFlowCount",
    ...
)

# CloudWatch: RequestCount per CLB
cw.get_metric_statistics(
    Namespace="AWS/ELB",
    MetricName="RequestCount",
    ...
)
```

### Derived metrics

```python
# ALB
alb_has_targets    = any(t["HealthStatus"] == "healthy" for t in target_health)
alb_request_count  = sum of RequestCount over 15 days
alb_has_listeners  = len(listeners) > 0

# NLB
nlb_active_flows   = from ActiveFlowCount
```

### Cache TTL
- LB metadata + target group health: **24 hours**
- CloudWatch metrics: **6 hours**

---

## Opportunity 1 — Idle / Unused Application Load Balancer

**Severity:** 🔴 CRITICAL  
**Opportunity type:** `alb-idle`

**Detection logic:**
- `RequestCount = 0` over the last 15 days, OR
- All target groups have 0 healthy targets, OR
- No listeners defined.

ALBs charge ~$0.008/LCU-hour + $0.0225/hour idle. An idle ALB costs ~$16/month minimum.

**Calculation:**
```
idle_cost_monthly = pricing["alb"]["hourly"] * 730
```

**Recommendation:**
> "ALB '{lb_name}' has had {request_count} requests over the last 15 days and {healthy_count}
> healthy targets. This load balancer appears idle. Delete it if it is no longer serving traffic
> to save ~${idle_cost}/month."

---

## Opportunity 2 — Classic Load Balancer (CLB) → ALB Migration

**Severity:** 🟠 HIGH  
**Opportunity type:** `elb-classic-to-alb`

**Detection logic:**
- Any Classic Load Balancer (`elb` client) found.
- CLBs are legacy, cost more per LCU, and lack modern features (path-based routing,
  HTTP/2, WebSockets, WAF integration).
- AWS has signalled CLB is end-of-life (no new features).

**Recommendation:**
> "Classic Load Balancer '{lb_name}' is a legacy ELB v1. Migrating to an Application Load
> Balancer (ALB) provides better routing capabilities, lower per-request cost, and access to
> WAF and Lambda targets. AWS provides a migration tool: `aws-load-balancer-controller`."

---

## Opportunity 3 — Idle / Unused Network Load Balancer

**Severity:** 🔴 CRITICAL  
**Opportunity type:** `nlb-idle`

**Detection logic:**
- `ActiveFlowCount` average = 0 over the last 15 days.
- No healthy registered targets.

**Recommendation:**
> "NLB '{lb_name}' has had zero active flows over the last 15 days. It appears idle and is
> costing ~${idle_cost}/month. Delete it if no longer in use."

---

## Opportunity 4 — Load Balancer With No Healthy Targets

**Severity:** 🔴 CRITICAL  
**Opportunity type:** `lb-no-healthy-targets`

**Detection logic:**
- All target groups associated with a load balancer have **0 healthy targets**.
- The LB is forwarding traffic to nothing (or serving 503s).

**Recommendation:**
> "Load balancer '{lb_name}' has no healthy targets across all target groups. Either the
> associated service is down (operational issue) or the load balancer is unused. Investigate
> and delete if confirmed unused."

---

## Opportunity 5 — Multiple Underutilised ALBs: Consolidation Opportunity

**Severity:** 🟡 MEDIUM  
**Opportunity type:** `alb-consolidation`

**Detection logic:**
- > 3 ALBs in the same VPC, each with `RequestCount < 1000/day` average.
- ALBs support host-based and path-based routing — many low-traffic services could share one ALB
  via routing rules (up to 100 rules per ALB listener).

**Recommendation:**
> "The account has {alb_count} ALBs in VPC '{vpc_id}', several with very low traffic. Consider
> consolidating low-traffic services onto fewer ALBs using host-based routing rules, reducing
> idle LB charges. Each ALB saved reduces costs by ~${per_lb_monthly}/month."

---

## Opportunity 6 — ALB Access Logs Not Stored in S3 with Lifecycle

**Severity:** 🟢 LOW  
**Opportunity type:** `alb-access-log-cost`

**Detection logic:**
- ALB access logs are enabled (`access_logs.s3.enabled = true`).
- The target S3 bucket has no lifecycle policy (cross-reference with S3 skill).
- ALB log volumes can be substantial for high-traffic load balancers.

**Recommendation:**
> "ALB '{lb_name}' has access logging enabled but the destination S3 bucket '{bucket}' has
> no lifecycle policy. ALB logs can grow quickly. Add a lifecycle rule to expire logs after
> 30–90 days."

---

## Pricing Sources

| Item | ALB Price | NLB Price | CLB Price |
|---|---|---|---|
| Hourly (idle) | ~$0.0225/hr | ~$0.0225/hr | ~$0.025/hr |
| LCU/NLCU | ~$0.008/LCU-hr | ~$0.006/NLCU-hr | N/A |

Fetch exact pricing: `pricing.get_products(ServiceCode="AWSElasticLoadBalancing", ...)`

---

## Notes

- The `LoadBalancer` dimension value for CloudWatch is the suffix of the ALB ARN
  (e.g., `app/my-alb/50dc6c495c0c9188`). Extract from ARN accordingly.
- Tag-based filtering: skip load balancers with tag `cost-analyser:ignore = true`.
- Cross-reference ALB idle findings with ECS services — if an ECS service was deleted but
  its load balancer remains, it will appear idle here.
