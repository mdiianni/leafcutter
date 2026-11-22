#!/usr/bin/env bash
# test-container.sh — End-to-end Docker test for the AWS Cost Analyser
# ─────────────────────────────────────────────────────────────────────
#
# Builds a linux/amd64 Docker image, starts a test container, copies the
# distributable ZIP into it, extracts it, runs the cost analysis for the
# specified AWS profile, generates the self-contained HTML dashboard, and
# opens the report in your local browser via a port-forward.
#
# Prerequisites:
#   • Docker Desktop (or Docker Engine) running
#   • ~/.aws configured with the target profile (credentials / SSO cache live)
#   • The distributable ZIP present in the project root (run package.sh first)
#
# Usage:
#   bash test-container.sh [OPTIONS]
#
# Options:
#   --profile      PROFILE   AWS profile to use inside the container
#                            (default: first profile found, or $AWS_PROFILE)
#   --strip-prefix PREFIX    Strip org prefix when parsing the profile name into
#                            name/env/group for the dashboard
#                            (e.g. --strip-prefix myorg- → "myorg-app-dev"
#                             becomes name="app", env="dev", group="app")
#   --port         PORT      Host port to forward for the dashboard (default: 8080)
#   --zip          PATH      Path to the ZIP archive to test
#                            (default: aws-cost-analyser-<VERSION>.zip)
#   --shell                  Drop into an interactive bash shell instead of
#                            running the full analysis automatically
#   --clean                  Remove the test container and image after exit
#   --no-browser             Skip opening the browser automatically
#   --help                   Show this help and exit
#
# Examples:
#   # Full automated test (runs analysis + opens dashboard in browser)
#   bash test-container.sh --profile cloudeng-1-dev --strip-prefix myorg-
#
#   # Interactive shell — explore the container manually
#   bash test-container.sh --profile cloudeng-1-dev --strip-prefix myorg- --shell
#
#   # Test a specific ZIP on a custom port, remove everything on exit
#   bash test-container.sh --profile my-profile --zip /tmp/aws-cost-analyser-2.0.0.zip \
#       --port 9090 --clean

set -euo pipefail

# ── Defaults ──────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VERSION="$(cat "${SCRIPT_DIR}/REPORT_VERSION" 2>/dev/null | tr -d '[:space:]' || echo "1.0.0")"

PROFILE="${AWS_PROFILE:-}"
STRIP_PREFIX=""
HOST_PORT=8080
ZIP_PATH=""
SHELL_MODE=false
CLEAN_ON_EXIT=false
OPEN_BROWSER=true
CONTAINER_NAME="aws-cost-analyser-test"
IMAGE_NAME="aws-cost-analyser-test:latest"
DOCKERFILE="${SCRIPT_DIR}/Dockerfile.test"

# ── Colours ───────────────────────────────────────────────────────────────────
if [[ -t 1 ]]; then
  GREEN='\033[32m'; YELLOW='\033[33m'; RED='\033[31m'
  CYAN='\033[36m';  GREY='\033[90m';   BOLD='\033[1m'; RESET='\033[0m'
else
  GREEN=''; YELLOW=''; RED=''; CYAN=''; GREY=''; BOLD=''; RESET=''
fi

info()    { echo -e "${CYAN}▸${RESET} $*"; }
success() { echo -e "${GREEN}✓${RESET} $*"; }
warn()    { echo -e "${YELLOW}⚠${RESET} $*"; }
error()   { echo -e "${RED}✗${RESET} $*" >&2; }
step()    { echo -e "\n${BOLD}$*${RESET}"; }

# ── Argument parsing ──────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --profile|-p)    PROFILE="$2";       shift 2 ;;
    --strip-prefix)  STRIP_PREFIX="$2";  shift 2 ;;
    --port)          HOST_PORT="$2";     shift 2 ;;
    --zip)           ZIP_PATH="$2";   shift 2 ;;
    --shell|-s)      SHELL_MODE=true; shift   ;;
    --clean|-c)      CLEAN_ON_EXIT=true; shift ;;
    --no-browser)    OPEN_BROWSER=false; shift ;;
    --help|-h)
      sed -n '/^[^#]/q; s/^# \{0,1\}//p' "$0" | sed '1d'
      exit 0
      ;;
    *) error "Unknown option: $1"; exit 1 ;;
  esac
done

# ── Resolve defaults ──────────────────────────────────────────────────────────
if [[ -z "$ZIP_PATH" ]]; then
  ZIP_PATH="${SCRIPT_DIR}/aws-cost-analyser-${VERSION}.zip"
fi

if [[ -z "$PROFILE" ]]; then
  # Auto-detect first profile from ~/.aws/config
  PROFILE="$(grep '^\[profile ' ~/.aws/config 2>/dev/null | head -1 | sed 's/\[profile //;s/\]//' || true)"
  if [[ -z "$PROFILE" ]]; then
    error "No AWS profile specified and none detected in ~/.aws/config."
    error "Use --profile <name> or set AWS_PROFILE."
    exit 1
  fi
  warn "No --profile specified — using auto-detected profile: ${BOLD}${PROFILE}${RESET}"
fi

# ── Resolve short profile names against ~/.aws/config ─────────────────────────
# Supports both exact names and partial names (e.g. "cloudeng-1-dev" →
# "sandbox-cloudeng-1-dev") by searching for any profile whose name ends with
# or contains the supplied string.
_resolve_profile() {
  local name="$1"
  local config="${HOME}/.aws/config"
  [[ ! -f "$config" ]] && echo "$name" && return

  # Exact match first
  if grep -q "^\[profile ${name}\]" "$config"; then
    echo "$name"
    return
  fi

  # Suffix / substring match (picks the first match)
  local resolved
  resolved="$(grep '^\[profile ' "$config" \
    | sed 's/\[profile //;s/\]//' \
    | grep -i "${name}$" | head -1)"
  if [[ -z "$resolved" ]]; then
    resolved="$(grep '^\[profile ' "$config" \
      | sed 's/\[profile //;s/\]//' \
      | grep -i "${name}" | head -1)"
  fi
  echo "${resolved:-$name}"
}

RESOLVED_PROFILE="$(_resolve_profile "$PROFILE")"
if [[ "$RESOLVED_PROFILE" != "$PROFILE" ]]; then
  warn "Profile '${PROFILE}' resolved to full name: ${BOLD}${RESOLVED_PROFILE}${RESET}"
  PROFILE="$RESOLVED_PROFILE"
fi

# ── Banner ────────────────────────────────────────────────────────────────────
echo -e "\n${BOLD}AWS Cost Analyser — Docker End-to-End Test${RESET}"
echo -e "  Image     : ${CYAN}${IMAGE_NAME}${RESET}  (linux/amd64)"
echo -e "  Container : ${CONTAINER_NAME}"
echo -e "  Profile   : ${BOLD}${PROFILE}${RESET}"
[[ -n "$STRIP_PREFIX" ]] && echo -e "  Prefix    : ${GREY}stripping '${STRIP_PREFIX}'${RESET}"
echo -e "  ZIP       : ${ZIP_PATH}"
echo -e "  Port      : localhost:${HOST_PORT}"
[[ "$SHELL_MODE" == true ]] && echo -e "  Mode      : ${YELLOW}interactive shell${RESET}" || \
                               echo -e "  Mode      : automated analysis + serve"
echo ""

# ── Pre-flight checks ─────────────────────────────────────────────────────────
step "Step 1 — Pre-flight checks"

if ! command -v docker &>/dev/null; then
  error "Docker is not installed or not in PATH."
  exit 1
fi
success "Docker $(docker version --format '{{.Server.Version}}' 2>/dev/null)"

if ! docker info &>/dev/null; then
  error "Docker daemon is not running. Start Docker Desktop and retry."
  exit 1
fi

if [[ ! -f "$ZIP_PATH" ]]; then
  error "ZIP not found: ${ZIP_PATH}"
  error "Run  bash package.sh  to create it first."
  exit 1
fi
success "ZIP found: $(basename "$ZIP_PATH")  ($(du -sh "$ZIP_PATH" | cut -f1))"

if [[ ! -d "${HOME}/.aws" ]]; then
  warn "~/.aws directory not found — AWS calls inside the container may fail."
fi

# Verify the profile exists in ~/.aws/config (after resolution)
if ! grep -q "^\[profile ${PROFILE}\]" "${HOME}/.aws/config" 2>/dev/null; then
  warn "Profile '${PROFILE}' not found in ~/.aws/config — proceeding anyway."
else
  success "Profile '${PROFILE}' found in ~/.aws/config"
fi

# Remind user to refresh SSO session if applicable
if grep -A5 "^\[profile ${PROFILE}\]" "${HOME}/.aws/config" 2>/dev/null | grep -q "sso_start_url"; then
  info "SSO profile detected — checking token cache ..."
  SSO_TOKEN_VALID=$(python3 -c "
import json, glob, os
from datetime import datetime, timezone
files = glob.glob(os.path.expanduser('~/.aws/sso/cache/*.json'))
valid = any(
    'accessToken' in json.load(open(f))
    and datetime.fromisoformat(json.load(open(f)).get('expiresAt','1970').replace('Z','+00:00')) > datetime.now(timezone.utc)
    for f in files
    if open(f).read(1) == '{'
)
print('yes' if valid else 'no')
" 2>/dev/null || echo "unknown")
  if [[ "$SSO_TOKEN_VALID" == "no" ]]; then
    warn "SSO token appears expired. Run:  aws sso login --profile ${PROFILE}"
    warn "Then re-run this script."
    exit 1
  elif [[ "$SSO_TOKEN_VALID" == "yes" ]]; then
    success "SSO token is valid"
  fi
fi

# ── Build Docker image ─────────────────────────────────────────────────────────
step "Step 2 — Build Docker image (linux/amd64)"
info "Building ${IMAGE_NAME} from ${DOCKERFILE} ..."
docker build \
  --platform linux/amd64 \
  --tag "${IMAGE_NAME}" \
  --file "${DOCKERFILE}" \
  "${SCRIPT_DIR}" \
  --quiet && success "Image built: ${IMAGE_NAME}"

# ── Remove existing test container ────────────────────────────────────────────
step "Step 3 — Start container"
if docker ps -a --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
  info "Removing existing container: ${CONTAINER_NAME}"
  docker rm -f "${CONTAINER_NAME}" &>/dev/null
fi

# Start detached container with:
#   • ~/.aws mounted read-only  (profiles + SSO cache available)
#   • AWS_PROFILE set
#   • Port 8080 forwarded for dashboard serving
docker run \
  --detach \
  --platform linux/amd64 \
  --name "${CONTAINER_NAME}" \
  --volume "${HOME}/.aws:/root/.aws:ro" \
  --env "AWS_PROFILE=${PROFILE}" \
  --env "AWS_DEFAULT_REGION=${AWS_DEFAULT_REGION:-eu-central-1}" \
  --publish "${HOST_PORT}:8080" \
  "${IMAGE_NAME}" \
  tail -f /dev/null > /dev/null

success "Container '${CONTAINER_NAME}' started (linux/amd64)"

# ── Cleanup handler ───────────────────────────────────────────────────────────
_cleanup() {
  if [[ "$CLEAN_ON_EXIT" == true ]]; then
    step "Cleanup"
    info "Removing container ${CONTAINER_NAME} ..."
    docker rm -f "${CONTAINER_NAME}" &>/dev/null && success "Container removed"
    info "Removing image ${IMAGE_NAME} ..."
    docker rmi "${IMAGE_NAME}" &>/dev/null && success "Image removed"
  else
    echo -e "\n${GREY}Container '${CONTAINER_NAME}' is still running.${RESET}"
    echo -e "${GREY}Connect:  docker exec -it ${CONTAINER_NAME} bash${RESET}"
    echo -e "${GREY}Stop:     docker rm -f ${CONTAINER_NAME}${RESET}"
    echo -e "${GREY}Cleanup:  docker rm -f ${CONTAINER_NAME} && docker rmi ${IMAGE_NAME}${RESET}"
  fi
}
trap _cleanup EXIT

# ── Copy & extract ZIP ────────────────────────────────────────────────────────
step "Step 4 — Copy and extract archive"
ZIP_FILENAME="$(basename "$ZIP_PATH")"
info "Copying ${ZIP_FILENAME} into container ..."
docker cp "${ZIP_PATH}" "${CONTAINER_NAME}:/tmp/${ZIP_FILENAME}"

info "Extracting archive ..."
docker exec "${CONTAINER_NAME}" bash -c "
  cd /aws-cost-analyser && \
  unzip -o /tmp/${ZIP_FILENAME} -d . 2>&1 | grep -c 'inflating\|extracting' | \
    xargs -I{} echo 'Extracted {} files' || true && \
  unzip -o /tmp/${ZIP_FILENAME} -d . > /dev/null 2>&1
"
success "Archive extracted to /aws-cost-analyser"

# ── Interactive shell mode ─────────────────────────────────────────────────────
if [[ "$SHELL_MODE" == true ]]; then
  echo ""
  echo -e "${BOLD}Dropping into interactive shell.  Type 'exit' to leave.${RESET}"
  echo -e "${GREY}The container will keep running after you exit.${RESET}"
  echo -e "${GREY}Useful commands inside the container:${RESET}"
  echo -e "${GREY}  python3 run_analysis.py --profile ${PROFILE} --html${RESET}"
  echo -e "${GREY}  python3 -m http.server 8080 --directory output/${RESET}"
  echo ""
  docker exec -it \
    --env "AWS_PROFILE=${PROFILE}" \
    "${CONTAINER_NAME}" \
    bash
  exit 0
fi

# ── Run analysis ──────────────────────────────────────────────────────────────
step "Step 5 — Run cost analysis (this may take several minutes)"
info "Profile: ${BOLD}${PROFILE}${RESET}  |  Region: ${AWS_DEFAULT_REGION:-eu-central-1}"
echo -e "${GREY}Tip: open another terminal and run:${RESET}"
echo -e "${GREY}  docker exec -it ${CONTAINER_NAME} bash${RESET}"
echo ""

docker exec \
  --env "AWS_PROFILE=${PROFILE}" \
  --env "AWS_DEFAULT_REGION=${AWS_DEFAULT_REGION:-eu-central-1}" \
  "${CONTAINER_NAME}" \
  bash -c "
    set -euo pipefail
    cd /aws-cost-analyser

    echo '── Installing Python dependencies ──'
    pip install -q --root-user-action=ignore -r requirements.txt

    echo '── Installing Node.js dashboard dependencies ──'
    cd dashboard && npm install --silent && cd ..

    echo '── Discovering account metadata ──'
    python3 discover_accounts.py \
      --profiles '${PROFILE}' \
      ${STRIP_PREFIX:+--strip-prefix '${STRIP_PREFIX}'}

    echo '── Running cost analysis ──'
    python3 run_analysis.py \
      --profile '${PROFILE}' \
      --region  '${AWS_DEFAULT_REGION:-eu-central-1}' \
      --html
  "
success "Analysis complete"

# ── Serve the HTML report ─────────────────────────────────────────────────────
step "Step 6 — Serve dashboard on port ${HOST_PORT}"
docker exec --detach "${CONTAINER_NAME}" bash -c "
  cd /aws-cost-analyser && \
  python3 -m http.server 8080 --directory output/ \
    >> /tmp/server.log 2>&1
"
sleep 1

# Verify server is up
if docker exec "${CONTAINER_NAME}" bash -c "curl -sf http://localhost:8080/ > /dev/null 2>&1"; then
  success "Dashboard serving on http://localhost:${HOST_PORT}/"
else
  warn "Server may still be starting — try opening the browser in a moment."
fi

# ── Open in browser ───────────────────────────────────────────────────────────
DASHBOARD_URL="http://localhost:${HOST_PORT}/AWS_Cost_Dashboard.html"

step "Step 7 — Open dashboard in browser"
info "URL: ${BOLD}${DASHBOARD_URL}${RESET}"

if [[ "$OPEN_BROWSER" == true ]]; then
  case "$(uname -s)" in
    Darwin*)  open "${DASHBOARD_URL}" ;;
    Linux*)   xdg-open "${DASHBOARD_URL}" 2>/dev/null || true ;;
    CYGWIN*|MINGW*) cmd.exe /c start "${DASHBOARD_URL}" ;;
  esac
  success "Browser opened"
else
  info "Browser auto-open skipped (--no-browser).  Open manually: ${DASHBOARD_URL}"
fi

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
echo -e "${GREEN}${BOLD}  All steps completed successfully!${RESET}"
echo -e "${GREEN}${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
echo ""
echo -e "  Dashboard : ${CYAN}${DASHBOARD_URL}${RESET}"
echo -e "  Container : ${CONTAINER_NAME}  (still running)"
echo ""
echo -e "${BOLD}Useful follow-up commands:${RESET}"
echo -e "  ${GREY}# Connect interactively${RESET}"
echo -e "  docker exec -it ${CONTAINER_NAME} bash"
echo ""
echo -e "  ${GREY}# Copy generated reports to host${RESET}"
echo -e "  docker cp ${CONTAINER_NAME}:/aws-cost-analyser/output ./output-from-container"
echo ""
echo -e "  ${GREY}# Stop and remove everything when done${RESET}"
echo -e "  docker rm -f ${CONTAINER_NAME} && docker rmi ${IMAGE_NAME}"
echo ""

# Prevent _cleanup from removing container (user will do it manually)
CLEAN_ON_EXIT=false
