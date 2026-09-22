#!/usr/bin/env bash
#
# 04-build-and-deploy.sh
#
# Builds both tiers, loads the DB schema, installs the systemd units, and
# starts checkout-java + pricing-python (NOT the load generator - start that
# yourself once you're ready to demo).
#
# Prerequisites:
#   - 01-provision-host.sh has been run
#   - 02-install-oneagent.sh has been run (and the tenant EEC toggles are on)
#   - 03-download-otel-agent.sh has been run
#   - This repo is checked out at /opt/dtdemo/app (git clone / rsync / scp it there)

set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "ERROR: run this script as root (sudo -i, then run it)." >&2
  exit 1
fi

APP_DIR="/opt/dtdemo/app"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ ! -f "${APP_DIR}/java-tier/pom.xml" ]]; then
  cat >&2 <<EOF
ERROR: ${APP_DIR}/java-tier/pom.xml not found.

This script expects the dt-demo-app repo to be checked out at ${APP_DIR}.
Copy/clone it there first, e.g.:
  git clone <your-repo-url> ${APP_DIR}
then re-run this script.
EOF
  exit 1
fi

echo "==> [1/6] building checkout-java"
mvn -q -f "${APP_DIR}/java-tier/pom.xml" clean package -DskipTests

# pom.xml pins <finalName>checkout-java-1.0.0</finalName>, so that is the
# exact jar name Maven produces under target/.
JAR_SRC="${APP_DIR}/java-tier/target/checkout-java-1.0.0.jar"
if [[ ! -f "${JAR_SRC}" ]]; then
  echo "ERROR: expected build output ${JAR_SRC} not found." >&2
  exit 1
fi
mkdir -p /opt/dtdemo/bin
cp "${JAR_SRC}" /opt/dtdemo/bin/checkout-java.jar
echo "    -> /opt/dtdemo/bin/checkout-java.jar"

echo "==> [2/6] building pricing-python virtualenv"
if [[ ! -d /opt/dtdemo/venv ]]; then
  python3 -m venv /opt/dtdemo/venv
fi
/opt/dtdemo/venv/bin/pip install --upgrade pip
/opt/dtdemo/venv/bin/pip install -r "${APP_DIR}/python-tier/requirements.txt"
# `requests` is what loadgen.py needs (it is also a transitive dependency of
# opentelemetry-instrumentation-requests, but pin it explicitly here so the
# load generator doesn't depend on that being true).
/opt/dtdemo/venv/bin/pip install requests
# Safety net: requirements.txt already pins the Flask/psycopg2/requests/logging
# instrumentation packages explicitly, so this is mostly a no-op, but it will
# pick up anything opentelemetry-bootstrap detects that requirements.txt didn't.
/opt/dtdemo/venv/bin/opentelemetry-bootstrap -a install

echo "==> [3/6] loading DB schema"
# Source DB_PASSWORD from the installed env file so this matches whatever
# 01-provision-host.sh actually set, even if it differs from the "dtdemo" default.
if [[ -f /etc/dtdemo/python.env ]]; then
  DB_PASSWORD_VAL="$(grep -E '^DB_PASSWORD=' /etc/dtdemo/python.env | tail -1 | cut -d= -f2-)" || true
fi
DB_PASSWORD_VAL="${DB_PASSWORD_VAL:-dtdemo}"
PGPASSWORD="${DB_PASSWORD_VAL}" psql -h 127.0.0.1 -U dtdemo -d dtdemo -f "${APP_DIR}/db/schema.sql"

echo "==> [4/6] setting ownership"
chown -R dtdemo:dtdemo /opt/dtdemo

echo "==> [5/6] installing systemd units"
cp "${SCRIPT_DIR}/systemd/dtdemo-java.service"    /etc/systemd/system/
cp "${SCRIPT_DIR}/systemd/dtdemo-python.service"  /etc/systemd/system/
cp "${SCRIPT_DIR}/systemd/dtdemo-loadgen.service" /etc/systemd/system/
systemctl daemon-reload

echo "==> [6/6] enabling and starting application tiers (loadgen is left disabled)"
systemctl enable --now dtdemo-java
systemctl enable --now dtdemo-python

cat <<'EOF'

============================================================================
Deploy complete. dtdemo-java and dtdemo-python are enabled and running.
dtdemo-loadgen was installed but NOT started/enabled - start it yourself
when you're ready to demo:
  systemctl start dtdemo-loadgen        # steady background traffic
  # or run it interactively to watch stats / use --scenario:
  sudo -u dtdemo /opt/dtdemo/venv/bin/python /opt/dtdemo/app/loadgen/loadgen.py

Smoke-test commands:
  curl -s http://localhost:8080/health
  curl -s http://localhost:8000/health
  curl -s -X POST http://localhost:8080/api/orders \
       -H 'Content-Type: application/json' \
       -d '{"customerId":1,"sku":"ELEC-001","qty":2}'
  curl -s http://localhost:8080/api/orders/recent?limit=5
  curl -s http://localhost:8080/api/reports/revenue

Check logs:
  journalctl -u dtdemo-java -f
  journalctl -u dtdemo-python -f
============================================================================
EOF
