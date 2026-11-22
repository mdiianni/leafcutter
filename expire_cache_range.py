#!/usr/bin/env python3
"""
expire_cache_range.py — Expire cache records collected within a date range.

Use this to invalidate data gathered during a known-bad period (e.g. a bug
that caused double-aggregation, a misconfigured analyser, or an account that
was temporarily misconfigured).

Records are NOT deleted — their `expires_at` timestamp is set to just before
the range start so they are treated as stale on the next analysis run while
the historical audit trail is preserved.

After expiry the script optionally rebuilds the dashboard data so the next
view of the dashboard reflects the cleaned state.

Usage examples
──────────────
  # Expire all cache records collected between Apr 11 and May 29, 2026
  python3 expire_cache_range.py --start 2026-04-11 --end 2026-05-29

  # Same but dry-run first (shows what would be expired without touching files)
  python3 expire_cache_range.py --start 2026-04-11 --end 2026-05-29 --dry-run

  # Expire only EBS and EC2 cache for a specific region
  python3 expire_cache_range.py --start 2026-04-11 --end 2026-05-29 \\
      --services ebs ec2 --region eu-central-1

  # Expire for specific accounts only
  python3 expire_cache_range.py --start 2026-04-11 --end 2026-05-29 \\
      --accounts 123456789012 234567890123

  # Expire and immediately rebuild dashboard data
  python3 expire_cache_range.py --start 2026-04-11 --end 2026-05-29 --rebuild-dashboard

  # List all records within the range (inspect before expiring)
  python3 expire_cache_range.py --start 2026-04-11 --end 2026-05-29 --list-only
"""

from __future__ import annotations


import argparse
import glob
import json
import os
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

HERE = Path(__file__).parent

# Add parent dir to path so cache_utils is importable
sys.path.insert(0, str(HERE))
from cache_utils import expire_cache_range, load_all_records

CACHE_DIR = HERE / "cache"


# ── Helpers ────────────────────────────────────────────────────────────────────

def parse_date(s: str) -> datetime:
    """Parse YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS into a timezone-aware UTC datetime."""
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            dt = datetime.strptime(s, fmt)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(
        f"Date '{s}' is not in YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS format"
    )


def find_cache_files(
    accounts: list[str] | None,
    regions:  list[str] | None,
    services: list[str] | None,
) -> list[tuple[str, str, str, Path]]:
    """
    Scan the cache directory tree and return matching (account, region, service, path) tuples.
    Filters by account IDs, regions, and service slugs when provided.
    """
    results = []
    if not CACHE_DIR.exists():
        return results

    for acc_dir in sorted(CACHE_DIR.iterdir()):
        if not acc_dir.is_dir():
            continue
        acc_id = acc_dir.name
        if acc_id in ("pricing",):      # skip pricing sub-cache
            continue
        if accounts and acc_id not in accounts:
            continue

        for reg_dir in sorted(acc_dir.iterdir()):
            if not reg_dir.is_dir():
                continue
            region = reg_dir.name
            if regions and region not in regions:
                continue

            for cache_file in sorted(reg_dir.glob("*.json")):
                service = cache_file.stem
                if services and service not in services:
                    continue
                results.append((acc_id, region, service, cache_file))

    return results


def records_in_range(
    path: Path,
    start: datetime,
    end: datetime,
) -> list[dict]:
    """Return all records in path whose collected_at falls within [start, end]."""
    all_records = load_all_records(str(path))
    in_range = []
    for r in all_records:
        collected_str = r.get("collected_at", "")
        if not collected_str:
            continue
        try:
            collected = datetime.fromisoformat(collected_str)
            if collected.tzinfo is None:
                collected = collected.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if start <= collected <= end:
            in_range.append({
                "collected_at": collected_str,
                "expires_at":   r.get("expires_at", ""),
                "already_expired": (
                    datetime.fromisoformat(r["expires_at"]).replace(tzinfo=timezone.utc)
                    < datetime.now(timezone.utc)
                ) if r.get("expires_at") else False,
            })
    return in_range


def rebuild_dashboard(python: str | None = None) -> bool:
    """Regenerate dashboard_data.json and rebuild the static HTML."""
    py = python or sys.executable
    script = HERE / "generate_dashboard_data.py"
    print("\n📊 Rebuilding dashboard data…")
    r = subprocess.run([py, str(script)], cwd=str(HERE))
    if r.returncode != 0:
        print("  ⚠️  Dashboard data regeneration failed", file=sys.stderr)
        return False
    print("  ✅ Dashboard data regenerated")

    # Vite build
    dashboard_dir = HERE / "dashboard"
    node_modules  = dashboard_dir / "node_modules"
    if node_modules.exists():
        print("  🔨 Rebuilding HTML export…")
        env = {**os.environ, "BUILD_MODE": "singlefile"}
        rb = subprocess.run(
            ["npm", "run", "build"],
            cwd=str(dashboard_dir),
            env=env,
            capture_output=True,
        )
        if rb.returncode == 0:
            # Copy built file
            src = dashboard_dir / "dist" / "index.html"
            dst = HERE / "output" / "AWS_Cost_Dashboard.html"
            dst.parent.mkdir(exist_ok=True)
            if src.exists():
                import shutil
                shutil.copy2(src, dst)
                size_kb = dst.stat().st_size // 1024
                print(f"  ✅ output/AWS_Cost_Dashboard.html  ({size_kb} KB)")
        else:
            print("  ⚠️  HTML build failed (npm run build returned non-zero)", file=sys.stderr)
    else:
        print("  ℹ️  dashboard/node_modules not found — skipping HTML rebuild")
        print("       Run: cd dashboard && npm install && npm run build")

    return True


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        prog="python3 expire_cache_range.py",
        description=(
            "Expire cache records collected within a date range. "
            "Records are marked stale (not deleted) so the next analysis run "
            "fetches fresh data while preserving the audit trail."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    # Required date range
    parser.add_argument(
        "--start", "-s",
        type=parse_date,
        required=True,
        metavar="YYYY-MM-DD",
        help="Start of the bad period (inclusive). Format: YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS",
    )
    parser.add_argument(
        "--end", "-e",
        type=parse_date,
        required=True,
        metavar="YYYY-MM-DD",
        help=(
            "End of the bad period (inclusive). A bare date (YYYY-MM-DD) "
            "is treated as end-of-day (23:59:59 UTC)."
        ),
    )

    # Filters
    parser.add_argument(
        "--accounts", "-a",
        nargs="+",
        metavar="ACCOUNT_ID",
        help="Limit to specific AWS account IDs. Omit for all accounts.",
    )
    parser.add_argument(
        "--region", "-r",
        nargs="+",
        metavar="REGION",
        dest="regions",
        help="Limit to specific AWS regions. Omit for all regions.",
    )
    parser.add_argument(
        "--services", "-v",
        nargs="+",
        metavar="SERVICE",
        help="Limit to specific service slugs (e.g. ebs ec2 s3). Omit for all services.",
    )

    # Modes
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be expired without modifying any files.",
    )
    parser.add_argument(
        "--list-only",
        action="store_true",
        help=(
            "List all cache records in the date range (with their current expiry status) "
            "without making any changes."
        ),
    )

    # Post-action
    parser.add_argument(
        "--rebuild-dashboard",
        action="store_true",
        help="Regenerate dashboard_data.json and rebuild the HTML export after expiry.",
    )

    args = parser.parse_args()

    # Normalise end date: bare YYYY-MM-DD → end of that day (23:59:59 UTC)
    end = args.end
    if end.hour == 0 and end.minute == 0 and end.second == 0:
        end = end.replace(hour=23, minute=59, second=59)

    start = args.start

    if start > end:
        parser.error(f"--start ({start.date()}) must be on or before --end ({end.date()})")

    print(f"\n🔍 {'[DRY RUN] ' if args.dry_run else ''}Cache range expiry")
    print(f"   Period  : {start.date()} → {end.date()}")
    if args.accounts: print(f"   Accounts: {', '.join(args.accounts)}")
    if args.regions:  print(f"   Regions : {', '.join(args.regions)}")
    if args.services: print(f"   Services: {', '.join(args.services)}")
    print()

    # Find all matching cache files
    cache_files = find_cache_files(args.accounts, args.regions, args.services)
    if not cache_files:
        print("  ℹ️  No cache files found matching the given filters.")
        return 0

    # ── list-only mode: inspect without modifying ────────────────────────────
    if args.list_only:
        print(f"{'Account':<14} {'Region':<18} {'Service':<20} {'Collected At':<26} {'Status'}")
        print(f"{'─'*14} {'─'*18} {'─'*20} {'─'*26} {'─'*15}")
        total = 0
        for acc, region, service, path in cache_files:
            in_range = records_in_range(path, start, end)
            for r in in_range:
                status = "already expired" if r["already_expired"] else "ACTIVE"
                print(f"{acc:<14} {region:<18} {service:<20} {r['collected_at'][:25]:<26} {status}")
                total += 1
        print(f"\n  Total records in range: {total}")
        return 0

    # ── expire mode ──────────────────────────────────────────────────────────
    total_files_checked  = 0
    total_records_expired = 0
    affected_files: list[str] = []

    for acc, region, service, path in cache_files:
        in_range = records_in_range(path, start, end)
        if not in_range:
            continue
        total_files_checked += 1

        active = [r for r in in_range if not r["already_expired"]]
        already_expired_count = len(in_range) - len(active)

        if not active:
            print(f"  ⏭  {acc}/{region}/{service}  — {len(in_range)} record(s) in range, all already expired")
            continue

        label = f"  {'[dry-run] ' if args.dry_run else ''}⏱  {acc}/{region}/{service}"
        print(f"{label}  — {len(active)} record(s) will be expired"
              + (f" ({already_expired_count} already expired)" if already_expired_count else ""))

        if not args.dry_run:
            n = expire_cache_range(str(path), start, end)
            total_records_expired += n
            affected_files.append(str(path))
        else:
            total_records_expired += len(active)

    # ── Summary ──────────────────────────────────────────────────────────────
    print(f"\n{'─' * 60}")
    if args.dry_run:
        print(f"  [DRY RUN] Would expire {total_records_expired} record(s) "
              f"across {total_files_checked} cache file(s).")
        print(f"  Re-run without --dry-run to apply.")
    else:
        print(f"  ✅ Expired {total_records_expired} record(s) "
              f"across {total_files_checked} cache file(s).")
        if total_records_expired > 0:
            print(f"\n  Next analysis run will fetch fresh data for affected services.")
            print(f"  To refresh now, run:")
            print(f"    python3 refresh_all.py --force-refresh")
            print(f"    # or for a specific profile:")
            print(f"    python3 run_analysis.py --profile <profile> --force-refresh")

        if args.rebuild_dashboard and total_records_expired > 0:
            rebuild_dashboard()
        elif args.rebuild_dashboard:
            print("\n  ℹ️  No records were expired — dashboard not rebuilt.")

    print(f"{'─' * 60}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
