#!/usr/bin/env python3
"""
run_analysis.py — AWS Cost Analyser Orchestrator
─────────────────────────────────────────────────
Single entry point to run cost analysis against one or more AWS accounts.

Usage examples
──────────────
  # Run all checks for a profile (auto-bumps patch version + refreshes dashboard)
  python3 run_analysis.py --profile my-aws-profile

  # Run only EC2 and RDS checks
  python3 run_analysis.py --profile my-aws-profile --services ec2 rds

  # Run all checks, bump minor version, rebuild dashboard HTML
  python3 run_analysis.py --profile my-aws-profile --bump minor --html

  # Run for multiple profiles in sequence
  python3 run_analysis.py --profile dev-profile stg-profile prd-profile

  # Run all checks but skip dashboard refresh
  python3 run_analysis.py --profile my-aws-profile --no-dashboard

  # List all available services
  python3 run_analysis.py --list-services

Environment variables
─────────────────────
  AWS_DEFAULT_REGION   Region to analyse (default: eu-central-1)
  AWS_PROFILE          Overrides --profile when running a single account

Rate limiting
─────────────
  A configurable delay (default 2 s) is inserted between each service analyser
  to stay within AWS API rate limits.  Use --delay 0 to disable.
"""

from __future__ import annotations


# ── Dependency bootstrap (must run before any third-party imports) ────────────
import subprocess
import sys
from pathlib import Path


def _ensure_dependencies() -> None:
    """
    Check that all packages listed in requirements.txt are importable.
    If any are missing, offer to install them automatically.
    Called at the very start of the script before other imports.
    """
    req_file = Path(__file__).parent / "requirements.txt"
    if not req_file.exists():
        return

    # Parse package names from requirements.txt (ignore comments / blank lines)
    import re

    required: list[str] = []
    for line in req_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Strip version specifiers to get the importable name
        pkg = re.split(r"[><=!;]", line)[0].strip()
        required.append(pkg)

    # Map pip package names → Python import names where they differ
    import_names = {
        "python-pptx": "pptx",
    }

    missing: list[str] = []
    for pkg in required:
        import_name = import_names.get(pkg, pkg.replace("-", "_"))
        try:
            __import__(import_name)
        except ImportError:
            missing.append(pkg)

    if not missing:
        return

    USE_COLOUR = sys.stdout.isatty()

    def yellow(t):
        return f"\033[33m{t}\033[0m" if USE_COLOUR else t

    def green(t):
        return f"\033[32m{t}\033[0m" if USE_COLOUR else t

    def red(t):
        return f"\033[31m{t}\033[0m" if USE_COLOUR else t

    print(f"\n{yellow('⚠  Missing Python dependencies:')}  {', '.join(missing)}")
    print(f"   Required by: {req_file.name}\n")

    # Auto-install without prompting when stdout is not a TTY (CI / scripts)
    if not sys.stdout.isatty():
        auto = True
    else:
        try:
            answer = input("   Install now? [Y/n] ").strip().lower()
            auto = answer in ("", "y", "yes")
        except (EOFError, KeyboardInterrupt):
            auto = False

    if not auto:
        print(red("\n  Aborting — run:  pip install -r requirements.txt\n"))
        sys.exit(1)

    print(f"\n  Installing via pip…\n")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "-r",
            str(req_file),
            "--quiet",
            "--disable-pip-version-check",
        ],
        check=False,
    )
    if result.returncode != 0:
        # Retry with --user if the global install fails (e.g. externally-managed env)
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "-r",
                str(req_file),
                "--user",
                "--quiet",
                "--disable-pip-version-check",
            ],
            check=False,
        )

    if result.returncode == 0:
        print(f"\n  {green('✓ Dependencies installed.')}  Continuing…\n")
    else:
        print(red("\n  ✗ pip install failed."))
        print(f"  Please run manually:  pip install -r {req_file}\n")
        sys.exit(1)


_ensure_dependencies()
# ─────────────────────────────────────────────────────────────────────────────

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from cache_utils import expire_cache

# ── Constants ─────────────────────────────────────────────────────────────────

BASE = Path(__file__).parent.resolve()
VERSION_FILE = BASE / "REPORT_VERSION"
DASHBOARD_DIR = BASE / "dashboard"
OUTPUT_DIR = BASE / "output"

# Ordered list of all available service analysers (slug → script filename)
ALL_SERVICES: list[tuple[str, str]] = [
    ("billing", "analyse_billing.py"),
    ("ec2", "analyse_ec2.py"),
    ("s3", "analyse_s3.py"),
    ("rds", "analyse_rds.py"),
    ("ecs", "analyse_ecs.py"),
    ("eks", "analyse_eks.py"),
    ("elb", "analyse_elb.py"),
    ("lambda", "analyse_lambda.py"),
    ("cloudwatch", "analyse_cloudwatch.py"),
    ("vpc", "analyse_vpc.py"),
    ("ebs", "analyse_ebs.py"),
    ("ecr", "analyse_ecr.py"),
    ("efs", "analyse_efs.py"),
    ("elasticache", "analyse_elasticache.py"),
    ("msk", "analyse_msk.py"),
    ("kinesis", "analyse_kinesis.py"),
    ("kms", "analyse_kms.py"),
    ("waf", "analyse_waf.py"),
    ("opensearch", "analyse_opensearch.py"),
    ("apigw", "analyse_apigw.py"),
    ("sqs", "analyse_sqs.py"),
    ("sns", "analyse_sns.py"),
    ("secrets_manager", "analyse_secrets_manager.py"),
    ("ssm", "analyse_ssm.py"),
    ("guardduty", "analyse_guardduty.py"),
    ("xray", "analyse_xray.py"),
    ("datasync", "analyse_datasync.py"),
    ("ses", "analyse_ses.py"),
    ("pinpoint", "analyse_pinpoint.py"),
    ("emr", "analyse_emr.py"),
    ("transfer", "analyse_transfer.py"),
    ("redshift", "analyse_redshift.py"),
    ("dax", "analyse_dax.py"),
    ("amp", "analyse_amp.py"),
    ("quicksight", "analyse_quicksight.py"),
    ("config", "analyse_config.py"),
    ("sagemaker", "analyse_sagemaker.py"),
    ("glue", "analyse_glue.py"),
    ("bedrock",           "analyse_bedrock.py"),
    ("global",           "analyse_global.py"),
]

SERVICE_MAP = {slug: script for slug, script in ALL_SERVICES}

# ── Version helpers ───────────────────────────────────────────────────────────


def read_version() -> tuple[int, int, int]:
    raw = VERSION_FILE.read_text().strip() if VERSION_FILE.exists() else "1.0.0"
    parts = raw.split(".")
    try:
        return int(parts[0]), int(parts[1]), int(parts[2])
    except (ValueError, IndexError):
        return 1, 0, 0


def bump_version(component: str) -> str:
    major, minor, patch = read_version()
    if component == "major":
        major, minor, patch = major + 1, 0, 0
    elif component == "minor":
        major, minor, patch = major, minor + 1, 0
    else:  # patch (default)
        patch += 1
    new_ver = f"{major}.{minor}.{patch}"
    VERSION_FILE.write_text(new_ver + "\n")
    return new_ver


def current_version() -> str:
    return VERSION_FILE.read_text().strip() if VERSION_FILE.exists() else "0.0.0"


# ── Colour helpers ────────────────────────────────────────────────────────────

USE_COLOUR = sys.stdout.isatty()


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if USE_COLOUR else text


def green(t):
    return _c("32", t)


def yellow(t):
    return _c("33", t)


def red(t):
    return _c("31", t)


def cyan(t):
    return _c("36", t)


def bold(t):
    return _c("1", t)


def grey(t):
    return _c("90", t)


# ── Subprocess runner ─────────────────────────────────────────────────────────


def run_script(script: str, env: dict) -> tuple[bool, float]:
    """Run a single analyser script.  Returns (success, elapsed_seconds)."""
    path = BASE / script
    if not path.exists():
        print(f"  {yellow('SKIP')}  {script} — file not found")
        return False, 0.0

    t0 = time.monotonic()
    result = subprocess.run(
        [sys.executable, str(path)],
        env=env,
        capture_output=True,
        text=True,
    )
    elapsed = time.monotonic() - t0

    if result.returncode == 0:
        print(f"  {green('OK')}    {script:<38s}  {elapsed:>5.1f}s")
    else:
        print(f"  {red('FAIL')}  {script:<38s}  {elapsed:>5.1f}s")
        # Print last few lines of stderr for context
        err_lines = (result.stderr or result.stdout or "").strip().splitlines()
        for line in err_lines[-5:]:
            print(f"         {grey(line)}")

    return result.returncode == 0, elapsed


# ── Dashboard helpers ─────────────────────────────────────────────────────────


def refresh_dashboard_data() -> bool:
    """Regenerate dashboard_data.json."""
    print(f"\n{bold('Refreshing dashboard data…')}")
    result = subprocess.run(
        [sys.executable, str(BASE / "generate_dashboard_data.py")],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        # Copy to dashboard/public for dev server
        src = BASE / "dashboard_data.json"
        dst = DASHBOARD_DIR / "public" / "dashboard_data.json"
        if dst.parent.exists():
            shutil.copy(src, dst)
        print(green("  ✓ dashboard_data.json updated"))
        # Print brief summary from output
        for line in result.stdout.splitlines()[:6]:
            print(f"  {grey(line)}")
        return True
    else:
        print(red("  ✗ generate_dashboard_data.py failed"))
        print(grey(result.stderr[-500:] if result.stderr else result.stdout[-500:]))
        return False


def rebuild_html() -> bool:
    """Build single-file HTML export via Vite."""
    print(f"\n{bold('Building static HTML dashboard…')}")
    npm = shutil.which("npm")
    if not npm:
        print(yellow("  ⚠  npm not found — skipping HTML build"))
        return False

    env = {**os.environ, "BUILD_MODE": "singlefile"}
    result = subprocess.run(
        [npm, "run", "build", "--", "--logLevel", "warn"],
        cwd=DASHBOARD_DIR,
        env=env,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(red("  ✗ Vite build failed"))
        print(grey(result.stderr[-500:]))
        return False

    # Copy output
    OUTPUT_DIR.mkdir(exist_ok=True)
    src = DASHBOARD_DIR / "dist" / "index.html"
    dst = OUTPUT_DIR / "AWS_Cost_Dashboard.html"
    shutil.copy(src, dst)
    size_kb = dst.stat().st_size // 1024
    print(green(f"  ✓ output/AWS_Cost_Dashboard.html  ({size_kb} KB)"))
    return True


# ── Core orchestration ────────────────────────────────────────────────────────


def analyse_profile(
    profile: str,
    services: list[tuple[str, str]],
    delay: float,
    region: str,
    force_refresh: bool = False,
    cache_ttl_days: int | None = None,
) -> tuple[int, int, float]:
    """Run analysers for a single profile. Returns (ok, fail, total_elapsed)."""

    # Build environment for subprocesses
    env = {
        **os.environ,
        "AWS_PROFILE": profile,
        "AWS_DEFAULT_REGION": region,
    }
    if force_refresh:
        env["AWS_COST_ANALYSER_FORCE_REFRESH"] = "1"
    if cache_ttl_days is not None:
        env["AWS_COST_ANALYSER_CACHE_TTL_DAYS"] = str(cache_ttl_days)

    print(f"\n{'─' * 60}")
    print(bold(f"  Profile : {cyan(profile)}"))
    print(f"  Region  : {region}")
    print(f"  Checks  : {len(services)}/{len(ALL_SERVICES)}")
    print(f"{'─' * 60}")

    ok_count = fail_count = 0
    total_elapsed = 0.0

    for i, (slug, script) in enumerate(services):
        success, elapsed = run_script(script, env)
        total_elapsed += elapsed
        if success:
            ok_count += 1
        else:
            fail_count += 1

        # Rate-limit delay between calls (skip after last)
        if delay > 0 and i < len(services) - 1:
            time.sleep(delay)

    print(
        f"\n  {bold('Summary')}  {green(str(ok_count))} ok  "
        f"{(red(str(fail_count)) + ' failed') if fail_count else grey('0 failed')}  "
        f"elapsed {total_elapsed:.0f}s"
    )
    return ok_count, fail_count, total_elapsed


# ── CLI ───────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    svc_slugs = [s for s, _ in ALL_SERVICES]

    p = argparse.ArgumentParser(
        prog="python run_analysis.py",
        description="AWS Cost Analyser — orchestrate service checks for one or more accounts.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    p.add_argument(
        "--profile",
        "-p",
        nargs="+",
        metavar="PROFILE",
        help="One or more AWS CLI profile names to analyse (space-separated).",
    )
    p.add_argument(
        "--services",
        "-s",
        nargs="+",
        metavar="SERVICE",
        choices=svc_slugs,
        help=(
            "Limit analysis to specific services. "
            f"Available: {', '.join(svc_slugs)}. "
            "Omit to run all."
        ),
    )
    p.add_argument(
        "--region",
        "-r",
        default=os.environ.get("AWS_DEFAULT_REGION", "eu-central-1"),
        metavar="REGION",
        help="AWS region (default: eu-central-1, or $AWS_DEFAULT_REGION).",
    )
    p.add_argument(
        "--delay",
        "-d",
        type=float,
        default=2.0,
        metavar="SECONDS",
        help="Delay in seconds between service analysers to respect API rate limits (default: 2).",
    )

    # Version bump
    bump_group = p.add_mutually_exclusive_group()
    bump_group.add_argument(
        "--bump",
        choices=["patch", "minor", "major"],
        nargs="?",
        const="patch",
        metavar="COMPONENT",
        help=(
            "Bump REPORT_VERSION after analysis. "
            "Choices: patch (default when flag is present), minor, major."
        ),
    )
    bump_group.add_argument(
        "--no-bump",
        action="store_true",
        help="Explicitly skip version bump (default behaviour).",
    )

    # Dashboard
    p.add_argument(
        "--no-dashboard",
        action="store_true",
        help="Skip regenerating dashboard_data.json after analysis.",
    )
    p.add_argument(
        "--html",
        action="store_true",
        help="Also rebuild the self-contained static HTML export (runs Vite build).",
    )

    p.add_argument(
        "--list-services",
        action="store_true",
        help="Print all available service slugs and exit.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be executed without running any analysers.",
    )

    # ── Cache controls ────────────────────────────────────────────────────────
    p.add_argument(
        "--force-refresh",
        action="store_true",
        help=(
            "Ignore cached data for all analysers; fetch live data from AWS. "
            "Sets AWS_COST_ANALYSER_FORCE_REFRESH=1 for all subprocesses."
        ),
    )
    p.add_argument(
        "--cache-ttl-days",
        type=int,
        metavar="N",
        help=(
            "Override the default 30-day cache TTL. "
            "New cache records will expire after N days. "
            "Sets AWS_COST_ANALYSER_CACHE_TTL_DAYS=N for all subprocesses."
        ),
    )
    p.add_argument(
        "--clear-cache",
        nargs="*",
        metavar="SERVICE",
        help=(
            "Expire the latest cache record for SERVICE(s) so the next run fetches fresh data. "
            "If no services are listed, expires cache for ALL services. "
            "Historical records are preserved. "
            "Example: --clear-cache ec2 rds"
        ),
    )
    return p


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    print(f"{green('🌿')} {bold('leafcutter')} {grey('— Cloud cost analysis')}\n")

    # ── --list-services ──────────────────────────────────────────────────────
    if args.list_services:
        print(bold("\nAvailable AWS service analysers:\n"))
        for slug, script in ALL_SERVICES:
            exists = "✓" if (BASE / script).exists() else "✗"
            print(f"  {green(exists)}  {slug:<22s}  {grey(script)}")
        print()
        return 0

    # ── --clear-cache ─────────────────────────────────────────────────────────
    if args.clear_cache is not None:
        slugs_to_clear = args.clear_cache if args.clear_cache else [s for s, _ in ALL_SERVICES]
        region = args.region
        # Resolve profile(s) for account IDs — use first profile if available
        profiles = args.profile or []
        account_ids: list[str] = []
        for prof in profiles:
            try:
                import boto3
                acct = boto3.Session(profile_name=prof).client("sts").get_caller_identity()["Account"]
                account_ids.append(acct)
            except Exception:
                pass
        if not account_ids:
            print(yellow("  ⚠️  Could not resolve account IDs — searching all cache dirs"))
            cache_root = BASE / "cache"
            account_ids = [d.name for d in cache_root.iterdir() if d.is_dir()] if cache_root.exists() else []

        total_expired = 0
        for acct in account_ids:
            for slug in slugs_to_clear:
                path = BASE / "cache" / acct / region / f"{slug}.json"
                if expire_cache(str(path)):
                    print(f"  ⏱  Expired cache: {acct}/{region}/{slug}.json")
                    total_expired += 1
                else:
                    print(grey(f"  –  No cache found: {acct}/{region}/{slug}.json"))
        print(f"\n  {bold(str(total_expired))} cache record(s) expired. "
              f"Next run will fetch fresh data for these services.")
        return 0

    # ── Require --profile unless --list-services / --clear-cache ─────────────
    if not args.profile:
        parser.error(
            "--profile is required (use --list-services to see available services)"
        )

    # ── Resolve services to run ───────────────────────────────────────────────
    if args.services:
        services = [(s, SERVICE_MAP[s]) for s in args.services if s in SERVICE_MAP]
    else:
        services = ALL_SERVICES[:]

    # ── Dry-run ───────────────────────────────────────────────────────────────
    if args.dry_run:
        print(bold("\n[DRY RUN] Would execute:\n"))
        for profile in args.profile:
            print(f"  Profile: {cyan(profile)}  Region: {args.region}")
            for slug, script in services:
                print(f"    → {script}")
        print(f"\n  Delay between services: {args.delay}s")
        if args.bump:
            print(f"  Version bump: {args.bump}")
        if not args.no_dashboard:
            print("  Dashboard refresh: yes")
        if args.html:
            print("  HTML rebuild: yes")
        return 0

    # ── Header ────────────────────────────────────────────────────────────────
    started_at = datetime.now(timezone.utc)
    print(f"\n{bold('AWS Cost Analyser')}")
    print(f"Version : {current_version()}")
    print(f"Started : {started_at.strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"Region  : {args.region}")
    print(f"Profiles: {len(args.profile)}")
    print(f"Services: {len(services)}/{len(ALL_SERVICES)}")

    # ── Run analysers per profile ─────────────────────────────────────────────
    grand_ok = grand_fail = 0
    for profile in args.profile:
        ok, fail, _ = analyse_profile(
            profile, services, args.delay, args.region,
            force_refresh=args.force_refresh,
            cache_ttl_days=args.cache_ttl_days,
        )
        grand_ok += ok
        grand_fail += fail

    # ── Version bump ──────────────────────────────────────────────────────────
    # Default: auto-bump patch whenever dashboard is refreshed (unless --no-bump
    # or an explicit --bump component was passed).
    bump_component = args.bump  # None | "patch" | "minor" | "major"
    if not args.no_dashboard and not args.no_bump and bump_component is None:
        bump_component = "patch"

    if bump_component:
        new_ver = bump_version(bump_component)
        print(f"\n{bold('Version bumped')} ({bump_component}) → {green(new_ver)}")

    # ── Dashboard refresh ─────────────────────────────────────────────────────
    if not args.no_dashboard:
        refresh_dashboard_data()

    # ── HTML rebuild — always triggered by a version bump; also on --html ─────
    if bump_component or args.html:
        rebuild_html()

    # ── Final summary ─────────────────────────────────────────────────────────
    elapsed_total = (datetime.now(timezone.utc) - started_at).total_seconds()
    print(f"\n{'═' * 60}")
    print(bold("All done"))
    print(f"  Version  : {current_version()}")
    print(f"  Profiles : {len(args.profile)}")
    print(
        f"  Checks   : {green(str(grand_ok))} ok"
        f"  {(red(str(grand_fail)) + ' failed') if grand_fail else grey('0 failed')}"
    )
    print(f"  Elapsed  : {elapsed_total / 60:.1f} min")
    print(f"{'═' * 60}\n")

    return 1 if grand_fail > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
