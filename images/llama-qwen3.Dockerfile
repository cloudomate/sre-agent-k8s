# syntax=docker/dockerfile:1
# llama.cpp server with Qwen3.5-4B (Q4_K_M) baked in.
# Model is downloaded from the Ollama registry during build — no manual download needed.
#
# Build (run from project root):
#   docker buildx build \
#     --platform linux/amd64,linux/arm64 \
#     -f images/llama-qwen3.Dockerfile \
#     -t cr.imys.in/hci/llama-qwen3.5-4b:latest \
#     --push .

# ── download model from Ollama registry ─────────────────────────────────────
FROM ubuntu:24.04 AS downloader
RUN apt-get update && apt-get install -y --no-install-recommends curl jq ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /models && \
    MANIFEST=$(curl -fsSL https://registry.ollama.ai/v2/library/qwen3.5/manifests/4b) && \
    DIGEST=$(echo "$MANIFEST" | jq -r '.layers[] | select(.mediaType == "application/vnd.ollama.image.model") | .digest') && \
    curl -fSL -o /models/qwen3.5.gguf "https://registry.ollama.ai/v2/library/qwen3.5/blobs/$DIGEST"

# ── final image ─────────────────────────────────────────────────────────────
FROM cr.imys.in/hci/llama-server:latest

RUN mkdir -p /models
COPY --from=downloader /models/qwen3.5.gguf /models/qwen3.5.gguf

CMD ["--model", "/models/qwen3.5.gguf", \
     "--host", "0.0.0.0", \
     "--port", "11434", \
     "--ctx-size", "8192", \
     "--n-predict", "-1", \
     "--embedding", \
     "--parallel", "2", \
     "--log-disable"]
