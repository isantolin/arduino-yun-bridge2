# Multi-stage production container for MCU Bridge Protobuf Gateway
FROM python:3.14-slim AS runtime

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Install runtime dependencies
COPY mcubridge-gateway/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy gateway service and protocol package
COPY mcubridge-gateway/gateway.py ./gateway.py
COPY mcubridge/mcubridge /app/mcubridge

# Expose default gRPC and Prometheus metrics ports
EXPOSE 8443 9100

ENTRYPOINT ["python", "gateway.py"]
CMD ["--host", "0.0.0.0", "--port", "8443", "--metrics-port", "9100"]
