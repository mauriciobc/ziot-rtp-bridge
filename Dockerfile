FROM python:3.11-slim
RUN mkdir -p /app
COPY ziot_rtp_bridge.py /app/
WORKDIR /app
# Port the healthcheck probes. Keep in sync with the config's "port" via
#   -e BRIDGE_PORT=9090
# when the deployment does not use the 8085 default.
ENV BRIDGE_PORT=8085
# /health answers from the first second (the HTTP server binds before
# bring-up), so this works even while cameras are still rendezvousing.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python3", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%d/health' % int(os.environ.get('BRIDGE_PORT', '8085')), timeout=4)"]
# Runs as root on purpose: the config is a read-only bind mount that owners
# are told to chmod 600 (it holds an account JWT). A non-root USER would
# make a 600 root-owned mount unreadable and strand every deployment that
# followed the README. Drop this only together with a README change to
# chown the mounted file.
# No config is baked in; supply it at run time with
#   -v /host/path/ziot_config.json:/app/ziot_config.json:ro
CMD ["python3", "/app/ziot_rtp_bridge.py", "--config", "/app/ziot_config.json"]
