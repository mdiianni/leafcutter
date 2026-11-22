# Skill: AWS QuickSight — Cost-Saving Opportunities

## Service Overview

AWS QuickSight is a cloud-native BI service. Costs are driven by three independent
dimensions:

1. **User seats** — Author/Admin vs Reader, per-month flat or pay-per-session
2. **SPICE capacity** — in-memory accelerated query engine, billed per GB/month
3. **Edition** — Standard vs Enterprise (different per-seat pricing + feature set)

---

## Pricing Tiers

### Standard Edition

| Role | Price/month |
|------|-------------|
| Author | $18 |
| Reader (flat) | $5 |
| Reader (pay-per-session, max $5/month) | $0.30/session |

- Max 5 free Authors included in base subscription
- Standard edition has no row-level security, no AD, no ML insights

### Enterprise Edition

| Role | Price/month |
|------|-------------|
| Author | $24 |
| Reader (flat) | $5 |
| Reader (pay-per-session, max $5/month) | $0.30/session |

Enterprise adds:
- Row-level security (RLS) on datasets
- Active Directory / SAML integration
- ML-powered anomaly detection and forecasting
- Private VPC connectivity
- Column-level security

### SPICE

| Item | Price |
|------|-------|
| SPICE capacity | $0.25 / GB / month |
| First 1 GB | Free (per account) |

SPICE is pre-provisioned capacity. You pay for allocated GB, not used GB. Unused
capacity should be released.

---

## Author vs Reader Licensing

### Author / Admin

- Can create and publish analyses, dashboards, and stories
- Full access to dataset connections, SPICE ingestion controls
- Billed at $18 (Standard) or $24 (Enterprise) per seat/month regardless of usage
- An Author who never logs in still consumes a full seat fee

### Reader (flat)

- Can only view published dashboards
- Billed at $5/seat/month regardless of how many sessions they have
- Best for users with **≥ 17 sessions/month** (break-even vs pay-per-session)

### Reader (pay-per-session)

- Same view-only access as flat Reader
- Billed at $0.30/session, capped at $5/month (automatically converts to flat if exceeded)
- Best for **infrequent viewers** with < 17 sessions/month
- Ideal for: occasional executives, external stakeholders, audit-only users

**Break-even formula:**
```
break_even_sessions = READER_COST_MO / READER_SESSION = $5 / $0.30 ≈ 17 sessions/month
```

If a user averages < 17 sessions/month → pay-per-session is cheaper or equal.

---

## SPICE Capacity Management

SPICE is in-memory data storage for fast query performance. Key facts:

- Capacity is **pre-provisioned** per account, not per dataset
- Billed for allocated capacity even if datasets don't use it all
- Each dataset ingestion consumes SPICE capacity proportional to row × column size
- Released capacity is available immediately for reuse

**Over-provisioning signal:** `spice_capacity_gb - spice_used_gb > 50%` of capacity.

**Release unused capacity:**
1. QuickSight console → Manage QuickSight → SPICE capacity
2. Click "Release" next to unused capacity
3. Or delete unused datasets to free SPICE before releasing

**API to check capacity** (no direct public API for used bytes per dataset):
```python
# describe_account_subscription returns CapacityPricingPlan in some editions
qs.describe_account_subscription(AwsAccountId=account_id)
# For dataset-level storage, use list_ingestions per dataset
qs.list_ingestions(AwsAccountId=account_id, DataSetId=dataset_id)
# Ingestion object has IngestionSizeInBytes
```

---

## Enterprise vs Standard Edition

Enterprise costs $6 more per Author per month. Only justifiable when you use:

| Feature | Enterprise | Standard |
|---------|-----------|---------|
| Row-level security | ✅ | ❌ |
| Column-level security | ✅ | ❌ |
| Active Directory / SAML | ✅ | ❌ |
| ML anomaly detection | ✅ | ❌ |
| ML forecasting | ✅ | ❌ |
| Private VPC connectivity | ✅ | ❌ |
| Paginated reports | ✅ | ❌ |
| API-based embedding | ✅ | ❌ |

**If none of these features are in use** → Standard edition is cheaper.

Switching from Enterprise to Standard requires deleting and recreating the QuickSight
account; all assets must be migrated. This is a destructive, high-risk operation.

---

## AWS API Reference

QuickSight control plane is **always `us-east-1`** regardless of where your data
sources or users are located.

```python
qs = session.client("quicksight", region_name="us-east-1")
```

### Key API Calls

```python
# Account subscription + edition
qs.describe_account_subscription(AwsAccountId=account_id)

# List all users (paginated)
qs.list_users(
    AwsAccountId=account_id,
    Namespace="default",
    MaxResults=100,
    NextToken=token,       # omit on first call
)
# Returns UserList[].Role: ADMIN | AUTHOR | READER | RESTRICTED_AUTHOR | RESTRICTED_READER
# Returns UserList[].LastLoginAt: datetime (not always populated)

# List groups (paginated)
qs.list_groups(
    AwsAccountId=account_id,
    Namespace="default",
    MaxResults=100,
    NextToken=token,
)

# SPICE capacity
qs.describe_account_subscription(AwsAccountId=account_id)
# → AccountInfo.CapacityPricingPlan.ActiveLicenseCapacity (GB)

# Dataset ingestion (for per-dataset SPICE usage)
qs.list_ingestions(AwsAccountId=account_id, DataSetId=dataset_id)
# → Ingestion.IngestionSizeInBytes
```

### Error Handling

```python
try:
    qs.describe_account_subscription(AwsAccountId=account_id)
except qs.exceptions.ResourceNotFoundException:
    # QuickSight is not set up in this account — exit cleanly
    pass
```

---

## Data Collection

### Derived Metrics

```python
quicksight_data = {
    "subscribed":         bool,
    "edition":            str,    # "STANDARD" | "ENTERPRISE" | "ENTERPRISE_AND_Q"
    "users":              list,   # full user objects with _inactive, _last_login_str annotations
    "groups":             list,   # group names
    "spice_capacity_gb":  float,  # provisioned SPICE capacity
    "spice_used_gb":      float,  # used SPICE (0.0 if not available via API)
}
```

### Inactivity Detection

```python
INACTIVE_DAYS = 30
cutoff = datetime.now(timezone.utc) - timedelta(days=INACTIVE_DAYS)

for user in users:
    last_login = user.get("LastLoginAt")  # datetime or None
    if last_login is None:
        user["_inactive"] = True          # never logged in
    else:
        user["_inactive"] = last_login < cutoff
```

### Cache TTL

- User list + subscription: **24 hours**
- SPICE capacity: **24 hours**
- No real-time API data needed for cost analysis

---

## Real-World Learnings

### ✅ QuickSight client must always use us-east-1

```python
# WRONG — will fail for non-us-east-1 accounts
qs = session.client("quicksight")   # uses AWS_DEFAULT_REGION

# CORRECT — QuickSight APIs are always routed via us-east-1
qs = session.client("quicksight", region_name="us-east-1")
```

### ✅ LastLoginAt may not be present for all users

`list_users` returns `LastLoginAt` only for users who have logged in at least once.
Users who were added via API but never logged in will not have this field. Treat
missing `LastLoginAt` as "never logged in" (inactive).

### ✅ SPICE used capacity is not directly available

There is no single API call to get total account-level SPICE used bytes. You must
iterate over all datasets and sum `list_ingestions()[].IngestionSizeInBytes` for
the most recent successful ingestion per dataset. This is expensive at scale; use
the `CapacityPricingPlan` field from `describe_account_subscription` as a proxy
for provisioned capacity.

### ✅ ResourceNotFoundException means QuickSight not subscribed

```python
try:
    qs.describe_account_subscription(AwsAccountId=account_id)
except qs.exceptions.ResourceNotFoundException:
    # Account is not subscribed to QuickSight — produce empty CSV and exit
```

### ✅ Reader session vs flat is a purchasing-model choice, not a per-user flag

Reader pricing mode is set at the account level, not per user. You cannot mix
pay-per-session and flat Reader pricing in the same account. The analysis should
recommend switching the account-level Reader billing model based on aggregate
session volume.

---

## Opportunity Reference

| Opportunity | Severity | Detection | Saving |
|-------------|----------|-----------|--------|
| Inactive Author/Admin seats (≥30d no login) | HIGH | `LastLoginAt < cutoff` | `(Author - Reader) × count` |
| SPICE over-capacity (> 50% unused) | MEDIUM | `used < capacity × 0.5` | `unused_gb × $0.25` |
| Reader flat → pay-per-session | LOW | `readers > 5` + low sessions | `(flat - session) × readers` |
| Enterprise → Standard review | MEDIUM | `edition == ENTERPRISE` | `$6 × author_count` (advisory) |

---

## Notes

- QuickSight has **no per-region cost** — all billing is account-level regardless
  of where datasets or data sources reside.
- The `analyse_quicksight.py` script always calls the QuickSight API via `us-east-1`
  but records the `AWS_DEFAULT_REGION` env var in output for consistency with other
  analysers.
- Tag-based exclusions: if an account-level tag `cost-analyser:ignore=quicksight`
  is set, the analyser should skip the account.
- QuickSight Q (generative BI) adds additional per-author charges on top of Enterprise;
  check for `ENTERPRISE_AND_Q` edition in `describe_account_subscription`.
