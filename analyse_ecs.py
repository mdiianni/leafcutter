#!/usr/bin/env python3
"""
AWS Cost Analyser — ECS Skill
Implements analysis from skills/ecs.md (with real-world learnings applied)

Key behaviours:
  - Container Insights CloudWatch metrics for CPU + MEM per service (last 15 days)
  - Fargate Graviton migration savings (ARM64 vs X86_64) with dependency warning
  - Overprovision detection for both CPU and Memory
  - Auto-scaling gap detection (min==max → can't scale in)
  - Zombie service detection (desired > 0, running == 0)
  - Org-level Savings Plans reused from CE (Fargate covered by Compute SP)
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
CACHE_FILE  = os.path.join(CACHE_DIR, "ecs.json")
CW_DAYS     = 15

now = datetime.now(timezone.utc)

# ── Severity ──────────────────────────────────────────────────────────────────
CRITICAL = "🔴 CRITICAL"
HIGH     = "🟠 HIGH"
MEDIUM   = "🟡 MEDIUM"
LOW      = "🟢 LOW"
DEP      = "⚠️  DEPENDENCY"
INFO     = "ℹ️  INFO"

# ── Fargate pricing: eu-central-1 (Frankfurt) ─────────────────────────────────
# Source: https://aws.amazon.com/fargate/pricing/
FARGATE = {
    "X86_64": {
        "vcpu_hr":  0.04868,   # per vCPU per hour
        "mem_gb_hr": 0.00532,  # per GB per hour
    },
    "ARM64": {
        "vcpu_hr":  0.03874,   # per vCPU per hour  (~20% less)
        "mem_gb_hr": 0.00423,  # per GB per hour    (~20% less)
    },
}

# ── Valid Fargate CPU/memory combinations ─────────────────────────────────────
# CPU units → list of valid memory values (MB)
FARGATE_VALID = {
    256:  [512, 1024, 2048],
    512:  list(range(1024, 4097,  1024)),
    1024: list(range(2048, 8193,  1024)),
    2048: list(range(4096, 16385, 1024)),
    4096: list(range(8192, 30721, 1024)),
    8192: list(range(16384,61441, 4096)),
    16384:list(range(32768,122881,8192)),
}

def next_smaller_fargate(cpu_units, mem_mb):
    """Return (new_cpu_units, new_mem_mb) one step smaller, or None if at minimum."""
    valid_cpus = sorted(FARGATE_VALID.keys())
    idx = valid_cpus.index(cpu_units) if cpu_units in valid_cpus else -1
    if idx <= 0:
        return None
    new_cpu = valid_cpus[idx - 1]
    valid_mems = FARGATE_VALID[new_cpu]
    # pick the smallest mem that still fits — here we return the max of the smaller tier
    new_mem = max(m for m in valid_mems if m <= mem_mb) if any(m <= mem_mb for m in valid_mems) else min(valid_mems)
    return new_cpu, new_mem

def fargate_monthly(cpu_units, mem_mb, arch, task_count=1):
    p    = FARGATE[arch]
    vcpu = cpu_units / 1024
    gb   = mem_mb   / 1024
    return (vcpu * p["vcpu_hr"] + gb * p["mem_gb_hr"]) * 730 * task_count

# ── Cache helpers ─────────────────────────────────────────────────────────────
# ── CloudWatch helpers ────────────────────────────────────────────────────────
def get_cw_metric(cw, cluster_name, service_name, metric_name, days=CW_DAYS):
    """Fetch avg/max/p95 for a Container Insights ECS metric."""
    try:
        r = cw.get_metric_statistics(
            Namespace="AWS/ECS",
            MetricName=metric_name,
            Dimensions=[
                {"Name": "ClusterName", "Value": cluster_name},
                {"Name": "ServiceName", "Value": service_name},
            ],
            StartTime=now - timedelta(days=days),
            EndTime=now,
            Period=3600,
            Statistics=["Average", "Maximum"],
        )
        pts = r.get("Datapoints", [])
        if not pts:
            return None, None, None
        avgs = [p["Average"] for p in pts]
        maxs = [p["Maximum"] for p in pts]
        p95  = sorted(avgs)[min(int(len(avgs) * 0.95), len(avgs) - 1)]
        return round(sum(avgs) / len(avgs), 2), round(max(maxs), 2), round(p95, 2)
    except Exception:
        return None, None, None

# ── CE: reuse org SP data (Fargate is covered by Compute Savings Plans) ───────
def collect_sp_data(session):
    ce = session.client("ce", region_name="us-east-1")
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).strftime("%Y-%m-%d")
    today       = now.strftime("%Y-%m-%d")
    days_elapsed= (now - now.replace(day=1)).days + 1
    result = {
        "has_org_sp": False, "coverage_pct": 0.0, "sp_savings_mtd": 0.0,
        "sp_savings_monthly_est": 0.0, "effective_discount_pct": 0.0,
        "uncovered_od_spend": 0.0, "days_elapsed": days_elapsed,
    }
    try:
        r = ce.get_cost_and_usage(
            TimePeriod={"Start": month_start, "End": today},
            Granularity="MONTHLY",
            Metrics=["UnblendedCost", "AmortizedCost"],
            GroupBy=[{"Type": "DIMENSION", "Key": "RECORD_TYPE"}],
        )
        rtypes = {g["Keys"][0]: {"unblended": float(g["Metrics"]["UnblendedCost"]["Amount"]),
                                  "amortized": float(g["Metrics"]["AmortizedCost"]["Amount"])}
                  for period in r["ResultsByTime"] for g in period["Groups"]}
        if "SavingsPlanCoveredUsage" in rtypes:
            result["has_org_sp"]   = True
            od   = rtypes["SavingsPlanCoveredUsage"]["unblended"]
            amrt = rtypes["SavingsPlanCoveredUsage"]["amortized"]
            result["sp_savings_mtd"]          = od - amrt
            result["sp_savings_monthly_est"]  = (od - amrt) / max(days_elapsed, 1) * 30
            result["effective_discount_pct"]  = (od - amrt) / max(od, 0.01) * 100
        if "Usage" in rtypes:
            result["uncovered_od_spend"] = rtypes["Usage"]["unblended"]
    except Exception:
        pass
    try:
        cov = ce.get_savings_plans_coverage(
            TimePeriod={"Start": month_start, "End": today}, Granularity="MONTHLY")
        pcts = [float(c["Coverage"].get("CoveragePercentage", 0))
                for c in cov.get("SavingsPlansCoverages", [])
                if c.get("Coverage", {}).get("CoveragePercentage")]
        if pcts:
            result["coverage_pct"] = sum(pcts) / len(pcts)
    except Exception:
        pass
    return result

# ── Data collection ───────────────────────────────────────────────────────────
def collect(session):
    cached = load_cache(CACHE_FILE)
    if cached:
        print("  ✓ Using cached ECS data")
        return cached

    ecs = session.client("ecs", region_name=REGION)
    cw  = session.client("cloudwatch", region_name=REGION)
    aas = session.client("application-autoscaling", region_name=REGION)

    print("  → Fetching ECS clusters...")
    cluster_arns = ecs.list_clusters()["clusterArns"]
    described    = ecs.describe_clusters(
        clusters=cluster_arns, include=["SETTINGS", "STATISTICS", "TAGS"])
    clusters_raw = described["clusters"]
    print(f"  → {len(clusters_raw)} clusters found.")

    print("  → Querying Cost Explorer for org-level SP coverage...")
    sp_data = collect_sp_data(session)
    if sp_data["has_org_sp"]:
        print(f"     ✓ Org SP active — coverage {sp_data['coverage_pct']:.1f}%, "
              f"~${sp_data['sp_savings_monthly_est']:.0f}/month est. (covers Fargate too)")

    services_all = []
    td_cache     = {}

    def enrich_cluster(cluster):
        cname    = cluster["clusterName"]
        settings = {s["name"]: s["value"] for s in cluster.get("settings", [])}
        ci_on    = settings.get("containerInsights", "disabled") == "enabled"

        # List all services
        svc_arns = []
        paginator = ecs.get_paginator("list_services")
        for page in paginator.paginate(cluster=cname):
            svc_arns.extend(page["serviceArns"])

        svcs = []
        for i in range(0, len(svc_arns), 10):
            batch = ecs.describe_services(cluster=cname, services=svc_arns[i:i+10])["services"]
            svcs.extend(batch)

        # Auto-scaling for all services in one call
        resource_ids = [f"service/{cname}/{s['serviceName']}" for s in svcs]
        as_targets   = {}
        for i in range(0, len(resource_ids), 50):
            try:
                resp = aas.describe_scalable_targets(
                    ServiceNamespace="ecs", ResourceIds=resource_ids[i:i+50])
                for t in resp.get("ScalableTargets", []):
                    sname = t["ResourceId"].split("/")[-1]
                    as_targets[sname] = {"min": t["MinCapacity"], "max": t["MaxCapacity"]}
            except Exception as e:
                if "ThrottlingException" in str(type(e).__name__) or "Rate exceeded" in str(e):
                    print(f"  ⚠ DescribeScalableTargets throttled — skipping autoscaling data for {cname}")
                else:
                    raise

        enriched = []
        for svc in svcs:
            sname   = svc["serviceName"]
            td_arn  = svc["taskDefinition"]

            # Task definition (cached by ARN)
            if td_arn not in td_cache:
                td_cache[td_arn] = ecs.describe_task_definition(
                    taskDefinition=td_arn)["taskDefinition"]
            td = td_cache[td_arn]

            cpu_units = int(td.get("cpu",  256))
            mem_mb    = int(td.get("memory", 512))
            arch      = td.get("runtimePlatform", {}).get("cpuArchitecture", "X86_64")

            # CloudWatch metrics (only meaningful if Container Insights enabled)
            cpu_avg = cpu_max = cpu_p95 = None
            mem_avg = mem_max = mem_p95 = None
            if ci_on:
                cpu_avg, cpu_max, cpu_p95 = get_cw_metric(cw, cname, sname, "CPUUtilization")
                mem_avg, mem_max, mem_p95 = get_cw_metric(cw, cname, sname, "MemoryUtilization")

            enriched.append({
                "cluster":        cname,
                "service":        sname,
                "status":         svc["status"],
                "desired":        svc["desiredCount"],
                "running":        svc["runningCount"],
                "launch_type":    svc.get("launchType", "FARGATE"),
                "task_def":       td_arn.split("/")[-1],
                "cpu_units":      cpu_units,
                "mem_mb":         mem_mb,
                "arch":           arch,
                "vcpu":           cpu_units / 1024,
                "mem_gb":         mem_mb / 1024,
                "container_insights": ci_on,
                "autoscaling":    as_targets.get(sname),
                "cpu_avg":        cpu_avg,
                "cpu_max":        cpu_max,
                "cpu_p95":        cpu_p95,
                "mem_avg":        mem_avg,
                "mem_max":        mem_max,
                "mem_p95":        mem_p95,
                "monthly_cost":   fargate_monthly(cpu_units, mem_mb, arch, svc["desiredCount"]),
            })
        return enriched

    total = 0
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {executor.submit(enrich_cluster, c): c["clusterName"] for c in clusters_raw}
        for future in as_completed(futures):
            result = future.result()
            services_all.extend(result)
            total += len(result)
            sys.stdout.write(f"\r  → Collected {total} services across {len(services_all)//max(total,1)+1} clusters   ")
            sys.stdout.flush()
    print(f"\n  ✓ {len(services_all)} ECS services collected across {len(clusters_raw)} clusters.")

    data = {"clusters": clusters_raw, "services": services_all, "sp_data": sp_data}
    save_cache(CACHE_FILE, data)
    return data

# ── Analysis ──────────────────────────────────────────────────────────────────
def analyse(data):
    services = data["services"]
    sp_data  = data["sp_data"]
    opps     = []

    active = [s for s in services if s["status"] == "ACTIVE" and s["desired"] > 0]

    for svc in active:
        cname   = svc["cluster"]
        sname   = svc["service"]
        arch    = svc["arch"]
        desired = svc["desired"]
        cpu     = svc["cpu_units"]
        mem     = svc["mem_mb"]
        vcpu    = svc["vcpu"]
        mem_gb  = svc["mem_gb"]
        cost_mo = svc["monthly_cost"]
        ci_on   = svc["container_insights"]

        cpu_avg = svc.get("cpu_avg")
        cpu_p95 = svc.get("cpu_p95")
        cpu_max = svc.get("cpu_max")
        mem_avg = svc.get("mem_avg")
        mem_max = svc.get("mem_max")
        mem_p95 = svc.get("mem_p95")

        # ── Zombie service (desired > 0 but 0 running tasks) ─────────────────
        if svc["running"] == 0 and desired > 0:
            opps.append({
                "cluster": cname, "service": sname,
                "type": "ecs-zombie-service", "severity": HIGH,
                "current_mo": round(cost_mo, 2), "saving_mo": round(cost_mo, 2),
                "dep_warning": "",
                "recommendation": (
                    f"Service '{sname}' has desiredCount={desired} but 0 running tasks. "
                    f"It is configured to run but nothing is healthy. "
                    f"If this service is no longer needed, set desiredCount=0 or delete it "
                    f"to stop incurring scheduling and ALB costs. "
                    f"Investigate the stop reason in the ECS task stopped events."
                ),
            })
            continue  # no further analysis on zombie

        # ── Auto-scaling locked (min == max) ──────────────────────────────────
        asc = svc.get("autoscaling")
        if asc:
            if asc["min"] == asc["max"]:
                opps.append({
                    "cluster": cname, "service": sname,
                    "type": "ecs-autoscaling-locked", "severity": MEDIUM,
                    "current_mo": round(cost_mo, 2), "saving_mo": 0.0,
                    "dep_warning": "",
                    "recommendation": (
                        f"Service '{sname}' has auto-scaling registered but min={asc['min']} == max={asc['max']}. "
                        f"This is effectively static — tasks can neither scale in nor scale out. "
                        f"Set min < desired < max with a CPUUtilization target tracking policy "
                        f"to allow cost-saving scale-in during off-peak hours."
                    ),
                })
        else:
            # No auto-scaling at all
            if desired > 1:
                opps.append({
                    "cluster": cname, "service": sname,
                    "type": "ecs-no-autoscaling", "severity": MEDIUM,
                    "current_mo": round(cost_mo, 2), "saving_mo": 0.0,
                    "dep_warning": "",
                    "recommendation": (
                        f"Service '{sname}' runs {desired} tasks with no Application Auto Scaling. "
                        f"Without scale-in, tasks run at peak allocation 24/7. "
                        f"Add target tracking on CPUUtilization (target 60%) to scale down during off-peak."
                    ),
                })

        # ── CPU over-provisioned ──────────────────────────────────────────────
        if cpu_avg is not None:
            smaller = next_smaller_fargate(cpu, mem)
            if cpu_avg < 10 and (cpu_p95 or 0) < 25 and smaller:
                new_cpu, new_mem = smaller
                saving = cost_mo - fargate_monthly(new_cpu, new_mem, arch, desired)
                if saving > 0:
                    opps.append({
                        "cluster": cname, "service": sname,
                        "type": "ecs-cpu-overprovision", "severity": HIGH,
                        "current_mo": round(cost_mo, 2), "saving_mo": round(saving, 2),
                        "dep_warning": "",
                        "recommendation": (
                            f"Service '{sname}': avg CPU {cpu_avg:.1f}%, p95 {cpu_p95:.1f}%, "
                            f"max {cpu_max:.1f}% over {CW_DAYS} days (provisioned {vcpu:.2f} vCPU). "
                            f"Reducing to {new_cpu/1024:.2f} vCPU / {new_mem} MB saves "
                            f"~${saving:.2f}/month across {desired} task(s)."
                        ),
                    })
            elif cpu_avg < 20 and (cpu_p95 or 0) < 40 and smaller:
                new_cpu, new_mem = smaller
                saving = cost_mo - fargate_monthly(new_cpu, new_mem, arch, desired)
                if saving > 0:
                    opps.append({
                        "cluster": cname, "service": sname,
                        "type": "ecs-cpu-overprovision", "severity": MEDIUM,
                        "current_mo": round(cost_mo, 2), "saving_mo": round(saving, 2),
                        "dep_warning": "",
                        "recommendation": (
                            f"Service '{sname}': avg CPU {cpu_avg:.1f}%, p95 {cpu_p95:.1f}%, "
                            f"max {cpu_max:.1f}% over {CW_DAYS} days (provisioned {vcpu:.2f} vCPU). "
                            f"Consider evaluating downsize to {new_cpu/1024:.2f} vCPU / {new_mem} MB "
                            f"(~${saving:.2f}/month saving, {desired} task(s))."
                        ),
                    })

        # ── Memory over-provisioned ───────────────────────────────────────────
        if mem_avg is not None:
            # Only flag memory independently if CPU is NOT already flagged
            # (to avoid double-counting the same downsize)
            already_flagged_cpu = any(
                o["service"] == sname and o["type"] == "ecs-cpu-overprovision"
                for o in opps
            )
            headroom_needed_mb = int((mem_max or 0) / 100 * mem * 1.3)  # max% * alloc * 1.3 headroom
            if not already_flagged_cpu and mem_avg < 20 and (mem_max or 0) < 40:
                # Memory is genuinely oversized independent of CPU
                suggested_mb = max(512, round(headroom_needed_mb / 1024) * 1024)
                # Find smallest valid mem for current cpu_units
                valid_mems = FARGATE_VALID.get(cpu, [])
                suggested_mb = min((m for m in valid_mems if m >= suggested_mb), default=min(valid_mems) if valid_mems else mem)
                if suggested_mb < mem:
                    saving = cost_mo - fargate_monthly(cpu, suggested_mb, arch, desired)
                    if saving > 0.01:
                        opps.append({
                            "cluster": cname, "service": sname,
                            "type": "ecs-memory-overprovision", "severity": MEDIUM,
                            "current_mo": round(cost_mo, 2), "saving_mo": round(saving, 2),
                            "dep_warning": "",
                            "recommendation": (
                                f"Service '{sname}': avg memory {mem_avg:.1f}%, "
                                f"max {mem_max:.1f}% over {CW_DAYS} days "
                                f"(provisioned {mem_gb:.1f} GB). "
                                f"Reducing memory from {mem} MB to {suggested_mb} MB "
                                f"(30% headroom above max) saves ~${saving:.2f}/month."
                            ),
                        })

        # ── Graviton (ARM64) migration ────────────────────────────────────────
        if arch == "X86_64":
            arm_cost = fargate_monthly(cpu, mem, "ARM64", desired)
            saving   = cost_mo - arm_cost
            pct      = saving / cost_mo * 100 if cost_mo > 0 else 0
            if saving > 0.5:
                opps.append({
                    "cluster": cname, "service": sname,
                    "type": "ecs-graviton-fargate", "severity": DEP,
                    "current_mo": round(cost_mo, 2), "saving_mo": round(saving, 2),
                    "dep_warning": (
                        "Docker images must be built for linux/arm64 (multi-arch manifest or ARM64-only). "
                        "All native dependencies, Lambda layers, and sidecars must support ARM64. "
                        "Test on a separate ARM64 task definition before switching."
                    ),
                    "recommendation": (
                        f"Service '{sname}' runs X86_64 Fargate ({vcpu:.2f} vCPU / {mem_gb:.1f} GB, "
                        f"{desired} task(s)). Migrating to ARM64 (Graviton) saves "
                        f"~${saving:.2f}/month (~{pct:.0f}%). "
                        f"Change task def runtimePlatform.cpuArchitecture to ARM64 "
                        f"and update ECR images. ⚠️ linux/arm64 build required."
                    ),
                })

        # ── Container Insights not enabled ───────────────────────────────────
        if not ci_on:
            opps.append({
                "cluster": cname, "service": sname,
                "type": "ecs-no-container-insights", "severity": LOW,
                "current_mo": 0.0, "saving_mo": 0.0,
                "dep_warning": "",
                "recommendation": (
                    f"Cluster '{cname}' does not have Container Insights enabled. "
                    f"CPU and memory utilisation metrics are unavailable, making "
                    f"right-sizing analysis impossible for '{sname}'. "
                    f"Enable Container Insights on the cluster."
                ),
            })

    return opps

# ── Reporting ─────────────────────────────────────────────────────────────────
def print_report(data, opps):
    services  = data["services"]
    clusters  = data["clusters"]
    sp_data   = data["sp_data"]

    active    = [s for s in services if s["status"] == "ACTIVE" and s["desired"] > 0]
    zombies   = [s for s in active if s["running"] == 0]
    healthy   = [s for s in active if s["running"] > 0]

    total_mo    = sum(s["monthly_cost"] for s in active)
    total_tasks = sum(s["desired"] for s in active)
    real_opps   = [o for o in opps if o["type"] != "ecs-no-container-insights"]
    real_sorted = sorted(real_opps, key=lambda x: x["saving_mo"], reverse=True)
    resources_evaluated = len(active)
    no_findings_desc = (
        f"No resources found (0 ECS services in {REGION})."
        if resources_evaluated == 0
        else f"Evaluated {resources_evaluated} ECS service(s). No cost-saving opportunities found."
    )

    # Cluster breakdown
    clusters_by_name = {c["clusterName"]: c for c in clusters}

    print()
    print(BORDER_TOP)
    print(_bl("           AWS Cost Analyser — ECS Analysis Results"))
    print(BORDER_MID)
    print(_bl(f"  Account  : {ACCOUNT_ID}"))
    print(_bl(f"  Region   : {REGION}"))
    print(_bl(f"  Run at   : {now.strftime('%Y-%m-%d %H:%M UTC')}"))
    print(_bl(f"  Clusters : {len(clusters)}"))
    print(_bl(f"  Services : {len(active)} active ({len(zombies)} zombie · {len(healthy)} healthy) · {len(active)} total desired tasks: {total_tasks}"))
    print(_bl(f"  Launch   : All Fargate"))
    sp_label = f"Active ✅ ({sp_data['coverage_pct']:.0f}% coverage)" if sp_data['has_org_sp'] else "NOT DETECTED ⚠️"
    print(_bl(f"  Org SP   : {sp_label}"))
    print(BORDER_MID)
    print(_bl(f"  💸 Est. Fargate cost (all active)    : ${total_mo:>8.2f} / month"))
    if sp_data["has_org_sp"]:
        sp_share   = total_mo / max(total_mo, 1) * sp_data["effective_discount_pct"] / 100
        eff_cost   = total_mo * (1 - sp_data["effective_discount_pct"] / 100)
        print(_bl(f"  ✅ Est. effective cost after org SP  : ${eff_cost:>8.2f} / month"))
        print(_bl(f"  ✅ SP effective discount             : {sp_data['effective_discount_pct']:>7.1f}% (Fargate included)"))
    print(_bl(f"  💰 Additional savings potential      : ${sum(o['saving_mo'] for o in real_opps):>8.2f} / month"))
    print(_bl(f"  💰 Annualised additional savings     : ${sum(o['saving_mo'] for o in real_opps)*12:>8.2f} / year"))
    print(_bl(f"  🔍 Opportunities found               : {len(real_opps)}"))
    print(BORDER_BOT)
    if not opps:
        print(f"\n✅ {no_findings_desc}")

    # Cluster summary
    print("\n📦 Cluster Summary:")
    print(f"  {'Cluster':<45} {'Svc':>3} {'Tasks':>5} {'$/mo':>9}  {'CI':>4}  {'Arch'}")
    print(f"  {'-'*45} {'-'*3} {'-'*5} {'-'*9}  {'-'*4}  {'-'*10}")
    cluster_svcs = {}
    for s in active:
        k = s["cluster"]
        if k not in cluster_svcs:
            cluster_svcs[k] = {"svcs": 0, "tasks": 0, "cost": 0.0, "ci": s["container_insights"]}
        cluster_svcs[k]["svcs"]  += 1
        cluster_svcs[k]["tasks"] += s["desired"]
        cluster_svcs[k]["cost"]  += s["monthly_cost"]
    for cname, v in sorted(cluster_svcs.items(), key=lambda x: -x[1]["cost"]):
        archs = set(s["arch"] for s in active if s["cluster"] == cname)
        ci_icon = "✅" if v["ci"] else "❌"
        print(f"  {cname:<45} {v['svcs']:>3} {v['tasks']:>5} ${v['cost']:>8.2f}  {ci_icon:>4}  {','.join(archs)}")

    # Service resource allocation table
    print(f"\n📊 Service Resource Allocation & Utilisation (last {CW_DAYS} days):")
    print(f"  {'Service':<48} {'vCPU':>5} {'MEM':>6} {'$/mo':>7}  "
          f"{'CpuAvg':>6} {'CpuMax':>6} {'MemAvg':>6} {'MemMax':>6}")
    print(f"  {'-'*48} {'-'*5} {'-'*6} {'-'*7}  {'-'*6} {'-'*6} {'-'*6} {'-'*6}")
    for s in sorted(active, key=lambda x: -x["monthly_cost"]):
        ca   = f"{s['cpu_avg']:.0f}%" if s.get("cpu_avg") is not None else "  N/A"
        cmax = f"{s['cpu_max']:.0f}%" if s.get("cpu_max") is not None else "  N/A"
        ma   = f"{s['mem_avg']:.0f}%" if s.get("mem_avg") is not None else "  N/A"
        mmax = f"{s['mem_max']:.0f}%" if s.get("mem_max") is not None else "  N/A"
        flag = ""
        if s.get("cpu_avg") is not None and s["cpu_avg"] < 10:
            flag = " 🔴"
        elif s.get("cpu_avg") is not None and s["cpu_avg"] < 20:
            flag = " 🟠"
        print(f"  {s['service'][:48]:<48} {s['vcpu']:>5.2f} "
              f"{s['mem_mb']:>5}M ${s['monthly_cost']:>6.2f}  "
              f"{ca:>6} {cmax:>6} {ma:>6} {mmax:>6}{flag}")

    # Opportunity type summary
    type_summary = {}
    for o in real_opps:
        t = o["type"]
        if t not in type_summary:
            type_summary[t] = {"count": 0, "saving": 0.0}
        type_summary[t]["count"]  += 1
        type_summary[t]["saving"] += o["saving_mo"]
    print("\n📊 Opportunities by Type:")
    print(f"  {'Type':<35} {'Count':>5}  {'Saving/mo':>12}  {'Saving/yr':>12}")
    print(f"  {'-'*35} {'-'*5}  {'-'*12}  {'-'*12}")
    for t, v in sorted(type_summary.items(), key=lambda x: -x[1]["saving"]):
        print(f"  {t:<35} {v['count']:>5}  ${v['saving']:>10.2f}  ${v['saving']*12:>10.2f}")

    sev_counts = {}
    for o in real_opps:
        sev_counts[o["severity"]] = sev_counts.get(o["severity"], 0) + 1
    print(f"\n🎯 Severity: " + "  ".join(f"{s}: {c}" for s, c in sev_counts.items()))

    # Detailed findings
    print("\n" + "─" * 72)
    print("📋 Detailed Findings (sorted by potential saving):\n")
    for i, opp in enumerate(real_sorted, 1):
        print(f"  [{i:02d}] {opp['severity']}  —  {opp['type']}")
        print(f"       Cluster  : {opp['cluster']}")
        print(f"       Service  : {opp['service']}")
        if opp["saving_mo"] > 0:
            print(f"       Saving   : ${opp['saving_mo']:.2f}/month  (${opp['saving_mo']*12:.2f}/year)")
            print(f"       Current  : ${opp['current_mo']:.2f}/month")
        if opp["dep_warning"]:
            # truncate dep warning to 68 chars
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

    # Zombie services callout
    if zombies:
        print(f"\n🧟 Zombie Services ({len(zombies)}) — desired > 0 but 0 running tasks:")
        for s in zombies:
            print(f"   • {s['cluster']:<45}  {s['service']}")

    # CSV
    ts = now.strftime("%Y%m%d_%H%M%S")
    csv_path = os.path.join(OUTPUT_DIR, f"{ts}_ecs_report.csv")
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(csv_path, "w") as f:
        f.write("account_id,region,cluster,service,opportunity_type,severity,"
                "current_cost_usd_monthly,estimated_saving_usd_monthly,saving_pct,saving_basis,price_basis,"
                "dependency_warning,recommendation\n")
        if not opps:
            f.write(f'"{ACCOUNT_ID}","{REGION}","","","no-findings","INFO",0.00,0.00,0.0,"actual","list","","{no_findings_desc}"\n')
        else:
            for o in opps:
                pct = (o["saving_mo"] / max(o["current_mo"], 0.01)) * 100 if o["current_mo"] > 0 else 0
                saving_basis = "actual"
                sev = o["severity"].replace("🔴 ","").replace("🟠 ","").replace("🟡 ","").replace("🟢 ","").replace("⚠️  ","").strip()
                f.write(f'"{ACCOUNT_ID}","{REGION}","{o["cluster"]}","{o["service"]}",'
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
    print("\n🔍 AWS ECS Cost Analyser")
    print(f"   Account : {ACCOUNT_ID}")
    print(f"   Region  : {REGION}")
    print(f"   Profile : {os.environ.get('AWS_PROFILE', 'default')}\n")
    session = boto3.Session()
    print("📡 Collecting ECS + CloudWatch + Cost Explorer data...")
    data    = collect(session)
    print(f"\n🧠 Analysing {len(data['services'])} services...")
    opps    = analyse(data)
    print_report(data, opps)


if __name__ == "__main__":
    main()
