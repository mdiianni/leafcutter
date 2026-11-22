#!/usr/bin/env python3
"""
AWS Cost Analyser — EC2 Skill
Implements the analysis defined in skills/ec2.md

Corrections vs v1:
  - Uses InstanceLifecycle AWS metadata field (not tags) for Spot detection
  - Fetches live Spot prices via describe_spot_price_history
  - Queries Cost Explorer for org-level Savings Plan coverage / negation
  - Skips RI and Spot recommendations for already-Spot instances
  - Reports accurate amortized costs alongside on-demand equivalent
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
CACHE_FILE  = os.path.join(CACHE_DIR, "ec2.json")
CW_DAYS     = 15

now = datetime.now(timezone.utc)

# ── Severity ──────────────────────────────────────────────────────────────────
CRITICAL = "🔴 CRITICAL"
HIGH     = "🟠 HIGH"
MEDIUM   = "🟡 MEDIUM"
LOW      = "🟢 LOW"
DEP      = "⚠️  DEPENDENCY"
INFO     = "ℹ️  INFO"

# ── On-Demand pricing: eu-central-1 Linux ($/hr) ─────────────────────────────
ON_DEMAND = {
    "t3.nano": 0.006, "t3.micro": 0.0116, "t3.small": 0.023,
    "t3.medium": 0.046, "t3.large": 0.0832, "t3.xlarge": 0.1664, "t3.2xlarge": 0.3328,
    "t3a.nano": 0.0054, "t3a.micro": 0.0104, "t3a.small": 0.0208,
    "t3a.medium": 0.0416, "t3a.large": 0.0752, "t3a.xlarge": 0.1504, "t3a.2xlarge": 0.3008,
    "t4g.nano": 0.0046, "t4g.micro": 0.0092, "t4g.small": 0.0184,
    "t4g.medium": 0.0368, "t4g.large": 0.0672, "t4g.xlarge": 0.1344, "t4g.2xlarge": 0.2688,
    "m5.large": 0.107, "m5.xlarge": 0.214, "m5.2xlarge": 0.428, "m5.4xlarge": 0.856,
    "m5a.large": 0.104, "m5a.xlarge": 0.208, "m5a.2xlarge": 0.416, "m5a.4xlarge": 0.832,
    "m6i.large": 0.107, "m6i.xlarge": 0.214, "m6i.2xlarge": 0.428,
    "m6a.large": 0.0944, "m6a.xlarge": 0.1888, "m6a.2xlarge": 0.3776, "m6a.4xlarge": 0.7552,
    "m7i.large": 0.1028, "m7i.xlarge": 0.2056, "m7i.2xlarge": 0.4112,
    "m6g.large": 0.0808, "m6g.xlarge": 0.1616, "m6g.2xlarge": 0.3232,
    "m7g.large": 0.0864, "m7g.xlarge": 0.1728, "m7g.2xlarge": 0.3456, "m7g.4xlarge": 0.6912,
    "c5.large": 0.096, "c5.xlarge": 0.192, "c5.2xlarge": 0.384, "c5.4xlarge": 0.768,
    "c5a.large": 0.086, "c5a.xlarge": 0.172,
    "c6i.large": 0.0896, "c6i.xlarge": 0.1792, "c6i.2xlarge": 0.3584,
    "c6a.large": 0.0816, "c6a.xlarge": 0.1632, "c6a.2xlarge": 0.3264,
    "c7i.large": 0.0896, "c7i.xlarge": 0.1792, "c7i.2xlarge": 0.3584,
    "c6g.large": 0.0765, "c6g.xlarge": 0.153, "c6g.2xlarge": 0.306,
    "c7g.large": 0.0765, "c7g.xlarge": 0.153, "c7g.2xlarge": 0.306,
    "r5.large": 0.141, "r5.xlarge": 0.282, "r5.2xlarge": 0.564, "r5.4xlarge": 1.128,
    "r5a.large": 0.127, "r5a.xlarge": 0.254, "r5a.2xlarge": 0.508,
    "r6i.large": 0.141, "r6i.xlarge": 0.282, "r6i.2xlarge": 0.564,
    "r6g.large": 0.1128, "r6g.xlarge": 0.2256, "r6g.2xlarge": 0.4512,
    "r7g.large": 0.1209, "r7g.xlarge": 0.2419, "r7g.2xlarge": 0.4837,
    # ── Graviton4 (2024) — ~30% better price/perf than Graviton3 ─────────────
    "m8g.large": 0.0816, "m8g.xlarge": 0.1632, "m8g.2xlarge": 0.3264, "m8g.4xlarge": 0.6528,
    "c8g.large": 0.0729, "c8g.xlarge": 0.1459, "c8g.2xlarge": 0.2918, "c8g.4xlarge": 0.5836,
    "r8g.large": 0.1134, "r8g.xlarge": 0.2269, "r8g.2xlarge": 0.4537, "r8g.4xlarge": 0.9075,
    # ── AMD EPYC Genoa (m7a / c7a / r7a) — ~50% better than m6a/c6a/r6a ─────
    "m7a.large": 0.1188, "m7a.xlarge": 0.2376, "m7a.2xlarge": 0.4752, "m7a.4xlarge": 0.9504,
    "c7a.large": 0.1058, "c7a.xlarge": 0.2117, "c7a.2xlarge": 0.4234, "c7a.4xlarge": 0.8467,
    "r7a.large": 0.1588, "r7a.xlarge": 0.3175, "r7a.2xlarge": 0.6350, "r7a.4xlarge": 1.2700,
    # ── Intel Flex (burstable baseline, cheaper for lightly-used workloads) ───
    "m7i-flex.large": 0.0854, "m7i-flex.xlarge": 0.1709, "m7i-flex.2xlarge": 0.3418,
    "c7i-flex.large": 0.0762, "c7i-flex.xlarge": 0.1524, "c7i-flex.2xlarge": 0.3048,
}

RI_1YR_DISCOUNT = 0.38   # conservative 38% off on-demand for 1yr no-upfront

# Graviton equivalents (x86/AMD family → Graviton family)
GRAVITON_MAP = {
    "t3": "t4g", "t3a": "t4g",
    "m5": "m7g", "m5a": "m7g", "m6i": "m7g", "m6a": "m7g",
    "c5": "c7g", "c5a": "c7g", "c6i": "c7g", "c6a": "c7g",
    "r5": "r7g", "r5a": "r7g", "r6i": "r7g",
    # Graviton3 → Graviton4 upgrades
    "m7g": "m8g", "m6g": "m8g",
    "c7g": "c8g", "c6g": "c8g",
    "r7g": "r8g", "r6g": "r8g",
    # x86 → Graviton4 (skip gen, better savings)
    "m7i": "m8g", "m7a": "m8g",
    "c7i": "c8g",
    "r7i": "r8g",
}

# Safe same-arch newer family upgrades
NEWER_FAMILY_MAP = {
    "m5": "m6a", "m5a": "m6a",
    "c5": "c6a",
    "r5": "r6i",
    "t3": "t3a",
    "m6a": "m7a", "m6i": "m7a",
    "c6a": "c7a", "c6i": "c7a",
    "r6a": "r7a", "r6i": "r7a",
}

SIZE_ORDER = ["nano", "micro", "small", "medium", "large", "xlarge",
              "2xlarge", "4xlarge", "8xlarge", "12xlarge", "16xlarge", "24xlarge"]

# ── Cache helpers ─────────────────────────────────────────────────────────────
# ── Pricing helpers ───────────────────────────────────────────────────────────
def od_price(itype):    return ON_DEMAND.get(itype)
def graviton_type(itype):
    fam, sz = itype.rsplit(".", 1)
    gfam = GRAVITON_MAP.get(fam)
    c = f"{gfam}.{sz}" if gfam else None
    return c if c and od_price(c) else None

def newer_x86_type(itype):
    fam, sz = itype.rsplit(".", 1)
    nfam = NEWER_FAMILY_MAP.get(fam)
    c = f"{nfam}.{sz}" if nfam else None
    return c if c and od_price(c) else None

# ── CloudWatch CPU ────────────────────────────────────────────────────────────
def get_cpu_stats(cw, instance_id):
    try:
        r = cw.get_metric_statistics(
            Namespace="AWS/EC2", MetricName="CPUUtilization",
            Dimensions=[{"Name": "InstanceId", "Value": instance_id}],
            StartTime=now - timedelta(days=CW_DAYS), EndTime=now,
            Period=3600, Statistics=["Average", "Maximum"],
        )
        pts = r.get("Datapoints", [])
        if not pts:
            return None, None, None
        avgs = [p["Average"] for p in pts]
        maxs = [p["Maximum"] for p in pts]
        p95  = sorted(avgs)[min(int(len(avgs) * 0.95), len(avgs)-1)]
        return round(sum(avgs)/len(avgs), 2), round(max(maxs), 2), round(p95, 2)
    except Exception:
        return None, None, None

# ── Cost Explorer — org-level Savings Plan data ───────────────────────────────
def collect_sp_data(session):
    """
    Query CE for Savings Plan coverage and negation amounts for this account.
    SP may be purchased at the AWS Organizations management account level and
    allocated to child accounts — it does NOT appear in describe_savings_plans()
    on the child account, but IS visible in Cost Explorer as SavingsPlanNegation
    and SavingsPlanCoveredUsage record types.
    """
    ce = session.client("ce", region_name="us-east-1")
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).strftime("%Y-%m-%d")
    today       = now.strftime("%Y-%m-%d")
    days_elapsed = (now - now.replace(day=1)).days + 1

    result = {
        "has_org_sp":              False,
        "coverage_pct":            0.0,
        "od_equiv_covered":        0.0,   # what it would have cost at OD rates
        "amortized_sp_cost":       0.0,   # actual amortized cost under SP
        "sp_savings_mtd":          0.0,   # month-to-date savings
        "sp_savings_monthly_est":  0.0,   # extrapolated full-month saving
        "uncovered_od_spend":      0.0,   # on-demand spend NOT covered by SP
        "effective_discount_pct":  0.0,
        "days_elapsed":            days_elapsed,
        "raw_record_types":        {},
    }

    # 1. Record-type breakdown — finds SavingsPlanCoveredUsage + SavingsPlanNegation
    try:
        r = ce.get_cost_and_usage(
            TimePeriod={"Start": month_start, "End": today},
            Granularity="MONTHLY",
            Metrics=["UnblendedCost", "AmortizedCost"],
            GroupBy=[{"Type": "DIMENSION", "Key": "RECORD_TYPE"}],
        )
        for period in r["ResultsByTime"]:
            for g in period["Groups"]:
                rec_type  = g["Keys"][0]
                unblended = float(g["Metrics"]["UnblendedCost"]["Amount"])
                amortized = float(g["Metrics"]["AmortizedCost"]["Amount"])
                result["raw_record_types"][rec_type] = {
                    "unblended": unblended, "amortized": amortized
                }
    except Exception as e:
        result["ce_error"] = str(e)

    rtypes = result["raw_record_types"]

    # SavingsPlanCoveredUsage: on-demand equivalent of SP-covered compute
    if "SavingsPlanCoveredUsage" in rtypes:
        result["has_org_sp"]       = True
        od_equiv   = rtypes["SavingsPlanCoveredUsage"]["unblended"]   # OD rate
        amort_cost = rtypes["SavingsPlanCoveredUsage"]["amortized"]   # SP rate
        result["od_equiv_covered"] = od_equiv
        result["amortized_sp_cost"]= amort_cost
        result["sp_savings_mtd"]   = od_equiv - amort_cost
        # Extrapolate to full month
        result["sp_savings_monthly_est"] = (result["sp_savings_mtd"] / max(days_elapsed, 1)) * 30
        if od_equiv > 0:
            result["effective_discount_pct"] = (result["sp_savings_mtd"] / od_equiv) * 100

    # 2. SP coverage API — use OnDemandCost for the true SP-eligible uncovered spend.
    # OnDemandCost here is ONLY SP-eligible services (EC2, Lambda, ECS Fargate) that
    # ran at full OD rates without coverage. This is what the AWS console shows.
    # Do NOT use rtypes["Usage"]["unblended"] — that includes every non-SP-eligible
    # service (RDS, S3, CloudWatch…) and inflates the gap by 10-20×.
    try:
        cov = ce.get_savings_plans_coverage(
            TimePeriod={"Start": month_start, "End": today},
            Granularity="MONTHLY",
        )
        od_uncovered_total = 0.0
        pcts = []
        for c in cov.get("SavingsPlansCoverages", []):
            cvg = c.get("Coverage", {})
            pct = cvg.get("CoveragePercentage")
            if pct:
                pcts.append(float(pct))
            od_uncovered_total += float(cvg.get("OnDemandCost") or 0)
        if pcts:
            result["coverage_pct"] = sum(pcts) / len(pcts)
        result["uncovered_od_spend"] = od_uncovered_total
    except Exception:
        pass

    return result

# ── Data collection ───────────────────────────────────────────────────────────
def collect_spot_prices(session, instance_types):
    """Fetch the most recent Spot price per instance type in the region."""
    ec2 = session.client("ec2", region_name=REGION)
    spot_prices = {}
    try:
        r = ec2.describe_spot_price_history(
            InstanceTypes=list(instance_types),
            ProductDescriptions=["Linux/UNIX"],
            StartTime=now - timedelta(hours=6),
        )
        for item in r.get("SpotPriceHistory", []):
            itype = item["InstanceType"]
            if itype not in spot_prices:
                spot_prices[itype] = float(item["SpotPrice"])
    except Exception:
        pass
    return spot_prices


def _is_spot(inst):
    """Return True if instance is a Spot instance.

    Checks both InstanceLifecycle and SpotInstanceRequestId:
    - InstanceLifecycle == "spot"  Direct Spot request / ASG mixed-instances policy
    - SpotInstanceRequestId set    Authoritative: AWS only populates for Spot instances
    Note: InstanceLifecycle == "fleet" means on-demand via EC2 Fleet (NOT Spot);
    Spot-via-fleet still reports InstanceLifecycle = "spot" per AWS API contract.
    """
    return (
        inst.get("InstanceLifecycle", "") == "spot"
        or bool(inst.get("SpotInstanceRequestId"))
    )


def collect(session):
    cached = load_cache(CACHE_FILE)
    if cached:
        print("  ✓ Using cached EC2 data")
        return cached

    ec2 = session.client("ec2", region_name=REGION)
    cw  = session.client("cloudwatch", region_name=REGION)

    print("  → Fetching instances (running + stopped)...")
    paginator = ec2.get_paginator("describe_instances")
    instances = []
    for page in paginator.paginate(Filters=[
        {"Name": "instance-state-name", "Values": ["running", "stopped"]}
    ]):
        for r in page["Reservations"]:
            instances.extend(r["Instances"])
    print(f"  → {len(instances)} instances found.")

    # Identify which types need Spot prices.
    spot_count   = sum(1 for i in instances if _is_spot(i))
    print(f"  → Spot instances detected: {spot_count}/{len(instances)}")
    running_types = set(i["InstanceType"] for i in instances if _is_spot(i))
    print(f"  → Fetching current Spot prices for: {sorted(running_types)}")
    spot_prices = collect_spot_prices(session, running_types)
    for t, p in spot_prices.items():
        print(f"     {t}: ${p:.4f}/hr  (OD: ${od_price(t) or '?'}/hr)")

    print("  → Fetching Reserved Instances...")
    ri_resp = ec2.describe_reserved_instances(
        Filters=[{"Name": "state", "Values": ["active"]}])
    reserved = ri_resp.get("ReservedInstances", [])
    print(f"  → {len(reserved)} active RIs found.")

    # NOTE: describe_savings_plans() only shows SP *owned* by this account.
    # Org-level SP is collected separately via Cost Explorer (collect_sp_data).
    print("  → Querying Cost Explorer for org-level Savings Plan coverage...")
    sp_data = collect_sp_data(session)
    if sp_data["has_org_sp"]:
        print(f"     ✓ Org SP detected — coverage {sp_data['coverage_pct']:.1f}%, "
              f"saving ~${sp_data['sp_savings_monthly_est']:.0f}/month est.")
    else:
        print("     ⚠️  No org-level Savings Plan coverage detected.")

    print(f"  → Collecting CloudWatch CPU for {len(instances)} instances "
          f"(last {CW_DAYS} days)...")

    def enrich(inst):
        iid     = inst["InstanceId"]
        tags    = {t["Key"]: t["Value"] for t in inst.get("Tags", [])}
        # ── Spot detection: check InstanceLifecycle AND SpotInstanceRequestId ───
        # InstanceLifecycle = "spot"  → direct Spot request (always reliable)
        # SpotInstanceRequestId set   → authoritative: AWS only sets this for Spot
        # Note: InstanceLifecycle = "fleet" → on-demand via EC2 Fleet (NOT Spot)
        is_spot = _is_spot(inst)
        itype   = inst["InstanceType"]
        age     = (now - inst["LaunchTime"]).days if inst["State"]["Name"] == "running" else None
        avg, mx, p95 = get_cpu_stats(cw, iid)
        # Cost: use live Spot price for spot instances, OD otherwise
        if is_spot:
            price_hr = spot_prices.get(itype, (od_price(itype) or 0) * 0.30)
        else:
            price_hr = od_price(itype) or 0
        return {
            "id":          iid,
            "type":        itype,
            "state":       inst["State"]["Name"],
            "arch":        inst.get("Architecture", "x86_64"),
            "az":          inst["Placement"]["AvailabilityZone"],
            "launch":      inst["LaunchTime"].isoformat(),
            "age_days":    age,
            "is_spot":     is_spot,
            "price_hr":    price_hr,         # actual effective price/hr
            "od_price_hr": od_price(itype),  # on-demand equivalent
            "tags":        tags,
            "name":        tags.get("Name", ""),
            "env":         tags.get("Environment", tags.get("Env", "")),
            "eks_cluster": tags.get("aws:eks:cluster-name", ""),
            "platform":    inst.get("Platform", "Linux"),
            "cpu_avg":     avg,
            "cpu_max":     mx,
            "cpu_p95":     p95,
        }

    enriched = []
    with ThreadPoolExecutor(max_workers=15) as executor:
        futures = {executor.submit(enrich, i): i["InstanceId"] for i in instances}
        for future in as_completed(futures):
            enriched.append(future.result())
            sys.stdout.write(f"\r  → Enriched {len(enriched)}/{len(instances)}   ")
            sys.stdout.flush()
    print()

    data = {
        "instances":  enriched,
        "reserved":   reserved,
        "sp_data":    sp_data,
    }
    save_cache(CACHE_FILE, data)
    print(f"  ✓ Data cached → {CACHE_FILE}")
    return data

# ── Analysis ──────────────────────────────────────────────────────────────────
def analyse(data):
    instances = data["instances"]
    reserved  = data["reserved"]
    sp_data   = data["sp_data"]
    opps = []

    running = [i for i in instances if i["state"] == "running"]
    stopped = [i for i in instances if i["state"] == "stopped"]
    spot_instances  = [i for i in running if i["is_spot"]]
    od_instances    = [i for i in running if not i["is_spot"]]

    # ── Opportunity 0: Savings Plan coverage (org-level) ──────────────────────
    if sp_data["has_org_sp"]:
        # SP IS active from org. Show coverage and flag any uncovered OD spend.
        # uncovered_od_spend is now sourced from get_savings_plans_coverage()
        # OnDemandCost — SP-eligible services only (EC2/Lambda/Fargate).
        uncovered = sp_data["uncovered_od_spend"]
        uncovered_mo = round(uncovered / max(sp_data["days_elapsed"], 1) * 30, 2)
        # Only raise a gap opportunity when coverage is materially below 85% AND
        # the uncovered monthly spend is meaningful (>$500/month after extrapolation).
        if sp_data["coverage_pct"] < 85.0 and uncovered_mo > 500:
            opps.append({
                "resource_id": "ACCOUNT",
                "name":        "Org SP: Uncovered On-Demand Compute",
                "type":        "savings-plan-gap",
                "severity":    HIGH,
                "current_mo":  uncovered_mo,
                "saving_mo":   round(uncovered_mo * RI_1YR_DISCOUNT, 2),
                "dep_warning": "",
                "recommendation": (
                    f"Org-level Savings Plans cover {sp_data['coverage_pct']:.1f}% of eligible compute "
                    f"(EC2, Lambda, ECS Fargate). ${uncovered_mo:,.2f}/month of SP-eligible spend "
                    f"runs at full On-Demand rates. A 1-year no-upfront Compute Savings Plan "
                    f"would save ~${uncovered_mo*RI_1YR_DISCOUNT:,.2f}/month (~{RI_1YR_DISCOUNT*100:.0f}%). "
                    f"Escalate to the AWS Organizations management account to increase SP commitment."
                ),
            })
        else:
            # Coverage is essentially complete — informational only
            opps.append({
                "resource_id": "ACCOUNT",
                "name":        "Org SP: Active & Well-Covered",
                "type":        "savings-plan-active",
                "severity":    INFO,
                "current_mo":  0.0,
                "saving_mo":   0.0,
                "dep_warning": "",
                "recommendation": (
                    f"Org-level Savings Plans are active on this account. "
                    f"Coverage: {sp_data['coverage_pct']:.1f}% of eligible compute (EC2, Lambda, ECS Fargate). "
                    f"Effective discount on covered usage: {sp_data['effective_discount_pct']:.1f}%. "
                    f"SP savings applied MTD: ${sp_data['sp_savings_mtd']:,.2f} "
                    f"(est. ${sp_data['sp_savings_monthly_est']:,.0f}/month, "
                    f"${sp_data['sp_savings_monthly_est']*12:,.0f}/year). "
                    f"Uncovered SP-eligible spend (MTD): ${sp_data['uncovered_od_spend']:,.2f}."
                ),
            })
    else:
        # No org SP at all — flag as critical
        total_od_monthly = sum((i["od_price_hr"] or 0) * 730 for i in od_instances)
        saving_est       = total_od_monthly * RI_1YR_DISCOUNT
        opps.append({
            "resource_id": "ACCOUNT",
            "name":        "No Compute Savings Plan",
            "type":        "compute-savings-plan",
            "severity":    CRITICAL,
            "current_mo":  round(total_od_monthly, 2),
            "saving_mo":   round(saving_est, 2),
            "dep_warning": "",
            "recommendation": (
                f"No Savings Plans coverage detected (neither in this account nor from AWS Org). "
                f"{len(od_instances)} On-Demand instances running at ~${total_od_monthly:.2f}/month. "
                f"A 1-year no-upfront Compute Savings Plan would save ~${saving_est:.2f}/month (~38%). "
                f"Check AWS Cost Explorer → Savings Plans → Recommendations."
            ),
        })

    # ── RI gap for ON-DEMAND instances (skip Spot — already discounted) ───────
    ri_remaining = {}
    for r in reserved:
        key = (r["InstanceType"], r.get("Scope", "Region"))
        ri_remaining[key] = ri_remaining.get(key, 0) + r["InstanceCount"]

    for inst in od_instances:
        iid   = inst["id"]
        itype = inst["type"]
        name  = inst["name"] or iid
        age   = inst["age_days"] or 0
        price = inst["od_price_hr"]
        if not price:
            continue

        # Check RI coverage
        has_ri = False
        for key in ((itype, "Region"), (itype, inst["az"])):
            if ri_remaining.get(key, 0) > 0:
                ri_remaining[key] -= 1
                has_ri = True
                break

        if not has_ri and age > 30 and not sp_data["has_org_sp"]:
            # Skip RI recommendations when org Compute SP is active: the SP already
            # provides ~38% discount on this compute. Recommending RIs on top would
            # double-count the saving. Address coverage gaps via sp-coverage-gap instead.
            ri_saving  = price * RI_1YR_DISCOUNT * 730
            monthly_od = price * 730
            opps.append({
                "resource_id": iid,
                "name":        name,
                "type":        "reserved-instance",
                "severity":    CRITICAL if age > 90 else HIGH,
                "current_mo":  round(monthly_od, 2),
                "saving_mo":   round(ri_saving, 2),
                "dep_warning": "",
                "recommendation": (
                    f"Instance {iid} ({itype}, '{name}') is On-Demand and has been running "
                    f"for {age} days at ~${monthly_od:.2f}/month. "
                    f"A 1-year no-upfront RI saves ~${ri_saving:.2f}/month ({RI_1YR_DISCOUNT*100:.0f}% off). "
                    f"Note: Spot/EKS nodes are excluded from this check. "
                    f"If org Savings Plans are being expanded, prioritise these instances."
                ),
            })

    # ── Per-instance: newer x86 family, Graviton, right-sizing ───────────────
    # These apply to BOTH Spot and OD instances (architecture savings are additive
    # with any pricing model — a Graviton Spot node is cheaper than an x86 Spot node)
    for inst in running:
        iid    = inst["id"]
        itype  = inst["type"]
        name   = inst["name"] or iid
        price  = inst["price_hr"]          # actual price (spot or OD)
        od_p   = inst["od_price_hr"] or 0  # OD equivalent
        is_sp  = inst["is_spot"]
        ignore = inst["tags"].get("cost-analyser:ignore", "").lower() == "true"
        if ignore or not od_p:
            continue

        monthly = price * 730

        # ── Newer x86/AMD family (safe, no recompile) ────────────────────────
        newer = newer_x86_type(itype)
        if newer and not is_sp:   # for Spot nodes the instance type is set by the fleet
            newer_od = od_price(newer)
            if newer_od and newer_od < od_p:
                saving = (od_p - newer_od) * 730
                pct    = (od_p - newer_od) / od_p * 100
                opps.append({
                    "resource_id": iid, "name": name,
                    "type": "instance-family-upgrade", "severity": MEDIUM,
                    "current_mo": round(monthly, 2), "saving_mo": round(saving, 2),
                    "dep_warning": "",
                    "recommendation": (
                        f"Instance {iid} ({itype}, '{name}') — migrating to {newer} "
                        f"saves ~${saving:.2f}/month ({pct:.0f}% cheaper at OD rate). "
                        f"Safe stop/start change; no recompile needed."
                    ),
                })

        # ── Graviton (⚠️ dependency — ARM64) ──────────────────────────────────
        grav = graviton_type(itype)
        if grav:
            grav_od = od_price(grav)
            if grav_od and grav_od < od_p:
                # For Spot: saving is proportional to the Graviton spot discount vs current spot
                # Approximate: Graviton Spot is ~same % below Graviton OD as x86 Spot is below x86 OD
                if is_sp and od_p:
                    spot_discount = 1 - (price / od_p)
                    grav_spot_est = grav_od * (1 - spot_discount)
                    saving = (price - grav_spot_est) * 730
                else:
                    saving = (od_p - grav_od) * 730
                pct = (od_p - grav_od) / od_p * 100
                is_eks = bool(inst["eks_cluster"])
                dep_msg = (
                    "All container images must be built for linux/arm64. "
                    + ("For EKS: create a new AL2_ARM_64 node group, validate workloads, "
                       "then drain x86 nodes."
                       if is_eks else
                       "Validate native extensions and compiled code support ARM64.")
                )
                opps.append({
                    "resource_id": iid, "name": name,
                    "type": "graviton-migration", "severity": DEP,
                    "current_mo": round(monthly, 2), "saving_mo": round(saving, 2),
                    "dep_warning": dep_msg,
                    "recommendation": (
                        f"Instance {iid} ({itype}, '{name}') → {grav}: "
                        f"~${saving:.2f}/month saving (~{pct:.0f}% OD-rate delta). "
                        + (f"Pricing basis: {'Spot-adjusted estimate' if is_sp else 'On-Demand rate'}. "
                           f"EKS cluster: {inst['eks_cluster']}." if is_eks else "")
                        + f" ⚠️ ARM64 build required."
                    ),
                })

        # ── Right-sizing (CPU) ─────────────────────────────────────────────────
        avg_cpu = inst.get("cpu_avg")
        max_cpu = inst.get("cpu_max")
        p95_cpu = inst.get("cpu_p95")
        if avg_cpu is not None and max_cpu is not None:
            fam, sz = itype.rsplit(".", 1)
            if sz in SIZE_ORDER and SIZE_ORDER.index(sz) > 0:
                smaller_sz   = SIZE_ORDER[SIZE_ORDER.index(sz) - 1]
                smaller_type = f"{fam}.{smaller_sz}"
                smaller_od   = od_price(smaller_type)
                if smaller_od:
                    saving_od = (od_p - smaller_od) * 730
                    if avg_cpu < 10 and (p95_cpu or 0) < 25:
                        opps.append({
                            "resource_id": iid, "name": name,
                            "type": "right-sizing", "severity": HIGH,
                            "current_mo": round(monthly, 2), "saving_mo": round(saving_od, 2),
                            "dep_warning": "",
                            "recommendation": (
                                f"Instance {iid} ({itype}, '{name}'): avg CPU {avg_cpu:.1f}%, "
                                f"p95 {p95_cpu:.1f}%, max {max_cpu:.1f}% over {CW_DAYS} days. "
                                f"Strong downsize candidate → {smaller_type}. "
                                f"OD-rate saving: ~${saving_od:.2f}/month. "
                                f"Validate memory and network I/O before resizing."
                            ),
                        })
                    elif avg_cpu < 20 and (p95_cpu or 0) < 40:
                        opps.append({
                            "resource_id": iid, "name": name,
                            "type": "right-sizing", "severity": MEDIUM,
                            "current_mo": round(monthly, 2), "saving_mo": round(saving_od, 2),
                            "dep_warning": "",
                            "recommendation": (
                                f"Instance {iid} ({itype}, '{name}'): avg CPU {avg_cpu:.1f}%, "
                                f"p95 {p95_cpu:.1f}%, max {max_cpu:.1f}% over {CW_DAYS} days. "
                                f"Consider evaluating downsize → {smaller_type}. "
                                f"OD-rate saving: ~${saving_od:.2f}/month."
                            ),
                        })

    # ── Stopped instances ─────────────────────────────────────────────────────
    for inst in stopped:
        opps.append({
            "resource_id": inst["id"], "name": inst["name"] or inst["id"],
            "type": "stopped-instance", "severity": MEDIUM,
            "current_mo": 0.0, "saving_mo": 0.0, "dep_warning": "",
            "recommendation": (
                f"Instance {inst['id']} ({inst['type']}, '{inst['name']}') is stopped. "
                f"Attached EBS volumes still incur charges. Terminate and snapshot if no longer needed."
            ),
        })

    return opps

# ── Reporting ─────────────────────────────────────────────────────────────────
def print_report(data, opps):
    instances = data["instances"]
    sp_data   = data["sp_data"]
    reserved  = data["reserved"]
    resources_evaluated = len(instances)
    no_findings_desc = (
        f"No resources found (0 EC2 instances in {REGION})."
        if resources_evaluated == 0
        else f"Evaluated {resources_evaluated} EC2 instance(s). No cost-saving opportunities found."
    )

    running  = [i for i in instances if i["state"] == "running"]
    stopped  = [i for i in instances if i["state"] == "stopped"]
    spot_ins = [i for i in running if i["is_spot"]]
    od_ins   = [i for i in running if not i["is_spot"]]

    spot_cost_mo  = sum(i["price_hr"] * 730 for i in spot_ins)
    od_cost_mo    = sum((i["od_price_hr"] or 0) * 730 for i in od_ins)
    total_cost_mo = spot_cost_mo + od_cost_mo

    real_opps    = [o for o in opps if o["type"] not in ("stopped-instance", "savings-plan-active")]
    info_opps    = [o for o in opps if o["type"] == "savings-plan-active"]
    stopped_opps = [o for o in opps if o["type"] == "stopped-instance"]
    real_sorted  = sorted(real_opps, key=lambda x: x["saving_mo"], reverse=True)

    # Summary box
    print()
    print(BORDER_TOP)
    print(_bl("           AWS Cost Analyser — EC2 Analysis Results"))
    print(BORDER_MID)
    print(_bl(f"  Account  : {ACCOUNT_ID}"))
    print(_bl(f"  Region   : {REGION}"))
    print(_bl(f"  Run at   : {now.strftime('%Y-%m-%d %H:%M UTC')}"))
    print(_bl(f"  Instances: {len(running)} running ({len(spot_ins)} Spot / {len(od_ins)} On-Demand) · {len(stopped)} stopped"))
    print(_bl(f"  Org Savings Plans  : {'Active ✅' if sp_data['has_org_sp'] else 'NOT DETECTED ⚠️'}"))
    print(_bl(f"  Reserved Instances : {'NONE ⚠️' if not reserved else str(len(reserved)) + ' active'}"))
    print(BORDER_MID)
    print(_bl(f"  💸 Est. Spot compute cost        : ${spot_cost_mo:>8.2f} / month"))
    print(_bl(f"  💸 Est. On-Demand compute cost   : ${od_cost_mo:>8.2f} / month"))
    print(_bl(f"  💸 Total compute (effective est.): ${total_cost_mo:>8.2f} / month"))
    if sp_data["has_org_sp"]:
        print(_bl(f"  ✅ Org SP savings (MTD actual)    : ${sp_data['sp_savings_mtd']:>8.2f} (est. ${sp_data['sp_savings_monthly_est']:.0f}/mo)"))
        print(_bl(f"  ✅ SP effective discount          : {sp_data['effective_discount_pct']:>7.1f}% on covered usage"))
        print(_bl(f"  ✅ SP coverage                    : {sp_data['coverage_pct']:>7.1f}% of eligible compute"))
    print(BORDER_MID)
    print(_bl(f"  💰 Additional savings potential  : ${sum(o['saving_mo'] for o in real_opps):>8.2f} / month"))
    print(_bl(f"  💰 Annualised additional savings : ${sum(o['saving_mo'] for o in real_opps)*12:>8.2f} / year"))
    print(_bl(f"  🔍 Opportunities found           : {len(real_opps)}"))
    print(BORDER_BOT)
    if not opps:
        print(f"\n✅ {no_findings_desc}")

    # Org SP info panel
    if info_opps:
        print()
        print("ℹ️  Org-Level Savings Plans Status:")
        for o in info_opps:
            words = o["recommendation"].split()
            line = "   "
            for w in words:
                if len(line) + len(w) + 1 > 74:
                    print(line); line = "   " + w + " "
                else:
                    line += w + " "
            if line.strip(): print(line)

    # Instance inventory
    print("\n📦 Instance Inventory:")
    type_counts = {}
    for i in running:
        k = i["type"]
        if k not in type_counts:
            type_counts[k] = {"spot": 0, "od": 0, "spot_mo": 0.0, "od_mo": 0.0, "eks": 0}
        if i["is_spot"]:
            type_counts[k]["spot"] += 1
            type_counts[k]["spot_mo"] += i["price_hr"] * 730
        else:
            type_counts[k]["od"] += 1
            type_counts[k]["od_mo"] += (i["od_price_hr"] or 0) * 730
        if i["eks_cluster"]:
            type_counts[k]["eks"] += 1

    print(f"  {'Type':<14} {'Spot':>4} {'OD':>4}  {'Spot $/mo':>10}  {'OD $/mo':>8}  {'Graviton?'}")
    print(f"  {'-'*14} {'-'*4} {'-'*4}  {'-'*10}  {'-'*8}  {'-'*22}")
    for t, v in sorted(type_counts.items(), key=lambda x: -(x[1]["spot_mo"]+x[1]["od_mo"])):
        grav  = graviton_type(t)
        newer = newer_x86_type(t)
        upg   = f"→ {grav} (⚠️ ARM)" if grav else (f"→ {newer} ✅" if newer else "—")
        print(f"  {t:<14} {v['spot']:>4} {v['od']:>4}  ${v['spot_mo']:>9.2f}  ${v['od_mo']:>7.2f}  {upg}")

    # CPU utilisation
    cpu_data = [(i["id"], i["type"], i["name"], i["is_spot"], i["cpu_avg"], i["cpu_max"], i["cpu_p95"])
                for i in running if i.get("cpu_avg") is not None]
    if cpu_data:
        print(f"\n📊 CPU Utilisation (last {CW_DAYS} days):")
        print(f"  {'ID':<21} {'Type':<14} {'Mode':<6} {'Name':<32} {'Avg%':>5} {'Max%':>5} {'p95%':>5}")
        print(f"  {'-'*21} {'-'*14} {'-'*6} {'-'*32} {'-'*5} {'-'*5} {'-'*5}")
        for iid, itype, iname, is_sp, avg, mx, p95 in sorted(cpu_data, key=lambda x: x[4] or 99):
            mode = "Spot" if is_sp else "OD"
            flag = " 🔴" if (avg or 0) < 10 else (" 🟠" if (avg or 0) < 20 else "")
            print(f"  {iid:<21} {itype:<14} {mode:<6} {iname[:32]:<32} {avg:>5.1f} {mx:>5.1f} {p95:>5.1f}{flag}")

    # Opportunities by type
    type_summary = {}
    for o in real_opps:
        t = o["type"]
        if t not in type_summary:
            type_summary[t] = {"count": 0, "saving": 0.0}
        type_summary[t]["count"]  += 1
        type_summary[t]["saving"] += o["saving_mo"]

    print("\n📊 Additional Opportunities by Type:")
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
        print(f"       Resource : {opp['resource_id']}  ({opp['name'][:48]})")
        if opp["saving_mo"] > 0:
            print(f"       Saving   : ${opp['saving_mo']:.2f}/month  (${opp['saving_mo']*12:.2f}/year)")
        if opp["current_mo"] > 0:
            print(f"       Current  : ${opp['current_mo']:.2f}/month (effective est.)")
        if opp["dep_warning"]:
            print(f"       ⚠️  Dep   : {opp['dep_warning'][:68]}")
        words = opp["recommendation"].split()
        line  = "       "
        for w in words:
            if len(line) + len(w) + 1 > 74:
                print(line); line = "       " + w + " "
            else:
                line += w + " "
        if line.strip(): print(line)
        print()

    if stopped_opps:
        print(f"\n⏹  Stopped Instances ({len(stopped_opps)}) — attached EBS still billed:")
        for o in stopped_opps:
            print(f"   • {o['resource_id']}  {o['name'][:50]}")

    # CSV export
    ts = now.strftime("%Y%m%d_%H%M%S")
    csv_path = os.path.join(OUTPUT_DIR, f"{ts}_ec2_report.csv")
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(csv_path, "w") as f:
        f.write("account_id,region,resource_id,resource_name,opportunity_type,severity,"
                "current_cost_usd_monthly,estimated_saving_usd_monthly,saving_pct,saving_basis,price_basis,"
                "dependency_warning,recommendation\n")
        if not opps:
            f.write(f'"{ACCOUNT_ID}","{REGION}","","","no-findings","INFO",0.00,0.00,0.0,"actual","list","","{no_findings_desc}"\n')
        else:
            for o in opps:
                pct = (o["saving_mo"] / max(o["current_mo"], 0.01)) * 100 if o["current_mo"] > 0 else 0
                saving_basis = "actual"
                sev = o["severity"].replace("🔴 ","").replace("🟠 ","").replace("🟡 ","").replace("🟢 ","").replace("⚠️  ","").replace("ℹ️  ","").strip()
                f.write(f'"{ACCOUNT_ID}","{REGION}","{o["resource_id"]}","{o["name"]}",'
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
    print("\n🔍 AWS EC2 Cost Analyser")
    print(f"   Account : {ACCOUNT_ID}")
    print(f"   Region  : {REGION}")
    print(f"   Profile : {os.environ.get('AWS_PROFILE', 'default')}\n")
    session = boto3.Session()
    print("📡 Collecting EC2 + Cost Explorer data...")
    data    = collect(session)
    print(f"\n🧠 Analysing {len(data['instances'])} instances...")
    opps    = analyse(data)
    print_report(data, opps)


if __name__ == "__main__":
    main()
