#!/usr/bin/env bash
#
# 01-provision-host.sh
#
# Idempotent host provisioning for the dt-demo-app two-tier Dynatrace/OpenTelemetry
# demo, on a fresh Amazon Linux 2023 EC2 instance. Run as root.
#
# What this does:
#   - installs Corretto 17, Maven, PostgreSQL 15, Python 3, and misc tooling
#   - initializes and starts PostgreSQL
#   - creates the `dtdemo` role + `dtdemo` database (idempotent)
#   - tightens local/loopback pg_hba.conf auth to scram-sha-256
#   - creates the `dtdemo` system user and /opt/dtdemo, /etc/dtdemo directory tree
#   - installs /etc/dtdemo/java.env and /etc/dtdemo/python.env from the templates
#     shipped next to this script (install/dtdemo-java.env, install/dtdemo-python.env)
#
# It does NOT install Dynatrace OneAgent (see 02-install-oneagent.sh), does NOT
# download the OTel Java agent (see 03-download-otel-agent.sh), and does NOT
# build/deploy the application (see 04-build-and-deploy.sh).
#
# Usage:
#   DB_PASSWORD=<something-better-than-the-default> ./01-provision-host.sh
#
# DB_PASSWORD defaults to "dtdemo" (matches the default DB_PASSWORD baked into
# both application tiers' config, per java-tier/src/main/resources/application.yml
# and python-tier/app.py) if not set. Change it for anything beyond a throwaway demo.

set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "ERROR: run this script as root (sudo -i, then run it)." >&2
  exit 1
fi

DB_PASSWORD="${DB_PASSWORD:-dtdemo}"
DB_NAME="dtdemo"
DB_ROLE="dtdemo"
SYS_USER="dtdemo"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "==> [1/8] dnf update"
dnf -y update

echo "==> [2/8] installing packages"
dnf -y install \
  java-17-amazon-corretto-devel \
  maven \
  postgresql15-server \
  postgresql15 \
  python3 \
  python3-pip \
  git \
  jq \
  curl \
  tar

# ---------------------------------------------------------------------------
# PostgreSQL: figure out the real unit name. AL2023's postgresql15-server
# package has shipped the systemd unit as plain "postgresql" in most releases,
# but some AL2023 AMIs register it as "postgresql15". Detect whichever is
# actually present instead of guessing wrong.
# ---------------------------------------------------------------------------
PG_SERVICE="postgresql"
if systemctl list-unit-files 2>/dev/null | grep -q '^postgresql15\.service'; then
  PG_SERVICE="postgresql15"
fi
echo "==> [3/8] using PostgreSQL systemd unit: ${PG_SERVICE}"

PGDATA_DIR="/var/lib/pgsql/data"
if [[ ! -f "${PGDATA_DIR}/PG_VERSION" ]]; then
  echo "    initializing PostgreSQL data directory (${PGDATA_DIR})"
  if command -v postgresql-setup >/dev/null 2>&1; then
    postgresql-setup --initdb
  else
    "/usr/pgsql-15/bin/postgresql-15-setup" --initdb
  fi
else
  echo "    PostgreSQL data directory already initialized, skipping initdb"
fi

systemctl enable "${PG_SERVICE}"
systemctl start "${PG_SERVICE}"

echo "==> [4/8] waiting for PostgreSQL to accept connections"
for _ in $(seq 1 30); do
  if sudo -u postgres psql -tAc 'SELECT 1' >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

echo "==> [5/8] creating role/database (idempotent)"
ROLE_EXISTS=$(sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='${DB_ROLE}'" || true)
if [[ "${ROLE_EXISTS}" != "1" ]]; then
  echo "    creating role ${DB_ROLE}"
  sudo -u postgres psql -c "CREATE ROLE ${DB_ROLE} LOGIN PASSWORD '${DB_PASSWORD}';"
else
  echo "    role ${DB_ROLE} already exists, updating password"
  sudo -u postgres psql -c "ALTER ROLE ${DB_ROLE} WITH PASSWORD '${DB_PASSWORD}';"
fi

DB_EXISTS=$(sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='${DB_NAME}'" || true)
if [[ "${DB_EXISTS}" != "1" ]]; then
  echo "    creating database ${DB_NAME} owned by ${DB_ROLE}"
  sudo -u postgres psql -c "CREATE DATABASE ${DB_NAME} OWNER ${DB_ROLE};"
else
  echo "    database ${DB_NAME} already exists, skipping"
fi

echo "==> [6/8] tightening pg_hba.conf auth to scram-sha-256"
PG_HBA="${PGDATA_DIR}/pg_hba.conf"
cp -n "${PG_HBA}" "${PG_HBA}.orig" || true
sed -i -E 's/^(local[[:space:]]+all[[:space:]]+all[[:space:]]+)(peer|ident|trust|md5)$/\1scram-sha-256/' "${PG_HBA}"
sed -i -E 's/^(host[[:space:]]+all[[:space:]]+all[[:space:]]+127\.0\.0\.1\/32[[:space:]]+)(peer|ident|trust|md5)$/\1scram-sha-256/' "${PG_HBA}"
systemctl reload "${PG_SERVICE}"

echo "==> [7/8] creating system user and directory tree"
if ! id "${SYS_USER}" >/dev/null 2>&1; then
  useradd --system --no-create-home --shell /sbin/nologin "${SYS_USER}"
fi

mkdir -p /opt/dtdemo /etc/dtdemo
chown -R "${SYS_USER}:${SYS_USER}" /opt/dtdemo
chown root:"${SYS_USER}" /etc/dtdemo
chmod 750 /etc/dtdemo

echo "==> [8/8] installing /etc/dtdemo env files"
install_env_file() {
  local src="$1" dest="$2"
  if [[ -f "${dest}" ]]; then
    echo "    ${dest} already exists, leaving it alone (edit it by hand if needed)"
    return
  fi
  cp "${src}" "${dest}"
  sed -i "s/^DB_PASSWORD=.*/DB_PASSWORD=${DB_PASSWORD}/" "${dest}"
  chown root:"${SYS_USER}" "${dest}"
  chmod 640 "${dest}"
  echo "    installed ${dest}"
}
install_env_file "${SCRIPT_DIR}/dtdemo-java.env"   "/etc/dtdemo/java.env"
install_env_file "${SCRIPT_DIR}/dtdemo-python.env" "/etc/dtdemo/python.env"

cat <<EOF

============================================================================
Host provisioning complete.

  PostgreSQL service: ${PG_SERVICE}
  Database:           ${DB_NAME} (owner: ${DB_ROLE})
  Env files:           /etc/dtdemo/java.env, /etc/dtdemo/python.env

NEXT STEPS:
  1. Copy/clone this repo to /opt/dtdemo/app on this host, e.g.:
       git clone <your-repo-url> /opt/dtdemo/app
     (or rsync/scp the dt-demo-app directory there).
  2. Run install/02-install-oneagent.sh with your Dynatrace tenant URL + PaaS token.
  3. Run install/03-download-otel-agent.sh to fetch the OTel Java agent jar.
  4. Run install/04-build-and-deploy.sh to build, load the schema, and start both
     application tiers.
============================================================================
EOF
