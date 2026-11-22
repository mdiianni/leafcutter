#!/usr/bin/env python3
"""
AWS Cost Analyser — SSM Parameter Store Skill

Key findings for this account:
  - 485 Standard-tier parameters (all free, within 10,000-parameter free tier)
  - 478 of 485 parameters not modified in 1+ year (98.6% stale)
  - 0 parameters with expiration policies
  - 481 SecureString, 4 String
  - No Advanced-tier parameters

Key cost drivers:
  - Advanced parameters: $0.05/parameter/month
  - Standard parameters: FREE (storage + up to 40 TPS API throughput)
  - Primary concern is security surface and operational hygiene, not direct cost
"""

import boto3
import os
import sys
from collections import Counter, defaultdict
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
CACHE_FILE = os.path.join(CACHE_DIR, "ssm.json")

now = datetime.now(timezone.utc)

# ── Severity ──────────────────────────────────────────────────────────────────
CRITICAL = "🔴 CRITICAL"
HIGH     = "🟠 HIGH"
MEDIUM   = "🟡 MEDIUM"
LOW      = "🟢 LOW"
INFO     = "ℹ️  INFO"

# ── Pricing (eu-central-1) ────────────────────────────────────────────────────
PRICE = {
    "advanced_param_mo": 0.05,   # per Advanced-tier parameter per month
}

STALE_DAYS = 365   # parameters not modified in this many days are considered stale

CREDENTIAL_KEYWORDS = ("password", "passwd", "key", "secret", "token", "credential", "apikey", "api_key")


# ── Data collection ───────────────────────────────────────────────────────────
def collect(session):
    ssm = session.client("ssm", region_name=REGION)

    print("  → Paginating SSM parameters (describe_parameters) …")
    params = []
    paginator = ssm.get_paginator("describe_parameters")
    for page in paginator.paginate():
        params.extend(page["Parameters"])
        sys.stdout.write(f"\r    → {len(params)} parameters fetched …")
        sys.stdout.flush()
    print(f"\r  ✓ {len(params)} parameters found.                    ")

    cutoff = now - timedelta(days=STALE_DAYS)

    enriched = []
    for p in params:
        name           = p["Name"]
        param_type     = p.get("Type", "String")
        tier           = p.get("Tier", "Standard")
        last_modified  = p.get("LastModifiedDate")
        policies       = p.get("Policies", [])
        has_expiry     = any(pol.get("Type") == "Expiration" for pol in policies) if policies else False

        # Determine path prefix (first two segments) and flat param status
        parts = [seg for seg in name.split("/") if seg]
        if len(parts) >= 2:
            prefix = "/" + "/".join(parts[:2])
        elif len(parts) == 1:
            prefix = "/" + parts[0]
        else:
            prefix = "/"
        is_flat = not name.startswith("/") or name.count("/") < 2

        # Stale check
        if last_modified:
            if hasattr(last_modified, "tzinfo") and last_modified.tzinfo is None:
                last_modified = last_modified.replace(tzinfo=timezone.utc)
            is_stale = last_modified < cutoff
        else:
            is_stale = True

        # Credential keyword check (for String type only)
        name_lower       = name.lower()
        looks_like_cred  = (param_type == "String" and
                            any(kw in name_lower for kw in CREDENTIAL_KEYWORDS))

        enriched.append({
            "name":           name,
            "type":           param_type,
            "tier":           tier,
            "last_modified":  last_modified.isoformat() if last_modified else None,
            "has_expiry":     has_expiry,
            "prefix":         prefix,
            "is_flat":        is_flat,
            "is_stale":       is_stale,
            "looks_like_cred": looks_like_cred,
        })

    data = {"parameters": enriched}
    return data


# ── Analysis ──────────────────────────────────────────────────────────────────
def build_opportunities(data):
    params = data["parameters"]
    opps   = []

    if not params:
        return opps

    advanced      = [p for p in params if p["tier"] == "Advanced"]
    stale         = [p for p in params if p["is_stale"]]
    no_expiry     = [p for p in params if not p["has_expiry"]]
    flat          = [p for p in params if p["is_flat"]]
    string_creds  = [p for p in params if p["looks_like_cred"]]

    # Opp 1: Stale parameters
    if stale:
        prefix_counts = Counter(p["prefix"] for p in stale)
        top_prefixes  = prefix_counts.most_common(5)
        top_str       = ", ".join(f"{pfx} ({cnt})" for pfx, cnt in top_prefixes)
        opps.append({
            "resource":       f"{len(stale)} stale parameters",
            "type":           "ssm-stale-parameters",
            "severity":       MEDIUM,
            "current_mo":     0.0,
            "saving_mo":      0.0,
            "dep_warning":    (
                "Deleting parameters without confirming they are no longer referenced "
                "by running services or CI/CD pipelines will cause runtime failures."
            ),
            "recommendation": (
                f"{len(stale)} of {len(params)} parameters ({len(stale)/len(params)*100:.1f}%) "
                f"have not been modified in {STALE_DAYS}+ days. "
                f"Stale parameters may belong to decommissioned services and expand the "
                f"attack surface (unrotated secrets, orphaned credentials). "
                f"Storage cost is $0 (Standard tier), but security hygiene recommends "
                f"auditing and deleting orphaned parameters. "
                f"Top path prefixes in stale set: {top_str}."
            ),
        })

    # Opp 2: No expiration policies
    if no_expiry:
        opps.append({
            "resource":       f"{len(no_expiry)} parameters without expiry",
            "type":           "ssm-no-expiration-policies",
            "severity":       LOW,
            "current_mo":     0.0,
            "saving_mo":      0.0,
            "dep_warning":    (
                "SSM Advanced tier is required for expiration policies. "
                "Upgrading to Advanced costs $0.05/parameter/month."
            ),
            "recommendation": (
                f"0 of {len(params)} parameters have expiration policies. "
                f"SSM Standard-tier parameters do not support expiration or notification policies. "
                f"For secrets requiring rotation or time-limited access, consider: "
                f"(a) upgrading to SSM Advanced tier to attach expiration policies, "
                f"(b) migrating secrets to AWS Secrets Manager (built-in rotation, "
                f"costs $0.40/secret/month + $0.05 per 10,000 API calls)."
            ),
        })

    # Opp 3: Advanced tier parameters (flag cost)
    if advanced:
        advanced_cost = len(advanced) * PRICE["advanced_param_mo"]
        # Check which could be downgraded (no policies, under 4KB)
        downgradeable = [p for p in advanced if not p["has_expiry"]]
        opps.append({
            "resource":       f"{len(advanced)} Advanced-tier parameters",
            "type":           "ssm-advanced-params-cost",
            "severity":       HIGH,
            "current_mo":     round(advanced_cost, 2),
            "saving_mo":      round(len(downgradeable) * PRICE["advanced_param_mo"], 2),
            "dep_warning":    (
                "Downgrading Advanced parameters to Standard will remove any attached "
                "notification or expiration policies and reduces max value size from 8KB to 4KB."
            ),
            "recommendation": (
                f"{len(advanced)} Advanced-tier parameters at "
                f"${advanced_cost:.2f}/month (${advanced_cost*12:.2f}/year). "
                f"{len(downgradeable)} of these have no expiration policies and may be "
                f"downgradeable to Standard tier (free) if the value is ≤4KB. "
                f"Review each parameter and downgrade where Advanced features are not needed."
            ),
        })

    # Opp 4: Flat parameters (no path hierarchy)
    if flat:
        opps.append({
            "resource":       f"{len(flat)} flat parameters",
            "type":           "ssm-flat-params",
            "severity":       LOW,
            "current_mo":     0.0,
            "saving_mo":      0.0,
            "dep_warning":    (
                "Renaming parameters requires updating all consumers "
                "(application code, ECS task definitions, Lambda environment variables, etc.)."
            ),
            "recommendation": (
                f"{len(flat)} parameter(s) do not use a path-based hierarchy "
                f"(e.g., /service/env/key). Flat names make it difficult to apply IAM "
                f"path-based access controls (e.g., allow role X to read /myservice/*). "
                f"Flat params: {', '.join(p['name'] for p in flat[:5])}. "
                f"Recommend migrating to /service/environment/parameter-name format."
            ),
        })

    # Opp 5: String-type parameters that look like credentials
    if string_creds:
        opps.append({
            "resource":       f"{len(string_creds)} potential plain-text credentials",
            "type":           "ssm-string-type-credentials",
            "severity":       MEDIUM,
            "current_mo":     0.0,
            "saving_mo":      0.0,
            "dep_warning":    "",
            "recommendation": (
                f"{len(string_creds)} String (not SecureString) parameter(s) have names "
                f"suggesting they may contain credentials (keywords: password, key, secret, "
                f"token, credential). Plain-text parameters are stored unencrypted and visible "
                f"in CloudTrail API call logs. "
                f"Affected: {', '.join(p['name'] for p in string_creds[:5])}. "
                f"Upgrade to SecureString type to ensure encryption at rest using KMS."
            ),
        })

    return opps


# ── Reporting ─────────────────────────────────────────────────────────────────
def print_report(data, opps):
    params = data["parameters"]

    tier_counts  = Counter(p["tier"] for p in params)
    type_counts  = Counter(p["type"] for p in params)
    stale_count  = sum(1 for p in params if p["is_stale"])
    advanced     = [p for p in params if p["tier"] == "Advanced"]
    advanced_cost = len(advanced) * PRICE["advanced_param_mo"]
    total_cost   = advanced_cost
    total_saving = sum(o["saving_mo"] for o in opps)

    # Path prefix breakdown
    prefix_counts = Counter(p["prefix"] for p in params)
    top_prefixes  = prefix_counts.most_common(10)

    print()
    print(BORDER_TOP)
    print(_bl("          AWS Cost Analyser — SSM Parameter Store Results"))
    print(BORDER_MID)
    print(_bl(f"  Account  : {ACCOUNT_ID}"))
    print(_bl(f"  Region   : {REGION}"))
    print(_bl(f"  Run at   : {now.strftime('%Y-%m-%d %H:%M UTC')}"))
    print(BORDER_MID)
    print(_bl(f"  Total parameters : {len(params)}"))
    for tier, cnt in sorted(tier_counts.items()):
        tier_str = f"    {tier:<12}: {cnt}"
        print(_bl(f"  {tier_str}"))
    for ptype, cnt in sorted(type_counts.items()):
        type_str = f"    {ptype:<12}: {cnt}"
        print(_bl(f"  {type_str}"))
    print(_bl(f"  Stale (>{STALE_DAYS}d)   : {stale_count:<3} ({stale_count/max(len(params),1)*100:.1f}% of total){''}"))
    print(BORDER_MID)
    std_cost_str  = "FREE (Standard tier, within 10,000 free)"
    adv_cost_str  = f"${advanced_cost:.2f}/month ({len(advanced)} × ${PRICE['advanced_param_mo']:.2f})"
    print(_bl(f"  💸 Standard param cost  : {std_cost_str}"))
    print(_bl(f"  💸 Advanced param cost  : {adv_cost_str}"))
    print(_bl(f"  💸 Total SSM cost       : ${total_cost:<8.2f} / month{''}"))
    print(_bl(f"  💰 Potential savings    : ${total_saving:<8.2f} / month{''}"))
    print(_bl(f"  🔍 Opportunities found  : {len(opps)}"))
    print(BORDER_BOT)

    # Path prefix breakdown
    print("\n📂 Top 10 Path Prefixes:")
    print(f"  {'Prefix':<50} {'Count':>6}")
    print(f"  {'-'*50} {'-'*6}")
    for prefix, cnt in top_prefixes:
        print(f"  {prefix[:50]:<50} {cnt:>6}")

    # Stale breakdown by prefix
    if stale_count:
        stale_by_prefix = Counter(p["prefix"] for p in params if p["is_stale"])
        print(f"\n⏰ Stale Parameters by Prefix (not modified in {STALE_DAYS}+ days):")
        print(f"  {'Prefix':<50} {'Stale':>6}")
        print(f"  {'-'*50} {'-'*6}")
        for prefix, cnt in stale_by_prefix.most_common(10):
            print(f"  {prefix[:50]:<50} {cnt:>6}")

    # Cost breakdown
    print(f"\n💰 Cost Breakdown:")
    print(f"  Standard tier ({tier_counts.get('Standard', 0)} params)  : FREE")
    if advanced:
        print(f"  Advanced tier ({len(advanced)} params)  : ${advanced_cost:.2f}/month")
    print(f"  Total                           : ${total_cost:.2f}/month")

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
    csv_path = os.path.join(OUTPUT_DIR, f"{ts}_ssm_report.csv")
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

    print("🔍 AWS SSM Parameter Store Cost Analyser")
    print(f"   Account : {ACCOUNT_ID}")
    print(f"   Region  : {REGION}")
    print(f"   Profile : {PROFILE}\n")

    session = boto3.Session(profile_name=PROFILE, region_name=REGION)

    if not refresh:
        cached = load_cache(CACHE_FILE)
        if cached:
            print("✅  Loaded SSM data from cache.")
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
