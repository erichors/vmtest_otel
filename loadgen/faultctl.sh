#!/usr/bin/env bash
#
# faultctl.sh - manual curl+jq wrapper around each tier's /api/admin/fault
# endpoint. Both tiers share the mode vocabulary none|slow|error|dbslow, but
# use DIFFERENT JSON key casing for the slow-duration field:
#   java   (com.aheaddemo.checkout.AdminController): {"mode","rate","slowMs"}
#   python (python-tier/app.py update_fault_state):  {"mode","rate","slow_ms"}
#
# Usage:
#   ./faultctl.sh status
#   ./faultctl.sh java   <mode> [rate] [slowMs]
#   ./faultctl.sh python <mode> [rate] [slow_ms]
#   ./faultctl.sh clear
#
# Examples:
#   ./faultctl.sh status
#   ./faultctl.sh java slow 0.5 2000
#   ./faultctl.sh python error 0.2
#   ./faultctl.sh clear
#
# Env overrides: JAVA_URL (default http://localhost:8080), PYTHON_URL (default http://localhost:8000)

set -euo pipefail

JAVA_URL="${JAVA_URL:-http://localhost:8080}"
PYTHON_URL="${PYTHON_URL:-http://localhost:8000}"

usage() {
  cat <<'EOF'
Usage:
  ./faultctl.sh status
  ./faultctl.sh java   <mode> [rate] [slowMs]
  ./faultctl.sh python <mode> [rate] [slow_ms]
  ./faultctl.sh clear

  mode:            none | slow | error | dbslow
  rate:            0.0-1.0, probability a given request is faulted (default 0)
  slowMs/slow_ms:  sleep duration in ms for slow/dbslow modes (default 1500)

Env overrides: JAVA_URL (default http://localhost:8080), PYTHON_URL (default http://localhost:8000)
EOF
}

fault_get() {
  local url="$1" label="$2"
  echo "--- ${label} (${url}) ---"
  curl -sS "${url}/api/admin/fault" | jq .
}

fault_post() {
  local url="$1" body="$2" label="$3"
  echo "--- setting ${label} fault: ${body} ---"
  curl -sS -X POST -H 'Content-Type: application/json' -d "${body}" "${url}/api/admin/fault" | jq .
}

cmd="${1:-}"
case "${cmd}" in
  status)
    fault_get "${JAVA_URL}" "java"
    fault_get "${PYTHON_URL}" "python"
    ;;
  clear)
    fault_post "${JAVA_URL}"   '{"mode":"none","rate":0}' "java"
    fault_post "${PYTHON_URL}" '{"mode":"none","rate":0}' "python"
    ;;
  java)
    mode="${2:?mode required: none|slow|error|dbslow}"
    rate="${3:-0}"
    slow_ms="${4:-1500}"
    body="$(jq -n --arg mode "${mode}" --argjson rate "${rate}" --argjson slowMs "${slow_ms}" \
      '{mode: $mode, rate: $rate, slowMs: $slowMs}')"
    fault_post "${JAVA_URL}" "${body}" "java"
    ;;
  python)
    mode="${2:?mode required: none|slow|error|dbslow}"
    rate="${3:-0}"
    slow_ms="${4:-1500}"
    body="$(jq -n --arg mode "${mode}" --argjson rate "${rate}" --argjson slow_ms "${slow_ms}" \
      '{mode: $mode, rate: $rate, slow_ms: $slow_ms}')"
    fault_post "${PYTHON_URL}" "${body}" "python"
    ;;
  -h|--help|"")
    usage
    ;;
  *)
    echo "Unknown command: ${cmd}" >&2
    usage
    exit 1
    ;;
esac
