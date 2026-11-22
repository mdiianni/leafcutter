#!/usr/bin/env python3
"""
analyse_xray.py — AWS X-Ray Cost Analysis

Analyses X-Ray tracing costs via:
  - Sampling rule configuration
  - Cost Explorer segment/trace volume (back-calculated from cost)
  - 60-day daily cost trend for spike detection
  - Current trace rate via 1-hour get_trace_summaries window
  - Top services generating traces
"""

import csv
import json
import os
import sys
from datetime import datetime, timezone, timedelta

import boto3

sys.path.insert(0, os.path.dirname(__file__))
from cache_utils import load_cache, save_cache
from box_utils import _bl, BORDER_TOP, BORDER_MID, BORDER_BOT

# ── constants ─────────────────────────────────────────────────────────────────

REGION     = os.environ.get("AWS_DEFAULT_REGION", "eu-central-1")
SESSION    = boto3.Session()
ACCOUNT    = SESSION.client("sts").get_caller_identity()["Account"]
CACHE_DIR  = os.path.join(os.path.dirname(__file__), "cache", ACCOUNT, REGION)
CACHE_FILE = os.path.join(CACHE_DIR, "xray.json")
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "output")
os.makedirs(OUTPUT_DIR, exist_ok=True)

# X-Ray pricing (after free tier: 100K recorded, 1M retrieved/month)
PRICE_PER_M_RECORDED = 5.00    # $5 per million segments
PRICE_PER_M_ACCESSED = 0.50    # $0.50 per million retrievals


# ── data collection ────────────────────────────────────────────────────────────

def collect(refresh=False):
    cached = load_cache(CACHE_FILE)
    if cached and not refresh:
        print("  ✅ Using cached X-Ray data")
        return cached

    print("  🔄 Collecting X-Ray data…")
    xray = SESSION.client("xray", region_name=REGION)
    ce   = SESSION.client("ce",   region_name="us-east-1")
    now  = datetime.now(timezone.utc)

    # ── Sampling rules ────────────────────────────────────────────────────────
    rules_resp = xray.get_sampling_rules()
    sampling_rules = [r["SamplingRule"] for r in rules_resp.get("SamplingRuleRecords", [])]

    default_rate = next(
        (r["FixedRate"] for r in sampling_rules if r["RuleName"] == "Default"), None
    )

    # ── Groups ────────────────────────────────────────────────────────────────
    groups = xray.get_groups().get("Groups", [])

    # ── Encryption ────────────────────────────────────────────────────────────
    enc = xray.get_encryption_config().get("EncryptionConfig", {})

    # ── CE: last full month cost by usage type ────────────────────────────────
    lm_end   = now.replace(day=1)
    lm_start = (lm_end - timedelta(days=1)).replace(day=1)
    lm_s     = lm_start.strftime("%Y-%m-%d")
    lm_e     = lm_end.strftime("%Y-%m-%d")

    ce_lm = ce.get_cost_and_usage(
        TimePeriod={"Start": lm_s, "End": lm_e},
        Granularity="MONTHLY",
        Metrics=["UnblendedCost"],
        Filter={"Dimensions": {"Key": "SERVICE", "Values": ["AWS X-Ray"]}},
        GroupBy=[{"Type": "DIMENSION", "Key": "USAGE_TYPE"}],
    )
    lm_by_type = {
        g["Keys"][0]: float(g["Metrics"]["UnblendedCost"]["Amount"])
        for g in ce_lm["ResultsByTime"][0]["Groups"]
    }
    lm_cost_total = sum(lm_by_type.values())
    cost_stored   = sum(v for k, v in lm_by_type.items() if "Stored" in k or "TracesStored" in k)
    cost_accessed = sum(v for k, v in lm_by_type.items() if "Accessed" in k)

    # ── CE: MTD cost ──────────────────────────────────────────────────────────
    mtd_s = now.replace(day=1).strftime("%Y-%m-%d")
    mtd_e = now.strftime("%Y-%m-%d")
    ce_mtd = ce.get_cost_and_usage(
        TimePeriod={"Start": mtd_s, "End": mtd_e},
        Granularity="MONTHLY",
        Metrics=["UnblendedCost"],
        Filter={"Dimensions": {"Key": "SERVICE", "Values": ["AWS X-Ray"]}},
        GroupBy=[{"Type": "DIMENSION", "Key": "USAGE_TYPE"}],
    )
    mtd_cost = sum(
        float(g["Metrics"]["UnblendedCost"]["Amount"])
        for g in ce_mtd["ResultsByTime"][0]["Groups"]
    )
    days_elapsed = now.day
    mtd_daily_avg = mtd_cost / max(days_elapsed, 1)

    # ── CE: 60-day daily trend ────────────────────────────────────────────────
    trend_start = (now - timedelta(days=60)).strftime("%Y-%m-%d")
    ce_daily = ce.get_cost_and_usage(
        TimePeriod={"Start": trend_start, "End": mtd_e},
        Granularity="DAILY",
        Metrics=["UnblendedCost"],
        Filter={"Dimensions": {"Key": "SERVICE", "Values": ["AWS X-Ray"]}},
    )
    daily_costs = [
        {
            "date": d["TimePeriod"]["Start"],
            "cost": float(d["Total"]["UnblendedCost"]["Amount"]),
        }
        for d in ce_daily["ResultsByTime"]
    ]

    # Spike detection: find days where cost > 3× 7-day rolling average
    spike_days = []
    for i, day in enumerate(daily_costs):
        if i < 7:
            continue
        baseline = sum(d["cost"] for d in daily_costs[max(0, i-14):i]) / min(14, i)
        if baseline > 0 and day["cost"] > baseline * 3 and day["cost"] > 1.0:
            spike_days.append({"date": day["date"], "cost": day["cost"], "baseline": baseline})

    recent_7d_avg = sum(d["cost"] for d in daily_costs[-7:]) / 7 if len(daily_costs) >= 7 else mtd_daily_avg
    spike_resolved = bool(spike_days) and recent_7d_avg < 1.0
    lm_daily_avg   = lm_cost_total / lm_start.replace(month=lm_start.month % 12 + 1,
                                                        day=1).day if lm_cost_total > 0 else 0

    # ── Current trace rate (1-hour sample) ───────────────────────────────────
    traces_per_hour = 0
    top_services: dict = {}
    try:
        ts_resp = xray.get_trace_summaries(
            StartTime=now - timedelta(hours=1),
            EndTime=now,
            Sampling=False,
        )
        traces_per_hour = ts_resp.get("ApproximateCount", len(ts_resp.get("TraceSummaries", [])))
        for t in ts_resp.get("TraceSummaries", []):
            for svc in t.get("ServiceIds", []):
                name = svc.get("Name", "unknown")
                top_services[name] = top_services.get(name, 0) + 1
        # Keep top 10 by count
        top_services = dict(sorted(top_services.items(), key=lambda x: -x[1])[:10])
    except Exception:
        pass

    # Back-calculate segment volume
    segments_recorded_M = cost_stored   / PRICE_PER_M_RECORDED if cost_stored   > 0 else 0
    traces_accessed_M   = cost_accessed / PRICE_PER_M_ACCESSED if cost_accessed > 0 else 0

    data = {
        "account_id":           ACCOUNT,
        "region":               REGION,
        "last_month_period":    f"{lm_s} to {lm_e}",
        "last_month_cost":      round(lm_cost_total, 2),
        "cost_stored":          round(cost_stored, 2),
        "cost_accessed":        round(cost_accessed, 2),
        "segments_recorded_M":  round(segments_recorded_M, 1),
        "traces_accessed_M":    round(traces_accessed_M, 1),
        "mtd_cost":             round(mtd_cost, 4),
        "mtd_daily_avg":        round(mtd_daily_avg, 4),
        "lm_daily_avg":         round(lm_daily_avg, 4),
        "days_elapsed_mtd":     days_elapsed,
        "projected_month_cost": round(mtd_daily_avg * 30, 2),
        "sampling_rules":       sampling_rules,
        "default_sampling_rate": default_rate,
        "groups":               [g["GroupName"] for g in groups],
        "encryption_type":      enc.get("Type", "NONE"),
        "traces_per_hour":      traces_per_hour,
        "top_services":         top_services,
        "daily_costs":          daily_costs,
        "spike_days":           spike_days,
        "spike_resolved":       spike_resolved,
        "recent_7d_daily_avg":  round(recent_7d_avg, 4),
    }

    save_cache(CACHE_FILE, data, account_id=ACCOUNT, region=REGION)
    return data


# ── opportunity generation ─────────────────────────────────────────────────────

def build_opportunities(d):
    opps = []

    lm_cost   = d["last_month_cost"]
    lm_daily  = d["lm_daily_avg"]
    curr_daily = d["mtd_daily_avg"]
    proj_cost  = d["projected_month_cost"]
    rate       = d["default_sampling_rate"]
    seg_M      = d["segments_recorded_M"]
    spike_days = d["spike_days"]
    resolved   = d["spike_resolved"]

    # ── Opp 1: Cost spike detected ────────────────────────────────────────────
    if spike_days:
        peak = max(spike_days, key=lambda x: x["cost"])
        if resolved:
            opps.append({
                "service":    "X-Ray",
                "resource_id": "xray/spike-resolved",
                "opportunity_type": "xray-cost-spike",
                "severity":   "INFO",
                "saving_mo":  0.0,
                "saving_yr":  0.0,
                "current":    round(proj_cost, 2),
                "description": (
                    f"✅ X-Ray cost spike in {peak['date'][:7]} "
                    f"(peak ${peak['cost']:.2f}/day, {peak['cost']/peak['baseline']:.1f}× baseline) "
                    f"has RESOLVED. Current daily average: ${d['recent_7d_daily_avg']:.4f}/day "
                    f"(projected this month: ${proj_cost:.2f}). "
                    f"Review what changed on {peak['date']} to ensure the fix is permanent "
                    f"(check service deployments, sampling rule changes, service removals)."
                ),
            })
        else:
            monthly_excess = (curr_daily - lm_daily) * 30
            opps.append({
                "service":    "X-Ray",
                "resource_id": "xray/spike-active",
                "opportunity_type": "xray-cost-spike",
                "severity":   "HIGH",
                "saving_mo":  round(monthly_excess, 2),
                "saving_yr":  round(monthly_excess * 12, 2),
                "current":    round(proj_cost, 2),
                "description": (
                    f"X-Ray cost spike detected from {peak['date']} "
                    f"({peak['cost']:.1f}× baseline ${peak['baseline']:.2f}/day). "
                    f"Current daily avg ${curr_daily:.2f} remains elevated. "
                    f"Use `xray get-trace-summaries` (1h windows) to identify "
                    f"top-traffic services and apply tighter per-service sampling rules."
                ),
            })

    # ── Opp 2: Sampling rate too high ─────────────────────────────────────────
    if rate is not None and rate > 0.10 and proj_cost > 20:
        target_rate = 0.05
        saving = round(proj_cost * (1 - target_rate / rate), 2)
        sev = "CRITICAL" if rate == 1.0 else "HIGH"
        opps.append({
            "service":    "X-Ray",
            "resource_id": "xray/sampling-rate",
            "opportunity_type": "xray-sampling-rate-high",
            "severity":   sev,
            "saving_mo":  saving,
            "saving_yr":  round(saving * 12, 2),
            "current":    round(proj_cost, 2),
            "description": (
                f"X-Ray Default sampling rate is {rate*100:.0f}%. "
                f"Recommended for production: 5%. "
                f"Reducing to {target_rate*100:.0f}% would save ~${saving:.2f}/month. "
                f"Update via: aws xray update-sampling-rule --sampling-rule-update "
                f"'{{\"RuleName\":\"Default\",\"FixedRate\":{target_rate}}}'"
            ),
        })

    # ── Opp 3: Only default rule, high volume ─────────────────────────────────
    custom_rules = [r for r in d["sampling_rules"] if r["RuleName"] != "Default"]
    if not custom_rules and seg_M > 10 and proj_cost > 20:
        potential_saving = round(proj_cost * 0.30, 2)
        opps.append({
            "service":    "X-Ray",
            "resource_id": "xray/sampling-rules",
            "opportunity_type": "xray-sampling-rules-optimise",
            "severity":   "MEDIUM",
            "saving_mo":  potential_saving,
            "saving_yr":  round(potential_saving * 12, 2),
            "current":    round(proj_cost, 2),
            "description": (
                f"Only the Default sampling rule is configured. "
                f"With {seg_M:.0f}M segments/month, per-service rules could exclude "
                f"health checks, metrics scrapers, and other low-value paths. "
                f"Example: add rule for URL '/health' with FixedRate=0.0 to skip them. "
                f"Estimated saving: ~${potential_saving:.2f}/month (~30%)."
            ),
        })

    # ── Opp 4: KMS encryption adds cost at high volume ────────────────────────
    if d["encryption_type"] == "KMS" and seg_M > 50:
        opps.append({
            "service":    "X-Ray",
            "resource_id": "xray/kms-encryption",
            "opportunity_type": "xray-kms-encryption",
            "severity":   "LOW",
            "saving_mo":  0.0,
            "saving_yr":  0.0,
            "current":    round(proj_cost, 2),
            "description": (
                f"X-Ray uses KMS CMK encryption at {seg_M:.0f}M segments/month. "
                f"Each trace API call triggers a KMS Decrypt, adding KMS API costs. "
                f"Unless CMK is a compliance requirement, switching to AWS-managed "
                f"encryption (Type=NONE) removes the KMS overhead."
            ),
        })

    # ── Opp 5: X-Ray negligible / not actively used ───────────────────────────
    if proj_cost < 1.0 and lm_cost < 10 and not spike_days:
        opps.append({
            "service":    "X-Ray",
            "resource_id": "xray/low-usage",
            "opportunity_type": "xray-not-in-use",
            "severity":   "INFO",
            "saving_mo":  0.0,
            "saving_yr":  0.0,
            "current":    round(proj_cost, 2),
            "description": (
                f"X-Ray cost is negligible (projected ${proj_cost:.2f}/month, "
                f"last month ${lm_cost:.2f}). Service is within free tier or barely "
                f"used. No cost action needed."
            ),
        })

    return opps


# ── output ─────────────────────────────────────────────────────────────────────

def print_summary(d, opps):
    print(f"\n{'='*70}")
    print(f"  AWS X-Ray Cost Analysis — Account {d['account_id']} / {d['region']}")
    print(f"{'='*70}")
    print(f"  Last month ({d['last_month_period']}):")
    print(f"    Total cost:           ${d['last_month_cost']:>8.2f}")
    print(f"    Segments recorded:    {d['segments_recorded_M']:>6.1f}M  (${d['cost_stored']:.2f})")
    print(f"    Traces accessed:      {d['traces_accessed_M']:>6.1f}M  (${d['cost_accessed']:.2f})")
    print(f"  MTD ({d['days_elapsed_mtd']} days):")
    print(f"    MTD cost:             ${d['mtd_cost']:>8.4f}")
    print(f"    Daily avg (MTD):      ${d['mtd_daily_avg']:>8.4f}/day")
    print(f"    Projected this month: ${d['projected_month_cost']:>8.2f}")
    print(f"  Configuration:")
    print(f"    Default sampling rate:{d['default_sampling_rate']*100 if d['default_sampling_rate'] else 0:.0f}%")
    print(f"    Custom rules:         {len([r for r in d['sampling_rules'] if r['RuleName'] != 'Default'])}")
    print(f"    Groups:               {', '.join(d['groups']) or 'Default only'}")
    print(f"    Encryption:           {d['encryption_type']}")
    print(f"  Current activity:")
    print(f"    Traces/hour (sample): {d['traces_per_hour']}")
    print(f"    Recent 7d daily avg:  ${d['recent_7d_daily_avg']:.4f}/day")

    if d["spike_days"]:
        peak = max(d["spike_days"], key=lambda x: x["cost"])
        status = "✅ RESOLVED" if d["spike_resolved"] else "🔴 STILL ACTIVE"
        print(f"  Cost spike:           {status} (peak ${peak['cost']:.2f}/day on {peak['date']})")

    if d["top_services"]:
        print(f"  Top services (1h sample):")
        for svc, count in list(d["top_services"].items())[:5]:
            print(f"    {svc[:60]}: {count} traces")

    total_saving = sum(o["saving_mo"] for o in opps)
    print(f"\n  Opportunities ({len(opps)})  |  Saving: ${total_saving:.2f}/month")
    print(f"  {'─'*66}")
    SEV_ICON = {"CRITICAL":"🔴","HIGH":"🟠","MEDIUM":"🟡","LOW":"🟢","INFO":"ℹ️ "}
    for op in sorted(opps, key=lambda x: (["CRITICAL","HIGH","MEDIUM","LOW","INFO"].index(x["severity"])
                                           if x["severity"] in ["CRITICAL","HIGH","MEDIUM","LOW","INFO"] else 99,
                                           -x["saving_mo"])):
        icon = SEV_ICON.get(op["severity"], "")
        print(f"    [{icon} {op['severity']:<8}] ${op['saving_mo']:>6.2f}/mo  {op['description'][:85]}")
    print(f"{'='*70}\n")


def write_csv(opps):
    ts   = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    path = os.path.join(OUTPUT_DIR, f"{ts}_xray_report.csv")
    fields = ["service","resource_id","opportunity_type","severity",
              "saving_mo","saving_yr","current","saving_basis","price_basis","description"]
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for o in opps:
            row = dict(o)
            row.setdefault("saving_basis", "actual")
            row["price_basis"] = "list"
            w.writerow(row)
    print(f"  💾 CSV report: {path}")


# ── entry point ────────────────────────────────────────────────────────────────

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
    print(f"\n🔭 AWS X-Ray Analysis")
    print(f"   Account: {ACCOUNT} | Region: {REGION}")
    data = collect(refresh=refresh)
    opps = build_opportunities(data)
    print_summary(data, opps)
    write_csv(opps)


if __name__ == "__main__":
    main()
