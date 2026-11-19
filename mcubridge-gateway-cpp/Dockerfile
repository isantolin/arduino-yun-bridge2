# syntax=docker/dockerfile:1.7
# Multi-stage production OCI image for the standalone Pigweed Linux Gateway
# (ADR-0003 / ADR-0007).
#
# Build from repository root (required for third_party/pigweed and tools/protocol):
#   docker build -f mcubridge-gateway-cpp/Dockerfile -t mcubridge-gateway:latest .

FROM debian:bookworm-slim AS builder

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential \
      cmake \
      libssl-dev \
      ninja-build \
      protobuf-compiler \
      python3 \
      python3-venv \
    && rm -rf /var/lib/apt/lists/*

# Match repository-pinned Pigweed and Protobuf codegen versions (pyproject.toml).
RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir \
      "pigweed==0.0.24" \
      "protobuf==5.28.3"

ENV PATH="/opt/venv/bin:${PATH}"

WORKDIR /src
COPY third_party/pigweed /src/third_party/pigweed
COPY tools/protocol /src/tools/protocol
COPY mcubridge-gateway-cpp /src/mcubridge-gateway-cpp

RUN cmake -S /src/mcubridge-gateway-cpp \
      -B /tmp/build-gateway \
      -G Ninja \
      -DCMAKE_BUILD_TYPE=Release \
    && cmake --build /tmp/build-gateway \
    && cmake --install /tmp/build-gateway --prefix /out \
    && strip --strip-unneeded /out/bin/mcubridge-gateway-cpp

FROM debian:bookworm-slim AS runtime

ARG IMAGE_SOURCE="https://github.com/isantolin/arduino-yun-bridge2"
LABEL org.opencontainers.image.title="mcubridge-gateway-cpp" \
      org.opencontainers.image.description="Standalone Pigweed C++20 mTLS 1.3 Gateway for Arduino MCU Bridge 2" \
      org.opencontainers.image.source="${IMAGE_SOURCE}" \
      org.opencontainers.image.licenses="GPL-2.0-or-later"

ENV DEBIAN_FRONTEND=noninteractive \
    MCUBRIDGE_GATEWAY_PORT=8443

RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates \
      libssl3 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 10001 mcubridge \
    && useradd --system --uid 10001 --gid mcubridge \
      --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin mcubridge \
    && install -d -o root -g mcubridge -m 0750 /etc/mcubridge-gateway \
    && install -d -o mcubridge -g mcubridge -m 0700 /var/lib/mcubridge-gateway

COPY --from=builder /out/bin/mcubridge-gateway-cpp /usr/local/bin/mcubridge-gateway-cpp

USER mcubridge:mcubridge
WORKDIR /var/lib/mcubridge-gateway

EXPOSE 8443/tcp
STOPSIGNAL SIGTERM

# Verify the gateway TCP listener socket is active in TCP_LISTEN (0x0A) without
# triggering noisy unauthenticated TLS handshake failures in the gateway logs.
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD sh -c 'hex=$(printf "%04X" "${MCUBRIDGE_GATEWAY_PORT:-8443}"); grep -qis ":${hex} .* 0A " /proc/net/tcp /proc/net/tcp6'

ENTRYPOINT ["/usr/local/bin/mcubridge-gateway-cpp"]
CMD ["--cert", "/etc/mcubridge-gateway/gateway.crt", \
     "--key", "/etc/mcubridge-gateway/gateway.key", \
     "--client-ca", "/etc/mcubridge-gateway/clients-ca.crt", \
     "--acl", "/etc/mcubridge-gateway/gateway.acl", \
     "--staging-dir", "/var/lib/mcubridge-gateway", \
     "--bind", "*", \
     "--port", "8443"]
