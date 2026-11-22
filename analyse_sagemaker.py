#!/usr/bin/env python3
"""
AWS Cost Analyser — SageMaker Skill

Key behaviours:
  - Notebook instances: idle detection via CloudWatch CPUUtilization (15-day window)
  - Endpoints: idle detection via CloudWatch Invocations (7-day window)
  - Training jobs: On-Demand vs Spot usage in the last 30 days
  - Graviton inference: ml.m5/c5/r5 endpoint instance types with cheaper ml.m6g/c6g alternatives
  - Studio Domain idle kernel apps via ListApps + LastUserActivityTimestamp
  - Oversized notebooks: ml.m5.xlarge+ with low CPU when ml.t3.medium would suffice
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
REGION     = os.environ.get("AWS_DEFAULT_REGION", "eu-central-1")
ACCOUNT_ID = boto3.Session().client("sts").get_caller_identity()["Account"]
CACHE_DIR  = os.path.join(os.path.dirname(__file__), "cache", ACCOUNT_ID, REGION)
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "output")
CACHE_FILE = os.path.join(CACHE_DIR, "sagemaker.json")

now = datetime.now(timezone.utc)

# ── Severity ──────────────────────────────────────────────────────────────────
CRITICAL = "🔴 CRITICAL"
HIGH     = "🟠 HIGH"
MEDIUM   = "🟡 MEDIUM"
LOW      = "🟢 LOW"
INFO     = "ℹ️  INFO"

# ── Pricing (eu-central-1 On-Demand per hour) ─────────────────────────────────
# Source: https://aws.amazon.com/sagemaker/pricing/
NOTEBOOK_PRICE_HR = {
    "ml.t3.medium":  0.059,
    "ml.t3.large":   0.118,
    "ml.t3.xlarge":  0.236,
    "ml.m5.xlarge":  0.269,
    "ml.m5.2xlarge": 0.538,
    "ml.m5.4xlarge": 1.075,
    "ml.m6g.xlarge": 0.227,
    "ml.m6g.2xlarge":0.454,
    "ml.p3.2xlarge": 4.234,
    "ml.p3.8xlarge": 16.934,
}
ENDPOINT_PRICE_HR = {
    "ml.t2.medium":  0.064,
    "ml.m5.large":   0.134,
    "ml.m5.xlarge":  0.269,
    "ml.c5.large":   0.119,
    "ml.c5.xlarge":  0.238,
    "ml.m6g.large":  0.117,
    "ml.c6g.large":  0.093,
}
# Graviton equivalents for ml.m5 / ml.c5 / ml.r5 instance families
GRAVITON_MAP = {
    "ml.m5.large":   "ml.m6g.large",
    "ml.m5.xlarge":  "ml.m6g.xlarge",
    "ml.m5.2xlarge": "ml.m6g.2xlarge",
    "ml.c5.large":   "ml.c6g.large",
    "ml.c5.xlarge":  "ml.c6g.xlarge",
    "ml.r5.large":   "ml.r6g.large",
    "ml.r5.xlarge":  "ml.r6g.xlarge",
}
GRAVITON_DISCOUNT = 0.15   # 15-20% cheaper on average
SPOT_DISCOUNT     = 0.70   # SageMaker Spot training saves ~70-90%

HOURS_PER_MONTH = 730


# ── CloudWatch helpers ────────────────────────────────────────────────────────
def _cw_sum(cw, namespace, metric, dimensions, days):
    """Return total sum of a CloudWatch metric over `days` days (hourly periods)."""
    try:
        r = cw.get_metric_statistics(
            Namespace=namespace,
            MetricName=metric,
            Dimensions=dimensions,
            StartTime=now - timedelta(days=days),
            EndTime=now,
            Period=3600,
            Statistics=["Sum"],
        )
        pts = r.get("Datapoints", [])
        return sum(p["Sum"] for p in pts)
    except Exception:
        return None


def _cw_avg(cw, namespace, metric, dimensions, days):
    """Return (average, max) of a CloudWatch metric over `days` days (hourly periods)."""
    try:
        r = cw.get_metric_statistics(
            Namespace=namespace,
            MetricName=metric,
            Dimensions=dimensions,
            StartTime=now - timedelta(days=days),
            EndTime=now,
            Period=3600,
            Statistics=["Average", "Maximum"],
        )
        pts = r.get("Datapoints", [])
        if not pts:
            return None, None
        avgs = [p["Average"] for p in pts]
        maxs = [p["Maximum"] for p in pts]
        return round(sum(avgs) / len(avgs), 2), round(max(maxs), 2)
    except Exception:
        return None, None


def _fetch_notebook_metrics(cw, nb_name):
    avg, mx = _cw_avg(
        cw, "AWS/SageMaker", "CPUUtilization",
        [{"Name": "NotebookInstanceName", "Value": nb_name}],
        days=15,
    )
    return {"cpu_avg": avg, "cpu_max": mx}


def _fetch_endpoint_metrics(cw, ep_name):
    total_invocations = _cw_sum(
        cw, "AWS/SageMaker", "Invocations",
        [{"Name": "EndpointName", "Value": ep_name}],
        days=7,
    )
    return {"invocations_7d": total_invocations}


# ── Data collection ───────────────────────────────────────────────────────────
def collect(session):
    cached = load_cache(CACHE_FILE)
    if cached:
        print("  ✓ Using cached SageMaker data")
        return cached

    sm = session.client("sagemaker",    region_name=REGION)
    cw = session.client("cloudwatch",   region_name=REGION)
    ce = session.client("ce",           region_name="us-east-1")

    # ── Notebook instances ────────────────────────────────────────────────────
    print("  → Listing SageMaker notebook instances...")
    notebooks_raw = []
    nb_paginator  = sm.get_paginator("list_notebook_instances")
    for page in nb_paginator.paginate():
        notebooks_raw.extend(page["NotebookInstances"])
    print(f"  → {len(notebooks_raw)} notebook instance(s) found.")

    # ── Endpoints ─────────────────────────────────────────────────────────────
    print("  → Listing SageMaker endpoints...")
    endpoints_raw = []
    ep_paginator  = sm.get_paginator("list_endpoints")
    for page in ep_paginator.paginate():
        endpoints_raw.extend(page["Endpoints"])
    print(f"  → {len(endpoints_raw)} endpoint(s) found.")

    # ── Training jobs (last 30 days, Completed) ────────────────────────────────
    print("  → Listing completed training jobs (last 30 days)...")
    training_raw  = []
    thirty_ago    = now - timedelta(days=30)
    tj_paginator  = sm.get_paginator("list_training_jobs")
    for page in tj_paginator.paginate(
        StatusEquals="Completed",
        CreationTimeAfter=thirty_ago,
    ):
        training_raw.extend(page["TrainingJobSummaries"])
    print(f"  → {len(training_raw)} completed training job(s) in last 30 days.")

    # ── Studio domains ────────────────────────────────────────────────────────
    print("  → Listing SageMaker Studio domains...")
    domains_raw  = []
    dom_paginator = sm.get_paginator("list_domains")
    for page in dom_paginator.paginate():
        domains_raw.extend(page["Domains"])
    print(f"  → {len(domains_raw)} Studio domain(s) found.")

    # Studio apps per domain
    apps_by_domain = {}
    for domain in domains_raw:
        did   = domain["DomainId"]
        apps  = []
        app_paginator = sm.get_paginator("list_apps")
        for page in app_paginator.paginate(DomainIdEquals=did):
            apps.extend(page["Apps"])
        apps_by_domain[did] = apps

    # ── Parallel CloudWatch metrics for notebooks ─────────────────────────────
    in_service_nbs = [n for n in notebooks_raw if n.get("NotebookInstanceStatus") == "InService"]
    print(f"  → Fetching CloudWatch metrics for {len(in_service_nbs)} InService notebook(s)...")
    nb_metrics = {}
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {
            executor.submit(_fetch_notebook_metrics, cw, n["NotebookInstanceName"]): n["NotebookInstanceName"]
            for n in in_service_nbs
        }
        done = 0
        for future in as_completed(futures):
            name = futures[future]
            nb_metrics[name] = future.result()
            done += 1
            sys.stdout.write(f"\r  → {done}/{len(in_service_nbs)} notebook metrics fetched...")
            sys.stdout.flush()
    if in_service_nbs:
        print()

    # ── Parallel CloudWatch metrics for endpoints ──────────────────────────────
    in_service_eps = [e for e in endpoints_raw if e.get("EndpointStatus") == "InService"]
    print(f"  → Fetching CloudWatch metrics for {len(in_service_eps)} InService endpoint(s)...")
    ep_metrics = {}
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {
            executor.submit(_fetch_endpoint_metrics, cw, e["EndpointName"]): e["EndpointName"]
            for e in in_service_eps
        }
        done = 0
        for future in as_completed(futures):
            name = futures[future]
            ep_metrics[name] = future.result()
            done += 1
            sys.stdout.write(f"\r  → {done}/{len(in_service_eps)} endpoint metrics fetched...")
            sys.stdout.flush()
    if in_service_eps:
        print()

    # ── Build notebook records ─────────────────────────────────────────────────
    notebooks = []
    for nb in notebooks_raw:
        name         = nb["NotebookInstanceName"]
        status       = nb.get("NotebookInstanceStatus", "unknown")
        instance_type = nb.get("InstanceType", "ml.t3.medium")
        price_hr     = NOTEBOOK_PRICE_HR.get(instance_type, 0.0)
        monthly_cost = price_hr * HOURS_PER_MONTH
        m            = nb_metrics.get(name, {})
        notebooks.append({
            "name":          name,
            "status":        status,
            "instance_type": instance_type,
            "price_hr":      price_hr,
            "monthly_cost":  round(monthly_cost, 2),
            "cpu_avg":       m.get("cpu_avg"),
            "cpu_max":       m.get("cpu_max"),
        })

    # ── Build endpoint records ─────────────────────────────────────────────────
    endpoints = []
    for ep in endpoints_raw:
        name         = ep["EndpointName"]
        status       = ep.get("EndpointStatus", "unknown")
        # Instance type is on the endpoint config; we approximate from the endpoint summary
        instance_type = ep.get("InstanceType", "")
        price_hr     = ENDPOINT_PRICE_HR.get(instance_type, 0.134)  # default ml.m5.large
        monthly_cost = price_hr * HOURS_PER_MONTH
        m            = ep_metrics.get(name, {})
        endpoints.append({
            "name":           name,
            "status":         status,
            "instance_type":  instance_type,
            "price_hr":       price_hr,
            "monthly_cost":   round(monthly_cost, 2),
            "invocations_7d": m.get("invocations_7d"),
        })

    # ── Build training job records ─────────────────────────────────────────────
    training_jobs = []
    for tj in training_raw:
        name = tj["TrainingJobName"]
        # Describe to get EnableManagedSpotTraining flag
        try:
            detail = sm.describe_training_job(TrainingJobName=name)
            spot   = detail.get("EnableManagedSpotTraining", False)
            itype  = detail.get("ResourceConfig", {}).get("InstanceType", "unknown")
            icount = detail.get("ResourceConfig", {}).get("InstanceCount", 1)
        except Exception:
            spot   = False
            itype  = "unknown"
            icount = 1
        training_jobs.append({
            "name":           name,
            "instance_type":  itype,
            "instance_count": icount,
            "spot_enabled":   spot,
            "status":         tj.get("TrainingJobStatus", "Completed"),
        })

    # ── Build Studio domain / app records ─────────────────────────────────────
    domains = []
    for domain in domains_raw:
        did  = domain["DomainId"]
        dname = domain.get("DomainName", did)
        apps = apps_by_domain.get(did, [])
        # Find the most recent user activity across all apps in this domain
        last_activity = None
        kernel_apps   = []
        for app in apps:
            if app.get("AppType") in ("KernelGateway", "JupyterServer"):
                ts = app.get("LastUserActivityTimestamp")
                if ts:
                    if last_activity is None or ts > last_activity:
                        last_activity = ts
                if app.get("Status") == "InService":
                    kernel_apps.append(app)
        days_idle = None
        if last_activity:
            if hasattr(last_activity, "replace"):
                la = last_activity if last_activity.tzinfo else last_activity.replace(tzinfo=timezone.utc)
            else:
                la = last_activity
            days_idle = (now - la).days
        domains.append({
            "domain_id":        did,
            "domain_name":      dname,
            "total_apps":       len(apps),
            "active_kernel_apps": len(kernel_apps),
            "last_activity_ts": str(last_activity) if last_activity else None,
            "days_idle":        days_idle,
        })

    # ── SageMaker Savings Plans coverage ────────────────────────────────────────
    print("  → Checking SageMaker Savings Plans coverage via Cost Explorer...")
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).strftime("%Y-%m-%d")
    today       = now.strftime("%Y-%m-%d")
    sm_sp_data  = {"has_sm_sp": False, "coverage_pct": 0.0, "uncovered_mo": 0.0,
                   "sm_monthly_cost": 0.0}
    try:
        # Get SageMaker MTD cost
        sm_cost_resp = ce.get_cost_and_usage(
            TimePeriod={"Start": month_start, "End": today},
            Granularity="MONTHLY",
            Metrics=["UnblendedCost"],
            Filter={"Dimensions": {"Key": "SERVICE", "Values": ["Amazon SageMaker"]}},
        )
        sm_mtd = sum(
            float(r["Total"]["UnblendedCost"]["Amount"])
            for r in sm_cost_resp.get("ResultsByTime", [])
        )
        days_elapsed = now.day
        sm_monthly_est = (sm_mtd / max(days_elapsed, 1)) * 30
        sm_sp_data["sm_monthly_cost"] = round(sm_monthly_est, 2)

        # Get SageMaker SP coverage (SageMakerSavingsPlans)
        sp_cov_resp = ce.get_savings_plans_coverage(
            TimePeriod={"Start": month_start, "End": today},
            Granularity="MONTHLY",
            Filter={"Dimensions": {"Key": "SERVICE", "Values": ["Amazon SageMaker"]}},
        )
        od_unc = 0.0
        pcts   = []
        for c in sp_cov_resp.get("SavingsPlansCoverages", []):
            cvg = c.get("Coverage", {})
            pct = cvg.get("CoveragePercentage")
            if pct:
                pcts.append(float(pct))
                sm_sp_data["has_sm_sp"] = True
            od_unc += float(cvg.get("OnDemandCost") or 0)
        if pcts:
            sm_sp_data["coverage_pct"] = sum(pcts) / len(pcts)
        sm_sp_data["uncovered_mo"] = round((od_unc / max(days_elapsed, 1)) * 30, 2)
    except Exception as ex:
        print(f"     ⚠ CE SageMaker SP check failed: {ex}")

    data = {
        "notebooks":    notebooks,
        "endpoints":    endpoints,
        "training_jobs": training_jobs,
        "domains":      domains,
        "sm_sp_data":   sm_sp_data,
    }
    save_cache(CACHE_FILE, data)
    total = len(notebooks) + len(endpoints) + len(training_jobs) + len(domains)
    print(f"  ✓ {total} SageMaker resource(s) collected and cached.")
    return data


# ── Analysis ──────────────────────────────────────────────────────────────────
def analyse(data):
    notebooks     = data["notebooks"]
    endpoints     = data["endpoints"]
    training_jobs = data["training_jobs"]
    domains       = data["domains"]
    sm_sp_data    = data.get("sm_sp_data", {})
    opps          = []

    # ── Opp 0: SageMaker Savings Plans coverage ───────────────────────────────
    # SageMaker Savings Plans (distinct from Compute SPs) cover SageMaker ML
    # instance hours at up to ~17-20% discount (1yr no-upfront).
    SM_SP_1YR_NOUPFRONT = 0.17   # ~17% discount, 1-year no-upfront SageMaker SP
    sm_monthly = sm_sp_data.get("sm_monthly_cost", 0)
    if sm_monthly > 100:
        if sm_sp_data.get("has_sm_sp"):
            cov_pct    = sm_sp_data.get("coverage_pct", 0)
            unc_mo     = sm_sp_data.get("uncovered_mo", 0)
            if cov_pct < 80.0 and unc_mo > 100:
                gap_saving = round(unc_mo * SM_SP_1YR_NOUPFRONT, 2)
                opps.append({
                    "resource":    "ACCOUNT",
                    "type":        "sagemaker-sp-gap",
                    "severity":    MEDIUM,
                    "current_mo":  round(unc_mo, 2),
                    "saving_mo":   gap_saving,
                    "dep_warning": "",
                    "recommendation": (
                        f"SageMaker Savings Plans cover {cov_pct:.1f}% of eligible SageMaker spend. "
                        f"${unc_mo:,.2f}/month of SageMaker compute runs at On-Demand rates. "
                        f"A 1-year no-upfront SageMaker Savings Plan saves ~{SM_SP_1YR_NOUPFRONT*100:.0f}% "
                        f"(~${gap_saving:,.2f}/month). "
                        f"Review in AWS Cost Explorer → Savings Plans → Recommendations (SageMaker)."
                    ),
                })
        else:
            # No SageMaker SP at all — flag if meaningful SageMaker spend
            saving_est = round(sm_monthly * SM_SP_1YR_NOUPFRONT, 2)
            opps.append({
                "resource":    "ACCOUNT",
                "type":        "sagemaker-no-savings-plan",
                "severity":    MEDIUM,
                "current_mo":  round(sm_monthly, 2),
                "saving_mo":   saving_est,
                "dep_warning": "",
                "recommendation": (
                    f"No SageMaker Savings Plans detected. Estimated SageMaker spend: "
                    f"${sm_monthly:,.2f}/month. A 1-year no-upfront SageMaker Savings Plan "
                    f"typically saves ~{SM_SP_1YR_NOUPFRONT*100:.0f}% (~${saving_est:,.2f}/month, "
                    f"${saving_est*12:,.0f}/year). "
                    f"Check AWS Cost Explorer → Savings Plans → Recommendations."
                ),
            })

    # ── Opp 1: Notebook instances left running (idle) ─────────────────────────
    idle_nbs = [
        n for n in notebooks
        if n["status"] == "InService"
        and n["cpu_avg"] is not None
        and n["cpu_avg"] < 5.0
    ]
    for nb in idle_nbs:
        saving = nb["monthly_cost"]
        opps.append({
            "resource":    nb["name"],
            "type":        "sagemaker-idle-notebook",
            "severity":    CRITICAL,
            "current_mo":  nb["monthly_cost"],
            "saving_mo":   round(saving, 2),
            "dep_warning": (
                "Stopping a notebook instance terminates its compute but preserves files on the "
                "attached EBS volume. Any in-progress work not saved to S3 or Git may be lost "
                "if the instance is deleted rather than stopped."
            ),
            "recommendation": (
                f"Notebook '{nb['name']}' ({nb['instance_type']}, ${nb['price_hr']:.3f}/hr) "
                f"is InService with {nb['cpu_avg']:.1f}% avg CPU over 15 days. "
                f"Notebook instances bill per hour even when idle — cost is "
                f"${nb['monthly_cost']:.2f}/month. "
                f"Stop or delete this instance. For interactive development, migrate to "
                f"SageMaker Studio which charges only while a kernel is running (pay-per-use). "
                f"Estimated saving: ${saving:.2f}/month (${saving*12:.0f}/year)."
            ),
        })

    # ── Opp 2: Idle endpoints (zero invocations for 7 days) ───────────────────
    idle_eps = [
        e for e in endpoints
        if e["status"] == "InService"
        and e["invocations_7d"] is not None
        and e["invocations_7d"] == 0
    ]
    for ep in idle_eps:
        saving = ep["monthly_cost"]
        opps.append({
            "resource":    ep["name"],
            "type":        "sagemaker-idle-endpoint",
            "severity":    CRITICAL,
            "current_mo":  ep["monthly_cost"],
            "saving_mo":   round(saving, 2),
            "dep_warning": (
                "Deleting an endpoint removes real-time inference capacity immediately. "
                "Confirm no application holds a stale reference to this endpoint before removal. "
                "Re-deploying from the model registry takes seconds to minutes."
            ),
            "recommendation": (
                f"Endpoint '{ep['name']}' has received 0 invocations over the last 7 days "
                f"yet continues to incur ${ep['monthly_cost']:.2f}/month. "
                f"SageMaker real-time endpoints bill per instance-hour regardless of traffic. "
                f"Delete this endpoint and, if inference is needed on-demand, consider "
                f"SageMaker Serverless Inference (pay-per-request, 0 cost when idle) or "
                f"Asynchronous Inference for burst workloads. "
                f"Estimated saving: ${saving:.2f}/month (${saving*12:.0f}/year)."
            ),
        })

    # ── Opp 3: On-Demand training jobs (Spot not enabled) ─────────────────────
    on_demand_jobs = [tj for tj in training_jobs if not tj["spot_enabled"]]
    if on_demand_jobs:
        # Rough cost estimate: assume avg 1hr per job at ml.m5.xlarge rate ($0.269/hr)
        avg_hr_cost   = NOTEBOOK_PRICE_HR.get("ml.m5.xlarge", 0.269)
        approx_mo_cost = len(on_demand_jobs) * avg_hr_cost * 1.0   # 1hr per job estimate
        saving_mo     = round(approx_mo_cost * SPOT_DISCOUNT, 2)
        names_sample  = ", ".join(tj["name"] for tj in on_demand_jobs[:3])
        if len(on_demand_jobs) > 3:
            names_sample += f" … (+{len(on_demand_jobs)-3} more)"
        opps.append({
            "resource":    f"{len(on_demand_jobs)} training job(s)",
            "type":        "sagemaker-ondemand-training",
            "severity":    MEDIUM,
            "current_mo":  round(approx_mo_cost, 2),
            "saving_mo":   saving_mo,
            "dep_warning": (
                "SageMaker Managed Spot training requires checkpointing: training code must "
                "periodically save model state to S3 so that an interrupted job can resume "
                "from the last checkpoint rather than restart from scratch. Jobs that do not "
                "implement checkpointing may restart fully on interruption, increasing total "
                "training time."
            ),
            "recommendation": (
                f"{len(on_demand_jobs)} completed training job(s) in the last 30 days used "
                f"On-Demand compute (EnableManagedSpotTraining=False). "
                f"Enabling SageMaker Managed Spot Training reduces compute cost by 70-90% "
                f"for training workloads. Set EnableManagedSpotTraining=True and configure "
                f"a StoppingCondition.MaxWaitTimeInSeconds to cap total wall-clock time. "
                f"Jobs: {names_sample}. "
                f"Estimated monthly saving (based on job count × avg duration): "
                f"~${saving_mo:.2f}/month."
            ),
        })

    # ── Opp 4: Graviton inference endpoints ───────────────────────────────────
    graviton_candidates = [
        e for e in endpoints
        if e["status"] == "InService"
        and e["instance_type"] in GRAVITON_MAP
    ]
    for ep in graviton_candidates:
        current_hr   = ENDPOINT_PRICE_HR.get(ep["instance_type"], 0.134)
        graviton_type = GRAVITON_MAP[ep["instance_type"]]
        # Graviton equivalent at 15% discount
        graviton_hr  = current_hr * (1 - GRAVITON_DISCOUNT)
        current_mo   = current_hr * HOURS_PER_MONTH
        graviton_mo  = graviton_hr * HOURS_PER_MONTH
        saving_mo    = round(current_mo - graviton_mo, 2)
        opps.append({
            "resource":    ep["name"],
            "type":        "sagemaker-graviton-endpoint",
            "severity":    HIGH,
            "current_mo":  round(current_mo, 2),
            "saving_mo":   saving_mo,
            "dep_warning": (
                "Migrating to Graviton (ARM64) requires validating that the serving container "
                "and model framework support ARM64. TensorFlow and PyTorch DLC images are "
                "available as multi-arch. Custom containers or non-standard dependencies may "
                "require a rebuild. Test on a shadow endpoint before promoting to production."
            ),
            "recommendation": (
                f"Endpoint '{ep['name']}' uses {ep['instance_type']} "
                f"(${current_hr:.3f}/hr → ${current_mo:.2f}/month). "
                f"Switching to the Graviton equivalent {graviton_type} "
                f"(${graviton_hr:.3f}/hr → ${graviton_mo:.2f}/month) "
                f"saves ~{GRAVITON_DISCOUNT*100:.0f}% with no model changes for most "
                f"TensorFlow/PyTorch models. "
                f"Estimated saving: ${saving_mo:.2f}/month (${saving_mo*12:.0f}/year)."
            ),
        })

    # ── Opp 5: Studio Domain idle kernel apps ─────────────────────────────────
    # KernelGateway apps charge for ml.* instance types even when no notebook is open
    IDLE_STUDIO_DAYS = 7
    idle_domains = [
        d for d in domains
        if d["active_kernel_apps"] > 0
        and d["days_idle"] is not None
        and d["days_idle"] >= IDLE_STUDIO_DAYS
    ]
    for dom in idle_domains:
        # Each KernelGateway app runs at least one ml.t3.medium by default
        app_cost_mo = dom["active_kernel_apps"] * NOTEBOOK_PRICE_HR.get("ml.t3.medium", 0.059) * HOURS_PER_MONTH
        saving_mo   = round(app_cost_mo, 2)
        opps.append({
            "resource":    dom["domain_name"],
            "type":        "sagemaker-studio-idle-kernels",
            "severity":    MEDIUM,
            "current_mo":  round(app_cost_mo, 2),
            "saving_mo":   saving_mo,
            "dep_warning": (
                "Shutting down Studio apps terminates running kernels and any unsaved "
                "in-memory state. Ensure all notebook outputs are saved and any long-running "
                "training or processing jobs have been submitted to SageMaker jobs (not "
                "running in the kernel) before stopping apps."
            ),
            "recommendation": (
                f"Studio domain '{dom['domain_name']}' has "
                f"{dom['active_kernel_apps']} active KernelGateway/JupyterServer app(s) "
                f"with no user activity for {dom['days_idle']} days "
                f"(last activity: {dom['last_activity_ts'] or 'unknown'}). "
                f"Studio kernel gateway apps bill per instance-hour even when no notebook "
                f"is open. Shut down idle apps via the Studio UI or "
                f"`sagemaker.delete_app()`. Configure lifecycle policies to auto-shutdown "
                f"idle kernels after a timeout. "
                f"Estimated saving: ${saving_mo:.2f}/month (${saving_mo*12:.0f}/year)."
            ),
        })

    # ── Opp 6: Oversized notebooks ────────────────────────────────────────────
    OVERSIZED_TYPES    = {"ml.m5.xlarge", "ml.m5.2xlarge", "ml.m5.4xlarge",
                          "ml.m6g.xlarge", "ml.m6g.2xlarge",
                          "ml.p3.2xlarge", "ml.p3.8xlarge"}
    oversized_nbs = [
        n for n in notebooks
        if n["status"] == "InService"
        and n["instance_type"] in OVERSIZED_TYPES
        and n["cpu_avg"] is not None
        and n["cpu_avg"] < 10.0
    ]
    for nb in oversized_nbs:
        target_type  = "ml.t3.medium"
        target_hr    = NOTEBOOK_PRICE_HR[target_type]
        target_mo    = target_hr * HOURS_PER_MONTH
        saving_mo    = round(nb["monthly_cost"] - target_mo, 2)
        if saving_mo <= 0:
            continue
        opps.append({
            "resource":    nb["name"],
            "type":        "sagemaker-oversized-notebook",
            "severity":    MEDIUM,
            "current_mo":  nb["monthly_cost"],
            "saving_mo":   saving_mo,
            "dep_warning": (
                "Downsizing a running notebook instance requires stopping and restarting it; "
                "the instance type cannot be changed while running. Files on the EBS volume "
                "are preserved across stop/start. Confirm memory requirements before "
                "downsizing — ml.t3.medium provides 4 GB RAM."
            ),
            "recommendation": (
                f"Notebook '{nb['name']}' is a {nb['instance_type']} "
                f"(${nb['price_hr']:.3f}/hr → ${nb['monthly_cost']:.2f}/month) "
                f"but averages only {nb['cpu_avg']:.1f}% CPU over 15 days. "
                f"For interactive EDA and light development, ml.t3.medium "
                f"(${target_hr:.3f}/hr → ${target_mo:.2f}/month) is sufficient. "
                f"Stop the instance, change the instance type to {target_type}, and restart. "
                f"Reserve larger instances for GPU training or memory-intensive workloads. "
                f"Estimated saving: ${saving_mo:.2f}/month (${saving_mo*12:.0f}/year)."
            ),
        })

    return opps


# ── Reporting ─────────────────────────────────────────────────────────────────
def print_report(data, opps):
    notebooks     = data["notebooks"]
    endpoints     = data["endpoints"]
    training_jobs = data["training_jobs"]
    domains       = data["domains"]
    resources_evaluated = len(notebooks) + len(endpoints) + len(training_jobs) + len(domains)
    no_findings_desc = (
        f"No resources found (0 SageMaker resources in {REGION})."
        if resources_evaluated == 0
        else f"Evaluated {resources_evaluated} SageMaker resource(s). No cost-saving opportunities found."
    )

    nb_cost    = sum(n["monthly_cost"] for n in notebooks if n["status"] == "InService")
    ep_cost    = sum(e["monthly_cost"] for e in endpoints if e["status"] == "InService")
    total_mo   = nb_cost + ep_cost
    total_save = sum(o["saving_mo"] for o in opps)

    print()
    print(BORDER_TOP)
    print(_bl("       AWS Cost Analyser — SageMaker Analysis Results"))
    print(BORDER_MID)
    print(_bl(f"  Account            : {ACCOUNT_ID}"))
    print(_bl(f"  Region             : {REGION}"))
    print(_bl(f"  Run at             : {now.strftime('%Y-%m-%d %H:%M UTC')}"))
    print(_bl(f"  Notebook instances : {len(notebooks)}"))
    print(_bl(f"  Endpoints          : {len(endpoints)}"))
    print(_bl(f"  Training jobs (30d): {len(training_jobs)}"))
    print(_bl(f"  Studio domains     : {len(domains)}"))
    print(BORDER_MID)
    print(_bl(f"  💸 Est. notebook cost (InService)     : ${nb_cost:>8.2f} / month"))
    print(_bl(f"  💸 Est. endpoint cost (InService)     : ${ep_cost:>8.2f} / month"))
    print(_bl(f"  💸 Est. total SageMaker cost          : ${total_mo:>8.2f} / month"))
    print(_bl(f"  💰 Savings potential                  : ${total_save:>8.2f} / month"))
    print(_bl(f"  💰 Annualised savings potential       : ${total_save*12:>8.2f} / year"))
    print(_bl(f"  🔍 Opportunities found                : {len(opps)}"))
    print(BORDER_BOT)

    # Notebook table
    in_service_nbs = [n for n in notebooks if n["status"] == "InService"]
    if notebooks:
        print(f"\n📓 Notebook Instances:")
        print(f"  {'Name':<48} {'Instance Type':<18} {'Status':<12} "
              f"{'CPU%':>6} {'$/mo':>7}")
        print(f"  {'-'*48} {'-'*18} {'-'*12} {'-'*6} {'-'*7}")
        for nb in sorted(notebooks, key=lambda x: x["name"]):
            cpu = f"{nb['cpu_avg']:.1f}" if nb["cpu_avg"] is not None else "N/A"
            print(f"  {nb['name'][:48]:<48} {nb['instance_type']:<18} "
                  f"{nb['status']:<12} {cpu:>6} ${nb['monthly_cost']:>6.2f}")

    # Endpoint table
    if endpoints:
        print(f"\n🔌 Endpoints:")
        print(f"  {'Name':<48} {'Status':<12} {'Invoc/7d':>10} {'$/mo':>7}")
        print(f"  {'-'*48} {'-'*12} {'-'*10} {'-'*7}")
        for ep in sorted(endpoints, key=lambda x: x["name"]):
            inv = str(int(ep["invocations_7d"])) if ep["invocations_7d"] is not None else "N/A"
            print(f"  {ep['name'][:48]:<48} {ep['status']:<12} {inv:>10} ${ep['monthly_cost']:>6.2f}")

    # Training jobs summary
    if training_jobs:
        spot_count = sum(1 for tj in training_jobs if tj["spot_enabled"])
        od_count   = len(training_jobs) - spot_count
        print(f"\n🏋️  Training Jobs (last 30 days, Completed): {len(training_jobs)}")
        print(f"   On-Demand : {od_count}")
        print(f"   Spot      : {spot_count}")

    # Studio domains
    if domains:
        print(f"\n🖥️  Studio Domains:")
        print(f"  {'Domain':<40} {'Apps':>5} {'Active Kernels':>15} {'Days Idle':>10}")
        print(f"  {'-'*40} {'-'*5} {'-'*15} {'-'*10}")
        for d in sorted(domains, key=lambda x: x["domain_name"]):
            idle_str = str(d["days_idle"]) if d["days_idle"] is not None else "N/A"
            print(f"  {d['domain_name'][:40]:<40} {d['total_apps']:>5} "
                  f"{d['active_kernel_apps']:>15} {idle_str:>10}")

    # Opportunities by type
    type_summary = {}
    for o in opps:
        t = o["type"]
        if t not in type_summary:
            type_summary[t] = {"count": 0, "saving": 0.0}
        type_summary[t]["count"]  += 1
        type_summary[t]["saving"] += o["saving_mo"]

    if type_summary:
        print("\n📊 Opportunities by Type:")
        print(f"  {'Type':<45} {'Count':>5}  {'Saving/mo':>12}  {'Saving/yr':>12}")
        print(f"  {'-'*45} {'-'*5}  {'-'*12}  {'-'*12}")
        for t, v in sorted(type_summary.items(), key=lambda x: -x[1]["saving"]):
            print(f"  {t:<45} {v['count']:>5}  ${v['saving']:>10.2f}  ${v['saving']*12:>10.2f}")

    sev_counts = {}
    for o in opps:
        sev_counts[o["severity"]] = sev_counts.get(o["severity"], 0) + 1
    if sev_counts:
        print(f"\n🎯 Severity: " + "  ".join(f"{s}: {c}" for s, c in sev_counts.items()))

    # Detailed findings
    sorted_opps = sorted(opps, key=lambda x: -x["saving_mo"])
    if sorted_opps:
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
                    print(line)
                    line = "       " + w + " "
                else:
                    line += w + " "
            if line.strip():
                print(line)
            print()
    else:
        print(f"\n✅ {no_findings_desc}")

    # CSV output
    ts       = now.strftime("%Y%m%d_%H%M%S")
    csv_path = os.path.join(OUTPUT_DIR, f"{ts}_sagemaker_report.csv")
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(csv_path, "w") as f:
        f.write(
            "account_id,region,resource,opportunity_type,severity,"
            "current_cost_usd_monthly,estimated_saving_usd_monthly,saving_pct,saving_basis,price_basis,"
            "dependency_warning,recommendation\n"
        )
        if not sorted_opps:
            f.write(f'"{ACCOUNT_ID}","{REGION}","","no-findings","INFO",0.00,0.00,0.0,"actual","list","","{no_findings_desc}"\n')
        else:
            for o in sorted_opps:
                pct = (o["saving_mo"] / max(o["current_mo"], 0.01)) * 100 if o["current_mo"] > 0 else 0
                saving_basis = "actual"
                sev = (
                    o["severity"]
                    .replace("🔴 ", "").replace("🟠 ", "").replace("🟡 ", "")
                    .replace("🟢 ", "").replace("⚠️  ", "").replace("ℹ️  ", "")
                    .strip()
                )
                f.write(
                    f'"{ACCOUNT_ID}","{REGION}","{o["resource"]}",'
                    f'"{o["type"]}","{sev}",{o["current_mo"]:.2f},{o["saving_mo"]:.2f},{pct:.1f},"{saving_basis}","list",'
                    f'"{o["dep_warning"].replace(chr(34), chr(39))}",'
                    f'"{o["recommendation"].replace(chr(34), chr(39))}"\n'
                )
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
    print("\n🔍 AWS SageMaker Cost Analyser")
    print(f"   Account : {ACCOUNT_ID}")
    print(f"   Region  : {REGION}")
    print(f"   Profile : {os.environ.get('AWS_PROFILE', 'default')}\n")
    session = boto3.Session()
    print("📡 Collecting SageMaker + CloudWatch data...")
    data = collect(session)
    nb_count = len(data["notebooks"])
    ep_count = len(data["endpoints"])
    tj_count = len(data["training_jobs"])
    print(f"\n🧠 Analysing {nb_count} notebook(s), {ep_count} endpoint(s), "
          f"{tj_count} training job(s)...")
    opps = analyse(data)
    print_report(data, opps)


if __name__ == "__main__":
    main()
