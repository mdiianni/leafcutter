# Skill: AWS Secrets Manager — Cost-Saving Opportunities

## Service Overview

AWS Secrets Manager charges **$0.40/secret/month** (no free tier) plus $0.05 per
10,000 API calls. With no free tier, every secret costs money regardless of usage.
The primary opportunities are: removing orphaned secrets, migrating non-sensitive
config to SSM Parameter Store (free), and enforcing automatic rotation.

---

## Data Collection

```python
sm = session.client("secretsmanager", region_name=region)
paginator = sm.get_paginator("list_secrets")
for page in paginator.paginate():
    secrets.extend(page["SecretList"])
# Each secret: Name, ARN, RotationEnabled, RotationRules, LastAccessedDate,
#              LastChangedDate, DeletedDate, Tags, KmsKeyId
```

---

## Pricing (eu-central-1)

| Item | Price |
|------|-------|
| Per secret stored | **$0.40/secret/month** |
| API calls | $0.05 per 10,000 API calls |
| Replication (cross-region) | $0.40/replica/month |

**Compare with SSM:**
- SSM Standard SecureString: **FREE** (up to 10,000 params)
- SSM Advanced SecureString: $0.05/param/month
- Secrets Manager: always $0.40/secret/month

**Rule of thumb:** use SSM SecureString for config/non-sensitive values;
use Secrets Manager for true credentials that need automatic rotation.

---

## Opportunities

### 1 — No Rotation Enabled (`secrets-no-rotation`) HIGH
Detection: `RotationEnabled = False` or `None` on any secret.
Unrotated secrets are a security risk. Secrets Manager's killer feature is automatic rotation
via Lambda. The following secret types should be prioritised:
- Database credentials (RDS, MSK, Redis) → rotate every 30-90 days
- API keys (Datadog, 3rd party) → rotate every 90-180 days
- Application secrets → rotate every 180 days
Saving: $0 (security improvement, not cost)

### 2 — Orphaned / Unaccessed Secrets (`secrets-orphaned`) MEDIUM
Detection: `LastAccessedDate` > 90 days ago.
Secrets that are never read may belong to decommissioned services.
Saving: $0.40/month per deleted secret.

### 3 — Secrets Better Served by SSM (`secrets-use-ssm`) INFO
Detection: secret name/value pattern suggests non-rotating config (e.g. contains `/config`,
`config`, `settings`) that could live in SSM Parameter Store SecureString for free.
**Note:** Only applicable if the secret does not need rotation or cross-account replication.
Saving: $0.40/month per migrated secret.

### 4 — No Resource Policy (`secrets-no-resource-policy`) LOW
Detection: `get_resource_policy()` returns no policy for a secret.
Without a resource policy, any IAM principal in the account with
`secretsmanager:GetSecretValue` permission can read the secret.
For secrets used by specific services, add a resource policy restricting access to
the relevant IAM roles (e.g. ECS task execution role).

---

## Real-World Learnings

### Account: `<YOUR_ACCOUNT_ID>`, Region: `eu-central-1`, March 2026

- 7 secrets → **$2.80/month** ($33.60/year)
- **0/7 have rotation enabled** — 100% without rotation (security debt)
- Secrets present:
  | Secret | Type | Last Accessed | Rotation |
  |--------|------|--------------|---------|
  | `datadog_api_key...` | API key | 2026-03-23 | ❌ None |
  | `AmazonMSK_<project>-<env>` | MSK credentials | 2026-03-23 | ❌ None |
  | `digital_key_24mm_secrets` | App secret | 2026-03-23 | ❌ None |
  | `tme/24mm/dev/upvr-ccpa-svc` | App secret | 2026-03-23 | ❌ None |
  | `events!connection/one_ctp_eu_...` | VPC Lattice | **2025-10-16** (158d ago) | ❌ None |
  | `/connectedaudio/musicpref.../config` | Config | 2026-03-23 | ❌ None |
  | `/connectedaudio/tokenservice/config` | Config | 2026-03-23 | ❌ None |
- 1 orphaned secret (`events!connection/...`) — VPC Lattice connection, 158 days unaccessed
- The two `/connectedaudio/.../config` secrets may be better as SSM SecureString (free)

**API quirk:** `LastAccessedDate` is a date-only field (not datetime) — it returns a
`datetime.date` or `datetime` object. Handle both with `.replace(tzinfo=timezone.utc)` check.
