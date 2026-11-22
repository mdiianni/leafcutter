# Skill: AWS SSM Parameter Store — Cost-Saving Opportunities

## Service Overview

AWS Systems Manager Parameter Store has two tiers. **Standard** parameters are
**free** (up to 10,000 per account per region). **Advanced** parameters cost
$0.05/parameter/month. The main cost-saving opportunities are: avoiding unnecessary
Advanced tier usage, cleaning up stale/orphaned parameters to reduce security surface,
and enforcing parameter expiration policies.

---

## Data Collection

```python
ssm = session.client("ssm", region_name=region)

paginator = ssm.get_paginator("describe_parameters")
for page in paginator.paginate():
    params.extend(page["Parameters"])
# Each param: Name, Type (String/StringList/SecureString), Tier, LastModifiedDate,
#             Description, Policies (list of expiry/notification policies)
```

---

## Pricing (eu-central-1)

| Item | Price |
|------|-------|
| Standard parameters (first 10,000) | **FREE** |
| Advanced parameters | **$0.05/parameter/month** |
| Standard API throughput (up to 40 TPS) | FREE |
| Higher throughput (paid) | $0.05 / 10,000 API interactions |

---

## Opportunities

### 1 — Advanced Tier Parameters (`ssm-advanced-params-cost`) HIGH
Detection: `Tier = "Advanced"`. Cost: $0.05/param/month.
Evaluate whether Advanced features are needed (> 4KB values, parameter policies, higher TPS).
If not, downgrade to Standard. **In-place downgrade is possible from Advanced to Standard
only if the parameter value is ≤ 4KB.**
Saving: `count_advanced × $0.05/month`.

### 2 — Stale Parameters (`ssm-stale-parameters`) MEDIUM
Detection: `LastModifiedDate` > 1 year ago.
Stale parameters are likely orphaned (services decommissioned, Terraform configs removed).
No direct cost saving for Standard tier, but reduces security surface and parameter clutter.
**Recommendation:** Export to CSV and review with owning teams.

### 3 — No Expiration Policies (`ssm-no-expiration-policies`) LOW
Detection: `Policies` array is empty on all parameters.
SSM Advanced tier supports `ExpirationNotification` and `Expiration` policies to auto-delete
or alert on old parameters. Without them, parameters accumulate indefinitely.
**Note:** Policies require Advanced tier.

### 4 — String-Type Credentials (`ssm-string-type-credentials`) MEDIUM
Detection: `Type = "String"` (not SecureString) AND name contains: `password`, `key`,
`secret`, `token`, `credential`, `passwd`.
Plaintext credential parameters are stored unencrypted and visible in AWS Console / CloudTrail.
Upgrade to `SecureString` (still free at Standard tier with AWS-managed KMS key).

### 5 — Flat Parameter Names (`ssm-flat-params`) LOW
Detection: parameter name does not start with `/`.
Best practice: hierarchical paths (`/service/env/key`) enable IAM path-based access control
and parameter retrieval by path (`GetParametersByPath`).

---

## Real-World Learnings

### Account: `<YOUR_ACCOUNT_ID>`, Region: `eu-central-1`, March 2026

- 485 parameters, **all Standard tier → $0/month** ✅
- Types: 481 SecureString ✅, 4 String
- **478 of 485 (98.6%) not modified in 1+ year** — massive stale parameter debt
- 0 expiration policies
- 3 flat-named parameters (not using path hierarchy)
- Top paths all under `/config/` — Spring Cloud Config Server pattern (services read config at startup)
- `CACHE_TTL_DAYS = 30` — Standard parameter metadata is stable

**API quirk:** `describe_parameters` paginates by `NextToken` automatically via `get_paginator`.
No need to handle NextToken manually. Returns up to 50 parameters per page by default.
