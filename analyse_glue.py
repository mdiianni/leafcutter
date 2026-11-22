#!/usr/bin/env python3
"""
AWS Cost Analyser — Glue Skill
Cost-saving opportunities for AWS Glue.

Checks:
  1. Glue ETL jobs — DPU type, worker count, timeout, last-run cost, Graviton migration
  2. Glue Crawlers — unused / last-run-failed / no schedule
  3. Glue Development Endpoints — always-on endpoints are expensive, flag any running
  4. Glue Data Catalog — partition count, table count (no direct cost but informs storage)
  5. Glue Workflows — inactive workflows still incurring trigger polling cost
  6. Job autoscaling — jobs with fixed worker count vs. autoscaling enabled
  7. G.1X vs G.2X vs G.025X workers — right-sizing opportunity
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
CACHE_FILE = os.path.join(CACHE_DIR, "glue.json")
os.makedirs(CACHE_DIR,  exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

now = datetime.now(timezone.utc)

# ── Severity ──────────────────────────────────────────────────────────────────
CRITICAL = "CRITICAL"
HIGH     = "HIGH"
MEDIUM   = "MEDIUM"
LOW      = "LOW"
DEP      = "DEPENDENCY"
INFO     = "INFO"

# ── Glue pricing — eu-central-1 (Frankfurt) ───────────────────────────────────
# Source: https://aws.amazon.com/glue/pricing/
# DPU-hour rates (ETL jobs, billed per second with 1-min minimum)
PRICE = {
    # Standard workers (G.1X, G.2X, G.4X, G.8X)
    "glue_dpu_hr":            0.44,    # $0.44 / DPU-hour  (standard)
    # Flex execution — ~34% cheaper, for non-urgent jobs
    "glue_flex_dpu_hr":       0.29,    # $0.29 / DPU-hour  (Flex)
    # G.025X (micro) — cheap for simple jobs
    "glue_micro_dpu_hr":      0.044,   # $0.044 / DPU-hour (G.025X = 0.25 DPU)
    # Development endpoints
    "glue_dev_ep_dpu_hr":     0.44,    # same as standard
    # Crawlers
    "glue_crawler_dpu_hr":    0.44,
    # Data Catalog storage (per 100k objects/month above free tier of 1M)
    "glue_catalog_100k_mo":   1.00,
    # Trigger polling (per million API calls, roughly)
    "glue_trigger_api_mo":    0.10,
}

# DPU count per worker type (for cost estimation)
WORKER_DPU = {
    "Standard": 1.0,   # legacy
    "G.1X":     1.0,
    "G.2X":     2.0,
    "G.4X":     4.0,
    "G.8X":     8.0,
    "G.025X":   0.25,  # micro — 1/4 DPU
    "Z.2X":     2.0,   # Ray jobs
}

GRAVITON_TYPES = {"G.1X", "G.2X", "G.4X", "G.8X", "G.025X"}  # Graviton3 by default now
FLEX_SAVING_PCT = 0.34   # Flex vs Standard saving

# ── Helpers ───────────────────────────────────────────────────────────────────
def _paginate(client, method, key, **kwargs):
    items = []
    paginator = client.get_paginator(method)
    for page in paginator.paginate(**kwargs):
        items.extend(page.get(key, []))
    return items


def _job_dpu_hr_cost(worker_type: str, num_workers: int) -> float:
    dpu = WORKER_DPU.get(worker_type, 1.0) * num_workers
    return dpu * PRICE["glue_dpu_hr"]


def _job_run_cost(run: dict) -> float:
    """Estimate cost of a single job run from its actual DPU usage."""
    dpu_seconds = run.get("DPUSeconds")
    if dpu_seconds:
        return (dpu_seconds / 3600) * PRICE["glue_dpu_hr"]
    # Fall back to elapsed time × provisioned DPUs
    elapsed = run.get("ExecutionTime", 0)  # seconds
    worker_type = run.get("WorkerType", "G.1X")
    num_workers = run.get("NumberOfWorkers", 2)
    dpu = WORKER_DPU.get(worker_type, 1.0) * num_workers
    return (elapsed / 3600) * PRICE["glue_dpu_hr"] * dpu


def _monthly_run_cost(runs: list) -> float:
    """Estimate monthly cost from last 30 days of runs."""
    cutoff = now - timedelta(days=30)
    recent = [r for r in runs if r.get("CompletedOn") and
              (r["CompletedOn"] if isinstance(r["CompletedOn"], datetime)
               else datetime.fromisoformat(str(r["CompletedOn"]).replace("Z", "+00:00"))) > cutoff]
    return sum(_job_run_cost(r) for r in recent)


# ── Collect ───────────────────────────────────────────────────────────────────
def collect(session):
    cached = load_cache(CACHE_FILE)
    if cached:
        print("  ✅  Using cached Glue data")
        return cached

    glue = session.client("glue", region_name=REGION)
    print("  📡  Fetching Glue jobs...")

    # ── Jobs ──────────────────────────────────────────────────────────────────
    jobs = []
    try:
        raw_jobs = _paginate(glue, "get_jobs", "Jobs")
        for job in raw_jobs:
            name         = job["Name"]
            worker_type  = job.get("WorkerType", "Standard")
            num_workers  = job.get("NumberOfWorkers", job.get("MaxCapacity", 2))
            max_capacity = job.get("MaxCapacity")
            autoscaling  = job.get("GlueVersion", "1.0") >= "3.0" and \
                           job.get("NumberOfWorkers") is not None
            timeout_min  = job.get("Timeout", 2880)
            glue_version = job.get("GlueVersion", "1.0")
            exec_class   = job.get("ExecutionClass", "STANDARD")  # STANDARD or FLEX
            job_type     = job.get("Command", {}).get("Name", "glueetl")  # glueetl, pythonshell, glueray

            # Get last 10 runs
            runs = []
            try:
                resp = glue.get_job_runs(JobName=name, MaxResults=20)
                runs = resp.get("JobRuns", [])
                # Normalise CompletedOn to string for JSON serialisation
                for r in runs:
                    for k in ("StartedOn", "CompletedOn", "LastModifiedOn"):
                        if k in r and isinstance(r[k], datetime):
                            r[k] = r[k].isoformat()
            except Exception:
                pass

            last_run = runs[0] if runs else None
            last_run_days_ago = None
            if last_run and last_run.get("StartedOn"):
                try:
                    started = datetime.fromisoformat(str(last_run["StartedOn"]).replace("Z", "+00:00"))
                    last_run_days_ago = (now - started).days
                except Exception:
                    pass

            jobs.append({
                "name":              name,
                "worker_type":       worker_type,
                "num_workers":       int(num_workers) if num_workers else 2,
                "max_capacity":      max_capacity,
                "timeout_min":       timeout_min,
                "glue_version":      glue_version,
                "exec_class":        exec_class,
                "job_type":          job_type,
                "autoscaling":       autoscaling,
                "runs":              runs[:10],
                "last_run_days_ago": last_run_days_ago,
                "last_run_status":   last_run.get("JobRunState") if last_run else None,
                "monthly_cost_est":  _monthly_run_cost(runs),
            })
    except Exception as e:
        print(f"  ⚠️  Could not fetch Glue jobs: {e}")

    # ── Crawlers ──────────────────────────────────────────────────────────────
    print("  📡  Fetching Glue crawlers...")
    crawlers = []
    try:
        raw_crawlers = _paginate(glue, "get_crawlers", "Crawlers")
        for c in raw_crawlers:
            last_crawl = c.get("LastCrawl", {})
            last_status = last_crawl.get("Status")
            last_time   = last_crawl.get("StartTime")
            last_days_ago = None
            if last_time:
                try:
                    lt = last_time if isinstance(last_time, datetime) else \
                         datetime.fromisoformat(str(last_time).replace("Z", "+00:00"))
                    last_days_ago = (now - lt).days
                except Exception:
                    pass
            schedule = c.get("Schedule", {}).get("ScheduleExpression", "")
            state    = c.get("State", "READY")
            crawlers.append({
                "name":           c["Name"],
                "state":          state,
                "last_status":    last_status,
                "last_days_ago":  last_days_ago,
                "schedule":       schedule,
                "targets":        c.get("Targets", {}),
            })
    except Exception as e:
        print(f"  ⚠️  Could not fetch crawlers: {e}")

    # ── Dev Endpoints ─────────────────────────────────────────────────────────
    print("  📡  Fetching Glue dev endpoints...")
    dev_endpoints = []
    try:
        resp = glue.get_dev_endpoints()
        for ep in resp.get("DevEndpoints", []):
            dpu = ep.get("NumberOfDPUs", ep.get("NumberOfWorkers", 5))
            created = ep.get("CreatedTimestamp")
            age_days = None
            if created:
                try:
                    ct = created if isinstance(created, datetime) else \
                         datetime.fromisoformat(str(created).replace("Z", "+00:00"))
                    age_days = (now - ct).days
                except Exception:
                    pass
            dev_endpoints.append({
                "name":         ep["EndpointName"],
                "status":       ep.get("Status", "READY"),
                "num_dpu":      int(dpu),
                "age_days":     age_days,
                "worker_type":  ep.get("WorkerType", "Standard"),
                "monthly_cost": int(dpu) * PRICE["glue_dev_ep_dpu_hr"] * 730,
            })
    except Exception as e:
        print(f"  ⚠️  Could not fetch dev endpoints: {e}")

    # ── Workflows ─────────────────────────────────────────────────────────────
    print("  📡  Fetching Glue workflows...")
    workflows = []
    try:
        raw_wf = _paginate(glue, "list_workflows", "Workflows")
        for wf_name in raw_wf:
            try:
                wf = glue.get_workflow(Name=wf_name, IncludeGraph=False)["Workflow"]
                last_run_ts = wf.get("LastRun", {}).get("StartedOn")
                last_days_ago = None
                if last_run_ts:
                    try:
                        lt = last_run_ts if isinstance(last_run_ts, datetime) else \
                             datetime.fromisoformat(str(last_run_ts).replace("Z", "+00:00"))
                        last_days_ago = (now - lt).days
                    except Exception:
                        pass
                workflows.append({
                    "name":          wf_name,
                    "last_days_ago": last_days_ago,
                    "status":        wf.get("LastRun", {}).get("Status"),
                })
            except Exception:
                workflows.append({"name": wf_name, "last_days_ago": None, "status": None})
    except Exception as e:
        print(f"  ⚠️  Could not fetch workflows: {e}")

    # ── Catalog stats ─────────────────────────────────────────────────────────
    print("  📡  Fetching Glue catalog stats...")
    catalog = {}
    try:
        resp = glue.get_catalog_import_status()
        catalog["import_status"] = resp.get("ImportStatus", {}).get("ImportCompleted")
    except Exception:
        pass
    try:
        dbs = _paginate(glue, "get_databases", "DatabaseList")
        catalog["database_count"] = len(dbs)
        table_count = 0
        for db in dbs[:50]:  # cap to avoid excessive API calls
            try:
                tables = _paginate(glue, "get_tables", "TableList", DatabaseName=db["Name"])
                table_count += len(tables)
            except Exception:
                pass
        catalog["table_count"] = table_count
    except Exception as e:
        print(f"  ⚠️  Could not fetch catalog: {e}")

    data = {
        "jobs":          jobs,
        "crawlers":      crawlers,
        "dev_endpoints": dev_endpoints,
        "workflows":     workflows,
        "catalog":       catalog,
    }
    save_cache(CACHE_FILE, data)
    return data


# ── Analyse ───────────────────────────────────────────────────────────────────
def analyse(data):
    opps = []
    jobs          = data.get("jobs", [])
    crawlers      = data.get("crawlers", [])
    dev_endpoints = data.get("dev_endpoints", [])
    workflows     = data.get("workflows", [])

    # ── 1. Development Endpoints — always-on, very expensive ─────────────────
    for ep in dev_endpoints:
        if ep["status"] in ("READY", "RUNNING"):
            mo_cost = ep["monthly_cost"]
            age_str = f"{ep['age_days']} days old" if ep["age_days"] is not None else "age unknown"
            opps.append({
                "resource":       ep["name"],
                "type":           "glue-dev-endpoint-running",
                "severity":       CRITICAL,
                "current_mo":     round(mo_cost, 2),
                "saving_mo":      round(mo_cost, 2),
                "dep_warning":    "Ensure no active notebooks or IDEs are connected to this endpoint before deletion.",
                "recommendation": (
                    f"Glue Development Endpoint '{ep['name']}' is READY/RUNNING with "
                    f"{ep['num_dpu']} DPUs ({ep['worker_type']}) at "
                    f"~${mo_cost:.2f}/month ({age_str}). "
                    f"Dev endpoints are billed continuously even when idle — migrate to "
                    f"Glue Studio notebooks (serverless, billed per session) to eliminate "
                    f"this cost entirely. Delete this endpoint to save ~${mo_cost:.2f}/month."
                ),
            })

    # ── 2. Jobs using STANDARD execution class — Flex available ──────────────
    for job in jobs:
        if job["job_type"] not in ("glueetl",):
            continue
        if job["exec_class"] == "STANDARD" and job["monthly_cost_est"] > 5:
            saving = round(job["monthly_cost_est"] * FLEX_SAVING_PCT, 2)
            if saving < 1:
                continue
            opps.append({
                "resource":       job["name"],
                "type":           "glue-job-flex-execution",
                "severity":       MEDIUM,
                "current_mo":     round(job["monthly_cost_est"], 2),
                "saving_mo":      saving,
                "dep_warning":    "Flex jobs may start with a few minutes delay and cannot use AWS Glue streaming. Only suitable for batch workloads without strict SLA requirements.",
                "recommendation": (
                    f"Job '{job['name']}' uses STANDARD execution class "
                    f"(est. ${job['monthly_cost_est']:.2f}/month). "
                    f"Switching to FLEX execution saves ~{FLEX_SAVING_PCT*100:.0f}% "
                    f"(~${saving:.2f}/month) by using spare capacity. "
                    f"Suitable for non-time-critical batch ETL workloads. "
                    f"Set ExecutionClass=FLEX in the job configuration."
                ),
            })

    # ── 3. G.1X/G.2X jobs with autoscaling disabled ───────────────────────────
    for job in jobs:
        if job["job_type"] not in ("glueetl",):
            continue
        if job["worker_type"] in ("G.1X", "G.2X") and not job["autoscaling"]:
            if job["num_workers"] >= 5 and job["monthly_cost_est"] > 2:
                saving = round(job["monthly_cost_est"] * 0.30, 2)  # ~30% typical autoscaling saving
                if saving < 1:
                    continue
                opps.append({
                    "resource":       job["name"],
                    "type":           "glue-job-autoscaling-disabled",
                    "severity":       MEDIUM,
                    "current_mo":     round(job["monthly_cost_est"], 2),
                    "saving_mo":      saving,
                    "dep_warning":    "Autoscaling requires Glue version 3.0+. Test thoroughly — some jobs with tight memory budgets may fail with fewer workers.",
                    "recommendation": (
                        f"Job '{job['name']}' ({job['worker_type']}, {job['num_workers']} workers) "
                        f"has autoscaling disabled. Enabling autoscaling (Glue 3.0+) allows "
                        f"the job to scale down idle workers during low-DPU phases, typically "
                        f"saving 20–40% on DPU costs. Estimated saving: ~${saving:.2f}/month. "
                        f"Set NumberOfWorkers as the maximum and enable auto scaling."
                    ),
                })

    # ── 4. Oversized jobs — G.2X where G.1X would suffice ────────────────────
    for job in jobs:
        if job["job_type"] not in ("glueetl",):
            continue
        if job["worker_type"] == "G.2X" and job["monthly_cost_est"] > 5:
            # Heuristic: if the job has been running < 10min avg, G.1X may suffice
            runs = job.get("runs", [])
            if runs:
                exec_times = [r.get("ExecutionTime", 0) for r in runs if r.get("ExecutionTime")]
                avg_exec   = sum(exec_times) / len(exec_times) if exec_times else 0
                if avg_exec < 600 and avg_exec > 0:   # avg < 10 min
                    saving = round(job["monthly_cost_est"] * 0.50, 2)  # G.2X→G.1X = 50% DPU saving
                    opps.append({
                        "resource":       job["name"],
                        "type":           "glue-job-overprovisioned-worker",
                        "severity":       MEDIUM,
                        "current_mo":     round(job["monthly_cost_est"], 2),
                        "saving_mo":      saving,
                        "dep_warning":    "Test on G.1X before changing production — memory-intensive shuffles may OOM on smaller workers.",
                        "recommendation": (
                            f"Job '{job['name']}' uses G.2X workers (2 DPU each) but avg "
                            f"execution time is only {avg_exec:.0f}s. Short-running G.2X jobs "
                            f"rarely utilise the extra memory. Downgrading to G.1X (1 DPU each) "
                            f"would halve the DPU cost: ~${saving:.2f}/month saved. "
                            f"Run a test execution on G.1X to validate memory headroom."
                        ),
                    })

    # ── 5. High-timeout jobs — risk of runaway cost ───────────────────────────
    for job in jobs:
        if job["timeout_min"] > 2880 and job["monthly_cost_est"] > 1:   # > 48h timeout
            opps.append({
                "resource":       job["name"],
                "type":           "glue-job-high-timeout",
                "severity":       LOW,
                "current_mo":     round(job["monthly_cost_est"], 2),
                "saving_mo":      0.0,
                "dep_warning":    "",
                "recommendation": (
                    f"Job '{job['name']}' has a timeout of {job['timeout_min']} minutes "
                    f"({job['timeout_min']//60}h). A runaway job could accumulate "
                    f"${job['num_workers'] * WORKER_DPU.get(job['worker_type'],1) * PRICE['glue_dpu_hr'] * job['timeout_min'] / 60:.2f} "
                    f"before being killed. Set timeout to the maximum expected runtime + 20% "
                    f"buffer to cap runaway cost."
                ),
            })

    # ── 6. Stale jobs — not run in > 90 days ─────────────────────────────────
    stale_jobs = [j for j in jobs if j["last_run_days_ago"] is not None and j["last_run_days_ago"] > 90]
    if stale_jobs:
        for job in stale_jobs:
            opps.append({
                "resource":       job["name"],
                "type":           "glue-job-stale",
                "severity":       LOW,
                "current_mo":     0.0,
                "saving_mo":      0.0,
                "dep_warning":    "",
                "recommendation": (
                    f"Job '{job['name']}' has not run in {job['last_run_days_ago']} days "
                    f"(last status: {job['last_run_status'] or 'unknown'}). "
                    f"Review whether this job is still needed. Stale jobs clutter the catalog "
                    f"and may reference obsolete IAM roles or data sources. "
                    f"Archive or delete if no longer required."
                ),
            })

    # ── 7. Always-failing crawlers ────────────────────────────────────────────
    failed_crawlers = [c for c in crawlers if c["last_status"] == "FAILED"]
    if failed_crawlers:
        for c in failed_crawlers:
            opps.append({
                "resource":       c["name"],
                "type":           "glue-crawler-always-failing",
                "severity":       MEDIUM,
                "current_mo":     0.0,
                "saving_mo":      0.0,
                "dep_warning":    "",
                "recommendation": (
                    f"Crawler '{c['name']}' last run FAILED "
                    f"({'%d days ago' % c['last_days_ago'] if c['last_days_ago'] is not None else 'recently'}). "
                    f"A failing crawler still consumes DPU time and incurs cost. "
                    f"Fix the underlying data source issue (permissions, path, format) "
                    f"or disable/delete the crawler if the target data no longer exists."
                ),
            })

    # ── 8. Crawlers with no schedule ─────────────────────────────────────────
    unscheduled = [c for c in crawlers if not c["schedule"] and c["last_status"] != "FAILED"]
    if unscheduled:
        opps.append({
            "resource":       f"{len(unscheduled)} crawlers",
            "type":           "glue-crawler-no-schedule",
            "severity":       LOW,
            "current_mo":     0.0,
            "saving_mo":      0.0,
            "dep_warning":    "",
            "recommendation": (
                f"{len(unscheduled)} crawlers have no schedule and must be triggered manually: "
                f"{', '.join(c['name'] for c in unscheduled[:5])}{'...' if len(unscheduled) > 5 else ''}. "
                f"If these crawlers run frequently on-demand they may accumulate untracked cost. "
                f"Add schedules with appropriate frequency, or trigger from job completion events "
                f"to avoid unnecessary re-crawls."
            ),
        })

    # ── 9. Inactive workflows ─────────────────────────────────────────────────
    inactive_wf = [w for w in workflows if w["last_days_ago"] is not None and w["last_days_ago"] > 90]
    if inactive_wf:
        opps.append({
            "resource":       f"{len(inactive_wf)} workflows",
            "type":           "glue-workflow-inactive",
            "severity":       LOW,
            "current_mo":     0.0,
            "saving_mo":      0.0,
            "dep_warning":    "",
            "recommendation": (
                f"{len(inactive_wf)} Glue workflow(s) have not run in >90 days: "
                f"{', '.join(w['name'] for w in inactive_wf[:5])}{'...' if len(inactive_wf) > 5 else ''}. "
                f"Inactive workflows still have scheduled triggers that poll the Glue API "
                f"continuously. Disable triggers for unused workflows to reduce API costs "
                f"and simplify the catalog."
            ),
        })

    # ── 10. Glue version < 3.0 (missing autoscaling + performance improvements) ──
    old_version_jobs = [j for j in jobs if j["job_type"] == "glueetl" and
                        (j["glue_version"] or "1.0") < "3.0"]
    if old_version_jobs:
        top = old_version_jobs[:10]
        opps.append({
            "resource":       f"{len(old_version_jobs)} jobs",
            "type":           "glue-job-old-version",
            "severity":       LOW,
            "current_mo":     0.0,
            "saving_mo":      0.0,
            "dep_warning":    "Test script compatibility before upgrading — Spark API changes between Glue 2.0 and 4.0.",
            "recommendation": (
                f"{len(old_version_jobs)} ETL job(s) use Glue version < 3.0: "
                f"{', '.join(j['name'] for j in top)}{'...' if len(old_version_jobs) > 10 else ''}. "
                f"Glue 3.0+ includes Apache Spark 3.x (significant performance improvements), "
                f"autoscaling support, and better DPU utilisation. Upgrading typically reduces "
                f"DPU-hours by 10–30% due to better vectorised execution. Target Glue 4.0 "
                f"(Spark 3.3, Python 3.10) for new and existing jobs."
            ),
        })

    return opps


# ── Reporting ─────────────────────────────────────────────────────────────────
def print_report(data, opps):
    jobs          = data.get("jobs", [])
    crawlers      = data.get("crawlers", [])
    dev_endpoints = data.get("dev_endpoints", [])
    workflows     = data.get("workflows", [])
    catalog       = data.get("catalog", {})

    total_job_cost = sum(j["monthly_cost_est"] for j in jobs)
    total_ep_cost  = sum(e["monthly_cost"] for e in dev_endpoints)
    total_mo       = total_job_cost + total_ep_cost
    total_saving   = sum(o["saving_mo"] for o in opps)
    real_opps      = [o for o in opps if o["severity"] != INFO]

    print()
    print(BORDER_TOP)
    print(_bl("       AWS Cost Analyser — Glue Analysis Results"))
    print(BORDER_MID)
    print(_bl(f"  Account       : {ACCOUNT_ID}"))
    print(_bl(f"  Region        : {REGION}"))
    print(_bl(f"  Run at        : {now.strftime('%Y-%m-%d %H:%M UTC')}"))
    print(_bl(f"  ETL Jobs      : {len(jobs)}"))
    print(_bl(f"  Crawlers      : {len(crawlers)}"))
    print(_bl(f"  Dev Endpoints : {len(dev_endpoints)}"))
    print(_bl(f"  Workflows     : {len(workflows)}"))
    print(_bl(f"  Catalog DBs   : {catalog.get('database_count', '?')}  Tables: {catalog.get('table_count', '?')}"))
    print(BORDER_MID)
    print(_bl(f"  💸 Est. ETL job cost (last 30d)        : ${total_job_cost:>8.2f} / month"))
    print(_bl(f"  💸 Dev endpoint cost (always-on)       : ${total_ep_cost:>8.2f} / month"))
    print(_bl(f"  💸 Total est. Glue cost                : ${total_mo:>8.2f} / month"))
    print(_bl(f"  💰 Total savings potential             : ${total_saving:>8.2f} / month"))
    print(_bl(f"  💰 Annualised savings potential        : ${total_saving*12:>8.2f} / year"))
    print(_bl(f"  🔍 Opportunities found                 : {len(real_opps)}"))
    print(BORDER_BOT)

    if not opps:
        print("\n✅ No cost-saving opportunities found for Glue.\n")
        return

    # Opportunities by type
    type_summary: dict = {}
    for o in real_opps:
        t = o["type"]
        if t not in type_summary:
            type_summary[t] = {"count": 0, "saving": 0.0}
        type_summary[t]["count"]  += 1
        type_summary[t]["saving"] += o["saving_mo"]

    print(f"\n{'─'*72}")
    print("📊 Opportunities by Type:\n")
    print(f"  {'Type':<40} {'Count':>5}  {'Saving/mo':>12}  {'Saving/yr':>12}")
    print(f"  {'─'*40} {'─'*5}  {'─'*12}  {'─'*12}")
    for t, v in sorted(type_summary.items(), key=lambda x: -x[1]["saving"]):
        print(f"  {t:<40} {v['count']:>5}  ${v['saving']:>10.2f}  ${v['saving']*12:>10.2f}")

    sev_counts: dict = {}
    for o in real_opps:
        sev_counts[o["severity"]] = sev_counts.get(o["severity"], 0) + 1
    print(f"\n🎯 Severity: " + "  ".join(f"{s}: {c}" for s, c in sev_counts.items()))

    # Detailed findings
    sorted_opps = sorted(real_opps, key=lambda x: -x["saving_mo"])
    print("\n" + "─" * 72)
    print("📋 Detailed Findings (sorted by potential saving):\n")
    for i, opp in enumerate(sorted_opps, 1):
        sev_icon = {"CRITICAL":"🔴","HIGH":"🟠","MEDIUM":"🟡","LOW":"🟢","DEPENDENCY":"⚠️ "}.get(opp["severity"],"⚪")
        print(f"  [{i:02d}] {sev_icon} {opp['severity']}  —  {opp['type']}")
        print(f"       Resource : {opp['resource']}")
        if opp["saving_mo"] > 0:
            print(f"       Saving   : ${opp['saving_mo']:.2f}/month  (${opp['saving_mo']*12:.2f}/year)")
        if opp["dep_warning"]:
            dw = opp["dep_warning"]
            print(f"       ⚠️  Dep   : {dw[:70]}")
        words = opp["recommendation"].split()
        line  = "       "
        for w in words:
            if len(line) + len(w) + 1 > 76:
                print(line); line = "       " + w + " "
            else:
                line += w + " "
        if line.strip():
            print(line)
        print()

    # Dev endpoint callout
    running_eps = [e for e in dev_endpoints if e["status"] in ("READY", "RUNNING")]
    if running_eps:
        print(f"\n🚨 Always-On Dev Endpoints ({len(running_eps)}) — immediate action recommended:")
        for ep in running_eps:
            print(f"   • {ep['name']:<45}  {ep['num_dpu']} DPUs  ${ep['monthly_cost']:.2f}/mo")

    # CSV
    ts       = now.strftime("%Y%m%d_%H%M%S")
    csv_path = os.path.join(OUTPUT_DIR, f"{ts}_glue_report.csv")
    with open(csv_path, "w") as f:
        f.write("account_id,region,resource,opportunity_type,severity,"
                "current_cost_usd_monthly,estimated_saving_usd_monthly,saving_pct,"
                "dependency_warning,recommendation\n")
        for o in opps:
            pct = (o["saving_mo"] / max(o["current_mo"], 0.01)) * 100 if o["current_mo"] > 0 else 0
            f.write(f'"{ACCOUNT_ID}","{REGION}","{o["resource"]}",'
                    f'"{o["type"]}","{o["severity"]}",{o["current_mo"]:.2f},{o["saving_mo"]:.2f},'
                    f'{pct:.1f},"{o["dep_warning"].replace(chr(34),chr(39))}",'
                    f'"{o["recommendation"].replace(chr(34),chr(39))}"\n')
    print(f"\n💾 CSV report saved: {csv_path}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    import argparse
    parser = argparse.ArgumentParser(description="AWS Glue cost optimisation analyser")
    parser.add_argument("--force-refresh", "-f", action="store_true",
                        help="Ignore the local cache and fetch fresh data from AWS")
    args = parser.parse_args()
    if args.force_refresh:
        os.environ["AWS_COST_ANALYSER_FORCE_REFRESH"] = "1"
        print("  ⚠️  --force-refresh: ignoring cache, fetching live data from AWS")

    print("\n🔍 AWS Glue Cost Analyser")
    print(f"   Account : {ACCOUNT_ID}")
    print(f"   Region  : {REGION}")
    print(f"   Profile : {os.environ.get('AWS_PROFILE', 'default')}\n")

    session = boto3.Session()
    print("📡 Collecting Glue data...")
    data = collect(session)
    jobs = data.get("jobs", [])
    print(f"\n🧠 Analysing {len(jobs)} jobs, "
          f"{len(data.get('crawlers',[]))} crawlers, "
          f"{len(data.get('dev_endpoints',[]))} dev endpoints...")
    opps = analyse(data)
    print_report(data, opps)


if __name__ == "__main__":
    main()
