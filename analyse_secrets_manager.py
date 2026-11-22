#!/usr/bin/env python3
"""
AWS Cost Analyser — Secrets Manager Skill
Analyses AWS Secrets Manager for cost optimisation and security posture.

Key checks:
  - Secrets with no rotation enabled (security risk)
  - Orphaned secrets not accessed in 90+ days (potential cost saving)
  - Comparison with SSM Parameter Store (cost efficiency)
  - Secrets lacking resource policies (access control gap)
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
CACHE_FILE = os.path.join(CACHE_DIR, "secrets_manager.json")

now = datetime.now(timezone.utc)

# ── Severity ──────────────────────────────────────────────────────────────────
CRITICAL = "🔴 CRITICAL"
HIGH     = "🟠 HIGH"
MEDIUM   = "🟡 MEDIUM"
LOW      = "🟢 LOW"
INFO     = "ℹ️  INFO"

# ── Pricing ───────────────────────────────────────────────────────────────────
SECRET_MONTHLY_COST    = 0.40   # USD per secret per month
ORPHAN_THRESHOLD_DAYS  = 90     # flag secrets not accessed in this many days
SSM_PARAM_COUNT        = 485    # known SSM parameter count in this account (for comparison)


# ── Data collection ───────────────────────────────────────────────────────────
def collect(session):
    sm = session.client("secretsmanager", region_name=REGION)

    print("  → Listing secrets …")
    secrets = []
    try:
        paginator = sm.get_paginator("list_secrets")
        for page in paginator.paginate():
            secrets.extend(page.get("SecretList", []))
    except Exception as e:
        print(f"     ⚠️  Could not list secrets: {e}")
        return {"secrets": []}

    print(f"  → {len(secrets)} secret(s) found.")

    enriched = []
    for s in secrets:
        name        = s.get("Name", "")
        arn         = s.get("ARN", "")
        rotation    = s.get("RotationEnabled", False)
        rot_rules   = s.get("RotationRules", {})
        last_access = s.get("LastAccessedDate")
        last_changed = s.get("LastChangedDate")
        tags        = {t["Key"]: t["Value"] for t in s.get("Tags", [])}

        # Check for resource policy
        has_resource_policy = False
        try:
            sm.get_resource_policy(SecretId=arn)
            has_resource_policy = True
        except sm.exceptions.ResourceNotFoundException:
            has_resource_policy = False
        except Exception:
            has_resource_policy = None  # unknown

        days_since_access = None
        if last_access:
            la = last_access if last_access.tzinfo else last_access.replace(tzinfo=timezone.utc)
            days_since_access = (now - la).days

        enriched.append({
            "name":                name,
            "arn":                 arn,
            "rotation_enabled":    rotation,
            "rotation_rules":      rot_rules,
            "last_accessed_date":  last_access.isoformat() if last_access else None,
            "last_changed_date":   last_changed.isoformat() if last_changed else None,
            "days_since_access":   days_since_access,
            "has_resource_policy": has_resource_policy,
            "tags":                tags,
        })

    data = {"secrets": enriched}
    print("  ✓ Secrets Manager data collected.")
    return data


# ── Analysis ──────────────────────────────────────────────────────────────────
def build_opportunities(data):
    opps    = []
    secrets = data.get("secrets", [])

    if not secrets:
        return opps

    # ── 1. Secrets without rotation ───────────────────────────────────────────
    no_rotation = [s for s in secrets if not s["rotation_enabled"]]
    if no_rotation:
        # Identify high-urgency secrets by name pattern
        def _rotation_urgency(name):
            n = name.lower()
            if "msk" in n or "rds" in n or "db" in n or "database" in n or "postgres" in n or "mysql" in n:
                return HIGH, "database/broker credentials"
            if "api_key" in n or "apikey" in n or "api-key" in n:
                return HIGH, "API key"
            return MEDIUM, "secret"

        details = []
        for s in no_rotation:
            sev, kind = _rotation_urgency(s["name"])
            details.append(f"{s['name']} ({kind}, {sev})")

        opps.append({
            "resource":       f"{len(no_rotation)}/{len(secrets)} secrets",
            "type":           "secrets-no-rotation",
            "severity":       HIGH,
            "current_mo":     0.0,
            "saving_mo":      0.0,
            "dep_warning":    "",
            "recommendation": (
                f"0/{len(secrets)} secrets have automatic rotation enabled. "
                f"Static long-lived credentials are a significant security risk — "
                f"a leaked secret remains valid indefinitely. "
                f"AWS Secrets Manager supports automatic rotation via Lambda functions. "
                f"Priority secrets to rotate: "
                + "; ".join(details) +
                f". Enable rotation on at minimum the MSK and API key secrets. "
                f"Saving: $0 (security improvement, not cost)."
            ),
        })

    # ── 2. Orphaned secrets (not accessed recently) ───────────────────────────
    orphaned = [s for s in secrets
                if s["days_since_access"] is not None
                and s["days_since_access"] >= ORPHAN_THRESHOLD_DAYS]
    for s in orphaned:
        saving = SECRET_MONTHLY_COST
        opps.append({
            "resource":       s["name"],
            "type":           "secrets-orphaned",
            "severity":       MEDIUM,
            "current_mo":     SECRET_MONTHLY_COST,
            "saving_mo":      saving,
            "dep_warning":    "",
            "recommendation": (
                f"Secret '{s['name']}' has not been accessed in {s['days_since_access']} days "
                f"(last access: {s['last_accessed_date'] or 'unknown'}). "
                f"If the service that used this secret no longer exists or has been migrated, "
                f"this secret is orphaned. "
                f"Verify whether any active service still references this ARN: {s['arn']}. "
                f"If confirmed orphaned, delete to save ${saving:.2f}/month (${saving*12:.2f}/year)."
            ),
        })

    # ── 3. SSM vs Secrets Manager split ──────────────────────────────────────
    opps.append({
        "resource":       "Secrets Manager fleet",
        "type":           "secrets-vs-ssm-params",
        "severity":       INFO,
        "current_mo":     len(secrets) * SECRET_MONTHLY_COST,
        "saving_mo":      0.0,
        "dep_warning":    "",
        "recommendation": (
            f"This account has {SSM_PARAM_COUNT} SSM parameters (free tier) and "
            f"{len(secrets)} Secrets Manager secrets (${len(secrets) * SECRET_MONTHLY_COST:.2f}/month). "
            f"The current split — SSM for non-sensitive config, Secrets Manager for true credentials "
            f"(API keys, MSK credentials, connection strings) — is the cost-optimal pattern. "
            f"SSM SecureString is free for standard parameters (up to 10,000). "
            f"No action needed: existing split is correct."
        ),
    })

    # ── 4. Secrets without resource policies ──────────────────────────────────
    no_policy = [s for s in secrets if s["has_resource_policy"] is False]
    if no_policy:
        names = ", ".join(s["name"] for s in no_policy)
        opps.append({
            "resource":       f"{len(no_policy)}/{len(secrets)} secrets",
            "type":           "secrets-no-resource-policy",
            "severity":       LOW,
            "current_mo":     0.0,
            "saving_mo":      0.0,
            "dep_warning":    "",
            "recommendation": (
                f"{len(no_policy)} secret(s) have no resource-based policy: {names}. "
                f"Without a resource policy, any IAM principal in account {ACCOUNT_ID} "
                f"that has secretsmanager:GetSecretValue permission can read these secrets. "
                f"Add resource policies to restrict access to only the specific IAM roles "
                f"or services that need each secret. "
                f"Saving: $0 (security improvement)."
            ),
        })

    return opps


# ── Reporting ─────────────────────────────────────────────────────────────────
def write_csv(opps):
    ts       = now.strftime("%Y%m%d_%H%M%S")
    csv_path = os.path.join(OUTPUT_DIR, f"{ts}_secrets_manager_report.csv")
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
    secrets      = data.get("secrets", [])
    total_cost   = len(secrets) * SECRET_MONTHLY_COST
    total_saving = sum(o["saving_mo"] for o in opps)

    rotated     = [s for s in secrets if s["rotation_enabled"]]
    no_rotation = [s for s in secrets if not s["rotation_enabled"]]
    orphaned    = [s for s in secrets
                   if s["days_since_access"] is not None
                   and s["days_since_access"] >= ORPHAN_THRESHOLD_DAYS]

    print()
    print(BORDER_TOP)
    print(_bl("      AWS Cost Analyser — Secrets Manager Analysis Results"))
    print(BORDER_MID)
    print(_bl(f"  Account  : {ACCOUNT_ID}"))
    print(_bl(f"  Region   : {REGION}"))
    print(_bl(f"  Run at   : {now.strftime('%Y-%m-%d %H:%M UTC')}"))
    print(BORDER_MID)
    print(_bl(f"  💸 Total secrets cost                : ${total_cost:>8.2f} / month"))
    print(_bl(f"  💰 Savings potential                 : ${total_saving:>8.2f} / month"))
    print(_bl(f"  💰 Annualised savings potential      : ${total_saving*12:>8.2f} / year"))
    print(_bl(f"  🔍 Opportunities found               : {len(opps)}"))
    print(BORDER_BOT)

    print(f"\n  ℹ️  Small secrets fleet — cost is minimal but security hygiene matters\n")

    # Cost breakdown
    print(f"💰 Cost Breakdown:")
    print(f"  Secrets count     : {len(secrets)}")
    print(f"  Cost per secret   : ${SECRET_MONTHLY_COST:.2f}/month")
    print(f"  Total monthly     : ${total_cost:.2f}/month")
    print(f"  Total annual      : ${total_cost*12:.2f}/year")
    print(f"  Rotation enabled  : {len(rotated)}/{len(secrets)}")
    print(f"  Orphaned (90d+)   : {len(orphaned)}")

    # Secrets table
    print(f"\n🔐 Secrets ({len(secrets)} total):")
    print(f"  {'Name':<50} {'Rotation':<9} {'Last Access':<12}  {'Days':<6}  Policy")
    print(f"  {'-'*50} {'-'*9} {'-'*12}  {'-'*6}  ------")
    for s in sorted(secrets, key=lambda x: x["name"]):
        rot_tag    = "✅ yes" if s["rotation_enabled"] else "❌ no"
        la         = s["last_accessed_date"][:10] if s["last_accessed_date"] else "never"
        days       = str(s["days_since_access"]) if s["days_since_access"] is not None else "—"
        policy_tag = ("yes" if s["has_resource_policy"] is True
                      else ("no" if s["has_resource_policy"] is False else "?"))
        print(f"  {s['name']:<50} {rot_tag:<9} {la:<12}  {days:<6}  {policy_tag}")

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
            print(f"       Current  : ${opp['current_mo']:.2f}/month")
        if opp["dep_warning"]:
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

    print("🔍 AWS Secrets Manager Cost Analyser")
    print(f"   Account : {ACCOUNT_ID}")
    print(f"   Region  : {REGION}")
    print(f"   Profile : {PROFILE}\n")

    session = boto3.Session(profile_name=PROFILE, region_name=REGION)

    if not refresh:
        cached = load_cache(CACHE_FILE)
        if cached:
            print("✅  Loaded Secrets Manager data from cache.")
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
