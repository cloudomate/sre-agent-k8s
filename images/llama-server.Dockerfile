# syntax=docker/dockerfile:1
# Minimal llama.cpp server image — multi-arch (linux/amd64 + linux/arm64)
# amd64: uses the pre-built GitHub release binary + bundled shared libs
# arm64: compiles from source natively on Apple Silicon

ARG LLAMA_VERSION=b8182

FROM ubuntu:24.04 AS builder
ARG LLAMA_VERSION
ARG TARGETARCH

RUN apt-get update && apt-get install -y --no-install-recommends \
      curl ca-certificates cmake make g++ git \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /opt/llama && \
    if [ "${TARGETARCH}" = "amd64" ]; then \
      echo "==> Downloading pre-built amd64 binary + libs..." \
      && curl -fsSL \
           "https://github.com/ggerganov/llama.cpp/releases/download/${LLAMA_VERSION}/llama-${LLAMA_VERSION}-bin-ubuntu-x64.tar.gz" \
         | tar -xz --strip-components=1 -C /opt/llama \
      && chmod +x /opt/llama/llama-server; \
    else \
      echo "==> Compiling from source for ${TARGETARCH}..." \
      && git clone --depth 1 --branch "${LLAMA_VERSION}" \
           https://github.com/ggerganov/llama.cpp /src \
      && cmake -S /src -B /src/build \
           -DCMAKE_BUILD_TYPE=Release \
           -DGGML_NATIVE=OFF \
           -DCMAKE_INSTALL_PREFIX=/opt/llama \
      && cmake --build /src/build --target llama-server -j"$(nproc)" \
      && install -m755 /src/build/bin/llama-server /opt/llama/llama-server \
      && rm -rf /src; \
    fi

# ── final: slim runtime ───────────────────────────────────────────────────────
FROM ubuntu:24.04
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/llama /opt/llama

# Make llama-server callable from PATH; shared libs live alongside the binary
ENV LD_LIBRARY_PATH=/opt/llama
RUN ln -s /opt/llama/llama-server /usr/local/bin/llama-server

EXPOSE 11434
ENTRYPOINT ["llama-server"]
