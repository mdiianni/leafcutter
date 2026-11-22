# Skill: AWS Config — Cost-Saving Opportunities

---

## Pricing Model

AWS Config pricing has three independent cost dimensions:

| Dimension | Price | Notes |
|---|---|---|
| Config item recorded | $0.003 / item | Charged per configuration change captured |
| Config rule evaluation | $0.001 / 1,000 evaluations | Per active rule per evaluation cycle |
| Conformance pack evaluation | $0.001 / 1,000 evaluations | Separate from standalone rule evals |

**There is no base/monthly subscription fee** — you pay only for what is recorded and evaluated.

### What triggers a config item?
A config item is recorded each time a resource's configuration state changes (e.g., a security group rule is modified, an EC2 instance changes state). High-churn resource types generate disproportionately more items per resource:

| Resource Type | Est. items/resource/month | Notes |
|---|---|---|
| `AWS::SSM::ManagedInstanceInventory` | ~8 | Inventory data updates frequently |
| `AWS::CloudWatch::Alarm` | ~5 | State changes (OK/ALARM/INSUFFICIENT) |
| `AWS::EC2::NetworkInterface` | ~4 | IP attachment/detachment, state changes |
| `AWS::Lambda::LayerVersion` | ~4 | New version publishes = new config item |
| `AWS::CloudFormation::Stack` | ~3 | Stack updates trigger items |
| Most other types | ~1 | Infrequent configuration changes |

---

## Data Collection

### AWS API Calls

```python
cfg = session.client("config", region_name=region)
s3  = session.client("s3",    region_name=region)

# Recorder configuration (what is being recorded)
cfg.describe_configuration_recorders()
cfg.describe_configuration_recorder_status()

# Where Config data is delivered (S3 bucket, SNS topic)
cfg.describe_delivery_channels()

# Standalone config rules (paginated via NextToken)
cfg.describe_config_rules()

# Conformance packs (paginated via NextToken)
cfg.describe_conformance_packs()

# Currently tracked resource inventory (paginated via nextToken — lowercase)
cfg.get_discovered_resource_counts()

# Compliance status per rule (paginated)
cfg.describe_compliance_by_config_rule()

# Remediation configurations (max 25 rule names per call)
cfg.describe_remediation_configurations(ConfigRuleNames=[...])

# S3 lifecycle on delivery bucket
s3.get_bucket_lifecycle_configuration(Bucket=bucket_name)
# Raises NoSuchLifecycleConfiguration if no policy exists
```

### Important API quirks

- `get_discovered_resource_counts()` uses lowercase `nextToken` (not `NextToken`) for pagination
- `describe_remediation_configurations()` accepts at most 25 rule names per call — batch accordingly
- If Config is not set up, `describe_configuration_recorders()` may raise `NoSuchConfigurationRecorderException` — handle gracefully
- The `recordingGroup.exclusionByResourceTypes` field is nested: `{"resourceTypes": [...]}`
- Compliance type `"NON_COMPLIANT"` in `describe_compliance_by_config_rule()` means ≥1 resource is non-compliant; `"COMPLIANT"` means all in scope are compliant

### Recorder recording group structure

```python
{
  "name": "default",
  "roleARN": "arn:aws:iam::123456789:role/aws-config-role",
  "recordingGroup": {
    "allSupported": True,                    # True = record all resource types
    "includeGlobalResourceTypes": False,     # True = also record IAM, Route53, etc.
    "resourceTypes": [],                     # Specific list (used if allSupported=False)
    "exclusionByResourceTypes": {
      "resourceTypes": []                    # Types to EXCLUDE when allSupported=True
    },
    "recordingStrategy": {
      "useOnly": "ALL_SUPPORTED_RESOURCE_TYPES"  # or "INCLUSION_BY_RESOURCE_TYPES" / "EXCLUSION_BY_RESOURCE_TYPES"
    }
  }
}
```

---

## Opportunity 1 — High Config Item Recording Cost (Many Types × High Churn)

**Severity:** 🟡 MEDIUM  
**Opportunity type:** `config-high-item-recording-cost`

**Detection logic:**
- Get total resources per type from `get_discovered_resource_counts()`
- Estimate monthly items: `count × churn_factor` per type (use defaults for unknown types)
- Total monthly cost = `total_items × $0.003`
- Flag if estimated monthly item cost > $50

**High-churn types to exclude (common examples):**
- `AWS::CloudWatch::Alarm` — state transitions generate many items
- `AWS::Lambda::LayerVersion` — each publish = new immutable resource = config item
- `AWS::EC2::NetworkInterface` — highly dynamic in ECS/EKS environments
- `AWS::SSM::ManagedInstanceInventory` — updates on every inventory sync
- `AWS::CloudFormation::Stack` — update-heavy in CI/CD environments

**Cost calculation:**
```python
CONFIG_ITEM_COST = 0.003
churn_factors = {
    "AWS::SSM::ManagedInstanceInventory": 8.0,
    "AWS::CloudWatch::Alarm": 5.0,
    "AWS::EC2::NetworkInterface": 4.0,
    "AWS::Lambda::LayerVersion": 4.0,
    "AWS::CloudFormation::Stack": 3.0,
}
DEFAULT_CHURN = 1.0

monthly_items = sum(
    count * churn_factors.get(rtype, DEFAULT_CHURN)
    for rtype, count in resource_counts.items()
)
monthly_cost = monthly_items * CONFIG_ITEM_COST

# Saving estimate: 40% reduction from excluding high-churn types
high_churn_items = sum(
    count * churn_factors[rtype]
    for rtype, count in resource_counts.items()
    if rtype in churn_factors
)
saving_mo = high_churn_items * CONFIG_ITEM_COST * 0.40
```

**Recommendation:**
> "Estimated {N} config items/month across {R} resources (~${cost}/month). High-churn types:
> {list}. Use exclusionByResourceTypes to exclude types that do not require a change audit trail.
> Reducing high-churn types can cut recording volume by 30–50%."

---

## Opportunity 2 — Conformance Pack Evaluation Cost (Overlapping Packs)

**Severity:** 🟡 MEDIUM  
**Opportunity type:** `config-conformance-pack-overlap`

**Detection logic:**
- Count active conformance packs from `describe_conformance_packs()`
- Flag if count > 5 (high overlap probability)
- Common overlapping rules between CIS, AWS Security Hub, PCI-DSS, NIST packs:
  - `s3-bucket-public-access-prohibited`
  - `iam-password-policy`
  - `cloudtrail-enabled`
  - `root-account-mfa-enabled`

**Cost estimation:**
```python
CONFORMANCE_PACK_COST = 0.001  # per 1000 evaluations

# Rough estimate — actual depends on rules per pack and resource scope
avg_rules_per_pack = 20
eval_count = num_packs * avg_rules_per_pack * total_resources
monthly_cost = (eval_count / 1000) * CONFORMANCE_PACK_COST
saving_mo = monthly_cost * 0.40  # ~40% from consolidation to ≤3 packs
```

**Recommendation:**
> "Audit rule overlap across {N} active conformance packs. Consolidate to ≤3 packs by
> identifying rules common to multiple packs. Each evaluation costs $0.001/1000 evals —
> duplicate evaluations waste budget without additional compliance signal."

---

## Opportunity 3 — Delivery Channel S3 Bucket Without Lifecycle Policy

**Severity:** 🟢 LOW  
**Opportunity type:** `config-delivery-bucket-no-lifecycle`

**Detection logic:**
- Get bucket name from `describe_delivery_channels()[].s3BucketName`
- Call `s3.get_bucket_lifecycle_configuration(Bucket=bucket)`
- If `NoSuchLifecycleConfiguration` is raised → no lifecycle policy exists

**Context:**
- AWS Config writes configuration history files daily and snapshots on demand/schedule
- Without lifecycle rules, all objects accumulate at S3 Standard pricing ($0.023/GB-month)
- Config history files for large accounts can reach hundreds of GB over 1–2 years
- S3 Intelligent-Tiering or Glacier transition can reduce older data costs by 60–80%

**Recommendation:**
> "Delivery channel bucket '{bucket}' has no lifecycle policy. Add a lifecycle rule to
> transition objects to S3 Intelligent-Tiering after 30 days and expire after 365–730 days.
> This can reduce storage costs by 60–80% for historical config data."

---

## Opportunity 4 — Config Rules Perpetually NON_COMPLIANT With No Remediation

**Severity:** 🟢 LOW  
**Opportunity type:** `config-rules-no-remediation`

**Detection logic:**
- For each rule in `describe_config_rules()`, check compliance from `describe_compliance_by_config_rule()`
- Cross-reference with `describe_remediation_configurations()` (25 rules per call max)
- Flag rules where `ComplianceType == "NON_COMPLIANT"` AND no remediation is configured

**Why this matters:**
- Rules that are always non-compliant generate continuous evaluation costs with no corrective action
- $0.001 per 1,000 evaluations is small per rule, but it multiplies across many rules and resources
- More importantly: perpetually NON_COMPLIANT rules generate alert fatigue and compliance noise

**Cost calculation (indicative — actual evals depend on resource scope):**
```python
RULE_EVAL_COST = 0.001  # per 1000 evaluations

# Estimate evaluations per rule per month
eval_per_rule = max(total_resources / 10, 100)
saving_mo = num_non_compliant_no_remedy * (eval_per_rule / 1000) * RULE_EVAL_COST
```

**Remediation options:**
1. Add an SSM Automation document as the rule's remediation action
2. Narrow rule scope with resource tags or IDs to reduce evaluations  
3. Delete the rule if compliance is not being enforced or tracked

**Dependency warning:** Deleting a rule removes its compliance history. Export data before deletion if audit records are required.

---

## Opportunity 5 — Multi-Region Recording With All Resource Types

**Severity:** 🟠 HIGH  
**Opportunity type:** `config-all-resources-all-regions`

**Detection logic:**
- Check recorder `recordingGroup.allSupported == True`
- Check that `recordingGroup.exclusionByResourceTypes.resourceTypes` is empty
- If both conditions met: recording every resource type with no exclusions

**Why this is high-severity:**
- Recording all types in all regions compounds costs: each region is independently billed
- `includeGlobalResourceTypes=True` in multiple regions records IAM, Route53, and other global resources multiple times (duplication)
- High-churn types (NetworkInterface, CloudWatch::Alarm) generate items across ALL regions

**Cost impact:**
- If Config is enabled in 5 regions with allSupported=True: estimated cost ≈ 5× single-region cost
- Global resource duplication (IAM alone can have thousands of entities): adds significant items

**Recommendation:**
```
1. Use exclusionByResourceTypes to exclude high-churn, low-audit-value types:
   - AWS::CloudWatch::Alarm
   - AWS::Lambda::LayerVersion
   - AWS::EC2::NetworkInterface
   - AWS::SSM::ManagedInstanceInventory
   - AWS::CodeDeploy::DeploymentConfig

2. Set includeGlobalResourceTypes=true in ONE region only (primary/home region)
   Disable it in all other regions to prevent IAM/Route53 duplication.

3. Use EXCLUSION_BY_RESOURCE_TYPES recording strategy for easier management
   (add exclusions rather than maintaining an explicit inclusion list)
```

**Estimated saving:** ~35% reduction from scoping exclusions in a typical account.

---

## Multi-Region Considerations

When AWS Config is deployed across multiple regions:

- Each region is independently billed — `$0.003/item` applies per region
- Global resource types (`AWS::IAM::*`, `AWS::Route53::*`) should only be recorded in one region
- Use AWS Config aggregators (`put_configuration_aggregator`) to get a cross-region inventory view without recording in every region
- Consider using AWS Organizations Config rules to centrally manage cross-account compliance without per-account conformance pack duplication

---

## Notes and Caveats

- `get_discovered_resource_counts()` reflects the current inventory snapshot, not actual recorded items. Use churn factors as estimates — actual item count requires CloudTrail analysis or AWS Cost Explorer.
- Rule evaluation costs are very small per rule (~$0.001/month for most accounts). The primary opportunity with rules is reducing compliance noise, not cost savings.
- Conformance pack evaluation costs are also modest unless you have very large resource counts (>10,000 resources × many packs).
- The highest-leverage optimisation is almost always **restricting the recorder to relevant resource types** in accounts with high-churn workloads (ECS/EKS, heavy CloudFormation usage, large SSM fleets).
- AWS Config is often enabled for compliance reasons — any recording restriction must be validated against your compliance framework requirements before implementation.
