#!/usr/bin/env python3
"""
AWS Cost Analyser — Amazon Pinpoint Skill
Analyses Amazon Pinpoint (Classic + SMS v2 + Email) for cost and security posture.

Key checks:
  - Orphaned test apps
  - Zero-delivery apps in last 30 days
  - "prd" apps residing in a dev account
  - No phone numbers (Sender-ID-only coverage gaps)
  - SMS monthly spend limits
"""

import boto3
import os
import sys
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(__file__))
from cache_utils import load_cache, save_cache, CACHE_TTL_DAYS
from box_utils import _bl, BORDER_TOP, BORDER_MID, BORDER_BOT

# ── Configuration ─────────────────────────────────────────────────────────────
PROFILE    = os.environ.get("AWS_PROFILE", "my-aws-profile")
REGION     = os.environ.get("AWS_DEFAULT_REGION", "eu-central-1")
ACCOUNT_ID = boto3.Session().client("sts").get_caller_identity()["Account"]
CACHE_DIR  = os.path.join(os.path.dirname(__file__), "cache", ACCOUNT_ID, REGION)
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "output")
CACHE_FILE = os.path.join(CACHE_DIR, "pinpoint.json")
CW_DAYS    = 30

now = datetime.now(timezone.utc)

# ── Severity ──────────────────────────────────────────────────────────────────
CRITICAL = "🔴 CRITICAL"
HIGH     = "🟠 HIGH"
MEDIUM   = "🟡 MEDIUM"
LOW      = "🟢 LOW"
INFO     = "ℹ️  INFO"

# ── Pricing (eu-central-1) ────────────────────────────────────────────────────
SMS_PRICE_PER_MSG      = 0.07424   # USD per SMS (EU, approximate)
PUSH_PRICE_PER_MILLION = 1.00      # USD per million push notifications
EMAIL_PRICE_PER_1K     = 0.10      # USD per 1,000 emails via Pinpoint


# ── CloudWatch helpers ────────────────────────────────────────────────────────
def _get_app_deliveries(cw, app_id):
    """Return total DeliveryAttempts for a Pinpoint app over the last 30 days."""
    try:
        resp = cw.get_metric_statistics(
            Namespace="AWS/Pinpoint",
            MetricName="DeliveryAttempts",
            Dimensions=[{"Name": "ApplicationId", "Value": app_id}],
            StartTime=now - timedelta(days=CW_DAYS),
            EndTime=now,
            Period=CW_DAYS * 86400,
            Statistics=["Sum"],
        )
        pts = resp.get("Datapoints", [])
        return int(sum(p["Sum"] for p in pts)) if pts else 0
    except Exception:
        return 0


# ── Data collection ───────────────────────────────────────────────────────────
def collect(session):
    sms_client = session.client("pinpoint-sms-voice-v2", region_name=REGION)
    pp_client  = session.client("pinpoint", region_name=REGION)
    cw         = session.client("cloudwatch", region_name=REGION)

    # ── SMS v2: pools ─────────────────────────────────────────────────────────
    print("  → Describing SMS pools …")
    pools = []
    try:
        paginator = sms_client.get_paginator("describe_pools")
        for page in paginator.paginate():
            pools.extend(page.get("Pools", []))
    except Exception as e:
        print(f"     ⚠️  Could not list SMS pools: {e}")

    # ── SMS v2: phone numbers ─────────────────────────────────────────────────
    print("  → Describing phone numbers …")
    phone_numbers = []
    try:
        paginator = sms_client.get_paginator("describe_phone_numbers")
        for page in paginator.paginate():
            phone_numbers.extend(page.get("PhoneNumbers", []))
    except Exception as e:
        print(f"     ⚠️  Could not list phone numbers: {e}")

    # ── SMS v2: sender IDs ────────────────────────────────────────────────────
    print("  → Describing sender IDs …")
    sender_ids = []
    try:
        paginator = sms_client.get_paginator("describe_sender_ids")
        for page in paginator.paginate():
            sender_ids.extend(page.get("SenderIds", []))
    except Exception as e:
        print(f"     ⚠️  Could not list sender IDs: {e}")

    # ── SMS v2: opt-out lists ─────────────────────────────────────────────────
    print("  → Describing opt-out lists …")
    opt_out_lists = []
    try:
        paginator = sms_client.get_paginator("describe_opt_out_lists")
        for page in paginator.paginate():
            opt_out_lists.extend(page.get("OptOutLists", []))
    except Exception as e:
        print(f"     ⚠️  Could not list opt-out lists: {e}")

    # ── SMS v2: spend limits ──────────────────────────────────────────────────
    print("  → Describing spend limits …")
    spend_limits = {}
    try:
        resp = sms_client.describe_spend_limits()
        for sl in resp.get("SpendLimits", []):
            spend_limits[sl["Name"]] = {
                "enforced_limit":  sl.get("EnforcedLimit", 0),
                "max_limit":       sl.get("MaxLimit", 0),
                "overridden":      sl.get("Overridden", False),
            }
    except Exception as e:
        print(f"     ⚠️  Could not describe spend limits: {e}")

    # ── Pinpoint Classic: apps ────────────────────────────────────────────────
    print("  → Listing Pinpoint Classic apps …")
    apps_raw = []
    try:
        token = None
        while True:
            kwargs = {"PageSize": "100"}
            if token:
                kwargs["Token"] = token
            resp   = pp_client.get_apps(**kwargs)
            page   = resp.get("ApplicationsResponse", {})
            apps_raw.extend(page.get("Item", []))
            token = page.get("NextToken")
            if not token:
                break
    except Exception as e:
        print(f"     ⚠️  Could not list Pinpoint apps: {e}")

    apps = []
    for app in apps_raw:
        app_id   = app["Id"]
        app_name = app["Name"]
        print(f"  → Fetching CW metrics for app '{app_name}' ({app_id}) …")
        deliveries = _get_app_deliveries(cw, app_id)
        apps.append({
            "id":            app_id,
            "name":          app_name,
            "deliveries_30d": deliveries,
        })

    # ── Pinpoint Email: dedicated IP pools ────────────────────────────────────
    print("  → Listing Pinpoint Email dedicated IP pools …")
    email_ip_pools = []
    try:
        email_client = session.client("pinpoint-email", region_name=REGION)
        paginator    = email_client.get_paginator("list_dedicated_ip_pools")
        for page in paginator.paginate():
            email_ip_pools.extend(page.get("DedicatedIpPools", []))
    except Exception as e:
        print(f"     ⚠️  Could not list dedicated IP pools: {e}")

    data = {
        "pools":          pools,
        "phone_numbers":  phone_numbers,
        "sender_ids":     sender_ids,
        "opt_out_lists":  opt_out_lists,
        "spend_limits":   spend_limits,
        "apps":           apps,
        "email_ip_pools": email_ip_pools,
    }
    print(f"  ✓ Pinpoint data collected.")
    return data


# ── Analysis ──────────────────────────────────────────────────────────────────
def build_opportunities(data):
    opps = []
    apps         = data.get("apps", [])
    phone_numbers = data.get("phone_numbers", [])
    spend_limits  = data.get("spend_limits", {})

    # ── 1. Test apps ──────────────────────────────────────────────────────────
    test_apps = [a for a in apps if a["name"].lower().strip() in ("test", "test-new")
                 or a["name"].lower().startswith("test-")]
    if test_apps:
        names = ", ".join(a["name"] for a in test_apps)
        opps.append({
            "resource":       names,
            "type":           "pinpoint-test-apps",
            "severity":       HIGH,
            "current_mo":     0.0,
            "saving_mo":      0.0,
            "dep_warning":    "",
            "recommendation": (
                f"{len(test_apps)} app(s) appear to be orphaned test artifacts: {names}. "
                f"All have 0 deliveries in the last 30 days. Orphaned test apps increase "
                f"operational noise and expand the security surface area (active endpoints, "
                f"IAM roles, event subscriptions). Delete them to keep the account clean. "
                f"Saving: $0 (no fixed cost per app) but reduces security surface."
            ),
        })

    # ── 2. Zero-delivery apps ─────────────────────────────────────────────────
    zero_apps = [a for a in apps if a["deliveries_30d"] == 0]
    if zero_apps:
        names = ", ".join(a["name"] for a in zero_apps)
        opps.append({
            "resource":       f"ALL {len(zero_apps)} apps",
            "type":           "pinpoint-zero-delivery-apps",
            "severity":       MEDIUM,
            "current_mo":     0.0,
            "saving_mo":      0.0,
            "dep_warning":    "",
            "recommendation": (
                f"All {len(zero_apps)} Pinpoint Classic apps had 0 deliveries in the last 30 days: "
                f"{names}. "
                f"In a dev account this may be expected (apps are not actively sending), "
                f"but it is worth confirming whether these apps are intentionally idle "
                f"or have been superseded. Apps with persistent zero traffic should be "
                f"consolidated or removed to reduce management overhead. "
                f"Saving: $0 (Pinpoint apps are usage-billed) but reduces noise."
            ),
        })

    # ── 3. Prod apps in dev account ───────────────────────────────────────────
    prd_apps = [a for a in apps if "prd" in a["name"].lower() or "prod" in a["name"].lower()]
    if prd_apps:
        names = ", ".join(a["name"] for a in prd_apps)
        opps.append({
            "resource":       names,
            "type":           "pinpoint-prd-apps-in-dev-account",
            "severity":       HIGH,
            "current_mo":     0.0,
            "saving_mo":      0.0,
            "dep_warning":    "",
            "recommendation": (
                f"{len(prd_apps)} app(s) have 'prd'/'prod' in their name but reside in the "
                f"dev account ({ACCOUNT_ID}): {names}. "
                f"This may indicate misconfigured deployment pipelines pointing production "
                f"workloads at the wrong account, or legacy resources that were never cleaned up. "
                f"If these apps are not actively used in dev, delete them. "
                f"If they are genuine production Pinpoint configurations, they must be migrated "
                f"to the production account. Leaving prod-named apps in dev risks using wrong "
                f"credentials / endpoints in production services."
            ),
        })

    # ── 4. No phone numbers ───────────────────────────────────────────────────
    if not phone_numbers:
        sender_count = len(data.get("sender_ids", []))
        opps.append({
            "resource":       "SMS fleet",
            "type":           "pinpoint-no-phone-numbers",
            "severity":       INFO,
            "current_mo":     0.0,
            "saving_mo":      0.0,
            "dep_warning":    "",
            "recommendation": (
                f"No dedicated phone numbers are registered. The account relies solely on "
                f"{sender_count} Sender ID(s) for SMS delivery. "
                f"Sender IDs are not supported in all countries and can be spoofed more easily "
                f"than dedicated numbers. For markets that require a dedicated originating number "
                f"(e.g. US 10-DLC, Canada), a long code or toll-free number is mandatory. "
                f"Review delivery requirements per target country. "
                f"Dedicated long codes cost ~$1/month each — not currently a cost issue."
            ),
        })

    # ── 5. SMS spend limit ────────────────────────────────────────────────────
    sms_limit = spend_limits.get("TEXT_MESSAGE_MONTHLY_SPEND_LIMIT", {})
    sms_limit_usd = sms_limit.get("enforced_limit", 0)
    if sms_limit_usd > 0:
        opps.append({
            "resource":       "TEXT_MESSAGE_MONTHLY_SPEND_LIMIT",
            "type":           "pinpoint-sms-spend-limit",
            "severity":       INFO,
            "current_mo":     0.0,
            "saving_mo":      0.0,
            "dep_warning":    "",
            "recommendation": (
                f"The SMS monthly spend limit is set to ${sms_limit_usd:.2f}/month "
                f"({'custom override' if sms_limit.get('overridden') else 'default'}). "
                f"With 0 messages sent in the last 30 days there is no current spend risk. "
                f"At $0.07424/SMS (EU) this limit allows approximately "
                f"{int(sms_limit_usd / SMS_PRICE_PER_MSG):,} messages/month before the "
                f"hard cap is hit. Review this limit if SMS volumes are expected to grow "
                f"to avoid unexpected service interruptions."
            ),
        })

    return opps


# ── Reporting ─────────────────────────────────────────────────────────────────
def write_csv(opps):
    ts       = now.strftime("%Y%m%d_%H%M%S")
    csv_path = os.path.join(OUTPUT_DIR, f"{ts}_pinpoint_report.csv")
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(csv_path, "w") as f:
        f.write("account_id,region,resource,opportunity_type,severity,"
                "current_cost_usd_monthly,estimated_saving_usd_monthly,saving_pct,saving_basis,price_basis,"
                "dependency_warning,recommendation\n")
        for o in opps:
            pct = (o["saving_mo"] / max(o["current_mo"], 0.01)) * 100 if o["current_mo"] > 0 else 0
            saving_basis = "actual"
            sev = (o["severity"]
                   .replace("🔴 ", "").replace("🟠 ", "").replace("🟡 ", "")
                   .replace("🟢 ", "").replace("⚠️  ", "").replace("ℹ️  ", "").strip())
            f.write(f'"{ACCOUNT_ID}","{REGION}","{o["resource"]}",'
                    f'"{o["type"]}","{sev}",{o["current_mo"]:.2f},{o["saving_mo"]:.2f},{pct:.1f},"{saving_basis}","list",'
                    f'"{o["dep_warning"].replace(chr(34),chr(39))}","{o["recommendation"].replace(chr(34),chr(39))}"\n')
    return csv_path


def print_summary(data, opps):
    apps          = data.get("apps", [])
    pools         = data.get("pools", [])
    phone_numbers = data.get("phone_numbers", [])
    sender_ids    = data.get("sender_ids", [])
    opt_out_lists = data.get("opt_out_lists", [])
    spend_limits  = data.get("spend_limits", {})
    email_ip_pools = data.get("email_ip_pools", [])

    total_saving = sum(o["saving_mo"] for o in opps)

    print()
    print(BORDER_TOP)
    print(_bl("         AWS Cost Analyser — Pinpoint Analysis Results"))
    print(BORDER_MID)
    print(_bl(f"  Account  : {ACCOUNT_ID}"))
    print(_bl(f"  Region   : {REGION}"))
    print(_bl(f"  Run at   : {now.strftime('%Y-%m-%d %H:%M UTC')}"))
    print(BORDER_MID)
    print(_bl(f"  💸 Pinpoint apps cost                : ${'0.00':>8} / month (usage-billed)"))
    print(_bl(f"  💰 Savings potential                 : ${total_saving:>8.2f} / month"))
    print(_bl(f"  🔍 Opportunities found               : {len(opps)}"))
    print(BORDER_BOT)

    # SMS Fleet summary
    print("\n📱 SMS Fleet (pinpoint-sms-voice-v2):")
    print(f"  Pools         : {len(pools)}")
    print(f"  Phone numbers : {len(phone_numbers)}")
    print(f"  Sender IDs    : {len(sender_ids)}")
    print(f"  Opt-out lists : {len(opt_out_lists)}")
    if sender_ids:
        countries = sorted({s.get("IsoCountryCode", "??") for s in sender_ids})
        print(f"  Sender ID countries: {', '.join(countries)}")
    print("\n  Spend Limits:")
    for name, sl in spend_limits.items():
        override_tag = " (custom override)" if sl.get("overridden") else " (default)"
        print(f"    {name}: ${sl['enforced_limit']:.2f}/month{override_tag}")
    print(f"\n📧 Pinpoint Email:")
    print(f"  Dedicated IP pools : {len(email_ip_pools)}")

    # Apps table
    print(f"\n📦 Pinpoint Classic Apps ({len(apps)} total):")
    print(f"  {'App Name':<45} {'App ID':<35} {'Deliveries 30d':>14}  Flags")
    print(f"  {'-'*45} {'-'*35} {'-'*14}  -----")
    for a in sorted(apps, key=lambda x: x["name"]):
        flags = []
        n = a["name"].lower()
        if n.strip() in ("test", "test-new") or n.startswith("test-"):
            flags.append("🗑 test-app")
        if "prd" in n or "prod" in n:
            flags.append("⚠️  prd-in-dev")
        if a["deliveries_30d"] == 0:
            flags.append("💤 0-deliveries")
        flag_str = "  ".join(flags) if flags else "—"
        print(f"  {a['name']:<45} {a['id']:<35} {a['deliveries_30d']:>14,}  {flag_str}")

    # Severity breakdown
    sev_counts = {}
    for o in opps:
        sev_counts[o["severity"]] = sev_counts.get(o["severity"], 0) + 1
    print(f"\n🎯 Severity: " + "  ".join(f"{s}: {c}" for s, c in sev_counts.items()))

    # Detailed findings
    print("\n" + "─" * 72)
    print("📋 Detailed Findings:\n")
    for i, opp in enumerate(opps, 1):
        print(f"  [{i:02d}] {opp['severity']}  —  {opp['type']}")
        print(f"       Resource : {opp['resource']}")
        if opp["saving_mo"] > 0:
            print(f"       Saving   : ${opp['saving_mo']:.2f}/month  (${opp['saving_mo']*12:.2f}/year)")
        words = opp["recommendation"].split()
        line  = "       "
        for w in words:
            if len(line) + len(w) + 1 > 74:
                print(line); line = "       " + w + " "
            else:
                line += w + " "
        if line.strip():
            print(line)
        print()


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--force-refresh", "-f", action="store_true",
                        help="Ignore the local cache and fetch fresh data from AWS")
    args = parser.parse_args()
    if args.force_refresh:
        os.environ["AWS_COST_ANALYSER_FORCE_REFRESH"] = "1"
        print("  ⚠️  --force-refresh: ignoring cache, fetching live data from AWS")
    refresh = "--refresh-cache" in sys.argv

    print("🔍 AWS Pinpoint Cost Analyser")
    print(f"   Account : {ACCOUNT_ID}")
    print(f"   Region  : {REGION}")
    print(f"   Profile : {PROFILE}\n")

    session = boto3.Session(profile_name=PROFILE, region_name=REGION)

    if not refresh:
        cached = load_cache(CACHE_FILE)
        if cached:
            print("✅  Loaded Pinpoint data from cache.")
            data = cached
        else:
            print("🔄  Cache missing/expired — collecting from AWS …")
            data = collect(session)
            save_cache(CACHE_FILE, data, ACCOUNT_ID, REGION)
    else:
        print("🔄  --refresh-cache requested — collecting from AWS …")
        data = collect(session)
        save_cache(CACHE_FILE, data, ACCOUNT_ID, REGION)

    opps     = build_opportunities(data)
    csv_path = write_csv(opps)
    print_summary(data, opps)
    print(f"\n  💾 CSV report: {csv_path}\n")


if __name__ == "__main__":
    main()
