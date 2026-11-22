#!/usr/bin/env python3
"""
AWS Cost Analyser — Lambda Skill
Analyses AWS Lambda functions for cost and security optimisation.

Key behaviours:
  - Detects idle functions (0 invocations in 30 days) — security + noise
  - Flags deprecated/EOL runtimes (security risk)
  - Right-sizes high-memory Java functions based on duration metrics
  - Identifies x86 functions eligible for ARM64 (Graviton) migration
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
CACHE_FILE  = os.path.join(CACHE_DIR, "lambda.json")
CW_DAYS     = 30

now = datetime.now(timezone.utc)

# ── Severity ──────────────────────────────────────────────────────────────────
CRITICAL = "🔴 CRITICAL"
HIGH     = "🟠 HIGH"
MEDIUM   = "🟡 MEDIUM"
LOW      = "🟢 LOW"
DEP      = "⚠️  DEPENDENCY"
INFO     = "ℹ️  INFO"

# ── Lambda pricing: eu-central-1 ─────────────────────────────────────────────
# Source: https://aws.amazon.com/lambda/pricing/
PRICE_PER_GB_SEC   = 0.0000166667   # per GB-second
PRICE_PER_1M_REQS  = 0.20           # per million requests
FREE_GB_SEC        = 400_000        # monthly free tier GB-seconds
FREE_REQS          = 1_000_000      # monthly free tier requests

# ARM64 (Graviton2) is ~20% cheaper
ARM64_DISCOUNT = 0.20

# ── Deprecated / EOL runtimes ─────────────────────────────────────────────────
DEPRECATED_RUNTIMES = {
    "nodejs12.x":  "EOL — no security patches",
    "nodejs14.x":  "EOL — no security patches",
    "nodejs16.x":  "EOL — no security patches",
    "python3.8":   "EOL — deprecation in progress",
    "python3.9":   "EOL — deprecation in progress",
    "java11":      "EOL — deprecation in progress",
    "go1.x":       "Deprecated — migrate to provided.al2023",
    "dotnet5.0":   "EOL — no security patches",
    "dotnet6":     "EOL — deprecation in progress",
    "ruby2.7":     "EOL — no security patches",
}

# Runtimes eligible for ARM64 migration
ARM64_ELIGIBLE_RUNTIMES = {
    "nodejs18.x", "nodejs20.x", "nodejs22.x",
    "python3.11", "python3.12", "python3.13",
    "java17", "java21",
    "provided.al2", "provided.al2023",
}

# ── Cache helpers ─────────────────────────────────────────────────────────────
# ── CloudWatch helpers ────────────────────────────────────────────────────────
def get_lambda_metrics(cw, function_name):
    """Fetch Invocations, Duration, Errors, Throttles for a Lambda function."""
    dims = [{"Name": "FunctionName", "Value": function_name}]
    end   = now
    start = now - timedelta(days=CW_DAYS)

    def _stat(metric, stat, period=CW_DAYS * 86400):
        try:
            r = cw.get_metric_statistics(
                Namespace="AWS/Lambda",
                MetricName=metric,
                Dimensions=dims,
                StartTime=start,
                EndTime=end,
                Period=period,
                Statistics=[stat],
            )
            pts = r.get("Datapoints", [])
            if not pts:
                return None
            return round(sum(p[stat] for p in pts), 4)
        except Exception:
            return None

    def _avg(metric, period=3600):
        try:
            r = cw.get_metric_statistics(
                Namespace="AWS/Lambda",
                MetricName=metric,
                Dimensions=dims,
                StartTime=start,
                EndTime=end,
                Period=period,
                Statistics=["Average"],
            )
            pts = r.get("Datapoints", [])
            if not pts:
                return None
            return round(sum(p["Average"] for p in pts) / len(pts), 2)
        except Exception:
            return None

    return {
        "invocations":    _stat("Invocations", "Sum"),
        "duration_avg_ms": _avg("Duration"),
        "errors":         _stat("Errors", "Sum"),
        "throttles":      _stat("Throttles", "Sum"),
    }

# ── Data collection ───────────────────────────────────────────────────────────
def collect(session):
    cached = load_cache(CACHE_FILE)
    if cached:
        print("  ✓ Using cached Lambda data")
        return cached

    lmb = session.client("lambda", region_name=REGION)
    cw  = session.client("cloudwatch", region_name=REGION)

    print("  → Listing Lambda functions...")
    functions = []
    paginator = lmb.get_paginator("list_functions")
    for page in paginator.paginate():
        functions.extend(page.get("Functions", []))
    print(f"  → {len(functions)} Lambda function(s) found.")

    print(f"  → Fetching CloudWatch metrics for {len(functions)} functions (parallel)...")

    enriched = []
    completed = 0

    def enrich(fn):
        fname   = fn["FunctionName"]
        runtime = fn.get("Runtime", "unknown")
        arch    = fn.get("Architectures", ["x86_64"])[0]
        mem_mb  = fn.get("MemorySize", 128)
        timeout = fn.get("Timeout", 3)
        metrics = get_lambda_metrics(cw, fname)
        return {
            "name":            fname,
            "runtime":         runtime,
            "arch":            arch,
            "memory_mb":       mem_mb,
            "timeout_s":       timeout,
            "code_size_bytes": fn.get("CodeSize", 0),
            "last_modified":   fn.get("LastModified", ""),
            "description":     fn.get("Description", ""),
            "invocations":     metrics.get("invocations") or 0,
            "duration_avg_ms": metrics.get("duration_avg_ms"),
            "errors":          metrics.get("errors") or 0,
            "throttles":       metrics.get("throttles") or 0,
        }

    with ThreadPoolExecutor(max_workers=20) as executor:
        futures = {executor.submit(enrich, fn): fn["FunctionName"] for fn in functions}
        for future in as_completed(futures):
            enriched.append(future.result())
            completed += 1
            sys.stdout.write(f"\r  → Metrics collected: {completed}/{len(functions)}   ")
            sys.stdout.flush()
    print()

    data = {"functions": enriched}
    save_cache(CACHE_FILE, data)
    print(f"  ✓ Lambda data collected and cached.")
    return data

# ── Cost estimate helpers ─────────────────────────────────────────────────────
def lambda_monthly_cost(invocations, duration_avg_ms, memory_mb, arch="x86_64"):
    """Estimate monthly Lambda cost (invocations are already monthly or 30-day totals)."""
    if not invocations or not duration_avg_ms:
        return 0.0
    gb_sec       = (memory_mb / 1024) * (duration_avg_ms / 1000) * invocations
    price_gb_sec = PRICE_PER_GB_SEC * (1 - ARM64_DISCOUNT if arch == "arm64" else 1)
    compute_cost = max(0, gb_sec - FREE_GB_SEC) * price_gb_sec
    req_cost     = max(0, invocations - FREE_REQS) / 1_000_000 * PRICE_PER_1M_REQS
    return round(compute_cost + req_cost, 4)

# ── Analysis ──────────────────────────────────────────────────────────────────
def analyse(data):
    functions = data["functions"]
    opps      = []

    idle       = [f for f in functions if f["invocations"] == 0]
    deprecated = [f for f in functions if f["runtime"] in DEPRECATED_RUNTIMES]
    active     = [f for f in functions if f["invocations"] > 0]

    # ── Deprecated runtimes ───────────────────────────────────────────────────
    if deprecated:
        # Group by runtime for the recommendation
        by_runtime = {}
        for f in deprecated:
            rt = f["runtime"]
            by_runtime.setdefault(rt, []).append(f["name"])

        runtime_summary = "; ".join(
            f"{rt} ({len(names)} fn): {', '.join(names[:3])}{'...' if len(names)>3 else ''}"
            for rt, names in sorted(by_runtime.items())
        )
        opps.append({
            "resource":       f"{len(deprecated)} functions",
            "type":           "lambda-deprecated-runtimes",
            "severity":       HIGH,
            "current_mo":     0.0,
            "saving_mo":      0.0,
            "dep_warning":    "",
            "recommendation": (
                f"{len(deprecated)} functions are running on deprecated/EOL runtimes "
                f"that no longer receive AWS security patches. "
                f"Runtime breakdown: {runtime_summary}. "
                f"Upgrade each function to a supported runtime. "
                f"Node.js: migrate to nodejs20.x or nodejs22.x. "
                f"Python: migrate to python3.12 or python3.13. "
                f"Java: migrate to java21. "
                f"Go (go1.x): migrate to provided.al2023 with Go runtime bootstrap."
            ),
        })

    # ── Idle functions ────────────────────────────────────────────────────────
    if idle:
        # Batch names into the recommendation (up to 10 shown)
        names_preview = ", ".join(f["name"] for f in idle[:10])
        if len(idle) > 10:
            names_preview += f" ... (+{len(idle)-10} more)"
        opps.append({
            "resource":       f"{len(idle)} functions",
            "type":           "lambda-idle-functions",
            "severity":       MEDIUM,
            "current_mo":     0.0,
            "saving_mo":      0.0,
            "dep_warning":    "",
            "recommendation": (
                f"{len(idle)} Lambda functions had 0 invocations in the last {CW_DAYS} days. "
                f"While idle Lambda functions incur no compute cost, they represent an "
                f"expanded security attack surface (IAM roles, VPC access, env vars with secrets). "
                f"Review and delete truly unused functions, or tag with 'inactive' for audit trail. "
                f"Idle functions: {names_preview}."
            ),
        })

    # ── High-memory Java functions ────────────────────────────────────────────
    HIGH_MEM_THRESHOLD_MB = 1024
    java_high_mem = [
        f for f in active
        if f["runtime"] in ("java11", "java17", "java21")
        and f["memory_mb"] >= HIGH_MEM_THRESHOLD_MB
        and f["duration_avg_ms"] is not None
    ]
    for f in java_high_mem:
        current_mem  = f["memory_mb"]
        suggested    = max(512, current_mem // 2)
        invocations  = f["invocations"]
        duration_ms  = f["duration_avg_ms"]
        arch         = f["arch"]
        current_cost = lambda_monthly_cost(invocations, duration_ms, current_mem, arch)
        reduced_cost = lambda_monthly_cost(invocations, duration_ms, suggested, arch)
        saving       = current_cost - reduced_cost
        if saving > 0.01:
            opps.append({
                "resource":       f["name"],
                "type":           "lambda-high-memory-java",
                "severity":       MEDIUM,
                "current_mo":     round(current_cost, 4),
                "saving_mo":      round(saving, 4),
                "dep_warning":    "",
                "recommendation": (
                    f"Function '{f['name']}' ({f['runtime']}, {current_mem} MB) had "
                    f"{int(invocations):,} invocations with avg duration {duration_ms:.0f} ms. "
                    f"JVM functions benefit from higher memory but {current_mem} MB may be excessive. "
                    f"Reducing from {current_mem} MB → {suggested} MB could save "
                    f"~${saving:.4f}/month. "
                    f"Use AWS Lambda Power Tuning (Step Functions) to find the optimal memory "
                    f"that balances cost and duration for this function."
                ),
            })

    # ── x86 → ARM64 (Graviton) migration ─────────────────────────────────────
    arm64_candidates = [
        f for f in functions
        if f["arch"] == "x86_64"
        and f["runtime"] in ARM64_ELIGIBLE_RUNTIMES
    ]
    if arm64_candidates:
        total_saving = 0.0
        candidate_names = []
        for f in arm64_candidates:
            inv  = f["invocations"]
            dur  = f["duration_avg_ms"]
            mem  = f["memory_mb"]
            cost_x86 = lambda_monthly_cost(inv, dur, mem, "x86_64")
            cost_arm = lambda_monthly_cost(inv, dur, mem, "arm64")
            total_saving += cost_x86 - cost_arm
            candidate_names.append(f["name"])

        names_preview = ", ".join(candidate_names[:8])
        if len(candidate_names) > 8:
            names_preview += f" ... (+{len(candidate_names)-8} more)"

        opps.append({
            "resource":       f"{len(arm64_candidates)} functions",
            "type":           "lambda-x86-to-arm64",
            "severity":       DEP,
            "current_mo":     0.0,
            "saving_mo":      round(total_saving, 4),
            "dep_warning":    (
                "Lambda Layers and container images must support arm64 architecture. "
                "Test each function on arm64 before switching. "
                "Native extensions compiled for x86_64 will not run on arm64."
            ),
            "recommendation": (
                f"{len(arm64_candidates)} functions on eligible runtimes "
                f"({', '.join(sorted(ARM64_ELIGIBLE_RUNTIMES & {f['runtime'] for f in arm64_candidates}))}) "
                f"are running on x86_64 but could use ARM64 (Graviton2) at ~20% lower cost. "
                f"Estimated saving: ~${total_saving:.4f}/month across all candidates. "
                f"Change each function's Architectures from [x86_64] to [arm64] and verify Layers support arm64. "
                f"Candidates: {names_preview}."
            ),
        })

    return opps

# ── Reporting ─────────────────────────────────────────────────────────────────
def print_report(data, opps):
    functions  = data["functions"]
    active     = [f for f in functions if f["invocations"] > 0]
    idle       = [f for f in functions if f["invocations"] == 0]
    deprecated = [f for f in functions if f["runtime"] in DEPRECATED_RUNTIMES]
    total_saving = sum(o["saving_mo"] for o in opps)
    resources_evaluated = len(functions)
    no_findings_desc = (
        f"No resources found (0 Lambda functions in {REGION})."
        if resources_evaluated == 0
        else f"Evaluated {resources_evaluated} Lambda function(s). No cost-saving opportunities found."
    )

    print()
    print(BORDER_TOP)
    print(_bl("         AWS Cost Analyser — Lambda Analysis Results"))
    print(BORDER_MID)
    print(_bl(f"  Account     : {ACCOUNT_ID}"))
    print(_bl(f"  Region      : {REGION}"))
    print(_bl(f"  Run at      : {now.strftime('%Y-%m-%d %H:%M UTC')}"))
    print(_bl(f"  Functions   : {len(functions)} total ({len(active)} active · {len(idle)} idle · {len(deprecated)} deprecated)"))
    print(BORDER_MID)
    print(_bl(f"  💰 Savings potential (compute)       : ${total_saving:>8.4f} / month"))
    print(_bl(f"  💰 Annualised savings potential      : ${total_saving*12:>8.4f} / year"))
    print(_bl(f"  🔍 Opportunities found               : {len(opps)}"))
    print(BORDER_BOT)
    if not opps:
        print(f"\n✅ {no_findings_desc}")

    # Runtime breakdown
    runtime_counts = {}
    for f in functions:
        rt = f["runtime"]
        runtime_counts[rt] = runtime_counts.get(rt, 0) + 1
    print("\n📦 Runtime Breakdown:")
    print(f"  {'Runtime':<25} {'Count':>5}  {'Active':>6}  {'Idle':>5}  {'Status'}")
    print(f"  {'-'*25} {'-'*5}  {'-'*6}  {'-'*5}  {'-'*20}")
    for rt, cnt in sorted(runtime_counts.items(), key=lambda x: -x[1]):
        act = sum(1 for f in functions if f["runtime"] == rt and f["invocations"] > 0)
        idl = cnt - act
        status = f"⚠️  {DEPRECATED_RUNTIMES[rt]}" if rt in DEPRECATED_RUNTIMES else "✅ Supported"
        print(f"  {rt:<25} {cnt:>5}  {act:>6}  {idl:>5}  {status}")

    # Top active functions by invocations
    top_active = sorted(active, key=lambda x: -(x["invocations"] or 0))[:15]
    print(f"\n📊 Top Active Functions by Invocations (last {CW_DAYS} days):")
    print(f"  {'Function':<55} {'Runtime':<14} {'Mem':>6}  {'Arch':<7}  {'Invocations':>12}  {'Avg Dur':>8}  {'Errors':>7}")
    print(f"  {'-'*55} {'-'*14} {'-'*6}  {'-'*7}  {'-'*12}  {'-'*8}  {'-'*7}")
    for f in top_active:
        dur = f"{f['duration_avg_ms']:.0f}ms" if f.get("duration_avg_ms") is not None else "  N/A"
        dep_flag = " ⚠️" if f["runtime"] in DEPRECATED_RUNTIMES else ""
        print(f"  {f['name'][:55]:<55} {f['runtime']:<14} {f['memory_mb']:>5}M  {f['arch']:<7}  "
              f"{int(f['invocations']):>12,}  {dur:>8}  {int(f['errors']):>7,}{dep_flag}")

    # Opportunity type summary
    type_summary = {}
    for o in opps:
        t = o["type"]
        if t not in type_summary:
            type_summary[t] = {"count": 0, "saving": 0.0}
        type_summary[t]["count"]  += 1
        type_summary[t]["saving"] += o["saving_mo"]
    print("\n📊 Opportunities by Type:")
    print(f"  {'Type':<35} {'Count':>5}  {'Saving/mo':>12}  {'Saving/yr':>12}")
    print(f"  {'-'*35} {'-'*5}  {'-'*12}  {'-'*12}")
    for t, v in sorted(type_summary.items(), key=lambda x: -x[1]["saving"]):
        print(f"  {t:<35} {v['count']:>5}  ${v['saving']:>10.4f}  ${v['saving']*12:>10.4f}")

    sev_counts = {}
    for o in opps:
        sev_counts[o["severity"]] = sev_counts.get(o["severity"], 0) + 1
    print(f"\n🎯 Severity: " + "  ".join(f"{s}: {c}" for s, c in sev_counts.items()))

    # Detailed findings
    sorted_opps = sorted(opps, key=lambda x: x["saving_mo"], reverse=True)
    print("\n" + "─" * 72)
    print("📋 Detailed Findings (sorted by potential saving):\n")
    for i, opp in enumerate(sorted_opps, 1):
        print(f"  [{i:02d}] {opp['severity']}  —  {opp['type']}")
        print(f"       Resource : {opp['resource']}")
        if opp["saving_mo"] > 0:
            print(f"       Saving   : ${opp['saving_mo']:.4f}/month  (${opp['saving_mo']*12:.4f}/year)")
        if opp["current_mo"] > 0:
            print(f"       Current  : ${opp['current_mo']:.4f}/month")
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

    # CSV
    ts       = now.strftime("%Y%m%d_%H%M%S")
    csv_path = os.path.join(OUTPUT_DIR, f"{ts}_lambda_report.csv")
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(csv_path, "w") as f:
        f.write("account_id,region,resource,opportunity_type,severity,"
                "current_cost_usd_monthly,estimated_saving_usd_monthly,saving_pct,saving_basis,price_basis,"
                "dependency_warning,recommendation\n")
        if not opps:
            f.write(f'"{ACCOUNT_ID}","{REGION}","","no-findings","INFO",0.0000,0.0000,0.0,"actual","list","","{no_findings_desc}"\n')
        else:
            for o in opps:
                pct = (o["saving_mo"] / max(o["current_mo"], 0.0001)) * 100 if o["current_mo"] > 0 else 0
                saving_basis = "actual"
                sev = (o["severity"]
                       .replace("🔴 ", "").replace("🟠 ", "").replace("🟡 ", "")
                       .replace("🟢 ", "").replace("⚠️  ", "").replace("ℹ️  ", "").strip())
                f.write(f'"{ACCOUNT_ID}","{REGION}","{o["resource"]}",'
                        f'"{o["type"]}","{sev}",{o["current_mo"]:.4f},{o["saving_mo"]:.4f},{pct:.1f},"{saving_basis}","list",'
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
    print("\n🔍 AWS Lambda Cost Analyser")
    print(f"   Account : {ACCOUNT_ID}")
    print(f"   Region  : {REGION}")
    print(f"   Profile : {os.environ.get('AWS_PROFILE', 'default')}\n")
    session = boto3.Session()
    print("📡 Collecting Lambda + CloudWatch data...")
    data    = collect(session)
    print(f"\n🧠 Analysing {len(data['functions'])} Lambda function(s)...")
    opps    = analyse(data)
    print_report(data, opps)


if __name__ == "__main__":
    main()
