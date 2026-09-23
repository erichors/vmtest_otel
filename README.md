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

### Launch the instance (AWS Management Console)

1. Sign in to the [AWS Console](https://console.aws.amazon.com/), and in the
   top-right region picker, select the region you want to deploy into (e.g.
   `us-east-1`). Note it down — you'll need it later to find the instance
   again.
2. Open the **EC2** service, then click **Instances** in the left nav, then
   the orange **Launch instance** button.
3. **Name and tags**: enter `dtdemo-app01`.
4. **Application and OS Images (Amazon Machine Image)**: the default
   quick-start tab already shows **Amazon Linux**. Make sure the dropdown
   underneath is set to **Amazon Linux 2023 AMI** (the default/first option),
   architecture **64-bit (x86)**.
5. **Instance type**: click the instance type dropdown/search box and select
   `t3.medium`.
6. **Key pair (login)**:
   - Click **Create new key pair**.
   - Key pair name: `dtdemo-key`.
   - Key pair type: **RSA**. Private key file format: **.pem** (use `.ppk`
     only if you're connecting with PuTTY on Windows).
   - Click **Create key pair** — your browser downloads the `.pem` file
     immediately. Move it somewhere durable, e.g. `~/.ssh/dtdemo-key.pem`.
     AWS does not let you download it again later.
7. **Network settings**: click **Edit** in the top-right of this panel.
   - VPC: leave the default VPC selected (or pick your own if you have one).
   - Auto-assign public IP: **Enable**.
   - Firewall (security groups): select **Create security group**.
     - Security group name: `dtdemo-sg`.
     - It will already show one inbound rule for SSH (port 22). Change its
       **Source type** to **My IP** so AWS fills in your current public IP
       automatically, scoped with a `/32`.
     - Click **Add security group rule** for a second rule only if you want
       to hit `checkout-java` from your laptop for a demo: Type = **Custom
       TCP**, Port range = `8080`, Source type = **My IP**. Skip this rule
       entirely if you don't need outside access — see the perimeter-control
       note above.
8. **Configure storage**: change the root volume size to `20` (GiB), and the
   volume type dropdown to **gp3**.
9. Review the **Summary** panel on the right, then click **Launch instance**.
10. Click the instance ID link on the confirmation page (or go back to
    **Instances**) and wait until **Instance state** shows **Running** and
    **Status check** shows **2/2 checks passed** (takes 1-2 minutes).
11. Select the instance's checkbox, click **Connect** at the top, open the
    **SSH client** tab, and copy the example `ssh -i ...` command shown there
    — it already has the correct public IP/DNS filled in.
12. From your local terminal:
    ```bash
    chmod 400 ~/.ssh/dtdemo-key.pem
    ```
    then paste the `ssh` command you copied in step 11 (or run it as
    `ssh -i ~/.ssh/dtdemo-key.pem ec2-user@<public-ip-or-dns>`, using the
    Public IPv4 address/DNS shown on the instance's **Details** tab).

You're now logged into the instance as `ec2-user`. Continue to Prerequisites
and Install below.

### Terminate the instance and clean up the AWS-side resources (Console)

When you're done demoing (in addition to the app-level teardown at the
bottom of this README):

1. EC2 > **Instances** > select `dtdemo-app01` > **Instance state** >
   **Terminate instance** > confirm.
2. EC2 > **Security Groups** (left nav) > select `dtdemo-sg` > **Actions** >
   **Delete security group** > confirm. (AWS will refuse this if the
   instance hasn't fully terminated yet — wait a minute and retry.)
3. EC2 > **Key Pairs** (left nav) > select `dtdemo-key` > **Actions** >
   **Delete** > confirm. Then delete the local file:
   ```bash
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

## Prerequisites (on the instance)

You're SSH'd into a stock Amazon Linux 2023 instance at this point - nothing
is installed yet. `install/01-provision-host.sh` (next section) installs
everything the app needs, but you need **git** before you can even clone
this repo down, so install that much by hand first:

```bash
sudo dnf -y update
sudo dnf -y install git java-17-amazon-corretto-devel maven python3 python3-pip curl jq tar
```

Verify each landed:

```bash
git --version       # git version 2.x
java -version       # openjdk version "17...", Amazon Corretto
mvn -version        # Apache Maven 3.x
python3 --version   # Python 3.9+
```

This is the same package list `01-provision-host.sh` installs, so running it
again in the next step is harmless - it'll just no-op on anything already
present. You only need to do this by hand if you want git available before
that script exists on the box (i.e. before you've cloned the repo), or if
you're sanity-checking the AMI before proceeding.

## Install

Run these in order, as root, on the EC2 instance. `04-build-and-deploy.sh`
expects the repo to be checked out at `/opt/dtdemo/app`.

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

Then terminate the EC2 instance and clean up the security group/key pair -
see "Terminate the instance and clean up the AWS-side resources (Console)"
above.

## Cost note

A `t3.medium` running continuously is roughly **$30/month** plus one
Dynatrace host unit - stop the instance when you're not actively demoing.
