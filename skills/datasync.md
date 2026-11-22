# Skill: AWS DataSync — Cost-Saving Opportunities

## Service Overview

AWS DataSync costs come primarily from **data transferred** between storage locations.
There is no hourly charge for tasks themselves — you only pay per GB moved.
The biggest savings opportunities are removing stale/idle tasks, scheduling transfers
off-peak, and reducing unnecessary transfer volume through compression or filtering.

---

## Data Collection

### AWS API Calls

```python
ds = session.client("datasync", region_name=region)
cw = session.client("cloudwatch", region_name=region)

# All tasks
ds.list_tasks()                              # TaskArn, Status, Name

# Per-task detail
ds.describe_task(TaskArn=arn)                # TaskSchedule, Options, Status,
                                             # SourceLocationArn, DestinationLocationArn

# Per-task execution history (last 5)
ds.list_task_executions(TaskArn=arn)         # TaskExecutionArn, Status

# Per-execution detail
ds.describe_task_execution(
    TaskExecutionArn=exec_arn                # BytesTransferred, Status, StartTime
)

# All locations (for context)
ds.list_locations()                          # LocationArn, LocationUri

# S3-specific location detail (optional)
ds.describe_location_s3(LocationArn=arn)
```

### Cache TTL
- Task list + execution history: **24 hours**

---

## Opportunity 1 — Idle Task (No Recent Successful Executions)

**Severity:** 🟠 HIGH
**Opportunity type:** `datasync-idle-task`

**Detection logic:**
- Task has had no successful execution (`Status = "SUCCESS"`) in the last **30 days**, OR
- Task `Status` is `"UNAVAILABLE"` or `"ERROR"`.
- Idle tasks themselves incur **no cost**, but represent stale infrastructure that should
  be reviewed and cleaned up to avoid confusion and accidental re-activation.

**Recommendation:**
> "DataSync task '{name}' ({arn}) has had no successful execution in the last 30 days
> (last run: {last_run}). Although idle tasks incur no transfer cost, they represent
> stale infrastructure. Review and delete if no longer required."

---

## Opportunity 2 — Error / Unavailable Task

**Severity:** 🔴 CRITICAL
**Opportunity type:** `datasync-error-task`

**Detection logic:**
- Task `Status = "UNAVAILABLE"` — agent or location is unreachable.
- OR the most recent execution has `Status = "ERROR"` — transfer failed.
- Error tasks may be scheduled and repeatedly attempting to run, generating API calls
  and agent activity without transferring any data.

**Recommendation:**
> "DataSync task '{name}' is in ERROR/UNAVAILABLE state (last execution: {exec_status}).
> Continuously failing tasks waste agent resources and generate unnecessary API calls.
> Investigate the source/destination location, fix the underlying error, or delete the task."

---

## Opportunity 3 — High Monthly Transfer Cost (> $50/month)

**Severity:** ℹ️  INFO
**Opportunity type:** `datasync-transfer-cost-estimate`

**Detection logic:**
- Sum `BytesTransferred` from all successful executions within the last 30 days.
- `monthly_gb = total_bytes_30d / (1024 ** 3)`
- `monthly_cost_estimate = monthly_gb * 0.0125`
- If `monthly_cost_estimate > 50`, flag for review.

**Calculation:**
```
total_bytes_30d       = sum(exec.BytesTransferred for exec in recent_successes)
monthly_gb            = total_bytes_30d / (1024 ** 3)
monthly_cost_estimate = monthly_gb * 0.0125    # $0.0125/GB transferred
```

**Recommendation:**
> "DataSync task '{name}' transferred ~{gb:.1f} GB in the last 30 days
> (~${cost:.2f}/month at $0.0125/GB). Consider enabling compression in task Options,
> using include/exclude filters to reduce transfer volume, or scheduling transfers
> during off-peak hours to reduce concurrency load."

---

## Opportunity 4 — No Schedule Configured (Ad-Hoc Only)

**Severity:** 🟢 LOW
**Opportunity type:** `datasync-no-schedule`

**Detection logic:**
- `describe_task()` returns no `TaskSchedule` field (or `TaskSchedule` is absent/null).
- Ad-hoc tasks are triggered manually or via external orchestration, missing the chance
  to batch transfers during off-peak windows for lower concurrent agent usage.

**Recommendation:**
> "DataSync task '{name}' has no schedule configured. Setting a schedule (e.g., nightly
> off-peak) ensures predictable transfer windows, reduces concurrency with other workloads,
> and makes cost monitoring easier. Use the task's Schedule tab or set ScheduleExpression
> via the API."

---

## Pricing — eu-central-1 (Frankfurt)

| Charge | Rate |
|---|---|
| Data transferred | **$0.0125 per GB** |
| Idle tasks | **Free** (no hourly charge) |
| Minimum fee | **None** |

> ℹ️  DataSync pricing is the same across all standard AWS regions for
> intra-region transfers. Cross-region or cross-account transfers may incur
> additional data-transfer egress charges.

---

## Notes

- Tasks in `CREATING` or `RUNNING` state are skipped during analysis to avoid
  misleading results mid-transfer.
- `BytesTransferred` is the number of bytes physically written to the destination
  (after dedup/compression applied by DataSync internally).
- Tag-based filtering: skip tasks with tag `cost-analyser:ignore = true`.
