# syntax=docker/dockerfile:1
# llama.cpp server with Qwen3.5-4B (Q5_K_M) baked in.
# Model is downloaded from Hugging Face (unsloth) during build.
#
# Build (run from project root):
#   docker buildx build \
#     --platform linux/amd64,linux/arm64 \
#     -f images/llama-qwen3.Dockerfile \
#     -t cr.imys.in/hci/llama-qwen3.5-4b:latest \
#     --push .

# ── download model from Hugging Face ──────────────────────────────────────
FROM ubuntu:24.04 AS downloader
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /models && \
    curl -fSL -o /models/qwen3.5.gguf \
      "https://huggingface.co/unsloth/Qwen3.5-4B-GGUF/resolve/main/Qwen3.5-4B-Q5_K_M.gguf"

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
