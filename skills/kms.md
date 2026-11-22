# Skill: AWS KMS — Cost-Saving Opportunities

## Service Overview

AWS KMS costs come from **Customer Managed Key (CMK) monthly fees** ($1/CMK/month)
and **API call charges** ($0.03 per 10,000 calls, beyond a 20,000 free-tier per month).
AWS Managed Keys (created and owned by AWS services) are **free**.
The biggest savings opportunities are deleting unused CMKs, scheduling disabled CMKs
for deletion, and replacing custom CMKs with AWS managed keys where the service supports it.

---

## Data Collection

### AWS API Calls

```python
kms = session.client("kms", region_name=region)
cw  = session.client("cloudwatch", region_name=region)

# All keys in the account
kms.list_keys()                              # KeyId, KeyArn

# Per-key metadata
kms.describe_key(KeyId=key_id)              # KeyMetadata: KeyState, KeyManager,
                                             # Description, CreationDate, KeySpec

# All aliases (build alias → key mapping)
kms.list_aliases()                           # AliasName, TargetKeyId

# Key rotation status (symmetric CMKs only)
kms.get_key_rotation_status(KeyId=key_id)   # KeyRotationEnabled

# CloudWatch: API usage for a specific key (last 30 days)
cw.get_metric_statistics(
    Namespace="AWS/KMS",
    MetricName="KeyUsage",
    Dimensions=[{"Name": "KeyId", "Value": key_id}],
    StartTime=now - timedelta(days=30),
    EndTime=now,
    Period=2592000,   # 30-day single bucket
    Statistics=["Sum"],
)
# NOTE: KeyUsage metric may not exist for all keys — always use try/except.
```

### Cache TTL
- Key metadata + alias map + rotation status: **24 hours**
- CloudWatch API usage metrics: **6 hours** (separate cache or combined)

---

## Opportunity 1 — Unused CMK (No Alias, No Description, Low API Activity)

**Severity:** 🟠 HIGH
**Opportunity type:** `kms-unused-cmk`

**Detection logic:**
- `KeyManager = "CUSTOMER"` (skip AWS-managed keys).
- `KeyState = "Enabled"`.
- Key has **no aliases** (not present in alias map).
- Key has an **empty or blank Description**.
- CloudWatch `KeyUsage` shows **< 100 API calls** in the last 30 days
  (or metric does not exist — treat as 0 calls).

Each unused CMK costs **$1/month** with no operational value.

**Calculation:**
```
current_cost = 1.00     # $1/CMK/month
saving       = 1.00     # schedule for deletion → $0/month
```

**Recommendation:**
> "KMS CMK '{key_id}' has no aliases, no description, and fewer than 100 API calls in
> the last 30 days. This key appears unused and is costing $1/month. Schedule it for
> deletion (minimum 7-day waiting period) after confirming no application depends on it."

---

## Opportunity 2 — Disabled CMK Still Incurring Monthly Fee

**Severity:** 🟡 MEDIUM
**Opportunity type:** `kms-disabled-cmk`

**Detection logic:**
- `KeyManager = "CUSTOMER"`.
- `KeyState = "Disabled"`.
- Disabled keys **still incur the $1/month** charge.
- If the key is not required for future decryption of existing ciphertext, it should
  be scheduled for deletion.

**Calculation:**
```
current_cost = 1.00     # $1/CMK/month even when disabled
saving       = 1.00     # schedule for deletion → $0/month
```

**Recommendation:**
> "KMS CMK '{key_id}' is Disabled but still costs $1/month. If this key is no longer
> needed to decrypt existing data, schedule it for deletion (minimum 7-day waiting period).
> Note: deleted keys are unrecoverable — verify all dependent ciphertext has been
> re-encrypted or is no longer needed before proceeding."

---

## Opportunity 3 — Key Pending Deletion (No Action Required)

**Severity:** ℹ️  INFO
**Opportunity type:** `kms-pending-deletion`

**Detection logic:**
- `KeyManager = "CUSTOMER"`.
- `KeyState = "PendingDeletion"`.
- No cost action needed — the key will stop incurring the $1/month fee automatically
  once deletion completes. Surfaced for awareness.

**Recommendation:**
> "KMS CMK '{key_id}' is scheduled for deletion (PendingDeletion). Cost will cease once
> deletion completes. No action required — this is informational only."

---

## Opportunity 4 — Key Rotation Not Enabled

**Severity:** 🟢 LOW
**Opportunity type:** `kms-no-rotation`

**Detection logic:**
- `KeyManager = "CUSTOMER"`.
- `KeyState = "Enabled"`.
- `KeySpec = "SYMMETRIC_DEFAULT"` (asymmetric keys **cannot** rotate — skip them).
- `CreationDate` older than **90 days**.
- `get_key_rotation_status()` returns `KeyRotationEnabled = False`.

Not a direct cost issue, but a security best practice. Enabling rotation has **no
additional cost** and AWS performs the rotation automatically once per year.

**Recommendation:**
> "KMS CMK '{key_id}' is a symmetric key older than 90 days with rotation disabled.
> Enabling automatic key rotation (once per year) is a security best practice and has
> no additional cost. Enable via the AWS Console or: aws kms enable-key-rotation --key-id {key_id}"

---

## Opportunity 5 — Custom CMK for AWS-Native Service (AWS Managed Key Available)

**Severity:** 🟢 LOW
**Opportunity type:** `kms-aws-managed-candidate`

**Detection logic:**
- `KeyManager = "CUSTOMER"`.
- `KeyState = "Enabled"`.
- Key alias starts with `alias/aws/` — these are actually **AWS managed keys** and are
  free (informational only, skip as cost opportunities).
- Conversely, if a CMK's alias or description references a service that supports AWS
  managed keys (S3, EBS, RDS, Secrets Manager, etc.) and the key was created by the
  user, consider reverting to the service's AWS managed key.

> ⚠️  This opportunity requires manual judgement — the analyser flags candidates based
> on alias/description patterns referencing known AWS services.

**Calculation:**
```
current_cost = 1.00     # $1/CMK/month
saving       = 1.00     # revert to AWS managed key → free
```

**Recommendation:**
> "KMS CMK '{key_id}' (alias: {alias}) appears to be used for a service that supports
> AWS managed keys (e.g., S3, EBS, RDS). AWS managed keys are free. If you do not
> require independent key control, switching to the service's AWS managed key saves
> $1/month per key."

---

## Pricing

| Resource | Rate |
|---|---|
| Customer Managed Key (CMK) | **$1.00 / key / month** |
| API calls | **$0.03 / 10,000 calls** (first 20,000/month free) |
| AWS Managed Keys | **Free** |
| AWS Owned Keys | **Free** |

---

## Notes

- Only `KeyManager = "CUSTOMER"` keys are analysed. AWS-managed keys (`KeyManager = "AWS"`)
  are always free and not surfaced as opportunities.
- `list_aliases()` returns all aliases across the account; build a `{TargetKeyId: [aliases]}`
  dict before processing individual keys.
- `KeyUsage` CloudWatch metric may not be published for all keys (especially rarely-used
  ones) — treat missing metric data as 0 API calls.
- Asymmetric keys (`KeySpec != "SYMMETRIC_DEFAULT"`) cannot have automatic rotation enabled
  — skip the rotation check for those keys.
- `KeyState` values: `Enabled`, `Disabled`, `PendingDeletion`, `PendingImport`,
  `PendingReplicaDeletion`, `Unavailable`.
- Tag-based filtering: skip keys with tag `cost-analyser:ignore = true`.
- Total baseline CMK cost: `count(customer_enabled_keys) × $1/month`.
