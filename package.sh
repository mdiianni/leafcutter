#!/usr/bin/env bash
# package.sh — Package the AWS Cost Analyser for distribution
# ─────────────────────────────────────────────────────────────
# Creates a versioned ZIP archive containing all source files needed to run
# the tool from scratch on a new machine.
#
# What is INCLUDED:
#   ✓  All Python scripts (analyse_*.py, run_analysis.py, etc.)
#   ✓  Shell scripts: package.sh, test-container.sh
#   ✓  Dockerfile.test  (linux/amd64 test container)
#   ✓  requirements.txt
#   ✓  REPORT_VERSION
#   ✓  instructions.md, skills/*.md, releases/*.md  (documentation)
#   ✓  .gitignore
#   ✓  dashboard/src/          (React source)
#   ✓  dashboard/*.json        (package.json, tsconfig*, eslint.config.js)
#   ✓  dashboard/index.html
#   ✓  dashboard/vite.config.ts
#
# What is EXCLUDED:
#   ✗  *.md files              (internal docs / sensitive observations)
#   ✗  accounts/               (real account IDs — gitignored)
#   ✗  accounts.json           (real account IDs — gitignored)
#   ✗  dashboard_data.json     (contains real cost figures)
#   ✗  cache/                  (raw AWS API responses)
#   ✗  output/                 (generated reports)
#   ✗  dashboard/node_modules/ (rebuilt via npm install)
#   ✗  dashboard/dist/         (rebuilt via npm run build)
#   ✗  dashboard/public/       (dashboard_data.json lives here at runtime)
#   ✗  __pycache__/
#   ✗  *.pyc
#   ✗  .DS_Store
#
# Usage:
#   bash package.sh                   # uses version from REPORT_VERSION
#   bash package.sh --version 2.0.0   # override version string
#   bash package.sh --output /tmp     # override output directory
#   bash package.sh --dry-run         # list files without creating ZIP
#
# Output:
#   aws-cost-analyser-<version>.zip

set -euo pipefail

# ── Defaults ──────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VERSION_FILE="$SCRIPT_DIR/REPORT_VERSION"
OUTPUT_DIR="$SCRIPT_DIR"
DRY_RUN=false
OVERRIDE_VERSION=""

# ── Argument parsing ──────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --version|-v)   OVERRIDE_VERSION="$2"; shift 2 ;;
    --output|-o)    OUTPUT_DIR="$2";        shift 2 ;;
    --dry-run|-n)   DRY_RUN=true;           shift   ;;
    --help|-h)
      sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

# ── Resolve version ───────────────────────────────────────────────────────────
if [[ -n "$OVERRIDE_VERSION" ]]; then
  VERSION="$OVERRIDE_VERSION"
elif [[ -f "$VERSION_FILE" ]]; then
  VERSION="$(cat "$VERSION_FILE" | tr -d '[:space:]')"
else
  VERSION="1.0.0"
fi

ARCHIVE_NAME="aws-cost-analyser-${VERSION}.zip"
ARCHIVE_PATH="${OUTPUT_DIR}/${ARCHIVE_NAME}"

# ── Colour helpers ────────────────────────────────────────────────────────────
if [[ -t 1 ]]; then
  GREEN='\033[32m'; YELLOW='\033[33m'; RED='\033[31m'
  CYAN='\033[36m';  GREY='\033[90m';   BOLD='\033[1m'; RESET='\033[0m'
else
  GREEN=''; YELLOW=''; RED=''; CYAN=''; GREY=''; BOLD=''; RESET=''
fi

echo -e "\n${BOLD}AWS Cost Analyser — Packaging${RESET}"
echo -e "  Version : ${CYAN}${VERSION}${RESET}"
echo -e "  Output  : ${ARCHIVE_PATH}"
[[ "$DRY_RUN" == true ]] && echo -e "  ${YELLOW}[DRY RUN — no file will be created]${RESET}"
echo ""

# ── Collect files ─────────────────────────────────────────────────────────────
cd "$SCRIPT_DIR"

# Build the include list
mapfile -t FILES < <(
  # ── Root Python scripts
  find . -maxdepth 1 -name "*.py" ! -name "__pycache__" -type f | sort

  # ── Root shell scripts and Docker test files
  for f in package.sh test-container.sh Dockerfile.test; do
    [[ -f "$f" ]] && echo "./$f"
  done

  # ── Root config / metadata files
  for f in requirements.txt REPORT_VERSION accounts.example.json .gitignore; do
    [[ -f "$f" ]] && echo "./$f"
  done

  # ── Documentation (instructions, skills, release notes)
  find . -maxdepth 1 -name "*.md" -type f | sort
  find ./skills   -type f -name "*.md" 2>/dev/null | sort
  find ./releases -type f -name "*.md" 2>/dev/null | sort

  # ── dashboard source
  find ./dashboard/src -type f | sort

  # ── dashboard config files (but NOT node_modules, dist, public)
  for f in \
      dashboard/index.html \
      dashboard/vite.config.ts \
      dashboard/package.json \
      dashboard/package-lock.json \
      dashboard/tsconfig.json \
      dashboard/tsconfig.app.json \
      dashboard/tsconfig.node.json \
      dashboard/eslint.config.js; do
    [[ -f "$f" ]] && echo "./$f"
  done
)

# Filter out unwanted files (safety net — the includes above are already selective)
EXCLUDED_PATTERNS=(
  "accounts.json"
  "/accounts/"
  "/roadmap/"
  "dashboard_data.json"
  "__pycache__"
  ".pyc"
  ".DS_Store"
  "node_modules"
  "/dist/"
  "/output/"
  "/cache/"
  "dashboard/public/"
)

FINAL_FILES=()
for f in "${FILES[@]}"; do
  skip=false
  for pat in "${EXCLUDED_PATTERNS[@]}"; do
    if [[ "$f" == *"$pat"* ]]; then
      skip=true
      break
    fi
  done
  $skip || FINAL_FILES+=("$f")
done

# ── Print manifest ────────────────────────────────────────────────────────────
echo -e "${BOLD}Files to package (${#FINAL_FILES[@]} total):${RESET}"
for f in "${FINAL_FILES[@]}"; do
  echo -e "  ${GREY}${f}${RESET}"
done
echo ""

if [[ "$DRY_RUN" == true ]]; then
  echo -e "${YELLOW}Dry run complete — no ZIP created.${RESET}\n"
  exit 0
fi

# ── Validate zip is available ─────────────────────────────────────────────────
if ! command -v zip &>/dev/null; then
  echo -e "${RED}✗ 'zip' command not found. Install it with:  brew install zip${RESET}"
  exit 1
fi

# ── Remove stale archive if it exists ────────────────────────────────────────
[[ -f "$ARCHIVE_PATH" ]] && rm "$ARCHIVE_PATH"

# ── Create ZIP ────────────────────────────────────────────────────────────────
zip -q "$ARCHIVE_PATH" "${FINAL_FILES[@]}"

# ── Verify ───────────────────────────────────────────────────────────────────
if [[ -f "$ARCHIVE_PATH" ]]; then
  SIZE_KB=$(du -k "$ARCHIVE_PATH" | cut -f1)
  FILE_COUNT=$(zip -sf "$ARCHIVE_PATH" | grep -c "^\s" || true)
  echo -e "${GREEN}✓ Created:${RESET} ${ARCHIVE_NAME}"
  echo -e "  Size    : ${SIZE_KB} KB"
  echo -e "  Files   : ${#FINAL_FILES[@]}"
  echo ""
  echo -e "${GREY}Share this file. Recipients should run:${RESET}"
  echo -e "  ${CYAN}unzip ${ARCHIVE_NAME}${RESET}"
  echo -e "  ${CYAN}cd aws-cost-analyser-${VERSION}${RESET}"
  echo -e "  ${CYAN}python3 run_analysis.py --profile my-aws-profile${RESET}"
  echo ""
else
  echo -e "${RED}✗ Archive was not created.${RESET}"
  exit 1
fi
