#!/usr/bin/env python3
"""
AWS Cost Analyser — DataSync Skill
Implements the analysis defined in skills/datasync.md

Opportunities:
  1. Idle task — no successful execution in 30 days (HIGH)
  2. Error/unavailable task — Status=UNAVAILABLE or last exec=ERROR (CRITICAL)
  3. High monthly transfer cost > $50/month (INFO)
  4. No schedule configured — ad-hoc only (LOW)
"""

import boto3
import os
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from cache_utils import load_cache, save_cache
from box_utils import _bl, BORDER_TOP, BORDER_MID, BORDER_BOT

# ── Configuration ─────────────────────────────────────────────────────────────
REGION     = os.environ.get("AWS_DEFAULT_REGION", "eu-central-1")
ACCOUNT_ID = boto3.Session().client("sts").get_caller_identity()["Account"]
CACHE_DIR  = os.path.join(os.path.dirname(__file__), "cache", ACCOUNT_ID, REGION)
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "output")
CACHE_FILE = os.path.join(CACHE_DIR, "datasync.json")

now = datetime.now(timezone.utc)
os.makedirs(CACHE_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

# ── Severity ──────────────────────────────────────────────────────────────────
CRITICAL = "🔴 CRITICAL"
HIGH     = "🟠 HIGH"
MEDIUM   = "🟡 MEDIUM"
LOW      = "🟢 LOW"
INFO     = "ℹ️  INFO"

# ── Pricing — eu-central-1 ────────────────────────────────────────────────────
PRICE_PER_GB      = 0.0125   # $0.0125 per GB transferred
HIGH_COST_THRESHOLD = 50.0   # flag tasks with monthly est. > $50


# ── Helpers ───────────────────────────────────────────────────────────────────
def _bytes_to_gb(b):
    return b / (1024 ** 3)


def _fetch_task_detail(ds, task):
    """Fetch describe_task + last 5 executions for a single task."""
    arn  = task["TaskArn"]
    name = task.get("Name", arn.split("/")[-1])

    try:
        detail = ds.describe_task(TaskArn=arn)
    except Exception as e:
        return {"arn": arn, "name": name, "status": task.get("Status", "UNKNOWN"),
                "error": str(e), "schedule": None, "executions": []}

    schedule = detail.get("Schedule") or detail.get("TaskSchedule")
    # schedule is a dict with "ScheduleExpression" when set

    executions = []
    try:
        exec_page = ds.list_task_executions(TaskArn=arn, MaxResults=5)
        exec_arns = [e["TaskExecutionArn"] for e in exec_page.get("TaskExecutionSummaryList", [])]
        for exec_arn in exec_arns:
            try:
                ex = ds.describe_task_execution(TaskExecutionArn=exec_arn)
                start = ex.get("StartTime")
                executions.append({
                    "arn":              exec_arn,
                    "status":           ex.get("Status", "UNKNOWN"),
                    "bytes_transferred": ex.get("BytesTransferred", 0),
                    "start_time":       start.isoformat() if start else None,
                })
            except Exception:
                pass
    except Exception:
        pass

    return {
        "arn":        arn,
        "name":       name,
        "status":     detail.get("Status", task.get("Status", "UNKNOWN")),
        "schedule":   schedule,
        "source_arn": detail.get("SourceLocationArn"),
        "dest_arn":   detail.get("DestinationLocationArn"),
        "executions": executions,
    }


# ── Data Collection ───────────────────────────────────────────────────────────
def collect(session):
    cached = load_cache(CACHE_FILE)
    if cached:
        print("  ✅ Loaded from cache")
        return cached

    ds = session.client("datasync", region_name=REGION)

    raw_tasks = []
    try:
        paginator = ds.get_paginator("list_tasks")
        for page in paginator.paginate():
            raw_tasks.extend(page.get("Tasks", []))
    except Exception as e:
        print(f"  ⚠️  Could not list DataSync tasks: {e}")

    print(f"  📡 Fetching details for {len(raw_tasks)} task(s) in parallel…")
    tasks = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(_fetch_task_detail, ds, t): t for t in raw_tasks}
        for fut in as_completed(futs):
            try:
                tasks.append(fut.result())
            except Exception as e:
                print(f"  ⚠️  Error fetching task detail: {e}")

    data = {
        "account_id":   ACCOUNT_ID,
        "region":       REGION,
        "tasks":        tasks,
        "collected_at": now.isoformat(),
    }
    save_cache(CACHE_FILE, data, account_id=ACCOUNT_ID, region=REGION)
    return data


# ── Analysis ──────────────────────────────────────────────────────────────────
def analyse(data):
    opps  = []
    tasks = data.get("tasks", [])
    cutoff_30d = now - timedelta(days=30)

    for task in tasks:
        status     = task.get("status", "UNKNOWN")
        name       = task.get("name", task["arn"].split("/")[-1])
        executions = task.get("executions", [])
        schedule   = task.get("schedule")

        # Skip tasks currently running or being created
        if status in ("RUNNING", "CREATING"):
            continue

        # Parse executions into useful summaries
        recent_successes = []
        last_exec_status = None
        last_exec_time   = None
        for ex in executions:
            last_exec_status = last_exec_status or ex["status"]
            if ex.get("start_time"):
                t = datetime.fromisoformat(ex["start_time"])
                if t.tzinfo is None:
                    t = t.replace(tzinfo=timezone.utc)
                if last_exec_time is None or t > last_exec_time:
                    last_exec_time = t
            if ex["status"] == "SUCCESS" and ex.get("start_time"):
                t = datetime.fromisoformat(ex["start_time"])
                if t.tzinfo is None:
                    t = t.replace(tzinfo=timezone.utc)
                if t >= cutoff_30d:
                    recent_successes.append(ex)

        last_run_str = last_exec_time.strftime("%Y-%m-%d") if last_exec_time else "never"

        # ── Opp 2: Error / Unavailable task ───────────────────────────────
        # Check this before idle so error tasks get CRITICAL (higher severity)
        if status in ("UNAVAILABLE", "ERROR") or last_exec_status == "ERROR":
            opps.append({
                "type":       "datasync-error-task",
                "severity":   CRITICAL,
                "resource":   task["arn"],
                "name":       name,
                "current_mo": 0.0,
                "saving_mo":  0.0,
                "dep_warning": "",
                "recommendation": (
                    f"DataSync task '{name}' is in error state "
                    f"(task status: {status}, last execution: {last_exec_status or 'none'}). "
                    f"Continuously failing tasks waste agent resources and generate unnecessary "
                    f"API calls. Investigate the source/destination location, fix the underlying "
                    f"error, or delete the task."
                ),
            })
            continue  # Don't also flag as idle — error is the primary issue

        # ── Opp 1: Idle task — no successful execution in 30 days ─────────
        if not recent_successes:
            opps.append({
                "type":       "datasync-idle-task",
                "severity":   HIGH,
                "resource":   task["arn"],
                "name":       name,
                "current_mo": 0.0,
                "saving_mo":  0.0,
                "dep_warning": "",
                "recommendation": (
                    f"DataSync task '{name}' has had no successful execution in the last 30 days "
                    f"(last run: {last_run_str}, task status: {status}). Although idle tasks incur "
                    f"no transfer cost, they represent stale infrastructure. Review and delete if "
                    f"no longer required."
                ),
            })

        # ── Opp 3: High monthly transfer cost ─────────────────────────────
        total_bytes_30d = sum(ex.get("bytes_transferred", 0) for ex in recent_successes)
        monthly_gb      = _bytes_to_gb(total_bytes_30d)
        monthly_cost    = monthly_gb * PRICE_PER_GB
        if monthly_cost > HIGH_COST_THRESHOLD:
            opps.append({
                "type":       "datasync-transfer-cost-estimate",
                "severity":   INFO,
                "resource":   task["arn"],
                "name":       name,
                "current_mo": monthly_cost,
                "saving_mo":  0.0,
                "dep_warning": "",
                "recommendation": (
                    f"DataSync task '{name}' transferred ~{monthly_gb:.1f} GB in the last 30 days "
                    f"(~${monthly_cost:.2f}/month at ${PRICE_PER_GB}/GB). Consider enabling compression "
                    f"in task Options, using include/exclude filters to reduce transfer volume, or "
                    f"scheduling transfers during off-peak hours."
                ),
            })

        # ── Opp 4: No schedule configured ─────────────────────────────────
        has_schedule = bool(schedule and schedule.get("ScheduleExpression"))
        if not has_schedule:
            opps.append({
                "type":       "datasync-no-schedule",
                "severity":   LOW,
                "resource":   task["arn"],
                "name":       name,
                "current_mo": 0.0,
                "saving_mo":  0.0,
                "dep_warning": "",
                "recommendation": (
                    f"DataSync task '{name}' has no schedule configured (ad-hoc execution only). "
                    f"Setting a schedule (e.g., nightly off-peak) ensures predictable transfer windows, "
                    f"reduces concurrency with other workloads, and makes cost monitoring easier. "
                    f"Set ScheduleExpression via the Console or API."
                ),
            })

    return opps


# ── Report ────────────────────────────────────────────────────────────────────
def print_report(data, opps):
    tasks = data.get("tasks", [])

    total_saving = sum(o["saving_mo"] for o in opps)
    total_cost   = sum(o["current_mo"] for o in opps if o["type"] == "datasync-transfer-cost-estimate")

    print(f"\n{'=' * 70}")
    print(f"  AWS DataSync Cost Analysis — Account {ACCOUNT_ID} / {REGION}")
    print(f"{'=' * 70}")
    print(f"  Tasks total:         {len(tasks)}")
    available_tasks  = [t for t in tasks if t.get("status") not in ("RUNNING", "CREATING")]
    idle_count       = sum(1 for t in available_tasks
                           if t.get("status") not in ("UNAVAILABLE", "ERROR")
                           and not any(
                               e["status"] == "SUCCESS" and e.get("start_time") and
                               datetime.fromisoformat(e["start_time"]).replace(
                                   tzinfo=timezone.utc if datetime.fromisoformat(
                                       e["start_time"]).tzinfo is None else
                                   datetime.fromisoformat(e["start_time"]).tzinfo
                               ) >= now - timedelta(days=30)
                               for e in t.get("executions", [])
                           ))
    error_count      = sum(1 for t in available_tasks
                           if t.get("status") in ("UNAVAILABLE", "ERROR"))
    scheduled_count  = sum(1 for t in available_tasks
                           if t.get("schedule") and
                           t["schedule"].get("ScheduleExpression"))
    print(f"  Error/unavailable:   {error_count}")
    print(f"  Idle (30d):          {idle_count}")
    print(f"  Scheduled tasks:     {scheduled_count} / {len(available_tasks)}")
    print(f"  Est. transfer cost:  ${total_cost:.2f}/month (flagged tasks)")
    print()

    # Task table
    if available_tasks:
        cutoff_30d = now - timedelta(days=30)
        print(f"  {'Name':<40} {'Status':<14} {'Last Run':<12} {'30d GB':>8}  {'Est $/mo':>9}  {'Sched':>5}")
        print(f"  {'-'*40} {'-'*14} {'-'*12} {'-'*8}  {'-'*9}  {'-'*5}")
        for t in sorted(available_tasks, key=lambda x: x["name"]):
            last_run = "never"
            bytes_30d = 0
            for ex in t.get("executions", []):
                if ex.get("start_time"):
                    st = datetime.fromisoformat(ex["start_time"])
                    if st.tzinfo is None:
                        st = st.replace(tzinfo=timezone.utc)
                    if last_run == "never" or st > datetime.fromisoformat(
                            last_run if last_run != "never" else "1970-01-01T00:00:00+00:00"):
                        last_run = st.strftime("%Y-%m-%d")
                    if ex["status"] == "SUCCESS" and st >= cutoff_30d:
                        bytes_30d += ex.get("bytes_transferred", 0)
            gb_30d   = _bytes_to_gb(bytes_30d)
            est_cost = gb_30d * PRICE_PER_GB
            sched    = "✓" if (t.get("schedule") and t["schedule"].get("ScheduleExpression")) else " "
            print(f"  {t['name'][:40]:<40} {t['status']:<14} {last_run:<12} {gb_30d:>8.2f}  {est_cost:>9.2f}  {sched:>5}")
    print()

    SEV_ORDER = {CRITICAL: 0, HIGH: 1, MEDIUM: 2, LOW: 3, INFO: 4}
    sorted_opps = sorted(opps, key=lambda o: (SEV_ORDER.get(o["severity"], 9), -o["saving_mo"]))

    print(f"  Opportunities ({len(opps)})  |  Flagged transfer cost: ${total_cost:,.2f}/month")
    print(f"  {'─' * 68}")
    for o in sorted_opps:
        sev_clean = (o["severity"]
                     .replace("🔴 ", "").replace("🟠 ", "").replace("🟡 ", "")
                     .replace("🟢 ", "").replace("ℹ️  ", "").strip())
        icon = o["severity"].split()[0]
        rec  = o["recommendation"][:70] + "…" if len(o["recommendation"]) > 70 else o["recommendation"]
        print(f"    [{icon} {sev_clean:<8}] ${o['current_mo']:>7.2f}/mo  {rec}")
    print(f"{'=' * 70}")

    # CSV
    ts       = now.strftime("%Y%m%d_%H%M%S")
    csv_path = os.path.join(OUTPUT_DIR, f"{ts}_datasync_report.csv")
    with open(csv_path, "w") as f:
        f.write("account_id,region,resource_id,opportunity_type,severity,"
                "current_cost_usd_monthly,estimated_saving_usd_monthly,saving_pct,saving_basis,price_basis,"
                "dependency_warning,recommendation\n")
        for o in sorted_opps:
            sev = (o["severity"]
                   .replace("🔴 ", "").replace("🟠 ", "").replace("🟡 ", "")
                   .replace("🟢 ", "").replace("ℹ️  ", "").strip())
            pct = (o["saving_mo"] / max(o["current_mo"], 0.01)) * 100 if o["current_mo"] else 0
            saving_basis = "actual"
            rec = o["recommendation"].replace('"', "'")
            dep = o["dep_warning"].replace('"', "'")
            f.write(f'"{ACCOUNT_ID}","{REGION}","{o["resource"]}",'
                    f'"{o["type"]}","{sev}",{o["current_mo"]:.2f},{o["saving_mo"]:.2f},{pct:.1f},"{saving_basis}","list",'
                    f'"{dep}","{rec}"\n')
    print(f"\n  💾 CSV report: {csv_path}")


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
    print(f"\n🔄 AWS DataSync Cost Analyser")
    print(f"   Account: {ACCOUNT_ID} | Region: {REGION}")
    print(f"   Profile: {os.environ.get('AWS_PROFILE', 'default')}\n")
    session = boto3.Session()
    print("📡 Collecting DataSync data…")
    data  = collect(session)
    tasks = data.get("tasks", [])
    if not tasks:
        print("  ℹ️  No DataSync tasks found — skipping.")
        ts = now.strftime("%Y%m%d_%H%M%S")
        csv_path = os.path.join(OUTPUT_DIR, f"{ts}_datasync_report.csv")
        with open(csv_path, "w") as f:
            f.write("account_id,region,resource_id,opportunity_type,severity,"
                    "current_cost_usd_monthly,estimated_saving_usd_monthly,saving_pct,saving_basis,price_basis,"
                    "dependency_warning,recommendation\n")
        print(f"  💾 CSV report: {csv_path}")
        return
    print(f"🧠 Analysing {len(tasks)} task(s)…")
    opps = analyse(data)
    print_report(data, opps)


if __name__ == "__main__":
    main()
