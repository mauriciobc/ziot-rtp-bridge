FROM python:3.11-slim
RUN mkdir -p /app
COPY ziot_rtp_bridge.py /app/
WORKDIR /app
# /health answers from the first second (the HTTP server binds before
# bring-up), so this works even while cameras are still rendezvousing.
# Port 8085 is the documented default; keep it in sync with the config.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python3", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8085/health', timeout=4)"]
# Runs as root on purpose: the config is a read-only bind mount that owners
# are told to chmod 600 (it holds an account JWT). A non-root USER would
# make a 600 root-owned mount unreadable and strand every deployment that
# followed the README. Drop this only together with a README change to
# chown the mounted file.
# No config is baked in; supply it at run time with
#   -v /host/path/ziot_config.json:/app/ziot_config.json:ro
CMD ["python3", "/app/ziot_rtp_bridge.py", "--config", "/app/ziot_config.json"]
