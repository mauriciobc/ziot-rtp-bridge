FROM python:3.11-slim
RUN mkdir -p /app
COPY ziot_rtp_bridge.py /app/
WORKDIR /app
# No config is baked in; supply it at run time with
#   -v /host/path/ziot_config.json:/app/ziot_config.json:ro
CMD ["python3", "/app/ziot_rtp_bridge.py", "--config", "/app/ziot_config.json"]
