# Skill: AWS WAF (WAFv2) — Cost-Saving Opportunities

## Service Overview

AWS WAFv2 charges a **flat monthly fee per Web ACL**, a **per-rule monthly fee**, and a
**per-million-requests processing fee**. The Bot Control add-on adds an additional flat fee
plus a higher per-request rate. The main savings levers are removing idle or misconfigured
Web ACLs, consolidating redundant custom rules, and reviewing overlapping managed rule groups.

---

## Data Collection

### AWS API Calls

```python
wafv2 = session.client("wafv2",      region_name=region)
cw    = session.client("cloudwatch", region_name=region)

# List all REGIONAL Web ACLs
wafv2.list_web_acls(Scope="REGIONAL", Limit=100)

# Full ACL detail: rules, capacity, default action
wafv2.get_web_acl(Name=name, Scope="REGIONAL", Id=acl_id)
# Returns: Rules[], Capacity, DefaultAction

# Resource associations (ALBs, API Gateways, AppSync, Cognito)
wafv2.list_resources_for_web_acl(WebACLArn=arn)

# Sampled requests — last 3 hours, quick traffic sanity check
wafv2.get_sampled_requests(
    WebAclArn=arn,
    RuleMetricName="ALL",
    Scope="REGIONAL",
    TimeWindow={"StartTime": (now - timedelta(hours=3)).isoformat(), "EndTime": now.isoformat()},
    MaxItems=100,
)

# CloudWatch: AllowedRequests, BlockedRequests, CountedRequests (30-day sum)
cw.get_metric_statistics(
    Namespace="AWS/WAFV2",
    MetricName="AllowedRequests",   # also BlockedRequests, CountedRequests
    Dimensions=[
        {"Name": "WebACL",  "Value": web_acl_name},
        {"Name": "Region",  "Value": region},
        {"Name": "Rule",    "Value": "ALL"},
    ],
    StartTime=now - timedelta(days=30),
    EndTime=now,
    Period=86400,   # daily resolution
    Statistics=["Sum"],
)
```

### Cache TTL
- Web ACL metadata (rules, associations, metrics): **24 hours** (shared `CACHE_TTL_DAYS` constant)

---

## Opportunity 1 — Zero-Traffic Web ACL Investigation

**Severity:** 🟡 MEDIUM  
**Opportunity type:** `waf-zero-traffic-investigation`

**Detection logic:**
- `AllowedRequests + BlockedRequests + CountedRequests = 0` over the last **30 days** in the
  `AWS/WAFV2` namespace (dimensions: `WebACL`, `Region`, `Rule=ALL`).
- Sampled requests (last 3 hours) also = 0 as a corroborating signal.
- Zero traffic indicates either:
  - **(a) Misconfiguration** — the Web ACL exists but no resources are associated, or the
    associated resources are not routing real traffic through it.
  - **(b) Idle ACL** — the protected resource has been decommissioned but the Web ACL was not
    cleaned up.

**Calculation:**
```
monthly_cost = WAF_ACL_MO + (rule_count × WAF_RULE_MO) [+ BOT_CONTROL_MO if applicable]
             = $5.00 + (rule_count × $1.00) [+ $10.00]
saving_mo    = monthly_cost   # full cost is potentially wasted
```

**Dependency warning:**
> Verify CloudWatch metrics in the `AWS/WAFV2` namespace with `WebACL + Region + Rule=ALL`
> dimensions before deleting. Metrics may have a delay of up to 5 minutes.

**Recommendation:**
> "Web ACL '{name}' has 0 detected requests over 30 days (AllowedRequests + BlockedRequests +
> CountedRequests = 0, sampled requests also 0). Either (a) the WAF is not receiving traffic —
> resource associations may be misconfigured ({assoc_count} associations found) — or
> (b) CloudWatch metrics are not being published. Check the AWS/WAFV2 namespace. If confirmed
> unused, deleting this Web ACL saves ${saving:.2f}/month (${WAF_ACL_MO:.2f} ACL +
> ${rule_count * WAF_RULE_MO:.2f} rules)."

---

## Opportunity 2 — High Custom Rule Count

**Severity:** 🟢 LOW  
**Opportunity type:** `waf-rule-count`

**Detection logic:**
- Web ACL has **≥ 3 custom rules** (non-managed rules).
- Each custom rule costs **$1.00/month** regardless of whether it matches any traffic.
- Custom rules may be redundant with each other or with enabled managed rule groups.

**Calculation:**
```
rule_cost_mo      = rule_count × $1.00
potential_saving  = custom_rule_count × $1.00   # if all custom rules removed
conservative_save = potential_saving × 0.5      # assume ~half could be removed
```

**Recommendation:**
> "Web ACL '{name}' has {rule_count} rules ({managed_count} managed, {custom_count} custom) at
> $1.00/rule/month = ${rule_count * 1.00:.2f}/month. Review the {custom_count} custom rule(s)
> ({rule_names}) for overlap with managed rule groups or rules no longer relevant to current
> traffic. Removing ~half could save ~${conservative_save:.2f}/month."

---

## Opportunity 3 — Overlapping Managed Rule Groups

**Severity:** ℹ️  INFO  
**Opportunity type:** `waf-managed-rule-group-cost`

**Detection logic:**
- Web ACL has **both** of the following AWS managed rule groups enabled simultaneously:
  - `AWSManagedRulesKnownBadInputsRuleSet`
  - `AWSManagedRulesCommonRuleSet`
- These two groups have **partial overlap** in the threat patterns they cover (e.g. Log4JRCE,
  SSRF patterns). Enabling both may provide redundant protection without additional security value.

**Calculation:**
```
saving_mo = $1.00   # cost of one managed rule group if removed
```

**Recommendation:**
> "Web ACL '{name}' has both AWSManagedRulesKnownBadInputsRuleSet and
> AWSManagedRulesCommonRuleSet enabled. These two managed rule groups have partial overlap
> (e.g. Log4JRCE, SSRF patterns). Review whether both are necessary or if the KnownBadInputs
> group covers your threat model adequately without CommonRuleSet. Removing one group saves
> $1.00/month."

---

## Pricing Reference (eu-central-1)

| Component | Price |
|---|---|
| Web ACL | $5.00 / month |
| Rule (custom or managed group) | $1.00 / rule / month |
| Requests processed | $0.60 / million requests |
| Bot Control add-on (per Web ACL) | $10.00 / month |
| Bot Control requests | $1.00 / million requests |

> Source: https://aws.amazon.com/waf/pricing/

---

## Pricing Sources

- WAF pricing is **hardcoded** as constants in `analyse_waf.py`. Update constants when AWS
  adjusts rates.
- Request-based costs are estimated from CloudWatch `AllowedRequests + BlockedRequests +
  CountedRequests` over 30 days.

---

## Notes

- Only **REGIONAL** scope Web ACLs are analysed. CloudFront-associated (CLOUDFRONT) Web ACLs
  must be queried from `us-east-1` and are not included.
- The `waf-rule-count` opportunity fires at **≥ 3 custom rules** (not the 5 mentioned in
  the task brief) — this reflects the actual threshold implemented in `analyse_waf.py`.
- Bot Control detection: the analyser checks for `AWSManagedRulesBotControlRuleSet` in the
  managed rules list and adds `$10.00/month` to the ACL's estimated cost.
- The managed rule overlap check is conservative — it only flags the specific pair
  `KnownBadInputsRuleSet` + `CommonRuleSet`. Additional overlap patterns can be added to the
  `OVERLAPPING_MANAGED` set in the analyser.
- Tag-based filtering: skip Web ACLs with tag `cost-analyser:ignore = true`.
