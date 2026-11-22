#!/usr/bin/env python3
"""
AWS Cost Analyser — SES Skill

Key cost drivers:
  - Email sending beyond free tier ($0.10/1,000 emails after first 62,000/month)
  - Dedicated IP pools ($24.95/IP/month)
  - Unverified / orphaned identities (no monetary cost but operational risk)
  - Weak DMARC posture (reputational / deliverability risk)
"""

import boto3
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(__file__))
from cache_utils import load_cache, save_cache, CACHE_TTL_DAYS
from box_utils import _bl, BORDER_TOP, BORDER_MID, BORDER_BOT

# ── Configuration ─────────────────────────────────────────────────────────────
PROFILE    = os.environ.get("AWS_PROFILE", "my-aws-profile")
REGION     = os.environ.get("AWS_DEFAULT_REGION", "eu-central-1")
ACCOUNT_ID = boto3.Session().client("sts").get_caller_identity()["Account"]
CACHE_DIR  = os.path.join(os.path.dirname(__file__), "cache", ACCOUNT_ID, REGION)
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "output")
CACHE_FILE = os.path.join(CACHE_DIR, "ses.json")

now = datetime.now(timezone.utc)

# ── Severity ──────────────────────────────────────────────────────────────────
CRITICAL = "🔴 CRITICAL"
HIGH     = "🟠 HIGH"
MEDIUM   = "🟡 MEDIUM"
LOW      = "🟢 LOW"
INFO     = "ℹ️  INFO"

# ── Pricing (eu-central-1) ────────────────────────────────────────────────────
PRICE = {
    "email_per_1k":         0.10,    # per 1,000 emails after free tier
    "free_tier_emails_mo":  62000,   # first 62,000 emails/month free (EC2-originated)
    "dedicated_ip_mo":      24.95,   # per dedicated IP per month
}


# ── Data collection ───────────────────────────────────────────────────────────
def collect(session):
    ses = session.client("sesv2", region_name=REGION)

    print("  → Fetching SES account info …")
    account = ses.get_account()
    send_quota = account.get("SendQuota", {})
    sending_enabled   = account.get("SendingEnabled", False)
    production_access = account.get("ProductionAccessEnabled", False)
    max_24h           = send_quota.get("Max24HourSend", 0)
    sent_24h          = send_quota.get("SentLast24Hours", 0)

    print("  → Listing email identities …")
    identities = []
    next_token = None
    while True:
        kwargs = {}
        if next_token:
            kwargs["NextToken"] = next_token
        resp = ses.list_email_identities(**kwargs)
        identities.extend(resp.get("EmailIdentities", []))
        next_token = resp.get("NextToken")
        if not next_token:
            break

    print(f"    → {len(identities)} identities found. Fetching details …")
    enriched_identities = []
    for ident in identities:
        name = ident["IdentityName"]
        try:
            detail = ses.get_email_identity(EmailIdentity=name)
            dkim_status    = detail.get("DkimAttributes", {}).get("Status", "NOT_STARTED")
            dkim_enabled   = detail.get("DkimAttributes", {}).get("SigningEnabled", False)
            dmarc_policy   = (detail.get("DkimAttributes", {}) or {}).get("DmarcPolicy", None)
            # DMARC enforcement lives under MailFromAttributes or policies
            # Try to find it in policies list
            policies = detail.get("Policies", {})
            dmarc_enforcement = None
            for policy_name, policy_doc in policies.items():
                if "dmarc" in policy_name.lower():
                    dmarc_enforcement = policy_doc
            enriched_identities.append({
                "name":              name,
                "type":              ident.get("IdentityType", "?"),
                "sending_enabled":   ident.get("SendingEnabled", False),
                "verified":          ident.get("SendingEnabled", False),  # verified = sending enabled
                "dkim_status":       dkim_status,
                "dkim_enabled":      dkim_enabled,
                "dmarc_policy":      dmarc_policy,
                "dmarc_enforcement": dmarc_enforcement,
            })
        except Exception as e:
            enriched_identities.append({
                "name":              name,
                "type":              ident.get("IdentityType", "?"),
                "sending_enabled":   ident.get("SendingEnabled", False),
                "verified":          ident.get("SendingEnabled", False),
                "dkim_status":       "UNKNOWN",
                "dkim_enabled":      False,
                "dmarc_policy":      None,
                "dmarc_enforcement": None,
            })

    print("  → Listing configuration sets …")
    config_sets = []
    next_token = None
    while True:
        kwargs = {}
        if next_token:
            kwargs["NextToken"] = next_token
        resp = ses.list_configuration_sets(**kwargs)
        config_sets.extend(resp.get("ConfigurationSets", []))
        next_token = resp.get("NextToken")
        if not next_token:
            break

    print("  → Listing dedicated IP pools …")
    dedicated_ip_pools = []
    try:
        next_token = None
        while True:
            kwargs = {}
            if next_token:
                kwargs["NextToken"] = next_token
            resp = ses.list_dedicated_ip_pools(**kwargs)
            dedicated_ip_pools.extend(resp.get("DedicatedIpPools", []))
            next_token = resp.get("NextToken")
            if not next_token:
                break
    except Exception:
        pass

    # Gather IPs per pool
    dedicated_ips = []
    for pool in dedicated_ip_pools:
        try:
            resp = ses.get_dedicated_ips(PoolName=pool)
            dedicated_ips.extend(resp.get("DedicatedIps", []))
        except Exception:
            pass

    print("  → Listing contact lists …")
    contact_lists = []
    try:
        next_token = None
        while True:
            kwargs = {}
            if next_token:
                kwargs["NextToken"] = next_token
            resp = ses.list_contact_lists(**kwargs)
            contact_lists.extend(resp.get("ContactLists", []))
            next_token = resp.get("NextToken")
            if not next_token:
                break
    except Exception:
        pass

    # Config set suppression check
    config_set_details = []
    for cs_name in config_sets:
        try:
            detail = ses.get_configuration_set(ConfigurationSetName=cs_name)
            suppression = detail.get("SuppressionOptions", {})
            config_set_details.append({
                "name":                cs_name,
                "suppression_enabled": bool(suppression.get("SuppressedReasons")),
                "suppressed_reasons":  suppression.get("SuppressedReasons", []),
            })
        except Exception:
            config_set_details.append({
                "name":                cs_name,
                "suppression_enabled": False,
                "suppressed_reasons":  [],
            })

    data = {
        "sending_enabled":       sending_enabled,
        "production_access":     production_access,
        "max_24h_send":          max_24h,
        "sent_last_24h":         sent_24h,
        "identities":            enriched_identities,
        "config_sets":           config_set_details,
        "dedicated_ip_pools":    dedicated_ip_pools,
        "dedicated_ips":         dedicated_ips,
        "contact_lists":         contact_lists,
    }
    return data


# ── Analysis ──────────────────────────────────────────────────────────────────
def build_opportunities(data):
    opps = []

    identities      = data["identities"]
    config_sets     = data["config_sets"]
    dedicated_ips   = data["dedicated_ips"]
    sent_24h        = data.get("sent_last_24h", 0)
    max_24h         = data.get("max_24h_send", 1)

    # Opp 1: Unverified identities
    unverified = [i for i in identities if not i["sending_enabled"]]
    for ident in unverified:
        opps.append({
            "resource":       ident["name"],
            "type":           "ses-unverified-identity",
            "severity":       MEDIUM,
            "current_mo":     0.0,
            "saving_mo":      0.0,
            "dep_warning":    "",
            "recommendation": (
                f"Identity '{ident['name']}' (type={ident['type']}) has SendingEnabled=False — "
                f"it is not verified and cannot send email. Unverified identities cost nothing "
                f"but create confusion and may be orphaned resources from decommissioned services. "
                f"Verify the identity if it is still needed, or delete it to reduce clutter."
            ),
        })

    # Opp 2: Config sets with no suppression list
    for cs in config_sets:
        if not cs["suppression_enabled"]:
            opps.append({
                "resource":       cs["name"],
                "type":           "ses-no-configuration-set-suppression",
                "severity":       LOW,
                "current_mo":     0.0,
                "saving_mo":      0.0,
                "dep_warning":    "",
                "recommendation": (
                    f"Configuration set '{cs['name']}' does not have a suppression list enabled. "
                    f"Enabling suppression for BOUNCE and COMPLAINT reasons automatically prevents "
                    f"re-sending to addresses that previously bounced or complained, protecting "
                    f"your sender reputation. High bounce/complaint rates can result in SES "
                    f"suspending your sending privileges."
                ),
            })

    # Opp 3: Weak DMARC enforcement per identity
    for ident in identities:
        if not ident["sending_enabled"]:
            continue  # skip unverified — already flagged
        dmarc = ident.get("dmarc_policy")
        # If we could not determine DMARC policy or it is "NONE", flag it
        if dmarc is None or str(dmarc).upper() in ("NONE", ""):
            opps.append({
                "resource":       ident["name"],
                "type":           "ses-no-dmarc-enforcement",
                "severity":       LOW,
                "current_mo":     0.0,
                "saving_mo":      0.0,
                "dep_warning":    "",
                "recommendation": (
                    f"Identity '{ident['name']}' does not have a DMARC enforcement policy of "
                    f"'quarantine' or 'reject'. A DMARC policy of 'none' means that spoofed "
                    f"emails from your domain are delivered without any action by receiving mail "
                    f"servers. Set DMARC to 'quarantine' (minimum) or 'reject' to protect your "
                    f"domain from email spoofing and phishing."
                ),
            })

    # Opp 4: Dedicated IPs with low send volume
    if dedicated_ips:
        ip_count     = len(dedicated_ips)
        ip_cost_mo   = ip_count * PRICE["dedicated_ip_mo"]
        # Dedicated IPs cost-effective when volume > ~50,000/day consistently
        if sent_24h < 50000:
            opps.append({
                "resource":       f"{ip_count} dedicated IP(s)",
                "type":           "ses-dedicated-ip-underutilised",
                "severity":       HIGH,
                "current_mo":     round(ip_cost_mo, 2),
                "saving_mo":      round(ip_cost_mo, 2),
                "dep_warning":    (
                    "Releasing dedicated IPs means reverting to shared IPs. Shared IPs have "
                    "well-managed reputation by AWS but are shared across many senders."
                ),
                "recommendation": (
                    f"{ip_count} dedicated IP(s) at ${ip_cost_mo:.2f}/month "
                    f"(${PRICE['dedicated_ip_mo']:.2f}/IP/month). "
                    f"Current send volume is only {int(sent_24h):,} emails/day. "
                    f"Dedicated IPs are cost-effective at volumes above ~50,000 emails/day. "
                    f"At current volume, shared IPs would provide the same deliverability "
                    f"for free, saving ${ip_cost_mo:.2f}/month."
                ),
            })

    # Opp 5: Low utilisation info
    utilisation_pct = (sent_24h / max(max_24h, 1)) * 100
    if utilisation_pct < 5.0 and max_24h > 0:
        opps.append({
            "resource":       "SES sending quota",
            "type":           "ses-low-utilisation",
            "severity":       INFO,
            "current_mo":     0.0,
            "saving_mo":      0.0,
            "dep_warning":    "",
            "recommendation": (
                f"SES sending utilisation is {utilisation_pct:.2f}% "
                f"({int(sent_24h):,} of {int(max_24h):,} emails sent in the last 24h). "
                f"Headroom: {int(max_24h - sent_24h):,} emails/day remaining. "
                f"No cost action required — this is informational. "
                f"If sending volume is expected to grow significantly, ensure dedicated IPs "
                f"are evaluated at that point."
            ),
        })

    return opps


# ── Reporting ─────────────────────────────────────────────────────────────────
def print_report(data, opps):
    identities      = data["identities"]
    config_sets     = data["config_sets"]
    dedicated_ips   = data["dedicated_ips"]
    dedicated_pools = data["dedicated_ip_pools"]
    contact_lists   = data["contact_lists"]
    sent_24h        = data.get("sent_last_24h", 0)
    max_24h         = data.get("max_24h_send", 1)
    utilisation_pct = (sent_24h / max(max_24h, 1)) * 100

    ip_cost_mo    = len(dedicated_ips) * PRICE["dedicated_ip_mo"]
    total_cost_mo = ip_cost_mo   # email sending cost is usage-based, hard to project monthly
    total_saving  = sum(o["saving_mo"] for o in opps)

    print()
    print(BORDER_TOP)
    print(_bl("            AWS Cost Analyser — SES Analysis Results"))
    print(BORDER_MID)
    print(_bl(f"  Account  : {ACCOUNT_ID}"))
    print(_bl(f"  Region   : {REGION}"))
    print(_bl(f"  Run at   : {now.strftime('%Y-%m-%d %H:%M UTC')}"))
    print(BORDER_MID)
    sending_str = "✅ Enabled" if data["sending_enabled"] else "❌ Disabled"
    prod_str    = "✅ Production" if data["production_access"] else "⚠️  Sandbox"
    print(_bl(f"  Sending  : {sending_str}"))
    print(_bl(f"  Access   : {prod_str}"))
    quota_str   = f"{int(sent_24h):,} / {int(max_24h):,} ({utilisation_pct:.2f}%)"
    print(_bl(f"  Quota 24h: {quota_str}"))
    print(_bl(f"  Identities       : {len(identities):<3}  ({sum(1 for i in identities if i['sending_enabled'])} verified)"))
    print(_bl(f"  Config sets      : {len(config_sets)}"))
    print(_bl(f"  Dedicated IP pools: {len(dedicated_pools):<2}  ({len(dedicated_ips)} IPs)"))
    print(_bl(f"  Contact lists    : {len(contact_lists)}"))
    print(BORDER_MID)
    print(_bl(f"  💸 Dedicated IP cost         : ${ip_cost_mo:>8.2f} / month"))
    print(_bl(f"  💸 Total estimated cost      : ${total_cost_mo:>8.2f} / month"))
    print(_bl(f"  💰 Potential savings         : ${total_saving:>8.2f} / month"))
    print(_bl(f"  🔍 Opportunities found       : {len(opps)}"))
    print(BORDER_BOT)

    # Identities table
    print("\n📧 Email Identities:")
    print(f"  {'Name':<45} {'Type':<8} {'Verified':<10} {'DKIM':<12} {'DKIM Enabled'}")
    print(f"  {'-'*45} {'-'*8} {'-'*10} {'-'*12} {'-'*12}")
    for ident in identities:
        verified_str = "✅ Yes" if ident["sending_enabled"] else "❌ No"
        dkim_str     = ident.get("dkim_status", "?")
        dkim_en_str  = "✅" if ident.get("dkim_enabled") else "❌"
        print(f"  {ident['name'][:45]:<45} {ident['type']:<8} {verified_str:<10} {dkim_str:<12} {dkim_en_str}")

    # Dedicated IPs
    if dedicated_ips:
        print(f"\n🌐 Dedicated IPs ({len(dedicated_ips)} × ${PRICE['dedicated_ip_mo']:.2f}/month = ${ip_cost_mo:.2f}/month):")
        for ip in dedicated_ips:
            print(f"  {ip.get('Ip','?'):<18}  pool={ip.get('PoolName','?')}  warmup={ip.get('WarmupStatus','?')}")
    else:
        print("\n🌐 Dedicated IPs: none (using shared IPs — no fixed monthly cost)")

    # Config sets
    if config_sets:
        print("\n⚙️  Configuration Sets:")
        for cs in config_sets:
            sup_str = "✅ Enabled" if cs["suppression_enabled"] else "❌ Disabled"
            print(f"  {cs['name']:<40}  suppression={sup_str}")

    # Opportunities
    if opps:
        sev_counts = {}
        for o in opps:
            sev_counts[o["severity"]] = sev_counts.get(o["severity"], 0) + 1
        print(f"\n🎯 Severity: " + "  ".join(f"{s}: {c}" for s, c in sev_counts.items()))

        sorted_opps = sorted(opps, key=lambda x: -x["saving_mo"])
        print("\n" + "─" * 72)
        print("📋 Detailed Findings:\n")
        for i, opp in enumerate(sorted_opps, 1):
            print(f"  [{i:02d}] {opp['severity']}  —  {opp['type']}")
            print(f"       Resource : {opp['resource']}")
            if opp["saving_mo"] > 0:
                print(f"       Saving   : ${opp['saving_mo']:.2f}/month  (${opp['saving_mo']*12:.2f}/year)")
                print(f"       Current  : ${opp['current_mo']:.2f}/month")
            if opp.get("dep_warning"):
                print(f"       ⚠️  Dep   : {opp['dep_warning'][:68]}")
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
    else:
        print("\n✅  No opportunities found.")


def write_csv(opps):
    ts       = now.strftime("%Y%m%d_%H%M%S")
    csv_path = os.path.join(OUTPUT_DIR, f"{ts}_ses_report.csv")
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(csv_path, "w") as f:
        f.write("account_id,region,resource,opportunity_type,severity,"
                "current_cost_usd_monthly,estimated_saving_usd_monthly,saving_pct,saving_basis,price_basis,"
                "dependency_warning,recommendation\n")
        sorted_opps = sorted(opps, key=lambda x: -x["saving_mo"])
        for o in sorted_opps:
            pct = (o["saving_mo"] / max(o["current_mo"], 0.01)) * 100 if o["current_mo"] > 0 else 0
            saving_basis = "actual"
            sev = (o["severity"]
                   .replace("🔴 ", "").replace("🟠 ", "").replace("🟡 ", "")
                   .replace("🟢 ", "").replace("ℹ️  ", "").strip())
            dep = o.get("dep_warning", "").replace('"', "'")
            rec = o["recommendation"].replace('"', "'")
            f.write(f'"{ACCOUNT_ID}","{REGION}","{o["resource"]}",'
                    f'"{o["type"]}","{sev}",{o["current_mo"]:.2f},{o["saving_mo"]:.2f},{pct:.1f},"{saving_basis}","list",'
                    f'"{dep}","{rec}"\n')
    return csv_path


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

    print("🔍 AWS SES Cost Analyser")
    print(f"   Account : {ACCOUNT_ID}")
    print(f"   Region  : {REGION}")
    print(f"   Profile : {PROFILE}\n")

    session = boto3.Session(profile_name=PROFILE, region_name=REGION)

    if not refresh:
        cached = load_cache(CACHE_FILE)
        if cached:
            print("✅  Loaded SES data from cache.")
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
    print_report(data, opps)
    print(f"\n  💾 CSV report: {csv_path}\n")


if __name__ == "__main__":
    main()
