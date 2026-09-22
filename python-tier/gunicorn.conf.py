"""
Gunicorn config for the pricing-python tier.

This process must be launched as:

    opentelemetry-instrument gunicorn -c gunicorn.conf.py app:app

`opentelemetry-instrument` wraps the gunicorn master process, configures the
global TracerProvider/OTLP exporter, and re-applies auto-instrumentation
(Flask, psycopg2, requests, logging) inside each forked worker. That handshake
works cleanly with the "gthread" worker class used here (each worker is a
plain forked process using real OS threads, no greenlet/eventlet
monkey-patching to fight with), so no extra post_fork hook is needed in this
file.

NOTE: `threads` only has an effect under worker_class="gthread" - gunicorn
silently ignores it for the default "sync" worker class, so this must stay
"gthread" for the configured concurrency to be real.
"""

bind = "0.0.0.0:8000"
workers = 3
worker_class = "gthread"
threads = 4
timeout = 60

accesslog = "-"
errorlog = "-"
