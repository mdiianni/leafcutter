#!/usr/bin/env python3
"""
AWS Cost Analyser — SNS Skill

Key checks:
  - Test/POC topics with no production use (should be deleted)
  - Idle topics (0 messages published in 30d — likely orphaned)
  - Topics with 0 confirmed subscribers (messages silently discarded)
  - Healthcheck topics with no active subscriptions (monitoring blind spot)
  - Near-duplicate topic names (same purpose, two separate topics)
  - FIFO vs Standard ratio and fan-out cost implications

Real-world API notes:
  - list_topics and list_subscriptions both paginate via NextToken
  - get_topic_attributes returns SubscriptionsConfirmed, FifoTopic, KmsMasterKeyId
  - CW metric period=2592000 gives a single 30-day datapoint
  - Use ThreadPoolExecutor for CW calls — 27 topics is fast, but pattern scales
"""

from __future__ import annotations


import boto3
import csv
import os
import re
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
CACHE_FILE = os.path.join(CACHE_DIR, "sns.json")

now = datetime.now(timezone.utc)

# ── Severity ──────────────────────────────────────────────────────────────────
CRITICAL = "🔴 CRITICAL"
HIGH     = "🟠 HIGH"
MEDIUM   = "🟡 MEDIUM"
LOW      = "🟢 LOW"
INFO     = "ℹ️  INFO"

# ── Pricing (eu-central-1) ────────────────────────────────────────────────────
FREE_TIER_REQUESTS       = 1_000_000
STANDARD_PRICE_PER_M     = 0.50    # USD per million API calls beyond free tier
FIFO_PRICE_PER_M         = 0.33    # USD per million API calls beyond free tier
# SQS and Lambda deliveries are free; HTTP(S) is $0.60/M

# ── Thresholds ────────────────────────────────────────────────────────────────
TEST_POC_PATTERNS = re.compile(
    r"(test|poc|demo|temp|tmp|trial)", re.IGNORECASE
)
CW_MAX_WORKERS = 20


def _topic_name(arn: str) -> str:
    return arn.split(":")[-1]


def _is_fifo(name: str) -> bool:
    return name.endswith(".fifo")


# ── AWS data collection ───────────────────────────────────────────────────────
def _fetch_cw_published(cw, topic_name: str) -> float:
    """Return total NumberOfMessagesPublished over the last 30 days."""
    try:
        resp = cw.get_metric_statistics(
            Namespace="AWS/SNS",
            MetricName="NumberOfMessagesPublished",
            Dimensions=[{"Name": "TopicName", "Value": topic_name}],
            StartTime=now - timedelta(days=30),
            EndTime=now,
            Period=2592000,
            Statistics=["Sum"],
        )
        datapoints = resp.get("Datapoints", [])
        return float(datapoints[0]["Sum"]) if datapoints else 0.0
    except Exception:
        return 0.0


def collect(session):
    sns = session.client("sns", region_name=REGION)
    cw  = session.client("cloudwatch", region_name=REGION)

    # ── Topics (paginated) ─────────────────────────────────────────────────
    print("  Listing SNS topics …")
    topics_raw = []
    paginator  = sns.get_paginator("list_topics")
    for page in paginator.paginate():
        topics_raw.extend(page.get("Topics", []))
    print(f"    → {len(topics_raw)} topic(s)")

    # ── Topic attributes ───────────────────────────────────────────────────
    print("  Fetching topic attributes …")
    topics = []
    for t in topics_raw:
        arn  = t["TopicArn"]
        name = _topic_name(arn)
        try:
            attrs = sns.get_topic_attributes(TopicArn=arn)["Attributes"]
            subs_confirmed = int(attrs.get("SubscriptionsConfirmed", 0))
            subs_pending   = int(attrs.get("SubscriptionsPending",   0))
            subs_deleted   = int(attrs.get("SubscriptionsDeleted",    0))
            is_fifo        = attrs.get("FifoTopic", "false").lower() == "true"
            kms_key        = attrs.get("KmsMasterKeyId", "")
        except Exception as e:
            print(f"    ⚠️  {name}: {e}")
            subs_confirmed = subs_pending = subs_deleted = 0
            is_fifo = False
            kms_key = ""

        topics.append({
            "arn":             arn,
            "name":            name,
            "is_fifo":         is_fifo or _is_fifo(name),
            "subs_confirmed":  subs_confirmed,
            "subs_pending":    subs_pending,
            "subs_deleted":    subs_deleted,
            "kms_key":         kms_key,
            "published_30d":   0.0,   # filled in below
        })

    # ── Subscriptions (paginated) ──────────────────────────────────────────
    print("  Listing SNS subscriptions …")
    subs = []
    paginator = sns.get_paginator("list_subscriptions")
    for page in paginator.paginate():
        subs.extend(page.get("Subscriptions", []))
    print(f"    → {len(subs)} subscription(s)")

    # ── CloudWatch: published in 30d (parallel) ────────────────────────────
    print(f"  Fetching CloudWatch NumberOfMessagesPublished "
          f"(parallel, {CW_MAX_WORKERS} workers) …")
    name_to_idx = {t["name"]: i for i, t in enumerate(topics)}
    with ThreadPoolExecutor(max_workers=CW_MAX_WORKERS) as pool:
        futures = {
            pool.submit(_fetch_cw_published, cw, t["name"]): t["name"]
            for t in topics
        }
        for future in as_completed(futures):
            name = futures[future]
            if name in name_to_idx:
                topics[name_to_idx[name]]["published_30d"] = future.result()
    print(f"    → CW metrics fetched for {len(topics)} topics")

    return {
        "topics":        topics,
        "subscriptions": subs,
        "collected_at":  now.isoformat(),
    }


# ── Opportunity builders ──────────────────────────────────────────────────────
def build_opportunities(data):
    opps   = []
    topics = data["topics"]
    subs   = data["subscriptions"]

    # Protocol breakdown
    protocol_counts: dict[str, int] = {}
    for s in subs:
        p = s.get("Protocol", "unknown")
        protocol_counts[p] = protocol_counts.get(p, 0) + 1

    fifo_count     = sum(1 for t in topics if t["is_fifo"])
    standard_count = len(topics) - fifo_count

    # ── Opp 1: Test/POC topics ─────────────────────────────────────────────
    test_topics = [
        t for t in topics
        if TEST_POC_PATTERNS.search(t["name"]) and t["published_30d"] == 0.0
    ]
    if test_topics:
        names = ", ".join(t["name"] for t in test_topics)
        opps.append({
            "id":          "sns-test-poc-topics",
            "service":     "SNS",
            "severity":    HIGH,
            "title":       f"{len(test_topics)} test/POC topics with 0 publishes — delete them",
            "description": (
                f"{len(test_topics)} topics have names matching test/poc/demo/temp patterns "
                "and have published 0 messages in the last 30 days. "
                f"Topics: {names}. "
                "These are likely leftover from experiments or one-off POCs. "
                "Deleting them reduces clutter and removes the risk of accidentally "
                "subscribing a real consumer to a test topic."
            ),
            "saving_mo":   0.0,
            "saving_yr":   0.0,
        })

    # ── Opp 2: Idle topics ─────────────────────────────────────────────────
    idle_topics = [t for t in topics if t["published_30d"] == 0.0]
    if idle_topics:
        # Split into sub-categories
        no_subs = [t for t in idle_topics if t["subs_confirmed"] == 0]
        with_subs = [t for t in idle_topics if t["subs_confirmed"] > 0]

        # Group by rough category prefix
        category_counts: dict[str, int] = {}
        for t in idle_topics:
            # First two meaningful tokens (skip common org prefix)
            parts = t["name"].split("-")
            cat = "-".join(parts[:3]) if len(parts) >= 3 else t["name"]
            category_counts[cat] = category_counts.get(cat, 0) + 1

        top_cats = sorted(category_counts.items(), key=lambda x: -x[1])[:6]
        cat_summary = ", ".join(f"{c} ({n})" for c, n in top_cats)

        opps.append({
            "id":          "sns-idle-topics",
            "service":     "SNS",
            "severity":    MEDIUM,
            "title":       f"{len(idle_topics)} idle topics (0 published in 30d)",
            "description": (
                f"{len(idle_topics)} topics have published 0 messages in the last 30 days. "
                f"{len(no_subs)} of those also have 0 confirmed subscribers. "
                f"Top prefix groups: {cat_summary}. "
                "Idle topics are likely orphaned from decommissioned services. "
                "Review with the owning team before deleting — check for seasonal workflows "
                "that may publish infrequently."
            ),
            "saving_mo":   0.0,
            "saving_yr":   0.0,
        })

    # ── Opp 3: Topics with 0 confirmed subscribers ─────────────────────────
    no_sub_topics = [t for t in topics if t["subs_confirmed"] == 0]
    if no_sub_topics:
        names = ", ".join(t["name"] for t in no_sub_topics[:8])
        publishing = [t for t in no_sub_topics if t["published_30d"] > 0]
        pub_note   = (
            f" {len(publishing)} of these topics ARE actively publishing — messages are "
            "being silently discarded."
            if publishing else ""
        )
        opps.append({
            "id":          "sns-topics-no-subscriptions",
            "service":     "SNS",
            "severity":    MEDIUM,
            "title":       f"{len(no_sub_topics)} topics have 0 confirmed subscribers",
            "description": (
                f"{len(no_sub_topics)} topics have no confirmed subscriptions. "
                "Any messages published to these topics are silently discarded — "
                "there is no error, no DLQ, no record."
                f"{pub_note} "
                f"Topics (first 8): {names}. "
                "Either the subscribing service failed to create its subscription "
                "(misconfiguration) or the topic is completely orphaned."
            ),
            "saving_mo":   0.0,
            "saving_yr":   0.0,
        })

    # ── Opp 4: Healthcheck topic with 0 subscribers ────────────────────────
    healthcheck_topics = [
        t for t in topics
        if "healthcheck" in t["name"].lower() and t["subs_confirmed"] == 0
    ]
    for t in healthcheck_topics:
        opps.append({
            "id":          "sns-healthcheck-no-subs",
            "service":     "SNS",
            "severity":    LOW,
            "title":       f"Healthcheck topic '{t['name']}' has 0 subscribers — alerts going nowhere",
            "description": (
                f"Topic '{t['name']}' publishes healthcheck/alert messages "
                f"({t['published_30d']:.0f} in 30d) but has 0 confirmed subscribers. "
                "Healthcheck alerts are being silently discarded — this is an operational "
                "blind spot. Add a subscription (email, Lambda, PagerDuty via HTTPS) to "
                "ensure infrastructure alerts are actually received."
            ),
            "saving_mo":   0.0,
            "saving_yr":   0.0,
        })

    # ── Opp 5: Near-duplicate topic names ─────────────────────────────────
    # Normalise: lowercase, strip .fifo, collapse non-alphanumeric to space,
    # then check for pairs sharing ≥80% of tokens
    def _normalise(name: str) -> set[str]:
        n = name.lower().removesuffix(".fifo")
        tokens = set(re.split(r"[^a-z0-9]+", n)) - {"", "dev", "eu", "central", "1", "sns"}
        return tokens

    pairs = []
    names_list = [(t["name"], _normalise(t["name"])) for t in topics]
    seen: set[tuple[str, str]] = set()
    for i, (n1, t1) in enumerate(names_list):
        for j, (n2, t2) in enumerate(names_list):
            if i >= j:
                continue
            if not t1 or not t2:
                continue
            intersection = t1 & t2
            union        = t1 | t2
            similarity   = len(intersection) / len(union)
            if similarity >= 0.60 and n1 != n2:
                pair = tuple(sorted([n1, n2]))
                if pair not in seen:
                    seen.add(pair)
                    pairs.append((n1, n2, similarity))

    if pairs:
        pairs.sort(key=lambda x: -x[2])
        pair_list = "; ".join(
            f"'{a}' ↔ '{b}' ({sim:.0%} overlap)" for a, b, sim in pairs[:6]
        )
        opps.append({
            "id":          "sns-duplicate-topic-names",
            "service":     "SNS",
            "severity":    LOW,
            "title":       f"{len(pairs)} near-duplicate topic name pair(s) detected",
            "description": (
                f"{len(pairs)} topic pair(s) appear to serve the same purpose based on "
                "name similarity. Duplicate topics split the fan-out and can cause "
                "message routing bugs where some consumers receive events and others don't. "
                f"Pairs: {pair_list}. "
                "Consolidate to a single topic per logical event stream."
            ),
            "saving_mo":   0.0,
            "saving_yr":   0.0,
        })

    # ── Opp 6: FIFO topic ratio ────────────────────────────────────────────
    if fifo_count > 0 and standard_count > 0:
        fifo_pct = fifo_count / len(topics) * 100
        total_published = sum(t["published_30d"] for t in topics)
        opps.append({
            "id":          "sns-fifo-topics",
            "service":     "SNS",
            "severity":    INFO,
            "title":       (
                f"{fifo_count}/{len(topics)} topics ({fifo_pct:.0f}%) are FIFO — "
                "review ordering requirements"
            ),
            "description": (
                f"{fifo_count} of {len(topics)} SNS topics ({fifo_pct:.0f}%) are FIFO. "
                f"FIFO costs ${FIFO_PRICE_PER_M}/M vs ${STANDARD_PRICE_PER_M}/M for Standard. "
                f"Total published in 30d: {total_published:,.0f} — within free tier, cost is $0. "
                "FIFO topics can only fan out to FIFO SQS queues, which also cost 25% more than "
                "Standard SQS. "
                "🔗 Changing topic type requires recreating the topic and all subscriptions, "
                "and updating all publisher/consumer code — high coordination cost. "
                "Evaluate only for high-volume topics where ordering is genuinely not required."
            ),
            "saving_mo":   0.0,
            "saving_yr":   0.0,
        })

    return opps


# ── CSV output ────────────────────────────────────────────────────────────────
def write_csv(opps):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    ts   = now.strftime("%Y%m%d_%H%M%S")
    path = os.path.join(OUTPUT_DIR, f"{ts}_sns_report.csv")
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
    topics = data["topics"]
    subs   = data["subscriptions"]

    fifo_count     = sum(1 for t in topics if t["is_fifo"])
    standard_count = len(topics) - fifo_count
    idle_count     = sum(1 for t in topics if t["published_30d"] == 0.0)
    no_sub_count   = sum(1 for t in topics if t["subs_confirmed"] == 0)
    active_topics  = [t for t in topics if t["published_30d"] > 0]
    active_topics.sort(key=lambda t: -t["published_30d"])

    total_published = sum(t["published_30d"] for t in topics)

    # Protocol breakdown
    protocol_counts: dict[str, int] = {}
    for s in subs:
        p = s.get("Protocol", "unknown")
        protocol_counts[p] = protocol_counts.get(p, 0) + 1

    pending_conf = sum(1 for s in subs if s.get("SubscriptionArn") == "PendingConfirmation")

    # Cost estimate
    fifo_pub     = sum(t["published_30d"] for t in topics if t["is_fifo"])
    std_pub      = sum(t["published_30d"] for t in topics if not t["is_fifo"])
    fifo_bill    = max(0, fifo_pub - FREE_TIER_REQUESTS)
    std_bill     = max(0, std_pub  - FREE_TIER_REQUESTS)
    estimated_mo = (fifo_bill / 1_000_000 * FIFO_PRICE_PER_M +
                    std_bill  / 1_000_000 * STANDARD_PRICE_PER_M)

    print("\n" + "=" * 70)
    print("  AWS SNS — Cost & Operational Analysis Summary")
    print("=" * 70)
    print(f"  Account  : {ACCOUNT_ID}")
    print(f"  Region   : {REGION}")
    print()
    print("  ── Topic Inventory ──")
    print(f"    Total topics              : {len(topics)}")
    print(f"    Standard                  : {standard_count}")
    print(f"    FIFO                      : {fifo_count}  ({fifo_count/max(len(topics),1)*100:.0f}%)")
    print(f"    Active (published 30d)    : {len(active_topics)}")
    print(f"    Idle (0 published 30d)    : {idle_count}")
    print(f"    Topics with 0 subscribers : {no_sub_count}  ⚠️")
    print()
    print("  ── Subscriptions ──")
    print(f"    Total subscriptions       : {len(subs)}")
    print(f"    Pending confirmation      : {pending_conf}")
    for proto, cnt in sorted(protocol_counts.items(), key=lambda x: -x[1]):
        print(f"    {proto:<28} : {cnt}")
    print()
    print("  ── Volume & Cost (last 30d) ──")
    print(f"    Total messages published  : {total_published:,.0f}")
    print(f"      FIFO                    : {fifo_pub:,.0f}")
    print(f"      Standard                : {std_pub:,.0f}")
    print(f"    Estimated monthly cost    : ${estimated_mo:.2f}  (within free tier)")
    print()

    if active_topics:
        print("  ── Active Topics (published in 30d) ──")
        print(f"    {'Topic':<55}  {'Published':>10}  {'Subs':>5}  {'Type':>8}")
        print(f"    {'-'*55}  {'-'*10}  {'-'*5}  {'-'*8}")
        for t in active_topics:
            ttype = "FIFO" if t["is_fifo"] else "Standard"
            print(
                f"    {t['name']:<55}  "
                f"{t['published_30d']:>10,.0f}  "
                f"{t['subs_confirmed']:>5}  "
                f"{ttype:>8}"
            )
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

    print("🔍 AWS SNS Cost & Operational Analyser")
    print(f"   Account : {ACCOUNT_ID}")
    print(f"   Region  : {REGION}")
    print(f"   Profile : {PROFILE}\n")

    session = boto3.Session(profile_name=PROFILE, region_name=REGION)

    if not refresh:
        cached = load_cache(CACHE_FILE)
        if cached:
            print("✅  Loaded SNS data from cache.")
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
