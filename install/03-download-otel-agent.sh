#!/usr/bin/env bash
#
# 03-download-otel-agent.sh
#
# Downloads the OpenTelemetry Java instrumentation agent jar used to trace
# the checkout-java tier (attached via -javaagent: see java-tier/pom.xml,
# which deliberately declares NO OpenTelemetry SDK dependency - the javaagent
# is the only source of spans for that process, per
# install/systemd/dtdemo-java.service).
#
# Usage:
#   ./03-download-otel-agent.sh                     # latest release
#   OTEL_JAVAAGENT_VERSION=2.9.0 ./03-download-otel-agent.sh   # pinned version

set -euo pipefail

DEST_DIR="/opt/dtdemo/otel"
DEST_FILE="${DEST_DIR}/opentelemetry-javaagent.jar"
REPO="open-telemetry/opentelemetry-java-instrumentation"

mkdir -p "${DEST_DIR}"

if [[ -n "${OTEL_JAVAAGENT_VERSION:-}" ]]; then
  URL="https://github.com/${REPO}/releases/download/v${OTEL_JAVAAGENT_VERSION}/opentelemetry-javaagent.jar"
  echo "==> Downloading pinned OTel Java agent v${OTEL_JAVAAGENT_VERSION}"
else
  URL="https://github.com/${REPO}/releases/latest/download/opentelemetry-javaagent.jar"
  echo "==> Downloading latest OTel Java agent"
fi

curl -sSL -o "${DEST_FILE}" "${URL}"

SIZE=$(stat -c%s "${DEST_FILE}" 2>/dev/null || echo 0)
MIN_BYTES=$((1 * 1024 * 1024))
if [[ "${SIZE}" -lt "${MIN_BYTES}" ]]; then
  echo "ERROR: downloaded file is only ${SIZE} bytes (< 1MB) - download likely failed." >&2
  echo "       Check OTEL_JAVAAGENT_VERSION or your network/proxy settings." >&2
  exit 1
fi

echo "==> OK: ${DEST_FILE} (${SIZE} bytes)"
