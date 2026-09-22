#!/usr/bin/env python3
"""
loadgen.py - steady-rate synthetic load generator + fault-injection driver
for the dt-demo-app two-tier Dynatrace/OpenTelemetry demo.

This drives the checkout-java tier ONLY (default http://localhost:8080).
Every request therefore flows checkout-java -> pricing-python -> Postgres,
so each one produces a single complete distributed trace across both tiers.

IMPORTANT: this script is deliberately plain stdlib + `requests`, with NO
OpenTelemetry instrumentation. It is the synthetic client generating demo
traffic, not part of the traced application - it must not appear as a
service in Dynatrace or inject trace headers.

Endpoints used (read directly from the checkout-java source):
  POST /api/orders            body: {"customerId": int, "sku": str, "qty": int}
                               -> 201 {"id": ..., "customerId": ..., "sku": ..., ...}
                               (com.aheaddemo.checkout.OrderController#createOrder)
  GET  /api/orders/recent?limit=20
  GET  /api/orders/{id}       -> {"order": {...}, "events": [...]}
  GET  /api/reports/revenue   (deliberately the heaviest query in the app)
  POST /api/admin/fault       body: {"mode": ..., "rate": ..., "slowMs": ...}   (Java: camelCase)

The pricing-python tier's fault endpoint uses the SAME path but snake_case
keys instead: {"mode": ..., "rate": ..., "slow_ms": ...} (python-tier/app.py).
Both tiers share the same mode vocabulary: none | slow | error | dbslow.

SKUs are generated in the exact PREFIX-NNN format seeded in db/schema.sql
(ELEC-001..008, APP-001..008, HOME-001..008, GRO-001..008, SPT-001..008 -
5 categories x 8 products = 40 SKUs). Customer ids are seeded 1-25.
"""

import argparse
import random
import signal
import sys
import threading
import time
from collections import Counter

try:
    import requests
except ImportError:
    sys.stderr.write("ERROR: the 'requests' package is required (pip install requests)\n")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Static demo data, matching db/schema.sql exactly.
# ---------------------------------------------------------------------------
SKU_PREFIXES = ["ELEC", "APP", "HOME", "GRO", "SPT"]
SKUS = [f"{prefix}-{i:03d}" for prefix in SKU_PREFIXES for i in range(1, 9)]  # 40 SKUs
CUSTOMER_IDS = list(range(1, 26))  # 25 seeded customers

# ---------------------------------------------------------------------------
# Scripted --scenario phases.
#   tier=None means "clear faults on both tiers" (baseline/normal traffic).
#   duration=0 on the final phase means "run until the process is stopped".
# rate here is the FAULT firing probability (FaultConfig.rate / _fault_state
# rate), a separate knob from --rate (the load generator's requests/sec).
# ---------------------------------------------------------------------------
SCENARIO_PHASES = [
    {"label": "normal (baseline)",  "tier": None,     "mode": None,    "rate": None, "slow_ms": None, "duration": 300},
    {"label": "python tier SLOW",   "tier": "python", "mode": "slow",  "rate": 0.5,  "slow_ms": 2000, "duration": 180},
    {"label": "normal (recovery)",  "tier": None,     "mode": None,    "rate": None, "slow_ms": None, "duration": 120},
    {"label": "java tier ERROR",    "tier": "java",   "mode": "error", "rate": 0.25, "slow_ms": None, "duration": 180},
    {"label": "normal (recovery)",  "tier": None,     "mode": None,    "rate": None, "slow_ms": None, "duration": 120},
    {"label": "python tier DBSLOW", "tier": "python", "mode": "dbslow","rate": 0.4,  "slow_ms": 1500, "duration": 180},
    {"label": "normal (final)",     "tier": None,     "mode": None,    "rate": None, "slow_ms": None, "duration": 0},
]


def pick_sku():
    return random.choice(SKUS)


def pick_customer_id():
    return random.choice(CUSTOMER_IDS)


def pick_qty():
    # Mostly small orders; occasionally large enough to trigger the >=10 and
    # >=50 volume discount tiers in pricing-python's compute_discount(), and
    # occasionally to exceed on-hand inventory (50-5000 units seeded) and
    # trigger a 409 out-of-stock response.
    if random.random() < 0.80:
        return random.randint(1, 3)
    return random.randint(10, 60)


# ---------------------------------------------------------------------------
# Rate limiter: simple token bucket, capacity = 1 second of tokens at the
# configured rate, shared by all worker threads so the AGGREGATE rate across
# all workers is roughly `rate` requests/sec.
# ---------------------------------------------------------------------------
class RateLimiter:
    def __init__(self, rate):
        self.rate = rate
        self.lock = threading.Lock()
        self.tokens = 0.0
        self.last = time.monotonic()

    def acquire(self):
        if self.rate <= 0:
            return
        while True:
            with self.lock:
                now = time.monotonic()
                elapsed = now - self.last
                self.last = now
                self.tokens = min(self.rate, self.tokens + elapsed * self.rate)
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
            time.sleep(0.01)


# ---------------------------------------------------------------------------
# Rolling + cumulative stats.
# ---------------------------------------------------------------------------
class Stats:
    def __init__(self):
        self.lock = threading.Lock()
        self.total_requests = 0
        self.total_errors = 0
        self.total_status = Counter()
        self.window_latencies = []
        self.window_status = Counter()
        self.window_errors = 0

    def record(self, latency_ms, status_code, error):
        with self.lock:
            self.total_requests += 1
            self.total_status[status_code] += 1
            self.window_latencies.append(latency_ms)
            self.window_status[status_code] += 1
            if error:
                self.total_errors += 1
                self.window_errors += 1

    def pop_window(self):
        with self.lock:
            lat, st, err = self.window_latencies, self.window_status, self.window_errors
            self.window_latencies = []
            self.window_status = Counter()
            self.window_errors = 0
            return lat, st, err


def percentile(sorted_values, p):
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    k = (len(sorted_values) - 1) * p
    f = int(k)
    c = min(f + 1, len(sorted_values) - 1)
    if f == c:
        return sorted_values[f]
    d0 = sorted_values[f] * (c - k)
    d1 = sorted_values[c] * (k - f)
    return d0 + d1


# ---------------------------------------------------------------------------
# HTTP actions against the checkout-java tier.
# ---------------------------------------------------------------------------
def _timed_request(fn):
    t0 = time.monotonic()
    try:
        resp = fn()
        latency_ms = (time.monotonic() - t0) * 1000.0
        error = resp.status_code >= 500
        return latency_ms, resp.status_code, error, resp
    except requests.RequestException:
        latency_ms = (time.monotonic() - t0) * 1000.0
        return latency_ms, 0, True, None


def create_order(session, base_url, recent_ids, ids_lock):
    payload = {"customerId": pick_customer_id(), "sku": pick_sku(), "qty": pick_qty()}
    latency_ms, status, error, resp = _timed_request(
        lambda: session.post(f"{base_url}/api/orders", json=payload, timeout=15)
    )
    if resp is not None and status == 201:
        try:
            order_id = resp.json().get("id")
        except ValueError:
            order_id = None
        if order_id is not None:
            with ids_lock:
                recent_ids.append(order_id)
                if len(recent_ids) > 200:
                    del recent_ids[: len(recent_ids) - 200]
    return latency_ms, status, error


def recent_orders(session, base_url):
    latency_ms, status, error, _ = _timed_request(
        lambda: session.get(f"{base_url}/api/orders/recent", params={"limit": 20}, timeout=10)
    )
    return latency_ms, status, error


def get_order(session, base_url, recent_ids, ids_lock):
    with ids_lock:
        order_id = random.choice(recent_ids) if recent_ids else None
    if order_id is None:
        # No orders created yet - fall back to a safe read-only action.
        return recent_orders(session, base_url)
    latency_ms, status, error, _ = _timed_request(
        lambda: session.get(f"{base_url}/api/orders/{order_id}", timeout=10)
    )
    return latency_ms, status, error


def revenue_report(session, base_url):
    latency_ms, status, error, _ = _timed_request(
        lambda: session.get(f"{base_url}/api/reports/revenue", timeout=20)
    )
    return latency_ms, status, error


ACTIONS = ["create", "recent", "get", "revenue"]
WEIGHTS = [0.70, 0.15, 0.10, 0.05]


def worker_loop(base_url, limiter, stats, stop_event, deadline, recent_ids, ids_lock):
    session = requests.Session()
    while not stop_event.is_set():
        if deadline is not None and time.monotonic() >= deadline:
            break
        limiter.acquire()
        if stop_event.is_set():
            break
        action = random.choices(ACTIONS, weights=WEIGHTS, k=1)[0]
        if action == "create":
            latency_ms, status, error = create_order(session, base_url, recent_ids, ids_lock)
        elif action == "recent":
            latency_ms, status, error = recent_orders(session, base_url)
        elif action == "get":
            latency_ms, status, error = get_order(session, base_url, recent_ids, ids_lock)
        else:
            latency_ms, status, error = revenue_report(session, base_url)
        stats.record(latency_ms, status, error)


def reporter_loop(stats, stop_event, start_time):
    while not stop_event.wait(10):
        lat, st, err = stats.pop_window()
        lat_sorted = sorted(lat)
        n = len(lat_sorted)
        rps = n / 10.0
        p50 = percentile(lat_sorted, 0.50)
        p95 = percentile(lat_sorted, 0.95)
        p99 = percentile(lat_sorted, 0.99)
        hist = ", ".join(f"{code}:{count}" for code, count in sorted(st.items(), key=lambda kv: str(kv[0])))
        elapsed = time.monotonic() - start_time
        print(
            f"[{elapsed:8.1f}s] total={stats.total_requests:7d} req/s={rps:6.1f} "
            f"p50={p50:7.1f}ms p95={p95:7.1f}ms p99={p99:7.1f}ms "
            f"window_errors={err} status={{{hist}}}",
            flush=True,
        )


def print_final_summary(stats, start_time):
    elapsed = time.monotonic() - start_time
    avg_rps = stats.total_requests / elapsed if elapsed > 0 else 0.0
    hist = ", ".join(
        f"{code}:{count}" for code, count in sorted(stats.total_status.items(), key=lambda kv: str(kv[0]))
    )
    print("\n" + "=" * 72)
    print("loadgen FINAL SUMMARY")
    print(f"  elapsed:        {elapsed:.1f}s")
    print(f"  total requests: {stats.total_requests}")
    print(f"  avg req/s:      {avg_rps:.2f}")
    print(f"  total errors:   {stats.total_errors}")
    print(f"  status codes:   {{{hist}}}")
    print("=" * 72, flush=True)


# ---------------------------------------------------------------------------
# Fault injection helpers (used only by --scenario; see also faultctl.sh for
# manual control). Payload key casing intentionally differs per tier:
#   java:   {"mode": ..., "rate": ..., "slowMs": ...}   (AdminController body.get("slowMs"))
#   python: {"mode": ..., "rate": ..., "slow_ms": ...}  (app.py update_fault_state(slow_ms=...))
# ---------------------------------------------------------------------------
def set_fault(base_url, key_style, mode, rate=None, slow_ms=None):
    body = {}
    if mode is not None:
        body["mode"] = mode
    if rate is not None:
        body["rate"] = rate
    if slow_ms is not None:
        body["slowMs" if key_style == "camel" else "slow_ms"] = slow_ms
    try:
        requests.post(f"{base_url}/api/admin/fault", json=body, timeout=5)
    except requests.RequestException as exc:
        print(f"[scenario] WARNING: failed to set fault on {base_url}: {exc}", flush=True)


def clear_faults(java_url, python_url):
    set_fault(java_url, "camel", "none", rate=0.0)
    set_fault(python_url, "snake", "none", rate=0.0)


def scenario_loop(java_url, python_url, stop_event):
    clear_faults(java_url, python_url)
    for phase in SCENARIO_PHASES:
        if stop_event.is_set():
            return
        tier = phase["tier"]
        if tier == "java":
            set_fault(java_url, "camel", phase["mode"], phase["rate"], phase["slow_ms"])
            set_fault(python_url, "snake", "none", rate=0.0)
        elif tier == "python":
            set_fault(python_url, "snake", phase["mode"], phase["rate"], phase["slow_ms"])
            set_fault(java_url, "camel", "none", rate=0.0)
        else:
            clear_faults(java_url, python_url)

        print(
            "\n"
            + "#" * 72
            + f"\n# SCENARIO PHASE: {phase['label']}"
            + (f" (tier={tier}, fault_rate={phase['rate']}, slow_ms={phase['slow_ms']})" if tier else "")
            + f"\n# duration: {'until stopped' if phase['duration'] == 0 else str(phase['duration']) + 's'}"
            + "\n" + "#" * 72,
            flush=True,
        )

        if phase["duration"] == 0:
            return  # final phase: stays "normal" until the process is stopped
        stop_event.wait(phase["duration"])


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def parse_args():
    parser = argparse.ArgumentParser(description="dt-demo-app load generator")
    parser.add_argument("--rate", type=float, default=5.0, help="requests/sec, aggregate across all workers (default 5)")
    parser.add_argument("--workers", type=int, default=8, help="worker thread count (default 8)")
    parser.add_argument("--duration", type=int, default=0, help="seconds to run, 0 = forever (default 0)")
    parser.add_argument("--target", default="http://localhost:8080", help="checkout-java base URL (default http://localhost:8080)")
    parser.add_argument(
        "--pricing-target",
        default="http://localhost:8000",
        help="pricing-python base URL, used ONLY for --scenario fault injection (default http://localhost:8000)",
    )
    parser.add_argument(
        "--scenario",
        action="store_true",
        help="run the scripted demo fault sequence instead of pure steady-state traffic (ignores --duration)",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    stop_event = threading.Event()

    def handle_signal(signum, _frame):
        print(f"\n[loadgen] received signal {signum}, shutting down...", flush=True)
        stop_event.set()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    stats = Stats()
    recent_ids = []
    ids_lock = threading.Lock()
    limiter = RateLimiter(args.rate)
    start_time = time.monotonic()
    deadline = start_time + args.duration if args.duration > 0 else None

    print(
        f"[loadgen] target={args.target} pricing_target={args.pricing_target} "
        f"rate={args.rate}/s workers={args.workers} "
        f"duration={'forever' if args.duration == 0 else str(args.duration) + 's'} "
        f"scenario={args.scenario}",
        flush=True,
    )

    workers = [
        threading.Thread(
            target=worker_loop,
            args=(args.target, limiter, stats, stop_event, deadline, recent_ids, ids_lock),
            daemon=True,
        )
        for _ in range(args.workers)
    ]
    for w in workers:
        w.start()

    reporter = threading.Thread(target=reporter_loop, args=(stats, stop_event, start_time), daemon=True)
    reporter.start()

    if args.scenario:
        scenario_thread = threading.Thread(
            target=scenario_loop, args=(args.target, args.pricing_target, stop_event), daemon=True
        )
        scenario_thread.start()

    try:
        if deadline is not None:
            remaining = max(deadline - time.monotonic(), 0)
            stop_event.wait(remaining)
        else:
            while not stop_event.is_set():
                time.sleep(0.5)
    except KeyboardInterrupt:
        pass

    stop_event.set()
    for w in workers:
        w.join(timeout=5)

    print_final_summary(stats, start_time)


if __name__ == "__main__":
    main()
