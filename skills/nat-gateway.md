# Skill: AWS NAT Gateway — Cost-Saving Opportunities

## Service Overview

NAT Gateways are deceptively expensive. They charge both for **idle gateway hours** AND for
**every byte of data processed**. In large accounts, NAT Gateway data processing fees frequently
exceed compute costs. The principal levers are consolidation, VPC endpoints, and reviewing
high-traffic data flows.

---

## Data Collection

### AWS API Calls

```python
ec2 = session.client("ec2", region_name=region)
cw  = session.client("cloudwatch", region_name=region)

# List all NAT Gateways
ec2.describe_nat_gateways(Filters=[{"Name": "state", "Values": ["available"]}])

# List VPCs
ec2.describe_vpcs()

# List Subnets (to understand NAT Gateway placement)
ec2.describe_subnets()

# List VPC Endpoints (to identify which services already have endpoints)
ec2.describe_vpc_endpoints()

# CloudWatch: BytesInFromDestination (data processed through NAT GW, last 15 days)
cw.get_metric_statistics(
    Namespace="AWS/NATGateway",
    MetricName="BytesInFromDestination",
    Dimensions=[{"Name": "NatGatewayId", "Value": nat_id}],
    StartTime=now - timedelta(days=15),
    EndTime=now,
    Period=86400,
    Statistics=["Sum"]
)

# BytesOutToDestination (outbound through NAT GW)
cw.get_metric_statistics(
    Namespace="AWS/NATGateway",
    MetricName="BytesOutToDestination",
    ...
)

# ActiveConnectionCount (detect idle gateways)
cw.get_metric_statistics(
    Namespace="AWS/NATGateway",
    MetricName="ActiveConnectionCount",
    ...
)
```

### Cache TTL
- NAT Gateway metadata + VPC endpoints: **24 hours**
- CloudWatch traffic metrics: **6 hours**

---

## Opportunity 1 — S3 Traffic Flowing Through NAT Gateway

**Severity:** 🔴 CRITICAL  
**Opportunity type:** `nat-s3-vpc-endpoint`

**Detection logic:**
- `describe_vpc_endpoints` → no Gateway VPC Endpoint for S3 exists in the VPC containing the
  NAT Gateway.
- S3 to/from EC2/ECS/Lambda traffic passes through the NAT Gateway, incurring data processing
  charges ($0.045/GB in most regions).
- S3 Gateway Endpoints are **free** — zero NAT Gateway data processing cost for S3 traffic.

**Calculation:**
```
# We cannot easily isolate S3 traffic from total NAT GW traffic at this point.
# Use a conservative estimate: assume 20% of NAT GW traffic is S3 (flag as conservative).
total_bytes_15d  = sum of BytesInFromDestination + BytesOutToDestination over 15 days
total_bytes_mo   = total_bytes_15d * 2
s3_bytes_est_mo  = total_bytes_mo * 0.20   # conservative estimate
nat_price_per_gb = pricing["nat"]["data_processing"]   # e.g. $0.045/GB
monthly_saving   = s3_bytes_est_mo / 1e9 * nat_price_per_gb
```

**Recommendation:**
> "VPC '{vpc_id}' has a NAT Gateway but no S3 Gateway VPC Endpoint. Creating a free S3 VPC
> Endpoint will route all S3 traffic within the AWS network, bypassing NAT Gateway data
> processing charges. Estimated saving: ~${saving}/month (conservative)."

---

## Opportunity 2 — DynamoDB Traffic Flowing Through NAT Gateway

**Severity:** 🔴 CRITICAL  
**Opportunity type:** `nat-dynamodb-vpc-endpoint`

**Detection logic:**
- No Gateway VPC Endpoint for DynamoDB exists in the VPC.
- Same as S3 — DynamoDB Gateway Endpoints are **free**.

**Recommendation:**
> "VPC '{vpc_id}' has no DynamoDB Gateway VPC Endpoint. Adding one is free and routes
> DynamoDB traffic within the AWS network, eliminating NAT Gateway data processing fees
> for DynamoDB calls."

---

## Opportunity 3 — AWS Service Traffic: Interface VPC Endpoints

**Severity:** 🟠 HIGH  
**Opportunity type:** `nat-interface-vpc-endpoint`

**Detection logic:**
- No Interface VPC Endpoints for commonly used services (ECR, Secrets Manager, SSM,
  CloudWatch Logs, STS, etc.) exist in the VPC.
- Traffic to these services flows through the NAT Gateway.

> Note: Interface VPC Endpoints **are not free** (~$0.01/hour per AZ + $0.01/GB).
> Only recommend when the NAT Gateway data processing cost for those services exceeds
> the endpoint cost.

**Services to check:**

| Service | VPC Endpoint service name |
|---|---|
| ECR API | `com.amazonaws.{region}.ecr.api` |
| ECR Docker | `com.amazonaws.{region}.ecr.dkr` |
| Secrets Manager | `com.amazonaws.{region}.secretsmanager` |
| SSM | `com.amazonaws.{region}.ssm` |
| CloudWatch Logs | `com.amazonaws.{region}.logs` |
| STS | `com.amazonaws.{region}.sts` |
| KMS | `com.amazonaws.{region}.kms` |

**Recommendation:**
> "VPC '{vpc_id}' does not have VPC Endpoints for ECR, Secrets Manager, and SSM. If your
> workloads pull Docker images or secrets frequently, Interface VPC Endpoints may cost less
> than the NAT Gateway data processing fees for these services."

---

## Opportunity 4 — Idle NAT Gateway (No Traffic)

**Severity:** 🔴 CRITICAL  
**Opportunity type:** `nat-idle-gateway`

**Detection logic:**
- `ActiveConnectionCount` = 0 over the last 15 days.
- `BytesInFromDestination + BytesOutToDestination` = 0 over the last 15 days.
- Still incurring idle per-hour charge (~$0.045/hour = ~$32.85/month).

**Calculation:**
```
idle_cost_monthly = pricing["nat"]["hourly"] * 730
```

**Recommendation:**
> "NAT Gateway '{nat_id}' has had zero traffic over the last 15 days and is costing
> ~${idle_cost}/month in idle charges. Delete it if no longer required."

---

## Opportunity 5 — Multiple NAT Gateways in Same VPC (Consolidation)

**Severity:** 🟡 MEDIUM  
**Opportunity type:** `nat-consolidation`

**Detection logic:**
- VPC has > 1 NAT Gateway.
- Multiple NAT Gateways may exist for high-availability (one per AZ) — this is correct
  architecture for production.
- Flag only if the account has **non-production VPCs** with multiple NAT Gateways (infer
  from VPC/subnet tags).

**Recommendation:**
> "VPC '{vpc_id}' (tagged as '{env}') has {nat_count} NAT Gateways. For non-production
> environments, a single NAT Gateway in one AZ is sufficient and saves ~${saving}/month
> in idle charges."

---

## Pricing Sources

| Item | Price (approx.) |
|---|---|
| NAT Gateway hourly | ~$0.045/hour (~$32.85/month) |
| Data processing | ~$0.045/GB processed |
| S3 Gateway Endpoint | Free |
| DynamoDB Gateway Endpoint | Free |
| Interface VPC Endpoint | ~$0.01/hour/AZ + $0.01/GB |

Fetch exact pricing: `pricing.get_products(ServiceCode="AmazonVPC", ...)`

---

## Notes

- NAT Gateway is one of the most underestimated cost drivers. Always show annualised savings
  prominently on the dashboard for NAT opportunities.
- If AWS Cost Explorer is available (`ce.get_cost_and_usage`), use it to confirm actual
  NAT Gateway spend rather than estimating from metrics.
- Tag-based filtering: skip NAT Gateways with tag `cost-analyser:ignore = true`.
