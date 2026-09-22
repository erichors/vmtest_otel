#!/usr/bin/env bash
#
# 02-install-oneagent.sh
#
# Installs Dynatrace OneAgent in INFRASTRUCTURE MONITORING ONLY mode. In this
# mode OneAgent supplies host metrics, host/process log ingestion, and
# process/network topology - it does NOT attach deep-code auto-instrumentation
# and does NOT create its own traces. All distributed tracing in this demo
# comes from the OpenTelemetry SDKs on each tier (see 03/04 + the /etc/dtdemo
# env files), so there is exactly one source of spans and zero risk of
# duplicate traces.
#
# Usage:
#   DT_ENV_URL=https://abc12345.live.dynatrace.com DT_PAAS_TOKEN=dt0c01.xxx ./02-install-oneagent.sh
#   ./02-install-oneagent.sh https://abc12345.live.dynatrace.com dt0c01.xxx
#
# DT_ENV_URL  - your Dynatrace environment/tenant base URL (no trailing slash)
# DT_PAAS_TOKEN - a Dynatrace API token with "PaaS integration - installer download" scope

set -euo pipefail

DT_ENV_URL="${DT_ENV_URL:-${1:-}}"
DT_PAAS_TOKEN="${DT_PAAS_TOKEN:-${2:-}}"

if [[ -z "${DT_ENV_URL}" || -z "${DT_PAAS_TOKEN}" ]]; then
  cat >&2 <<'EOF'
Usage:
  DT_ENV_URL=https://<tenant>.live.dynatrace.com DT_PAAS_TOKEN=<token> ./02-install-oneagent.sh
  ./02-install-oneagent.sh <DT_ENV_URL> <DT_PAAS_TOKEN>

Both DT_ENV_URL and DT_PAAS_TOKEN are required, either as environment
variables or positional arguments.
EOF
  exit 1
fi

if [[ "${EUID}" -ne 0 ]]; then
  echo "ERROR: run this script as root (sudo -i, then run it)." >&2
  exit 1
fi

INSTALLER_PATH="/tmp/Dynatrace-OneAgent-Linux.sh"
DOWNLOAD_URL="${DT_ENV_URL}/api/v1/deployment/installer/agent/unix/default/latest?arch=x86"

echo "==> Downloading OneAgent installer from ${DT_ENV_URL}"
curl -sS -o "${INSTALLER_PATH}" -H "Api-Token: ${DT_PAAS_TOKEN}" "${DOWNLOAD_URL}"

SIZE=$(stat -c%s "${INSTALLER_PATH}" 2>/dev/null || echo 0)
if [[ "${SIZE}" -lt 10000 ]]; then
  echo "ERROR: downloaded installer is suspiciously small (${SIZE} bytes)." >&2
  echo "       This usually means the token or environment URL is wrong. Response body:" >&2
  cat "${INSTALLER_PATH}" >&2
  exit 1
fi
echo "    downloaded ${SIZE} bytes"

chmod +x "${INSTALLER_PATH}"

echo "==> Installing OneAgent (infrastructure-monitoring-only mode)"
/bin/sh "${INSTALLER_PATH}" \
  --set-infra-only=true \
  --set-app-log-content-access=true \
  --set-host-group=dtdemo-otel \
  --set-host-name=dtdemo-app01

cat <<'EOF'

============================================================================
OneAgent installed in INFRASTRUCTURE MONITORING ONLY mode.

*** IMPORTANT - REQUIRED TENANT-SIDE STEP (do this now, in the Dynatrace UI) ***

  Go to: Settings > Preferences > Extension Execution Controller
  Enable BOTH toggles:
    1. "Enable Extension Execution Controller"
    2. "Enable local HTTP Metric, Log and Event Ingest API"

  The second toggle is what actually opens local port 14499 for OTLP trace
  ingest on this host, even though its label only mentions metrics/logs/events.
  Without it, the OpenTelemetry exporters on both application tiers will get
  connection-refused errors when they try to reach http://localhost:14499/otlp/v1/traces.

  After flipping the toggle it can take a minute or two to propagate to this
  host - don't panic if the port isn't listening immediately.

Verify after a minute or two:
  ss -ltnp | grep 14499
      (should show a LISTEN socket owned by a oneagent/EEC process)

Verify OneAgent itself:
  /opt/dynatrace/oneagent/agent/tools/oneagentctl --get-host-name
  systemctl status oneagent
============================================================================
EOF
