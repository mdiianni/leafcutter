#!/usr/bin/env python3
"""
discover_accounts.py — Resolve AWS profiles to account metadata
────────────────────────────────────────────────────────────────
Loops over AWS CLI profiles, calls STS + IAM to resolve each profile to
an account ID and alias, then infers name / env / group from the profile
name convention.  Results are cached in:

    cache/<account_id>/account_meta.json

and optionally written to accounts.json (the dashboard registry).

Usage examples
──────────────
  # Discover all profiles in ~/.aws/config
  python3 discover_accounts.py --all

  # Discover specific profiles only
  python3 discover_accounts.py --profiles my-app-dev my-app-stg my-app-prd

  # Discover, strip an org prefix, and write accounts.json
  python3 discover_accounts.py --all --strip-prefix myorg- --write-accounts

  # Override env/group for a specific profile
  python3 discover_accounts.py --profiles my-profile --env prd --group my-team

  # Preview without writing anything
  python3 discover_accounts.py --all --dry-run

Profile name parsing
────────────────────
Given a profile like: myorg-my-app-dev

  1. Strip --strip-prefix (e.g. "myorg-") → my-app-dev
  2. Detect env suffix: dev / stg / prd / prod / production / staging /
                        development / qa / test / sandbox / playground
  3. name  = everything between prefix and env  → my-app
  4. group = same as name (customise in accounts.json afterward)

Schema written to cache/<id>/account_meta.json
──────────────────────────────────────────────
{
  "account_id": "123456789012",
  "name":       "my-app",
  "env":        "dev",
  "group":      "my-app",
  "alias":      "myorg-my-app-dev",
  "profile":    "my-aws-profile",
  "discovered_at": "2026-04-09T..."
}
"""

from __future__ import annotations


import argparse
import configparser
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import boto3
from botocore.exceptions import ClientError, ProfileNotFound, NoCredentialsError

BASE       = Path(__file__).parent.resolve()
CACHE_DIR  = BASE / "cache"
META_FILE  = "account_meta.json"

# ── Env suffix map ────────────────────────────────────────────────────────────
ENV_ALIASES = {
    "dev":         "dev",
    "development": "dev",
    "stg":         "stg",
    "staging":     "stg",
    "stage":       "stg",
    "prd":         "prd",
    "prod":        "prd",
    "production":  "prd",
    "qa":          "qa",
    "test":        "test",
    "sandbox":     "sandbox",
    "playground":  "sandbox",
}

# ── Colour helpers ────────────────────────────────────────────────────────────
USE_COLOUR = sys.stdout.isatty()

def _c(code, t): return f"\033[{code}m{t}\033[0m" if USE_COLOUR else t
def green(t):  return _c("32", t)
def yellow(t): return _c("33", t)
def red(t):    return _c("31", t)
def grey(t):   return _c("90", t)
def bold(t):   return _c("1",  t)
def cyan(t):   return _c("36", t)


# ── Profile helpers ───────────────────────────────────────────────────────────

def all_aws_profiles() -> list[str]:
    """Return all profile names from ~/.aws/config."""
    cfg_path = Path.home() / ".aws" / "config"
    if not cfg_path.exists():
        return []
    cfg = configparser.ConfigParser()
    cfg.read(cfg_path)
    profiles = []
    for section in cfg.sections():
        if section.startswith("profile "):
            profiles.append(section[len("profile "):])
        elif section == "default":
            profiles.append("default")
    return sorted(profiles)


def parse_profile_name(profile: str, strip_prefix: str = "") -> dict:
    """
    Infer name, env, and group from a profile name.

    Examples (strip_prefix="myorg-"):
      myorg-my-app-dev   → name=my-app,      env=dev,  group=my-app
      myorg-core-logging-production → name=core-logging, env=prd, group=core-logging
      myorg-platform-prd → name=platform,    env=prd,  group=platform
      plain-profile      → name=plain-profile, env=unknown, group=plain-profile
    """
    raw = profile
    if strip_prefix and raw.startswith(strip_prefix):
        raw = raw[len(strip_prefix):]

    parts = raw.split("-")

    # Walk from the end to find an env token
    env = "unknown"
    name_parts = parts[:]
    for i in range(len(parts) - 1, -1, -1):
        token = parts[i].lower()
        if token in ENV_ALIASES:
            env = ENV_ALIASES[token]
            name_parts = parts[:i]
            break

    name = "-".join(name_parts) if name_parts else raw
    if not name:
        name = raw

    return {
        "name":  name,
        "env":   env,
        "group": name,   # group defaults to name; user can override in accounts.json
    }


# ── AWS resolution ────────────────────────────────────────────────────────────

def resolve_profile(profile: str) -> dict | None:
    """
    Call STS + IAM for a profile.  Returns a metadata dict or None on failure.
    """
    try:
        session = boto3.Session(profile_name=profile)
        sts = session.client("sts", region_name="eu-central-1")
        identity = sts.get_caller_identity()
        account_id = identity["Account"]

        # Try to get the IAM account alias (often the human-readable account name)
        alias = None
        try:
            iam = session.client("iam", region_name="us-east-1")
            aliases = iam.list_account_aliases().get("AccountAliases", [])
            alias = aliases[0] if aliases else None
        except ClientError:
            pass

        # Try AWS Organizations DescribeAccount for the display name
        org_name = None
        try:
            org = session.client("organizations", region_name="us-east-1")
            org_name = org.describe_account(AccountId=account_id)["Account"]["Name"]
        except (ClientError, Exception):
            pass

        return {
            "account_id": account_id,
            "profile":    profile,
            "alias":      alias,
            "org_name":   org_name,
        }

    except ProfileNotFound:
        return {"error": f"Profile '{profile}' not found in ~/.aws/config"}
    except NoCredentialsError:
        return {"error": f"No credentials for profile '{profile}'"}
    except ClientError as e:
        code = e.response["Error"]["Code"]
        return {"error": f"{code}: {e.response['Error']['Message']}"}
    except Exception as e:
        return {"error": str(e)}


# ── Cache helpers ─────────────────────────────────────────────────────────────

def read_meta(account_id: str) -> dict | None:
    path = CACHE_DIR / account_id / META_FILE
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            return None
    return None


def write_meta(meta: dict) -> None:
    aid = meta["account_id"]
    out_dir = CACHE_DIR / aid
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / META_FILE
    path.write_text(json.dumps(meta, indent=2))


# ── Main ──────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python3 discover_accounts.py",
        description="Resolve AWS profiles → account metadata and cache locally.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--profiles", "-p", nargs="+", metavar="PROFILE",
                   help="Specific profile names to resolve.")
    p.add_argument("--all", "-a", action="store_true",
                   help="Resolve every profile found in ~/.aws/config.")
    p.add_argument("--strip-prefix", default="",
                   metavar="PREFIX",
                   help="Strip an org prefix from profile names before parsing "
                        "(e.g. 'myorg-'). Does not affect actual AWS calls.")
    p.add_argument("--env", help="Override env for all supplied --profiles.")
    p.add_argument("--group", help="Override group for all supplied --profiles.")
    p.add_argument("--name", help="Override name for all supplied --profiles.")
    p.add_argument("--write-accounts", action="store_true",
                   help="Also write/merge results into accounts.json.")
    p.add_argument("--dry-run", action="store_true",
                   help="Parse profile names and print plan; do not call AWS.")
    p.add_argument("--skip-existing", action="store_true",
                   help="Skip profiles whose account_meta.json already exists in cache.")
    p.add_argument("--filter-env", metavar="ENV",
                   help="Only process profiles that resolve to this env (dev/stg/prd…).")
    return p


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if not args.profiles and not args.all:
        parser.error("Specify --profiles <name> [name …] or --all")

    # Gather profile list
    if args.all:
        profiles = all_aws_profiles()
        print(f"Found {len(profiles)} profiles in ~/.aws/config")
    else:
        profiles = args.profiles

    if not profiles:
        print(red("No profiles found."))
        return 1

    # Load existing accounts.json if present
    accounts_json_path = BASE / "accounts.json"
    existing_accounts: dict = {}
    if accounts_json_path.exists():
        try:
            existing_accounts = json.loads(accounts_json_path.read_text())
            # Strip _comment key
            existing_accounts.pop("_comment", None)
        except Exception:
            pass

    resolved: list[dict] = []
    ok = skipped = failed = 0

    print(f"\n{'─'*64}")
    print(bold(f"  {'PROFILE':<38}  {'ACCOUNT ID':>12}  ENV    GROUP"))
    print(f"{'─'*64}")

    for profile in profiles:
        # Parse name from profile regardless of dry-run
        parsed = parse_profile_name(profile, args.strip_prefix)
        if args.name:  parsed["name"]  = args.name
        if args.env:   parsed["env"]   = args.env
        if args.group: parsed["group"] = args.group

        # Apply env filter
        if args.filter_env and parsed["env"] != args.filter_env:
            continue

        if args.dry_run:
            print(f"  {profile:<38}  {'(dry run)':>12}  {parsed['env']:<6} {parsed['group']}")
            continue

        # Resolve via AWS
        aws = resolve_profile(profile)
        if not aws or "error" in aws:
            err = (aws or {}).get("error", "unknown error")
            print(f"  {yellow('SKIP')} {profile:<36}  {grey(err)}")
            failed += 1
            continue

        account_id = aws["account_id"]

        # Skip if already cached and --skip-existing
        if args.skip_existing and read_meta(account_id):
            print(f"  {grey('SKIP')} {profile:<36}  {account_id}  (cached)")
            skipped += 1
            continue

        # Prefer org_name > alias > parsed name for the display name
        display_name = aws.get("org_name") or aws.get("alias") or parsed["name"]

        meta = {
            "account_id":    account_id,
            "name":          display_name,
            "env":           parsed["env"],
            "group":         parsed["group"],
            "alias":         aws.get("alias"),
            "org_name":      aws.get("org_name"),
            "profile":       profile,
            "discovered_at": datetime.now(timezone.utc).isoformat(),
        }

        write_meta(meta)
        resolved.append(meta)
        ok += 1

        name_col = (display_name or "")[:28]
        print(f"  {green('OK')}   {profile:<36}  {account_id}  "
              f"{meta['env']:<6} {cyan(meta['group'])}")

        # Merge into existing accounts dict
        if args.write_accounts:
            existing_accounts[account_id] = {
                "name":    meta["name"],
                "env":     meta["env"],
                "group":   meta["group"],
                "profile": profile,
            }

    print(f"{'─'*64}")

    if not args.dry_run:
        print(f"\n  {green(str(ok))} resolved  "
              f"{grey(str(skipped))} skipped  "
              f"{(red(str(failed))+' failed') if failed else grey('0 failed')}")

        if args.write_accounts and existing_accounts:
            accounts_json_path.write_text(json.dumps(existing_accounts, indent=2))
            print(f"\n{green('✓')} accounts.json updated  ({len(existing_accounts)} accounts)")

        if resolved:
            print(f"\n{green('✓')} Metadata cached to cache/<account_id>/{META_FILE}")
            print(grey("  Run: python3 generate_dashboard_data.py  to refresh the dashboard"))

    return 0


if __name__ == "__main__":
    sys.exit(main())
