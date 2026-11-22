#!/usr/bin/env python3
"""
report.py — Aggregated Cost Optimisation Report
─────────────────────────────────────────────────
Reads the latest CSV output from every analyse_*.py script and prints a
consolidated cost-saving report for one or more accounts / environments.

Usage
─────
  python3 report.py                              # all accounts, all services
  python3 report.py --group projectA             # filter by project group
  python3 report.py --group projectA --env prd   # filter by group + environment
  python3 report.py --account 123456789012       # filter by specific account ID
  python3 report.py --group projectA --top 20    # show top 20 opportunities (default 15)
  python3 report.py --group projectA --csv       # also write filtered CSV to output/
  python3 report.py --group projectA --min-saving 100  # only opps saving ≥ $100/month

Filtering is additive (AND): --group projectA --env dev --min-saving 50
shows all opportunities for projectA-dev accounts that save at least $50/month.

Accounts are identified via accounts.json (same file used by the dashboard).
If accounts.json is absent, all CSVs from the output/ directory are included.

Output
──────
  ╔══ Summary ══╗  — totals across all matched accounts & services
  Per-service breakdown  — cost + saving per AWS service
  By severity            — CRITICAL / HIGH / MEDIUM / LOW opportunity counts
  Top N opportunities    — sorted by monthly saving descending
  Optional CSV export    — output/report_<timestamp>.csv
"""

from __future__ import annotations


import argparse
import csv
import glob
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone

from box_utils import _bl, BORDER_TOP, BORDER_MID, BORDER_BOT

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE       = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(BASE, "output")
ACCTS_FILE = os.path.join(BASE, "accounts.json")

# ── Severity ordering ─────────────────────────────────────────────────────────
SEV_ORDER = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3,
             "DEPENDENCY": 4, "INFO": 5}

def sev_key(s: str) -> int:
    return SEV_ORDER.get(s.upper(), 99)

# ── Account registry ──────────────────────────────────────────────────────────
def load_accounts() -> dict:
    """Return {account_id: {"name": ..., "env": ..., "group": ...}}."""
    if not os.path.exists(ACCTS_FILE):
        return {}
    with open(ACCTS_FILE) as f:
        raw = json.load(f)
    out = {}
    for aid, v in raw.items():
        if aid.startswith("_"):
            continue
        if isinstance(v, (list, tuple)):
            out[aid] = {"name": v[0], "env": v[1], "group": v[2]}
        else:
            out[aid] = {
                "name":  v.get("name",  aid),
                "env":   v.get("env",   "unknown"),
                "group": v.get("group", "unknown"),
            }
    return out

# ── CSV loading ───────────────────────────────────────────────────────────────
def load_latest_csvs() -> dict:
    """Return {(account_id, service_slug): (timestamp, path)} using latest-wins dedup."""
    all_csv = sorted(glob.glob(os.path.join(OUTPUT_DIR, "*.csv")))
    latest: dict = {}
    for fpath in all_csv:
        fname = os.path.basename(fpath)
        m = re.match(r"(\d{8}_\d{6})_(.+)_report\.csv$", fname)
        if not m:
            continue
        ts, svc_slug = m.group(1), m.group(2)
        try:
            with open(fpath, newline="", encoding="utf-8") as f:
                first = next(csv.DictReader(f), None)
            if not first:
                continue
            aid = (first.get("account_id") or first.get("AccountId", "")).strip('"')
            if not aid:
                continue
        except Exception:
            continue
        key = (aid, svc_slug)
        if key not in latest or ts > latest[key][0]:
            latest[key] = (ts, fpath)
    return latest


def load_rows(latest: dict, account_ids: set | None) -> list[dict]:
    """Load all rows from the selected CSVs, normalising fields."""
    rows = []
    for (aid, svc_slug), (ts, fpath) in sorted(latest.items()):
        if account_ids and aid not in account_ids:
            continue
        try:
            with open(fpath, newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    # Normalise account_id
                    row["account_id"]  = row.get("account_id", aid).strip('"')
                    row["service"]     = svc_slug
                    row["csv_ts"]      = ts
                    # Unify the resource label (different scripts use different cols)
                    row["resource"] = (
                        row.get("resource_name") or
                        row.get("repository") or
                        row.get("bucket") or
                        row.get("service") or    # ECS service col
                        row.get("resource_id") or
                        row.get("function_name") or
                        ""
                    ).strip('"')
                    rows.append(row)
        except Exception as e:
            print(f"  ⚠️  Could not read {fpath}: {e}", file=sys.stderr)
    return rows


def safe_float(v) -> float:
    try:
        return float(str(v).strip('"'))
    except (ValueError, TypeError):
        return 0.0

# ── Filtering ─────────────────────────────────────────────────────────────────
def filter_rows(rows: list[dict], min_saving: float, exclude_info: bool) -> list[dict]:
    out = []
    for r in rows:
        sev = r.get("severity", "").strip('"').upper()
        if exclude_info and sev == "INFO":
            continue
        if safe_float(r.get("estimated_saving_usd_monthly", 0)) < min_saving:
            continue
        out.append(r)
    return out

# ── Deduplication ─────────────────────────────────────────────────────────────
def deduplicate(rows: list[dict]) -> list[dict]:
    """Keep one row per (account_id, service, opportunity_type, resource)."""
    seen: dict = {}
    out = []
    for r in rows:
        key = (
            r.get("account_id", ""),
            r.get("service", ""),
            r.get("opportunity_type", ""),
            r.get("resource", ""),
        )
        if key not in seen:
            seen[key] = True
            out.append(r)
    return out

# ── Formatting helpers ────────────────────────────────────────────────────────
SEV_ICON = {
    "CRITICAL":   "🔴",
    "HIGH":       "🟠",
    "MEDIUM":     "🟡",
    "LOW":        "🟢",
    "DEPENDENCY": "🔗",
    "INFO":       "ℹ️ ",
}

def sev_icon(s: str) -> str:
    return SEV_ICON.get(s.upper(), "⚪")


def fmt_usd(v: float) -> str:
    return f"${v:,.2f}"


def pct(part: float, total: float) -> str:
    if total <= 0:
        return "  —  "
    return f"{part / total * 100:5.1f}%"


def truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"

# ── Report sections ───────────────────────────────────────────────────────────
def print_summary(rows, accounts, matched_ids, args):
    total_current = sum(safe_float(r.get("estimated_saving_usd_monthly")) +
                        safe_float(r.get("current_cost_usd_monthly", 0)) -
                        safe_float(r.get("estimated_saving_usd_monthly", 0))
                        for r in rows)
    total_current = sum(safe_float(r.get("current_cost_usd_monthly", 0)) for r in rows)
    total_saving  = sum(safe_float(r.get("estimated_saving_usd_monthly", 0)) for r in rows)
    total_annual  = total_saving * 12
    sev_counts    = defaultdict(int)
    for r in rows:
        sev_counts[r.get("severity", "").strip('"').upper()] += 1

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    group_str = ", ".join(sorted({accounts.get(a, {}).get("group", a) for a in matched_ids})) or "all"
    env_str   = ", ".join(sorted({accounts.get(a, {}).get("env",   "?") for a in matched_ids})) or "all"
    acct_str  = f"{len(matched_ids)} account(s)"

    print()
    print(BORDER_TOP)
    print(_bl("       AWS Cost Analyser — Aggregated Opportunity Report"))
    print(BORDER_MID)
    print(_bl(f"  Generated  : {now_str}"))
    print(_bl(f"  Group      : {group_str}"))
    print(_bl(f"  Environment: {env_str}"))
    print(_bl(f"  Accounts   : {acct_str}"))
    print(_bl(f"  Services   : {len({r['service'] for r in rows})} with findings"))
    print(_bl(f"  Min saving : {fmt_usd(args.min_saving)}/month filter applied"))
    print(BORDER_MID)
    print(_bl(f"  💸 Est. total monthly cost (in-scope)  : {fmt_usd(total_current):>12}"))
    print(_bl(f"  💰 Total potential monthly saving      : {fmt_usd(total_saving):>12}"))
    print(_bl(f"  💰 Total potential annual saving       : {fmt_usd(total_annual):>12}"))
    if total_current > 0:
        print(_bl(f"  📊 Saving as % of in-scope spend       : {total_saving/total_current*100:>11.1f}%"))
    print(BORDER_MID)
    sev_line = "  Severity   : " + "  ".join(
        f"{sev_icon(s)} {s} {sev_counts[s]}"
        for s in ["CRITICAL", "HIGH", "MEDIUM", "LOW", "DEPENDENCY"]
        if sev_counts[s] > 0
    )
    print(_bl(sev_line))
    print(_bl(f"  🔍 Total opportunities                 : {len(rows):>12,}"))
    print(BORDER_BOT)


def print_by_service(rows):
    svc_data: dict = defaultdict(lambda: {"current": 0.0, "saving": 0.0, "count": 0})
    for r in rows:
        s = r["service"]
        svc_data[s]["current"] += safe_float(r.get("current_cost_usd_monthly", 0))
        svc_data[s]["saving"]  += safe_float(r.get("estimated_saving_usd_monthly", 0))
        svc_data[s]["count"]   += 1

    sorted_svcs = sorted(svc_data.items(), key=lambda x: -x[1]["saving"])
    total_saving = sum(v["saving"] for v in svc_data.values())

    print(f"\n{'─'*78}")
    print("  💡 Savings by Service\n")
    print(f"  {'Service':<22} {'Opportunities':>13}  {'Monthly Cost':>13}  {'Monthly Saving':>14}  {'% of Total':>10}")
    print(f"  {'─'*22} {'─'*13}  {'─'*13}  {'─'*14}  {'─'*10}")
    for svc, d in sorted_svcs:
        share = pct(d["saving"], total_saving)
        print(f"  {svc:<22} {d['count']:>13,}  {fmt_usd(d['current']):>13}  {fmt_usd(d['saving']):>14}  {share:>10}")
    print(f"  {'─'*22} {'─'*13}  {'─'*13}  {'─'*14}  {'─'*10}")
    total_current = sum(v["current"] for v in svc_data.values())
    total_opps    = sum(v["count"]   for v in svc_data.values())
    print(f"  {'TOTAL':<22} {total_opps:>13,}  {fmt_usd(total_current):>13}  {fmt_usd(total_saving):>14}  {'100.0%':>10}")


def print_by_account(rows, accounts):
    acc_data: dict = defaultdict(lambda: {"current": 0.0, "saving": 0.0, "count": 0})
    for r in rows:
        aid = r["account_id"]
        acc_data[aid]["current"] += safe_float(r.get("current_cost_usd_monthly", 0))
        acc_data[aid]["saving"]  += safe_float(r.get("estimated_saving_usd_monthly", 0))
        acc_data[aid]["count"]   += 1

    if len(acc_data) <= 1:
        return  # single account — not useful to show

    sorted_accs = sorted(acc_data.items(), key=lambda x: -x[1]["saving"])

    print(f"\n{'─'*78}")
    print("  🏦 Savings by Account\n")
    print(f"  {'Account ID':<14} {'Name':<20} {'Env':<6} {'Opps':>5}  {'Monthly Cost':>13}  {'Monthly Saving':>14}")
    print(f"  {'─'*14} {'─'*20} {'─'*6} {'─'*5}  {'─'*13}  {'─'*14}")
    for aid, d in sorted_accs:
        meta  = accounts.get(aid, {})
        name  = truncate(meta.get("name", aid), 20)
        env   = meta.get("env", "?")
        print(f"  {aid:<14} {name:<20} {env:<6} {d['count']:>5,}  {fmt_usd(d['current']):>13}  {fmt_usd(d['saving']):>14}")


def print_top_opportunities(rows, top_n: int):
    sorted_rows = sorted(rows, key=lambda r: -safe_float(r.get("estimated_saving_usd_monthly", 0)))

    print(f"\n{'─'*78}")
    print(f"  🏆 Top {top_n} Opportunities by Monthly Saving\n")

    header = f"  {'#':>3}  {'Sev':<4}  {'Service':<16}  {'Resource':<34}  {'Saving/mo':>10}  {'Saving/yr':>10}"
    print(header)
    print(f"  {'─'*3}  {'─'*4}  {'─'*16}  {'─'*34}  {'─'*10}  {'─'*10}")

    for i, r in enumerate(sorted_rows[:top_n], 1):
        sev     = r.get("severity", "").strip('"').upper()
        svc     = truncate(r["service"], 16)
        res     = truncate(r["resource"], 34)
        saving  = safe_float(r.get("estimated_saving_usd_monthly", 0))
        print(f"  {i:>3}  {sev_icon(sev):<4}  {svc:<16}  {res:<34}  {fmt_usd(saving):>10}  {fmt_usd(saving*12):>10}")

    print()
    print(f"  {'─'*78}")
    print(f"\n  📋 Recommendations for Top {min(top_n, len(sorted_rows))} Opportunities:\n")
    for i, r in enumerate(sorted_rows[:top_n], 1):
        sev   = r.get("severity", "").strip('"').upper()
        svc   = r["service"]
        res   = r["resource"]
        rec   = r.get("recommendation", "").strip('"')
        saving = safe_float(r.get("estimated_saving_usd_monthly", 0))
        dep   = r.get("dependency_warning", "").strip('"')
        print(f"  [{i}] {sev_icon(sev)} {sev} | {svc} | {fmt_usd(saving)}/mo")
        print(f"      Resource   : {res}")
        if dep:
            print(f"      ⚠️  Dep warn : {dep}")
        # Wrap recommendation text at ~74 chars
        words, line = rec.split(), ""
        for w in words:
            if len(line) + len(w) + 1 > 74:
                print(f"      {line}")
                line = w
            else:
                line = (line + " " + w).strip()
        if line:
            print(f"      {line}")
        print()


def export_csv(rows, accounts, args):
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix = ""
    if args.group:
        suffix += "_" + "_".join(args.group)
    if args.env:
        suffix += "_" + args.env
    out_path = os.path.join(OUTPUT_DIR, f"{ts}_aggregated_report{suffix}.csv")

    fieldnames = [
        "account_id", "account_name", "env", "group",
        "service", "resource", "opportunity_type", "severity",
        "current_cost_usd_monthly", "estimated_saving_usd_monthly", "saving_pct",
        "dependency_warning", "recommendation",
    ]
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for r in sorted(rows, key=lambda x: -safe_float(x.get("estimated_saving_usd_monthly", 0))):
            aid  = r.get("account_id", "")
            meta = accounts.get(aid, {})
            writer.writerow({
                **r,
                "account_name": meta.get("name", aid),
                "env":          meta.get("env",  "unknown"),
                "group":        meta.get("group","unknown"),
            })
    print(f"\n  📁 Exported: {out_path}")
    return out_path


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="Aggregated AWS cost optimisation report across all services.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 report.py
  python3 report.py --group projectA
  python3 report.py --group projectA --env prd
  python3 report.py --group projectA projectB --env dev --top 20
  python3 report.py --account 123456789012
  python3 report.py --group projectA --csv --min-saving 50
        """,
    )
    parser.add_argument("--group",       nargs="+", metavar="GROUP",
                        help="Filter by project group(s) from accounts.json")
    parser.add_argument("--env",         metavar="ENV",
                        help="Filter by environment (dev / stg / prd)")
    parser.add_argument("--account",     nargs="+", metavar="ACCOUNT_ID",
                        help="Filter by specific AWS account ID(s)")
    parser.add_argument("--top",         type=int, default=15, metavar="N",
                        help="Number of top opportunities to display (default: 15)")
    parser.add_argument("--min-saving",  type=float, default=0.0, metavar="USD",
                        help="Only include opportunities saving ≥ USD/month (default: 0)")
    parser.add_argument("--csv",         action="store_true",
                        help="Export filtered results to a CSV file in output/")
    parser.add_argument("--include-info", action="store_true",
                        help="Include INFO-severity findings (excluded by default)")
    parser.add_argument("--services",    nargs="+", metavar="SVC",
                        help="Limit to specific service slugs (e.g. ec2 ecs ecr)")
    args = parser.parse_args()

    accounts = load_accounts()

    # ── Resolve account IDs to include ────────────────────────────────────────
    account_ids: set | None = None
    if args.account:
        account_ids = set(args.account)
    elif args.group or args.env:
        account_ids = set()
        for aid, meta in accounts.items():
            if args.group and meta.get("group") not in args.group:
                continue
            if args.env and meta.get("env") != args.env:
                continue
            account_ids.add(aid)
        if not account_ids:
            print(f"\n⚠️  No accounts matched group={args.group} env={args.env}.")
            print("   Check your accounts.json configuration.\n")
            sys.exit(1)

    # ── Load CSVs ─────────────────────────────────────────────────────────────
    latest = load_latest_csvs()
    if not latest:
        print(f"\n⚠️  No *_report.csv files found in {OUTPUT_DIR}/")
        print("   Run python3 run_analysis.py --profile <profile> first.\n")
        sys.exit(1)

    rows = load_rows(latest, account_ids)

    # ── Apply service filter ───────────────────────────────────────────────────
    if args.services:
        rows = [r for r in rows if r["service"] in args.services]

    # ── Apply severity + min-saving filters ───────────────────────────────────
    rows = filter_rows(rows, args.min_saving, exclude_info=not args.include_info)

    # ── Deduplicate ───────────────────────────────────────────────────────────
    rows = deduplicate(rows)

    if not rows:
        print("\n⚠️  No opportunities found matching the given filters.\n")
        sys.exit(0)

    matched_ids = set(r["account_id"] for r in rows)

    # ── Print report ──────────────────────────────────────────────────────────
    print_summary(rows, accounts, matched_ids, args)
    print_by_service(rows)
    print_by_account(rows, accounts)
    print_top_opportunities(rows, args.top)

    # ── Optional CSV export ───────────────────────────────────────────────────
    if args.csv:
        export_csv(rows, accounts, args)


if __name__ == "__main__":
    main()
