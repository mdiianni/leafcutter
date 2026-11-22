#!/usr/bin/env python3
"""
AWS Cost Analyser — OpenSearch Skill

Key behaviours:
  - Single domain inventory with cluster config, EBS size, and engine version
  - CRITICAL: 2 dedicated master nodes is unsupported (AWS requires 3 or 0)
  - CloudWatch 14-day metrics: CPU, JVM memory pressure, search rate, indexable docs
  - UltraWarm storage opportunity for cold index data
"""

import boto3
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from cache_utils import load_cache, save_cache, CACHE_TTL_DAYS
from box_utils import _bl, BORDER_TOP, BORDER_MID, BORDER_BOT

# ── Configuration ─────────────────────────────────────────────────────────────
REGION     = os.environ.get("AWS_DEFAULT_REGION", "eu-central-1")
ACCOUNT_ID = boto3.Session().client("sts").get_caller_identity()["Account"]
CACHE_DIR  = os.path.join(os.path.dirname(__file__), "cache", ACCOUNT_ID, REGION)
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "output")
CACHE_FILE = os.path.join(CACHE_DIR, "opensearch.json")
CW_DAYS = 14

now = datetime.now(timezone.utc)

# ── Severity ──────────────────────────────────────────────────────────────────
CRITICAL = "🔴 CRITICAL"
HIGH     = "🟠 HIGH"
MEDIUM   = "🟡 MEDIUM"
LOW      = "🟢 LOW"
DEP      = "⚠️  DEPENDENCY"
INFO     = "ℹ️  INFO"

# ── Pricing (eu-central-1) ────────────────────────────────────────────────────
# Source: https://aws.amazon.com/opensearch-service/pricing/
OS_R7G_MEDIUM_HR  = 0.0698   # r7g.medium.search per hour
OS_T3_SMALL_HR    = 0.0328   # t3.small.search per hour
OS_EBS_GP3_GB_MO  = 0.10     # gp3 EBS per GB per month
OS_ULTRAWARM_GB_MO = 0.024   # UltraWarm storage per GB per month

# On-demand hourly pricing per instance type (eu-central-1, *.search)
INSTANCE_PRICE_HR = {
    "t3.small.search":    0.0328,
    "t3.medium.search":   0.0658,
    "m5.large.search":    0.163,   "m5.xlarge.search":   0.326,
    "m6g.large.search":   0.1385,  "m6g.xlarge.search":  0.277,
    "m7g.large.search":   0.1385,  "m7g.xlarge.search":  0.277,
    # M8g — Graviton4, up to 30% better performance than Graviton3 (GA 2025-10)
    "m8g.large.search":   0.121,   "m8g.xlarge.search":  0.242,
    "r5.large.search":    0.202,   "r5.xlarge.search":   0.404,
    "r6g.large.search":   0.171,   "r6g.xlarge.search":  0.343,
    "r7g.large.search":   0.171,   "r7g.xlarge.search":  0.343,
    "r7g.medium.search":  0.0698,
    # R8g/R8gd — Graviton4, up to 30% better performance than Graviton3 (GA 2025-10)
    "r8g.medium.search":  0.061,   "r8g.large.search":   0.150,   "r8g.xlarge.search": 0.300,
    "c6g.large.search":   0.125,   "c6g.xlarge.search":  0.250,
    "c7g.large.search":   0.125,   "c7g.xlarge.search":  0.250,
    # C8g — Graviton4, up to 30% better performance than Graviton3 (GA 2025-10)
    "c8g.large.search":   0.109,   "c8g.xlarge.search":  0.219,
}

# Graviton upgrade mapping: x86/older-Graviton class → recommended latest Graviton class.
# OpenSearch Graviton4 (C8g/M8g/R8g/R8gd) reached GA in 2025-10 — up to 30% better
# performance than Graviton3, available in 23+ regions.
GRAVITON_MAP = {
    "t3":  None,  # no Graviton4 T-family equivalent yet
    "m5":  "m8g", "m6g": "m8g", "m7g": "m8g",
    "r5":  "r8g", "r6g": "r8g", "r7g": "r8g",
    "c6g": "c8g", "c7g": "c8g",
}


def graviton_target(instance_type):
    """Return the recommended Graviton4 instance type for a given type, or None."""
    # instance_type format: "<family>.<size>.search"
    parts = instance_type.split(".")
    if len(parts) != 3 or parts[2] != "search":
        return None
    family, size = parts[0], parts[1]
    target_family = GRAVITON_MAP.get(family)
    if not target_family:
        return None
    candidate = f"{target_family}.{size}.search"
    return candidate if candidate in INSTANCE_PRICE_HR else None


def is_graviton4(instance_type):
    return instance_type.startswith(("m8g.", "r8g.", "c8g."))

# ── Cache helpers ─────────────────────────────────────────────────────────────
# ── CloudWatch helper ─────────────────────────────────────────────────────────
def get_cw_metric(cw, domain_name, metric_name, stat="Average"):
    """Fetch average of a metric over CW_DAYS for an OpenSearch domain."""
    try:
        r = cw.get_metric_statistics(
            Namespace="AWS/ES",
            MetricName=metric_name,
            Dimensions=[
                {"Name": "DomainName", "Value": domain_name},
                {"Name": "ClientId",   "Value": ACCOUNT_ID},
            ],
            StartTime=now - timedelta(days=CW_DAYS),
            EndTime=now,
            Period=3600,
            Statistics=[stat, "Maximum"],
        )
        pts = r.get("Datapoints", [])
        if not pts:
            return None, None
        vals = [p[stat] for p in pts]
        maxs = [p["Maximum"] for p in pts]
        return round(sum(vals) / len(vals), 2), round(max(maxs), 2)
    except Exception:
        return None, None

# ── Data collection ───────────────────────────────────────────────────────────
def collect(session):
    cached = load_cache(CACHE_FILE)
    if cached:
        print("  ✓ Using cached OpenSearch data")
        return cached

    es = session.client("opensearch", region_name=REGION)
    cw = session.client("cloudwatch", region_name=REGION)

    print("  → Listing OpenSearch domains...")
    domain_names = [d["DomainName"] for d in es.list_domain_names().get("DomainNames", [])]
    print(f"  → {len(domain_names)} domain(s) found.")

    domains = []
    for name in domain_names:
        print(f"  → Describing domain '{name}'...")
        try:
            resp   = es.describe_domain(DomainName=name)
            status = resp["DomainStatus"]
        except Exception as e:
            print(f"  ⚠️  Could not describe domain '{name}': {e}")
            continue

        cluster_cfg  = status.get("ClusterConfig", {})
        ebs_cfg      = status.get("EBSOptions", {})
        uw_cfg       = status.get("WarmEnabled", False)
        engine_ver   = status.get("EngineVersion", "unknown")

        instance_type  = cluster_cfg.get("InstanceType", "unknown")
        instance_count = cluster_cfg.get("InstanceCount", 1)
        master_enabled = cluster_cfg.get("DedicatedMasterEnabled", False)
        master_type    = cluster_cfg.get("DedicatedMasterType", "")
        master_count   = cluster_cfg.get("DedicatedMasterCount", 0) if master_enabled else 0
        ebs_enabled    = ebs_cfg.get("EBSEnabled", False)
        ebs_type       = ebs_cfg.get("VolumeType", "")
        ebs_size_gb    = ebs_cfg.get("VolumeSize", 0)

        # Monthly cost estimate: data nodes + master nodes + EBS
        instance_hr_cost = INSTANCE_PRICE_HR.get(instance_type, OS_R7G_MEDIUM_HR)
        master_hr_cost  = INSTANCE_PRICE_HR.get(master_type, instance_hr_cost)
        monthly_data    = instance_count  * instance_hr_cost * 730
        monthly_master  = master_count    * master_hr_cost * 730
        monthly_ebs     = ebs_size_gb     * OS_EBS_GP3_GB_MO if ebs_enabled else 0.0
        monthly_total   = monthly_data + monthly_master + monthly_ebs

        # CloudWatch metrics
        print(f"  → Fetching CloudWatch metrics for '{name}' (last {CW_DAYS} days)...")
        cpu_avg,        cpu_max        = get_cw_metric(cw, name, "CPUUtilization")
        jvm_avg,        jvm_max        = get_cw_metric(cw, name, "JVMMemoryPressure")
        search_avg,     search_max     = get_cw_metric(cw, name, "SearchRate")
        docs_avg,       docs_max       = get_cw_metric(cw, name, "SearchableDocuments")
        indexing_avg,   indexing_max   = get_cw_metric(cw, name, "IndexingRate")

        domains.append({
            "name":            name,
            "arn":             status.get("ARN", ""),
            "engine_version":  engine_ver,
            "instance_type":   instance_type,
            "instance_count":  instance_count,
            "master_enabled":  master_enabled,
            "master_type":     master_type,
            "master_count":    master_count,
            "ebs_enabled":     ebs_enabled,
            "ebs_type":        ebs_type,
            "ebs_size_gb":     ebs_size_gb,
            "ultrawarm":       uw_cfg,
            "monthly_data":    round(monthly_data,   2),
            "monthly_master":  round(monthly_master, 2),
            "monthly_ebs":     round(monthly_ebs,    2),
            "monthly_total":   round(monthly_total,  2),
            "cpu_avg":         cpu_avg,
            "cpu_max":         cpu_max,
            "jvm_avg":         jvm_avg,
            "jvm_max":         jvm_max,
            "search_avg":      search_avg,
            "search_max":      search_max,
            "docs_avg":        docs_avg,
            "docs_max":        docs_max,
            "indexing_avg":    indexing_avg,
            "indexing_max":    indexing_max,
        })

    data = {"domains": domains}
    save_cache(CACHE_FILE, data)
    print(f"  ✓ {len(domains)} domain(s) collected and cached.")
    return data


# ── Analysis ──────────────────────────────────────────────────────────────────
def analyse(data):
    domains = data["domains"]
    opps    = []

    for d in domains:
        name = d["name"]

        # ── Opp 0: Graviton / Graviton4 migration ─────────────────────────────
        # OpenSearch Graviton4 (C8g/M8g/R8g/R8gd) GA since 2025-10 — up to 30% better
        # performance than Graviton3, safe drop-in change via blue/green deployment.
        if not is_graviton4(d["instance_type"]):
            target = graviton_target(d["instance_type"])
            current_hr = INSTANCE_PRICE_HR.get(d["instance_type"])
            target_hr  = INSTANCE_PRICE_HR.get(target) if target else None
            if target and current_hr is not None and target_hr is not None:
                current_mo = d["instance_count"] * current_hr * 730
                target_mo  = d["instance_count"] * target_hr * 730
                saving_mo  = round(current_mo - target_mo, 2)
                if saving_mo > 0:
                    pct = round(saving_mo / current_mo * 100, 1)
                    opps.append({
                        "resource":    name,
                        "type":        "opensearch-graviton-migration",
                        "severity":    MEDIUM,
                        "current_mo":  round(current_mo, 2),
                        "saving_mo":   saving_mo,
                        "dep_warning": "",
                        "recommendation": (
                            f"Domain '{name}' runs {d['instance_count']}×{d['instance_type']} "
                            f"data node(s). Migrating to {target} (Graviton4, up to 30% better "
                            f"performance than Graviton3) saves ~${saving_mo:.2f}/month ({pct:.0f}%) "
                            f"— ${current_mo:.2f}/mo → ${target_mo:.2f}/mo. "
                            f"Apply via a blue/green deployment (no data loss, brief cutover)."
                        ),
                    })

        # ── Opp 1: Invalid dedicated master count ─────────────────────────────
        # AWS requires 3 or 5 dedicated master nodes (or none). 2 cannot achieve
        # quorum: if one master fails the cluster loses the master majority.
        if d["master_enabled"] and d["master_count"] not in (0, 3, 5):
            cost_to_fix = 1 * OS_R7G_MEDIUM_HR * 730  # +1 node to reach 3
            opps.append({
                "resource":    name,
                "type":        "opensearch-dedicated-master-count",
                "severity":    CRITICAL,
                "current_mo":  d["monthly_total"],
                "saving_mo":   0.0,
                "dep_warning": (
                    "This is a reliability fix, not a cost optimisation. Adding a third master "
                    "increases cost by ~${:.0f}/month. Alternatively, disable dedicated masters "
                    "entirely (suitable for small single-node domains).".format(cost_to_fix)
                ),
                "recommendation": (
                    f"Domain '{name}' has {d['master_count']} dedicated master node(s) "
                    f"({d['master_type']}). AWS OpenSearch requires 3 or 5 dedicated masters "
                    f"to maintain quorum — 2 masters cannot elect a new leader if one fails, "
                    f"leaving the cluster in a split-brain state. "
                    f"Fix immediately: either set DedicatedMasterCount=3 "
                    f"(+${cost_to_fix:.0f}/month) or disable dedicated masters. "
                    f"Current cluster: {d['instance_count']}×{d['instance_type']} data node(s)."
                ),
            })

        # ── Opp 2: Low CPU utilisation (informational right-sizing signal) ────
        if d["cpu_avg"] is not None and d["cpu_avg"] < 15.0:
            opps.append({
                "resource":    name,
                "type":        "opensearch-overprovisioned-cpu",
                "severity":    MEDIUM,
                "current_mo":  d["monthly_total"],
                "saving_mo":   0.0,
                "dep_warning": (
                    "Downsizing an OpenSearch instance type requires a blue/green deployment "
                    "(AWS-managed but causes brief performance impact). "
                    "Validate peak load and search latency SLAs before resizing."
                ),
                "recommendation": (
                    f"Domain '{name}': avg CPU {d['cpu_avg']:.1f}%, max {d['cpu_max']:.1f}% "
                    f"over {CW_DAYS} days on {d['instance_count']}×{d['instance_type']}. "
                    f"Search rate avg {d['search_avg']:.1f}/s, max {d['search_max']:.1f}/s. "
                    f"At this utilisation the domain is lightly used. "
                    f"If search rate consistently stays below 50/s, monitor for 30 days "
                    f"and consider whether the current instance type is appropriately sized. "
                    f"Savings are informational ($0 captured here) pending load validation."
                ),
            })

        # ── Opp 3: No UltraWarm for cold index data ───────────────────────────
        if not d["ultrawarm"] and d["ebs_size_gb"] > 0:
            uw_saving_est = 0.0  # not applicable at 60 GB scale; flag for future
            opps.append({
                "resource":    name,
                "type":        "opensearch-no-ultrawarm",
                "severity":    LOW,
                "current_mo":  d["monthly_total"],
                "saving_mo":   uw_saving_est,
                "dep_warning": "",
                "recommendation": (
                    f"Domain '{name}' has {d['ebs_size_gb']} GB EBS ({d['ebs_type']}) and "
                    f"UltraWarm is not enabled. UltraWarm storage costs "
                    f"${OS_ULTRAWARM_GB_MO}/GB-month vs ${OS_EBS_GP3_GB_MO}/GB-month for gp3, "
                    f"a {int((1 - OS_ULTRAWARM_GB_MO/OS_EBS_GP3_GB_MO)*100)}% saving on storage "
                    f"for infrequently queried (cold) indices. "
                    f"At {d['ebs_size_gb']} GB the absolute saving is small "
                    f"(~${d['ebs_size_gb'] * (OS_EBS_GP3_GB_MO - OS_ULTRAWARM_GB_MO):.2f}/month) "
                    f"but the pattern should be revisited if index volume grows beyond 200 GB. "
                    f"Current searchable docs: {int(d['docs_avg'] or 0):,}."
                ),
            })

    return opps


# ── Reporting ─────────────────────────────────────────────────────────────────
def print_report(data, opps):
    domains     = data["domains"]
    total_mo    = sum(d["monthly_total"] for d in domains)
    total_saving = sum(o["saving_mo"] for o in opps)
    resources_evaluated = len(domains)
    no_findings_desc = (
        f"No resources found (0 OpenSearch domains in {REGION})."
        if resources_evaluated == 0
        else f"Evaluated {resources_evaluated} OpenSearch domain(s). No cost-saving opportunities found."
    )

    print()
    print(BORDER_TOP)
    print(_bl("        AWS Cost Analyser — OpenSearch Analysis Results"))
    print(BORDER_MID)
    print(_bl(f"  Account : {ACCOUNT_ID}"))
    print(_bl(f"  Region  : {REGION}"))
    print(_bl(f"  Run at  : {now.strftime('%Y-%m-%d %H:%M UTC')}"))
    print(_bl(f"  Domains : {len(domains)}"))
    print(BORDER_MID)
    print(_bl(f"  💸 Est. OpenSearch cost (all domains): ${total_mo:>8.2f} / month"))
    print(_bl(f"  💰 Savings potential (excl. CRITICAL): ${total_saving:>8.2f} / month"))
    print(_bl(f"  💰 Annualised savings potential      : ${total_saving*12:>8.2f} / year"))
    print(_bl(f"  🔍 Opportunities found               : {len(opps)}"))
    print(BORDER_BOT)
    if not opps:
        print(f"\n✅ {no_findings_desc}")

    # Domain summary table
    print(f"\n📦 Domain Summary:")
    print(f"  {'Domain':<30} {'Engine':<12} {'Data Nodes':<20} {'Masters':<24} "
          f"{'EBS':>8} {'$/mo':>9}")
    print(f"  {'-'*30} {'-'*12} {'-'*20} {'-'*24} {'-'*8} {'-'*9}")
    for d in domains:
        master_str = (f"{d['master_count']}×{d['master_type'].split('.')[0]}"
                      if d["master_enabled"] else "none")
        master_warn = " ⚠️" if d["master_enabled"] and d["master_count"] not in (0, 3, 5) else ""
        print(f"  {d['name'][:30]:<30} {d['engine_version']:<12} "
              f"{d['instance_count']}×{d['instance_type']:<18} "
              f"{master_str + master_warn:<24} "
              f"{d['ebs_size_gb']:>5} GB  ${d['monthly_total']:>8.2f}")

    # Metrics table
    print(f"\n📊 CloudWatch Metrics (last {CW_DAYS} days):")
    print(f"  {'Domain':<30} {'CPU avg':>8} {'CPU max':>8} {'JVM avg':>8} {'JVM max':>8} "
          f"{'Search/s':>9} {'Docs':>12}")
    print(f"  {'-'*30} {'-'*8} {'-'*8} {'-'*8} {'-'*8} {'-'*9} {'-'*12}")
    for d in domains:
        cpu_a  = f"{d['cpu_avg']:.1f}%"   if d["cpu_avg"]  is not None else "  N/A"
        cpu_m  = f"{d['cpu_max']:.1f}%"   if d["cpu_max"]  is not None else "  N/A"
        jvm_a  = f"{d['jvm_avg']:.1f}%"   if d["jvm_avg"]  is not None else "  N/A"
        jvm_m  = f"{d['jvm_max']:.1f}%"   if d["jvm_max"]  is not None else "  N/A"
        srch   = f"{d['search_avg']:.1f}"  if d["search_avg"] is not None else "  N/A"
        docs   = f"{int(d['docs_avg']):,}" if d["docs_avg"]  is not None else "  N/A"
        print(f"  {d['name'][:30]:<30} {cpu_a:>8} {cpu_m:>8} {jvm_a:>8} {jvm_m:>8} "
              f"{srch:>9} {docs:>12}")

    # Cost breakdown per domain
    print(f"\n💰 Cost Breakdown:")
    print(f"  {'Domain':<30} {'Data nodes':>12} {'Masters':>10} {'EBS':>8} {'Total':>10}")
    print(f"  {'-'*30} {'-'*12} {'-'*10} {'-'*8} {'-'*10}")
    for d in domains:
        print(f"  {d['name'][:30]:<30} ${d['monthly_data']:>10.2f} "
              f"${d['monthly_master']:>8.2f} ${d['monthly_ebs']:>6.2f} ${d['monthly_total']:>9.2f}")

    # Opportunities by type
    type_summary = {}
    for o in opps:
        t = o["type"]
        if t not in type_summary:
            type_summary[t] = {"count": 0, "saving": 0.0}
        type_summary[t]["count"]  += 1
        type_summary[t]["saving"] += o["saving_mo"]
    print("\n📊 Opportunities by Type:")
    print(f"  {'Type':<45} {'Count':>5}  {'Saving/mo':>12}  {'Saving/yr':>12}")
    print(f"  {'-'*45} {'-'*5}  {'-'*12}  {'-'*12}")
    for t, v in sorted(type_summary.items(), key=lambda x: -x[1]["saving"]):
        print(f"  {t:<45} {v['count']:>5}  ${v['saving']:>10.2f}  ${v['saving']*12:>10.2f}")

    sev_counts = {}
    for o in opps:
        sev_counts[o["severity"]] = sev_counts.get(o["severity"], 0) + 1
    print(f"\n🎯 Severity: " + "  ".join(f"{s}: {c}" for s, c in sev_counts.items()))

    # Detailed findings
    sorted_opps = sorted(opps, key=lambda x: (
        0 if x["severity"] == CRITICAL else
        1 if x["severity"] == HIGH else
        2 if x["severity"] == MEDIUM else 3,
        -x["saving_mo"]
    ))
    print("\n" + "─" * 72)
    print("📋 Detailed Findings (CRITICAL first, then by saving):\n")
    for i, opp in enumerate(sorted_opps, 1):
        print(f"  [{i:02d}] {opp['severity']}  —  {opp['type']}")
        print(f"       Resource : {opp['resource']}")
        if opp["saving_mo"] > 0:
            print(f"       Saving   : ${opp['saving_mo']:.2f}/month  (${opp['saving_mo']*12:.2f}/year)")
            print(f"       Current  : ${opp['current_mo']:.2f}/month")
        if opp["dep_warning"]:
            dw = opp["dep_warning"]
            print(f"       ⚠️  Note  : {dw[:68]}")
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
    csv_path = os.path.join(OUTPUT_DIR, f"{ts}_opensearch_report.csv")
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(csv_path, "w") as f:
        f.write("account_id,region,resource,opportunity_type,severity,"
                "current_cost_usd_monthly,estimated_saving_usd_monthly,saving_pct,saving_basis,price_basis,"
                "dependency_warning,recommendation\n")
        if not sorted_opps:
            f.write(f'"{ACCOUNT_ID}","{REGION}","","no-findings","INFO",0.00,0.00,0.0,"actual","list","","{no_findings_desc}"\n')
        else:
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
    print("\n🔍 AWS OpenSearch Cost Analyser")
    print(f"   Account : {ACCOUNT_ID}")
    print(f"   Region  : {REGION}")
    print(f"   Profile : {os.environ.get('AWS_PROFILE', 'default')}\n")
    session = boto3.Session()
    print("📡 Collecting OpenSearch + CloudWatch data...")
    data = collect(session)
    print(f"\n🧠 Analysing {len(data['domains'])} domain(s)...")
    opps = analyse(data)
    print_report(data, opps)


if __name__ == "__main__":
    main()
