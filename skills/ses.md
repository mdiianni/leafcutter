# Skill: AWS SES (Simple Email Service v2) — Cost-Saving Opportunities

## Service Overview

Amazon SES costs accrue from **email sending volume**, **dedicated IP leasing**, and
optionally **Virtual Deliverability Manager**. For most accounts the dominant cost driver
is dedicated IPs ($24.95/IP/month) — shared IPs are free beyond the free tier.

---

## Data Collection

```python
ses = session.client("sesv2", region_name=region)

ses.get_account()               # SendingEnabled, ProductionAccessEnabled, SendQuota
ses.list_email_identities()     # all domain/email identities
ses.get_email_identity(EmailIdentity=name)   # DKIM, DMARC, suppression per identity
ses.list_configuration_sets()
ses.list_dedicated_ip_pools()
ses.list_contact_lists()
```

---

## Pricing (eu-central-1)

| Item | Price |
|------|-------|
| First 62,000 emails/month (from EC2) | FREE |
| Beyond free tier | $0.10 / 1,000 emails |
| Dedicated IP | **$24.95/IP/month** |
| Virtual Deliverability Manager | $0.00025/email monitored |
| Attachments | $0.12 / GB data transferred |

---

## Opportunities

### 1 — Unverified Email Identities (`ses-unverified-identity`) MEDIUM
Detection: `SendingEnabled = False` on an identity. Unverified identities cannot send email
and represent orphaned config. Example: `subaru.eu` identity exists but is not verified.

### 2 — Dedicated IP Under-utilised (`ses-dedicated-ip-underutilised`) HIGH
Detection: dedicated IP pools exist AND send volume < 50,000 emails/day average.
Dedicated IPs cost $24.95/IP/month. Only worth it above ~50K-100K emails/day for
reputation isolation. At lower volumes, shared IPs deliver the same or better deliverability.
Saving: $24.95/IP/month per removed dedicated IP.

### 3 — No Suppression List on Config Set (`ses-no-suppression-list`) LOW
Detection: configuration set exists with no suppression list enabled.
Without suppression, SES will attempt to send to previously bounced/complained addresses,
degrading sender reputation and risking account suspension.

### 4 — DMARC Not Enforced (`ses-no-dmarc-enforcement`) LOW
Detection: `get_email_identity` returns DMARC policy = "NONE" (not quarantine/reject).
A DMARC policy of "none" means spoofed emails from your domain are not rejected by
receiving servers, enabling phishing from your domain.

### 5 — Low Utilisation (`ses-low-utilisation`) INFO
Detection: sent last 24h < 5% of max24h send quota.
Informational — no direct saving but confirms headroom exists.

---

## Real-World API Quirks

- **`list_email_identities` is NOT pageable** via `get_paginator()` — raises `OperationNotPageableError`. Must use manual `NextToken` loop:
  ```python
  next_token = None
  while True:
      kwargs = {"NextToken": next_token} if next_token else {}
      resp = ses.list_email_identities(**kwargs)
      identities.extend(resp.get("EmailIdentities", []))
      next_token = resp.get("NextToken")
      if not next_token: break
  ```
- Same applies to `list_configuration_sets`, `list_dedicated_ip_pools`, `list_contact_lists` — all require manual NextToken loops.

---

## Real-World Learnings

### Account: `<YOUR_ACCOUNT_ID>`, Region: `eu-central-1`, March 2026

- Sending enabled, production access ✅
- Send quota: 65,000/day, sent last 24h: **96** (0.15% utilisation)
- Email identities:
  - `subaru.eu` — DOMAIN, **NOT verified** (SendingEnabled=False) → orphaned identity
  - `<your-domain>.dev` — DOMAIN, verified ✅
- 1 config set: `default_config_set`
- 0 dedicated IPs (shared only) → no fixed IP cost ✅
- 0 contact lists
- Estimated monthly cost: **~$0** (within free tier at 96 emails/day)
- Key action: verify or remove `subaru.eu` identity
