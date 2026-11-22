#!/usr/bin/env python3
"""
AWS Cost Analyser — ECR Skill
Implements analysis from skills/ecr.md (with real-world learnings applied)

Key behaviours:
  - All repositories: lifecycle policy presence, image count, storage size
  - Saving estimate: untagged images + tagged images beyond top-10 (keep policy simulation)
  - Stale repository detection (last push > 90 days)
  - Empty repository detection
  - Registry-level replication flagging (cost multiplier across accounts)
  - Pull-through cache awareness
"""

import boto3
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from cache_utils import load_cache, save_cache, CACHE_TTL_DAYS

# ── Configuration ─────────────────────────────────────────────────────────────
REGION     = os.environ.get("AWS_DEFAULT_REGION", "eu-central-1")
ACCOUNT_ID = boto3.Session().client("sts").get_caller_identity()["Account"]
CACHE_DIR  = os.path.join(os.path.dirname(__file__), "cache", ACCOUNT_ID, REGION)
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "output")
CACHE_FILE = os.path.join(CACHE_DIR, "ecr.json")
MAX_IMAGES  = 1000   # cap per repo to keep survey fast

now = datetime.now(timezone.utc)

# ── Severity ──────────────────────────────────────────────────────────────────
CRITICAL = "🔴 CRITICAL"
HIGH     = "🟠 HIGH"
MEDIUM   = "🟡 MEDIUM"
LOW      = "🟢 LOW"
INFO     = "ℹ️  INFO"

# ── ECR Storage pricing (eu-central-1) ───────────────────────────────────────
# Source: https://aws.amazon.com/ecr/pricing/
ECR_STORAGE_GB_MO = 0.10   # $0.10/GB/month (first 500 MB free per account)

# ── Cache helpers ─────────────────────────────────────────────────────────────
# ── Data collection ───────────────────────────────────────────────────────────
def collect(session):
    cached = load_cache(CACHE_FILE)
    if cached:
        print("  ✓ Using cached ECR data")
        return cached

    ecr = session.client("ecr", region_name=REGION)

    print("  → Listing all ECR repositories...")
    repos_raw = []
    for page in ecr.get_paginator("describe_repositories").paginate():
        repos_raw.extend(page["repositories"])
    print(f"  → {len(repos_raw)} repositories found.")

    # Registry-level settings
    replication_rules = []
    pull_through_rules = []
    scan_type = "BASIC"
    try:
        reg = ecr.describe_registry()
        replication_rules = reg.get("replicationConfiguration", {}).get("rules", [])
        sc = ecr.get_registry_scanning_configuration()
        scan_type = sc.get("scanType", "BASIC")
    except Exception:
        pass
    try:
        ptc = ecr.describe_pull_through_cache_rules()
        pull_through_rules = ptc.get("pullThroughCacheRules", [])
    except Exception:
        pass

    print(f"  → Registry: {len(replication_rules)} replication rule(s), "
          f"{len(pull_through_rules)} pull-through cache rule(s), scanning={scan_type}")

    def enrich_repo(r):
        name = r["repositoryName"]
        # Lifecycle policy
        try:
            ecr.get_lifecycle_policy(repositoryName=name)
            has_lifecycle = True
        except ecr.exceptions.LifecyclePolicyNotFoundException:
            has_lifecycle = False
        except Exception:
            has_lifecycle = None

        # Images (paginated, capped at MAX_IMAGES)
        images = []
        try:
            for page in ecr.get_paginator("describe_images").paginate(
                    repositoryName=name, filter={"tagStatus": "ANY"}):
                images.extend(page["imageDetails"])
                if len(images) >= MAX_IMAGES:
                    break
        except Exception:
            pass

        total_bytes    = sum(img.get("imageSizeInBytes", 0) for img in images)
        total_gb       = total_bytes / 1e9
        untagged       = [img for img in images if not img.get("imageTags")]
        untagged_bytes = sum(img.get("imageSizeInBytes", 0) for img in untagged)
        untagged_gb    = untagged_bytes / 1e9

        push_dates  = [img["imagePushedAt"] for img in images if "imagePushedAt" in img]
        newest_days = (now - max(push_dates)).days if push_dates else 9999
        oldest_days = (now - min(push_dates)).days if push_dates else 0

        # Estimate saving from lifecycle policy (keep last 10 tagged + delete untagged)
        tagged_count = len(images) - len(untagged)
        saving_untagged = untagged_gb
        if tagged_count > 10 and total_gb > 0:
            tagged_gb     = total_gb - untagged_gb
            saving_tagged = tagged_gb * (tagged_count - 10) / tagged_count
        else:
            saving_tagged = 0.0
        saving_lifecycle_gb = saving_untagged + saving_tagged
        saving_lifecycle_mo = saving_lifecycle_gb * ECR_STORAGE_GB_MO

        return {
            "name":               name,
            "arn":                r["repositoryArn"],
            "scan_on_push":       r.get("imageScanningConfiguration", {}).get("scanOnPush", False),
            "has_lifecycle":      has_lifecycle,
            "image_count":        len(images),
            "image_count_capped": len(images) >= MAX_IMAGES,
            "total_gb":           round(total_gb, 3),
            "untagged_count":     len(untagged),
            "untagged_gb":        round(untagged_gb, 3),
            "tagged_count":       tagged_count,
            "newest_days":        newest_days,
            "oldest_days":        oldest_days,
            "monthly_cost":       round(total_gb * ECR_STORAGE_GB_MO, 4),
            "saving_lifecycle_gb": round(saving_lifecycle_gb, 3),
            "saving_lifecycle_mo": round(saving_lifecycle_mo, 4),
        }

    print(f"  → Enriching {len(repos_raw)} repos (images + lifecycle, parallel)...")
    repos_enriched = []
    with ThreadPoolExecutor(max_workers=20) as ex:
        futures = {ex.submit(enrich_repo, r): r["repositoryName"] for r in repos_raw}
        for i, future in enumerate(as_completed(futures)):
            repos_enriched.append(future.result())
            if (i + 1) % 50 == 0:
                sys.stdout.write(f"\r  → {i+1}/{len(repos_raw)} repos enriched...")
                sys.stdout.flush()
    print(f"\r  ✓ All {len(repos_enriched)} repos enriched.    ")

    data = {
        "repositories":      repos_enriched,
        "replication_rules": [
            {"destinations": rule.get("destinations", []),
             "filters":       rule.get("repositoryFilters", [])}
            for rule in replication_rules
        ],
        "pull_through_rules": [
            {"prefix": r.get("ecrRepositoryPrefix"), "upstream": r.get("upstreamRegistryUrl")}
            for r in pull_through_rules
        ],
        "scan_type": scan_type,
    }
    save_cache(CACHE_FILE, data)
    return data


# ── Analysis ──────────────────────────────────────────────────────────────────
def analyse(data):
    repos        = data["repositories"]
    repl_rules   = data.get("replication_rules", [])
    pt_rules     = data.get("pull_through_rules", [])
    opps         = []

    # Derive replication target account IDs for the multiplier note
    repl_destinations = [
        d["registryId"]
        for rule in repl_rules
        for d in rule.get("destinations", [])
    ]
    has_broad_replication = len(repl_rules) > 0 and not any(
        rule.get("filters") for rule in repl_rules
    )

    total_cost_mo = sum(r["monthly_cost"] for r in repos)

    # ── Opp 1: No lifecycle policy ────────────────────────────────────────────
    for r in repos:
        if r["has_lifecycle"] is False and r["image_count"] > 5:
            sev = CRITICAL if r["saving_lifecycle_mo"] > 5.0 else HIGH if r["saving_lifecycle_mo"] > 1.0 else MEDIUM
            opps.append({
                "name":        r["name"],
                "type":        "ecr-missing-lifecycle-policy",
                "severity":    sev,
                "current_mo":  round(r["monthly_cost"], 2),
                "saving_mo":   round(r["saving_lifecycle_mo"], 2),
                "dep_warning": "",
                "recommendation": (
                    f"Repository '{r['name']}' has {r['image_count']}"
                    f"{'+ (capped)' if r['image_count_capped'] else ''} images "
                    f"({r['total_gb']:.1f} GB, ${r['monthly_cost']:.2f}/month) and NO lifecycle policy. "
                    f"Adding a policy to retain the last 10 tagged images and expire untagged images "
                    f"older than 7 days could save ~${r['saving_lifecycle_mo']:.2f}/month by removing "
                    f"~{r['saving_lifecycle_gb']:.1f} GB of stale/untagged images. "
                    f"Untagged: {r['untagged_count']} images ({r['untagged_gb']:.1f} GB)."
                ),
            })

    # ── Opp 2: Untagged images (even with a lifecycle policy) ─────────────────
    for r in repos:
        if r["has_lifecycle"] is True and r["untagged_count"] > 5:
            saving = r["untagged_gb"] * ECR_STORAGE_GB_MO
            if saving > 0.10:
                opps.append({
                    "name":        r["name"],
                    "type":        "ecr-untagged-images",
                    "severity":    MEDIUM,
                    "current_mo":  round(r["monthly_cost"], 2),
                    "saving_mo":   round(saving, 2),
                    "dep_warning": "",
                    "recommendation": (
                        f"Repository '{r['name']}' has a lifecycle policy but still has "
                        f"{r['untagged_count']} untagged images consuming {r['untagged_gb']:.1f} GB "
                        f"(~${saving:.2f}/month). Check that the lifecycle policy includes a rule "
                        f"to expire untagged images (e.g. sinceImagePushed > 7 days)."
                    ),
                })

    # ── Opp 3: Stale repository (no push in 90+ days) ─────────────────────────
    for r in repos:
        if r["newest_days"] > 90 and r["image_count"] > 0 and r["monthly_cost"] > 0.50:
            opps.append({
                "name":        r["name"],
                "type":        "ecr-stale-repository",
                "severity":    MEDIUM,
                "current_mo":  round(r["monthly_cost"], 2),
                "saving_mo":   round(r["monthly_cost"], 2),
                "dep_warning": "",
                "recommendation": (
                    f"Repository '{r['name']}' has not received a new image in {r['newest_days']} days "
                    f"({r['image_count']} images, {r['total_gb']:.1f} GB, "
                    f"${r['monthly_cost']:.2f}/month). If this service is retired, delete or archive "
                    f"this repository. The oldest image is {r['oldest_days']} days old."
                ),
            })

    # ── Opp 4: Empty repositories ─────────────────────────────────────────────
    empty = [r for r in repos if r["image_count"] == 0]
    if empty:
        opps.append({
            "name":        f"{len(empty)} repositories",
            "type":        "ecr-empty-repositories",
            "severity":    LOW,
            "current_mo":  0.0,
            "saving_mo":   0.0,
            "dep_warning": "",
            "recommendation": (
                f"{len(empty)} ECR repositories contain zero images. "
                f"While empty repos incur no storage cost, they add noise and may represent "
                f"abandoned projects. Consider deleting: "
                f"{', '.join(r['name'] for r in empty[:8])}"
                f"{'...' if len(empty) > 8 else ''}."
            ),
        })

    # ── Opp 5: Broad replication (no filters) — cost multiplier ───────────────
    if has_broad_replication and repl_destinations:
        dest_str = ", ".join(repl_destinations)
        multiplier_cost = total_cost_mo * len(repl_destinations)
        opps.append({
            "name":        "Registry-level replication (no filters)",
            "type":        "ecr-broad-replication",
            "severity":    HIGH,
            "current_mo":  round(total_cost_mo, 2),
            "saving_mo":   round(multiplier_cost * 0.75, 2),   # estimate: filter to 25% of repos
            "dep_warning": (
                "Restricting replication requires identifying which repositories truly need "
                "cross-account access. Audit destination account usage before adding filters."
            ),
            "recommendation": (
                f"ECR registry has {len(repl_rules)} replication rule(s) with NO repository filters, "
                f"replicating ALL {len(repos)} repositories to account(s): {dest_str}. "
                f"This multiplies your ECR storage cost — estimated ${multiplier_cost:.2f}/month "
                f"in destination account(s). Add repository filters to replicate only repositories "
                f"that genuinely need cross-account access, potentially saving "
                f"~${multiplier_cost*0.75:.2f}/month in destination accounts."
            ),
        })

    # ── Opp 6: No scan-on-push (informational) ────────────────────────────────
    no_scan = [r for r in repos if not r["scan_on_push"]]
    if len(no_scan) > 10:
        opps.append({
            "name":        f"{len(no_scan)} repositories",
            "type":        "ecr-no-image-scanning",
            "severity":    LOW,
            "current_mo":  0.0,
            "saving_mo":   0.0,
            "dep_warning": "",
            "recommendation": (
                f"{len(no_scan)}/{len(repos)} repositories do not have scan-on-push enabled. "
                f"Basic scanning is free. Enable it at registry level via "
                f"`put_registry_scanning_configuration` with BASIC scan type and a catch-all rule "
                f"to protect all repositories automatically."
            ),
        })

    return opps


# ── Reporting ─────────────────────────────────────────────────────────────────
from box_utils import _bl, BORDER_TOP, BORDER_MID, BORDER_BOT  # noqa: E402

def print_report(data, opps):
    repos       = data["repositories"]
    repl_rules  = data.get("replication_rules", [])
    pt_rules    = data.get("pull_through_rules", [])
    scan_type   = data.get("scan_type", "BASIC")

    no_lifecycle   = [r for r in repos if r["has_lifecycle"] is False]
    has_lifecycle  = [r for r in repos if r["has_lifecycle"] is True]
    stale_repos    = [r for r in repos if r["newest_days"] > 90 and r["image_count"] > 0]
    empty_repos    = [r for r in repos if r["image_count"] == 0]
    has_untagged   = [r for r in repos if r["untagged_count"] > 0]
    total_gb       = sum(r["total_gb"] for r in repos)
    total_mo       = sum(r["monthly_cost"] for r in repos)
    untagged_gb    = sum(r["untagged_gb"] for r in repos)
    total_images   = sum(r["image_count"] for r in repos)
    total_saving   = sum(o["saving_mo"] for o in opps)

    repl_dest_ids = [
        d["registryId"]
        for rule in repl_rules
        for d in rule.get("destinations", [])
    ]
    has_broad_replication = len(repl_rules) > 0 and not any(
        rule.get("filters") for rule in repl_rules
    )

    imgs_note = f"{total_images:,}"
    if any(r["image_count_capped"] for r in repos):
        imgs_note += " (some repos capped at 1000 — actual may be higher)"

    repl_dest_str = ", ".join(repl_dest_ids) if repl_dest_ids else "none"
    repl_warn = "⚠️  NO FILTERS — all repos replicated" if has_broad_replication else "Filtered ✅"

    savings_mo  = sum(r["saving_lifecycle_mo"] for r in repos)
    savings_yr  = savings_mo * 12

    print()
    print(BORDER_TOP)
    print(_bl("           AWS Cost Analyser — ECR Analysis Results"))
    print(BORDER_MID)
    print(_bl(f"  Account    : {ACCOUNT_ID}"))
    print(_bl(f"  Region     : {REGION}"))
    print(_bl(f"  Run at     : {now.strftime('%Y-%m-%d %H:%M UTC')}"))
    print(_bl(f"  Repos      : {len(repos)}"))
    print(_bl(f"  Total imgs : {imgs_note}"))
    print(_bl(f"  Storage    : {total_gb:.1f} GB across all repositories"))
    print(_bl(f"  Replication: {len(repl_rules)} rule(s) → {repl_dest_str}"))
    print(_bl(f"  Repl scope : {repl_warn}"))
    print(BORDER_MID)
    print(_bl(f"  💸 ECR storage cost (this account)   : ${total_mo:>8.2f} / month"))
    if has_broad_replication:
        repl_mult = total_mo * len(repl_dest_ids)
        print(_bl(f"  ⚠️  Estimated replication cost        : ${repl_mult:>8.2f} / month (dest. accts)"))
        print(_bl(f"  ⚠️  Total org ECR cost (est.)         : ${total_mo + repl_mult:>8.2f} / month"))
    print(_bl(f"  💰 Savings from lifecycle policies   : ${savings_mo:>8.2f} / month"))
    print(_bl(f"  💰 Annualised savings potential      : ${savings_yr:>8.2f} / year"))
    print(_bl(f"  🔍 Opportunities found               : {len(opps)}"))
    print(BORDER_BOT)

    # Policy coverage breakdown
    print(f"\n📋 Lifecycle Policy Coverage:")
    print(f"   With lifecycle policy   : {len(has_lifecycle):>4} repos")
    print(f"   Without lifecycle policy: {len(no_lifecycle):>4} repos  ⚠️  {'(100% — none set!)' if len(no_lifecycle) == len(repos) else ''}")
    print(f"\n📦 Storage Breakdown:")
    print(f"   Total images            : {total_images:>8,}")
    print(f"   Total storage           : {total_gb:>8.1f} GB  (${total_mo:.2f}/month)")
    print(f"   Untagged storage        : {untagged_gb:>8.1f} GB  (${untagged_gb*ECR_STORAGE_GB_MO:.2f}/month)")
    print(f"   Stale repos (>90d)      : {len(stale_repos):>8}")
    print(f"   Empty repos             : {len(empty_repos):>8}")

    # Top repos by cost
    sorted_by_cost = sorted(repos, key=lambda x: -x["monthly_cost"])
    print(f"\n🏆 Top 20 Repositories by Storage Cost:")
    print(f"  {'Repository':<55} {'GB':>7}  {'Images':>6}  {'$/mo':>7}  {'Lifecycle':>10}  {'Untagged':>8}  {'Stale':>5}")
    print(f"  {'-'*55} {'-'*7}  {'-'*6}  {'-'*7}  {'-'*10}  {'-'*8}  {'-'*5}")
    for r in sorted_by_cost[:20]:
        lc    = "✅" if r["has_lifecycle"] else "❌ NONE"
        stale = "YES" if r["newest_days"] > 90 else "-"
        print(f"  {r['name'][:55]:<55} {r['total_gb']:>7.2f}  "
              f"{'%d+' % r['image_count'] if r['image_count_capped'] else str(r['image_count']):>6}  "
              f"${r['monthly_cost']:>6.2f}  {lc:>10}  "
              f"{r['untagged_count']:>8}  {stale:>5}")

    # Opportunity type summary
    type_summary = {}
    for o in opps:
        t = o["type"]
        if t not in type_summary:
            type_summary[t] = {"count": 0, "saving": 0.0}
        type_summary[t]["count"]  += 1
        type_summary[t]["saving"] += o["saving_mo"]
    print("\n📊 Opportunities by Type:")
    print(f"  {'Type':<40} {'Count':>5}  {'Saving/mo':>12}  {'Saving/yr':>12}")
    print(f"  {'-'*40} {'-'*5}  {'-'*12}  {'-'*12}")
    for t, v in sorted(type_summary.items(), key=lambda x: -x[1]["saving"]):
        print(f"  {t:<40} {v['count']:>5}  ${v['saving']:>10.2f}  ${v['saving']*12:>10.2f}")

    sev_counts = {}
    for o in opps:
        sev_counts[o["severity"]] = sev_counts.get(o["severity"], 0) + 1
    print(f"\n🎯 Severity: " + "  ".join(f"{s}: {c}" for s, c in sev_counts.items()))

    # Detailed findings
    sorted_opps = sorted(opps, key=lambda x: -x["saving_mo"])
    print("\n" + "─" * 72)
    print("📋 Detailed Findings (sorted by potential saving):\n")
    for i, opp in enumerate(sorted_opps, 1):
        print(f"  [{i:02d}] {opp['severity']}  —  {opp['type']}")
        print(f"       Resource : {opp['name']}")
        if opp["saving_mo"] > 0:
            print(f"       Saving   : ${opp['saving_mo']:.2f}/month  (${opp['saving_mo']*12:.2f}/year)")
            print(f"       Current  : ${opp['current_mo']:.2f}/month")
        if opp["dep_warning"]:
            dw = opp["dep_warning"]
            print(f"       ⚠️  Dep   : {dw[:68]}")
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

    # Lifecycle policy template reminder
    print("📄 Recommended Lifecycle Policy (apply to all repositories):")
    print("""  {
    "rules": [
      {
        "rulePriority": 1,
        "description": "Expire untagged images older than 7 days",
        "selection": {"tagStatus": "untagged", "countType": "sinceImagePushed",
                      "countUnit": "days", "countNumber": 7},
        "action": {"type": "expire"}
      },
      {
        "rulePriority": 2,
        "description": "Keep only last 10 tagged images",
        "selection": {"tagStatus": "tagged", "tagPrefixList": ["v","release",""],
                      "countType": "imageCountMoreThan", "countNumber": 10},
        "action": {"type": "expire"}
      }
    ]
  }""")

    # CSV output
    ts       = now.strftime("%Y%m%d_%H%M%S")
    csv_path = os.path.join(OUTPUT_DIR, f"{ts}_ecr_report.csv")
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(csv_path, "w") as f:
        f.write("account_id,region,repository,opportunity_type,severity,"
                "current_cost_usd_monthly,estimated_saving_usd_monthly,saving_pct,saving_basis,price_basis,"
                "dependency_warning,recommendation\n")
        for o in sorted_opps:
            pct = (o["saving_mo"] / max(o["current_mo"], 0.01)) * 100 if o["current_mo"] > 0 else 0
            saving_basis = "actual"
            sev = o["severity"].replace("🔴 ","").replace("🟠 ","").replace("🟡 ","").replace("🟢 ","").replace("ℹ️  ","").strip()
            f.write(f'"{ACCOUNT_ID}","{REGION}","{o["name"]}",'
                    f'"{o["type"]}","{sev}",{o["current_mo"]:.2f},{o["saving_mo"]:.2f},{pct:.1f},"{saving_basis}","list",'
                    f'"{o["dep_warning"].replace(chr(34),chr(39))}","{o["recommendation"].replace(chr(34),chr(39))}"\n')
    print(f"\n💾 CSV report saved: {csv_path}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    import argparse
    parser = argparse.ArgumentParser(description="AWS ECR Cost Analyser")
    parser.add_argument("--force-refresh", "-f", action="store_true",
                        help="Ignore the local cache and fetch fresh data from AWS")
    args = parser.parse_args()
    if args.force_refresh:
        os.environ["AWS_COST_ANALYSER_FORCE_REFRESH"] = "1"
        print("  ⚠️  --force-refresh: ignoring cache, fetching live data from AWS")

    print("\n🔍 AWS ECR Cost Analyser")
    print(f"   Account : {ACCOUNT_ID}")
    print(f"   Region  : {REGION}")
    print(f"   Profile : {os.environ.get('AWS_PROFILE', 'default')}\n")
    session = boto3.Session()
    print("📡 Collecting ECR data...")
    data    = collect(session)
    repos   = data["repositories"]
    print(f"\n🧠 Analysing {len(repos)} repositories...")
    opps    = analyse(data)
    print_report(data, opps)


if __name__ == "__main__":
    main()
