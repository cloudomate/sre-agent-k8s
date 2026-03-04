# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

SRE Agent is an LLM-powered automated incident investigation system for Hyperconverged Infrastructure (HCI) platforms. It uses a ReAct loop (Think → Act → Observe) with any OpenAI-compatible LLM to investigate alerts, gather evidence natively via the Kubernetes API and Prometheus, and generate structured incident reports. It runs as a Kubernetes-native workload using a ServiceAccount — no kubeconfig or kubectl binary required.

## Running the Project

### Kubernetes — Kustomize

```bash
# 1. Edit deploy/kustomize/configmap.yaml — set LLM_BASE_URL and PROMETHEUS_URL
# 2. Edit deploy/kustomize/secret.yaml    — set LLM_API_KEY (base64), SLACK_WEBHOOK_URL, etc.
# 3. Build and push your image, then set it in deploy/kustomize/deployment.yaml
kubectl apply -k deploy/kustomize/
```

### Kubernetes — Helm (external LLM, recommended)

```bash
helm install sre-agent oci://cr.imys.in/hci/sre-agent-k8s/sre-agent \
  --version 0.3.0 -n sre-agent --create-namespace \
  --set llamacpp.enabled=false \
  --set llm.baseUrl=https://your-api.example.com/v1 \
  --set llm.apiKey=sk-your-key \
  --set llm.model=your-model-id \
  --set report.channels="stdout\,file\,email" \
  --set smtp.host=mail.example.com --set smtp.port=587
```

### Kubernetes — Helm (local llama.cpp)

```bash
helm install sre-agent oci://cr.imys.in/hci/sre-agent-k8s/sre-agent \
  --version 0.3.0 -n sre-agent --create-namespace \
  --set llm.apiKey=llamacpp \
  --set report.channels="stdout\,file\,email" \
  --set smtp.host=192.168.4.102 --set smtp.port=1025 --set smtp.tls=false

# Upgrade after changes
helm upgrade sre-agent oci://cr.imys.in/hci/sre-agent-k8s/sre-agent \
  --version 0.3.0 -n sre-agent --reuse-values
```

Note: escape commas in `--set` values with `\,` (Helm parses commas as value separators).

### Local development

```bash
pip install -r requirements.txt
cp .env.example .env
# Set K8S_IN_CLUSTER=false in .env — uses ~/.kube/config
python -m uvicorn sre_agent.main:app --host 0.0.0.0 --port 8080 --workers 1
```

**Important**: Use `--workers 1` — the LLM client is not thread-safe for concurrent investigations.

### Testing an investigation

```bash
curl -X POST http://localhost:8080/incidents \
  -H "Content-Type: application/json" \
  -d '{"service": "my-service", "namespace": "default", "error_type": "CrashLoopBackOff", "severity": "P2"}'

# Poll for results
curl http://localhost:8080/incidents/{incident_id}
```

There are no automated tests or linting configurations in this project.

## Architecture

### Request flow

```text
K8s Warning events (watcher.py) ──┐
Webhook/API (main.py)             ├──► SREAgent ReAct loop (agent.py)
Alertmanager webhook              ┘         ↓
                                    Tool calls (tools.py)
                                    Kubernetes Python client (in-cluster)
                                    Prometheus HTTP queries
                                    Loki / socket / HTTP checks
                                            ↓
                                    Runbook RAG lookup (runbook.py)
                                            ↓
                                    Report generation + delivery (reporter.py)
                                    stdout | file | slack | webhook | email | sms | whatsapp
```

### Key components

**[sre_agent/agent.py](sre_agent/agent.py)** — Core ReAct investigation engine. Uses `openai.OpenAI(base_url=..., api_key=...)` to talk to any OpenAI-compatible endpoint. Runs up to `AGENT_MAX_STEPS` (default 12) iterations of: model reasons → calls tools → observes results. OpenAI protocol requires `tool_call_id` on tool result messages and JSON-string arguments — both handled here.

**[sre_agent/tools.py](sre_agent/tools.py)** — All 12 diagnostic tools. Uses the `kubernetes` Python client for all K8s operations (pods, logs, events, deployments, resource quotas, KubeVirt VMIs). Initialises with `load_incluster_config()` when `K8S_IN_CLUSTER=true`, falls back to `load_kube_config()` for local dev. Non-K8s diagnostic commands (`df`, `free`, `ping`, etc.) still use a whitelisted subprocess path. kubectl is not installed in the image.

**[sre_agent/watcher.py](sre_agent/watcher.py)** — Background Kubernetes event watcher. Streams `Warning` events cluster-wide via `kubernetes.watch.Watch()` and auto-triggers investigations. Ignores `kube-system`, `kube-public`, `kube-node-lease`, `sre-agent` namespaces. 15-minute cooldown per service prevents alert storms. 30-second grouping window buffers events by namespace — if >=3 services fire in the same namespace, they dispatch as a single grouped investigation (cascading failure). P1 events (OOMKilling, NodeNotReady, NetworkNotReady) flush the group immediately. Controlled by `WATCH_EVENTS`, `WATCH_COOLDOWN_SECONDS`, and `WATCH_GROUP_WINDOW` env vars.

**[sre_agent/main.py](sre_agent/main.py)** — FastAPI server with in-memory incident store. Investigations run in a `ThreadPoolExecutor` (max 2 workers). Returns `incident_id` immediately; clients poll `GET /incidents/{id}`. Starts the event watcher thread on startup.

**[sre_agent/runbook.py](sre_agent/runbook.py)** — 10 built-in runbooks plus 47 JSON runbooks from `RUNBOOK_DIR` (57 total). Semantic search via the OpenAI embeddings API (`LLM_EMBED_MODEL`); falls back to keyword scoring if embeddings unavailable. JSON runbooks support both `summary` (ismp/uphci style) and `symptoms` (rook-ceph/metallb style) fields.

**[sre_agent/reporter.py](sre_agent/reporter.py)** — Formats and delivers reports to: Slack (Block Kit), webhook URL, file (JSON + Markdown), stdout, email (SMTP), SMS (Twilio), WhatsApp (Twilio). `SMTP_TLS=false` skips STARTTLS for plain relays (e.g. MailHog on port 1025).

**[sre_agent/config.py](sre_agent/config.py)** — All configuration via environment variables. Key settings: `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`, `K8S_IN_CLUSTER`, `METRICS_BACKEND`, `PROMETHEUS_URL`, `LOG_BACKEND`, `REPORT_CHANNELS`, `SMTP_TLS`, `WATCH_EVENTS`.

## Kubernetes setup

**Manifests** live in two forms — both are kept in sync:

- `deploy/kustomize/` — plain YAML, apply with `kubectl apply -k deploy/kustomize/`
- `deploy/helm/sre-agent/` — Helm chart, install with `helm install sre-agent deploy/helm/sre-agent/ -n sre-agent --create-namespace`

**RBAC** ([deploy/kustomize/rbac.yaml](deploy/kustomize/rbac.yaml)): ClusterRole with read-only access to pods, pod logs, events, nodes, deployments, services, resource quotas, KubeVirt VMIs, and metrics-server resources.

**External access** ([deploy/kustomize/service.yaml](deploy/kustomize/service.yaml)): Two services — `sre-agent` (ClusterIP, internal on port 8080) and `sre-agent-external` (LoadBalancer, external on port 80 → 8080). Optional Ingress in [deploy/kustomize/ingress.yaml](deploy/kustomize/ingress.yaml) or via `ingress.enabled=true` in Helm values.

**LLM inference** — Two modes: (1) **External API** — set `llamacpp.enabled=false` in Helm and configure `LLM_BASE_URL` + `LLM_API_KEY` to any OpenAI-compatible endpoint. No local model resources needed. (2) **In-cluster llama.cpp** ([deploy/kustomize/llamacpp.yaml](deploy/kustomize/llamacpp.yaml) / Helm `llamacpp.enabled=true`): llama.cpp server (b8196) with model baked into image (`cr.imys.in/hci/llama-qwen3.5-4b:latest`). Built from `images/llama-server.Dockerfile` (base) + `images/llama-qwen3.Dockerfile` (model layer, uses `MODEL_FILE` build arg to COPY a local GGUF). Key args: `--ctx-size 16384 --parallel 2`.

**Config** ([deploy/kustomize/configmap.yaml](deploy/kustomize/configmap.yaml)): Non-secret config. **Secrets** ([deploy/kustomize/secret.yaml](deploy/kustomize/secret.yaml)): `LLM_API_KEY`, `SLACK_WEBHOOK_URL`, `REPORT_WEBHOOK_URL`, `TWILIO_*`, `SMTP_PASSWORD` — base64-encoded. In Helm, pass these via `--set` or a values override file.

## LLM configuration

The agent supports any OpenAI-compatible endpoint via `LLM_BASE_URL` + `LLM_API_KEY`. For production, use an external API with `llamacpp.enabled=false` in Helm to avoid consuming cluster resources.

| Endpoint | LLM_BASE_URL | LLM_API_KEY | llamacpp.enabled |
| --- | --- | --- | --- |
| External API | `https://your-api.example.com/v1` | `sk-...` | `false` |
| OpenAI | `https://api.openai.com/v1` | `sk-...` | `false` |
| llama.cpp (in-cluster) | `http://llamacpp.sre-agent.svc.cluster.local:11434/v1` | `llamacpp` | `true` |
| llama.cpp (local dev) | `http://localhost:11434/v1` | `llamacpp` | N/A |
| vLLM | `http://vllm:8000/v1` | `token` | `false` |

Local model: Qwen3.5-4B Q4_0 on llama.cpp b8196 (model ID: `qwen3.5:4b`).

## Runbooks (57 total, baked into image at /app/runbooks)

| File | Count | Coverage |
| --- | --- | --- |
| `kubernetes-core-runbooks.json` | 8 | etcd, apiserver, node NotReady, certs, OOMKill, Flatcar, PVC, CoreDNS |
| `ismp-runbooks.json` | 9 | ISMP platform runbooks (RB-ISMP-001 to 009) |
| `uphci-runbooks.json` | 8 | upHCI platform runbooks (RB-UPHCI-001 to 008) |
| `rook-ceph-runbooks.json` | 6 | Ceph cluster health, OSD, MON, pool full, CSI |
| `kube-prometheus-runbooks.json` | 5 | Prometheus storage, scrape failures, Alertmanager |
| `kubevirt-runbooks.json` | 4 | VM not starting, virt-launcher, live migration |
| `kube-ovn-runbooks.json` | 4 | CNI failure, subnet/IP allocation, OVN controller |
| `metallb-runbooks.json` | 3 | LoadBalancer IP pending, speaker crash, IP conflict |
| Built-in (runbook.py) | 10 | OOMKill, CrashLoop, ImagePull, Pending, NodeNotReady, 5xx, PVC, HCI VM, etcd, cert expiry |

## Incident report schema

The agent outputs structured JSON with: `incident_id`, `severity` (P1/P2/P3), `category` (infra/app/storage/network), `title`, `root_cause`, `evidence[]`, `recommended_actions[]` (with `priority` and `safe_to_automate`), `requires_escalation`, `confidence` (high/medium/low), and investigation metadata. Severity and category are validated; invalid LLM output falls back to the initial alert severity and keyword-based category inference.

## Adding new tools

Add a Python function to [sre_agent/tools.py](sre_agent/tools.py) and register it in `TOOL_REGISTRY` with an OpenAI-style function schema in `TOOL_DEFINITIONS`. The agent will automatically be able to call it.

## Building images

```bash
# Base llama.cpp server (multi-arch, b8196)
docker buildx build --platform linux/amd64,linux/arm64 \
  -f images/llama-server.Dockerfile \
  -t cr.imys.in/hci/llama-server:latest --push .

# Model image (GGUF must be in build context directory)
cd /path/to/models
docker build -f /path/to/images/llama-qwen3.Dockerfile \
  --build-arg MODEL_FILE=Qwen3.5-4B-Q4_0.gguf \
  -t cr.imys.in/hci/llama-qwen3.5-4b:latest .
docker push cr.imys.in/hci/llama-qwen3.5-4b:latest

# SRE Agent (multi-arch)
docker buildx build --platform linux/amd64,linux/arm64 \
  -t cr.imys.in/hci/sre-agent:latest --push .

# Helm chart (OCI registry)
helm package deploy/helm/sre-agent/
helm push sre-agent-*.tgz oci://cr.imys.in/hci/sre-agent-k8s
```
