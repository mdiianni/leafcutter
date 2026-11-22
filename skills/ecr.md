# Skill: Amazon ECR — Cost-Saving Opportunities

---

## 🧪 Real-World Learnings (validated 2026-03-23, eu-central-1, account `<YOUR_ACCOUNT_ID>`)

These corrections come from running `analyse_ecr.py` against a live AWS account.

### 1. Lifecycle Policies Are Absent by Default — Even at Scale
**Observed:** 411/411 repositories (100%) had NO lifecycle policy. This is the single most
impactful finding in ECR. Without governance, images accumulate indefinitely as CI/CD pipelines
push new builds. Every repository in this account had been running without a cleanup policy since
creation.

### 2. `describe_images` Requires Pagination — Default Returns Only 100
**Critical:** `ecr.describe_images(repositoryName=name)` returns only 100 images without pagination.
Must use `ecr.get_paginator("describe_images")`. For performance, cap at 1000 per repo (still
enumerates enough for sizing). The analyser uses `image_count_capped=True` to flag repos where
actual count is likely higher than 1000.

### 3. Replication Rules With No Filters Multiply Costs Across Accounts
**Observed:** This account had 2 replication rules (to accounts `<ACCOUNT_ID_B>` and `<ACCOUNT_ID_C>`)
with **no repository filters** — meaning ALL 411 repositories and ALL 8,650 GB are replicated.
The $865/month storage in this account becomes ~$2,595/month across the org.
**This must be flagged as HIGH severity** with estimated multiplied cost in destination accounts.
Add repository filters to replicate only repos that truly need cross-account access.

### 4. Pull-Through Cache Is Benign — Skip It
**Observed:** 1 pull-through cache rule caching `public.ecr.aws` under the `ecr-public/` prefix.
This is a standard best-practice setup (avoids Docker Hub rate limits). It adds some storage cost
but is actively useful. Do not flag pull-through cache rules as an opportunity.

### 5. Savings Estimation Formula (validated)
The correct formula for "lifecycle policy saving" per repository:
```python
tagged_count    = image_count - untagged_count
saving_untagged = untagged_gb                    # all untagged can go
if tagged_count > 10:
    tagged_gb       = total_gb - untagged_gb
    saving_tagged   = tagged_gb * (tagged_count - 10) / tagged_count   # pro-rate by count
saving_lifecycle_gb = saving_untagged + saving_tagged
saving_lifecycle_mo = saving_lifecycle_gb * 0.10  # ECR storage price $/GB/month
```
This yielded $808/month estimated saving in a $865/month account — 93% of total cost recoverable.

### 6. Stale Repositories Are Very Common
**Observed:** 265/411 repos (64%) had no new image push in > 90 days. Many belong to deprecated
or seasonal services. Flag stale repos as MEDIUM with deletion recommendation.

### Validated ECR Pricing (eu-central-1, 2026-03):
| Item | Price | Notes |
|---|---|---|
| Storage | $0.10/GB/month | First 500 MB/month free per account |
| Data transfer in | Free | |
| API calls | Free | `describe_images`, `list_repositories`, etc. |

### Validated Analysis Results (eu-central-1, 2026-03-23):
- **411 repositories**, 0 lifecycle policies, **75,576+ images**, **8,650 GB**
- **$865/month** this account; **~$2,595/month** estimated org-wide (replication)
- **Lifecycle policy saving**: $808/month ($9,697/year) — applying policy to all 411 repos
- **Stale repos (>90d)**: 265 repos, $213/month in storage (consider deletion)
- **Untagged images**: 1,336 GB = $134/month immediately removable
- **Empty repos**: 20 (no cost, but cleanup noise)
- **Scan-on-push**: disabled on all 411 repos (enable for free via registry-level config)

---

## Service Overview

Amazon ECR (Elastic Container Registry) charges for **storage** of Docker/OCI images and for
**data transfer** out. Costs grow quickly when images are large, pushed frequently, and old
images are never removed. The primary levers are **lifecycle policies** and **image deduplication**.

---

## Data Collection

### AWS API Calls

```python
ecr = session.client("ecr", region_name=region)

# List all private repositories
ecr.describe_repositories()   # paginate with nextToken

# For each repository:
# 1. Describe images (up to 1000 per page; use pagination)
ecr.describe_images(repositoryName=repo_name)

# 2. Get lifecycle policy (if any)
try:
    ecr.get_lifecycle_policy(repositoryName=repo_name)
except ecr.exceptions.LifecyclePolicyNotFoundException:
    lifecycle = None

# 3. Get repository tags
ecr.list_tags_for_resource(resourceArn=repo_arn)

# ECR Public registries (if any)
ecr_public = session.client("ecr-public", region_name="us-east-1")
ecr_public.describe_registries()
ecr_public.describe_repositories()
```

> ℹ️ ECR `describe_images` is safe to call on all repositories as it returns image metadata
> (digest, tags, pushed date, size in bytes) — **not** image contents. This is low-cost and
> fast. ECR does not charge for API calls.

### Derived metrics

```python
# Per repository:
image_count         = len(images)
total_size_gb       = sum(img["imageSizeInBytes"] for img in images) / 1e9
untagged_images     = [img for img in images if not img.get("imageTags")]
oldest_image_days   = (now - min(img["imagePushedAt"] for img in images)).days
newest_image_days   = (now - max(img["imagePushedAt"] for img in images)).days
images_older_90d    = [img for img in images if (now - img["imagePushedAt"]).days > 90]
```

### Cache TTL
- Repository metadata + image list: **24 hours**
- Lifecycle policy: **24 hours**

---

## Opportunity 1 — No Lifecycle Policy

**Severity:** 🔴 CRITICAL  
**Opportunity type:** `ecr-missing-lifecycle-policy`

**Detection logic:**
- `get_lifecycle_policy` raises `LifecyclePolicyNotFoundException`.
- Repository has more than 5 images.
- Without a lifecycle policy, images accumulate indefinitely.

**Calculation:**
```
storage_gb       = total_size_gb
price_per_gb_mo  = pricing["ecr"]["storage_per_gb_month"]   # ~$0.10/GB in most regions
current_cost     = storage_gb * price_per_gb_mo

# If we assume a lifecycle policy keeps only the last 10 images
images_sorted    = sorted by imagePushedAt descending
images_to_delete = images[10:]   # everything beyond keep count
size_to_delete   = sum(img["imageSizeInBytes"] for img in images_to_delete) / 1e9
monthly_saving   = size_to_delete * price_per_gb_mo
```

**Recommendation:**
> "Repository '{repo}' has {image_count} images ({size_gb:.1f} GB) and no lifecycle policy.
> Adding a lifecycle policy to retain only the last 10 tagged images and expire all untagged
> images could save ~${saving}/month by removing {size_to_delete:.1f} GB of stale images."

**Example lifecycle policy JSON to recommend:**
```json
{
  "rules": [
    {
      "rulePriority": 1,
      "description": "Expire untagged images older than 7 days",
      "selection": {
        "tagStatus": "untagged",
        "countType": "sinceImagePushed",
        "countUnit": "days",
        "countNumber": 7
      },
      "action": { "type": "expire" }
    },
    {
      "rulePriority": 2,
      "description": "Keep only last 10 tagged images",
      "selection": {
        "tagStatus": "tagged",
        "tagPrefixList": ["v", "release", "latest"],
        "countType": "imageCountMoreThan",
        "countNumber": 10
      },
      "action": { "type": "expire" }
    }
  ]
}
```

---

## Opportunity 2 — Large Volume of Untagged Images

**Severity:** 🟠 HIGH  
**Opportunity type:** `ecr-untagged-images`

**Detection logic:**
- `untagged_images` count > 5 OR untagged images represent > 20% of total storage.
- Untagged images are typically build artefacts / intermediate layers with no deployment value.

**Calculation:**
```
untagged_size_gb = sum(img["imageSizeInBytes"] for img in untagged_images) / 1e9
monthly_saving   = untagged_size_gb * price_per_gb_mo
```

**Recommendation:**
> "Repository '{repo}' has {untagged_count} untagged images consuming {untagged_gb:.1f} GB
> (~${saving}/month). Add a lifecycle rule to expire untagged images older than 7 days."

---

## Opportunity 3 — Images Older Than 90 Days With No Recent Pushes

**Severity:** 🟡 MEDIUM  
**Opportunity type:** `ecr-stale-repository`

**Detection logic:**
- Most recent image push was > 90 days ago.
- Repository likely belongs to a deprecated service or abandoned project.

**Recommendation:**
> "Repository '{repo}' has not received a new image in {days_since_push} days. Its {image_count}
> images consume {size_gb:.1f} GB (~${cost}/month). If this service is retired, consider
> archiving or deleting the repository."

---

## Opportunity 4 — Duplicate Image Layers Across Repositories

**Severity:** 🟢 LOW  
**Opportunity type:** `ecr-consolidation`

**Detection logic:**
- If the account has > 20 repositories all storing variants of the same base image family
  (infer from image tag naming conventions), note that ECR deduplicates layers **within**
  a repository but **not across** repositories.
- Consolidating related images under a single repository with distinct tag prefixes enables
  layer deduplication and reduces storage.

**Recommendation:**
> "{repo_count} ECR repositories appear to contain images from the same application family.
> Consolidating into fewer repositories using tag-based separation enables cross-image layer
> deduplication, potentially reducing storage costs."

---

## Opportunity 5 — Image Scanning Not Enabled (Security + Cost Awareness)

**Severity:** 🟢 LOW  
**Opportunity type:** `ecr-no-image-scanning`

**Detection logic:**
- `describe_repositories` → `imageScanningConfiguration.scanOnPush = false`.
- While not a direct cost item, unscanned images lead to security debt that requires
  emergency remediation (operational cost). Flag as informational.

**Recommendation:**
> "Repository '{repo}' does not have scan-on-push enabled. Enabling automated image scanning
> is free for Basic scanning and helps prevent costly security incidents."

---

## Opportunity 6 — ECR Public Repositories Unnecessarily Used for Private Images

**Severity:** 🟡 MEDIUM  
**Opportunity type:** `ecr-public-private-images`

**Detection logic:**
- ECR Public repositories found in the account.
- ECR Public is free for public pull but should not host proprietary images.
- If an ECR Public repository appears to contain private/internal image names (not public
  open-source), flag it.

**Recommendation:**
> "ECR Public repository '{repo}' may contain private images. ECR Public repositories are
> accessible by anyone. Move proprietary images to private ECR repositories with VPC endpoints
> to avoid accidental exposure and unnecessary data egress costs."

---

## Pricing Sources

| Item | Price (approximate) | Notes |
|---|---|---|
| Storage | ~$0.10/GB/month | First 500 MB/month free |
| Data transfer out | $0.09/GB (first 10 TB) | Within same region: free |
| Data transfer (cross-region) | Varies | High cost driver for large images |

Fetch exact pricing: `pricing.get_products(ServiceCode="AmazonECR", ...)`

---

## Notes

- ECR is a regional service. Only query the target region.
- `describe_images` requires **pagination** — default returns only 100 images per page. Cap at 1000 per repo for performance; set `image_count_capped=True` in the data model to note the truncation.
- **Pull-through cache rules** are a standard best practice — do not flag them as an issue.
- For multi-account organisations, check registry-level replication rules (`describe_registry`). If rules have **no repository filters**, all images are replicated to destination accounts — flag as HIGH with multiplied cost estimate. Adding repository filters is the recommended fix.
- Tag-based filtering: skip repositories with tag `cost-analyser:ignore = true`.
- The ECR storage free tier (500 MB/month per account) is negligible at scale; ignore it in calculations.

