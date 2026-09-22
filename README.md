# dt-demo-app - Dynatrace + OpenTelemetry two-tier demo

A minimal two-tier checkout application built to demonstrate a specific
Dynatrace deployment pattern: **OneAgent running infrastructure-monitoring-only**,
with **all distributed tracing supplied by OpenTelemetry SDKs** exporting to
the local OneAgent Extension Execution Controller (EEC). This document covers
provisioning, telemetry wiring, install, verification, load generation, fault
injection, and teardown.

## What this is

```
                                                    EC2 host (Amazon Linux 2023)
                                          ┌───────────────────────────────────────────────┐
                                          │                                                 │
  ┌───────────┐   HTTP    ┌───────────────────────┐   HTTP    ┌────────────────────────┐  │
  │  loadgen  │ ────────▶ │  checkout-java :8080  │ ────────▶ │  pricing-python :8000  │  │
  │ (loadgen  │           │  (Spring Boot,        │           │  (Flask/gunicorn,      │  │
  │  .py, NOT │           │   OTel javaagent)     │           │   opentelemetry-       │  │
  │  traced)  │           │                        │           │   instrument)          │  │
  └───────────┘           └───────────┬────────────┘           └────────────┬───────────┘  │
                                       │  JDBC                                │  psycopg2   │
                                       └──────────────────┬───────────────────┘             │
                                                            ▼                                │
                                                  ┌────────────────────┐                     │
                                                  │  PostgreSQL :5432  │                     │
                                                  │      (dtdemo)      │                     │
                                                  └────────────────────┘                     │
                                                                                              │
        OTLP/HTTP-protobuf, uncompressed                                                     │
        traces only, both tiers  ────────────────▶  http://localhost:14499/otlp/v1/traces    │
                                                     (OneAgent Extension Execution Controller) │
                                                                     │                          │
                                          ┌──────────────────────────┴──────────────┐          │
                                          │   Dynatrace OneAgent (infra-only mode)   │          │
                                          │   host metrics, host+process logs,      │          │
                                          │   process/network topology              │          │
                                          └───────────────────────┬──────────────────┘         │
                                                                    │ HTTPS 443                 │
                                                                    ▼                            │
                                          ┌──────────────────────────────────────────┐          │
                                          │              Dynatrace tenant             │◀─────────┘
                                          └──────────────────────────────────────────┘
```

- **checkout-java** (`java-tier/`) - Spring Boot, port 8080. Owns the order
  flow: looks up the customer, calls pricing-python for price/discount/stock,
  writes the order, returns it.
- **pricing-python** (`python-tier/`) - Flask/gunicorn, port 8000. Owns
  pricing, inventory, and discount logic against Postgres.
- **PostgreSQL 15** - single `dtdemo` database shared by both tiers.
- **loadgen** - a plain, un-instrumented Python client that drives realistic
  traffic and can script fault-injection demos.

## How telemetry flows

| Signal | Source | Destination |
|---|---|---|
| Host metrics, processes, network topology | Dynatrace OneAgent (infra-only) | Dynatrace tenant directly |
| Host + application logs (stdout/journal) | Dynatrace OneAgent log monitoring (active even in infra-only mode) | Dynatrace tenant directly |
| Distributed traces (HTTP spans, JDBC/psycopg2 DB client spans, manual business-logic spans) | OpenTelemetry SDKs on each tier | local OneAgent EEC `:14499` → Dynatrace tenant |
| Database statements | Captured as OTel **client spans** via the JDBC and psycopg2 auto-instrumentation - **not** OneAgent deep-code, since OneAgent is infra-only | same OTLP path as traces above |

**Set expectations correctly for this deployment mode:** because OneAgent is
infrastructure-monitoring-only, you do **not** get automatic service
detection, code-level/method-level hotspots, or OneAgent's own PurePath
traces. The `checkout-java` and `pricing-python` **services in Dynatrace are
created by the OTel spans**, not by OneAgent. If the OTel exporters aren't
reaching port 14499, no services will appear at all, even though OneAgent
itself looks perfectly healthy.

## AWS setup

### Requirements summary

- **Instance**: EC2 `t3.medium` or larger (2 vCPU / 4GB RAM is the practical
  minimum for Java + Python + Postgres on one box).
- **AMI**: latest Amazon Linux 2023.
- **Storage**: 20GB gp3 root volume.
- **Security group**:
  - Inbound SSH (22) from your IP only.
  - Inbound 8080 only if you want to hit checkout-java from outside the box
    (e.g. to demo from your laptop) - note the app binds all interfaces
    (`server.port: 8080` with no bind-address restriction), so treat this SG
    rule as your actual perimeter control.
  - Outbound 443 to your Dynatrace tenant (OneAgent communication + any
    manual API calls).
- **IAM**: not required for anything in this demo.

### Build the instance from scratch (AWS CLI)

Everything below assumes you have the AWS CLI v2 installed and configured
(`aws configure`) with credentials that can create EC2 key pairs, security
groups, and instances in the target region. Replace `us-east-1` throughout if
you want a different region.

```bash
# 0. Pick a region and grab your current public IP for the SSH rule
export AWS_REGION=us-east-1
export MY_IP="$(curl -s https://checkip.amazonaws.com)/32"

# 1. Look up the latest Amazon Linux 2023 (x86_64) AMI via SSM public parameters
#    (no hardcoded AMI IDs - these go stale and differ per region)
export AMI_ID="$(aws ssm get-parameter \
  --region "$AWS_REGION" \
  --name /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64 \
  --query 'Parameter.Value' --output text)"
echo "AMI: $AMI_ID"

# 2. Create a key pair and save the private key locally (chmod 400 is required
#    for ssh to accept it)
aws ec2 create-key-pair --region "$AWS_REGION" \
  --key-name dtdemo-key \
  --query 'KeyMaterial' --output text > ~/.ssh/dtdemo-key.pem
chmod 400 ~/.ssh/dtdemo-key.pem

# 3. Find your default VPC (or swap in a specific --vpc-id you already use)
export VPC_ID="$(aws ec2 describe-vpcs --region "$AWS_REGION" \
  --filters Name=is-default,Values=true \
  --query 'Vpcs[0].VpcId' --output text)"

# 4. Create a security group scoped to this demo
export SG_ID="$(aws ec2 create-security-group --region "$AWS_REGION" \
  --group-name dtdemo-sg \
  --description "dt-demo-app: SSH + checkout-java" \
  --vpc-id "$VPC_ID" \
  --query 'GroupId' --output text)"

# SSH from your IP only
aws ec2 authorize-security-group-ingress --region "$AWS_REGION" \
  --group-id "$SG_ID" --protocol tcp --port 22 --cidr "$MY_IP"

# checkout-java from your IP only (drop/edit this rule if you don't need
# outside access - see the perimeter-control note above)
aws ec2 authorize-security-group-ingress --region "$AWS_REGION" \
  --group-id "$SG_ID" --protocol tcp --port 8080 --cidr "$MY_IP"

# 5. Launch the instance: t3.medium, 20GB gp3 root volume, AL2023
export INSTANCE_ID="$(aws ec2 run-instances --region "$AWS_REGION" \
  --image-id "$AMI_ID" \
  --instance-type t3.medium \
  --key-name dtdemo-key \
  --security-group-ids "$SG_ID" \
  --block-device-mappings '[{"DeviceName":"/dev/xvda","Ebs":{"VolumeSize":20,"VolumeType":"gp3","DeleteOnTermination":true}}]' \
  --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=dtdemo-app01}]' \
  --query 'Instances[0].InstanceId' --output text)"
echo "Instance: $INSTANCE_ID"

# 6. Wait for it to come up, then grab its public IP
aws ec2 wait instance-running --region "$AWS_REGION" --instance-ids "$INSTANCE_ID"
export PUBLIC_IP="$(aws ec2 describe-instances --region "$AWS_REGION" \
  --instance-ids "$INSTANCE_ID" \
  --query 'Reservations[0].Instances[0].PublicIpAddress' --output text)"
echo "Public IP: $PUBLIC_IP"

# 7. SSH in (give cloud-init a few seconds to finish on first boot)
ssh -i ~/.ssh/dtdemo-key.pem ec2-user@"$PUBLIC_IP"
```

Prefer the console instead? Launch an instance with: AMI = latest Amazon
Linux 2023, type = `t3.medium`, a key pair you control, a security group
matching the rules above, and a 20GB gp3 root volume — then SSH in as
`ec2-user` and skip to the Install section below.

### Terminate the instance and clean up the AWS-side resources

Run this when you're done (in addition to the app-level teardown at the
bottom of this README):

```bash
aws ec2 terminate-instances --region "$AWS_REGION" --instance-ids "$INSTANCE_ID"
aws ec2 wait instance-terminated --region "$AWS_REGION" --instance-ids "$INSTANCE_ID"
aws ec2 delete-security-group --region "$AWS_REGION" --group-id "$SG_ID"
aws ec2 delete-key-pair --region "$AWS_REGION" --key-name dtdemo-key
rm -f ~/.ssh/dtdemo-key.pem
```

## Dynatrace prerequisites

1. **Extension Execution Controller toggles** (Settings > Preferences >
   Extension Execution Controller in your tenant):
   - Enable **"Enable Extension Execution Controller"**
   - Enable **"Enable local HTTP Metric, Log and Event Ingest API"** - this is
     the one that actually opens local port 14499 for OTLP trace ingest, even
     though the label only mentions metrics/logs/events. Both must be on.
   - This local ingest endpoint **is available in infrastructure-monitoring-only
     mode** - it is only unavailable in containerized/classic full-stack setups
     without a host OneAgent, which doesn't apply here since OneAgent is
     installed directly on the EC2 host.
2. **PaaS token**: Settings > Integration > Dynatrace API - create a token with
   the "PaaS integration - installer download" scope (this is what
   `install/02-install-oneagent.sh` uses to download the OneAgent installer).
3. Remember port 14499 only starts listening a minute or two **after** you
   flip the toggle above - don't troubleshoot prematurely.

## Install

Run these in order, as root, on the EC2 instance. First get this repo onto
the box (git clone, scp, or rsync) - `04-build-and-deploy.sh` expects it at
`/opt/dtdemo/app`.

```bash
# 0. Get the repo onto the box
git clone <your-repo-url> /opt/dtdemo/app
cd /opt/dtdemo/app/install

# 1. Provision the host: packages, PostgreSQL, dtdemo role/db/user, /etc/dtdemo env files
sudo DB_PASSWORD='choose-a-real-password' ./01-provision-host.sh

# 2. Install Dynatrace OneAgent (infra-only mode)
sudo DT_ENV_URL='https://abc12345.live.dynatrace.com' \
     DT_PAAS_TOKEN='dt0c01.XXXXXXXX...' \
     ./02-install-oneagent.sh
# --> now go flip the two EEC toggles in the tenant (see above) if you haven't already

# 3. Download the OpenTelemetry Java agent
sudo ./03-download-otel-agent.sh
# or pin a version:
# sudo OTEL_JAVAAGENT_VERSION=2.9.0 ./03-download-otel-agent.sh

# 4. Build both tiers, load the schema, install systemd units, start both app tiers
sudo ./04-build-and-deploy.sh
```

If you changed `DB_PASSWORD` in step 1, make sure it matches what ends up in
`/etc/dtdemo/java.env` and `/etc/dtdemo/python.env` (step 1 patches both
files automatically from the `DB_PASSWORD` you passed in).

## Verify

**Application smoke tests** (endpoint paths and payload shapes taken directly
from the source):

```bash
# Liveness (custom controllers, not Spring Actuator)
curl -s http://localhost:8080/health
curl -s http://localhost:8000/health

# Place an order (flows checkout-java -> pricing-python -> Postgres and back)
curl -s -X POST http://localhost:8080/api/orders \
     -H 'Content-Type: application/json' \
     -d '{"customerId":1,"sku":"ELEC-001","qty":2}'

# Read it back (returns {"order": {...}, "events": [...]})
curl -s http://localhost:8080/api/orders/1

# Recent orders / the deliberately heavy revenue report
curl -s 'http://localhost:8080/api/orders/recent?limit=5'
curl -s http://localhost:8080/api/reports/revenue

# pricing-python directly
curl -s 'http://localhost:8000/api/pricing/ELEC-001?qty=2&tier=gold'
curl -s http://localhost:8000/api/inventory/low
```

**Port and process checks**:

```bash
ss -ltnp | grep 14499                          # OneAgent EEC OTLP ingest listening?
/opt/dynatrace/oneagent/agent/tools/oneagentctl --get-host-name
systemctl status oneagent
journalctl -u dtdemo-java -f
journalctl -u dtdemo-python -f
```

**Confirming traces arrive in Dynatrace**: open **Distributed Traces** or
**Services** in your tenant and filter on `service.name` = `checkout-java` and
`service.name` = `pricing-python` (from `OTEL_SERVICE_NAME` /
`OTEL_RESOURCE_ATTRIBUTES` in the `/etc/dtdemo/*.env` files). If you sent a
few requests via the smoke tests above, you should see both services and a
trace spanning both within a minute or so.

## Generating load

Run directly (useful while watching the console output):

```bash
sudo -u dtdemo /opt/dtdemo/venv/bin/python /opt/dtdemo/app/loadgen/loadgen.py \
     --target http://localhost:8080 --rate 5 --workers 8
```

Or via systemd (steady background traffic, restarts on failure):

```bash
sudo systemctl start dtdemo-loadgen     # NOT enabled by 04-build-and-deploy.sh on purpose
sudo systemctl enable dtdemo-loadgen    # only if you want it to survive reboots too
journalctl -u dtdemo-loadgen -f
```

Flags:

| Flag | Default | Meaning |
|---|---|---|
| `--rate` | 5 | aggregate requests/sec across all workers |
| `--workers` | 8 | worker thread count |
| `--duration` | 0 | seconds to run, `0` = forever |
| `--target` | `http://localhost:8080` | checkout-java base URL |
| `--pricing-target` | `http://localhost:8000` | pricing-python base URL, used only by `--scenario` to inject faults directly into that tier |
| `--scenario` | off | run the scripted demo sequence below instead of pure steady state (ignores `--duration`) |

Traffic mix: ~70% `POST /api/orders`, ~15% `GET /api/orders/recent`, ~10%
`GET /api/orders/{id}` on a recently created id, ~5% `GET /api/reports/revenue`.
Every 10 seconds it prints total requests, req/s, p50/p95/p99 latency, and a
status-code histogram; Ctrl-C (or `systemctl stop`) prints a final summary.

**`--scenario` demo sequence** (fault *rate* below is the probability a given
request to that tier is faulted - independent from `--rate`, the traffic
volume):

| Phase | Duration | What's happening | What to look for in Dynatrace |
|---|---|---|---|
| Normal (baseline) | 5 min | steady traffic, no faults | clean trace waterfalls, flat response time |
| Python tier SLOW (rate 0.5, 2000ms) | 3 min | half of pricing-python calls sleep 2s in-process | checkout-java response time rises, waterfall shows a wide gap inside the `pricing-python` span with no extra DB span |
| Normal (recovery) | 2 min | faults cleared | response time drops back to baseline |
| Java tier ERROR (rate 0.25) | 3 min | 25% of `POST /api/orders` throw before any downstream call | failure rate spike on `checkout-java`, traces stop early (no pricing-python span at all) |
| Normal (recovery) | 2 min | faults cleared | recovers |
| Python tier DBSLOW (rate 0.4, 1500ms) | 3 min | 40% of pricing calls run `SELECT pg_sleep()` on Postgres | a very visible, slow `pg_sleep` DB client span nested under `pricing-python`, distinct in shape from the plain SLOW phase |
| Normal (final) | until stopped | faults cleared | back to baseline; runs until you Ctrl-C |

A banner prints in the console every time the phase changes.

## Fault injection

Both tiers expose the identical `/api/admin/fault` contract with mode
`none | slow | error | dbslow`, but **different JSON key casing** for the
slow-duration field - `slowMs` on Java, `slow_ms` on Python. `faultctl.sh`
handles this for you:

```bash
cd /opt/dtdemo/app/loadgen
./faultctl.sh status                 # GET fault state from both tiers
./faultctl.sh java slow 0.5 2000      # 50% of checkout-java requests sleep 2000ms
./faultctl.sh python error 0.2        # 20% of pricing-python requests fail
./faultctl.sh clear                   # mode=none, rate=0 on both tiers
```

| Mode | What it does | What to look for in Dynatrace |
|---|---|---|
| `none` | no-op (default) | baseline |
| `slow` | sleeps in-process before doing anything else (Java: before the customer lookup; Python: before the product query) | latency increase with no corresponding DB span growth |
| `error` | throws/raises immediately (Java: 500 from `applyFault()` before any DB or downstream call; Python: `RuntimeError` caught and returned as a 500 JSON body) | failure-rate spike; on Java the trace has no downstream pricing-python span at all since the error fires before that call |
| `dbslow` | runs `SELECT pg_sleep(slowMs/1000)` against Postgres (Java: `OrderRepository.pgSleep`; Python: a dedicated cursor execute) | a distinct, slow `pg_sleep` database client span in the waterfall |

## What to demo in Dynatrace

- A full distributed trace waterfall spanning `checkout-java` -> `pricing-python`
  -> Postgres for a single `POST /api/orders`.
- The Service Flow / service-to-service view showing both services and the
  DB, built entirely from OTel spans (no OneAgent deep-code involved).
- The two manual child spans in pricing-python - `compute_discount` and
  `check_inventory` - each carrying business attributes
  (`customer.tier`, `discount.total_pct`, `inventory.qty_on_hand`, etc.).
- The JDBC and psycopg2 database client spans, including the deliberately
  slower `GET /api/reports/revenue` query and the `pg_sleep` fault-injection span.
- Response-time degradation during the `slow`/`dbslow` fault phases.
- Failure-rate spike during the `error` fault phase.
- Correlating a response-time or failure spike with host metrics (CPU,
  memory, network) from OneAgent on the same host timeline.

## Troubleshooting

- **No traces appearing at all**:
  - Confirm both EEC toggles are enabled in the tenant (see Prerequisites) -
    this is the single most common cause.
  - `ss -ltnp | grep 14499` - if nothing is listening, the toggle either isn't
    on yet or hasn't propagated (allow a minute or two).
  - Confirm `OTEL_EXPORTER_OTLP_TRACES_COMPRESSION=none` in both env files -
    OneAgent's EEC endpoint rejects `Content-Encoding` on this port.
  - Confirm `OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf` - **gRPC is not
    supported** on this endpoint, only HTTP/protobuf.
  - Confirm the endpoint URL is exactly `http://localhost:14499/otlp/v1/traces`
    in both env files.
- **401/403 during OneAgent install**: the PaaS token is missing the
  "PaaS integration - installer download" scope, or `DT_ENV_URL` is wrong
  (no trailing slash, must be the tenant base URL).
- **Postgres auth failures** (`password authentication failed`): re-check
  `pg_hba.conf` was patched to `scram-sha-256` for `local` and
  `host 127.0.0.1/32`, and that `DB_PASSWORD` in `/etc/dtdemo/*.env` matches
  what was actually set for the `dtdemo` role (re-run
  `01-provision-host.sh` with the same `DB_PASSWORD` to reset it, or
  `ALTER ROLE dtdemo WITH PASSWORD '...'` manually).
- **Java agent not attaching**: check `journalctl -u dtdemo-java` for a
  javaagent premain error, and confirm
  `/opt/dtdemo/otel/opentelemetry-javaagent.jar` exists and is >1MB (re-run
  `03-download-otel-agent.sh` if not).
- **Python instrumentation missing after a venv rebuild**: re-run
  `/opt/dtdemo/venv/bin/opentelemetry-bootstrap -a install` after any
  `pip install -r requirements.txt` - `requirements.txt` pins the
  instrumentation packages directly, but bootstrap is still the safety net
  for anything it detects that isn't pinned.

## Teardown

```bash
sudo systemctl disable --now dtdemo-loadgen dtdemo-python dtdemo-java
sudo -u postgres psql -c "DROP DATABASE dtdemo;"
sudo -u postgres psql -c "DROP ROLE dtdemo;"
sudo /opt/dynatrace/oneagent/agent/uninstall.sh
```

Then terminate the EC2 instance from the AWS console/CLI.

## Cost note

A `t3.medium` running continuously is roughly **$30/month** plus one
Dynatrace host unit - stop the instance when you're not actively demoing.
