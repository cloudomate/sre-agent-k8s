# syntax=docker/dockerfile:1
# llama.cpp server with Qwen3.5-4B baked in.
# Model GGUF must be placed in the build context directory.
#
# Build (from directory containing the .gguf file):
#   docker build \
#     -f images/llama-qwen3.Dockerfile \
#     --build-arg MODEL_FILE=Qwen3.5-4B-Q4_0.gguf \
#     -t cr.imys.in/hci/llama-qwen3.5-4b:latest .
#
#   docker push cr.imys.in/hci/llama-qwen3.5-4b:latest

ARG MODEL_FILE=Qwen3.5-4B-Q4_0.gguf

FROM cr.imys.in/hci/llama-server:latest

ARG MODEL_FILE
RUN mkdir -p /models
COPY ${MODEL_FILE} /models/qwen3.5.gguf

CMD ["--model", "/models/qwen3.5.gguf", \
     "--host", "0.0.0.0", \
     "--port", "11434", \
     "--ctx-size", "8192", \
     "--n-predict", "-1", \
     "--embedding", \
     "--parallel", "2", \
     "--log-disable"]
