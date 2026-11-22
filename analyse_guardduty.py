#!/usr/bin/env python3
"""
AWS Cost Analyser — GuardDuty Skill
Implements analysis from skills/guardduty.md

Key checks:
  - Month-to-date cost breakdown by feature (CloudTrail, FlowLogs, DNS)
  - Stale unarchived findings (no suppression rules)
  - No publishing destination (S3 export)
  - High CloudTrail cost relative to account activity
  - EKS audit logs disabled (security gap if EKS present)
  - No custom threat intelligence sets
  - Runtime Monitoring disabled for ECS Fargate

Real-world API quirks (see skills/guardduty.md):
  - UsageCriteria (not UsageCriterion)
  - Features list must be non-empty
  - Unit="USD" is the only valid value
  - Usage stats are month-to-date only — extrapolate to 30-day estimate
"""

import boto3
import json
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
CACHE_FILE = os.path.join(CACHE_DIR, "guardduty.json")

now = datetime.now(timezone.utc)

# ── Severity ──────────────────────────────────────────────────────────────────
CRITICAL = "🔴 CRITICAL"
HIGH     = "🟠 HIGH"
MEDIUM   = "🟡 MEDIUM"
LOW      = "🟢 LOW"
INFO     = "ℹ️  INFO"

# ── Pricing (eu-central-1, per unit) ─────────────────────────────────────────
# Prices are indicative — usage stats already return USD amounts
CLOUDTRAIL_HIGH_THRESHOLD_MO = 100.0   # flag if extrapolated monthly cost > $100
STALE_FINDING_DAYS            = 30     # findings older than this are "stale"

# ── All known features for usage query ───────────────────────────────────────
USAGE_FEATURES = [
    "CLOUD_TRAIL",
    "DNS_LOGS",
    "FLOW_LOGS",
    "S3_DATA_EVENTS",
    "EKS_AUDIT_LOGS",
    "EBS_MALWARE_PROTECTION",
    "RDS_LOGIN_EVENTS",
    "LAMBDA_NETWORK_LOGS",
]

# ── AWS data collection ───────────────────────────────────────────────────────
def collect(session):
    gd = session.client("guardduty", region_name=REGION)

    print("  Fetching GuardDuty detectors …")
    detector_ids = gd.list_detectors()["DetectorIds"]
    print(f"    → {len(detector_ids)} detector(s)")

    detectors = []
    for did in detector_ids:
        det = gd.get_detector(DetectorId=did)
        features = {
            f["Name"]: f["Status"]
            for f in det.get("Features", [])
        }
        runtime_addons = {}
        for f in det.get("Features", []):
            if f["Name"] == "RUNTIME_MONITORING":
                for addon in f.get("AdditionalConfiguration", []):
                    runtime_addons[addon["Name"]] = addon["Status"]

        # Month-to-date usage costs
        print("  Fetching month-to-date usage statistics …")
        usage_by_feature = {}
        try:
            resp = gd.get_usage_statistics(
                DetectorId=did,
                UsageStatisticType="SUM_BY_FEATURES",
                UsageCriteria={"Features": USAGE_FEATURES},
                Unit="USD"
            )
            for item in resp.get("UsageStatistics", {}).get("SumByFeature", []):
                usage_by_feature[item["Feature"]] = float(item["Total"]["Amount"])
        except Exception as e:
            print(f"    ⚠️  Usage stats error: {e}")

        # Extrapolate MTD → full-month estimate
        day_of_month = now.day
        usage_extrapolated = {
            k: round((v / day_of_month) * 30, 4)
            for k, v in usage_by_feature.items()
        }

        # Findings
        print("  Fetching findings …")
        findings_data = []
        for min_sev, label in [(7, "HIGH"), (4, "MEDIUM")]:
            try:
                ids = gd.list_findings(
                    DetectorId=did,
                    FindingCriteria={"Criterion": {"severity": {"Gte": min_sev}}}
                )["FindingIds"]
                if min_sev == 7.0:
                    high_ids = ids
                else:
                    all_ids = ids
            except Exception as e:
                print(f"    ⚠️  list_findings error: {e}")
                high_ids, all_ids = [], []

        # Get full details in batches of 50
        for i in range(0, len(all_ids), 50):
            try:
                batch = gd.get_findings(DetectorId=did, FindingIds=all_ids[i:i+50])
                findings_data.extend([
                    {
                        "id":         f["Id"],
                        "type":       f["Type"],
                        "severity":   f["Severity"],
                        "title":      f.get("Title", ""),
                        "created_at": f.get("CreatedAt", ""),
                        "updated_at": f.get("UpdatedAt", ""),
                        "archived":   f.get("Service", {}).get("Archived", False),
                        "count":      f.get("Service", {}).get("Count", 1),
                        "country":    (f.get("Service", {})
                                        .get("Action", {})
                                        .get("AwsApiCallAction", {})
                                        .get("RemoteIpDetails", {})
                                        .get("Country", {})
                                        .get("CountryName", "Unknown")),
                    }
                    for f in batch["Findings"]
                ])
            except Exception as e:
                print(f"    ⚠️  get_findings error: {e}")

        # Suppression filters
        filters = []
        try:
            filters = gd.list_filters(DetectorId=did).get("FilterNames", [])
        except Exception:
            pass

        # Publishing destinations
        destinations = []
        try:
            destinations = gd.list_publishing_destinations(DetectorId=did).get("Destinations", [])
        except Exception:
            pass

        # Threat intel sets
        threat_intel_sets = []
        ip_sets = []
        try:
            threat_intel_sets = gd.list_threat_intel_sets(DetectorId=did).get("ThreatIntelSetIds", [])
            ip_sets           = gd.list_ip_sets(DetectorId=did).get("IpSetIds", [])
        except Exception:
            pass

        detectors.append({
            "detector_id":          did,
            "status":               det.get("Status"),
            "created_at":           str(det.get("CreatedAt", "")),
            "updated_at":           str(det.get("UpdatedAt", "")),
            "finding_publish_freq": det.get("FindingPublishingFrequency"),
            "features":             features,
            "runtime_addons":       runtime_addons,
            "usage_mtd_usd":        usage_by_feature,
            "usage_est_monthly_usd":usage_extrapolated,
            "mtd_days":             day_of_month,
            "findings":             findings_data,
            "suppression_filters":  filters,
            "publishing_destinations": destinations,
            "threat_intel_sets":    threat_intel_sets,
            "ip_sets":              ip_sets,
        })

    print(f"    → collected {len(detectors)} detector(s) with full detail")
    return {"detectors": detectors}

# ── Opportunity builders ──────────────────────────────────────────────────────
def build_opportunities(data):
    opps = []
    for det in data["detectors"]:
        did      = det["detector_id"]
        features = det["features"]
        findings = det["findings"]
        usage_mo = det["usage_est_monthly_usd"]
        mtd      = det["usage_mtd_usd"]

        total_mo  = sum(usage_mo.values())
        ct_mo     = usage_mo.get("CLOUD_TRAIL", 0)
        fl_mo     = usage_mo.get("FLOW_LOGS", 0)
        dns_mo    = usage_mo.get("DNS_LOGS", 0)

        # ── Opp 1: Stale unarchived findings
        stale_cutoff = now - timedelta(days=STALE_FINDING_DAYS)
        stale = []
        for f in findings:
            updated = f.get("updated_at")
            if updated:
                try:
                    updated_dt = datetime.fromisoformat(str(updated).replace("Z", "+00:00"))
                    if updated_dt < stale_cutoff and not f.get("archived", False):
                        stale.append(f)
                except Exception:
                    pass

        if stale:
            type_counts = {}
            for f in stale:
                type_counts[f["type"]] = type_counts.get(f["type"], 0) + 1
            top_types = sorted(type_counts.items(), key=lambda x: -x[1])
            type_summary = ", ".join(f"{c}× {t.split(':')[-1]}" for t, c in top_types[:5])
            countries = {f.get("country", "Unknown") for f in stale if f.get("country")}
            opps.append({
                "id":          "guardduty-stale-findings",
                "service":     "GuardDuty",
                "severity":    MEDIUM,
                "title":       f"{len(stale)} stale unarchived findings (>{STALE_FINDING_DAYS}d, not actioned)",
                "description": (
                    f"Detector {did} has {len(stale)} findings not updated in >{STALE_FINDING_DAYS} days "
                    f"and not archived. Types: {type_summary}. "
                    f"Countries: {', '.join(countries)}. "
                    "No suppression filters configured. If these are recurring false positives, "
                    "create suppression rules to auto-archive them and reduce analyst noise."
                ),
                "saving_mo":   0.0,
                "saving_yr":   0.0,
            })

        # ── Opp 2: No suppression filters
        if not det["suppression_filters"] and findings:
            opps.append({
                "id":          "guardduty-no-suppression-filters",
                "service":     "GuardDuty",
                "severity":    LOW,
                "title":       "No GuardDuty suppression filters configured",
                "description": (
                    f"No suppression filters (allow-list rules) are configured for detector {did}. "
                    "Suppression filters auto-archive recurring false positives such as known "
                    "dev tooling IPs or expected infrastructure scanning. Without them, findings "
                    "accumulate and create alert fatigue."
                ),
                "saving_mo":   0.0,
                "saving_yr":   0.0,
            })

        # ── Opp 3: No publishing destination
        if not det["publishing_destinations"]:
            opps.append({
                "id":          "guardduty-no-export-destination",
                "service":     "GuardDuty",
                "severity":    LOW,
                "title":       "No finding publishing destination (findings lost after 90 days)",
                "description": (
                    f"Detector {did} has no S3 publishing destination. GuardDuty findings are "
                    "only retained for 90 days in the console. Adding an S3 export preserves "
                    "findings indefinitely, enables cross-account analysis, and integrates with "
                    "SIEM tools. No additional GuardDuty cost — only S3 storage cost (~$0.01/month)."
                ),
                "saving_mo":   0.0,
                "saving_yr":   0.0,
            })

        # ── Opp 4: High CloudTrail cost
        if ct_mo >= CLOUDTRAIL_HIGH_THRESHOLD_MO:
            opps.append({
                "id":          "guardduty-cloudtrail-high-cost",
                "service":     "GuardDuty",
                "severity":    MEDIUM,
                "title":       f"CloudTrail analysis cost high: ~${ct_mo:.2f}/month",
                "description": (
                    f"GuardDuty CloudTrail analysis costs an estimated ${ct_mo:.2f}/month "
                    f"(MTD: ${mtd.get('CLOUD_TRAIL',0):.2f} over {det['mtd_days']} days). "
                    "This reflects the volume of management API events in the account. "
                    "Actions to investigate: (1) check for Lambda/ECS tasks making API calls "
                    "in tight loops, (2) review if CloudTrail Insights are enabled and generating "
                    "additional events, (3) ensure no unnecessary cross-account calls are logged. "
                    "Note: GuardDuty CloudTrail cannot be scoped — all management events are analysed."
                ),
                "saving_mo":   0.0,  # informational — no safe direct optimisation
                "saving_yr":   0.0,
            })

        # ── Opp 5: EKS audit logs disabled (security gap)
        if features.get("EKS_AUDIT_LOGS") == "DISABLED":
            opps.append({
                "id":          "guardduty-eks-audit-logs-disabled",
                "service":     "GuardDuty",
                "severity":    MEDIUM,
                "title":       "EKS audit log monitoring disabled — EKS threat detection gap",
                "description": (
                    f"Detector {did} has EKS_AUDIT_LOGS disabled. If this account runs EKS, "
                    "Kubernetes API server audit logs are not being monitored for threats "
                    "(container escapes, privilege escalation, suspicious kubectl). "
                    "🔗 Enabling adds cost ($1.15/1M audit log events) — evaluate EKS audit "
                    "log volume before enabling."
                ),
                "saving_mo":   0.0,
                "saving_yr":   0.0,
            })

        # ── Opp 6: ECS Runtime Monitoring disabled
        ecs_fm = det.get("runtime_addons", {}).get("ECS_FARGATE_AGENT_MANAGEMENT", "DISABLED")
        if ecs_fm == "DISABLED":
            opps.append({
                "id":          "guardduty-ecs-runtime-monitoring-disabled",
                "service":     "GuardDuty",
                "severity":    LOW,
                "title":       "Runtime Monitoring disabled for ECS Fargate (process-level detection gap)",
                "description": (
                    "GuardDuty Runtime Monitoring ECS_FARGATE_AGENT_MANAGEMENT is disabled. "
                    "Runtime Monitoring detects threats inside running containers (reverse shells, "
                    "cryptomining, container escapes). "
                    "🔗 Enabling deploys a sidecar agent to Fargate tasks and adds per-vCPU cost. "
                    "Evaluate cost vs security coverage before enabling in a large Fargate fleet."
                ),
                "saving_mo":   0.0,
                "saving_yr":   0.0,
            })

        # ── Opp 7: No custom threat intel
        if not det["threat_intel_sets"] and not det["ip_sets"]:
            opps.append({
                "id":          "guardduty-no-threat-intel",
                "service":     "GuardDuty",
                "severity":    LOW,
                "title":       "No custom threat intelligence sets or trusted IP lists",
                "description": (
                    "No custom IP sets or threat intelligence sets configured. "
                    "Adding a trusted IP list (e.g. corporate VPN, dev machine IPs) would suppress "
                    "false-positive findings from known-good sources. "
                    "Adding custom threat intel sets improves detection of account-specific threat actors."
                ),
                "saving_mo":   0.0,
                "saving_yr":   0.0,
            })

    return opps

# ── CSV output ────────────────────────────────────────────────────────────────
def write_csv(opps):
    import csv
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    ts   = now.strftime("%Y%m%d_%H%M%S")
    path = os.path.join(OUTPUT_DIR, f"{ts}_guardduty_report.csv")
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "id", "service", "severity", "title", "saving_mo", "saving_yr", "saving_basis", "price_basis", "description"
        ])
        writer.writeheader()
        for o in opps:
            row = {k: o.get(k, "") for k in writer.fieldnames}
            row["saving_basis"] = o.get("saving_basis", "actual")
            row["price_basis"] = "list"
            writer.writerow(row)
    return path

# ── Summary printer ───────────────────────────────────────────────────────────
def print_summary(data, opps):
    for det in data["detectors"]:
        usage_mo = det["usage_est_monthly_usd"]
        mtd      = det["usage_mtd_usd"]
        total_mo = sum(usage_mo.values())
        total_mtd= sum(mtd.values())
        findings = det["findings"]
        sev_counts = {}
        for f in findings:
            s = f["severity"]
            sev_counts[s] = sev_counts.get(s, 0) + 1

        type_counts = {}
        for f in findings:
            type_counts[f["type"]] = type_counts.get(f["type"], 0) + 1

        print("\n" + "="*70)
        print("  AWS GuardDuty — Cost Analysis Summary")
        print("="*70)
        print(f"  Account  : {ACCOUNT_ID}")
        print(f"  Region   : {REGION}")
        print(f"  Detector : {det['detector_id']}")
        print(f"  Status   : {det['status']}  (since {str(det['created_at'])[:10]})")
        print(f"  Publish  : {det['finding_publish_freq']}")
        print()
        print(f"  ── Enabled Features ──")
        for name, status in sorted(det["features"].items()):
            icon = "✅" if status == "ENABLED" else "❌"
            print(f"    {icon} {name}")
        print()
        print(f"  ── Cost (MTD {det['mtd_days']} days → est. full month) ──")
        for feat, amt in sorted(mtd.items(), key=lambda x: -x[1]):
            if amt > 0:
                mo_est = usage_mo.get(feat, 0)
                print(f"    {feat:<30}  ${amt:>8.2f} MTD  →  ${mo_est:>8.2f}/mo est.")
        print(f"    {'TOTAL':<30}  ${total_mtd:>8.2f} MTD  →  ${total_mo:>8.2f}/mo est.")
        print()
        print(f"  ── Findings ──")
        print(f"    Total findings (sev≥4): {len(findings)}")
        for sev in sorted(sev_counts.keys(), reverse=True):
            print(f"    Severity {sev:.1f}: {sev_counts[sev]}")
        if type_counts:
            print(f"    Top types:")
            for t, c in sorted(type_counts.items(), key=lambda x: -x[1])[:5]:
                print(f"      {c:3d}× {t}")
        print()
        print(f"  ── Configuration ──")
        print(f"    Suppression filters  : {len(det['suppression_filters'])} ({'none' if not det['suppression_filters'] else ', '.join(det['suppression_filters'])})")
        print(f"    Publishing dest.     : {len(det['publishing_destinations'])}")
        print(f"    Threat intel sets    : {len(det['threat_intel_sets'])}")
        print(f"    IP sets              : {len(det['ip_sets'])}")
        print()
        print(f"  ── Opportunities ──")
        print(f"    {len(opps)} found  (note: GuardDuty opportunities are primarily security posture,")
        print(f"    not direct cost savings — disabling features is NOT recommended)")
        for o in opps:
            print(f"    [{o['severity']}] {o['title']}")
        print("="*70)

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

    print("🔍 AWS GuardDuty Cost Analyser")
    print(f"   Account : {ACCOUNT_ID}")
    print(f"   Region  : {REGION}")
    print(f"   Profile : {PROFILE}\n")

    session = boto3.Session(profile_name=PROFILE, region_name=REGION)

    if not refresh:
        cached = load_cache(CACHE_FILE)
        if cached:
            print("✅  Loaded GuardDuty data from cache.")
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
    print(f"\n  CSV report: {csv_path}\n")

if __name__ == "__main__":
    main()
