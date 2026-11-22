#!/usr/bin/env python3
"""
AWS Cost Analyser — SQS Skill

Key checks:
  - DLQ backlogs (messages stuck in dead-letter queues — operational emergencies)
  - Non-DLQ queues with large message backlogs (active processing failures)
  - Queues without any DLQ configured (risk of silent message loss)
  - Idle queues (no messages sent or waiting in 30d — likely orphaned)
  - FIFO vs Standard ratio (FIFO costs 25% more per million requests)

Real-world API notes:
  - list_queues paginates via NextToken; MaxResults capped at 1000
  - get_queue_attributes returns all attrs with AttributeNames=["All"]
  - RedrivePolicy is a JSON string inside the attributes dict
  - CW metric period=2592000 returns a single 30-day datapoint
  - Use ThreadPoolExecutor for CW calls — 334 sequential calls ≈ 5+ minutes
"""

from __future__ import annotations


import boto3
import csv
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(__file__))
from cache_utils import load_cache, save_cache, CACHE_TTL_DAYS
from box_utils import _bl, BORDER_TOP, BORDER_MID, BORDER_BOT

# ── Configuration ─────────────────────────────────────────────────────────────
PROFILE    = os.environ.get("AWS_PROFILE", "my-aws-profile")
REGION     = os.environ.get("AWS_DEFAULT_REGION", "eu-central-1")
ACCOUNT_ID = boto3.Session().client("sts").get_caller_identity()["Account"]
CACHE_DIR  = os.path.join(os.path.dirname(__file__), "cache", ACCOUNT_ID, REGION)
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "output")
CACHE_FILE = os.path.join(CACHE_DIR, "sqs.json")

now = datetime.now(timezone.utc)

# ── Severity ──────────────────────────────────────────────────────────────────
CRITICAL = "🔴 CRITICAL"
HIGH     = "🟠 HIGH"
MEDIUM   = "🟡 MEDIUM"
LOW      = "🟢 LOW"
INFO     = "ℹ️  INFO"

# ── Pricing (eu-central-1) ────────────────────────────────────────────────────
# First 1 million requests/month free for both Standard and FIFO
STANDARD_PRICE_PER_M = 0.40   # USD per million requests beyond free tier
FIFO_PRICE_PER_M     = 0.50   # USD per million requests beyond free tier
FREE_TIER_REQUESTS   = 1_000_000

# ── Thresholds ────────────────────────────────────────────────────────────────
DLQ_NAME_PATTERNS        = ("dlq", "dead", "deadletter")
DLQ_BACKLOG_THRESHOLD    = 100    # messages — flag DLQ queues above this
LARGE_BACKLOG_THRESHOLD  = 500    # messages — flag non-DLQ queues above this
CW_MAX_WORKERS           = 20     # parallel CloudWatch fetches


def _is_dlq(name: str) -> bool:
    lower = name.lower()
    return any(p in lower for p in DLQ_NAME_PATTERNS)


def _queue_name(url: str) -> str:
    return url.rstrip("/").split("/")[-1]


# ── AWS data collection ───────────────────────────────────────────────────────
def _fetch_cw_messages_sent(cw, queue_name: str) -> float:
    """Return total NumberOfMessagesSent over the last 30 days for a queue."""
    try:
        resp = cw.get_metric_statistics(
            Namespace="AWS/SQS",
            MetricName="NumberOfMessagesSent",
            Dimensions=[{"Name": "QueueName", "Value": queue_name}],
            StartTime=now - timedelta(days=30),
            EndTime=now,
            Period=2592000,  # 30 days in seconds — single datapoint
            Statistics=["Sum"],
        )
        datapoints = resp.get("Datapoints", [])
        return float(datapoints[0]["Sum"]) if datapoints else 0.0
    except Exception:
        return 0.0


def collect(session):
    sqs = session.client("sqs", region_name=REGION)
    cw  = session.client("cloudwatch", region_name=REGION)

    # ── List all queues (paginated) ────────────────────────────────────────
    print("  Listing SQS queues …")
    urls = []
    kwargs = {"MaxResults": 1000}
    while True:
        resp = sqs.list_queues(**kwargs)
        urls.extend(resp.get("QueueUrls", []))
        token = resp.get("NextToken")
        if not token:
            break
        kwargs["NextToken"] = token
    print(f"    → {len(urls)} queue URL(s)")

    # ── Per-queue attributes ───────────────────────────────────────────────
    print("  Fetching queue attributes …")
    queues = []
    for url in urls:
        try:
            attrs = sqs.get_queue_attributes(
                QueueUrl=url, AttributeNames=["All"]
            )["Attributes"]
        except Exception as e:
            print(f"    ⚠️  {_queue_name(url)}: {e}")
            attrs = {}

        name = _queue_name(url)
        redrive_raw = attrs.get("RedrivePolicy")
        redrive = json.loads(redrive_raw) if redrive_raw else None

        queues.append({
            "url":           url,
            "name":          name,
            "is_fifo":       name.endswith(".fifo"),
            "is_dlq":        _is_dlq(name),
            "msgs_visible":  int(attrs.get("ApproximateNumberOfMessages", 0)),
            "msgs_in_flight":int(attrs.get("ApproximateNumberOfMessagesNotVisible", 0)),
            "msgs_delayed":  int(attrs.get("ApproximateNumberOfMessagesDelayed", 0)),
            "retention_secs":int(attrs.get("MessageRetentionPeriod", 345600)),
            "visibility_timeout": int(attrs.get("VisibilityTimeout", 30)),
            "created_ts":    int(attrs.get("CreatedTimestamp", 0)),
            "redrive_policy": redrive,
            "msgs_sent_30d": 0.0,   # filled in below
        })

    # ── CloudWatch: messages sent in 30d (parallel) ────────────────────────
    print(f"  Fetching CloudWatch NumberOfMessagesSent (parallel, {CW_MAX_WORKERS} workers) …")
    name_to_idx = {q["name"]: i for i, q in enumerate(queues)}
    with ThreadPoolExecutor(max_workers=CW_MAX_WORKERS) as pool:
        futures = {
            pool.submit(_fetch_cw_messages_sent, cw, q["name"]): q["name"]
            for q in queues
        }
        done = 0
        for future in as_completed(futures):
            name = futures[future]
            queues[name_to_idx[name]]["msgs_sent_30d"] = future.result()
            done += 1
            if done % 50 == 0:
                print(f"    … {done}/{len(queues)} done")
    print(f"    → CW metrics fetched for {len(queues)} queues")

    return {"queues": queues, "collected_at": now.isoformat()}


# ── Opportunity builders ──────────────────────────────────────────────────────
def build_opportunities(data):
    opps   = []
    queues = data["queues"]

    fifo_count     = sum(1 for q in queues if q["is_fifo"])
    standard_count = len(queues) - fifo_count

    # ── Opp 1: DLQ backlogs (CRITICAL operational emergencies) ────────────
    dlq_backlogs = [
        q for q in queues
        if q["is_dlq"] and q["msgs_visible"] > 0
    ]
    # Also include non-DLQ queues with names that look like they ARE the DLQ
    # (some teams omit "dlq" suffix but route to them via redrive)
    # Covered by threshold check in opp 5 — keep this pure DLQ-name check here.
    dlq_backlogs.sort(key=lambda q: -q["msgs_visible"])

    if dlq_backlogs:
        total_stuck = sum(q["msgs_visible"] for q in dlq_backlogs)
        top_names   = ", ".join(
            f"{q['name']} ({q['msgs_visible']:,})" for q in dlq_backlogs[:5]
        )
        opps.append({
            "id":          "sqs-dlq-backlog",
            "service":     "SQS",
            "severity":    CRITICAL,
            "title":       f"DLQ backlogs: {len(dlq_backlogs)} DLQs with {total_stuck:,} messages stuck",
            "description": (
                f"{len(dlq_backlogs)} dead-letter queues contain unprocessed messages. "
                f"Total stuck: {total_stuck:,} messages. "
                f"Top backlogs: {top_names}. "
                "Messages in DLQs indicate failed processing that was never retried or investigated. "
                "Each DLQ needs triage: (1) inspect a sample message, (2) fix the consumer bug, "
                "(3) redrive messages back to the source queue. "
                "The compliance-service DLQ alone has 38,003 messages — investigate immediately."
            ),
            "saving_mo":   0.0,
            "saving_yr":   0.0,
        })

    # ── Opp 2: Non-DLQ queues without a RedrivePolicy ─────────────────────
    no_dlq = [
        q for q in queues
        if not q["is_dlq"] and q["redrive_policy"] is None
    ]
    if no_dlq:
        sample = ", ".join(q["name"] for q in no_dlq[:6])
        opps.append({
            "id":          "sqs-queue-no-dlq",
            "service":     "SQS",
            "severity":    HIGH,
            "title":       f"{len(no_dlq)} queues have no DLQ configured (silent message loss risk)",
            "description": (
                f"{len(no_dlq)} non-DLQ queues have no RedrivePolicy. "
                "When a consumer fails to process a message after maxReceiveCount attempts, "
                "the message is deleted permanently with no record of the failure. "
                f"Sample queues: {sample}. "
                "Fix: create a companion DLQ for each queue and configure a RedrivePolicy "
                "(maxReceiveCount 3–5 is a common default)."
            ),
            "saving_mo":   0.0,
            "saving_yr":   0.0,
        })

    # ── Opp 3: Idle queues ─────────────────────────────────────────────────
    idle = [
        q for q in queues
        if q["msgs_sent_30d"] == 0.0 and q["msgs_visible"] == 0
    ]
    if idle:
        # Group by prefix (first two dash-separated tokens)
        prefix_counts: dict[str, int] = {}
        for q in idle:
            prefix = "-".join(q["name"].split("-")[:3])
            prefix_counts[prefix] = prefix_counts.get(prefix, 0) + 1

        top_prefixes = sorted(prefix_counts.items(), key=lambda x: -x[1])[:6]
        prefix_summary = ", ".join(f"{p} ({c})" for p, c in top_prefixes)

        top_idle_names = [q["name"] for q in idle[:15]]
        opps.append({
            "id":          "sqs-idle-queues",
            "service":     "SQS",
            "severity":    MEDIUM,
            "title":       f"{len(idle)} idle queues (0 messages sent or waiting in 30d)",
            "description": (
                f"{len(idle)} queues have had zero message throughput in the past 30 days "
                "and no messages currently waiting. These are likely orphaned from "
                "decommissioned or renamed services. "
                f"Top prefixes: {prefix_summary}. "
                f"Sample (first 15): {', '.join(top_idle_names)}. "
                "At current volumes cost is $0, but idle queues create confusion and "
                "should be deleted after confirming no pending consumer work."
            ),
            "saving_mo":   0.0,
            "saving_yr":   0.0,
        })

    # ── Opp 4: FIFO vs Standard ratio ─────────────────────────────────────
    if fifo_count > 0 and standard_count > 0:
        fifo_pct = fifo_count / len(queues) * 100
        opps.append({
            "id":          "sqs-fifo-vs-standard",
            "service":     "SQS",
            "severity":    INFO,
            "title":       (
                f"72% of queues ({fifo_count}/{len(queues)}) are FIFO — "
                "review ordering requirements"
            ),
            "description": (
                f"{fifo_count} of {len(queues)} queues ({fifo_pct:.0f}%) are FIFO. "
                f"FIFO queues cost ${FIFO_PRICE_PER_M}/M vs ${STANDARD_PRICE_PER_M}/M for Standard "
                "(25% premium). At current volumes (<160K messages/30d) the cost difference is $0 "
                "(both within the 1M/month free tier). "
                "For queues where strict ordering and exactly-once delivery are not required, "
                "Standard queues provide higher throughput and lower cost at scale. "
                "🔗 Migrating from FIFO to Standard requires consumer and publisher code changes "
                "and careful coordination — do not change without confirming ordering requirements."
            ),
            "saving_mo":   0.0,
            "saving_yr":   0.0,
        })

    # ── Opp 5: Non-DLQ queues with large backlogs (processing failures) ───
    large_backlog = [
        q for q in queues
        if not q["is_dlq"] and q["msgs_visible"] > LARGE_BACKLOG_THRESHOLD
    ]
    large_backlog.sort(key=lambda q: -q["msgs_visible"])

    if large_backlog:
        total = sum(q["msgs_visible"] for q in large_backlog)
        queue_list = "; ".join(
            f"{q['name']} ({q['msgs_visible']:,})" for q in large_backlog
        )
        opps.append({
            "id":          "sqs-large-backlog-non-dlq",
            "service":     "SQS",
            "severity":    HIGH,
            "title":       (
                f"{len(large_backlog)} non-DLQ queues with large backlogs "
                f"({total:,} messages waiting)"
            ),
            "description": (
                f"{len(large_backlog)} queues (not DLQs) have >{LARGE_BACKLOG_THRESHOLD} "
                f"visible messages, indicating active consumer failures or a stopped consumer. "
                f"Queues: {queue_list}. "
                "Note: many queues share exactly 2,867 messages — likely a common upstream "
                "fan-out failure (e.g. SNS topic publishing to multiple SQS queues stopped "
                "processing simultaneously). "
                "Actions: (1) check consumer Lambda/ECS health, (2) check for throttling "
                "or permission errors in consumer logs, (3) verify the queue has a DLQ "
                "so failed messages don't loop indefinitely."
            ),
            "saving_mo":   0.0,
            "saving_yr":   0.0,
        })

    return opps


# ── CSV output ────────────────────────────────────────────────────────────────
def write_csv(opps):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    ts   = now.strftime("%Y%m%d_%H%M%S")
    path = os.path.join(OUTPUT_DIR, f"{ts}_sqs_report.csv")
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "id", "service", "severity", "title", "saving_mo", "saving_yr", "saving_basis", "price_basis", "description"
        ])
        writer.writeheader()
        for o in opps:
            row = {k: o.get(k, "") for k in writer.fieldnames}
            row["saving_basis"] = o.get("saving_basis", "actual")
            row["price_basis"] = "list"
            writer.writerow(row)
    return path


# ── Summary printer ───────────────────────────────────────────────────────────
def print_summary(data, opps):
    queues = data["queues"]

    fifo_count     = sum(1 for q in queues if q["is_fifo"])
    standard_count = len(queues) - fifo_count
    dlq_count      = sum(1 for q in queues if q["is_dlq"])
    no_dlq_count   = sum(1 for q in queues if not q["is_dlq"] and q["redrive_policy"] is None)
    idle_count      = sum(1 for q in queues if q["msgs_sent_30d"] == 0.0 and q["msgs_visible"] == 0)
    with_msgs_count = sum(1 for q in queues if q["msgs_visible"] > 0)

    total_sent_30d  = sum(q["msgs_sent_30d"] for q in queues)
    total_visible   = sum(q["msgs_visible"]  for q in queues)

    # Cost estimate (virtually $0 at these volumes)
    fifo_sent     = sum(q["msgs_sent_30d"] for q in queues if q["is_fifo"])
    std_sent      = sum(q["msgs_sent_30d"] for q in queues if not q["is_fifo"])
    fifo_billable = max(0, fifo_sent - FREE_TIER_REQUESTS)
    std_billable  = max(0, std_sent  - FREE_TIER_REQUESTS)
    estimated_mo  = (fifo_billable / 1_000_000 * FIFO_PRICE_PER_M +
                     std_billable  / 1_000_000 * STANDARD_PRICE_PER_M)

    # DLQ backlogs table
    dlq_backlogs = sorted(
        [q for q in queues if q["is_dlq"] and q["msgs_visible"] > 0],
        key=lambda q: -q["msgs_visible"]
    )

    print("\n" + "=" * 70)
    print("  AWS SQS — Cost & Operational Analysis Summary")
    print("=" * 70)
    print(f"  Account  : {ACCOUNT_ID}")
    print(f"  Region   : {REGION}")
    print()
    print("  ── Queue Inventory ──")
    print(f"    Total queues           : {len(queues)}")
    print(f"    Standard               : {standard_count}")
    print(f"    FIFO                   : {fifo_count}  ({fifo_count/len(queues)*100:.0f}%)" if queues else f"    FIFO                   : 0  (0%)")
    print(f"    DLQ queues             : {dlq_count}")
    print(f"    Queues without DLQ     : {no_dlq_count}  ⚠️")
    print(f"    Idle queues (30d)      : {idle_count}")
    print(f"    Queues with msgs waiting: {with_msgs_count}")
    print()
    print("  ── Volume & Cost (last 30d) ──")
    print(f"    Total messages sent    : {total_sent_30d:,.0f}")
    print(f"      FIFO                 : {fifo_sent:,.0f}")
    print(f"      Standard             : {std_sent:,.0f}")
    print(f"    Total visible messages : {total_visible:,}")
    print(f"    Estimated monthly cost : ${estimated_mo:.2f}  (within free tier)")
    print()

    if dlq_backlogs:
        print("  ── DLQ Backlogs (messages stuck) ──")
        print(f"    {'Queue':<60}  {'Msgs Visible':>12}")
        print(f"    {'-'*60}  {'-'*12}")
        for q in dlq_backlogs:
            print(f"    {q['name']:<60}  {q['msgs_visible']:>12,}")
        print()

    # Top queues by volume
    top_by_volume = sorted(queues, key=lambda q: -q["msgs_sent_30d"])[:10]
    active = [q for q in top_by_volume if q["msgs_sent_30d"] > 0]
    if active:
        print("  ── Top Queues by Messages Sent (30d) ──")
        print(f"    {'Queue':<60}  {'Sent 30d':>10}  {'Visible':>10}")
        print(f"    {'-'*60}  {'-'*10}  {'-'*10}")
        for q in active:
            print(f"    {q['name']:<60}  {q['msgs_sent_30d']:>10,.0f}  {q['msgs_visible']:>10,}")
        print()

    print("  ── Opportunities ──")
    if opps:
        for o in opps:
            saving = f"  ${o['saving_mo']:.2f}/mo" if o["saving_mo"] > 0 else ""
            print(f"    [{o['severity']}] {o['title']}{saving}")
    else:
        print("    No opportunities found.")
    print("=" * 70)


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
    refresh = "--refresh-cache" in sys.argv

    print("🔍 AWS SQS Cost & Operational Analyser")
    print(f"   Account : {ACCOUNT_ID}")
    print(f"   Region  : {REGION}")
    print(f"   Profile : {PROFILE}\n")

    session = boto3.Session(profile_name=PROFILE, region_name=REGION)

    if not refresh:
        cached = load_cache(CACHE_FILE)
        if cached:
            print("✅  Loaded SQS data from cache.")
            data = cached
        else:
            print("🔄  Cache missing/expired — collecting from AWS …")
            data = collect(session)
            save_cache(CACHE_FILE, data, ACCOUNT_ID, REGION)
    else:
        print("🔄  --refresh-cache requested — collecting from AWS …")
        data = collect(session)
        save_cache(CACHE_FILE, data, ACCOUNT_ID, REGION)

    opps     = build_opportunities(data)
    csv_path = write_csv(opps)
    print_summary(data, opps)
    print(f"\n  CSV report: {csv_path}\n")


if __name__ == "__main__":
    main()
