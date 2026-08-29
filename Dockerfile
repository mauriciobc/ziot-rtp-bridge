FROM python:3.11-slim
RUN mkdir -p /app
COPY ziot_rtp_bridge.py /app/
COPY ziot_config.json /app/
WORKDIR /app
CMD ["python3", "/app/ziot_rtp_bridge.py", "--config", "/app/ziot_config.json"]
