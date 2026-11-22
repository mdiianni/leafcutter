#!/usr/bin/env python3
"""
refresh_all.py — Parallel AWS Cost Analyser Refresh
─────────────────────────────────────────────────────
Runs run_analysis.py concurrently across every AWS profile discovered from
an accounts JSON file (or the AWS CLI config), with:

  • Configurable parallelism  (--workers, default 6)
  • Per-profile retry with exponential back-off (--retries, default 2)
  • Live progress bar showing completed / in-flight / failed profiles
  • Per-profile log files written to /tmp/refresh_<profile>.log
  • Dashboard auto-refresh after all profiles complete (unless --no-dashboard)

Usage:
  python3 refresh_all.py                              # all profiles in accounts/all.json
  python3 refresh_all.py --accounts accounts/all.json
  python3 refresh_all.py --group example.             # group filter from accounts JSON
  python3 refresh_all.py --profiles example-dev example-prd
  python3 refresh_all.py --workers 8 --delay 0        # faster — use with care
  python3 refresh_all.py --services ec2 rds ecs       # only specific services
  python3 refresh_all.py --dry-run                    # print profiles without running
"""

from __future__ import annotations


import argparse
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).parent.resolve()


# ── ANSI helpers ──────────────────────────────────────────────────────────────
def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if sys.stdout.isatty() else text


def green(t):
    return _c("32", t)


def red(t):
    return _c("31", t)


def yellow(t):
    return _c("33", t)


def cyan(t):
    return _c("36", t)


def grey(t):
    return _c("90", t)


def bold(t):
    return _c("1", t)


# ── Profile discovery ─────────────────────────────────────────────────────────
def profiles_from_accounts(accounts_path: Path, group: str | None) -> list[str]:
    """
    Map account IDs → profile names by cross-referencing the accounts JSON
    with AWS CLI profiles.  Falls back to best-guess profile name when no
    direct match is found.
    """
    with open(accounts_path) as f:
        accounts: dict = json.load(f)

    available = set(
        subprocess.check_output(
            ["aws", "configure", "list-profiles"], text=True
        ).split()
    )

    profiles = []
    for acc_id, meta in accounts.items():
        grp = meta.get("group", "")
        if group and grp != group:
            continue
        env = meta.get("env", "")
        name = meta.get("name", "")

        # Derive candidate profile names from group + env
        candidates = [
            f"project-{grp}-{env}",
            f"project-{grp}-management-{env}",
            f"project-{name}-{env}",
            f"project-{name}",
        ]
        for c in candidates:
            if c in available:
                if c not in profiles:
                    profiles.append(c)
                break

    return profiles


def profiles_from_cli(group_filter: str | None = None) -> list[str]:
    """All project-* workload profiles from AWS CLI config (infra accounts excluded)."""
    INFRA = {
        "management",
        "root",
        "shared-services",
        "ads-sandbox",
        "app-project",
        "cloudeng",
        "logging",
        "network",
        "security",
    }
    raw = subprocess.check_output(
        ["aws", "configure", "list-profiles"], text=True
    ).split()
    profiles = []
    for p in sorted(raw):
        if not p.startswith("project-"):
            continue
        if any(skip in p for skip in INFRA):
            continue
        if group_filter and group_filter not in p:
            continue
        profiles.append(p)
    return profiles


# ── Per-profile runner with retry + back-off ──────────────────────────────────
@dataclass
class ProfileResult:
    profile: str
    success: bool
    attempts: int
    elapsed: float
    log_path: str
    error: str = ""


def run_profile(
    profile: str,
    *,
    python: str,
    script: Path,
    services: list[str],
    delay: float,
    region: str,
    retries: int,
    base_backoff: float,
    dry_run: bool,
    force_refresh: bool = False,
    cache_ttl_days: int | None = None,
) -> ProfileResult:
    """Run run_analysis.py for one profile, with retries and exponential back-off."""
    log_path = f"/tmp/refresh_{profile}.log"
    cmd = [
        python,
        str(script),
        "--no-bump",
        "--no-dashboard",
        "--delay",
        str(delay),
        "--region",
        region,
        "--profile",
        profile,
    ]
    if services:
        cmd += ["--services"] + services
    if force_refresh:
        cmd += ["--force-refresh"]
    if cache_ttl_days is not None:
        cmd += ["--cache-ttl-days", str(cache_ttl_days)]

    if dry_run:
        print(f"  {cyan('[dry-run]')} {profile}  →  {' '.join(cmd[5:])}")
        return ProfileResult(profile, True, 0, 0.0, log_path)

    attempt = 0
    last_error = ""
    t_start = time.monotonic()

    for attempt in range(1, retries + 2):  # retries+1 total attempts
        with open(log_path, "w") as log_fh:
            try:
                result = subprocess.run(
                    cmd,
                    stdout=log_fh,
                    stderr=subprocess.STDOUT,
                    env={
                        **os.environ,
                        "AWS_PROFILE": profile,
                        "AWS_DEFAULT_REGION": region,
                        "PYTHONUNBUFFERED": "1",
                    },
                    timeout=600,
                )
                if result.returncode == 0:
                    return ProfileResult(
                        profile,
                        True,
                        attempt,
                        time.monotonic() - t_start,
                        log_path,
                    )
                # Non-zero exit: inspect log for throttle / access-denied
                with open(log_path) as f:
                    last_error = f.read()[-500:]

                if "ThrottlingException" in last_error or "Rate exceeded" in last_error:
                    backoff = base_backoff * (2 ** (attempt - 1))
                    print(
                        f"  {yellow('⚠')}  {profile}: throttled — back-off {backoff:.0f}s "
                        f"(attempt {attempt}/{retries + 1})"
                    )
                    time.sleep(backoff)
                    continue

                # Non-throttle failure — no point retrying
                break

            except subprocess.TimeoutExpired:
                last_error = "Timed out after 600s"
                break
            except Exception as exc:
                last_error = str(exc)
                break

    return ProfileResult(
        profile,
        False,
        attempt,
        time.monotonic() - t_start,
        log_path,
        last_error[-300:],
    )


# ── Progress display ──────────────────────────────────────────────────────────
class Progress:
    def __init__(self, total: int):
        self.total = total
        self.done = 0
        self.failed = 0
        self._start = time.monotonic()
        self._tty = sys.stdout.isatty()

    def update(self, result: ProfileResult):
        self.done += 1
        if not result.success:
            self.failed += 1
        self._print(result)

    def _print(self, r: ProfileResult):
        elapsed = time.monotonic() - self._start
        bar_width = 24
        filled = int(bar_width * self.done / self.total)
        bar = "█" * filled + "░" * (bar_width - filled)
        pct = self.done / self.total * 100
        eta_s = ""
        if self.done > 0:
            eta = (elapsed / self.done) * (self.total - self.done)
            eta_s = f"  ETA {eta:.0f}s"

        status = green("✅") if r.success else red("❌")
        attempts = f" ({r.attempts} attempts)" if r.attempts > 1 else ""
        prefix = "\r" if self._tty else ""
        suffix = "" if self._tty else "\n"

        line = (
            f"{prefix}  [{bar}] {self.done}/{self.total} "
            f"({pct:.0f}%){eta_s}  {status} {r.profile}{attempts}{suffix}"
        )
        print(line, end="", flush=True)

    def finish(self):
        if self._tty:
            print()  # newline after final \r


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> int:
    parser = argparse.ArgumentParser(
        prog="python3 refresh_all.py",
        description="Parallel AWS Cost Analyser refresh across all profiles.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--accounts",
        "-a",
        metavar="PATH",
        default=str(HERE / "accounts" / "all.json"),
        help="Path to accounts JSON file (default: accounts/all.json).",
    )
    parser.add_argument(
        "--profiles",
        "-p",
        nargs="+",
        metavar="PROFILE",
        help="Explicit profile list — overrides --accounts / --group.",
    )
    parser.add_argument(
        "--group",
        "-g",
        metavar="GROUP",
        help="Filter to a specific group name from the accounts JSON.",
    )
    parser.add_argument(
        "--workers",
        "-w",
        type=int,
        default=6,
        metavar="N",
        help="Max concurrent profiles (default: 6).",
    )
    parser.add_argument(
        "--delay",
        "-d",
        type=float,
        default=0.0,
        metavar="SECONDS",
        help="Delay between service analysers within each profile (default: 0).",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=2,
        metavar="N",
        help="Retry attempts on throttle/failure per profile (default: 2).",
    )
    parser.add_argument(
        "--backoff",
        type=float,
        default=15.0,
        metavar="SECONDS",
        help="Base back-off seconds on throttle — doubles each retry (default: 15).",
    )
    parser.add_argument(
        "--region",
        "-r",
        default=os.environ.get("AWS_DEFAULT_REGION", "eu-central-1"),
        metavar="REGION",
        help="AWS region (default: eu-central-1).",
    )
    parser.add_argument(
        "--services",
        "-s",
        nargs="+",
        metavar="SERVICE",
        help="Limit to specific services (e.g. ec2 rds ecs). Omit for all.",
    )
    parser.add_argument(
        "--no-dashboard",
        action="store_true",
        help="Skip dashboard data regeneration after analysis.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would run without executing anything.",
    )
    parser.add_argument(
        "--force-refresh",
        action="store_true",
        help="Ignore cached data; fetch fresh data from AWS for all profiles. Passes --force-refresh to run_analysis.py.",
    )
    parser.add_argument(
        "--cache-ttl-days",
        type=int,
        metavar="N",
        help="Override the default 30-day cache TTL for this run. Passes --cache-ttl-days to run_analysis.py.",
    )
    args = parser.parse_args()

    python = str(HERE / ".venv" / "bin" / "python3")
    if not Path(python).exists():
        python = sys.executable
    script = HERE / "run_analysis.py"

    # ── Resolve profiles ──────────────────────────────────────────────────────
    if args.profiles:
        profiles = args.profiles
    elif Path(args.accounts).exists():
        profiles = profiles_from_accounts(Path(args.accounts), args.group)
        if not profiles:
            print(
                f"  {yellow('⚠')}  No matching profiles found in {args.accounts} "
                f"(group={args.group!r}) — falling back to CLI discovery"
            )
            profiles = profiles_from_cli(args.group)
    else:
        print(
            f"  {grey('ℹ')}  {args.accounts} not found — discovering from AWS CLI config"
        )
        profiles = profiles_from_cli(args.group)

    if not profiles:
        print(red("  ✗  No profiles found. Check accounts JSON or AWS CLI config."))
        return 1

    # ── Print run plan ────────────────────────────────────────────────────────
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    print(bold(f"\n  AWS Cost Analyser — Parallel Refresh  [{ts}]"))
    print(f"  {'─' * 54}")
    print(f"  Profiles  : {bold(str(len(profiles)))}")
    print(f"  Workers   : {args.workers}")
    print(f"  Retries   : {args.retries}  (back-off base: {args.backoff}s × 2ⁿ)")
    print(f"  Delay     : {args.delay}s between services")
    print(f"  Region    : {args.region}")
    if args.services:
        print(f"  Services  : {', '.join(args.services)}")
    print(f"  Log dir   : /tmp/refresh_<profile>.log")
    print(f"  {'─' * 54}\n")

    if args.dry_run:
        print(cyan("  [dry-run] profiles that would be analysed:"))
        for p in profiles:
            print(f"    • {p}")
        print()
        return 0

    # ── Parallel execution ────────────────────────────────────────────────────
    progress = Progress(len(profiles))
    results = []
    t_wall = time.monotonic()

    run_kwargs = dict(
        python=python,
        script=script,
        services=args.services or [],
        delay=args.delay,
        region=args.region,
        retries=args.retries,
        base_backoff=args.backoff,
        dry_run=False,
        force_refresh=args.force_refresh,
        cache_ttl_days=args.cache_ttl_days,
    )

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(run_profile, p, **run_kwargs): p for p in profiles}
        for fut in as_completed(futures):
            result = fut.result()
            results.append(result)
            progress.update(result)

    progress.finish()

    # ── Summary ───────────────────────────────────────────────────────────────
    wall = time.monotonic() - t_wall
    ok = [r for r in results if r.success]
    failed = [r for r in results if not r.success]
    retried = [r for r in results if r.attempts > 1]

    print(f"\n  {'─' * 54}")
    print(bold("  Summary"))
    print(f"  {'─' * 54}")
    print(f"  {green('✅')} Succeeded : {len(ok)}/{len(results)}")
    if retried:
        print(
            f"  {yellow('↻')}  Retried   : {len(retried)}  "
            f"({', '.join(r.profile for r in retried)})"
        )
    if failed:
        print(f"  {red('❌')} Failed    : {len(failed)}")
        for r in failed:
            print(f"       • {r.profile}  — {r.error[:120]}")
            print(f"         log: {r.log_path}")
    print(
        f"  Wall time : {wall:.0f}s  "
        f"({wall / len(profiles):.1f}s avg/profile with {args.workers} workers)"
    )
    print(f"  {'─' * 54}")

    # ── Dashboard refresh ─────────────────────────────────────────────────────
    if not args.no_dashboard and ok:
        import shutil

        print(f"\n  {cyan('→')}  Regenerating dashboard data...")
        dash_gen = HERE / "generate_dashboard_data.py"
        r = subprocess.run([python, str(dash_gen)], capture_output=True, text=True)
        if r.returncode == 0:
            print(green("  ✅  Dashboard data refreshed"))
            # Rebuild HTML and copy to output/
            npm = "npm"
            dash_dir = HERE / "dashboard"
            if (dash_dir / "package.json").exists():
                print(f"  {cyan('→')}  Rebuilding dashboard HTML...")
                env = {**os.environ, "BUILD_MODE": "singlefile"}
                r2 = subprocess.run(
                    [npm, "run", "build"], cwd=dash_dir, capture_output=True, text=True,
                    env=env,
                )
                if r2.returncode == 0:
                    src = dash_dir / "dist" / "index.html"
                    dst = HERE / "output" / "AWS_Cost_Dashboard.html"
                    dst.parent.mkdir(exist_ok=True)
                    shutil.copy(src, dst)
                    size_kb = dst.stat().st_size // 1024
                    print(green(f"  ✅  output/AWS_Cost_Dashboard.html updated  ({size_kb} KB)"))
                else:
                    print(red(f"  ❌  npm build failed: {r2.stderr[-200:]}"))
        else:
            print(red(f"  ❌  Dashboard data generation failed: {r.stderr[-200:]}"))

    print()
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
