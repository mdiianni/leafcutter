#!/usr/bin/env python3
"""
AWS Cost Analyser — Global Services Skill

Analyses services that are globally-scoped or whose costs span all regions:
  - Route 53: hosted zones, record counts, health checks
  - CloudFront: distributions, request volume, missing WAF/compression
  - IAM: access analyser (cost signal only)
  - S3 cross-region aggregation: costs per region from Cost Explorer

Rules:
  - Run with --region us-east-1 (or any region; global APIs work from anywhere)
  - Route 53 and CloudFront are always global regardless of --region
  - S3 cross-region aggregation uses Cost Explorer grouped by region
  - This module should only run when explicitly enabled (--services global)
    or when the analysis region is us-east-1 (conventional "global checks" region)
"""

from __future__ import annotations


import boto3
import csv
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from cache_utils import load_cache, save_cache
from box_utils import _bl, BORDER_TOP, BORDER_MID, BORDER_BOT

# ── Configuration ─────────────────────────────────────────────────────────────
REGION      = os.environ.get("AWS_DEFAULT_REGION", "eu-central-1")
ACCOUNT_ID  = boto3.Session().client("sts").get_caller_identity()["Account"]
CACHE_DIR   = os.path.join(os.path.dirname(__file__), "cache", ACCOUNT_ID, REGION)
OUTPUT_DIR  = os.path.join(os.path.dirname(__file__), "output")
CACHE_FILE  = os.path.join(CACHE_DIR, "global.json")

now = datetime.now(timezone.utc)

# ── Severity ──────────────────────────────────────────────────────────────────
CRITICAL = "🔴 CRITICAL"
HIGH     = "🟠 HIGH"
MEDIUM   = "🟡 MEDIUM"
LOW      = "🟢 LOW"
INFO     = "ℹ️  INFO"

# ── Cost references (global services, USD/month) ───────────────────────────────
# Route 53 pricing (global, no region variance)
R53_HOSTED_ZONE_MO   = 0.50    # per zone/month (first 25 zones)
R53_HEALTH_CHECK_MO  = 3.00    # standard health check/month
R53_ADV_CHECK_MO     = 1.00    # HTTPS/string-matching health check/month (extra)

# CloudFront: no fixed monthly fee but cost signals from distributions without WAF/compression


# ── Data collection ────────────────────────────────────────────────────────────
def collect(session) -> dict:
    """Fetch global service data. Uses cache."""
    cached = load_cache(CACHE_FILE)
    if cached:
        print("  ✅ Using cached global service data")
        return cached

    print("  📡 Collecting Route 53 data…")
    r53    = session.client("route53")
    cf     = session.client("cloudfront")
    ce     = session.client("ce", region_name="us-east-1")

    # ── Route 53 ─────────────────────────────────────────────────────────────
    zones = []
    paginator = r53.get_paginator("list_hosted_zones")
    for page in paginator.paginate():
        for z in page["HostedZones"]:
            zone_id = z["Id"].split("/")[-1]
            rr_count = z["Config"].get("Comment", "")
            is_private = z["Config"].get("PrivateZone", False)
            zones.append({
                "id":         zone_id,
                "name":       z["Name"],
                "record_count": z["ResourceRecordSetCount"],
                "private":    is_private,
                "comment":    z["Config"].get("Comment", ""),
            })

    health_checks = []
    try:
        hc_resp = r53.list_health_checks()
        for hc in hc_resp.get("HealthChecks", []):
            cfg = hc.get("HealthCheckConfig", {})
            health_checks.append({
                "id":       hc["Id"],
                "type":     cfg.get("Type", ""),
                "fqdn":     cfg.get("FullyQualifiedDomainName", ""),
                "disabled": hc.get("HealthCheckConfig", {}).get("Disabled", False),
            })
    except Exception:
        pass

    print(f"    → {len(zones)} hosted zones, {len(health_checks)} health checks")

    # ── CloudFront ────────────────────────────────────────────────────────────
    print("  📡 Collecting CloudFront data…")
    distributions = []
    try:
        paginator_cf = cf.get_paginator("list_distributions")
        for page in paginator_cf.paginate():
            dist_list = page.get("DistributionList", {})
            for d in dist_list.get("Items", []):
                config = d.get("DefaultCacheBehavior", {})
                distributions.append({
                    "id":               d["Id"],
                    "domain":           d.get("DomainName", ""),
                    "status":           d.get("Status", ""),
                    "enabled":          d.get("Enabled", True),
                    "price_class":      d.get("PriceClass", ""),
                    "http_version":     d.get("HttpVersion", ""),
                    "is_ipv6":          d.get("IsIPV6Enabled", False),
                    "compress":         config.get("Compress", False),
                    "waf_id":           d.get("WebACLId", ""),
                    "origins":          len(d.get("Origins", {}).get("Items", [])),
                    "aliases":          [a for a in d.get("Aliases", {}).get("Items", [])],
                    "price_class_all":  d.get("PriceClass", "") == "PriceClass_All",
                })
    except Exception as e:
        print(f"    ⚠️  CloudFront listing error: {e}", file=sys.stderr)
    print(f"    → {len(distributions)} CloudFront distributions")

    # ── S3 cross-region cost breakdown from Cost Explorer ─────────────────────
    print("  📡 Fetching S3 cost breakdown by region from Cost Explorer…")
    s3_by_region: dict[str, float] = {}
    try:
        lm_start = (now.replace(day=1) - timedelta(days=1)).replace(day=1).strftime("%Y-%m-%d")
        lm_end   = now.replace(day=1).strftime("%Y-%m-%d")
        resp = ce.get_cost_and_usage(
            TimePeriod={"Start": lm_start, "End": lm_end},
            Granularity="MONTHLY",
            Metrics=["UnblendedCost"],
            Filter={"Dimensions": {"Key": "SERVICE", "Values": ["Amazon Simple Storage Service"]}},
            GroupBy=[{"Type": "DIMENSION", "Key": "REGION"}],
        )
        for g in resp.get("ResultsByTime", [{}])[0].get("Groups", []):
            region_key = g["Keys"][0] or "global"
            cost = float(g["Metrics"]["UnblendedCost"]["Amount"])
            if cost > 0.01:
                s3_by_region[region_key] = round(cost, 2)
    except Exception as e:
        print(f"    ⚠️  CE S3 breakdown error: {e}", file=sys.stderr)

    # ── Route 53 cost from CE ─────────────────────────────────────────────────
    r53_cost = 0.0
    try:
        resp = ce.get_cost_and_usage(
            TimePeriod={"Start": lm_start, "End": lm_end},
            Granularity="MONTHLY",
            Metrics=["UnblendedCost"],
            Filter={"Dimensions": {"Key": "SERVICE", "Values": ["Amazon Route 53"]}},
        )
        for rb in resp.get("ResultsByTime", []):
            r53_cost += float(rb.get("Total", {}).get("UnblendedCost", {}).get("Amount", 0))
    except Exception:
        pass

    # ── CloudFront cost from CE ───────────────────────────────────────────────
    cf_cost = 0.0
    try:
        resp = ce.get_cost_and_usage(
            TimePeriod={"Start": lm_start, "End": lm_end},
            Granularity="MONTHLY",
            Metrics=["UnblendedCost"],
            Filter={"Dimensions": {"Key": "SERVICE", "Values": ["Amazon CloudFront"]}},
        )
        for rb in resp.get("ResultsByTime", []):
            cf_cost += float(rb.get("Total", {}).get("UnblendedCost", {}).get("Amount", 0))
    except Exception:
        pass

    data = {
        "account_id":     ACCOUNT_ID,
        "region":         REGION,
        "zones":          zones,
        "health_checks":  health_checks,
        "distributions":  distributions,
        "s3_by_region":   s3_by_region,
        "r53_cost_mo":    round(r53_cost, 2),
        "cf_cost_mo":     round(cf_cost, 2),
    }

    os.makedirs(CACHE_DIR, exist_ok=True)
    save_cache(CACHE_FILE, data, account_id=ACCOUNT_ID, region=REGION)
    return data


# ── Analysis ──────────────────────────────────────────────────────────────────
def analyse(data: dict) -> list[dict]:
    opps = []
    zones        = data["zones"]
    health_checks= data["health_checks"]
    distributions= data["distributions"]
    s3_by_region = data["s3_by_region"]
    r53_cost_mo  = data["r53_cost_mo"]
    cf_cost_mo   = data["cf_cost_mo"]

    # ── Route 53: unused / empty zones ────────────────────────────────────────
    empty_zones = [z for z in zones if z["record_count"] <= 2]  # SOA + NS only
    if empty_zones:
        saving_mo = len(empty_zones) * R53_HOSTED_ZONE_MO
        opps.append({
            "service":    "Route53",
            "resource_id": f"{len(empty_zones)} zones",
            "type":       "r53-empty-zones",
            "severity":   MEDIUM,
            "saving_mo":  round(saving_mo, 2),
            "current_cost": round(saving_mo, 2),
            "saving_basis": "actual",
            "price_basis":  "list",
            "recommendation": (
                f"{len(empty_zones)} Route 53 hosted zone(s) contain only SOA/NS records "
                f"(effectively empty). Each zone costs ${R53_HOSTED_ZONE_MO:.2f}/month. "
                f"Delete unused zones to save ~${saving_mo:.2f}/month. "
                f"Zones: {', '.join(z['name'] for z in empty_zones[:5])}"
                + (" …" if len(empty_zones) > 5 else "")
            ),
            "details": {"zone_names": [z["name"] for z in empty_zones]},
        })

    # ── Route 53: disabled health checks ─────────────────────────────────────
    disabled_hc = [h for h in health_checks if h.get("disabled")]
    if disabled_hc:
        saving_mo = len(disabled_hc) * R53_HEALTH_CHECK_MO
        opps.append({
            "service":    "Route53",
            "resource_id": f"{len(disabled_hc)} health checks",
            "type":       "r53-disabled-health-checks",
            "severity":   LOW,
            "saving_mo":  round(saving_mo, 2),
            "current_cost": round(saving_mo, 2),
            "saving_basis": "actual",
            "price_basis":  "list",
            "recommendation": (
                f"{len(disabled_hc)} Route 53 health check(s) are DISABLED but still incur "
                f"the ${R53_HEALTH_CHECK_MO:.2f}/month charge. Delete them if no longer needed. "
                f"Estimated saving: ~${saving_mo:.2f}/month."
            ),
            "details": {"health_check_ids": [h["id"] for h in disabled_hc]},
        })

    # ── Route 53: public zones without health checks (informational) ──────────
    public_zones = [z for z in zones if not z["private"]]
    if public_zones and not health_checks:
        opps.append({
            "service":    "Route53",
            "resource_id": f"{len(public_zones)} public zones",
            "type":       "r53-no-health-checks",
            "severity":   INFO,
            "saving_mo":  0.0,
            "current_cost": 0.0,
            "saving_basis": "actual",
            "price_basis":  "list",
            "recommendation": (
                f"{len(public_zones)} public hosted zone(s) found but NO health checks configured. "
                "Route 53 health checks enable failover routing and improve reliability. "
                "Consider adding health checks for critical endpoints."
            ),
            "details": {},
        })

    # ── CloudFront: distributions without compression ─────────────────────────
    no_compress = [d for d in distributions if not d["compress"] and d["enabled"]]
    if no_compress:
        opps.append({
            "service":    "CloudFront",
            "resource_id": f"{len(no_compress)} distributions",
            "type":       "cf-no-compression",
            "severity":   MEDIUM,
            "saving_mo":  0.0,   # savings vary by traffic volume; flag for review
            "current_cost": round(cf_cost_mo, 2),
            "saving_basis": "estimated",
            "price_basis":  "list",
            "recommendation": (
                f"{len(no_compress)} CloudFront distribution(s) do not have compression enabled. "
                "Enabling gzip/Brotli compression reduces data transfer costs (CloudFront charges "
                "per GB transferred) and improves end-user latency. Enable 'Compress objects automatically' "
                "on the default cache behaviour."
            ),
            "details": {"distribution_ids": [d["id"] for d in no_compress[:10]]},
        })

    # ── CloudFront: distributions without WAF ────────────────────────────────
    no_waf = [d for d in distributions if not d.get("waf_id") and d["enabled"]]
    if no_waf:
        opps.append({
            "service":    "CloudFront",
            "resource_id": f"{len(no_waf)} distributions",
            "type":       "cf-no-waf",
            "severity":   LOW,
            "saving_mo":  0.0,
            "current_cost": round(cf_cost_mo, 2),
            "saving_basis": "actual",
            "price_basis":  "list",
            "recommendation": (
                f"{len(no_waf)} CloudFront distribution(s) have no WAF (Web ACL) attached. "
                "Without WAF, DDoS and bot traffic can inflate data-transfer costs significantly. "
                "Associate an AWS WAF Web ACL to each distribution. "
                "Note: WAF has its own cost ($5/Web ACL/month + $1/rule/month)."
            ),
            "details": {"distribution_ids": [d["id"] for d in no_waf[:10]]},
        })

    # ── CloudFront: HTTP/1.1 distributions ───────────────────────────────────
    old_http = [d for d in distributions if d.get("http_version") in ("http1.1", "") and d["enabled"]]
    if old_http:
        opps.append({
            "service":    "CloudFront",
            "resource_id": f"{len(old_http)} distributions",
            "type":       "cf-http-version",
            "severity":   LOW,
            "saving_mo":  0.0,
            "current_cost": round(cf_cost_mo, 2),
            "saving_basis": "actual",
            "price_basis":  "list",
            "recommendation": (
                f"{len(old_http)} CloudFront distribution(s) are not using HTTP/2 or HTTP/3. "
                "Upgrading to HTTP/2 (or HTTP/3) reduces connection overhead and can improve "
                "cache hit rates, lowering origin data transfer costs."
            ),
            "details": {"distribution_ids": [d["id"] for d in old_http[:10]]},
        })

    # ── S3 cross-region aggregation (informational) ───────────────────────────
    if s3_by_region:
        top_region  = max(s3_by_region, key=s3_by_region.get)
        total_s3_mo = sum(s3_by_region.values())
        region_breakdown = ", ".join(
            f"{r}: ${c:.2f}" for r, c in sorted(s3_by_region.items(), key=lambda x: -x[1])
        )
        opps.append({
            "service":    "S3",
            "resource_id": "cross-region-aggregation",
            "type":       "s3-cross-region-costs",
            "severity":   INFO,
            "saving_mo":  0.0,
            "current_cost": round(total_s3_mo, 2),
            "saving_basis": "actual",
            "price_basis":  "list",
            "recommendation": (
                f"S3 total cost last month: ${total_s3_mo:.2f} across "
                f"{len(s3_by_region)} region(s). "
                f"Largest cost: {top_region} (${s3_by_region[top_region]:.2f}/month). "
                f"Breakdown: {region_breakdown}. "
                "Run per-region S3 analysis for lifecycle/storage-class opportunities."
            ),
            "details": {"s3_by_region": s3_by_region},
        })

    return opps


# ── Reporting ─────────────────────────────────────────────────────────────────
def print_report(data: dict, opps: list[dict]):
    zones        = data["zones"]
    distributions= data["distributions"]
    health_checks= data["health_checks"]
    s3_by_region = data["s3_by_region"]

    print()
    print(BORDER_TOP)
    print(_bl("      AWS Cost Analyser — Global Services Analysis Results"))
    print(BORDER_MID)
    print(_bl(f"  Account        : {ACCOUNT_ID}"))
    print(_bl(f"  Run at         : {now.strftime('%Y-%m-%d %H:%M UTC')}"))
    print(_bl(f"  Route 53 zones : {len(zones)} ({len([z for z in zones if not z['private']])} public)"))
    print(_bl(f"  Health checks  : {len(health_checks)}"))
    print(_bl(f"  CloudFront     : {len(distributions)} distributions"))
    print(_bl(f"  S3 regions     : {len(s3_by_region)} with costs"))
    print(BORDER_MID)
    total_saving = sum(o["saving_mo"] for o in opps)
    print(_bl(f"  💰 Potential saving  : ${total_saving:>8.2f} / month"))
    print(_bl(f"  🔍 Opportunities     : {len([o for o in opps if o['saving_mo'] > 0 or o['type'] != 'no-findings'])}"))
    print(BORDER_BOT)

    if not opps:
        print("\n✅ No cost-saving opportunities found for global services.")
    else:
        for o in sorted(opps, key=lambda x: -x["saving_mo"]):
            sev = o["severity"]
            print(f"\n  [{sev}] {o['type']}")
            print(f"    Service : {o['service']}")
            print(f"    Saving  : ${o['saving_mo']:.2f}/month")
            rec = o["recommendation"]
            for line in [rec[i:i+80] for i in range(0, len(rec), 80)]:
                print(f"    {line}")

    # CSV
    ts = now.strftime("%Y%m%d_%H%M%S")
    csv_path = os.path.join(OUTPUT_DIR, f"{ts}_global_report.csv")
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    FIELDNAMES = [
        "account_id", "region", "service", "resource_id", "type", "severity",
        "current_cost", "saving_mo", "saving_yr", "saving_basis", "price_basis",
        "recommendation",
    ]
    with open(csv_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES, extrasaction="ignore")
        writer.writeheader()
        if opps:
            for o in opps:
                row = {
                    "account_id":  ACCOUNT_ID,
                    "region":      "global",
                    "service":     o.get("service", "Global"),
                    "resource_id": o.get("resource_id", ""),
                    "type":        o.get("type", ""),
                    "severity":    o["severity"].replace("🔴 ", "").replace("🟠 ", "").replace("🟡 ", "").replace("🟢 ", "").replace("⚠️  ", "").replace("ℹ️  ", "").strip(),
                    "current_cost": o.get("current_cost", 0.0),
                    "saving_mo":   o["saving_mo"],
                    "saving_yr":   round(o["saving_mo"] * 12, 2),
                    "saving_basis": o.get("saving_basis", "actual"),
                    "price_basis":  o.get("price_basis", "list"),
                    "recommendation": o.get("recommendation", "").replace("\n", " "),
                }
                writer.writerow(row)
        else:
            writer.writerow({
                "account_id": ACCOUNT_ID, "region": "global",
                "service": "Global", "resource_id": "", "type": "no-findings",
                "severity": "INFO", "current_cost": 0.0, "saving_mo": 0.0,
                "saving_yr": 0.0, "saving_basis": "actual", "price_basis": "list",
                "recommendation": f"No global service cost-saving opportunities found. "
                                  f"Route53 zones: {len(zones)}, CloudFront distributions: {len(distributions)}.",
            })
    print(f"\n💾 CSV report saved: {csv_path}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="AWS Cost Analyser — Global Services (Route53, CloudFront, S3 cross-region)"
    )
    parser.add_argument("--force-refresh", "-f", action="store_true",
                        help="Ignore cache and fetch fresh data from AWS")
    args = parser.parse_args()
    if args.force_refresh:
        os.environ["AWS_COST_ANALYSER_FORCE_REFRESH"] = "1"
        print("  ⚠️  --force-refresh: ignoring cache")

    print("\n🌐 AWS Global Services Cost Analyser")
    print(f"   Account : {ACCOUNT_ID}")
    print(f"   Profile : {os.environ.get('AWS_PROFILE', 'default')}\n")

    session = boto3.Session()
    data    = collect(session)
    opps    = analyse(data)
    print_report(data, opps)


if __name__ == "__main__":
    main()
