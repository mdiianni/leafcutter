#!/usr/bin/env python3
"""
AWS Cost Analyser — ELB Skill

Key behaviours:
  - All ALBs and NLBs: idle detection (0 target groups or 0 targets)
  - Target health summary per load balancer (unhealthy target detection)
  - CloudWatch request/flow metrics per LB (30 days)
  - Low-traffic LB identification (< 2000 requests/30d)
  - Monthly cost estimate per LB based on hourly rate
  - Parallel data collection for speed
"""

import boto3
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from cache_utils import load_cache, save_cache, CACHE_TTL_DAYS
from box_utils import _bl, BORDER_TOP, BORDER_MID, BORDER_BOT

# ── Configuration ─────────────────────────────────────────────────────────────
REGION      = os.environ.get("AWS_DEFAULT_REGION", "eu-central-1")
ACCOUNT_ID  = boto3.Session().client("sts").get_caller_identity()["Account"]
CACHE_DIR   = os.path.join(os.path.dirname(__file__), "cache", ACCOUNT_ID, REGION)
OUTPUT_DIR  = os.path.join(os.path.dirname(__file__), "output")
CACHE_FILE  = os.path.join(CACHE_DIR, "elb.json")
CW_DAYS     = 30
MAX_TGS_FOR_HEALTH = 3   # cap health checks per LB to keep survey fast

now = datetime.now(timezone.utc)

# ── Severity ──────────────────────────────────────────────────────────────────
CRITICAL = "🔴 CRITICAL"
HIGH     = "🟠 HIGH"
MEDIUM   = "🟡 MEDIUM"
LOW      = "🟢 LOW"
DEP      = "⚠️  DEPENDENCY"
INFO     = "ℹ️  INFO"

# ── ELB pricing (eu-central-1) ────────────────────────────────────────────────
# Source: https://aws.amazon.com/elasticloadbalancing/pricing/
NLB_HR  = 0.0075   # per hour → $5.475/month base
ALB_HR  = 0.008    # per hour → $5.840/month base
HOURS_MO = 730

NLB_BASE_MO = round(NLB_HR * HOURS_MO, 2)   # 5.475
ALB_BASE_MO = round(ALB_HR * HOURS_MO, 2)   # 5.84

# ── Cache helpers ─────────────────────────────────────────────────────────────
# ── CloudWatch metric helper ──────────────────────────────────────────────────
def get_lb_requests(cw, lb_type, lb_dim_value, days=CW_DAYS):
    """
    Fetch total requests/flows for a load balancer over the last N days.
    ALB uses RequestCount; NLB uses NewFlowCount.
    lb_dim_value is the last 3 segments of the ARN: app/name/id or net/name/id.
    """
    if lb_type == "application":
        namespace   = "AWS/ApplicationELB"
        metric_name = "RequestCount"
        dim_name    = "LoadBalancer"
    else:
        namespace   = "AWS/NetworkELB"
        metric_name = "NewFlowCount"
        dim_name    = "LoadBalancer"
    try:
        r = cw.get_metric_statistics(
            Namespace=namespace,
            MetricName=metric_name,
            Dimensions=[{"Name": dim_name, "Value": lb_dim_value}],
            StartTime=now - timedelta(days=days),
            EndTime=now,
            Period=86400,
            Statistics=["Sum"],
        )
        pts = r.get("Datapoints", [])
        return int(sum(p["Sum"] for p in pts))
    except Exception:
        return 0

# ── Data collection ───────────────────────────────────────────────────────────
def collect(session):
    cached = load_cache(CACHE_FILE)
    if cached:
        print("  ✓ Using cached ELB data")
        return cached

    elbv2 = session.client("elbv2",      region_name=REGION)
    cw    = session.client("cloudwatch", region_name=REGION)

    print("  → Listing all load balancers...")
    lbs_raw = []
    for page in elbv2.get_paginator("describe_load_balancers").paginate():
        lbs_raw.extend(page["LoadBalancers"])
    print(f"  → {len(lbs_raw)} load balancer(s) found.")

    # Pre-fetch all target groups in one call (more efficient)
    print("  → Fetching all target groups...")
    tgs_by_lb = {}
    for page in elbv2.get_paginator("describe_target_groups").paginate():
        for tg in page["TargetGroups"]:
            for lb_arn in tg.get("LoadBalancerArns", []):
                tgs_by_lb.setdefault(lb_arn, []).append(tg)

    def enrich_lb(lb):
        lb_arn    = lb["LoadBalancerArn"]
        lb_name   = lb["LoadBalancerName"]
        lb_type   = lb["Type"]   # "application" or "network"
        scheme    = lb["Scheme"]  # "internet-facing" or "internal"

        # ARN suffix for CloudWatch dimension: last 3 segments
        # arn:aws:elasticloadbalancing:region:acct:loadbalancer/app/name/id
        arn_parts    = lb_arn.split(":")
        lb_dim_value = "/".join(arn_parts[-1].split("/")[-3:]) if arn_parts else lb_name

        base_mo = ALB_BASE_MO if lb_type == "application" else NLB_BASE_MO

        tgs = tgs_by_lb.get(lb_arn, [])
        tg_count = len(tgs)

        # Health per TG (capped)
        healthy_count   = 0
        unhealthy_count = 0
        total_targets   = 0
        for tg in tgs[:MAX_TGS_FOR_HEALTH]:
            try:
                health = elbv2.describe_target_health(TargetGroupArn=tg["TargetGroupArn"])
                for th in health["TargetHealthDescriptions"]:
                    total_targets += 1
                    state = th["TargetHealth"]["State"]
                    if state == "healthy":
                        healthy_count += 1
                    else:
                        unhealthy_count += 1
            except Exception:
                pass

        # CloudWatch traffic metric
        requests_30d = get_lb_requests(cw, lb_type, lb_dim_value)

        is_idle = (tg_count == 0 or total_targets == 0) and requests_30d == 0

        return {
            "name":             lb_name,
            "arn":              lb_arn,
            "type":             lb_type,
            "scheme":           scheme,
            "state":            lb.get("State", {}).get("Code", "unknown"),
            "tg_count":         tg_count,
            "total_targets":    total_targets,
            "healthy_count":    healthy_count,
            "unhealthy_count":  unhealthy_count,
            "requests_30d":     requests_30d,
            "is_idle":          is_idle,
            "monthly_cost":     base_mo,
            "lb_dim_value":     lb_dim_value,
        }

    print(f"  → Enriching {len(lbs_raw)} load balancer(s) (TGs, health, metrics, parallel)...")
    lbs_enriched = []
    with ThreadPoolExecutor(max_workers=10) as ex:
        futures = {ex.submit(enrich_lb, lb): lb["LoadBalancerName"] for lb in lbs_raw}
        for i, future in enumerate(as_completed(futures)):
            lbs_enriched.append(future.result())
            sys.stdout.write(f"\r  → {i+1}/{len(lbs_raw)} load balancers enriched...")
            sys.stdout.flush()
    print(f"\r  ✓ All {len(lbs_enriched)} load balancer(s) enriched.          ")

    data = {"load_balancers": lbs_enriched}
    save_cache(CACHE_FILE, data)
    return data


# ── Analysis ──────────────────────────────────────────────────────────────────
def analyse(data):
    lbs  = data["load_balancers"]
    opps = []

    LOW_TRAFFIC_THRESHOLD = 2000  # requests/30d ≈ < 1 req/min on average

    for lb in lbs:
        name     = lb["name"]
        cost_mo  = lb["monthly_cost"]
        lb_type  = lb["type"]
        scheme   = lb["scheme"]

        # ── Idle load balancer ────────────────────────────────────────────────
        if lb["is_idle"]:
            opps.append({
                "resource":    name,
                "type":        "elb-idle-load-balancer",
                "severity":    HIGH,
                "current_mo":  cost_mo,
                "saving_mo":   cost_mo,
                "dep_warning": "",
                "recommendation": (
                    f"Load balancer '{name}' ({lb_type.upper()}, {scheme}) has "
                    f"{lb['tg_count']} target group(s) and {lb['total_targets']} registered target(s) "
                    f"with 0 traffic in the last {CW_DAYS} days. "
                    f"This LB costs ${cost_mo:.2f}/month while serving no traffic. "
                    f"Confirm it is no longer needed and delete it to eliminate this waste."
                ),
            })
            continue  # no further analysis on idle LBs

        # ── Unhealthy targets (reliability concern) ───────────────────────────
        if lb["unhealthy_count"] > 0 and lb["total_targets"] > 0:
            unhealthy_pct = lb["unhealthy_count"] / lb["total_targets"] * 100
            if unhealthy_pct >= 30:
                severity = HIGH if unhealthy_pct >= 50 else MEDIUM
                opps.append({
                    "resource":    name,
                    "type":        "elb-unhealthy-targets",
                    "severity":    severity,
                    "current_mo":  cost_mo,
                    "saving_mo":   0.0,
                    "dep_warning": "",
                    "recommendation": (
                        f"Load balancer '{name}' ({lb_type.upper()}, {scheme}) has "
                        f"{lb['unhealthy_count']}/{lb['total_targets']} targets unhealthy "
                        f"({unhealthy_pct:.0f}%). "
                        f"With {lb['requests_30d']:,} requests over {CW_DAYS} days, "
                        f"this is an active LB routing traffic to mostly-down targets. "
                        f"Investigate target health: check ECS task stop reasons, "
                        f"security group rules, and health check configuration."
                    ),
                })

        # ── Low-traffic LB ────────────────────────────────────────────────────
        if lb["requests_30d"] < LOW_TRAFFIC_THRESHOLD and lb["requests_30d"] > 0:
            opps.append({
                "resource":    name,
                "type":        "elb-low-traffic",
                "severity":    LOW,
                "current_mo":  cost_mo,
                "saving_mo":   0.0,   # saving depends on consolidation
                "dep_warning": "",
                "recommendation": (
                    f"Load balancer '{name}' ({lb_type.upper()}, {scheme}) handled only "
                    f"{lb['requests_30d']:,} requests in {CW_DAYS} days "
                    f"(< {LOW_TRAFFIC_THRESHOLD:,} requests/month, < 1 req/min average). "
                    f"In a dev environment this may be consistently idle. "
                    f"Evaluate whether this LB can be consolidated with another "
                    f"or removed to save ${cost_mo:.2f}/month."
                ),
            })

    return opps


# ── Reporting ─────────────────────────────────────────────────────────────────
def print_report(data, opps):
    lbs = data["load_balancers"]

    albs         = [lb for lb in lbs if lb["type"] == "application"]
    nlbs         = [lb for lb in lbs if lb["type"] == "network"]
    idle_lbs     = [lb for lb in lbs if lb["is_idle"]]
    low_traffic  = [lb for lb in lbs if 0 < lb["requests_30d"] < 2000 and not lb["is_idle"]]
    total_mo     = sum(lb["monthly_cost"] for lb in lbs)
    idle_saving  = sum(lb["monthly_cost"] for lb in idle_lbs)
    total_saving = sum(o["saving_mo"] for o in opps)

    print()
    print(BORDER_TOP)
    print(_bl("           AWS Cost Analyser — ELB Analysis Results"))
    print(BORDER_MID)
    print(_bl(f"  Account    : {ACCOUNT_ID}"))
    print(_bl(f"  Region     : {REGION}"))
    print(_bl(f"  Run at     : {now.strftime('%Y-%m-%d %H:%M UTC')}"))
    print(_bl(f"  ALBs       : {len(albs)}"))
    print(_bl(f"  NLBs       : {len(nlbs)}"))
    print(_bl(f"  Total LBs  : {len(lbs)}"))
    print(_bl(f"  Idle LBs   : {len(idle_lbs)}"))
    print(BORDER_MID)
    print(_bl(f"  💸 Total ELB base cost               : ${total_mo:>8.2f} / month"))
    print(_bl(f"  💰 Idle LB saving (delete idle)      : ${idle_saving:>8.2f} / month"))
    print(_bl(f"  💰 Total savings potential           : ${total_saving:>8.2f} / month"))
    print(_bl(f"  💰 Annualised savings potential      : ${total_saving*12:>8.2f} / year"))
    print(_bl(f"  🔍 Opportunities found               : {len(opps)}"))
    print(BORDER_BOT)

    # Full LB inventory table
    print(f"\n📋 Load Balancer Inventory:")
    print(f"  {'Name':<45} {'Type':>3}  {'Scheme':>14}  {'TGs':>3}  "
          f"{'Tgts':>4}  {'Hlthy':>5}  {'Requests/30d':>12}  {'$/mo':>7}")
    print(f"  {'-'*45} {'-'*3}  {'-'*14}  {'-'*3}  "
          f"{'-'*4}  {'-'*5}  {'-'*12}  {'-'*7}")
    for lb in sorted(lbs, key=lambda x: (-x["monthly_cost"], x["name"])):
        idle_flag = " ⚠️ IDLE" if lb["is_idle"] else ""
        unhealthy = lb["unhealthy_count"]
        hlthy_str = f"{lb['healthy_count']}/{lb['total_targets']}"
        if unhealthy > 0:
            hlthy_str += "⚠️"
        lb_type_short = "ALB" if lb["type"] == "application" else "NLB"
        print(f"  {lb['name'][:45]:<45} {lb_type_short:>3}  "
              f"{lb['scheme'][:14]:>14}  {lb['tg_count']:>3}  "
              f"{lb['total_targets']:>4}  {hlthy_str:>5}  "
              f"{lb['requests_30d']:>12,}  ${lb['monthly_cost']:>6.2f}{idle_flag}")

    # Idle LB callout
    if idle_lbs:
        print(f"\n⚠️  Idle Load Balancers ({len(idle_lbs)}) — no targets and no traffic:")
        for lb in idle_lbs:
            lb_type_short = "ALB" if lb["type"] == "application" else "NLB"
            print(f"   • [{lb_type_short}] {lb['name']:<45}  {lb['scheme']:<15}  "
                  f"${lb['monthly_cost']:.2f}/month")

    # Low traffic callout
    if low_traffic:
        print(f"\n📉 Low-Traffic Load Balancers ({len(low_traffic)}) — < 2,000 requests/30d:")
        for lb in sorted(low_traffic, key=lambda x: x["requests_30d"]):
            lb_type_short = "ALB" if lb["type"] == "application" else "NLB"
            print(f"   • [{lb_type_short}] {lb['name']:<45}  "
                  f"{lb['requests_30d']:>8,} req  ${lb['monthly_cost']:.2f}/month")

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
        print(f"       Resource : {opp['resource']}")
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

    # CSV output
    ts       = now.strftime("%Y%m%d_%H%M%S")
    csv_path = os.path.join(OUTPUT_DIR, f"{ts}_elb_report.csv")
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(csv_path, "w") as f:
        f.write("account_id,region,resource,opportunity_type,severity,"
                "current_cost_usd_monthly,estimated_saving_usd_monthly,saving_pct,saving_basis,price_basis,"
                "dependency_warning,recommendation\n")
        for o in sorted_opps:
            pct = (o["saving_mo"] / max(o["current_mo"], 0.01)) * 100 if o["current_mo"] > 0 else 0
            saving_basis = "actual"
            sev = (o["severity"]
                   .replace("🔴 ", "").replace("🟠 ", "").replace("🟡 ", "")
                   .replace("🟢 ", "").replace("⚠️  ", "").replace("ℹ️  ", "").strip())
            f.write(f'"{ACCOUNT_ID}","{REGION}","{o["resource"]}",'
                    f'"{o["type"]}","{sev}",{o["current_mo"]:.2f},{o["saving_mo"]:.2f},{pct:.1f},"{saving_basis}","list",'
                    f'"{o["dep_warning"].replace(chr(34),chr(39))}","{o["recommendation"].replace(chr(34),chr(39))}"\n')
    print(f"\n💾 CSV report saved: {csv_path}")


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
    print("\n🔍 AWS ELB Cost Analyser")
    print(f"   Account : {ACCOUNT_ID}")
    print(f"   Region  : {REGION}")
    print(f"   Profile : {os.environ.get('AWS_PROFILE', 'default')}\n")
    session = boto3.Session()
    print("📡 Collecting ELBv2 + CloudWatch data...")
    data    = collect(session)
    lbs     = data["load_balancers"]
    print(f"\n🧠 Analysing {len(lbs)} load balancer(s)...")
    opps    = analyse(data)
    print_report(data, opps)


if __name__ == "__main__":
    main()
