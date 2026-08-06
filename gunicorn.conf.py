# Loopback-only: gunicorn is fronted by Nginx/CloudPanel for TLS and
# security headers. Was previously 0.0.0.0:8000, directly exposing the
# app on the public internet - rebound live on the VPS 2026-08-05 but
# never committed until now (see PR #45).
workers = 2
worker_class = "uvicorn.workers.UvicornWorker"
bind = "127.0.0.1:8000"
timeout = 180
keepalive = 5
# "debug" was a leftover from initial setup: reviewed 2026-08-06, it does not
# emit request bodies/headers (those are gated separately by the app's own
# settings.log_level, default INFO) but does dump full gunicorn config + extra
# worker-lifecycle noise on every boot/reload, inflating gunicorn_error.log
# for no operational benefit. "info" is the standard prod default.
loglevel = "info"
errorlog = "/home/pdfconverterai-api/htdocs/api.pdfconverterai.com/logs/gunicorn_error.log"
accesslog = "/home/pdfconverterai-api/htdocs/api.pdfconverterai.com/logs/gunicorn_access.log"
access_log_format = '%(h)s:%(p)s - "%(r)s" %(s)s'