# syntax=docker/dockerfile:1
# llama.cpp server with Qwen3-4B-Instruct-2507 Q5_K_L baked in.
# Model is COPYed from the build context — no HuggingFace download needed.
#
# Build (run from project root after placing the GGUF next to this file):
#   docker buildx build \
#     --platform linux/amd64,linux/arm64 \
#     -f images/llama-qwen3.Dockerfile \
#     -t cr.imys.in/hci/llama-qwen3-4b:latest \
#     --push \
#     /tmp/llama-build/

FROM cr.imys.in/hci/llama-server:latest

RUN mkdir -p /models
COPY Qwen3-4B-Instruct-2507-Q5_K_L.gguf /models/qwen3.gguf

CMD ["--model", "/models/qwen3.gguf", \
     "--host", "0.0.0.0", \
     "--port", "11434", \
     "--ctx-size", "8192", \
     "--n-predict", "-1", \
     "--embedding", \
     "--parallel", "2", \
     "--log-disable"]
