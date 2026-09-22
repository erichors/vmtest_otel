"""
pricing-python: the second tier in the Dynatrace/OpenTelemetry two-tier demo.

This process is meant to be launched as:

    opentelemetry-instrument gunicorn -c gunicorn.conf.py app:app

`opentelemetry-instrument` bootstraps the global TracerProvider and the OTLP/HTTP
exporter (pointed at the local Dynatrace OneAgent EEC endpoint via standard
OTEL_EXPORTER_OTLP_* env vars, e.g. OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=
http://localhost:14499/otlp/v1/traces) and auto-instruments Flask, psycopg2,
requests, and logging BEFORE any of this module's code runs. Because of that,
we must NOT build our own TracerProvider/exporter here - we only grab the
already-configured global tracer and use it to add a few extra manual spans
around business logic, which makes the resulting trace waterfall in Dynatrace
noticeably richer than auto-instrumentation alone would produce.

Dynatrace OneAgent on this host runs infrastructure-only (no deep-code
instrumentation), so this OTel SDK is the only source of spans for this
service - there is no risk of duplicate/competing traces.
"""

import logging
import os
import random
import threading
import time
from contextlib import contextmanager

import psycopg2
import psycopg2.pool
from flask import Flask, jsonify, request
from opentelemetry import trace

# ---------------------------------------------------------------------------
# Logging - structured, one line per request at INFO, ERROR with exc_info on
# failures. OneAgent log monitoring tails stdout for this process even in
# infrastructure-only mode.
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("pricing-python")

# Tracer handle only - created AFTER opentelemetry-instrument has already wired
# up the global TracerProvider. Used exclusively to add child spans below.
tracer = trace.get_tracer(__name__)

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Database connection pool
# ---------------------------------------------------------------------------
DB_HOST = os.environ.get("DB_HOST", "localhost")
DB_PORT = os.environ.get("DB_PORT", "5432")
DB_NAME = os.environ.get("DB_NAME", "dtdemo")
DB_USER = os.environ.get("DB_USER", "dtdemo")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "dtdemo")

connection_pool = psycopg2.pool.ThreadedConnectionPool(
    minconn=1,
    maxconn=10,
    host=DB_HOST,
    port=DB_PORT,
    dbname=DB_NAME,
    user=DB_USER,
    password=DB_PASSWORD,
)


@contextmanager
def get_conn():
    """Borrow a connection from the pool and always return it, even on error.

    psycopg2 is auto-instrumented by opentelemetry-instrumentation-psycopg2,
    so every cursor.execute() call below automatically produces a DB client
    span nested under whichever span (request or manual) is current.

    Route handlers below have several early-return branches (404, the
    injected-error fault, unhandled exceptions) that do not explicitly
    commit or roll back. Without a safety net here, those paths would
    return the connection to the pool while still "idle in transaction" -
    in the worst case (an exception after the inventory FOR UPDATE lock is
    taken) that leaks a held row lock into the next borrower. A rollback()
    is a no-op if the connection isn't in a transaction (e.g. right after a
    normal commit()), so this is safe on every path.
    """
    conn = connection_pool.getconn()
    try:
        yield conn
    finally:
        try:
            conn.rollback()
        except Exception:
            logger.warning("rollback failed while returning pooled connection", exc_info=True)
        connection_pool.putconn(conn)


# ---------------------------------------------------------------------------
# Fault injection state - identical none|slow|error|dbslow contract as the
# Java tier's FaultConfig, so a demo script can drive both tiers together.
# ---------------------------------------------------------------------------
_fault_lock = threading.Lock()
_fault_state = {"mode": "none", "rate": 0.0, "slow_ms": 1500}

VALID_MODES = {"none", "slow", "error", "dbslow"}


def get_fault_state():
    with _fault_lock:
        return dict(_fault_state)


def update_fault_state(mode=None, rate=None, slow_ms=None):
    with _fault_lock:
        if mode is not None:
            _fault_state["mode"] = mode
        if rate is not None:
            _fault_state["rate"] = float(rate)
        if slow_ms is not None:
            _fault_state["slow_ms"] = int(slow_ms)
        return dict(_fault_state)


def _should_fire(state):
    return state["mode"] != "none" and random.random() < state["rate"]


def apply_fault(conn=None):
    """Apply the currently configured fault, if this call's dice roll fires.

    mode=slow    -> sleep in-process (pure latency, no extra DB span)
    mode=error   -> raise, caller maps this to a 500 JSON body
    mode=dbslow  -> run SELECT pg_sleep() on the given connection, producing a
                    slow, easy-to-spot psycopg2 span in the Dynatrace trace
    """
    state = get_fault_state()
    if not _should_fire(state):
        return
    mode = state["mode"]
    if mode == "slow":
        time.sleep(state["slow_ms"] / 1000.0)
    elif mode == "error":
        raise RuntimeError("injected pricing failure")
    elif mode == "dbslow" and conn is not None:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_sleep(%s)", (state["slow_ms"] / 1000.0,))


# ---------------------------------------------------------------------------
# Discount rules
# ---------------------------------------------------------------------------
TIER_DISCOUNT = {"standard": 0.0, "silver": 0.05, "gold": 0.12}
VOLUME_DISCOUNT_50 = 0.10
VOLUME_DISCOUNT_10 = 0.05
MAX_DISCOUNT = 0.25


def compute_discount(tier, qty):
    """Manual child span for the discount business rule - not something
    auto-instrumentation alone would ever surface. Returns the total discount
    as a fraction (e.g. 0.12 for 12%)."""
    with tracer.start_as_current_span("compute_discount") as span:
        tier_pct = TIER_DISCOUNT.get(tier, 0.0)
        if qty >= 50:
            volume_pct = VOLUME_DISCOUNT_50
        elif qty >= 10:
            volume_pct = VOLUME_DISCOUNT_10
        else:
            volume_pct = 0.0
        total_pct = min(tier_pct + volume_pct, MAX_DISCOUNT)

        span.set_attribute("customer.tier", tier)
        span.set_attribute("discount.tier_pct", tier_pct)
        span.set_attribute("discount.volume_pct", volume_pct)
        span.set_attribute("discount.total_pct", total_pct)
        return total_pct


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/health")
def health():
    return jsonify(status="UP", service="pricing-python")


@app.route("/api/pricing/<sku>")
def get_pricing(sku):
    start = time.time()
    qty = request.args.get("qty", default=1, type=int)
    tier = request.args.get("tier", default="standard", type=str)

    try:
        with get_conn() as conn:
            try:
                apply_fault(conn)
            except RuntimeError:
                logger.error("injected pricing failure sku=%s qty=%s", sku, qty, exc_info=True)
                return jsonify(error="injected pricing failure"), 500

            with conn.cursor() as cur:
                cur.execute(
                    "SELECT name, category, base_price FROM products WHERE sku = %s",
                    (sku,),
                )
                product = cur.fetchone()

            if product is None:
                duration_ms = (time.time() - start) * 1000
                logger.info("pricing sku=%s qty=%s duration_ms=%.1f status=404", sku, qty, duration_ms)
                return jsonify(error="product not found"), 404

            name, category, base_price = product
            base_price = float(base_price)

            # Manual child span: inventory check, with on-hand qty as an
            # attribute so a Dynatrace session can correlate low stock with
            # 409 responses in the trace/attribute view.
            with tracer.start_as_current_span("check_inventory") as span:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT qty_on_hand FROM inventory WHERE sku = %s FOR UPDATE",
                        (sku,),
                    )
                    row = cur.fetchone()
                qty_on_hand = row[0] if row else 0
                span.set_attribute("inventory.sku", sku)
                span.set_attribute("inventory.qty_on_hand", qty_on_hand)
                span.set_attribute("inventory.qty_requested", qty)

                if row is None or qty_on_hand < qty:
                    conn.rollback()
                    duration_ms = (time.time() - start) * 1000
                    logger.info("pricing sku=%s qty=%s duration_ms=%.1f status=409", sku, qty, duration_ms)
                    return jsonify(error="out of stock"), 409

            discount_fraction = compute_discount(tier, qty)
            unit_price = round(base_price * (1 - discount_fraction), 2)
            total_price = round(unit_price * qty, 2)

            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE inventory SET qty_on_hand = qty_on_hand - %s WHERE sku = %s",
                    (qty, sku),
                )
            conn.commit()

            duration_ms = (time.time() - start) * 1000
            logger.info("pricing sku=%s qty=%s duration_ms=%.1f status=200", sku, qty, duration_ms)

            return jsonify(
                sku=sku,
                name=name,
                category=category,
                unitPrice=round(unit_price, 2),
                totalPrice=round(total_price, 2),
                # Expressed as a percentage (0-25), e.g. 12.0 means 12% off.
                discountPct=round(discount_fraction * 100, 2),
                qty=qty,
                inStock=True,
            )
    except Exception:
        logger.error("unhandled error pricing sku=%s qty=%s", sku, qty, exc_info=True)
        return jsonify(error="internal pricing error"), 500


@app.route("/api/products")
def list_products():
    category = request.args.get("category")
    limit = request.args.get("limit", default=50, type=int)

    with get_conn() as conn:
        with conn.cursor() as cur:
            if category:
                cur.execute(
                    """
                    SELECT p.sku, p.name, p.category, p.base_price, i.qty_on_hand
                    FROM products p
                    JOIN inventory i ON i.sku = p.sku
                    WHERE p.category = %s
                    ORDER BY p.name
                    LIMIT %s
                    """,
                    (category, limit),
                )
            else:
                cur.execute(
                    """
                    SELECT p.sku, p.name, p.category, p.base_price, i.qty_on_hand
                    FROM products p
                    JOIN inventory i ON i.sku = p.sku
                    ORDER BY p.name
                    LIMIT %s
                    """,
                    (limit,),
                )
            rows = cur.fetchall()

    products = [
        {
            "sku": r[0],
            "name": r[1],
            "category": r[2],
            "basePrice": float(r[3]),
            "qtyOnHand": r[4],
        }
        for r in rows
    ]
    return jsonify(products)


@app.route("/api/inventory/low")
def low_inventory():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT i.sku, p.name, i.qty_on_hand, i.reorder_level
                FROM inventory i
                JOIN products p ON p.sku = i.sku
                WHERE i.qty_on_hand < i.reorder_level
                ORDER BY i.qty_on_hand ASC
                """
            )
            rows = cur.fetchall()

    return jsonify(
        [
            {"sku": r[0], "name": r[1], "qtyOnHand": r[2], "reorderLevel": r[3]}
            for r in rows
        ]
    )


@app.route("/api/admin/fault", methods=["GET"])
def get_fault():
    return jsonify(get_fault_state())


@app.route("/api/admin/fault", methods=["POST"])
def set_fault():
    body = request.get_json(silent=True) or {}
    mode = body.get("mode")
    if mode is not None and mode not in VALID_MODES:
        return jsonify(error="mode must be one of {}".format(sorted(VALID_MODES))), 400
    new_state = update_fault_state(mode=mode, rate=body.get("rate"), slow_ms=body.get("slow_ms"))
    return jsonify(new_state)


if __name__ == "__main__":
    # Local dev only. In the target deployment this app is served by gunicorn
    # under opentelemetry-instrument (see gunicorn.conf.py) - this branch
    # never runs there.
    app.run(host="0.0.0.0", port=8000)
