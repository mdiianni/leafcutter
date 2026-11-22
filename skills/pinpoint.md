# Skill: AWS End User Messaging / Pinpoint — Cost-Saving Opportunities

## Service Overview

AWS End User Messaging (formerly Amazon Pinpoint) covers SMS, push notifications,
email, and voice messaging. Costs vary significantly by channel:

- **SMS**: per-message pricing + optional phone number leasing ($1/month long code,
  ~$950/month short code) + 10-DLC/sender ID registration
- **Push notifications**: $1.00/million pushes (extremely low cost)
- **Email**: $0.10/1,000 emails (identical to SES pricing)
- **Pinpoint classic apps**: no fixed cost — usage-based

---

## Data Collection

```python
# SMS / Voice v2 API
sms = session.client("pinpoint-sms-voice-v2", region_name=region)
sms.describe_pools()            # SMS pools
sms.describe_phone_numbers()    # dedicated phone numbers + monthly leasing price
sms.describe_sender_ids()       # sender IDs + monthly leasing price
sms.describe_opt_out_lists()
sms.describe_spend_limits()     # configured monthly spend caps

# Classic Pinpoint apps
pp = session.client("pinpoint", region_name=region)
pp.get_apps()                   # paginate with Token
pp.get_app(ApplicationId=id)

# CloudWatch for delivery volumes (30d)
cw.get_metric_statistics(
    Namespace="AWS/Pinpoint",
    MetricName="DeliveryAttempts",
    Dimensions=[{"Name": "ApplicationId", "Value": app_id}],
    StartTime=now - timedelta(days=30), EndTime=now,
    Period=2592000, Statistics=["Sum"]
)
```

---

## Pricing (eu-central-1 / EU)

| Item | Price |
|------|-------|
| SMS to EU (per SMS) | ~$0.07424 |
| Push notification | $1.00 / 1M messages |
| Email | $0.10 / 1,000 emails |
| Dedicated long code (phone number) | ~$1.00/month |
| Dedicated short code | ~$950-1,000/month |
| 10-DLC brand registration | $4/brand/month |
| Sender ID (varies by country) | free–$10/month/country |

---

## Opportunities

### 1 — Test/Orphaned Applications (`pinpoint-test-apps`) HIGH
Detection: app name contains "test", "demo", "temp", "poc", AND deliveries = 0.
Test apps accumulate over time and add security surface (FCM/APNs credentials stored in app).
Saving: $0 (no fixed cost per app) but reduces credential exposure.

### 2 — Zero-Delivery Applications (`pinpoint-zero-delivery-apps`) MEDIUM
Detection: ALL apps showing 0 delivery attempts in 30 days.
In a dev account this may be expected, but flag for review. Apps with registered FCM/APNs
credentials that are unused still represent a security surface.

### 3 — "Production" Apps in Dev Account (`pinpoint-prd-apps-in-dev`) HIGH
Detection: app name contains "prd", "prod", "production" in a non-production account.
These apps may be misconfigured, using wrong FCM/APNs credentials, or routing
production traffic through a dev account.

### 4 — Leased Phone Numbers / Short Codes (`pinpoint-idle-phone-numbers`) HIGH
Detection: `describe_phone_numbers()` returns numbers with `MonthlyLeasingPrice > 0`
AND 0 messages sent in 30 days.
Leased numbers cost money even when idle. Delete unused numbers.
Saving: `count × monthly_leasing_price/month`.

### 5 — SMS Spend Limit Review (`pinpoint-sms-spend-limit`) INFO
Detection: `TEXT_MESSAGE_MONTHLY_SPEND_LIMIT` has been overridden (custom value set).
Review whether the limit matches expected volume. Too high = risk of runaway costs;
too low = messages silently dropped.

---

## Real-World Learnings

### Account: `<YOUR_ACCOUNT_ID>`, Region: `eu-central-1`, March 2026

**SMS (Pinpoint v2):**
- 0 phone numbers (no leasing cost ✅)
- 0 SMS pools
- 9 sender IDs (all VALIDATION type, $0 leasing — free sender IDs ✅)
  - Countries: PL, IT, GB, BG, BE + more
- SMS spend limit: $200/month (overridden/custom)
- 0 messages sent in 30 days

**Pinpoint Classic Apps (all 0 deliveries/30d):**
| App Name | Flag |
|----------|------|
| `sms_notifications` | ⚠️ 0 deliveries |
| `ios_<app>_push_notifications` | ⚠️ 0 deliveries |
| `ios_lexus_push_notifications` | ⚠️ 0 deliveries |
| `ios_subaru_push_notifications` | ⚠️ 0 deliveries |
| `ios_subaru_prd_push_notifications` | 🔴 "prd" in dev account + 0 deliveries |
| `ios_lexus_prd_push_notifications` | 🔴 "prd" in dev account + 0 deliveries |
| `ios_<app>_prd_push_notifications` | 🔴 "prd" in dev account + 0 deliveries |
| `test` | 🔴 test app + 0 deliveries |
| `test-new` | 🔴 test app + 0 deliveries |
| `android_lexus_notifications` | ⚠️ 0 deliveries |

- Estimated monthly cost: **~$0** (no leased numbers, no messages sent)
- All push notifications are within the free tier / extremely cheap

**API quirk:** `get_apps()` uses `Token` (not `NextToken`) for pagination.
Check `ApplicationsResponse["NextToken"]` to paginate beyond first page.
